#!/usr/bin/env python3
from __future__ import annotations

import shlex
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docx import Document
from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import openclaw_image_maker as image_maker
import openclaw_docx_writer as writer

ROOT_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT_DIR / "web_uploads"
OUTPUT_DIR = ROOT_DIR / "web_outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PYTHON_BIN = ROOT_DIR / ".venv" / "bin" / "python"
if not PYTHON_BIN.exists():
    PYTHON_BIN = Path(sys.executable)

DEFAULTS: dict[str, Any] = {
    "agent": "main",
    "tone": "friendly, encouraging, and easy to understand",
    "words": 250,
    "words_max": 320,
    "subwords": 250,
    "subwords_max": 320,
    "thinking": "",
    "timeout": 180,
    "images": True,
    "force": False,
    "image_prompt_variant": "rich-scene-no-text",
    "image_model": "gpt-image-1",
    "image_size": "1024x1536",
    "image_quality": "high",
    "image_width": 5.5,
}


@dataclass
class Job:
    id: str
    input_docx: str
    output_docx: str
    final_docx: str
    status: str = "queued"
    current_action: str = ""
    error: str = ""
    headings: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


app = Flask(__name__)
JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _timestamp() -> str:
    return time.strftime("%H:%M:%S")


def _get_job(job_id: str) -> Job:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        abort(404, description="Job not found")
    return job


def _append_log(job: Job, text: str) -> None:
    line = f"[{_timestamp()}] {text}"
    with job.lock:
        job.logs.append(line)
        job.updated_at = time.time()


def _set_status(job: Job, status: str, action: str = "", error: str = "") -> None:
    with job.lock:
        job.status = status
        job.current_action = action
        job.error = error
        job.updated_at = time.time()


def _list_image_headings(doc_path: Path) -> list[str]:
    doc = Document(str(doc_path))
    out: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(doc.paragraphs):
        selected = writer._select_heading_for_image(doc, i)
        if selected is None:
            i += 1
            continue
        heading_index, _para, heading = selected
        key = heading.strip().lower()
        if heading and key not in seen:
            seen.add(key)
            out.append(heading)
        i = heading_index + 1
    return out


def _pick_final_doc(output_doc: Path, logs: list[str]) -> Path:
    for line in reversed(logs):
        marker = "Final file:"
        if marker in line:
            candidate = Path(line.split(marker, 1)[1].strip())
            if candidate.exists():
                return candidate

    clear_candidate = output_doc.with_stem(output_doc.stem + "_formatted_clear")
    formatted_candidate = output_doc.with_stem(output_doc.stem + "_formatted")
    for candidate in (clear_candidate, formatted_candidate, output_doc):
        if candidate.exists():
            return candidate
    return output_doc


def _stream_command(job: Job, cmd: list[str]) -> None:
    pretty = " ".join(shlex.quote(part) for part in cmd)
    _append_log(job, f"$ {pretty}")

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.rstrip("\n")
        if line.strip():
            _append_log(job, line)
    proc.stdout.close()

    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"Command failed with exit code {rc}")


def _run_generation(job_id: str) -> None:
    job = _get_job(job_id)
    cfg = dict(job.config)

    _set_status(job, "running", action="generating_book", error="")

    input_doc = Path(job.input_docx)
    output_doc = input_doc

    # Keep the main write action minimal and predictable:
    # .venv/bin/python openclaw_docx_writer.py <input.docx> --agent main --images --image-model gpt-image-1
    cmd = [
        str(PYTHON_BIN),
        str(ROOT_DIR / "openclaw_docx_writer.py"),
        str(input_doc),
        "--agent",
        str(cfg["agent"]),
        "--images",
        "--image-model",
        str(cfg["image_model"]),
    ]

    if cfg.get("openai_api_key"):
        cmd.extend(["--openai-api-key", str(cfg["openai_api_key"])])

    try:
        _stream_command(job, cmd)

        with job.lock:
            final_doc = _pick_final_doc(output_doc, job.logs)
            job.final_docx = str(final_doc)

        headings = _list_image_headings(final_doc) if final_doc.exists() else []
        with job.lock:
            job.headings = headings

        _append_log(job, f"Ready. Final document: {final_doc}")
        _set_status(job, "success", action="", error="")
    except Exception as exc:
        _append_log(job, f"ERROR: {exc}")
        _set_status(job, "error", action="", error=str(exc))


