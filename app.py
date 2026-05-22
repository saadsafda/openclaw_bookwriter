#!/usr/bin/env python3
from __future__ import annotations

import json
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

import io
import base64
import qrcode
from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers.pil import (
    SquareModuleDrawer,
    RoundedModuleDrawer,
    CircleModuleDrawer,
    GappedSquareModuleDrawer,
    VerticalBarsDrawer,
    HorizontalBarsDrawer,
)
from qrcode.image.styles.colormasks import SolidFillColorMask

import openclaw_image_maker as image_maker
import openclaw_docx_writer as writer
import pub_listing_agent
import wp_landing_page
import db as bookdb

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
    kindle_docx: str = ""
    paperback_docx: str = ""
    status: str = "queued"
    current_action: str = ""
    error: str = ""
    headings: list[str] = field(default_factory=list)
    listing: dict[str, Any] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    pre_written: bool = False
    custom_title: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


app = Flask(__name__)
JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

bookdb.init_db()


def _timestamp() -> str:
    return time.strftime("%H:%M:%S")


def _rehydrate_job_from_db(job_id: str) -> Job | None:
    """Rebuild a Job dataclass from a persisted book record.

    Used when the user interacts with a historical book whose in-memory Job
    was lost (e.g. server restart, or job created before this session).
    """
    book = bookdb.get_book(job_id)
    if book is None:
        return None
    final_docx = book.get("final_docx") or ""
    output_docx = final_docx or book.get("input_docx") or ""
    derived_title = _derive_title(book.get("input_docx") or job_id)
    stored_title = (book.get("title") or "").strip()
    custom_title = stored_title if stored_title and stored_title != derived_title else ""
    return Job(
        id=job_id,
        input_docx=book.get("input_docx") or "",
        output_docx=output_docx,
        final_docx=final_docx,
        kindle_docx=book.get("kindle_docx") or "",
        paperback_docx=book.get("paperback_docx") or "",
        status=book.get("status") or "success",
        headings=list(book.get("headings") or []),
        listing=dict(book.get("listing") or {}),
        logs=list(book.get("logs") or []),
        config=dict(book.get("config") or {}),
        pre_written=bool(book.get("pre_written") or 0),
        custom_title=custom_title,
        created_at=float(book.get("created_at") or time.time()),
        updated_at=float(book.get("updated_at") or time.time()),
    )


def _get_job(job_id: str) -> Job:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            rehydrated = _rehydrate_job_from_db(job_id)
            if rehydrated is not None:
                JOBS[job_id] = rehydrated
                job = rehydrated
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


def _derive_title(input_path: str) -> str:
    """Extract a human-readable title from the input filename."""
    name = Path(input_path).stem
    # Strip uuid prefix added by upload (8hex_filename)
    import re
    name = re.sub(r'^[0-9a-f]{8}_', '', name)
    return name.replace('_', ' ').replace('-', ' ').strip() or 'Untitled Book'


def _sync_job_to_db(job: Job) -> None:
    """Persist current job state to SQLite."""
    with job.lock:
        title = job.custom_title.strip() or _derive_title(job.input_docx)
        bookdb.save_book(
            book_id=job.id,
            title=title,
            status=job.status,
            agent=job.config.get('agent', 'main'),
            model=job.config.get('image_model', ''),
            input_docx=job.input_docx,
            final_docx=job.final_docx,
            kindle_docx=job.kindle_docx,
            paperback_docx=job.paperback_docx,
            headings=list(job.headings),
            listing=dict(job.listing) if job.listing else {},
            config=dict(job.config),
            logs=list(job.logs),
            error=job.error,
            pre_written=bool(job.pre_written),
        )


def _detect_pre_written(doc_path: Path, min_body_paragraphs: int = 3) -> bool:
    """Return True if the uploaded .docx already contains substantial body prose.

    Uses writer.paragraph_looks_like_body to count paragraphs that look like
    real written prose (not headings, not bare outline topics). If the file
    has at least `min_body_paragraphs` such paragraphs, it's considered
    "already written" rather than an outline-only skeleton.
    """
    try:
        doc = Document(str(doc_path))
    except Exception:
        return False
    body_count = 0
    for p in doc.paragraphs:
        try:
            if writer.paragraph_looks_like_body(p):
                body_count += 1
                if body_count >= min_body_paragraphs:
                    return True
        except Exception:
            continue
    return False


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


