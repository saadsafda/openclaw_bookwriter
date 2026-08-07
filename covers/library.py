"""The reference cover library — proven covers, uploaded once, reused forever.

The operator's existing workflow attaches a different example cover each time
to get a different style. Re-uploading the same proven set for every one of a
hundred books is the part worth automating, so the library is persistent and
shared: upload once, and every book defaults to it.

Storage is a plain directory of images plus a JSON sidecar for metadata. No
database table — the library is small (tens of images), operator-managed, and
inspectable with a file browser, which matters when someone wants to check
what "proven cover 04" actually is.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from PIL import Image

ROOT_DIR = Path(__file__).resolve().parent.parent
LIBRARY_DIR = ROOT_DIR / "cover_references"
INDEX_PATH = LIBRARY_DIR / "index.json"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

# images.edit rejects a source image above 50 MB, and very large references
# waste upload time without improving style transfer. References are
# downscaled on ingest to bound both.
MAX_REFERENCE_EDGE = 1536
MAX_UPLOAD_BYTES = 40 * 1024 * 1024


class CoverLibraryError(RuntimeError):
    """Raised for operator-fixable problems (bad upload, missing reference)."""


@dataclass
class Reference:
    id: str
    filename: str
    label: str
    added_at: float

    @property
    def path(self) -> Path:
        return LIBRARY_DIR / self.filename

    def exists(self) -> bool:
        return self.path.is_file()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["exists"] = self.exists()
        return d


def _ensure_dir() -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)


def _load_index() -> list[Reference]:
    _ensure_dir()
    if not INDEX_PATH.is_file():
        return []
    try:
        raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []

    refs: list[Reference] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            refs.append(
                Reference(
                    id=str(item["id"]),
                    filename=str(item["filename"]),
                    label=str(item.get("label") or ""),
                    added_at=float(item.get("added_at") or 0.0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return refs


def _save_index(refs: list[Reference]) -> None:
    _ensure_dir()
    payload = [
        {
            "id": r.id,
            "filename": r.filename,
            "label": r.label,
            "added_at": r.added_at,
        }
        for r in refs
    ]
    INDEX_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def list_references(include_missing: bool = False) -> list[Reference]:
    """Every reference in the library, oldest first (stable display order)."""
    refs = _load_index()
    if not include_missing:
        refs = [r for r in refs if r.exists()]
    return sorted(refs, key=lambda r: r.added_at)


def get_reference(ref_id: str) -> Reference:
    for ref in _load_index():
        if ref.id == ref_id:
            return ref
    raise CoverLibraryError(f"No reference with id {ref_id!r}.")


def _safe_label(name: str) -> str:
    stem = Path(name or "").stem
    cleaned = re.sub(r"[^A-Za-z0-9 _-]+", "", stem).strip()
    return cleaned[:60] or "reference"


def add_reference(data: bytes, filename: str, label: str = "") -> Reference:
    """Ingest one uploaded cover: validate, downscale, store, index."""
    if not data:
        raise CoverLibraryError("Uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise CoverLibraryError("Reference image is too large (40 MB limit).")

    suffix = Path(filename or "").suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise CoverLibraryError(
            f"Unsupported image type {suffix or '(none)'}. "
            f"Use one of: {', '.join(sorted(IMAGE_SUFFIXES))}."
        )

    _ensure_dir()
    ref_id = uuid.uuid4().hex[:12]
    # Always store as PNG: images.edit wants PNG, and normalizing on ingest
    # means the generation path never has to convert.
    stored_name = f"{ref_id}.png"
    dest = LIBRARY_DIR / stored_name

    import io

    try:
        with Image.open(io.BytesIO(data)) as img:
            rgb = img.convert("RGB")
            rgb.thumbnail(
                (MAX_REFERENCE_EDGE, MAX_REFERENCE_EDGE),
                Image.Resampling.LANCZOS,
            )
            rgb.save(dest, format="PNG")
    except CoverLibraryError:
        raise
    except Exception as exc:
        raise CoverLibraryError(f"Could not read that image: {exc}") from exc

    ref = Reference(
        id=ref_id,
        filename=stored_name,
        label=label.strip() or _safe_label(filename),
        added_at=time.time(),
    )
    refs = _load_index()
    refs.append(ref)
    _save_index(refs)
    return ref


def delete_reference(ref_id: str) -> None:
    refs = _load_index()
    kept = [r for r in refs if r.id != ref_id]
    if len(kept) == len(refs):
        raise CoverLibraryError(f"No reference with id {ref_id!r}.")

    for ref in refs:
        if ref.id == ref_id:
            ref.path.unlink(missing_ok=True)
    _save_index(kept)


def rename_reference(ref_id: str, label: str) -> Reference:
    refs = _load_index()
    target: Reference | None = None
    for ref in refs:
        if ref.id == ref_id:
            ref.label = label.strip() or ref.label
            target = ref
            break
    if target is None:
        raise CoverLibraryError(f"No reference with id {ref_id!r}.")
    _save_index(refs)
    return target


def resolve_reference_paths(ref_ids: list[str] | None = None) -> list[Path]:
    """Paths for a run: the given ids, or the whole library when None/empty.

    Missing files are skipped rather than raising, so one deleted reference
    doesn't block a ten-cover run.
    """
    available = {r.id: r for r in list_references()}
    if ref_ids:
        chosen = [available[i] for i in ref_ids if i in available]
    else:
        chosen = list(available.values())
        chosen.sort(key=lambda r: r.added_at)

    if not chosen:
        raise CoverLibraryError(
            "No reference covers available. Upload at least one proven cover "
            "to the library first."
        )
    return [r.path for r in chosen]
