"""Bird pipeline — Flask routes and job runner.

Registered onto the main app via register(app), the same way trivia, puzzle,
stories and covers attach their own routes. Everything lives under /birds and
/api/birds/* and shares no state with the book pipelines beyond the API-key
helper it borrows from openclaw_image_maker.

A 250-bird run takes long enough that it cannot be a request: generation runs
on a background thread and the page polls the job. The whole finished batch
comes back as one ZIP, which is the form the layout step actually wants.
"""

from __future__ import annotations

import io
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import jsonify, render_template, request, send_file

import openclaw_image_maker as image_maker

from . import export as exportlib
from . import library
from . import prompts as promptlib
from .engine import (
    DEFAULT_FIDELITY,
    DEFAULT_QUALITY,
    DEFAULT_SIZE,
    DEFAULT_WORKERS,
    MAX_WORKERS,
    VALID_FIDELITIES,
    VALID_QUALITIES,
    VALID_SIZES,
    BirdGenerationError,
    BirdRun,
    generate_plates,
)
from .library import BirdLibraryError

_JOBS: dict[str, "BirdJob"] = {}
_JOBS_LOCK = threading.Lock()


@dataclass
class BirdJob:
    id: str
    status: str = "queued"          # queued | running | done | error
    error: str = ""
    run: BirdRun | None = None
    batch_name: str = ""
    total: int = 0
    cancel_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "batch_name": self.batch_name,
            "total": self.total,
            "cancel_requested": self.cancel_requested,
        }
        if self.run is not None:
            payload["run"] = self.run.to_dict()
        return payload


def _api_key() -> str:
    return image_maker.resolve_api_key("")


def _slug(name: str, fallback: str = "birds") -> str:
    """Filename-safe stem for a download name."""
    cleaned = "".join(c if c.isalnum() else "_" for c in (name or "").lower())
    return "_".join(part for part in cleaned.split("_") if part)[:60] or fallback


def _get_job(job_id: str) -> BirdJob | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def _run_job(
    job: BirdJob,
    batch_id: str,
    source_ids: list[str],
    style: str,
    notes: str,
    size: str,
    quality: str,
    fidelity: str,
    workers: int,
    trim: bool,
) -> None:
    try:
        batch, sources = library.resolve_sources(batch_id, source_ids)
        job.total = len(sources)
        job.batch_name = batch.name
        job.status = "running"

        def _prompt_for(species: str) -> str:
            return promptlib.build_prompt(species, style, notes)

        run = generate_plates(
            sources=[(s.id, s.species, batch.source_path(s)) for s in sources],
            prompt_for=_prompt_for,
            api_key=_api_key(),
            batch_id=batch.id,
            batch_name=batch.name,
            style=style,
            size=size,
            quality=quality,
            fidelity=fidelity,
            workers=workers,
            trim=trim,
            on_progress=lambda r: setattr(job, "run", r),
            should_cancel=lambda: job.cancel_requested,
        )
        job.run = run
        run.cancelled = job.cancel_requested
        job.status = "done"
    except (BirdGenerationError, BirdLibraryError) as exc:
        job.status = "error"
        job.error = str(exc)
    except Exception as exc:  # unexpected — still surface it to the operator
        job.status = "error"
        job.error = f"Unexpected error: {exc}"


