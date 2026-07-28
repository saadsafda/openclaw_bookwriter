"""Publications: post-write book state and routes.

A *publication* represents a book worldwide. Per-marketplace ASINs and URLs
live inside its ``marketplaces`` JSON column. This module owns:

  - the Launch tab page render
  - publication CRUD HTTP endpoints
  - zip-file upload + auto-detection of kindle/paperback/cover
  - multi-marketplace Sponsored Products fan-out

Routes are registered on the Flask app via ``register(app)``.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from flask import abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

import db as bookdb

ROOT_DIR = Path(__file__).resolve().parent
PUB_DIR = ROOT_DIR / "publications_data"
PUB_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pub_workdir(pub_id: str) -> Path:
    p = PUB_DIR / pub_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _detect_zip_contents(extract_dir: Path) -> dict[str, str]:
    """Inspect an extracted zip and find kindle/paperback/cover files.

    Returns paths as absolute strings.
    """
    kindle = ""
    paperback = ""
    cover = ""
    other_docx: list[Path] = []
    for path in sorted(extract_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name.lower()
        suffix = path.suffix.lower()
        if suffix == ".docx":
            if "kindle" in name and not kindle:
                kindle = str(path)
            elif "paperback" in name and not paperback:
                paperback = str(path)
            else:
                other_docx.append(path)
        elif suffix in (".jpg", ".jpeg", ".png", ".webp"):
            if not cover or "cover" in name or "front" in name:
                cover = str(path)
    # Fallbacks when names don't include kindle/paperback hints
    if not kindle and other_docx:
        kindle = str(other_docx.pop(0))
    if not paperback and other_docx:
        paperback = str(other_docx.pop(0))
    return {
        "kindle_docx_path": kindle,
        "paperback_docx_path": paperback,
        "front_cover_path": cover,
    }


def _amazon_url(marketplace: str, asin: str) -> str:
    domain = {
        "US": "amazon.com",
        "CA": "amazon.ca",
        "UK": "amazon.co.uk",
        "AU": "amazon.com.au",
    }.get(marketplace.upper(), "amazon.com")
    return f"https://{domain}/dp/{asin}"


def auto_create_from_book(book: dict[str, Any]) -> str:
    """Auto-create a draft publication from a finished writer book.

    Idempotent: if a publication already exists for this book, returns its id
    without creating a new one.
    """
    existing = bookdb.find_publication_by_book(book["id"])
    if existing:
        return existing["id"]
    pub_id = uuid.uuid4().hex[:12]
    listing = book.get("listing") or {}
    bookdb.save_publication(
        pub_id,
        book_id=book["id"],
        title=book.get("title") or listing.get("title") or "",
        subtitle=listing.get("subtitle") or "",
        description=listing.get("description") or "",
        categories=listing.get("categories") or [],
        status="draft",
        marketplaces={},
        kindle_docx_path=book.get("kindle_docx") or "",
        paperback_docx_path=book.get("paperback_docx") or "",
    )
    return pub_id


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(app) -> None:
    """Attach all Launch-tab routes to the given Flask app."""

    # -- Page render -------------------------------------------------------
    @app.get("/launch")
    def launch_page():  # noqa: ANN202
        return render_template("launch.html")

    # -- List / create publications ---------------------------------------
    @app.get("/api/publications")
    def list_pubs():  # noqa: ANN202
        return jsonify({"publications": bookdb.list_publications()})

    @app.get("/api/publications/<pub_id>")
    def get_pub(pub_id: str):  # noqa: ANN202
        pub = bookdb.get_publication(pub_id)
        if not pub:
            abort(404)
        return jsonify({"publication": pub})

    @app.post("/api/publications")
    def create_pub():  # noqa: ANN202
        body = request.get_json(silent=True) or {}
        book_id = (body.get("book_id") or "").strip()
        if book_id:
            book = bookdb.get_book(book_id)
            if not book:
                return jsonify({"error": "book_id not found"}), 404
            pub_id = auto_create_from_book(book)
            return jsonify({"id": pub_id, "publication": bookdb.get_publication(pub_id)})
        # Manual create
        pub_id = uuid.uuid4().hex[:12]
        bookdb.save_publication(
            pub_id,
            title=(body.get("title") or "").strip(),
            subtitle=(body.get("subtitle") or "").strip(),
            description=(body.get("description") or "").strip(),
            categories=body.get("categories") or [],
            status="draft",
        )
        return jsonify({"id": pub_id, "publication": bookdb.get_publication(pub_id)})

    @app.post("/api/publications/<pub_id>")
    def update_pub(pub_id: str):  # noqa: ANN202
        body = request.get_json(silent=True) or {}
        if not bookdb.get_publication(pub_id):
            abort(404)
        # Enrich marketplaces: if URL missing but ASIN present, infer URL
        if isinstance(body.get("marketplaces"), dict):
            for mp, info in list(body["marketplaces"].items()):
                if isinstance(info, dict) and info.get("asin") and not info.get("url"):
                    info["url"] = _amazon_url(mp, info["asin"])
        bookdb.update_publication(pub_id, **body)
        return jsonify({"publication": bookdb.get_publication(pub_id)})

    @app.delete("/api/publications/<pub_id>")
    def del_pub(pub_id: str):  # noqa: ANN202
        ok = bookdb.delete_publication(pub_id)
        return jsonify({"ok": ok})

    # -- ZIP upload --------------------------------------------------------
    @app.post("/api/publications/upload-zip")
    def upload_zip():  # noqa: ANN202
        if "zip" not in request.files:
            return jsonify({"error": "missing 'zip' file field"}), 400
        f = request.files["zip"]
        if not f or not f.filename:
            return jsonify({"error": "empty filename"}), 400
        filename = secure_filename(f.filename)
        if not filename.lower().endswith(".zip"):
            return jsonify({"error": "file must be a .zip"}), 400

        pub_id = uuid.uuid4().hex[:12]
        workdir = _pub_workdir(pub_id)
        zip_path = workdir / filename
        f.save(zip_path)

        # Extract
        extract_dir = workdir / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                # Defend against zip-slip
                for member in zf.infolist():
                    target = (extract_dir / member.filename).resolve()
                    if not str(target).startswith(str(extract_dir.resolve())):
                        return jsonify({"error": "unsafe zip member"}), 400
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            shutil.rmtree(workdir, ignore_errors=True)
            return jsonify({"error": "not a valid zip"}), 400

        detected = _detect_zip_contents(extract_dir)

        # Optional metadata.json
        meta_title = ""
        meta_subtitle = ""
        meta_description = ""
        meta_categories: list[Any] = []
        meta_marketplaces: dict[str, Any] = {}
        meta_file = next(extract_dir.rglob("metadata.json"), None)
        if meta_file and meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                meta_title = (meta.get("title") or "").strip()
                meta_subtitle = (meta.get("subtitle") or "").strip()
                meta_description = (meta.get("description") or "").strip()
                meta_categories = meta.get("categories") or []
                if isinstance(meta.get("marketplaces"), dict):
                    meta_marketplaces = meta["marketplaces"]
            except Exception:
                pass

        # Title fallback: derive from zip filename
        if not meta_title:
            meta_title = Path(filename).stem.replace("_", " ").replace("-", " ").strip()

        bookdb.save_publication(
            pub_id,
            title=meta_title,
            subtitle=meta_subtitle,
            description=meta_description,
            categories=meta_categories,
            status="draft",
            marketplaces=meta_marketplaces,
            source_zip_path=str(zip_path),
            **detected,
        )
        return jsonify({
            "id": pub_id,
            "detected": detected,
            "publication": bookdb.get_publication(pub_id),
        })

    # -- File downloads ----------------------------------------------------
    @app.get("/api/publications/<pub_id>/file/<kind>")
    def download_pub_file(pub_id: str, kind: str):  # noqa: ANN202
        pub = bookdb.get_publication(pub_id)
        if not pub:
            abort(404)
        path_field = {
            "kindle": "kindle_docx_path",
            "paperback": "paperback_docx_path",
            "cover": "front_cover_path",
            "zip": "source_zip_path",
        }.get(kind)
        if not path_field:
            abort(404)
        p = pub.get(path_field) or ""
        if not p or not Path(p).exists():
            abort(404)
        return send_file(p, as_attachment=True, download_name=Path(p).name)
