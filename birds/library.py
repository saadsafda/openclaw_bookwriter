"""The source photo library — the freelancer's bird photos, in batches.

Unlike the cover reference library (one shared pool, reused by every book), the
bird photos are grouped: roughly 70 guides, 200-300 birds each, and "the
warblers guide" must not mix with "the raptors guide". So the unit here is a
**batch** — one guide's worth of photos — and every batch carries its own
index.

Storage is a directory per batch plus a JSON sidecar, deliberately the same
shape as covers/library.py: the set is operator-managed and worth being able to
inspect with a file browser when someone asks what "plate 147" actually was.

Photos are normalized on ingest (downscaled, stored as PNG) so the generation
path never has to think about a 12 MP JPEG straight off a camera.
"""

from __future__ import annotations

import io
import json
import re
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from PIL import Image

from .prompts import species_from_filename

ROOT_DIR = Path(__file__).resolve().parent.parent
LIBRARY_DIR = ROOT_DIR / "bird_sources"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

# images.edit rejects a source above 50 MB, and a reference larger than the
# generated plate wastes upload time without improving fidelity. 1536 is the
# longest edge the model actually consumes.
MAX_SOURCE_EDGE = 1536
MAX_UPLOAD_BYTES = 40 * 1024 * 1024


class BirdLibraryError(RuntimeError):
    """Raised for operator-fixable problems (bad upload, missing batch)."""


@dataclass
class BirdSource:
    """One uploaded photo: the input side of one plate.

    Two files are kept per photo. ``filename`` is the downscaled PNG the image
    API is given as a reference; ``original_filename`` is the operator's
    upload, byte-for-byte as it arrived. They are separate because the
    reference has to be small (upload limits, and the model consumes 1536px
    anyway) while the original is an asset in its own right — it is the only
    copy of the freelancer's work the pipeline holds, and re-encoding it would
    quietly throw away resolution nobody can get back.
    """

    id: str
    filename: str          # downscaled API reference, always .png
    original_name: str     # the name it was uploaded under
    species: str
    added_at: float
    original_filename: str = ""   # untouched upload; "" for pre-existing rows

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Batch:
    """One guide's worth of photos."""

    id: str
    name: str
    created_at: float
    sources: list[BirdSource]

    @property
    def dir(self) -> Path:
        return LIBRARY_DIR / self.id

    @property
    def index_path(self) -> Path:
        return self.dir / "index.json"

    def source_path(self, source: BirdSource) -> Path:
        """The downscaled reference handed to the image API."""
        return self.dir / source.filename

    def original_path(self, source: BirdSource) -> Path | None:
        """The untouched upload, or None when this row predates originals."""
        if not source.original_filename:
            return None
        path = self.dir / "originals" / source.original_filename
        return path if path.is_file() else None

    def to_dict(self, include_sources: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "count": len(self.sources),
        }
        if include_sources:
            payload["sources"] = [s.to_dict() for s in self.sources]
        return payload


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _safe_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _'-]+", "", (name or "").strip())
    return cleaned[:80].strip() or fallback


# ---- batch persistence -----------------------------------------------------