def register(app) -> None:
    @app.route("/birds")
    def birds_page():
        return render_template(
            "birds.html",
            styles=[
                {"key": p.key, "label": p.label}
                for p in promptlib.PROFILES.values()
            ],
            default_style=promptlib.DEFAULT_STYLE,
            sizes=sorted(VALID_SIZES),
            qualities=sorted(VALID_QUALITIES),
            fidelities=sorted(VALID_FIDELITIES),
            default_size=DEFAULT_SIZE,
            default_quality=DEFAULT_QUALITY,
            default_fidelity=DEFAULT_FIDELITY,
            default_workers=DEFAULT_WORKERS,
            max_workers=MAX_WORKERS,
        )

    # ---- batches -----------------------------------------------------------

    @app.route("/api/birds/batches", methods=["GET"])
    def birds_list_batches():
        batches = [b.to_dict(include_sources=False) for b in library.list_batches()]
        return jsonify({"batches": batches})

    @app.route("/api/birds/batches", methods=["POST"])
    def birds_create_batch():
        data = request.get_json(silent=True) or {}
        batch = library.create_batch(str(data.get("name") or ""))
        return jsonify({"batch": batch.to_dict()})

    @app.route("/api/birds/batches/<batch_id>", methods=["GET"])
    def birds_get_batch(batch_id: str):
        try:
            batch = library.get_batch(batch_id)
        except BirdLibraryError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"batch": batch.to_dict()})

    @app.route("/api/birds/batches/<batch_id>", methods=["PATCH"])
    def birds_rename_batch(batch_id: str):
        data = request.get_json(silent=True) or {}
        try:
            batch = library.rename_batch(batch_id, str(data.get("name") or ""))
        except BirdLibraryError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"batch": batch.to_dict(include_sources=False)})

    @app.route("/api/birds/batches/<batch_id>", methods=["DELETE"])
    def birds_delete_batch(batch_id: str):
        try:
            library.delete_batch(batch_id)
        except BirdLibraryError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"ok": True})

    # ---- source photos -----------------------------------------------------

    @app.route("/api/birds/batches/<batch_id>/sources", methods=["POST"])
    def birds_add_sources(batch_id: str):
        uploaded = request.files.getlist("files") or request.files.getlist("file")
        if not uploaded:
            return jsonify({"error": "No file uploaded."}), 400

        added, errors = [], []
        for item in uploaded:
            try:
                source = library.add_source(
                    batch_id, item.read(), item.filename or "bird.png"
                )
                added.append(source.to_dict())
            except BirdLibraryError as exc:
                errors.append(f"{item.filename}: {exc}")

        if not added and errors:
            return jsonify({"error": " ".join(errors)}), 400
        return jsonify({"added": added, "errors": errors})

    @app.route("/api/birds/batches/<batch_id>/sources/<source_id>", methods=["PATCH"])
    def birds_update_source(batch_id: str, source_id: str):
        data = request.get_json(silent=True) or {}
        try:
            source = library.update_source(
                batch_id, source_id, str(data.get("species") or "")
            )
        except BirdLibraryError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"source": source.to_dict()})

    @app.route("/api/birds/batches/<batch_id>/sources/<source_id>", methods=["DELETE"])
    def birds_delete_source(batch_id: str, source_id: str):
        try:
            library.delete_source(batch_id, source_id)
        except BirdLibraryError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"ok": True})

    @app.route("/api/birds/batches/<batch_id>/sources/<source_id>/image")
    def birds_source_image(batch_id: str, source_id: str):
        try:
            batch, source = library.get_source(batch_id, source_id)
        except BirdLibraryError as exc:
            return jsonify({"error": str(exc)}), 404
        path = batch.source_path(source)
        if not path.is_file():
            return jsonify({"error": "Photo file is missing."}), 404
        return send_file(path, mimetype="image/png")

    # ---- prompt preview ----------------------------------------------------

    @app.route("/api/birds/prompt", methods=["POST"])
    def birds_preview_prompt():
        data = request.get_json(silent=True) or {}
        prompt = promptlib.build_prompt(
            str(data.get("species") or "Northern Cardinal"),
            str(data.get("style") or promptlib.DEFAULT_STYLE),
            str(data.get("notes") or ""),
        )
        return jsonify({"prompt": prompt})

    # ---- generation --------------------------------------------------------

    @app.route("/api/birds/generate", methods=["POST"])
    def birds_generate():
        data = request.get_json(silent=True) or {}
        batch_id = str(data.get("batch_id") or "").strip()
        if not batch_id:
            return jsonify({"error": "A batch is required."}), 400
        if not _api_key():
            return jsonify({"error": "No OpenAI API key configured."}), 400

        style = str(data.get("style") or promptlib.DEFAULT_STYLE)
        notes = str(data.get("notes") or "")
        size = str(data.get("size") or DEFAULT_SIZE)
        quality = str(data.get("quality") or DEFAULT_QUALITY)
        fidelity = str(data.get("fidelity") or DEFAULT_FIDELITY)
        trim = bool(data.get("trim", True))

        try:
            workers = int(data.get("workers") or DEFAULT_WORKERS)
        except (TypeError, ValueError):
            workers = DEFAULT_WORKERS

        source_ids = data.get("source_ids") or []
        if not isinstance(source_ids, list):
            source_ids = []
        source_ids = [str(s) for s in source_ids]

        if size not in VALID_SIZES:
            return jsonify({"error": f"size must be one of {sorted(VALID_SIZES)}."}), 400
        if quality not in VALID_QUALITIES:
            return jsonify({"error": f"quality must be one of {sorted(VALID_QUALITIES)}."}), 400

        job = BirdJob(id=uuid.uuid4().hex[:12])
        with _JOBS_LOCK:
            _JOBS[job.id] = job

        thread = threading.Thread(
            target=_run_job,
            args=(job, batch_id, source_ids, style, notes, size, quality,
                  fidelity, workers, trim),
            daemon=True,
        )
        thread.start()
        return jsonify({"job_id": job.id})

    @app.route("/api/birds/jobs/<job_id>")
    def birds_job_status(job_id: str):
        job = _get_job(job_id)
        if job is None:
            return jsonify({"error": "No such job."}), 404
        return jsonify(job.to_dict())

    @app.route("/api/birds/jobs/<job_id>/cancel", methods=["POST"])
    def birds_cancel_job(job_id: str):
        job = _get_job(job_id)
        if job is None:
            return jsonify({"error": "No such job."}), 404
        # In-flight birds finish; nothing new starts. Stopping a 250-bird run
        # that is going wrong should not mean losing the plates already paid
        # for.
        job.cancel_requested = True
        return jsonify({"ok": True})

    @app.route("/api/birds/jobs/<job_id>/image/<filename>")
    def birds_job_image(job_id: str, filename: str):
        job = _get_job(job_id)
        if job is None or job.run is None:
            return jsonify({"error": "No such job."}), 404

        # basename() so a crafted filename cannot escape the run directory.
        safe = Path(filename).name
        path = job.run.output_dir / safe
        if not path.is_file():
            return jsonify({"error": "No such image."}), 404
        return send_file(path, mimetype="image/png")

    @app.route("/api/birds/jobs/<job_id>/download/<filename>")
    def birds_job_download(job_id: str, filename: str):
        job = _get_job(job_id)
        if job is None or job.run is None:
            return jsonify({"error": "No such job."}), 404

        safe = Path(filename).name
        path = job.run.output_dir / safe
        if not path.is_file():
            return jsonify({"error": "No such image."}), 404
        return send_file(
            path, mimetype="image/png", as_attachment=True, download_name=safe
        )

    @app.route("/api/birds/jobs/<job_id>/zip")
    def birds_job_zip(job_id: str):
        """Every finished plate in one archive — the handoff to layout."""
        job = _get_job(job_id)
        if job is None or job.run is None:
            return jsonify({"error": "No such job."}), 404

        plates = [p for p in job.run.plates if p.status == "done" and p.filename]
        if not plates:
            return jsonify({"error": "No finished plates to download yet."}), 404

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for plate in plates:
                path = job.run.output_dir / plate.filename
                if path.is_file():
                    archive.write(path, arcname=plate.filename)

            # A manifest so the layout step can map plate -> species without
            # re-parsing filenames, and so flagged plates stay visible after
            # the job object is gone.
            lines = ["filename,species,transparent,warning"]
            for plate in plates:
                warning = plate.warning.replace(",", ";")
                lines.append(
                    f"{plate.filename},{plate.species},"
                    f"{'yes' if plate.transparent else 'no'},{warning}"
                )
            archive.writestr("manifest.csv", "\n".join(lines))

        buffer.seek(0)
        return send_file(
            buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{_slug(job.batch_name)}_plates.zip",
        )

    @app.route("/api/birds/jobs/<job_id>/docx", methods=["GET", "POST"])
    def birds_job_docx(job_id: str):
        """The finished plates as a DOCX book — one bird per page.

        Only plates that came back with real transparency are included by
        default: a flagged plate still has its original background, and
        dropping it into the book would print that background as a grey box.
        Pass ``include_flagged`` to override.
        """
        job = _get_job(job_id)
        if job is None or job.run is None:
            return jsonify({"error": "No such job."}), 404

        data = request.get_json(silent=True) or {}
        args = request.args
        title = str(data.get("title") or args.get("title") or job.batch_name or "Bird Guide")
        subtitle = str(data.get("subtitle") or args.get("subtitle") or "")
        author = str(data.get("author") or args.get("author") or "")
        include_flagged = bool(data.get("include_flagged") or args.get("include_flagged"))

        plates = [
            p for p in job.run.plates
            if p.status == "done" and p.filename and (include_flagged or p.transparent)
        ]
        if not plates:
            return jsonify(
                {"error": "No finished plates to put in a book yet."}
            ), 404

        out_path = job.run.output_dir / f"{_slug(title)}.docx"
        try:
            _, written = exportlib.build_docx(
                [(p.species, job.run.output_dir / p.filename) for p in plates],
                out_path,
                exportlib.GuideConfig(title=title, subtitle=subtitle, author=author),
            )
        except Exception as exc:
            return jsonify({"error": f"Could not build the document: {exc}"}), 500

        if not written:
            return jsonify({"error": "No plate images could be embedded."}), 500

        return send_file(
            out_path,
            mimetype=(
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            ),
            as_attachment=True,
            download_name=out_path.name,
        )