def _run_kdp_formatting(job: Job, source_doc: Path) -> tuple[Path, Path]:
    kindle_doc = source_doc.with_stem(source_doc.stem + "_kindle")
    paperback_doc = source_doc.with_stem(source_doc.stem + "_paperback")
    cfg = dict(job.config)

    cmd = [
        str(PYTHON_BIN),
        str(ROOT_DIR / "kdp_docx_formatter.py"),
        str(source_doc),
        "--kindle-output",
        str(kindle_doc),
        "--paperback-output",
        str(paperback_doc),
        "--title-placeholder",
        str(cfg.get("title_placeholder", "Book Title Placeholder")),
        "--author-placeholder",
        str(cfg.get("author_placeholder", "Author Name")),
    ]

    estimated_pages = int(cfg.get("estimated_pages", 0) or 0)
    if estimated_pages > 0:
        cmd.extend(["--estimated-pages", str(estimated_pages)])

    _append_log(job, "Formatting KDP deliverables (Kindle + Paperback)...")
    _stream_command(job, cmd)

    if not kindle_doc.exists() or not paperback_doc.exists():
        raise RuntimeError("KDP formatter completed but output files were not found.")

    return kindle_doc, paperback_doc


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
        kindle_doc, paperback_doc = _run_kdp_formatting(job, final_doc)
        with job.lock:
            job.headings = headings
            job.kindle_docx = str(kindle_doc)
            job.paperback_docx = str(paperback_doc)

        _append_log(job, f"Ready. Final document: {final_doc}")
        _append_log(job, f"Kindle output: {kindle_doc}")
        _append_log(job, f"Paperback output: {paperback_doc}")

        # --- Auto-create landing page + QR code ---
        with job.lock:
            page_title = job.custom_title.strip() or _derive_title(job.input_docx)
        try:
            _set_status(job, "running", action="creating_landing_page", error="")
            _do_landing_page_qr(job, page_title)
        except Exception as qr_exc:
            # Landing page failure is non-fatal — book is still usable.
            _append_log(job, f"WARNING: Landing page + QR failed (book is still ready): {qr_exc}")

        _set_status(job, "success", action="", error="")
        _sync_job_to_db(job)
    except Exception as exc:
        _append_log(job, f"ERROR: {exc}")
        _set_status(job, "error", action="", error=str(exc))
        _sync_job_to_db(job)


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
            if cfg.get("image_guidance"):
                cmd.extend(["--image-guidance", str(cfg["image_guidance"])])

            _append_log(job, f"Heading: {heading}")
            _stream_command(job, cmd)

        headings_after = _list_image_headings(final_doc) if final_doc.exists() else []
        kindle_doc, paperback_doc = _run_kdp_formatting(job, final_doc)
        with job.lock:
            job.headings = headings_after
            job.final_docx = str(final_doc)
            job.kindle_docx = str(kindle_doc)
            job.paperback_docx = str(paperback_doc)

        _append_log(job, "Image replacement completed.")
        _append_log(job, f"Kindle output refreshed: {kindle_doc}")
        _append_log(job, f"Paperback output refreshed: {paperback_doc}")
        _set_status(job, "success", action="", error="")
        _sync_job_to_db(job)
    except Exception as exc:
        _append_log(job, f"ERROR: {exc}")
        _set_status(job, "error", action="", error=str(exc))
        _sync_job_to_db(job)


def _run_rewrite_paragraphs(job_id: str, headings: list[str], overrides: dict[str, Any]) -> None:
    job = _get_job(job_id)

    with job.lock:
        final_doc = Path(job.final_docx or job.output_docx)
        base_cfg = dict(job.config)

    cfg = {**base_cfg, **overrides}

    _set_status(job, "running", action="rewriting_paragraphs", error="")
    _append_log(job, f"Rewriting paragraphs for {len(headings)} heading(s)...")

    try:
        for heading in headings:
            cmd = [
                str(PYTHON_BIN),
                str(ROOT_DIR / "openclaw_docx_writer.py"),
                str(final_doc),
                "--agent",
                str(cfg["agent"]),
                "--rewrite-heading",
                heading,
                "--tone",
                str(cfg.get("tone", DEFAULTS["tone"])),
                "--words",
                str(cfg.get("words", DEFAULTS["words"])),
                "--words-max",
                str(cfg.get("words_max", DEFAULTS["words_max"])),
                "--subwords",
                str(cfg.get("subwords", DEFAULTS["subwords"])),
                "--subwords-max",
                str(cfg.get("subwords_max", DEFAULTS["subwords_max"])),
            ]
            if cfg.get("rewrite_guidance"):
                cmd.extend(["--rewrite-guidance", str(cfg["rewrite_guidance"])])

            _append_log(job, f"Heading: {heading}")
            _stream_command(job, cmd)

        kindle_doc, paperback_doc = _run_kdp_formatting(job, final_doc)
        with job.lock:
            job.final_docx = str(final_doc)
            job.kindle_docx = str(kindle_doc)
            job.paperback_docx = str(paperback_doc)

        _append_log(job, "Paragraph rewrite completed.")
        _append_log(job, f"Kindle output refreshed: {kindle_doc}")
        _append_log(job, f"Paperback output refreshed: {paperback_doc}")
        _set_status(job, "success", action="", error="")
        _sync_job_to_db(job)
    except Exception as exc:
        _append_log(job, f"ERROR: {exc}")
        _set_status(job, "error", action="", error=str(exc))
        _sync_job_to_db(job)


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


@app.get("/qr-code")
def qr_code_page() -> str:
    return render_template("qr_code.html")


_QR_EC_LEVELS = {
    "L": ERROR_CORRECT_L,
    "M": ERROR_CORRECT_M,
    "Q": ERROR_CORRECT_Q,
    "H": ERROR_CORRECT_H,
}

