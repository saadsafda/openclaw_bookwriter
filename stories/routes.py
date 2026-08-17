"""Researched-stories book generator — Flask routes and job runner.

Registered onto the main app via register(app), the same way trivia.py,
puzzle.py and publications.py attach their own routes. Everything here lives
under /stories and /api/stories/* and shares no state with the other pipelines.
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
from .engine import (
    BookConfig,
    RawOutputCache,
    StoryError,
    UsageLedger,
    ValidationGateError,
)

# Two levels up: this file lives in <project>/stories/, and outputs belong
# beside the other project data, not inside the package.
ROOT_DIR = Path(__file__).resolve().parent.parent
STORY_OUTPUT_DIR = ROOT_DIR / "story_outputs"
STORY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_LOG_LINES = 800

# Uploaded outlines are parsed and discarded; only the extracted structure is
# kept, so this is a scratch area rather than durable storage.
UPLOAD_DIR = STORY_OUTPUT_DIR / "_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class StoryJob:
    id: str
    config: BookConfig
    status: str = "queued"          # queued | running | done | error | stopped
    stage: str = ""
    progress: float = 0.0
    logs: list[str] = field(default_factory=list)
    error: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    stories_written: int = 0
    total_words: int = 0
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
            "usage": dict(self.usage),
            "stories_written": self.stories_written,
            "story_count": len(self.config.all_stories()),
            "total_words": self.total_words,
            "logs": self.logs[-150:],
        }


JOBS: dict[str, StoryJob] = {}
JOBS_LOCK = threading.Lock()


def _job_dir(job_id: str) -> Path:
    return STORY_OUTPUT_DIR / job_id


def _safe_stem(title: str) -> str:
    keep = [c if c.isalnum() or c in " -_" else "" for c in (title or "stories")]
    stem = "".join(keep).strip().replace(" ", "_") or "story_book"
    return stem[:60]


def _run_build(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return

    job.status = "running"
    job.stage = "starting"
    bookdb.update_story_book(job_id, status="running", stage="starting")

    out_dir = _job_dir(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _log(message: str) -> None:
        job.log(message)

    def _progress(stage: str, pct: float) -> None:
        job.stage = stage
        job.progress = max(0.0, min(1.0, pct))
        bookdb.update_story_book(job_id, stage=stage, progress=job.progress)

    builder: Optional[pipeline.StoryBuilder] = None
    try:
        builder = pipeline.StoryBuilder(
            job.config,
            log=_log,
            progress=_progress,
            should_stop=lambda: job.stop_requested,
        )
        book = builder.build(out_dir)
        job.warnings = list(book.warnings)
        job.usage = dict(book.usage)
        job.stories_written = len(book.all_stories())
        job.total_words = book.total_words()

        stem = _safe_stem(job.config.book_title)

        json_path = pipeline.write_json(book, out_dir / f"{stem}.json")
        job.outputs["json"] = str(json_path)
        _log(f"Wrote JSON source of truth: {json_path.name}")

        md_path = exporter.write_markdown(book, out_dir / f"{stem}.md")
        job.outputs["markdown"] = str(md_path)
        _log(f"Wrote Markdown: {md_path.name}")

        fc_path = exporter.write_factcheck(book, out_dir / f"{stem}_factcheck.md")
        job.outputs["factcheck"] = str(fc_path)
        _log(f"Wrote fact-check sheet: {fc_path.name}")

        docx_path = exporter.build_docx(book, out_dir / f"{stem}.docx")
        job.outputs["docx"] = str(docx_path)
        _log(f"Wrote DOCX manuscript: {docx_path.name}")

        _progress("formatting", 0.97)
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
        bookdb.update_story_book(
            job_id,
            status="done",
            stage="done",
            progress=1.0,
            stories_written=job.stories_written,
            total_words=job.total_words,
            json_path=job.outputs.get("json", ""),
            markdown_path=job.outputs.get("markdown", ""),
            docx_path=job.outputs.get("docx", ""),
            kindle_path=job.outputs.get("kindle", ""),
            paperback_path=job.outputs.get("paperback", ""),
            factcheck_path=job.outputs.get("factcheck", ""),
            warnings_json=json.dumps(job.warnings),
            usage_json=json.dumps(job.usage),
        )
        _log("Build complete.")

    except ValidationGateError as exc:
        job.status = "error"
        job.stage = "validation-failed"
        job.error = str(exc)
        if builder is not None:
            # Spend up to the point of failure is real; keep it on the ledger.
            job.usage = builder.ledger.to_dict()
            job.warnings = list(builder.book.warnings)
        _log(f"BLOCKED: {exc}")
        bookdb.update_story_book(
            job_id, status="error", stage="validation-failed", error=str(exc),
            warnings_json=json.dumps(job.warnings),
            usage_json=json.dumps(job.usage),
        )
    except StoryError as exc:
        job.status = "stopped" if job.stop_requested else "error"
        job.stage = "stopped" if job.stop_requested else "failed"
        job.error = "" if job.stop_requested else str(exc)
        if builder is not None:
            job.usage = builder.ledger.to_dict()
        _log(str(exc))
        bookdb.update_story_book(
            job_id, status=job.status, stage=job.stage, error=job.error,
            usage_json=json.dumps(job.usage),
        )
    except Exception as exc:  # noqa: BLE001 - surface anything else to the UI
        job.status = "error"
        job.stage = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        _log(f"ERROR: {job.error}")
        bookdb.update_story_book(
            job_id, status="error", stage="failed", error=job.error
        )


def _load_book_for_edit(book_id: str) -> tuple[dict[str, Any], Path, Any]:
    """Fetch a book row, its JSON path, and the rehydrated book object.

    Raises StoryError with a caller-friendly message; routes turn that into a
    4xx rather than a 500.
    """
    row = bookdb.get_story_book(book_id)
    if not row:
        raise StoryError("Book not found.")
    json_path = Path(row.get("json_path") or "")
    if not json_path.exists():
        raise StoryError("This book has no generated content yet.")
    return row, json_path, pipeline.load_json(json_path)


def _save_book(book: Any, json_path: Path) -> None:
    pipeline.write_json(book, json_path)
    # Counts shown in the library come from the DB row, so they have to move
    # with an edit that added or deleted a story.
    bookdb.update_story_book(
        json_path.parent.name,
        stories_written=len(book.all_stories()),
        total_words=book.total_words(),
    )


def _edit_cache_and_ledger(json_path: Path) -> tuple[RawOutputCache, UsageLedger]:
    """AI edits share the build's raw-output cache, so repeating the same edit
    request costs nothing."""
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
    bookdb.update_story_book(book_id, usage_json=json.dumps(totals))
    return totals


def _parse_config(payload: dict[str, Any]) -> BookConfig:
    raw = payload.get("config")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StoryError(f"Config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raw = payload
    return BookConfig.from_dict(raw)


def _image_selector(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the story/chapter selector out of a request body.

    Illustrations hang off either a story or a chapter, so every image route
    accepts both and passes exactly one through.
    """
    story_id = str(payload.get("story_id") or "").strip()
    try:
        chapter_number = int(payload.get("chapter_number") or 0)
    except (TypeError, ValueError):
        raise StoryError("chapter_number must be a number.")
    if not story_id and not chapter_number:
        raise StoryError("Either story_id or chapter_number is required.")
    return {"story_id": story_id, "chapter_number": chapter_number}


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(app) -> None:  # noqa: ANN001
    """Attach all Stories generator routes to the given Flask app."""

    @app.get("/stories")
    def stories_page():  # noqa: ANN202
        return render_template("stories.html")

    @app.post("/api/stories/validate-config")
    def stories_validate_config():  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            cfg = _parse_config(payload)
        except StoryError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        stories = cfg.all_stories()
        return jsonify({
            "ok": True,
            "book_title": cfg.book_title,
            "chapter_count": len(cfg.chapters),
            "story_count": len(stories),
            "with_context": sum(1 for s in stories if s.has_context()),
            "estimated_words": [
                len(stories) * cfg.min_words, len(stories) * cfg.max_words
            ],
        })

    # -- Outline import ----------------------------------------------------

    @app.post("/api/stories/parse-outline")
    def stories_parse_outline():  # noqa: ANN202
        """Turn an uploaded DOCX/TXT/MD outline into editable story entries.

        The operator's outline usually already exists as a document — this
        saves retyping a hundred entries into the form.
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
            parsed = outline_parser.parse_outline_file(path)
            return jsonify({"ok": True, **parsed})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
        finally:
            path.unlink(missing_ok=True)

    @app.post("/api/stories/parse-outline-text")
    def stories_parse_outline_text():  # noqa: ANN202
        """Same parser, for an outline pasted straight into the browser."""
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text") or "")
        if not text.strip():
            return jsonify({"error": "No outline text was supplied."}), 400
        try:
            return jsonify({"ok": True, **outline_parser.parse_outline_text(text)})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/stories/suggest-outline")
    def stories_suggest_outline():  # noqa: ANN202
        """Have the AI propose real stories with context boxes pre-filled."""
        payload = request.get_json(silent=True) or {}
        book_title = str(payload.get("book_title") or "").strip()
        topic = str(payload.get("topic") or "").strip()
        if not book_title or not topic:
            return jsonify({"error": "book_title and topic are required."}), 400

        try:
            count = int(payload.get("count") or 10)
        except (TypeError, ValueError):
            return jsonify({"error": "count must be a number."}), 400
        # Each batch is a real API call, so an unbounded count from the client
        # would be an open-ended spend.
        count = max(1, min(count, 100))

        ledger = UsageLedger()
        try:
            proposed = pipeline.generate_outline(
                book_title,
                topic,
                count,
                notes=str(payload.get("notes") or "").strip(),
                agent=str(payload.get("agent") or "main").strip() or "main",
                ledger=ledger,
            )
            return jsonify({
                "ok": True,
                "stories": proposed,
                "usage": ledger.to_dict(),
            })
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    # -- Jobs --------------------------------------------------------------

    @app.post("/api/stories/jobs")
    def stories_create_job():  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            cfg = _parse_config(payload)
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

        job_id = uuid.uuid4().hex[:8]
        job = StoryJob(id=job_id, config=cfg)
        stories = cfg.all_stories()
        job.log(
            f"Queued '{cfg.book_title}' — {len(stories)} story(ies), "
            f"{cfg.min_words}-{cfg.max_words} words each"
        )
        thin = sum(1 for s in stories if not s.has_context())
        if thin:
            job.log(
                f"{thin} story(ies) have no context supplied; those will be "
                "written from the model's own knowledge and flagged for "
                "fact-checking."
            )
        with JOBS_LOCK:
            JOBS[job_id] = job

        bookdb.save_story_book(
            job_id,
            cfg.book_title,
            cfg.topic,
            status="queued",
            agent=cfg.agent,
            audience=cfg.audience,
            tone=cfg.tone,
            story_count=len(stories),
            min_words=cfg.min_words,
            max_words=cfg.max_words,
            config_json=json.dumps(cfg.to_dict()),
        )

        threading.Thread(target=_run_build, args=(job_id,), daemon=True).start()
        return jsonify({"job_id": job_id, "status": "queued"})

    @app.get("/api/stories/jobs/<job_id>/status")
    def stories_job_status(job_id: str):  # noqa: ANN202
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            row = bookdb.get_story_book(job_id)
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
                    for k in ("json", "markdown", "docx", "kindle",
                              "paperback", "factcheck")
                    if row.get(f"{k}_path")
                },
                "warnings": json.loads(row.get("warnings_json") or "[]"),
                "usage": json.loads(row.get("usage_json") or "{}"),
                "stories_written": row.get("stories_written", 0),
                "story_count": row.get("story_count", 0),
                "total_words": row.get("total_words", 0),
                "logs": [],
            })
        return jsonify(job.to_status())

    @app.post("/api/stories/jobs/<job_id>/stop")
    def stories_stop_job(job_id: str):  # noqa: ANN202
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            abort(404)
        job.stop_requested = True
        job.log("Stop requested — finishing the current story.")
        return jsonify({"ok": True})

    # -- Library -----------------------------------------------------------

    @app.get("/api/stories/books")
    def stories_list_books():  # noqa: ANN202
        return jsonify({"books": bookdb.list_story_books()})

    @app.get("/api/stories/books/<book_id>")
    def stories_get_book(book_id: str):  # noqa: ANN202
        row = bookdb.get_story_book(book_id)
        if not row:
            abort(404)
        return jsonify({"book": row})

    @app.delete("/api/stories/books/<book_id>")
    def stories_delete_book(book_id: str):  # noqa: ANN202
        if not bookdb.delete_story_book(book_id):
            abort(404)
        with JOBS_LOCK:
            JOBS.pop(book_id, None)
        return jsonify({"ok": True})

    @app.get("/api/stories/books/<book_id>/content")
    def stories_book_content(book_id: str):  # noqa: ANN202
        """Full structured book JSON — the source of truth for re-formatting."""
        row = bookdb.get_story_book(book_id)
        if not row:
            abort(404)
        json_path = row.get("json_path") or ""
        if not json_path or not Path(json_path).exists():
            return jsonify({"error": "No generated content for this book yet."}), 404
        return jsonify(json.loads(Path(json_path).read_text(encoding="utf-8")))

    # -- Preview & Edit ----------------------------------------------------

    @app.get("/stories/<book_id>/edit")
    def stories_edit_page(book_id: str):  # noqa: ANN202
        row = bookdb.get_story_book(book_id)
        if not row:
            abort(404)
        return render_template("stories_edit.html", book_id=book_id,
                               book_title=row.get("title", ""))

    @app.patch("/api/stories/books/<book_id>/stories/<item_id>")
    def stories_edit_story(book_id: str, item_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            story = editor.apply_story_edit(book, item_id, payload)
            _save_book(book, json_path)
            return jsonify({"ok": True, "story": story.to_dict()})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/stories/books/<book_id>/chapters/<int:chapter_number>")
    def stories_edit_chapter(book_id: str, chapter_number: int):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            ch = editor.apply_chapter_edit(book, chapter_number, payload)
            _save_book(book, json_path)
            return jsonify({"ok": True, "chapter": {
                "chapter_number": ch.number,
                "chapter_title": ch.title,
                "chapter_intro": ch.intro,
            }})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/api/stories/books/<book_id>/stories/<item_id>")
    def stories_delete_story(book_id: str, item_id: str):  # noqa: ANN202
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            if not editor.delete_story(book, item_id):
                return jsonify({"error": f"'{item_id}' not found."}), 404
            editor.renumber(book)
            _save_book(book, json_path)
            return jsonify({"ok": True})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/stories/books/<book_id>/strip-ai")
    def stories_strip_ai(book_id: str):  # noqa: ANN202
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
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/stories/books/<book_id>/reorder")
    def stories_reorder(book_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        order = payload.get("order")
        if not isinstance(order, list):
            return jsonify({"error": "order must be a list of story ids."}), 400
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            editor.reorder_stories(book, [str(i) for i in order])
            _save_book(book, json_path)
            return jsonify({"ok": True})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/stories/books/<book_id>/ai-edit")
    def stories_ai_edit(book_id: str):  # noqa: ANN202
        """Apply an AI action to one story."""
        payload = request.get_json(silent=True) or {}
        item_id = str(payload.get("item_id") or "").strip()
        action = str(payload.get("action") or "").strip()
        instruction = str(payload.get("instruction") or "").strip()

        if not item_id or not action:
            return jsonify({"error": "item_id and action are required."}), 400

        try:
            row, json_path, book = _load_book_for_edit(book_id)
            cache, ledger = _edit_cache_and_ledger(json_path)
            story = editor.ai_edit_story(
                book, item_id, action, instruction, cache=cache, ledger=ledger
            )
            _save_book(book, json_path)
            usage = _merge_usage(book_id, row, ledger)
            return jsonify({"ok": True, "usage": usage, "story": story.to_dict()})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/stories/books/<book_id>/regenerate")
    def stories_regenerate(book_id: str):  # noqa: ANN202
        """Rewrite one story from its context box, optionally with new context."""
        payload = request.get_json(silent=True) or {}
        item_id = str(payload.get("item_id") or "").strip()
        if not item_id:
            return jsonify({"error": "item_id is required."}), 400

        try:
            row, json_path, book = _load_book_for_edit(book_id)
            cache, ledger = _edit_cache_and_ledger(json_path)
            story = editor.regenerate_story(
                book, item_id,
                context_override=str(payload.get("context") or ""),
                cache=cache, ledger=ledger,
            )
            _save_book(book, json_path)
            usage = _merge_usage(book_id, row, ledger)
            return jsonify({"ok": True, "usage": usage, "story": story.to_dict()})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/stories/books/<book_id>/add-story")
    def stories_add_story(book_id: str):  # noqa: ANN202
        """Write and append one new story from a title + context box."""
        payload = request.get_json(silent=True) or {}
        try:
            row, json_path, book = _load_book_for_edit(book_id)
            cache, ledger = _edit_cache_and_ledger(json_path)
            story = editor.add_story(book, payload, cache=cache, ledger=ledger)
            _save_book(book, json_path)
            usage = _merge_usage(book_id, row, ledger)
            return jsonify({"ok": True, "usage": usage, "story": story.to_dict()})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.get("/api/stories/books/<book_id>/validate")
    def stories_validate_edits(book_id: str):  # noqa: ANN202
        try:
            _, _, book = _load_book_for_edit(book_id)
            return jsonify(editor.validate_edited_book(book))
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    # -- Illustrations -----------------------------------------------------

    @app.get("/api/stories/books/<book_id>/illustration")
    def stories_illustration(book_id: str):  # noqa: ANN202
        """Serve a story or chapter image for the preview pane."""
        try:
            _, _, book = _load_book_for_edit(book_id)
            sel = _image_selector(request.args.to_dict())
            target, _ = imgedit.resolve_target(book, **sel)
        except StoryError:
            abort(404)
        if not target.illustration_path:
            abort(404)
        path = Path(target.illustration_path)
        if not path.exists():
            abort(404)
        return send_file(str(path))

    @app.get("/api/stories/books/<book_id>/image-info")
    def stories_image_info(book_id: str):  # noqa: ANN202
        try:
            _, _, book = _load_book_for_edit(book_id)
            sel = _image_selector(request.args.to_dict())
            return jsonify(imgedit.image_info(book, **sel))
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/stories/books/<book_id>/regenerate-illustration")
    def stories_regen_illustration(book_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            sel = _image_selector(payload)
            prompt = editor.regenerate_illustration(
                book,
                prompt_hint=str(payload.get("prompt_hint") or ""),
                style_hint=str(payload.get("style_hint") or ""),
                out_dir=json_path.parent,
                **sel,
            )
            _save_book(book, json_path)
            return jsonify({"ok": True, "prompt": prompt})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/stories/books/<book_id>/upload-illustration")
    def stories_upload_illustration(book_id: str):  # noqa: ANN202
        file = request.files.get("image")
        if file is None or not file.filename:
            return jsonify({"error": "No image file was uploaded."}), 400
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            sel = _image_selector(request.form.to_dict())
            path = editor.set_illustration_from_upload(
                book, file, json_path.parent, **sel
            )
            _save_book(book, json_path)
            return jsonify({"ok": True, "path": path})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/stories/books/<book_id>/image/adjust")
    def stories_image_adjust(book_id: str):  # noqa: ANN202
        """Local brightness/contrast/saturation/sharpness. No API cost."""
        payload = request.get_json(silent=True) or {}
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            sel = _image_selector(payload)
            result = imgedit.adjust_image(
                book,
                brightness=float(payload.get("brightness", 1.0)),
                contrast=float(payload.get("contrast", 1.0)),
                saturation=float(payload.get("saturation", 1.0)),
                sharpness=float(payload.get("sharpness", 1.0)),
                **sel,
            )
            _save_book(book, json_path)
            return jsonify({"ok": True, **result.to_dict()})
        except (TypeError, ValueError):
            return jsonify({"error": "Adjustment values must be numbers."}), 400
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/stories/books/<book_id>/image/transform")
    def stories_image_transform(book_id: str):  # noqa: ANN202
        """Crop, rotate or flip — all local."""
        payload = request.get_json(silent=True) or {}
        op = str(payload.get("op") or "").strip()
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            sel = _image_selector(payload)

            if op == "crop":
                result = imgedit.crop_image(
                    book,
                    float(payload.get("left", 0)), float(payload.get("top", 0)),
                    float(payload.get("right", 1)), float(payload.get("bottom", 1)),
                    **sel,
                )
            elif op == "rotate":
                result = imgedit.rotate_image(
                    book, int(payload.get("degrees", 90)), **sel
                )
            elif op == "flip":
                result = imgedit.flip_image(
                    book, str(payload.get("axis") or "horizontal"), **sel
                )
            else:
                return jsonify({"error": "op must be crop, rotate or flip."}), 400

            _save_book(book, json_path)
            return jsonify({"ok": True, **result.to_dict()})
        except (TypeError, ValueError):
            return jsonify({"error": "Transform values must be numbers."}), 400
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/stories/books/<book_id>/image/ai-edit")
    def stories_image_ai_edit(book_id: str):  # noqa: ANN202
        """Natural-language image edit."""
        payload = request.get_json(silent=True) or {}
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            sel = _image_selector(payload)
            result = imgedit.ai_edit_image(
                book,
                str(payload.get("instruction") or ""),
                size=str(payload.get("size") or "auto"),
                quality=str(payload.get("quality") or "high"),
                **sel,
            )
            _save_book(book, json_path)
            return jsonify({"ok": True, **result.to_dict()})
        except StoryError as exc:
            # Image failures are usually upstream (content policy, size limits,
            # rate limits) and the message matters, so log it rather than
            # letting a bare 400 reach the browser with no trace.
            print(f"[stories] image ai-edit failed for {book_id}: {exc}", flush=True)
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/stories/books/<book_id>/image/undo")
    def stories_image_undo(book_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _, json_path, book = _load_book_for_edit(book_id)
            sel = _image_selector(payload)
            result = imgedit.undo(book, **sel)
            _save_book(book, json_path)
            return jsonify({"ok": True, **result.to_dict()})
        except StoryError as exc:
            return jsonify({"error": str(exc)}), 400

    # -- Download / re-export ---------------------------------------------

    @app.get("/api/stories/books/<book_id>/download/<kind>")
    def stories_download(book_id: str, kind: str):  # noqa: ANN202
        row = bookdb.get_story_book(book_id)
        if not row:
            abort(404)
        column = {
            "json": "json_path",
            "markdown": "markdown_path",
            "docx": "docx_path",
            "kindle": "kindle_path",
            "paperback": "paperback_path",
            "factcheck": "factcheck_path",
        }.get(kind)
        if not column:
            abort(404)
        path = row.get(column) or ""
        if not path or not Path(path).exists():
            abort(404)
        return send_file(path, as_attachment=True, download_name=Path(path).name)

    @app.post("/api/stories/books/<book_id>/reformat")
    def stories_reformat(book_id: str):  # noqa: ANN202
        """Rebuild Markdown/DOCX/KDP outputs from stored JSON without
        regenerating any content — the reason JSON is the source of truth."""
        row = bookdb.get_story_book(book_id)
        if not row:
            abort(404)
        json_path = Path(row.get("json_path") or "")
        if not json_path.exists():
            return jsonify({"error": "No stored JSON for this book."}), 400

        try:
            book = pipeline.load_json(json_path)
            out_dir = json_path.parent
            stem = _safe_stem(book.config.book_title)

            md_path = exporter.write_markdown(book, out_dir / f"{stem}.md")
            fc_path = exporter.write_factcheck(book, out_dir / f"{stem}_factcheck.md")
            docx_path = exporter.build_docx(book, out_dir / f"{stem}.docx")
            pipeline.write_json(book, json_path)

            updates: dict[str, Any] = {
                "markdown_path": str(md_path),
                "factcheck_path": str(fc_path),
                "docx_path": str(docx_path),
                "stories_written": len(book.all_stories()),
                "total_words": book.total_words(),
            }

            try:
                kdp = exporter.build_kdp_files(book, docx_path, out_dir)
                updates["kindle_path"] = kdp["kindle"]
                updates["paperback_path"] = kdp["paperback"]
            except Exception as exc:  # noqa: BLE001
                bookdb.update_story_book(book_id, **updates)
                return jsonify({
                    "ok": True,
                    "warning": f"KDP formatting failed: {exc}",
                    "outputs": updates,
                })

            bookdb.update_story_book(book_id, **updates)
            return jsonify({"ok": True, "outputs": updates})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
