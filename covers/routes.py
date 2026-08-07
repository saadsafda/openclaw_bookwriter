"""Cover generation — Flask routes and job runner.

Registered onto the main app via register(app), the same way trivia, puzzle
and stories attach their own routes. Everything here lives under /covers and
/api/covers/* and shares no state with the book pipelines beyond the API-key
helper it borrows from openclaw_image_maker.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import jsonify, render_template, request, send_file

import openclaw_image_maker as image_maker

from . import library
from . import prompts as promptlib
from .engine import (
    DEFAULT_QUALITY,
    DEFAULT_SIZE,
    VALID_QUALITIES,
    VALID_SIZES,
    CoverGenerationError,
    CoverRun,
    generate_covers,
)
from .library import CoverLibraryError

_JOBS: dict[str, "CoverJob"] = {}
_JOBS_LOCK = threading.Lock()


@dataclass
class CoverJob:
    id: str
    status: str = "queued"          # queued | running | done | error
    error: str = ""
    run: CoverRun | None = None
    title: str = ""
    prompt: str = ""
    total: int = 0
    _done: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "title": self.title,
            "prompt": self.prompt,
            "total": self.total,
            "done_count": self._done,
        }
        if self.run is not None:
            payload["run"] = self.run.to_dict()
        return payload


def _api_key() -> str:
    return image_maker.resolve_api_key("")


def _run_job(job: CoverJob, title: str, prompt: str, ref_ids: list[str],
             book_type: str, size: str, quality: str) -> None:
    try:
        paths = library.resolve_reference_paths(ref_ids)
        job.total = len(paths)
        job.status = "running"

        def _progress(run: CoverRun) -> None:
            job.run = run
            job._done = sum(1 for v in run.variations if v.status != "pending")

        run = generate_covers(
            title=title,
            prompt=prompt,
            references=paths,
            api_key=_api_key(),
            book_type=book_type,
            size=size,
            quality=quality,
            on_progress=_progress,
        )
        job.run = run
        job._done = sum(1 for v in run.variations if v.status != "pending")
        job.status = "done"
    except (CoverGenerationError, CoverLibraryError) as exc:
        job.status = "error"
        job.error = str(exc)
    except Exception as exc:  # unexpected — still surface it to the operator
        job.status = "error"
        job.error = f"Unexpected error: {exc}"


def register(app) -> None:
    @app.route("/covers")
    def covers_page():
        return render_template(
            "covers.html",
            profiles=[
                {"key": p.key, "label": p.label}
                for p in promptlib.PROFILES.values()
            ],
            default_type=promptlib.DEFAULT_TYPE,
            sizes=sorted(VALID_SIZES),
            qualities=sorted(VALID_QUALITIES),
            default_size=DEFAULT_SIZE,
            default_quality=DEFAULT_QUALITY,
        )

    # ---- reference library -------------------------------------------------

    @app.route("/api/covers/references", methods=["GET"])
    def covers_list_references():
        refs = [r.to_dict() for r in library.list_references()]
        return jsonify({"references": refs})

    @app.route("/api/covers/references", methods=["POST"])
    def covers_add_reference():
        uploaded = request.files.getlist("files") or request.files.getlist("file")
        if not uploaded:
            return jsonify({"error": "No file uploaded."}), 400

        added, errors = [], []
        for item in uploaded:
            try:
                ref = library.add_reference(
                    item.read(),
                    item.filename or "reference.png",
                    request.form.get("label", ""),
                )
                added.append(ref.to_dict())
            except CoverLibraryError as exc:
                errors.append(f"{item.filename}: {exc}")

        if not added and errors:
            return jsonify({"error": " ".join(errors)}), 400
        return jsonify({"added": added, "errors": errors})

    @app.route("/api/covers/references/<ref_id>", methods=["DELETE"])
    def covers_delete_reference(ref_id: str):
        try:
            library.delete_reference(ref_id)
        except CoverLibraryError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"ok": True})

    @app.route("/api/covers/references/<ref_id>/image")
    def covers_reference_image(ref_id: str):
        try:
            ref = library.get_reference(ref_id)
        except CoverLibraryError as exc:
            return jsonify({"error": str(exc)}), 404
        if not ref.exists():
            return jsonify({"error": "Reference file is missing."}), 404
        return send_file(ref.path, mimetype="image/png")

    # ---- prompt ------------------------------------------------------------

    @app.route("/api/covers/prompt", methods=["POST"])
    def covers_build_prompt():
        data = request.get_json(silent=True) or {}
        title = str(data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "A book title is required."}), 400

        book_type = str(data.get("book_type") or promptlib.DEFAULT_TYPE)
        topic = str(data.get("topic") or "")
        audience = str(data.get("audience") or "")
        use_ai = bool(data.get("draft", True))

        try:
            if use_ai:
                prompt, drafted = promptlib.draft_prompt(
                    title, book_type, topic, audience, api_key=_api_key()
                )
            else:
                prompt, drafted = promptlib.build_prompt(
                    title, book_type, topic, audience
                ), False
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify({"prompt": prompt, "drafted": drafted})

    # ---- generation --------------------------------------------------------

    @app.route("/api/covers/generate", methods=["POST"])
    def covers_generate():
        data = request.get_json(silent=True) or {}
        title = str(data.get("title") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        if not title:
            return jsonify({"error": "A book title is required."}), 400
        if not prompt:
            return jsonify({"error": "A cover prompt is required."}), 400
        if not _api_key():
            return jsonify({"error": "No OpenAI API key configured."}), 400

        book_type = str(data.get("book_type") or promptlib.DEFAULT_TYPE)
        size = str(data.get("size") or DEFAULT_SIZE)
        quality = str(data.get("quality") or DEFAULT_QUALITY)
        ref_ids = data.get("reference_ids") or []
        if not isinstance(ref_ids, list):
            ref_ids = []
        ref_ids = [str(r) for r in ref_ids]

        if size not in VALID_SIZES:
            return jsonify({"error": f"size must be one of {sorted(VALID_SIZES)}."}), 400
        if quality not in VALID_QUALITIES:
            return jsonify({"error": f"quality must be one of {sorted(VALID_QUALITIES)}."}), 400

        job = CoverJob(id=uuid.uuid4().hex[:12], title=title, prompt=prompt)
        with _JOBS_LOCK:
            _JOBS[job.id] = job

        thread = threading.Thread(
            target=_run_job,
            args=(job, title, prompt, ref_ids, book_type, size, quality),
            daemon=True,
        )
        thread.start()
        return jsonify({"job_id": job.id})

    @app.route("/api/covers/jobs/<job_id>")
    def covers_job_status(job_id: str):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "No such job."}), 404
        return jsonify(job.to_dict())

    @app.route("/api/covers/jobs/<job_id>/image/<filename>")
    def covers_job_image(job_id: str, filename: str):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None or job.run is None:
            return jsonify({"error": "No such job."}), 404

        # basename() so a crafted filename cannot escape the run directory.
        safe = Path(filename).name
        path = job.run.output_dir / safe
        if not path.is_file():
            return jsonify({"error": "No such image."}), 404
        return send_file(path, mimetype="image/png")

    @app.route("/api/covers/jobs/<job_id>/download/<filename>")
    def covers_job_download(job_id: str, filename: str):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None or job.run is None:
            return jsonify({"error": "No such job."}), 404

        safe = Path(filename).name
        path = job.run.output_dir / safe
        if not path.is_file():
            return jsonify({"error": "No such image."}), 404

        slug = "".join(
            c if c.isalnum() else "_" for c in job.title.lower()
        ).strip("_") or "cover"
        return send_file(
            path,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"{slug}_{safe}",
        )