_QR_DRAWERS = {
    "square": SquareModuleDrawer,
    "rounded": RoundedModuleDrawer,
    "circle": CircleModuleDrawer,
    "gapped": GappedSquareModuleDrawer,
    "vertical": VerticalBarsDrawer,
    "horizontal": HorizontalBarsDrawer,
}


def _hex_to_rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    if not value:
        return fallback
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return fallback
    try:
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except ValueError:
        return fallback


@app.post("/api/qr-code")
def generate_qr_code() -> Any:
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400
    if len(content) > 2000:
        return jsonify({"error": "content too long (max 2000 chars)"}), 400

    ec_key = (data.get("error_correction") or "M").upper()
    ec_level = _QR_EC_LEVELS.get(ec_key, ERROR_CORRECT_M)

    try:
        box_size = max(4, min(40, int(data.get("box_size") or 12)))
    except (TypeError, ValueError):
        box_size = 12
    try:
        border = max(0, min(16, int(data.get("border") or 4)))
    except (TypeError, ValueError):
        border = 4

    fg = _hex_to_rgb(data.get("fg_color") or "#111827", (17, 24, 39))
    bg = _hex_to_rgb(data.get("bg_color") or "#ffffff", (255, 255, 255))

    style_key = (data.get("style") or "square").lower()
    drawer_cls = _QR_DRAWERS.get(style_key, SquareModuleDrawer)

    qr = qrcode.QRCode(
        version=None,
        error_correction=ec_level,
        box_size=box_size,
        border=border,
    )
    qr.add_data(content)
    qr.make(fit=True)

    img = qr.make_image(
        image_factory=StyledPilImage,
        module_drawer=drawer_cls(),
        color_mask=SolidFillColorMask(back_color=bg, front_color=fg),
    )

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    png_bytes = buffer.getvalue()
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return jsonify({
        "data_url": f"data:image/png;base64,{encoded}",
        "size": len(png_bytes),
        "modules": qr.modules_count,
    })


def _qr_record_to_data_url(rec: dict[str, Any]) -> str:
    png_blob = rec.get("png_blob") or b""
    return f"data:image/png;base64,{base64.b64encode(png_blob).decode('ascii')}"


def _qr_record_public(rec: dict[str, Any], include_image: bool = True) -> dict[str, Any]:
    out = {
        "id": rec["id"],
        "label": rec.get("label") or "",
        "content": rec.get("content") or "",
        "style": rec.get("style") or "square",
        "fg_color": rec.get("fg_color") or "#111827",
        "bg_color": rec.get("bg_color") or "#ffffff",
        "error_correction": rec.get("error_correction") or "M",
        "box_size": rec.get("box_size") or 12,
        "border": rec.get("border") or 4,
        "created_at": rec.get("created_at") or 0,
    }
    if include_image and rec.get("png_blob") is not None:
        out["data_url"] = _qr_record_to_data_url(rec)
    return out


@app.post("/api/qr-codes")
def save_qr_endpoint() -> Any:
    data = request.get_json(silent=True) or {}
    data_url = (data.get("data_url") or "").strip()
    if not data_url.startswith("data:image/png;base64,"):
        return jsonify({"error": "Missing or invalid data_url"}), 400
    try:
        png_bytes = base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        return jsonify({"error": "Invalid base64 image"}), 400
    if not png_bytes:
        return jsonify({"error": "Empty image"}), 400

    label = (data.get("label") or "").strip()[:80]
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400
    if len(content) > 2000:
        return jsonify({"error": "content too long"}), 400
    if not label:
        label = content[:40] + ("…" if len(content) > 40 else "")

    qr_id = uuid.uuid4().hex
    bookdb.save_qr_code(
        qr_id,
        label=label,
        content=content,
        style=(data.get("style") or "square")[:32],
        fg_color=(data.get("fg_color") or "#111827")[:9],
        bg_color=(data.get("bg_color") or "#ffffff")[:9],
        error_correction=(data.get("error_correction") or "M")[:2],
        box_size=int(data.get("box_size") or 12),
        border=int(data.get("border") or 4),
        png_blob=png_bytes,
    )
    rec = bookdb.get_qr_code(qr_id)
    return jsonify(_qr_record_public(rec))


@app.get("/api/qr-codes")
def list_qr_endpoint() -> Any:
    include_image = request.args.get("include_image", "1") != "0"
    items: list[dict[str, Any]] = []
    for meta in bookdb.list_qr_codes():
        if include_image:
            rec = bookdb.get_qr_code(meta["id"])
            if rec is None:
                continue
            items.append(_qr_record_public(rec, include_image=True))
        else:
            items.append(_qr_record_public(meta, include_image=False))
    return jsonify({"items": items})


@app.delete("/api/qr-codes/<qr_id>")
def delete_qr_endpoint(qr_id: str) -> Any:
    deleted = bookdb.delete_qr_code(qr_id)
    if not deleted:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/attach-qr")
