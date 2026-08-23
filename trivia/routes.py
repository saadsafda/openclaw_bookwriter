"""Trivia & Facts book generator — Flask routes and job runner.

Registered onto the main app via register(app), the same way publications.py,
review_automation.py and book_editor.py attach their own routes. Everything
here lives under /trivia and /api/trivia/* and shares no state with the prose
book pipeline in app.py.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from flask import abort, jsonify, render_template, request, send_file

import db as bookdb
from print_hygiene import strip_ai_report
from . import edit as editor
from . import export as exporter
from . import image_edit as imgedit
from . import outline as outline_parser
from . import pipeline
from .engine import BookConfig, RawOutputCache, TriviaError, UsageLedger, ValidationGateError

# Two levels up: this file lives in <project>/trivia/, and outputs belong
# beside the other project data, not inside the package.
ROOT_DIR = Path(__file__).resolve().parent.parent
TRIVIA_OUTPUT_DIR = ROOT_DIR / "trivia_outputs"
TRIVIA_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Uploaded outlines are parsed and discarded; only the extracted structure is
# kept, so this is a scratch area rather than durable storage.
UPLOAD_DIR = TRIVIA_OUTPUT_DIR / "_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_LOG_LINES = 600


@dataclass
class TriviaJob:
    id: str
    config: BookConfig
    status: str = "queued"          # queued | running | done | error | stopped
    stage: str = ""
    progress: float = 0.0
    logs: list[str] = field(default_factory=list)
    error: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    collisions: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    stop_requested: bool = False
    created_at: float = field(default_factory=time.time)

    def log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        self.logs.append(line)
        if len(self.logs) > MAX_LOG_LINES:
            del self.logs[: len(self.logs) - MAX_LOG_LINES]

    def to_status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 4),
            "title": self.config.book_title,
            "topic": self.config.topic,
            "error": self.error,
            "outputs": dict(self.outputs),
            "warnings": list(self.warnings),
            "collisions": self.collisions[:50],
            "usage": dict(self.usage),
            "logs": self.logs[-120:],
            # The resolve button posts the browser's chapter rows back as
            # config overrides. Without the real config to load into the form
            # first, those rows are whatever was last typed -- possibly another
            # book's -- and resolving would silently rewrite this book's quotas.
            "config": self.config.to_dict(),
        }


JOBS: dict[str, TriviaJob] = {}
JOBS_LOCK = threading.Lock()


def _job_dir(job_id: str) -> Path:
    return TRIVIA_OUTPUT_DIR / job_id


def _safe_stem(title: str) -> str:
    keep = [c if c.isalnum() or c in " -_" else "" for c in (title or "trivia")]
    stem = "".join(keep).strip().replace(" ", "_") or "trivia_book"
    return stem[:60]


def _run_build(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return

    job.status = "running"
    job.stage = "starting"
    bookdb.update_trivia_book(job_id, status="running", stage="starting")

    out_dir = _job_dir(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _log(message: str) -> None:
        job.log(message)

    def _progress(stage: str, pct: float) -> None:
        job.stage = stage
        job.progress = max(0.0, min(1.0, pct))
        bookdb.update_trivia_book(job_id, stage=stage, progress=job.progress)

    builder: Optional[pipeline.TriviaBuilder] = None
    try:
        builder = pipeline.TriviaBuilder(
            job.config,
            log=_log,
            progress=_progress,
            should_stop=lambda: job.stop_requested,
        )
        book = builder.build(out_dir)
        job.warnings = list(book.warnings)
        job.collisions = [c.to_dict() for c in builder.collisions]
        job.usage = dict(book.usage)

        stem = _safe_stem(job.config.book_title)

        json_path = pipeline.write_json(book, out_dir / f"{stem}.json")
        job.outputs["json"] = str(json_path)
        _log(f"Wrote JSON source of truth: {json_path.name}")

        md_path = exporter.write_markdown(book, out_dir / f"{stem}.md")
        job.outputs["markdown"] = str(md_path)
        _log(f"Wrote Markdown: {md_path.name}")

        docx_path = exporter.build_docx(book, out_dir / f"{stem}.docx")
        job.outputs["docx"] = str(docx_path)
        _log(f"Wrote DOCX manuscript: {docx_path.name}")

        _log("Verifying every image is 300 DPI with no AI metadata")
        image_problems = exporter.verify_print_images(book, out_dir)
        if image_problems:
            for problem in image_problems:
                _log(f"WARNING: print check — {problem}")
            job.warnings.extend(f"Print check — {p}" for p in image_problems)
        else:
            _log("Print check passed: all images 300 DPI, metadata clean")

        _progress("formatting", 0.95)
        try:
            kdp = exporter.build_kdp_files(book, docx_path, out_dir)
            job.outputs["kindle"] = kdp["kindle"]
            job.outputs["paperback"] = kdp["paperback"]
            _log(
                f"KDP files ready (est. {kdp['estimated_pages']} pages, "
                f"inside margin {kdp['inside_margin_in']} in)"
            )
        except Exception as exc:
            # Print formatting is a post-process; a failure there must not
            # discard the generated content.
            _log(f"WARNING: KDP formatting failed: {exc}")
            job.warnings.append(f"KDP formatting failed: {exc}")

        job.status = "done"
        job.stage = "done"
        job.progress = 1.0
        bookdb.update_trivia_book(
            job_id,
            status="done",
            stage="done",
            progress=1.0,
            json_path=job.outputs.get("json", ""),
            markdown_path=job.outputs.get("markdown", ""),
            docx_path=job.outputs.get("docx", ""),
            kindle_path=job.outputs.get("kindle", ""),
            paperback_path=job.outputs.get("paperback", ""),
            warnings_json=json.dumps(job.warnings),
            usage_json=json.dumps(job.usage),
        )
        _log("Build complete.")

    except ValidationGateError as exc:
        job.status = "error"
        job.stage = "validation-failed"
        job.error = str(exc)
        if builder is not None:
            job.collisions = [c.to_dict() for c in builder.collisions]
            # Spend up to the point of failure is real; keep it on the ledger.
            job.usage = builder.ledger.to_dict()
            # The gate rejects a handful of items out of hundreds that were
            # already paid for. Persisting the book here is what makes the
            # failure recoverable: /resolve reloads this JSON and fixes only
            # the colliding facts instead of rebuilding from zero.
            try:
                stem = _safe_stem(job.config.book_title)
                draft_path = pipeline.write_json(
                    builder.book, out_dir / f"{stem}.json"
                )
                job.outputs["json"] = str(draft_path)
                _log(
                    f"Saved the blocked draft to {draft_path.name} — its "
                    "content is intact and can be resolved without "
                    "regenerating the book."
                )
            except Exception as save_exc:
                _log(f"WARNING: could not save blocked draft: {save_exc}")
        _log(f"BLOCKED: {exc}")
        bookdb.update_trivia_book(
            job_id, status="error", stage="validation-failed", error=str(exc),
            json_path=job.outputs.get("json", ""),
            usage_json=json.dumps(job.usage),
        )
    except TriviaError as exc:
        job.status = "stopped" if job.stop_requested else "error"
        job.stage = "stopped" if job.stop_requested else "failed"
        job.error = "" if job.stop_requested else str(exc)
        if builder is not None:
            job.usage = builder.ledger.to_dict()
        _log(str(exc))
        bookdb.update_trivia_book(
            job_id, status=job.status, stage=job.stage, error=job.error,
            usage_json=json.dumps(job.usage),
        )
    except Exception as exc:  # noqa: BLE001 - surface anything else to the UI
        job.status = "error"
        job.stage = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        _log(f"ERROR: {job.error}")
        bookdb.update_trivia_book(
            job_id, status="error", stage="failed", error=job.error
        )


def _load_book_for_edit(book_id: str) -> tuple[dict[str, Any], Path, Any]:
    """Fetch a book row, its JSON path, and the rehydrated book object.

    Raises TriviaError with a caller-friendly message; routes turn that into a
    4xx rather than a 500.
    """
    row = bookdb.get_trivia_book(book_id)
    if not row:
        raise TriviaError("Book not found.")
    json_path = Path(row.get("json_path") or "")
    if not json_path.exists():
        raise TriviaError("This book has no generated content yet.")
    return row, json_path, pipeline.load_json(json_path)


def _save_book(book: Any, json_path: Path) -> None:
    pipeline.write_json(book, json_path)


def _edit_cache_and_ledger(json_path: Path) -> tuple[RawOutputCache, UsageLedger]:
    """AI edits share the build's raw-output cache, so repeating the same edit
    request costs nothing (spec Section 12)."""
    return RawOutputCache(json_path.parent / "raw_cache"), UsageLedger()


def _merge_usage(book_id: str, row: dict[str, Any], ledger: UsageLedger) -> dict[str, Any]:
    """Fold an edit's spend into the book's running totals."""
    try:
        totals = json.loads(row.get("usage_json") or "{}")
    except json.JSONDecodeError:
        totals = {}
    delta = ledger.to_dict()
    for key in ("calls", "cache_hits", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "total_tokens"):
        totals[key] = int(totals.get(key, 0) or 0) + int(delta.get(key, 0) or 0)
    totals["cost_usd"] = round(
        float(totals.get("cost_usd", 0) or 0) + float(delta.get("cost_usd", 0) or 0), 6
    )
    bookdb.update_trivia_book(book_id, usage_json=json.dumps(totals))
    return totals


def _parse_config(payload: dict[str, Any]) -> BookConfig:
    raw = payload.get("config")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TriviaError(f"Config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raw = payload
    return BookConfig.from_dict(raw)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(app) -> None:  # noqa: ANN001
    """Attach all Trivia generator routes to the given Flask app."""

    @app.get("/trivia")
    def trivia_page():  # noqa: ANN202
        return render_template("trivia.html")

    @app.post("/api/trivia/validate-config")
    def trivia_validate_config():  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            cfg = _parse_config(payload)
        except TriviaError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({
            "ok": True,
            "book_title": cfg.book_title,
            "chapter_count": len(cfg.chapters),
            "trivia_total": sum(c.trivia_count for c in cfg.chapters),
            "fact_total": sum(c.fact_count for c in cfg.chapters),
        })

    # -- Outline import ----------------------------------------------------

    @app.post("/api/trivia/parse-outline")
    def trivia_parse_outline():  # noqa: ANN202
        """Turn an uploaded DOCX/TXT/MD outline into editable chapter rows.

        The chapter scheme usually already exists as a document, and typing it
        into the form one box at a time is the slowest part of setting up a
        book — so this reads the document instead.
        """
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "No outline file was uploaded."}), 400

        suffix = Path(file.filename).suffix.lower()
        if suffix not in {".docx", ".txt", ".md"}:
            return jsonify({"error": "Outline must be a .docx, .txt or .md file."}), 400

        path = UPLOAD_DIR / f"{uuid.uuid4().hex[:10]}{suffix}"
        try:
            file.save(str(path))
            return jsonify({"ok": True, **outline_parser.parse_outline_file(path)})
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
        finally:
            # Only the extracted structure is kept; the upload is scratch.
            path.unlink(missing_ok=True)

    @app.post("/api/trivia/parse-outline-text")
    def trivia_parse_outline_text():  # noqa: ANN202
        """Same parser, for an outline pasted straight into the browser."""
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text") or "")
        if not text.strip():
            return jsonify({"error": "No outline text was supplied."}), 400
        try:
            return jsonify({"ok": True, **outline_parser.parse_outline_text(text)})
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/trivia/jobs")
    def trivia_create_job():  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            cfg = _parse_config(payload)
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

        job_id = uuid.uuid4().hex[:8]
        job = TriviaJob(id=job_id, config=cfg)
        job.log(
            f"Queued '{cfg.book_title}' — {len(cfg.chapters)} chapter(s), "
            f"{sum(c.trivia_count for c in cfg.chapters)} questions, "
            f"{sum(c.fact_count for c in cfg.chapters)} facts"
        )
        with JOBS_LOCK:
            JOBS[job_id] = job

        bookdb.save_trivia_book(
            job_id,
            cfg.book_title,
            cfg.topic,
            status="queued",
            agent=cfg.agent,
            difficulty=cfg.difficulty,
            answer_key_position=cfg.answer_key_position,
            chapter_count=len(cfg.chapters),
            trivia_total=sum(c.trivia_count for c in cfg.chapters),
            fact_total=sum(c.fact_count for c in cfg.chapters),
            config_json=json.dumps(cfg.to_dict()),
        )

        threading.Thread(target=_run_build, args=(job_id,), daemon=True).start()
        return jsonify({"job_id": job_id, "status": "queued"})

    @app.post("/api/trivia/books/<book_id>/rerun")
    def trivia_rerun(book_id: str):  # noqa: ANN202
        """Start a fresh build from a previous book's stored config.

        Saves the operator re-entering a long chapter scheme after a failure.
        This is a new build under a new id: the original row is left untouched
        so a partial failure is never overwritten by the retry.
        """
        row = bookdb.get_trivia_book(book_id)
        if not row:
            abort(404)

        raw = row.get("config_json") or ""
        if not raw:
            return jsonify({
                "error": (
                    "This book has no stored configuration to re-run. Books "
                    "created before configs were saved have to be set up again."
                )
            }), 400

        try:
            cfg = _parse_config(json.loads(raw))
        except (ValueError, TriviaError) as exc:
            return jsonify({"error": f"Stored configuration is unusable: {exc}"}), 400

        job_id = uuid.uuid4().hex[:8]
        job = TriviaJob(id=job_id, config=cfg)
        job.log(
            f"Re-running '{cfg.book_title}' from the saved configuration — "
            f"{len(cfg.chapters)} chapter(s), "
            f"{sum(c.trivia_count for c in cfg.chapters)} questions, "
            f"{sum(c.fact_count for c in cfg.chapters)} facts"
        )
        with JOBS_LOCK:
            JOBS[job_id] = job

        bookdb.save_trivia_book(
            job_id,
            cfg.book_title,
            cfg.topic,
            status="queued",
            agent=cfg.agent,
            difficulty=cfg.difficulty,
            answer_key_position=cfg.answer_key_position,
            chapter_count=len(cfg.chapters),
            trivia_total=sum(c.trivia_count for c in cfg.chapters),
            fact_total=sum(c.fact_count for c in cfg.chapters),
            config_json=json.dumps(cfg.to_dict()),
        )

        threading.Thread(target=_run_build, args=(job_id,), daemon=True).start()
        return jsonify({"job_id": job_id, "status": "queued"})

    @app.get("/api/trivia/jobs/<job_id>/status")
    def trivia_job_status(job_id: str):  # noqa: ANN202
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            row = bookdb.get_trivia_book(job_id)
            if not row:
                abort(404)
            return jsonify({
                "id": job_id,
                "status": row.get("status", ""),
                "stage": row.get("stage", ""),
                "progress": row.get("progress", 0),
                "title": row.get("title", ""),
                "topic": row.get("topic", ""),
                "error": row.get("error", ""),
                "outputs": {
                    k: row.get(f"{k}_path", "")
                    for k in ("json", "markdown", "docx", "kindle", "paperback")
                    if row.get(f"{k}_path")
                },
                "warnings": json.loads(row.get("warnings_json") or "[]"),
                "usage": json.loads(row.get("usage_json") or "{}"),
                "collisions": [],
                "logs": [],
                "config": json.loads(row.get("config_json") or "null"),
            })
        return jsonify(job.to_status())

    @app.post("/api/trivia/jobs/<job_id>/stop")
    def trivia_stop_job(job_id: str):  # noqa: ANN202
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            abort(404)
        job.stop_requested = True
        job.log("Stop requested — finishing current batch.")
        return jsonify({"ok": True})

    @app.get("/api/trivia/books")
    def trivia_list_books():  # noqa: ANN202
        return jsonify({"books": bookdb.list_trivia_books()})

    @app.get("/api/trivia/books/<book_id>")
    def trivia_get_book(book_id: str):  # noqa: ANN202
        row = bookdb.get_trivia_book(book_id)
        if not row:
            abort(404)
        return jsonify({"book": row})

    @app.delete("/api/trivia/books/<book_id>")
    def trivia_delete_book(book_id: str):  # noqa: ANN202
        if not bookdb.delete_trivia_book(book_id):
            abort(404)
        with JOBS_LOCK:
            JOBS.pop(book_id, None)
        return jsonify({"ok": True})

    @app.get("/api/trivia/books/<book_id>/content")
    def trivia_book_content(book_id: str):  # noqa: ANN202
        """Full structured book JSON — the source of truth for re-formatting."""
        row = bookdb.get_trivia_book(book_id)
        if not row:
            abort(404)
        json_path = row.get("json_path") or ""
        if not json_path or not Path(json_path).exists():
            return jsonify({"error": "No generated content for this book yet."}), 404
        return jsonify(json.loads(Path(json_path).read_text(encoding="utf-8")))

    # -- Preview & Edit ----------------------------------------------------

    @app.get("/trivia/<book_id>/edit")
    def trivia_edit_page(book_id: str):  # noqa: ANN202
        row = bookdb.get_trivia_book(book_id)
        if not row:
            abort(404)
        return render_template("trivia_edit.html", book_id=book_id,
                               book_title=row.get("title", ""))

    @app.get("/api/trivia/books/<book_id>/illustration/<int:chapter_number>")
    def trivia_illustration(book_id: str, chapter_number: int):  # noqa: ANN202
        """Serve a chapter image for the preview pane."""
        try:
            _, _, book = _load_book_for_edit(book_id)
        except TriviaError:
            abort(404)
        ch = editor.find_chapter(book, chapter_number)
        if ch is None or not ch.illustration_path:
            abort(404)
        path = Path(ch.illustration_path)
        if not path.exists():
            abort(404)
        return send_file(str(path))

    @app.patch("/api/trivia/books/<book_id>/questions/<item_id>")
    def trivia_edit_question(book_id: str, item_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            q = editor.apply_question_edit(book, item_id, payload)
            _save_book(book, json_path)
            return jsonify({"ok": True, "question": q.to_dict()})
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/trivia/books/<book_id>/facts/<item_id>")
    def trivia_edit_fact(book_id: str, item_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            f = editor.apply_fact_edit(book, item_id, payload)
            _save_book(book, json_path)
            return jsonify({"ok": True, "fact": f.to_dict()})
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/trivia/books/<book_id>/chapters/<int:chapter_number>")
    def trivia_edit_chapter(book_id: str, chapter_number: int):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            ch = editor.apply_chapter_edit(book, chapter_number, payload)
            _save_book(book, json_path)
            return jsonify({"ok": True, "chapter": {
                "chapter_number": ch.number,
                "chapter_title": ch.title,
                "chapter_scope": ch.scope,
            }})
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/api/trivia/books/<book_id>/items/<item_id>")
    def trivia_delete_item(book_id: str, item_id: str):  # noqa: ANN202
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            if not editor.delete_item(book, item_id):
                return jsonify({"error": f"'{item_id}' not found."}), 404
            for ch in book.chapters:
                editor.renumber_chapter(ch)
            _save_book(book, json_path)
            return jsonify({"ok": True})
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/trivia/books/<book_id>/ai-edit")
    def trivia_ai_edit(book_id: str):  # noqa: ANN202
        """Apply an AI action to one question or fact."""
        payload = request.get_json(silent=True) or {}
        item_id = str(payload.get("item_id") or "").strip()
        action = str(payload.get("action") or "").strip()
        instruction = str(payload.get("instruction") or "").strip()
        kind = str(payload.get("kind") or "").strip()

        if not item_id or not action:
            return jsonify({"error": "item_id and action are required."}), 400

        try:
            row, json_path, book = _load_book_for_edit(book_id)
            cache, ledger = _edit_cache_and_ledger(json_path)

            if kind == "fact":
                item = editor.ai_edit_fact(
                    book, item_id, action, instruction, cache=cache, ledger=ledger
                )
                result = {"fact": item.to_dict()}
            else:
                item = editor.ai_edit_question(
                    book, item_id, action, instruction, cache=cache, ledger=ledger
                )
                result = {"question": item.to_dict()}

            _save_book(book, json_path)
            usage = _merge_usage(book_id, row, ledger)
            return jsonify({"ok": True, "usage": usage, **result})
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/trivia/books/<book_id>/regenerate-illustration")
    def trivia_regen_illustration(book_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            chapter_number = int(payload.get("chapter_number") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "chapter_number must be a number."}), 400

        try:
            _, json_path, book = _load_book_for_edit(book_id)
            ch = editor.regenerate_illustration(
                book,
                chapter_number,
                json_path.parent,
                prompt_hint=str(payload.get("prompt_hint") or ""),
                style_hint=str(payload.get("style_hint") or ""),
            )
            _save_book(book, json_path)
            return jsonify({"ok": True, "chapter_number": ch.number,
                            "prompt": ch.illustration_prompt})
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/trivia/books/<book_id>/upload-illustration")
    def trivia_upload_illustration(book_id: str):  # noqa: ANN202
        try:
            chapter_number = int(request.form.get("chapter_number") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "chapter_number must be a number."}), 400
        file = request.files.get("image")
        if file is None or not file.filename:
            return jsonify({"error": "No image file was uploaded."}), 400

        try:
            _, json_path, book = _load_book_for_edit(book_id)
            ch = editor.set_illustration_from_upload(
                book, chapter_number, file, json_path.parent
            )
            _save_book(book, json_path)
            return jsonify({"ok": True, "chapter_number": ch.number})
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    # -- Image editing -----------------------------------------------------

    @app.get("/api/trivia/books/<book_id>/image-info/<int:chapter_number>")
    def trivia_image_info(book_id: str, chapter_number: int):  # noqa: ANN202
        try:
            _, _, book = _load_book_for_edit(book_id)
            return jsonify(imgedit.image_info(book, chapter_number))
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/trivia/books/<book_id>/image/adjust")
    def trivia_image_adjust(book_id: str):  # noqa: ANN202
        """Local brightness/contrast/saturation/sharpness. No API cost."""
        payload = request.get_json(silent=True) or {}
        try:
            chapter_number = int(payload.get("chapter_number") or 0)
            _, json_path, book = _load_book_for_edit(book_id)
            result = imgedit.adjust_image(
                book, chapter_number,
                brightness=float(payload.get("brightness", 1.0)),
                contrast=float(payload.get("contrast", 1.0)),
                saturation=float(payload.get("saturation", 1.0)),
                sharpness=float(payload.get("sharpness", 1.0)),
            )
            _save_book(book, json_path)
            return jsonify({"ok": True, **result.to_dict()})
        except (TypeError, ValueError):
            return jsonify({"error": "Adjustment values must be numbers."}), 400
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/trivia/books/<book_id>/image/transform")
    def trivia_image_transform(book_id: str):  # noqa: ANN202
        """Crop, rotate or flip — all local."""
        payload = request.get_json(silent=True) or {}
        op = str(payload.get("op") or "").strip()
        try:
            chapter_number = int(payload.get("chapter_number") or 0)
            _, json_path, book = _load_book_for_edit(book_id)

            if op == "crop":
                result = imgedit.crop_image(
                    book, chapter_number,
                    float(payload.get("left", 0)), float(payload.get("top", 0)),
                    float(payload.get("right", 1)), float(payload.get("bottom", 1)),
                )
            elif op == "rotate":
                result = imgedit.rotate_image(
                    book, chapter_number, int(payload.get("degrees", 90))
                )
            elif op == "flip":
                result = imgedit.flip_image(
                    book, chapter_number, str(payload.get("axis") or "horizontal")
                )
            else:
                return jsonify({"error": "op must be crop, rotate or flip."}), 400

            _save_book(book, json_path)
            return jsonify({"ok": True, **result.to_dict()})
        except (TypeError, ValueError):
            return jsonify({"error": "Transform values must be numbers."}), 400
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/trivia/books/<book_id>/image/ai-edit")
    def trivia_image_ai_edit(book_id: str):  # noqa: ANN202
        """Natural-language image edit, optionally confined to a painted mask."""
        payload = request.get_json(silent=True) or {}
        try:
            chapter_number = int(payload.get("chapter_number") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "chapter_number must be a number."}), 400

        try:
            _, json_path, book = _load_book_for_edit(book_id)
            result = imgedit.ai_edit_image(
                book, chapter_number,
                str(payload.get("instruction") or ""),
                mask_data_url=str(payload.get("mask") or ""),
                size=str(payload.get("size") or "auto"),
                quality=str(payload.get("quality") or "high"),
            )
            _save_book(book, json_path)
            return jsonify({"ok": True, **result.to_dict()})
        except TriviaError as exc:
            # Image failures are usually upstream (content policy, size limits,
            # rate limits) and the message matters, so log it rather than
            # letting a bare 400 reach the browser with no trace.
            print(f"[trivia] image ai-edit failed for {book_id} ch{chapter_number}: {exc}",
                  flush=True)
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/trivia/books/<book_id>/image/undo")
    def trivia_image_undo(book_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            chapter_number = int(payload.get("chapter_number") or 0)
            _, json_path, book = _load_book_for_edit(book_id)
            result = imgedit.undo(book, chapter_number)
            _save_book(book, json_path)
            return jsonify({"ok": True, **result.to_dict()})
        except (TypeError, ValueError):
            return jsonify({"error": "chapter_number must be a number."}), 400
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/trivia/books/<book_id>/strip-ai")
    def trivia_strip_ai(book_id: str):  # noqa: ANN202
        """Report, or remove, AI fingerprints in this book's images and text.

        POST {"apply": false} previews; {"apply": true} performs the cleanup.
        """
        payload = request.get_json(silent=True) or {}
        apply = bool(payload.get("apply"))
        try:
            _row, json_path, book = _load_book_for_edit(book_id)
            result = strip_ai_report(book, json_path.parent, apply=apply)
            if apply:
                _save_book(book, json_path)
            return jsonify({"ok": True, **result})
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/trivia/books/<book_id>/validate")
    def trivia_validate_edits(book_id: str):  # noqa: ANN202
        try:
            _, _, book = _load_book_for_edit(book_id)
            return jsonify(editor.validate_edited_book(book))
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/trivia/books/<book_id>/download/<kind>")
    def trivia_download(book_id: str, kind: str):  # noqa: ANN202
        row = bookdb.get_trivia_book(book_id)
        if not row:
            abort(404)
        column = {
            "json": "json_path",
            "markdown": "markdown_path",
            "docx": "docx_path",
            "kindle": "kindle_path",
            "paperback": "paperback_path",
        }.get(kind)
        if not column:
            abort(404)
        path = row.get(column) or ""
        if not path or not Path(path).exists():
            abort(404)
        return send_file(path, as_attachment=True, download_name=Path(path).name)

    @app.post("/api/trivia/books/<book_id>/reformat")
    def trivia_reformat(book_id: str):  # noqa: ANN202
        """Rebuild DOCX/KDP outputs from stored JSON without regenerating any
        content — the reason JSON is the source of truth."""
        row = bookdb.get_trivia_book(book_id)
        if not row:
            abort(404)
        json_path = Path(row.get("json_path") or "")
        if not json_path.exists():
            return jsonify({"error": "No stored JSON for this book."}), 400

        payload = request.get_json(silent=True) or {}
        position = (payload.get("answer_key_position") or "").strip()

        try:
            book = pipeline.load_json(json_path)
            if position:
                book.config.answer_key_position = position
            out_dir = json_path.parent
            stem = _safe_stem(book.config.book_title)

            md_path = exporter.write_markdown(book, out_dir / f"{stem}.md")
            docx_path = exporter.build_docx(book, out_dir / f"{stem}.docx")
            pipeline.write_json(book, json_path)

            updates: dict[str, Any] = {
                "markdown_path": str(md_path),
                "docx_path": str(docx_path),
            }
            if position:
                updates["answer_key_position"] = position

            exporter.verify_print_images(book, out_dir)

            try:
                kdp = exporter.build_kdp_files(book, docx_path, out_dir)
                updates["kindle_path"] = kdp["kindle"]
                updates["paperback_path"] = kdp["paperback"]
            except Exception as exc:  # noqa: BLE001
                return jsonify({
                    "ok": True,
                    "warning": f"KDP formatting failed: {exc}",
                    "outputs": updates,
                })

            bookdb.update_trivia_book(book_id, **updates)
            return jsonify({"ok": True, "outputs": updates})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    def _apply_config_overrides(book: Any, payload: Any) -> list[str]:
        """Fold browser-side chapter edits into a loaded draft's config.

        fact_count, trivia_count and chapter_scope are honoured. trivia_count
        used to be ignored on the theory that questions are never auto-dropped,
        but that left a chapter short on questions with no way out at all: the
        export gate demands an exact match, so the operator could neither raise
        the supply nor lower the requirement. Lowering it now trims the draft
        here (see _trim_trivia_to_quota); raising it is handled downstream by
        top_up_short_trivia.

        Chapters are matched by chapter_number, so reordering the rows in the
        browser cannot silently retarget an edit at the wrong chapter.
        Returns a human-readable note per applied change.
        """
        if not isinstance(payload, dict):
            return []
        rows = payload.get("chapters")
        if not isinstance(rows, list) or not rows:
            return []

        by_number = {c.chapter_number: c for c in book.config.chapters}
        notes: list[str] = []

        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                number = int(row.get("chapter_number"))
            except (TypeError, ValueError):
                continue
            ch_cfg = by_number.get(number)
            if ch_cfg is None:
                continue

            if "chapter_scope" in row:
                scope = str(row.get("chapter_scope") or "").strip()
                if scope and scope != ch_cfg.chapter_scope:
                    ch_cfg.chapter_scope = scope
                    notes.append(f"Chapter {number}: scope widened.")

            for field_name in ("fact_count", "trivia_count"):
                if row.get(field_name) in (None, ""):
                    continue
                try:
                    new_count = int(row[field_name])
                except (TypeError, ValueError):
                    raise TriviaError(
                        f"Chapter {number}: {field_name} must be a whole number."
                    )
                if new_count < 0:
                    raise TriviaError(
                        f"Chapter {number}: {field_name} cannot be negative."
                    )
                old_count = getattr(ch_cfg, field_name)
                if new_count == old_count:
                    continue
                setattr(ch_cfg, field_name, new_count)
                notes.append(
                    f"Chapter {number}: {field_name} {old_count} -> {new_count}."
                )

        # Raising fact_count is handled downstream by top_up_short_chapters.
        # Lowering it needs the draft trimmed here, because validate_for_export
        # treats over-quota facts as a hard error.
        _trim_facts_to_quota(book, notes)
        _trim_trivia_to_quota(book, notes)
        return notes

    def _trim_trivia_to_quota(book: Any, notes: list[str]) -> None:
        """Drop questions from any chapter now sitting over its quota.

        Trimming from the tail is right here, unlike facts: questions are not
        what the no-overlap gate rejects, so there are no "problem" ones to
        prefer dropping. The answer spread is rebalanced afterwards because
        validate_for_export also checks that correct answers do not cluster on
        one letter, and cutting the tail can skew it.
        """
        for cfg, ch in zip(book.config.chapters, book.chapters):
            if len(ch.trivia) <= cfg.trivia_count:
                continue
            dropped = len(ch.trivia) - cfg.trivia_count
            ch.trivia[:] = ch.trivia[: cfg.trivia_count]
            for i, q in enumerate(ch.trivia, start=1):
                q.id = f"ch{cfg.chapter_number}_q{i:02d}"
            pipeline.engine.rebalance_answer_distribution(ch.trivia)
            notes.append(
                f"Chapter {cfg.chapter_number}: dropped {dropped} question(s) "
                f"to meet the lowered count."
            )

    def _trim_facts_to_quota(book: Any, notes: list[str]) -> None:
        """Drop facts from any chapter now sitting over its quota.

        Colliding facts are dropped first. Trimming from the tail instead would
        leave the very facts that failed the gate in place, so a lowered
        fact_count would not actually unblock the export -- which is the whole
        point of lowering it.
        """
        over = [
            (cfg, ch)
            for cfg, ch in zip(book.config.chapters, book.chapters)
            if len(ch.facts) > cfg.fact_count
        ]
        if not over:
            return

        # One dedup sweep over the draft as-is tells us which facts are the
        # problem ones. This is the free heuristic stage plus whatever the
        # cached judge verdicts already cover, so it spends nothing new.
        flagged: set[str] = set()
        try:
            probe = pipeline.TriviaBuilder(book.config)
            probe.book = book
            for collision in probe.global_dedup_pass():
                if collision.kind == "fact_vs_trivia":
                    flagged.add(collision.left_id)
                elif collision.kind == "fact_vs_fact":
                    # Keep one side of the pair; drop the later one.
                    flagged.add(max(collision.left_id, collision.right_id))
        except Exception:  # noqa: BLE001
            # A probe failure must not block the trim; fall back to tail order.
            flagged = set()

        for cfg, ch in over:
            keep = cfg.fact_count
            dropped = len(ch.facts) - keep
            # Stable sort: flagged facts move to the back, order preserved
            # otherwise, then the tail is cut.
            ch.facts.sort(key=lambda f: f.id in flagged)
            ch.facts[:] = ch.facts[:keep]
            notes.append(
                f"Chapter {cfg.chapter_number}: dropped {dropped} fact(s) to "
                f"meet the lowered count."
            )

    @app.post("/api/trivia/books/<book_id>/resolve")
    def trivia_resolve(book_id: str):  # noqa: ANN202
        """Finish a book the no-overlap gate blocked, without regenerating it.

        A gate failure typically rejects a handful of facts out of hundreds
        that were already generated and paid for. This reloads the saved draft,
        regenerates only the colliding facts, and runs the export the failed
        build never reached.
        """
        row = bookdb.get_trivia_book(book_id)
        if not row:
            abort(404)
        json_path = Path(row.get("json_path") or "")
        if not json_path.exists():
            return jsonify({
                "error": (
                    "No saved draft for this book. Builds that failed before "
                    "this fix shipped did not persist their content and have "
                    "to be rebuilt."
                )
            }), 400

        try:
            book = pipeline.load_json(json_path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Could not read the saved draft: {exc}"}), 500

        # The operator may have lowered fact_count (or edited a scope) in the
        # browser after reading the gate failure -- the error message tells
        # them to. Apply those edits to the saved draft's config before
        # resolving, so the button does what the message promises. Absent a
        # body, this is a no-op and the draft's own config is used.
        try:
            overrides = _apply_config_overrides(book, request.get_json(silent=True))
        except TriviaError as exc:
            return jsonify({"error": str(exc)}), 400

        out_dir = json_path.parent
        builder = pipeline.TriviaBuilder(book.config)
        builder.book = book
        # write_json/load_json do not round-trip a cache, so point the rebuilt
        # builder at the one the original build already paid to fill.
        builder.cache = RawOutputCache(out_dir / "raw_cache")
        builder.checker.cache = builder.cache

        try:
            remaining = builder.global_dedup_pass()
            for _ in range(pipeline.DEDUP_REPAIR_ROUNDS):
                if not remaining:
                    break
                before = len(remaining)
                builder.resolve_collisions(remaining)
                remaining = builder.global_dedup_pass()
                if len(remaining) >= before:
                    break

            blocking = [c for c in remaining if c.kind == "fact_vs_trivia"]
            if blocking:
                detail = "; ".join(
                    f"{c.left_id} repeats {c.right_id}" for c in blocking[:5]
                )
                # Save first: the repair rounds above regenerated facts that
                # were paid for, and any overrides trimmed the draft. Returning
                # without writing would discard both and re-run them next time.
                pipeline.write_json(book, json_path)
                return jsonify({
                    "error": (
                        f"Still blocked: {len(blocking)} fact(s) restate trivia "
                        f"content. {detail}. Widen the scope or lower "
                        "fact_count for those chapters, then try again."
                    ),
                    "collisions": [c.to_dict() for c in blocking],
                    "applied": overrides,
                }), 409

            # A build can also be blocked simply because a chapter came up
            # short of its fact quota. Extend those chapters in place before
            # validating, so a draft that only needs a few more facts finishes
            # instead of demanding a full rebuild.
            short_notes = builder.top_up_short_chapters()
            # Questions can fall short too -- a chapter that lost its trivia to
            # a failed batch is the most common reason a resolve stayed stuck.
            short_notes += builder.top_up_short_trivia()

            errors = builder.validate_for_export()
            if errors:
                detail = "Export blocked by validation:\n- " + "\n- ".join(errors[:12])
                if short_notes:
                    detail += (
                        "\n\nSome chapters could not be filled: "
                        + "; ".join(short_notes)
                        + ". Lower fact_count for those chapters, or widen their "
                        "scope, then resolve again."
                    )
                # As above: keep the topped-up facts this pass paid for.
                pipeline.write_json(book, json_path)
                return jsonify({"error": detail, "applied": overrides}), 409

            stem = _safe_stem(book.config.book_title)
            pipeline.write_json(book, json_path)
            md_path = exporter.write_markdown(book, out_dir / f"{stem}.md")
            docx_path = exporter.build_docx(book, out_dir / f"{stem}.docx")

            updates: dict[str, Any] = {
                "status": "done",
                "stage": "done",
                "progress": 1.0,
                "error": "",
                "json_path": str(json_path),
                "markdown_path": str(md_path),
                "docx_path": str(docx_path),
                # These were frozen at submit time, so the library card kept
                # showing the original counts after an override changed them.
                # Report what the finished book actually contains.
                "chapter_count": len(book.chapters),
                "trivia_total": sum(len(c.trivia) for c in book.chapters),
                "fact_total": sum(len(c.facts) for c in book.chapters),
                "config_json": json.dumps(book.config.to_dict()),
            }

            warnings: list[str] = list(book.warnings)
            warnings.extend(overrides)
            warnings.extend(short_notes)
            warnings.extend(exporter.verify_print_images(book, out_dir))

            try:
                kdp = exporter.build_kdp_files(book, docx_path, out_dir)
                updates["kindle_path"] = kdp["kindle"]
                updates["paperback_path"] = kdp["paperback"]
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"KDP formatting failed: {exc}")

            updates["warnings_json"] = json.dumps(warnings)
            # Resolving spends tokens of its own; fold them into the total the
            # book already carries rather than reporting only this pass.
            prior = json.loads(row.get("usage_json") or "{}")
            spent = builder.ledger.to_dict()
            merged = dict(spent)
            for key in ("calls", "cache_hits", "total_tokens", "cost_usd"):
                if key in prior and key in spent:
                    merged[key] = prior[key] + spent[key]
            updates["usage_json"] = json.dumps(merged)

            bookdb.update_trivia_book(book_id, **updates)
            return jsonify({"ok": True, "outputs": updates, "warnings": warnings})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
