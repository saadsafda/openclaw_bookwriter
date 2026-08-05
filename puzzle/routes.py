"""Puzzle & Activity book generator — Flask routes and job runner.

Registered onto the main app via register(app), the same way trivia/routes.py,
publications.py and book_editor.py attach their own routes. Everything here
lives under /puzzle and /api/puzzle/* and shares no state with the prose or
trivia pipelines.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from flask import abort, jsonify, render_template, request, send_file

import db as bookdb
from . import edit as editor
from . import export as exporter
from . import pipeline
from .engine import (
    ALL_SECTIONS,
    DEFAULT_COUNTS,
    SECTION_LABELS,
    BookConfig,
    PuzzleError,
    RawOutputCache,
    UsageLedger,
)

# This file lives in <project>/puzzle/; outputs belong beside the other project
# data, not inside the package.
ROOT_DIR = Path(__file__).resolve().parent.parent
PUZZLE_OUTPUT_DIR = ROOT_DIR / "puzzle_outputs"
PUZZLE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_LOG_LINES = 600


@dataclass
class PuzzleJob:
    id: str
    config: BookConfig
    status: str = "queued"          # queued | running | done | error | stopped
    stage: str = ""
    progress: float = 0.0
    logs: list[str] = field(default_factory=list)
    error: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
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
            "counts": dict(self.counts),
            "usage": dict(self.usage),
            "logs": self.logs[-120:],
        }


JOBS: dict[str, PuzzleJob] = {}
JOBS_LOCK = threading.Lock()


def _job_dir(job_id: str) -> Path:
    return PUZZLE_OUTPUT_DIR / job_id


def _safe_stem(title: str) -> str:
    keep = [c if c.isalnum() or c in " -_" else "" for c in (title or "puzzle")]
    stem = "".join(keep).strip().replace(" ", "_") or "puzzle_book"
    return stem[:60]


def _run_build(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return

    job.status = "running"
    job.stage = "starting"
    bookdb.update_puzzle_book(job_id, status="running", stage="starting")

    out_dir = _job_dir(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _log(message: str) -> None:
        job.log(message)

    def _progress(stage: str, pct: float) -> None:
        job.stage = stage
        job.progress = max(0.0, min(1.0, pct))
        bookdb.update_puzzle_book(job_id, stage=stage, progress=job.progress)

    try:
        builder = pipeline.PuzzleBuilder(
            job.config,
            log=_log,
            progress=_progress,
            should_stop=lambda: job.stop_requested,
            cache_dir=out_dir / "cache",
        )
        book = builder.build(out_dir)
        job.warnings = list(book.warnings)
        job.counts = book.counts()
        job.usage = dict(book.usage)

        # -- exports -------------------------------------------------------
        job.stage = "exporting"
        _log("Writing JSON, Markdown and DOCX")
        json_path = pipeline.write_json(book, out_dir / "puzzle_book.json")
        md_path = exporter.write_markdown(book, out_dir / "puzzle_book.md")
        docx_path = exporter.build_docx(book, out_dir / "puzzle_book.docx")

        job.outputs = {
            "json": str(json_path),
            "markdown": str(md_path),
            "docx": str(docx_path),
        }

        _log("Verifying every image is 300 DPI with no AI metadata")
        image_problems = exporter.verify_print_images(book, out_dir)
        if image_problems:
            for problem in image_problems:
                _log(f"WARNING: print check — {problem}")
            job.warnings.extend(f"Print check — {p}" for p in image_problems)
        else:
            _log("Print check passed: all images 300 DPI, metadata clean")

        # KDP formatting is best-effort: a failure there must not lose the
        # manuscript we already produced.
        try:
            _log("Building KDP 6x9 print files")
            kdp = exporter.build_kdp_files(book, docx_path, out_dir)
            job.outputs["kindle"] = kdp["kindle"]
            job.outputs["paperback"] = kdp["paperback"]
            _log(f"KDP files ready (~{kdp['estimated_pages']} pages)")
        except Exception as exc:  # noqa: BLE001 - report, never fail the build
            msg = f"KDP formatting failed: {exc}"
            _log(f"WARNING: {msg}")
            job.warnings.append(msg)

        _log("Packing the formatter handoff zip")
        stem = _safe_stem(job.config.book_title)
        zip_path = exporter.build_handoff_zip(book, out_dir, out_dir / f"{stem}_handoff.zip")
        job.outputs["zip"] = str(zip_path)

        job.status = "done"
        job.stage = "done"
        job.progress = 1.0
        counts = ", ".join(
            f"{n} {SECTION_LABELS[k].lower()}" for k, n in job.counts.items() if n
        )
        _log(f"Build complete — {counts}")

        bookdb.update_puzzle_book(
            job_id,
            status="done", stage="done", progress=1.0,
            counts_json=json.dumps(job.counts),
            estimated_pages=book.config.page_estimate().get("total_pages", 0),
            json_path=str(json_path),
            markdown_path=str(md_path),
            docx_path=str(docx_path),
            kindle_path=job.outputs.get("kindle", ""),
            paperback_path=job.outputs.get("paperback", ""),
            zip_path=str(zip_path),
            warnings_json=json.dumps(job.warnings),
            usage_json=json.dumps(job.usage),
        )

    except PuzzleError as exc:
        stopped = job.stop_requested
        job.status = "stopped" if stopped else "error"
        job.error = "" if stopped else str(exc)
        job.stage = job.status
        _log(("Stopped by operator." if stopped else f"ERROR: {exc}"))
        bookdb.update_puzzle_book(
            job_id, status=job.status, stage=job.stage, error=job.error
        )
    except Exception as exc:  # noqa: BLE001 - surface unexpected failures
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        job.stage = "error"
        _log(f"ERROR: {job.error}")
        bookdb.update_puzzle_book(
            job_id, status="error", stage="error", error=job.error
        )


def _parse_config(payload: Any) -> BookConfig:
    raw = payload
    if isinstance(payload, dict) and isinstance(payload.get("config"), dict):
        raw = payload["config"]
    return BookConfig.from_dict(raw)


# ---------------------------------------------------------------------------
# Preview & Edit helpers
# ---------------------------------------------------------------------------

def _load_book_for_edit(book_id: str) -> tuple[dict[str, Any], Path, Any]:
    """Fetch a book row, its JSON path, and the rehydrated book object.

    Raises PuzzleError with a caller-friendly message; routes turn that into a
    4xx rather than a 500.
    """
    row = bookdb.get_puzzle_book(book_id)
    if not row:
        raise PuzzleError("Book not found.")
    json_path = Path(row.get("json_path") or "")
    if not json_path.exists():
        raise PuzzleError("This book has no generated content yet.")
    return row, json_path, pipeline.load_json(json_path)


def _save_book(book: Any, json_path: Path) -> None:
    pipeline.write_json(book, json_path)


# Formats the manuscript editor can open. "docx" is the plain manuscript the
# exporter builds; the other two are the KDP-formatted 6x9 print files.
MANUSCRIPT_FORMATS = ("docx", "paperback", "kindle")


def _manuscript_path(row: dict[str, Any], which: str) -> str:
    """Resolve a format name to a .docx on disk.

    Falls back through the other formats so the editor still opens when only
    one of them was built (KDP formatting is best-effort during a build).
    """
    mapping = {
        "docx": row.get("docx_path"),
        "paperback": row.get("paperback_path"),
        "kindle": row.get("kindle_path"),
    }
    chosen = mapping.get(which)
    if chosen and Path(chosen).exists():
        return chosen
    for key in MANUSCRIPT_FORMATS:
        candidate = mapping.get(key)
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def _edit_cache_and_ledger(json_path: Path) -> tuple[RawOutputCache, UsageLedger]:
    """AI edits share the build's raw-output cache, so repeating the same edit
    request costs nothing."""
    return RawOutputCache(json_path.parent / "cache"), UsageLedger()


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
    bookdb.update_puzzle_book(book_id, usage_json=json.dumps(totals))
    return totals


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(app) -> None:  # noqa: ANN001
    """Attach all Puzzle generator routes to the given Flask app."""

    @app.get("/puzzle")
    def puzzle_page():  # noqa: ANN202
        return render_template("puzzle.html")

    @app.get("/api/puzzle/defaults")
    def puzzle_defaults():  # noqa: ANN202
        """Section list and pilot-book defaults, so the UI never hardcodes them."""
        return jsonify({
            "sections": [
                {
                    "kind": kind,
                    "label": SECTION_LABELS[kind],
                    "default_count": DEFAULT_COUNTS.get(kind, 0),
                    # Section 1 stays human — flagged so the UI can say so.
                    "human": kind == "picture_puzzles",
                    "unit": "chapters" if kind == "trivia" else "puzzles",
                }
                for kind in ALL_SECTIONS
            ],
        })

    @app.post("/api/puzzle/validate-config")
    def puzzle_validate_config():  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            cfg = _parse_config(payload)
        except PuzzleError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({
            "ok": True,
            "book_title": cfg.book_title,
            "page_estimate": cfg.page_estimate(),
            "sections": {
                k: {"enabled": cfg.section(k).enabled, "count": cfg.section(k).count}
                for k in ALL_SECTIONS
            },
        })

    @app.post("/api/puzzle/jobs")
    def puzzle_create_job():  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            cfg = _parse_config(payload)
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

        job_id = uuid.uuid4().hex[:8]
        job = PuzzleJob(id=job_id, config=cfg)
        enabled = [
            f"{cfg.section(k).count} {SECTION_LABELS[k].lower()}"
            for k in ALL_SECTIONS if cfg.section(k).enabled and cfg.section(k).count
        ]
        job.log(f"Queued '{cfg.book_title}' — " + ", ".join(enabled))
        with JOBS_LOCK:
            JOBS[job_id] = job

        est = cfg.page_estimate()
        bookdb.save_puzzle_book(
            job_id,
            cfg.book_title,
            cfg.topic,
            status="queued",
            agent=cfg.agent,
            audience=cfg.audience,
            difficulty=cfg.difficulty,
            estimated_pages=est.get("total_pages", 0),
            config_json=json.dumps(cfg.to_dict()),
        )

        threading.Thread(target=_run_build, args=(job_id,), daemon=True).start()
        return jsonify({"job_id": job_id, "status": "queued", "page_estimate": est})

    @app.get("/api/puzzle/jobs/<job_id>/status")
    def puzzle_job_status(job_id: str):  # noqa: ANN202
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            row = bookdb.get_puzzle_book(job_id)
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
                    for k in ("json", "markdown", "docx", "kindle", "paperback", "zip")
                    if row.get(f"{k}_path")
                },
                "warnings": json.loads(row.get("warnings_json") or "[]"),
                "counts": json.loads(row.get("counts_json") or "{}"),
                "usage": json.loads(row.get("usage_json") or "{}"),
                "logs": [],
            })
        return jsonify(job.to_status())

    @app.post("/api/puzzle/jobs/<job_id>/stop")
    def puzzle_stop_job(job_id: str):  # noqa: ANN202
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            abort(404)
        job.stop_requested = True
        job.log("Stop requested — finishing current step.")
        return jsonify({"ok": True})

    @app.get("/api/puzzle/books")
    def puzzle_list_books():  # noqa: ANN202
        return jsonify({"books": bookdb.list_puzzle_books()})

    @app.get("/api/puzzle/books/<book_id>")
    def puzzle_get_book(book_id: str):  # noqa: ANN202
        row = bookdb.get_puzzle_book(book_id)
        if not row:
            abort(404)
        return jsonify({"book": row})

    @app.delete("/api/puzzle/books/<book_id>")
    def puzzle_delete_book(book_id: str):  # noqa: ANN202
        if not bookdb.delete_puzzle_book(book_id):
            abort(404)
        with JOBS_LOCK:
            JOBS.pop(book_id, None)
        return jsonify({"ok": True})

    @app.get("/api/puzzle/books/<book_id>/content")
    def puzzle_book_content(book_id: str):  # noqa: ANN202
        """Full structured book JSON — the source of truth for re-formatting."""
        row = bookdb.get_puzzle_book(book_id)
        if not row:
            abort(404)
        json_path = row.get("json_path") or ""
        if not json_path or not Path(json_path).exists():
            return jsonify({"error": "No generated content for this book yet."}), 404
        return jsonify(json.loads(Path(json_path).read_text(encoding="utf-8")))

    @app.get("/api/puzzle/books/<book_id>/download/<kind>")
    def puzzle_download(book_id: str, kind: str):  # noqa: ANN202
        row = bookdb.get_puzzle_book(book_id)
        if not row:
            abort(404)
        if kind not in {"json", "markdown", "docx", "kindle", "paperback", "zip"}:
            abort(404)
        path_str = row.get(f"{kind}_path") or ""
        if not path_str or not Path(path_str).exists():
            return jsonify({"error": f"No {kind} file for this book."}), 404
        return send_file(path_str, as_attachment=True)

    @app.get("/api/puzzle/books/<book_id>/image/<section>/<int:number>")
    def puzzle_image(book_id: str, section: str, number: int):  # noqa: ANN202
        """Serve a rendered puzzle or its solution for the preview pane."""
        row = bookdb.get_puzzle_book(book_id)
        if not row:
            abort(404)
        json_path = row.get("json_path") or ""
        if not json_path or not Path(json_path).exists():
            abort(404)

        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        key = {
            "maze": "mazes",
            "wordsearch": "word_searches",
            "crossword": "crosswords",
        }.get(section)
        if key is None:
            abort(404)

        want_solution = request.args.get("solution", "").strip().lower() in {"1", "true", "yes"}
        field_name = "solution_path" if want_solution else "image_path"
        for item in data.get(key) or []:
            if int(item.get("number") or 0) == number:
                path_str = item.get(field_name) or ""
                if path_str and Path(path_str).exists():
                    return send_file(path_str, mimetype="image/png")
                abort(404)
        abort(404)

    # -- Preview & Edit ----------------------------------------------------

    @app.get("/puzzle/<book_id>/edit")
    def puzzle_edit_page(book_id: str):  # noqa: ANN202
        row = bookdb.get_puzzle_book(book_id)
        if not row:
            abort(404)
        return render_template("puzzle_edit.html", book_id=book_id,
                               book_title=row.get("title", ""))

    @app.get("/api/puzzle/books/<book_id>/item-image/<item_id>")
    def puzzle_item_image(book_id: str, item_id: str):  # noqa: ANN202
        """Serve one item's artwork by id, for the preview pane.

        Cache-busted by the caller with ?v=<counter>, because a re-render
        writes to the same path and the browser would otherwise reuse the
        stale image.
        """
        try:
            _row, _json_path, book = _load_book_for_edit(book_id)
        except PuzzleError:
            abort(404)
        _kind, item = editor.find_any(book, item_id)
        if item is None:
            abort(404)
        want_solution = request.args.get("solution", "").strip().lower() in {"1", "true", "yes"}
        path_str = getattr(item, "solution_path" if want_solution else "image_path", "")
        if not path_str or not Path(path_str).exists():
            abort(404)
        response = send_file(path_str, mimetype="image/png")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.patch("/api/puzzle/books/<book_id>/riddles/<item_id>")
    def puzzle_edit_riddle(book_id: str, item_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _row, json_path, book = _load_book_for_edit(book_id)
            item = editor.apply_riddle_edit(book, item_id, payload)
            _save_book(book, json_path)
            return jsonify({"ok": True, "riddle": item.to_dict()})
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/puzzle/books/<book_id>/cryptograms/<item_id>")
    def puzzle_edit_cryptogram(book_id: str, item_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _row, json_path, book = _load_book_for_edit(book_id)
            item = editor.apply_cryptogram_edit(book, item_id, payload)
            _save_book(book, json_path)
            return jsonify({"ok": True, "cryptogram": item.to_dict()})
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/puzzle/books/<book_id>/cryptograms/<item_id>/reshuffle")
    def puzzle_reshuffle_cipher(book_id: str, item_id: str):  # noqa: ANN202
        try:
            _row, json_path, book = _load_book_for_edit(book_id)
            item = editor.reshuffle_cipher(book, item_id)
            _save_book(book, json_path)
            return jsonify({"ok": True, "cryptogram": item.to_dict()})
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/puzzle/books/<book_id>/questions/<item_id>")
    def puzzle_edit_question(book_id: str, item_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _row, json_path, book = _load_book_for_edit(book_id)
            item = editor.apply_question_edit(book, item_id, payload)
            _save_book(book, json_path)
            return jsonify({"ok": True, "question": item.to_dict()})
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/puzzle/books/<book_id>/briefs/<item_id>")
    def puzzle_edit_brief(book_id: str, item_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _row, json_path, book = _load_book_for_edit(book_id)
            item = editor.apply_brief_edit(book, item_id, payload)
            _save_book(book, json_path)
            return jsonify({"ok": True, "brief": item.to_dict()})
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/puzzle/books/<book_id>/title/<item_id>")
    def puzzle_edit_title(book_id: str, item_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        try:
            _row, json_path, book = _load_book_for_edit(book_id)
            item = editor.apply_title_edit(book, item_id, str(payload.get("title") or ""))
            # The title is printed on the artwork, so it has to be redrawn.
            try:
                editor.rerender_item(book, item_id)
            except PuzzleError:
                pass
            _save_book(book, json_path)
            return jsonify({"ok": True, "title": item.title})
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/puzzle/books/<book_id>/word-searches/<item_id>")
    def puzzle_edit_wordsearch(book_id: str, item_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}
        words = payload.get("words")
        if isinstance(words, str):
            words = [w for w in re.split(r"[\n,]+", words)]
        if not isinstance(words, list):
            return jsonify({"error": "words must be a list."}), 400
        try:
            _row, json_path, book = _load_book_for_edit(book_id)
            item = editor.update_word_search_words(book, item_id, words)
            _save_book(book, json_path)
            return jsonify({"ok": True, "word_search": item.to_dict()})
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.patch("/api/puzzle/books/<book_id>/crosswords/<item_id>")
    def puzzle_edit_crossword(book_id: str, item_id: str):  # noqa: ANN202
        """Edit clues alone, or replace the whole word set.

        Clue-only edits keep the existing grid; a new word set has to relayout
        and can fail, which is reported rather than silently dropped.
        """
        payload = request.get_json(silent=True) or {}
        try:
            _row, json_path, book = _load_book_for_edit(book_id)
            if isinstance(payload.get("entries"), list):
                item = editor.update_crossword_entries(book, item_id, payload["entries"])
            elif isinstance(payload.get("clues"), dict):
                item = editor.update_crossword_clues(book, item_id, payload["clues"])
            else:
                return jsonify({"error": "Send either 'entries' or 'clues'."}), 400
            _save_book(book, json_path)
            return jsonify({"ok": True, "crossword": item.to_dict()})
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/puzzle/books/<book_id>/mazes/<item_id>/regenerate")
    def puzzle_regen_maze(book_id: str, item_id: str):  # noqa: ANN202
        payload = request.get_json(silent=True) or {}

        def _int(key: str) -> int:
            try:
                return int(payload.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        try:
            _row, json_path, book = _load_book_for_edit(book_id)
            item = editor.regenerate_maze(
                book, item_id, cols=_int("cols"), rows=_int("rows"), seed=_int("seed"))
            _save_book(book, json_path)
            return jsonify({"ok": True, "maze": item.to_dict()})
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.delete("/api/puzzle/books/<book_id>/items/<item_id>")
    def puzzle_delete_item(book_id: str, item_id: str):  # noqa: ANN202
        try:
            _row, json_path, book = _load_book_for_edit(book_id)
            if not editor.delete_item(book, item_id):
                return jsonify({"error": f"'{item_id}' not found."}), 404
            _save_book(book, json_path)
            return jsonify({"ok": True, "counts": book.counts()})
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/puzzle/books/<book_id>/ai-edit")
    def puzzle_ai_edit(book_id: str):  # noqa: ANN202
        """Apply an AI action to one item."""
        payload = request.get_json(silent=True) or {}
        item_id = str(payload.get("item_id") or "").strip()
        action = str(payload.get("action") or "").strip()
        instruction = str(payload.get("instruction") or "").strip()
        if not item_id or not action:
            return jsonify({"error": "item_id and action are required."}), 400

        try:
            row, json_path, book = _load_book_for_edit(book_id)
            cache, ledger = _edit_cache_and_ledger(json_path)
            kind, item = editor.ai_edit_item(
                book, item_id, action, instruction, cache=cache, ledger=ledger)
            _save_book(book, json_path)
            usage = _merge_usage(book_id, row, ledger)
            return jsonify({
                "ok": True, "kind": kind, "usage": usage,
                "item": item.to_dict() if hasattr(item, "to_dict") else {},
            })
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.get("/api/puzzle/books/<book_id>/validate")
    def puzzle_validate_book(book_id: str):  # noqa: ANN202
        try:
            _row, _json_path, book = _load_book_for_edit(book_id)
            return jsonify(editor.validate_edited_book(book))
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

    # -- Manuscript editing (Word-style toolbar over the built .docx) -------
    #
    # These three routes are the contract BookEditorWidget expects, matching
    # /api/books/<id>/content used by the prose editor. The heavy lifting
    # (docx <-> HTML round-trip) is book_editor's, shared as-is.

    @app.get("/api/puzzle/books/<book_id>/manuscript")
    def puzzle_manuscript_get(book_id: str):  # noqa: ANN202
        row = bookdb.get_puzzle_book(book_id)
        if not row:
            abort(404)
        which = (request.args.get("which") or "docx").lower()
        path = _manuscript_path(row, which)
        if not path or not Path(path).exists():
            return jsonify({"error": "That version has not been built yet.",
                            "which": which}), 404

        from book_editor import read_blocks
        return jsonify({
            "title": row.get("title") or "",
            "which": which,
            "blocks": read_blocks(Path(path)),
        })

    @app.post("/api/puzzle/books/<book_id>/manuscript")
    def puzzle_manuscript_save(book_id: str):  # noqa: ANN202
        row = bookdb.get_puzzle_book(book_id)
        if not row:
            abort(404)
        which = (request.args.get("which") or "docx").lower()
        path = _manuscript_path(row, which)
        if not path or not Path(path).exists():
            return jsonify({"error": "That version has not been built yet."}), 404

        from book_editor import _parse_edits, write_blocks
        edits = _parse_edits(request.get_json(silent=True) or {})
        if edits is None:
            return jsonify({"error": "body must be {edits: [{index, html}]}"}), 400
        result = write_blocks(Path(path), edits)
        return jsonify({"ok": True, "which": which, **result})

    @app.get("/api/puzzle/books/<book_id>/manuscript/image/<int:para_index>")
    def puzzle_manuscript_image(book_id: str, para_index: int):  # noqa: ANN202
        row = bookdb.get_puzzle_book(book_id)
        if not row:
            abort(404)
        which = (request.args.get("which") or "docx").lower()
        path = _manuscript_path(row, which)
        if not path or not Path(path).exists():
            abort(404)
        from book_editor import _serve_image
        return _serve_image(Path(path), para_index)

    @app.post("/api/puzzle/books/<book_id>/toc")
    def puzzle_manuscript_toc(book_id: str):  # noqa: ANN202
        """Insert or refresh a Table of Contents page — the widget's TOC button."""
        row = bookdb.get_puzzle_book(book_id)
        if not row:
            abort(404)
        which = (request.args.get("which") or "docx").lower()
        path = _manuscript_path(row, which)
        if not path or not Path(path).exists():
            return jsonify({"error": "That version has not been built yet."}), 404

        from book_editor import _toc_anchor, insert_or_refresh_toc
        try:
            result = insert_or_refresh_toc(Path(path), **_toc_anchor(request))
            return jsonify({"ok": True, **result})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    @app.post("/api/puzzle/books/<book_id>/reexport")
    def puzzle_reexport(book_id: str):  # noqa: ANN202
        """Rebuild the DOCX, KDP files and handoff zip from the edited JSON.

        No model calls: this restructures what is already on disk.
        """
        try:
            _row, json_path, book = _load_book_for_edit(book_id)
        except PuzzleError as exc:
            return jsonify({"error": str(exc)}), 400

        out_dir = json_path.parent
        warnings: list[str] = []
        try:
            md_path = exporter.write_markdown(book, out_dir / "puzzle_book.md")
            docx_path = exporter.build_docx(book, out_dir / "puzzle_book.docx")

            outputs = {
                "json": str(json_path),
                "markdown": str(md_path),
                "docx": str(docx_path),
            }
            warnings.extend(exporter.verify_print_images(book, out_dir))
            try:
                kdp = exporter.build_kdp_files(book, docx_path, out_dir)
                outputs["kindle"] = kdp["kindle"]
                outputs["paperback"] = kdp["paperback"]
            except Exception as exc:  # noqa: BLE001 - never lose the manuscript
                warnings.append(f"KDP formatting failed: {exc}")

            zip_path = exporter.build_handoff_zip(
                book, out_dir, out_dir / f"{_safe_stem(book.config.book_title)}_handoff.zip")
            outputs["zip"] = str(zip_path)

            bookdb.update_puzzle_book(
                book_id,
                markdown_path=str(md_path),
                docx_path=str(docx_path),
                kindle_path=outputs.get("kindle", ""),
                paperback_path=outputs.get("paperback", ""),
                zip_path=str(zip_path),
                counts_json=json.dumps(book.counts()),
                estimated_pages=book.config.page_estimate().get("total_pages", 0),
            )
            return jsonify({"ok": True, "outputs": outputs, "warnings": warnings})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