def _read_batch(batch_dir: Path) -> Batch | None:
    index = batch_dir / "index.json"
    if not index.is_file():
        return None
    try:
        raw = json.loads(index.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None

    sources: list[BirdSource] = []
    for item in raw.get("sources") or []:
        if not isinstance(item, dict):
            continue
        try:
            sources.append(
                BirdSource(
                    id=str(item["id"]),
                    filename=str(item["filename"]),
                    original_name=str(item.get("original_name") or ""),
                    species=str(item.get("species") or ""),
                    added_at=float(item.get("added_at") or 0.0),
                    original_filename=str(item.get("original_filename") or ""),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    return Batch(
        id=str(raw.get("id") or batch_dir.name),
        name=str(raw.get("name") or batch_dir.name),
        created_at=float(raw.get("created_at") or 0.0),
        sources=sources,
    )


def _write_batch(batch: Batch) -> None:
    _ensure_dir(batch.dir)
    payload = {
        "id": batch.id,
        "name": batch.name,
        "created_at": batch.created_at,
        "sources": [s.to_dict() for s in batch.sources],
    }
    batch.index_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def list_batches() -> list[Batch]:
    """Every batch, newest first — the operator works on the current guide."""
    _ensure_dir(LIBRARY_DIR)
    batches = [
        b
        for child in LIBRARY_DIR.iterdir()
        if child.is_dir() and (b := _read_batch(child)) is not None
    ]
    return sorted(batches, key=lambda b: b.created_at, reverse=True)


def get_batch(batch_id: str) -> Batch:
    safe = Path(batch_id or "").name       # never let an id escape LIBRARY_DIR
    batch = _read_batch(LIBRARY_DIR / safe) if safe else None
    if batch is None:
        raise BirdLibraryError(f"No batch with id {batch_id!r}.")
    return batch


def create_batch(name: str = "") -> Batch:
    batch = Batch(
        id=uuid.uuid4().hex[:12],
        name=_safe_name(name, "Untitled guide"),
        created_at=time.time(),
        sources=[],
    )
    _ensure_dir(batch.dir)
    _write_batch(batch)
    return batch


def rename_batch(batch_id: str, name: str) -> Batch:
    batch = get_batch(batch_id)
    batch.name = _safe_name(name, batch.name)
    _write_batch(batch)
    return batch


def delete_batch(batch_id: str) -> None:
    import shutil

    batch = get_batch(batch_id)
    shutil.rmtree(batch.dir, ignore_errors=True)


# ---- source photos ---------------------------------------------------------


def add_source(batch_id: str, data: bytes, filename: str, species: str = "") -> BirdSource:
    """Ingest one photo: validate, downscale, store as PNG, index.

    The species name defaults to the filename, which is how the freelancer's
    set is labelled. The operator can correct it before generating.
    """
    if not data:
        raise BirdLibraryError("Uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise BirdLibraryError("Photo is too large (40 MB limit).")

    suffix = Path(filename or "").suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise BirdLibraryError(
            f"Unsupported image type {suffix or '(none)'}. "
            f"Use one of: {', '.join(sorted(IMAGE_SUFFIXES))}."
        )

    batch = get_batch(batch_id)
    source_id = uuid.uuid4().hex[:12]
    stored_name = f"{source_id}.png"
    dest = batch.dir / stored_name

    # Validate before writing anything, so a corrupt upload leaves no files
    # behind.
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
    except Exception as exc:
        raise BirdLibraryError(f"Could not read that image: {exc}") from exc

    # The original is written first and never re-encoded — same bytes, same
    # resolution, original file extension. This is the operator's own photo;
    # the pipeline is a consumer of it, not its owner.
    originals_dir = batch.dir / "originals"
    originals_dir.mkdir(parents=True, exist_ok=True)
    original_stored = f"{source_id}{suffix}"
    (originals_dir / original_stored).write_bytes(data)

    try:
        with Image.open(io.BytesIO(data)) as img:
            # RGB: the source is a photograph, so any alpha it carries is
            # incidental, and images.edit wants a plain opaque reference.
            rgb = img.convert("RGB")
            rgb.thumbnail((MAX_SOURCE_EDGE, MAX_SOURCE_EDGE), Image.Resampling.LANCZOS)
            rgb.save(dest, format="PNG")
    except Exception as exc:
        (originals_dir / original_stored).unlink(missing_ok=True)
        raise BirdLibraryError(f"Could not read that image: {exc}") from exc

    source = BirdSource(
        id=source_id,
        filename=stored_name,
        original_name=(filename or "")[:200],
        species=(species or "").strip() or species_from_filename(filename),
        added_at=time.time(),
        original_filename=original_stored,
    )
    batch.sources.append(source)
    _write_batch(batch)
    return source


def update_source(batch_id: str, source_id: str, species: str) -> BirdSource:
    """Correct one bird's species name."""
    batch = get_batch(batch_id)
    for source in batch.sources:
        if source.id == source_id:
            source.species = (species or "").strip()
            _write_batch(batch)
            return source
    raise BirdLibraryError(f"No photo with id {source_id!r}.")


def delete_source(batch_id: str, source_id: str) -> None:
    batch = get_batch(batch_id)
    kept = [s for s in batch.sources if s.id != source_id]
    if len(kept) == len(batch.sources):
        raise BirdLibraryError(f"No photo with id {source_id!r}.")

    for source in batch.sources:
        if source.id == source_id:
            batch.source_path(source).unlink(missing_ok=True)
            original = batch.original_path(source)
            if original is not None:
                original.unlink(missing_ok=True)
    batch.sources = kept
    _write_batch(batch)


def get_source(batch_id: str, source_id: str) -> tuple[Batch, BirdSource]:
    batch = get_batch(batch_id)
    for source in batch.sources:
        if source.id == source_id:
            return batch, source
    raise BirdLibraryError(f"No photo with id {source_id!r}.")


def resolve_sources(batch_id: str, source_ids: list[str] | None = None) -> tuple[Batch, list[BirdSource]]:
    """The photos for a run: the given ids, or the whole batch when None/empty.

    Photos whose file has gone missing are skipped rather than raising — one
    deleted file should not block a 250-bird run.
    """
    batch = get_batch(batch_id)
    by_id = {s.id: s for s in batch.sources}

    if source_ids:
        chosen = [by_id[i] for i in source_ids if i in by_id]
    else:
        chosen = list(batch.sources)

    chosen = [s for s in chosen if batch.source_path(s).is_file()]
    if not chosen:
        raise BirdLibraryError(
            "No photos available to generate from. Upload the bird photos to "
            "this batch first."
        )
    return batch, chosen