def attach_qr_to_book(job_id: str) -> Any:
    job = _get_job(job_id)
    data = request.get_json(silent=True) or {}
    qr_id = (data.get("qr_id") or "").strip()
    heading = (data.get("heading") or "Scan this QR code").strip() or "Scan this QR code"
    caption = (data.get("caption") or "").strip()

    if not qr_id:
        return jsonify({"error": "qr_id is required"}), 400

    rec = bookdb.get_qr_code(qr_id)
    if rec is None:
        return jsonify({"error": "QR code not found"}), 404

    with job.lock:
        final_path = Path(job.final_docx or job.output_docx)
        status = job.status

    if status != "success":
        return jsonify({"error": "Book is not ready yet"}), 400
    if not final_path.exists():
        return jsonify({"error": "Final docx not found"}), 404

    # Write QR PNG to disk next to the document so it can be re-opened later.
    qr_assets_dir = OUTPUT_DIR / "qr_assets"
    qr_assets_dir.mkdir(parents=True, exist_ok=True)
    qr_png_path = qr_assets_dir / f"qr_{job_id}_{qr_id}.png"
    qr_png_path.write_bytes(rec["png_blob"])

    try:
        _append_qr_page(final_path, qr_png_path, heading=heading, caption=caption or rec.get("content", ""))
    except Exception as exc:
        return jsonify({"error": f"Failed to attach QR: {exc}"}), 500

    _append_log(job, f"Attached QR code to last page (label={rec.get('label')!r}).")

    # Regenerate Kindle / Paperback variants so the QR page is also in them.
    try:
        kindle_doc, paperback_doc = _run_kdp_formatting(job, final_path)
        with job.lock:
            job.kindle_docx = str(kindle_doc)
            job.paperback_docx = str(paperback_doc)
        _append_log(job, "Re-formatted Kindle + Paperback with QR page.")
    except Exception as exc:
        _append_log(job, f"WARNING: QR added to final doc but KDP re-format failed: {exc}")

    _sync_job_to_db(job)

    with job.lock:
        return jsonify({
            "ok": True,
            "final_docx": job.final_docx,
            "kindle_docx": job.kindle_docx,
            "paperback_docx": job.paperback_docx,
        })


def _append_qr_page(doc_path: Path, qr_png: Path, *, heading: str, caption: str) -> None:
    """Append a centred 'Scan this QR code' page to the given .docx in-place."""
    from docx.shared import Inches, Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.text import WD_BREAK

    doc = Document(str(doc_path))

    # Page break so the QR section starts on its own page.
    page_break_para = doc.add_paragraph()
    page_break_para.add_run().add_break(WD_BREAK.PAGE)

    # Heading: "Scan this QR code"
    h_para = doc.add_paragraph()
    h_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    h_run = h_para.add_run(heading)
    h_run.bold = True
    h_run.font.size = Pt(22)

    # Spacer
    doc.add_paragraph()

    # QR image, centred
    img_para = doc.add_paragraph()
    img_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    img_para.add_run().add_picture(str(qr_png), width=Inches(3.0))

    # Caption (URL / content) — small and muted
    if caption:
        c_para = doc.add_paragraph()
        c_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        c_run = c_para.add_run(caption)
        c_run.font.size = Pt(10)

    doc.save(str(doc_path))


def _insert_paperback_bonus_page(doc_path: Path, qr_png: Path) -> None:
    """Insert a FREE BONUS page with QR code into the paperback .docx.

    Layout:
      - Page break
      - "FREE BONUS" centred, 48 pt, bold
      - "GET OUR NEXT BOOK FOR FREE" centred, 24 pt
      - Spacer
      - QR image centred
    """
    from docx.shared import Inches, Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK

    doc = Document(str(doc_path))

    # Page break
    pb = doc.add_paragraph()
    pb.add_run().add_break(WD_BREAK.PAGE)

    # "FREE BONUS" – 48pt
    h1 = doc.add_paragraph()
    h1.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run1 = h1.add_run("FREE BONUS")
    run1.bold = True
    run1.font.size = Pt(48)

    # Spacer
    doc.add_paragraph()

    # "GET OUR NEXT BOOK FOR FREE" – 24pt
    h2 = doc.add_paragraph()
    h2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run2 = h2.add_run("GET OUR NEXT BOOK FOR FREE")
    run2.bold = True
    run2.font.size = Pt(24)

    # Spacer
    doc.add_paragraph()

    # QR image centred
    img_para = doc.add_paragraph()
    img_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    img_para.add_run().add_picture(str(qr_png), width=Inches(3.0))

    doc.save(str(doc_path))