def _run_replace_images(job_id: str, headings: list[str], overrides: dict[str, Any]) -> None:
    job = _get_job(job_id)

    with job.lock:
        final_doc = Path(job.final_docx or job.output_docx)
        base_cfg = dict(job.config)

    cfg = {**base_cfg, **overrides}

    _set_status(job, "running", action="replacing_images", error="")
    _append_log(job, f"Replacing images for {len(headings)} heading(s)...")

    try:
        for heading in headings:
            cmd = [
                str(PYTHON_BIN),
                str(ROOT_DIR / "openclaw_docx_writer.py"),
                str(final_doc),
                "--images",
                "--image-heading",
                heading,
                "--image-prompt-variant",
                str(cfg["image_prompt_variant"]),
                "--image-model",
                str(cfg["image_model"]),
                "--image-size",
                str(cfg["image_size"]),
                "--image-quality",
                str(cfg["image_quality"]),
                "--image-width",
                str(cfg["image_width"]),
            ]
            if cfg.get("openai_api_key"):
                cmd.extend(["--openai-api-key", str(cfg["openai_api_key"])])

            _append_log(job, f"Heading: {heading}")
            _stream_command(job, cmd)

        headings_after = _list_image_headings(final_doc) if final_doc.exists() else []
        with job.lock:
            job.headings = headings_after
            job.final_docx = str(final_doc)

        _append_log(job, "Image replacement completed.")
        _set_status(job, "success", action="", error="")
    except Exception as exc:
        _append_log(job, f"ERROR: {exc}")
        _set_status(job, "error", action="", error=str(exc))


def _bool_from_form(value: str | None) -> bool:
    if value is None:
        return False
    return value.lower() in {"1", "true", "yes", "on"}


def _normalize_output_name(name: str, input_path: Path, job_id: str) -> str:
    cleaned = secure_filename(name.strip()) if name.strip() else ""
    if not cleaned:
        cleaned = f"{input_path.stem}_generated_{job_id[:8]}.docx"
    if not cleaned.lower().endswith(".docx"):
        cleaned += ".docx"
    return cleaned


def _parse_int(value: str | None, fallback: int) -> int:
    try:
        return int(value) if value not in (None, "") else fallback
    except Exception:
        return fallback


def _parse_float(value: str | None, fallback: float) -> float:
    try:
        return float(value) if value not in (None, "") else fallback
    except Exception:
        return fallback


@app.get("/")
def index() -> str:
    prompt_variants = sorted(image_maker.PROMPT_VARIANTS.keys())
    return render_template(
        "index.html",
        defaults=DEFAULTS,
        prompt_variants=prompt_variants,
    )