def _insert_kindle_bonus_page(doc_path: Path, landing_url: str) -> None:
    """Insert a FREE BONUS page with a clickable link into the Kindle .docx.

    Layout:
      - Page break
      - "FREE BONUS" centred, 48 pt, bold
      - "GET OUR NEXT BOOK FOR FREE" centred, 24 pt
      - Spacer
      - Clickable hyperlink to landing page, 24 pt, centred
    """
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = Document(str(doc_path))

    # Page break
    pb = doc.add_paragraph()
    pb.add_run().add_break(WD_BREAK.PAGE)

    # "FREE BONUS" – 48pt
    h1 = doc.add_paragraph()
    h1.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run1 = h1.add_run("FREE BONUS")
    run1.bold = True
    run1.font.size = Pt(48)

    # Spacer
    doc.add_paragraph()

    # "GET OUR NEXT BOOK FOR FREE" – 24pt
    h2 = doc.add_paragraph()
    h2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run2 = h2.add_run("GET OUR NEXT BOOK FOR FREE")
    run2.bold = True
    run2.font.size = Pt(24)

    # Spacer
    doc.add_paragraph()

    # Clickable hyperlink – 24pt, centred
    link_para = doc.add_paragraph()
    link_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Build a w:hyperlink element with the URL as an external relationship.
    part = doc.part
    r_id = part.relate_to(
        landing_url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")

    # Blue underlined style for hyperlink
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    rPr.append(color)

    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rPr.append(underline)

    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), "48")  # half-points: 24pt = 48
    rPr.append(sz)

    szCs = OxmlElement("w:szCs")
    szCs.set(qn("w:val"), "48")
    rPr.append(szCs)

    new_run.append(rPr)
    text_el = OxmlElement("w:t")
    text_el.text = landing_url
    new_run.append(text_el)
    hyperlink.append(new_run)

    link_para._p.append(hyperlink)

    doc.save(str(doc_path))