@app.post("/api/jobs")
def create_job() -> Any:
    upload = request.files.get("layout_file")
    layout_path_raw = (request.form.get("layout_path") or "").strip()

    if upload and upload.filename:
        file_name = secure_filename(upload.filename)
        if not file_name.lower().endswith(".docx"):
            return jsonify({"error": "Uploaded file must be .docx"}), 400
        input_doc = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{file_name}"
        upload.save(str(input_doc))
    elif layout_path_raw:
        input_doc = Path(layout_path_raw).expanduser().resolve()
        if not input_doc.exists() or input_doc.suffix.lower() != ".docx":
            return jsonify({"error": "layout_path must point to an existing .docx file"}), 400
    else:
        return jsonify({"error": "Provide either layout_file or layout_path"}), 400

    job_id = uuid.uuid4().hex
    # Write in-place, same as the requested terminal command pattern.
    output_doc = input_doc

    cfg: dict[str, Any] = {
        "agent": (request.form.get("agent") or DEFAULTS["agent"]).strip() or DEFAULTS["agent"],
        "image_prompt_variant": (request.form.get("image_prompt_variant") or DEFAULTS["image_prompt_variant"]).strip() or DEFAULTS["image_prompt_variant"],
        "image_model": (request.form.get("image_model") or DEFAULTS["image_model"]).strip() or DEFAULTS["image_model"],
        "image_size": (request.form.get("image_size") or DEFAULTS["image_size"]).strip() or DEFAULTS["image_size"],
        "image_quality": (request.form.get("image_quality") or DEFAULTS["image_quality"]).strip() or DEFAULTS["image_quality"],
        "image_width": _parse_float(request.form.get("image_width"), float(DEFAULTS["image_width"])),
        "openai_api_key": (request.form.get("openai_api_key") or "").strip(),
    }

    if cfg["image_prompt_variant"] not in image_maker.PROMPT_VARIANTS:
        return jsonify({"error": "Invalid image prompt variant"}), 400

    job = Job(
        id=job_id,
        input_docx=str(input_doc),
        output_docx=str(output_doc),
        final_docx=str(output_doc),
        status="queued",
        config=cfg,
    )
    _append_log(job, "Job created.")

    with JOBS_LOCK:
        JOBS[job_id] = job

    t = threading.Thread(target=_run_generation, args=(job_id,), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.get("/api/jobs/<job_id>/status")
def job_status(job_id: str) -> Any:
    job = _get_job(job_id)
    with job.lock:
        return jsonify(
            {
                "id": job.id,
                "status": job.status,
                "current_action": job.current_action,
                "error": job.error,
                "input_docx": job.input_docx,
                "output_docx": job.output_docx,
                "final_docx": job.final_docx,
                "headings": list(job.headings),
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            }
        )


@app.get("/api/jobs/<job_id>/logs")
def job_logs(job_id: str) -> Any:
    job = _get_job(job_id)
    start = _parse_int(request.args.get("from"), 0)
    with job.lock:
        start = max(0, min(start, len(job.logs)))
        lines = job.logs[start:]
        next_cursor = len(job.logs)
    return jsonify({"logs": lines, "next": next_cursor})


@app.post("/api/jobs/<job_id>/replace-images")
def replace_images(job_id: str) -> Any:
    job = _get_job(job_id)
    payload = request.get_json(silent=True) or {}
    headings = payload.get("headings") or []
    if not isinstance(headings, list):
        return jsonify({"error": "headings must be an array"}), 400

    normalized = [str(h).strip() for h in headings if str(h).strip()]
    if not normalized:
        return jsonify({"error": "Select at least one heading"}), 400

    with job.lock:
        if job.status == "running":
            return jsonify({"error": "Job is busy. Wait for current task to finish."}), 409
        final_doc = Path(job.final_docx)

    if not final_doc.exists():
        return jsonify({"error": "Final document not found for this job"}), 400

    overrides = {
        "image_prompt_variant": str(payload.get("image_prompt_variant") or "").strip() or job.config.get("image_prompt_variant", DEFAULTS["image_prompt_variant"]),
        "image_model": str(payload.get("image_model") or "").strip() or job.config.get("image_model", DEFAULTS["image_model"]),
        "image_size": str(payload.get("image_size") or "").strip() or job.config.get("image_size", DEFAULTS["image_size"]),
        "image_quality": str(payload.get("image_quality") or "").strip() or job.config.get("image_quality", DEFAULTS["image_quality"]),
        "image_width": _parse_float(str(payload.get("image_width") or ""), float(job.config.get("image_width", DEFAULTS["image_width"]))),
        "openai_api_key": str(payload.get("openai_api_key") or "").strip() or job.config.get("openai_api_key", ""),
    }

    if overrides["image_prompt_variant"] not in image_maker.PROMPT_VARIANTS:
        return jsonify({"error": "Invalid image prompt variant"}), 400

    t = threading.Thread(target=_run_replace_images, args=(job_id, normalized, overrides), daemon=True)
    t.start()
    return jsonify({"ok": True, "queued": len(normalized)})


@app.get("/api/jobs/<job_id>/download/<kind>")
def download_file(job_id: str, kind: str) -> Any:
    job = _get_job(job_id)
    with job.lock:
        mapping = {
            "input": Path(job.input_docx),
            "output": Path(job.output_docx),
            "final": Path(job.final_docx),
        }
    target = mapping.get(kind)
    if target is None:
        abort(404)
    if not target.exists():
        abort(404, description="File does not exist yet")
    return send_file(target, as_attachment=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