def _generate_qr_png(content: str, output_path: Path) -> None:
    """Generate a simple black-on-white QR code PNG at ``output_path``."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_H,
        box_size=12,
        border=4,
    )
    qr.add_data(content)
    qr.make(fit=True)

    img = qr.make_image(
        image_factory=StyledPilImage,
        module_drawer=SquareModuleDrawer(),
        color_mask=SolidFillColorMask(
            back_color=(255, 255, 255),
            front_color=(17, 24, 39),
        ),
    )
    img.save(str(output_path), format="PNG")


def _do_landing_page_qr(job: Job, page_title: str) -> None:
    """Core logic: create WP landing page, QR code, update both docx files.

    This is designed to be called inline (e.g. at the end of _run_generation)
    or from a standalone background task.  It logs progress but does NOT set
    job status or sync to DB — callers handle that.
    """
    _append_log(job, f"Starting landing page + QR workflow for '{page_title}'...")

    # 1. Create the WordPress landing page
    _append_log(job, "Creating WordPress landing page...")
    page_info = wp_landing_page.create_landing_page(
        title=page_title,
        callback=lambda step, msg: _append_log(job, msg),
    )
    landing_url = page_info["url"]
    _append_log(job, f"Landing page published: {landing_url}")

    # 2. Generate QR code for the landing page URL
    _append_log(job, "Generating QR code for landing page URL...")
    qr_assets_dir = OUTPUT_DIR / "qr_assets"
    qr_assets_dir.mkdir(parents=True, exist_ok=True)
    qr_png_path = qr_assets_dir / f"qr_landing_{job.id}.png"
    _generate_qr_png(landing_url, qr_png_path)
    _append_log(job, f"QR code saved: {qr_png_path.name}")

    # 3. Insert FREE BONUS page into paperback docx
    with job.lock:
        paperback_path = Path(job.paperback_docx) if job.paperback_docx else None

    if paperback_path and paperback_path.exists():
        _append_log(job, "Inserting QR bonus page into paperback docx...")
        _insert_paperback_bonus_page(paperback_path, qr_png_path)
        _append_log(job, "Paperback FREE BONUS page added with QR code.")
    else:
        _append_log(job, "WARNING: No paperback docx found — skipping QR insertion.")

    # 4. Insert link bonus page into kindle docx
    with job.lock:
        kindle_path = Path(job.kindle_docx) if job.kindle_docx else None

    if kindle_path and kindle_path.exists():
        _append_log(job, "Inserting link bonus page into Kindle docx...")
        _insert_kindle_bonus_page(kindle_path, landing_url)
        _append_log(job, "Kindle FREE BONUS page added with landing page link.")
    else:
        _append_log(job, "WARNING: No Kindle docx found — skipping link insertion.")

    _append_log(job, "Landing page + QR workflow complete!")
    _append_log(job, f"Landing page URL: {landing_url}")


def _run_landing_page_qr(job_id: str, page_title: str) -> None:
    """Background task wrapper for _do_landing_page_qr (manual QR button)."""
    job = _get_job(job_id)

    _set_status(job, "running", action="creating_landing_page", error="")

    try:
        _do_landing_page_qr(job, page_title)
        _set_status(job, "success", action="", error="")
        _sync_job_to_db(job)

    except Exception as exc:
        _append_log(job, f"ERROR: {exc}")
        _set_status(job, "error", action="", error=str(exc))
        _sync_job_to_db(job)


@app.post("/api/jobs/<job_id>/create-landing-qr")
def create_landing_qr(job_id: str) -> Any:
    """Create a WordPress landing page, generate a QR code for it, and insert
    the QR code into the paperback docx and a link into the Kindle docx."""
    job = _get_job(job_id)
    payload = request.get_json(silent=True) or {}
    page_title = (payload.get("page_title") or "").strip()

    if not page_title:
        return jsonify({"error": "page_title is required"}), 400
    if len(page_title) > 200:
        return jsonify({"error": "page_title too long (max 200 chars)"}), 400

    with job.lock:
        if job.status == "running":
            return jsonify({"error": "Job is busy. Wait for current task to finish."}), 409

    t = threading.Thread(
        target=_run_landing_page_qr,
        args=(job_id, page_title),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "page_title": page_title})


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
        "title_placeholder": (request.form.get("title_placeholder") or "Book Title Placeholder").strip() or "Book Title Placeholder",
        "author_placeholder": (request.form.get("author_placeholder") or "Author Name").strip() or "Author Name",
        "estimated_pages": _parse_int(request.form.get("estimated_pages"), 0),
    }

    if cfg["image_prompt_variant"] not in image_maker.PROMPT_VARIANTS:
        return jsonify({"error": "Invalid image prompt variant"}), 400

    pre_written = _detect_pre_written(input_doc)

    job = Job(
        id=job_id,
        input_docx=str(input_doc),
        output_docx=str(output_doc),
        final_docx=str(output_doc),
        kindle_docx="",
        paperback_docx="",
        status="queued",
        config=cfg,
        pre_written=pre_written,
    )
    _append_log(job, "Job created.")
    if pre_written:
        _append_log(job, "Input file detected as already-written (contains full prose).")

    with JOBS_LOCK:
        JOBS[job_id] = job

    _sync_job_to_db(job)

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
                "kindle_docx": job.kindle_docx,
                "paperback_docx": job.paperback_docx,
                "headings": list(job.headings),
                "listing": dict(job.listing) if job.listing else {},
                "pre_written": bool(job.pre_written),
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
        "image_guidance": str(payload.get("image_guidance") or "").strip(),
    }

    if overrides["image_prompt_variant"] not in image_maker.PROMPT_VARIANTS:
        return jsonify({"error": "Invalid image prompt variant"}), 400

    t = threading.Thread(target=_run_replace_images, args=(job_id, normalized, overrides), daemon=True)
    t.start()
    return jsonify({"ok": True, "queued": len(normalized)})


@app.post("/api/jobs/<job_id>/rewrite-paragraphs")
def rewrite_paragraphs(job_id: str) -> Any:
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
        "agent": str(payload.get("agent") or "").strip() or job.config.get("agent", DEFAULTS["agent"]),
        "tone": str(payload.get("tone") or "").strip() or job.config.get("tone", DEFAULTS["tone"]),
        "words": int(payload.get("words") or job.config.get("words", DEFAULTS["words"])),
        "words_max": int(payload.get("words_max") or job.config.get("words_max", DEFAULTS["words_max"])),
        "subwords": int(payload.get("subwords") or job.config.get("subwords", DEFAULTS["subwords"])),
        "subwords_max": int(payload.get("subwords_max") or job.config.get("subwords_max", DEFAULTS["subwords_max"])),
        "rewrite_guidance": str(payload.get("rewrite_guidance") or "").strip(),
    }

    t = threading.Thread(target=_run_rewrite_paragraphs, args=(job_id, normalized, overrides), daemon=True)
    t.start()
    return jsonify({"ok": True, "queued": len(normalized)})


def _run_generate_listing(job_id: str, title_override: str, extra_context: str = "") -> None:
    job = _get_job(job_id)

    with job.lock:
        final_doc = Path(job.final_docx or job.output_docx)
        base_cfg = dict(job.config)

    _set_status(job, "running", action="generating_listing", error="")
    _append_log(job, "Starting publishing listing generation...")
    if extra_context.strip():
        _append_log(job, f"Using author guidance: {extra_context.strip()[:200]}")

    try:
        title = title_override or base_cfg.get("title_placeholder", "")

        def _progress(step: str, msg: str) -> None:
            _append_log(job, f"[{step}] {msg}")

        result = pub_listing_agent.generate_listing(
            docx_path=final_doc,
            title=title,
            agent_id="pub-listing-agent-1",
            timeout_s=base_cfg.get("timeout", 180),
            callback=_progress,
            extra_context=extra_context,
        )

        listing_data = {
            "title": result.title,
            "subtitles": result.subtitles,
            "description": result.description,
            "ebook_categories": result.ebook_categories,
            "paperback_categories": result.paperback_categories,
        }

        with job.lock:
            job.listing = listing_data

        _append_log(job, f"Subtitles: {len(result.subtitles)} ideas generated")
        _append_log(job, f"Description: {len(result.description.split())} words")
        _append_log(job, f"Ebook categories: {len(result.ebook_categories)}")
        _append_log(job, f"Paperback categories: {len(result.paperback_categories)}")
        _append_log(job, "Publishing listing generation complete.")
        _set_status(job, "success", action="", error="")
        _sync_job_to_db(job)
    except Exception as exc:
        _append_log(job, f"ERROR: {exc}")
        _set_status(job, "error", action="", error=str(exc))
        _sync_job_to_db(job)


@app.post("/api/jobs/<job_id>/generate-listing")
def generate_listing(job_id: str) -> Any:
    job = _get_job(job_id)
    payload = request.get_json(silent=True) or {}
    title_override = str(payload.get("title", "")).strip()
    extra_context = str(payload.get("extra_context", "")).strip()

    with job.lock:
        if job.status == "running":
            return jsonify({"error": "Job is busy. Wait for current task to finish."}), 409
        final_doc = Path(job.final_docx)

    if not final_doc.exists():
        return jsonify({"error": "Final document not found. Generate the book first."}), 400

    t = threading.Thread(
        target=_run_generate_listing,
        args=(job_id, title_override, extra_context),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True})


def _run_redo_listing_part(job_id: str, part: str, extra_context: str) -> None:
    """Regenerate just one part of the listing (description, subtitles, or categories)."""
    job = _get_job(job_id)

    with job.lock:
        final_doc = Path(job.final_docx or job.output_docx)
        base_cfg = dict(job.config)
        existing = dict(job.listing or {})

    _set_status(job, "running", action=f"redo_listing_{part}", error="")
    _append_log(job, f"Redoing {part} with new guidance...")
    if extra_context.strip():
        _append_log(job, f"Author guidance: {extra_context.strip()[:200]}")

    try:
        # Pull fresh outline/intro from the finished doc
        doc_title, outline, intro = pub_listing_agent._extract_outline_and_intro(final_doc)
        title = existing.get("title") or doc_title or base_cfg.get("title_placeholder", "") or "Untitled Book"
        timeout_s = base_cfg.get("timeout", 180)
        agent_id = "pub-listing-agent-1"

        updated = dict(existing)
        updated["title"] = title

        if part == "description":
            description, _raw = pub_listing_agent.generate_description(
                title, outline, intro, agent_id, timeout_s, extra_context=extra_context
            )
            updated["description"] = description
            _append_log(job, f"New description: {len(description.split())} words")
        elif part == "subtitles":
            subtitles, _raw = pub_listing_agent.generate_subtitles(
                title, outline, intro, agent_id, timeout_s, extra_context=extra_context
            )
            updated["subtitles"] = subtitles
            _append_log(job, f"New subtitles: {len(subtitles)} ideas")
        elif part == "categories":
            ebook_cats, pb_cats, _raw = pub_listing_agent.select_categories(
                title, outline, intro, agent_id, timeout_s, extra_context=extra_context
            )
            updated["ebook_categories"] = ebook_cats
            updated["paperback_categories"] = pb_cats
            _append_log(job, f"New categories: {len(ebook_cats)} ebook, {len(pb_cats)} paperback")
        else:
            raise ValueError(f"Unknown listing part: {part}")

        with job.lock:
            job.listing = updated

        _append_log(job, f"Redo of {part} complete.")
        _set_status(job, "success", action="", error="")
        _sync_job_to_db(job)
    except Exception as exc:
        _append_log(job, f"ERROR: {exc}")
        _set_status(job, "error", action="", error=str(exc))
        _sync_job_to_db(job)


@app.post("/api/jobs/<job_id>/redo-listing")
def redo_listing(job_id: str) -> Any:
    job = _get_job(job_id)
    payload = request.get_json(silent=True) or {}
    part = str(payload.get("part", "")).strip().lower()
    extra_context = str(payload.get("extra_context", "")).strip()

    if part not in {"description", "subtitles", "categories"}:
        return jsonify({"error": "part must be one of: description, subtitles, categories"}), 400

    with job.lock:
        if job.status == "running":
            return jsonify({"error": "Job is busy. Wait for current task to finish."}), 409
        final_doc = Path(job.final_docx)

    if not final_doc.exists():
        return jsonify({"error": "Final document not found. Generate the book first."}), 400

    t = threading.Thread(
        target=_run_redo_listing_part,
        args=(job_id, part, extra_context),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True})


@app.get("/api/jobs/<job_id>/listing")
def get_listing(job_id: str) -> Any:
    job = _get_job(job_id)
    with job.lock:
        return jsonify(job.listing or {})


@app.get("/api/jobs/<job_id>/download/<kind>")
def download_file(job_id: str, kind: str) -> Any:
    # Try in-memory job first; fall back to DB for historical books
    mapping: dict[str, Path | None] = {}
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job:
        with job.lock:
            mapping = {
                "input": Path(job.input_docx),
                "output": Path(job.output_docx),
                "final": Path(job.final_docx),
                "kindle": Path(job.kindle_docx) if job.kindle_docx else None,
                "paperback": Path(job.paperback_docx) if job.paperback_docx else None,
            }
    else:
        book = bookdb.get_book(job_id)
        if book is None:
            abort(404, description="Job not found")
        mapping = {
            "input": Path(book["input_docx"]) if book.get("input_docx") else None,
            "final": Path(book["final_docx"]) if book.get("final_docx") else None,
            "kindle": Path(book["kindle_docx"]) if book.get("kindle_docx") else None,
            "paperback": Path(book["paperback_docx"]) if book.get("paperback_docx") else None,
        }

    target = mapping.get(kind)
    if target is None:
        abort(404)
    if not target.exists() or not target.is_file():
        abort(404, description="File does not exist yet")
    return send_file(target, as_attachment=True)


# ----------------------------
# Book history
# ----------------------------

@app.get("/api/books")
def list_books() -> Any:
    """Return recent books for the history sidebar."""
    limit = _parse_int(request.args.get("limit"), 50)
    return jsonify({"books": bookdb.list_books(limit=limit)})


@app.get("/api/books/<book_id>")
def get_book(book_id: str) -> Any:
    """Return full book record for re-loading a past job."""
    book = bookdb.get_book(book_id)
    if book is None:
        abort(404, description="Book not found")
    return jsonify(book)


@app.delete("/api/books/<book_id>")
def delete_book(book_id: str) -> Any:
    """Delete a book from history."""
    if bookdb.delete_book(book_id):
        return jsonify({"ok": True})
    abort(404, description="Book not found")


@app.patch("/api/books/<book_id>")
def patch_book(book_id: str) -> Any:
    """Update editable fields of a book (currently: title)."""
    payload = request.get_json(silent=True) or {}
    new_title = (payload.get("title") or "").strip()
    if not new_title:
        return jsonify({"error": "title is required"}), 400
    if len(new_title) > 200:
        return jsonify({"error": "title too long (max 200 chars)"}), 400

    book = bookdb.get_book(book_id)
    if book is None:
        abort(404, description="Book not found")

    bookdb.update_book(book_id, title=new_title)

    # If this job is still in memory, keep the custom title aligned so future
    # syncs don't clobber it with the filename-derived default.
    with JOBS_LOCK:
        job = JOBS.get(book_id)
    if job is not None:
        with job.lock:
            job.custom_title = new_title

    return jsonify({"ok": True, "title": new_title})


# ----------------------------
# OpenClaw model & agent management
# ----------------------------

_MODEL_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_AGENT_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_MODEL_CACHE_TTL = 300  # seconds
_AGENT_CACHE_TTL = 300


@app.get("/api/openclaw-models")
def list_openclaw_models() -> Any:
    """Return available OpenClaw models + current default."""
    now = time.time()
    refresh = request.args.get("refresh") == "1"

    # Serve from cache if fresh enough
    if not refresh and _MODEL_CACHE["data"] and (now - _MODEL_CACHE["ts"]) < _MODEL_CACHE_TTL:
        return jsonify(_MODEL_CACHE["data"])

    try:
        models_proc = subprocess.run(
            ["openclaw", "models", "list", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        if models_proc.returncode != 0:
            return jsonify({"error": f"openclaw models list failed: {models_proc.stderr.strip()}"}), 500
        models_data = json.loads(models_proc.stdout)

        status_proc = subprocess.run(
            ["openclaw", "models", "status", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        default_model = ""
        if status_proc.returncode == 0:
            status_data = json.loads(status_proc.stdout)
            default_model = status_data.get("defaultModel", "")

        result = {
            "models": models_data.get("models", []),
            "default": default_model,
        }
        _MODEL_CACHE["data"] = result
        _MODEL_CACHE["ts"] = now
        return jsonify(result)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "OpenClaw CLI timed out. Make sure the gateway is running (openclaw gateway)."}), 504
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/openclaw-models/set")
def set_openclaw_model() -> Any:
    """Switch the default OpenClaw model."""
    payload = request.get_json(silent=True) or {}
    model_key = str(payload.get("model") or "").strip()
    if not model_key:
        return jsonify({"error": "model is required"}), 400

    try:
        proc = subprocess.run(
            ["openclaw", "models", "set", model_key],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return jsonify({"error": f"Failed to set model: {proc.stderr.strip() or proc.stdout.strip()}"}), 500
        # Invalidate cache so next fetch reflects the change
        _MODEL_CACHE["data"] = None
        _MODEL_CACHE["ts"] = 0.0
        return jsonify({"ok": True, "model": model_key})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "OpenClaw CLI timed out. Make sure the gateway is running (openclaw gateway)."}), 504
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/openclaw-agents")
def list_openclaw_agents() -> Any:
    """Return available OpenClaw agents."""
    now = time.time()
    refresh = request.args.get("refresh") == "1"

    if not refresh and _AGENT_CACHE["data"] and (now - _AGENT_CACHE["ts"]) < _AGENT_CACHE_TTL:
        return jsonify(_AGENT_CACHE["data"])

    try:
        proc = subprocess.run(
            ["openclaw", "agents", "list", "--json"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return jsonify({"error": f"openclaw agents list failed: {proc.stderr.strip()}"}), 500
        agents = json.loads(proc.stdout)
        result = {"agents": agents}
        _AGENT_CACHE["data"] = result
        _AGENT_CACHE["ts"] = now
        return jsonify(result)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "OpenClaw CLI timed out. Make sure the gateway is running (openclaw gateway)."}), 504
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
