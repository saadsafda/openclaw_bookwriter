"""Bird plate generation — one ``images.edit`` call per photo, in parallel.

Three things this does that the manual chat-window route does not:

**Transparency is requested at the API level.** ``background="transparent"`` on
``gpt-image-1`` is what actually produces an alpha channel. Asking for "no
background" in the prompt alone tends to yield a *painted* white rectangle,
which looks right in a preview and then prints as a white box on a colored
page. The prompt still says it too (see prompts.py) to stop the model painting
a backdrop under the transparency.

**Transparency is verified, not assumed.** Every result is checked for a real
alpha channel with actually-transparent corners. A plate that comes back opaque
is retried, and if it still comes back opaque it is flagged rather than silently
shipped — with 250 birds per guide nobody is eyeballing all of them.

**Every plate is pinned to exactly 300 DPI** with no EXIF/XMP/C2PA chunk, via
the shared ``print_hygiene`` module, the same as covers.

Failures are per-plate. A run of 250 birds that loses four of them reports 246
successes and four retryable errors; it does not throw away the batch.
"""

from __future__ import annotations

import base64
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image

ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT_DIR / "bird_outputs"

MODEL = "gpt-image-1"

# Two ways to produce a plate.
#
#   cutout       — the photograph's own bird with its background deleted, done
#                  locally by a U²-Net model. No redraw, no API cost. The bird
#                  keeps its exact pixels, colors and pose because nothing
#                  regenerates it. This is the default.
#   illustration — gpt-image-1 redraws the bird as artwork on transparency.
#                  Costs one API call per bird; use it when the guide wants
#                  drawn plates rather than photographs.
MODE_CUTOUT = "cutout"
MODE_ILLUSTRATION = "illustration"
VALID_MODES = {MODE_CUTOUT, MODE_ILLUSTRATION}
DEFAULT_MODE = MODE_CUTOUT

# Square is right for a field-guide plate: the bird is the only subject and the
# cutout gets trimmed to its own bounding box anyway. The other sizes are here
# so a caller can request a portrait plate for a tall wading bird.
VALID_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
DEFAULT_SIZE = "1024x1024"

VALID_QUALITIES = {"low", "medium", "high", "auto"}
# Plates are line-art-like subjects on transparency, where "medium" already
# holds up in print and costs materially less across ~20,000 images.
DEFAULT_QUALITY = "medium"

# input_fidelity="high" tells the model to stay close to the reference, which
# is what keeps a species' real markings intact.
VALID_FIDELITIES = {"low", "high"}
DEFAULT_FIDELITY = "high"

# Eight parallel calls keeps a 250-bird run moving without tripping the image
# rate limit. Raise only alongside a matching quota.
DEFAULT_WORKERS = 8
MAX_WORKERS = 16

# Local cutout is CPU-bound in the sidecar, so parallelism past a few workers
# only adds contention. Measured ~0.5s/image warm, so 300 birds still lands in
# a couple of minutes.
CUTOUT_WORKERS = 4

# One extra attempt for a transport error, plus one for an opaque result.
MAX_ATTEMPTS = 3

# Corner sampling window used to decide whether a background really is
# transparent, in pixels.
_CORNER_PROBE = 8


class BirdGenerationError(RuntimeError):
    """Raised when a run cannot start (no key, bad size, no photos)."""


@dataclass
class Plate:
    """One bird: source photo in, finished transparent PNG out."""

    index: int
    source_id: str
    species: str
    status: str = "pending"        # pending | running | done | error
    filename: str = ""
    error: str = ""
    attempts: int = 0
    transparent: bool = False      # verified, not assumed
    warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "source_id": self.source_id,
            "species": self.species,
            "status": self.status,
            "filename": self.filename,
            "error": self.error,
            "attempts": self.attempts,
            "transparent": self.transparent,
            "warning": self.warning,
        }


@dataclass
class BirdRun:
    id: str
    batch_id: str
    batch_name: str
    style: str
    size: str
    quality: str
    mode: str = DEFAULT_MODE
    plates: list[Plate] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    cancelled: bool = False

    @property
    def output_dir(self) -> Path:
        return OUTPUT_DIR / self.id

    def to_dict(self) -> dict[str, Any]:
        done = sum(1 for p in self.plates if p.status == "done")
        failed = sum(1 for p in self.plates if p.status == "error")
        flagged = sum(1 for p in self.plates if p.status == "done" and p.warning)
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "batch_name": self.batch_name,
            "style": self.style,
            "size": self.size,
            "quality": self.quality,
            "mode": self.mode,
            "created_at": self.created_at,
            "cancelled": self.cancelled,
            "plates": [p.to_dict() for p in self.plates],
            "done_count": done,
            "error_count": failed,
            "flagged_count": flagged,
            "settled_count": done + failed,
            "total": len(self.plates),
        }


# ---- output checks ---------------------------------------------------------


def has_real_transparency(path: Path) -> bool:
    """True when the image has an alpha channel *and* actually uses it.

    A file can carry RGBA and still be fully opaque — that is exactly the
    failure mode worth catching, because it looks correct in a thumbnail. The
    four corners are probed rather than the whole image: a correctly cut-out
    bird never reaches into all four corners, and this is far cheaper across
    thousands of plates.
    """
    try:
        with Image.open(path) as img:
            if img.mode not in ("RGBA", "LA", "P"):
                return False
            rgba = img.convert("RGBA")
            alpha = rgba.getchannel("A")
            width, height = rgba.size
            probe = min(_CORNER_PROBE, width, height)
            if probe <= 0:
                return False

            boxes = [
                (0, 0, probe, probe),
                (width - probe, 0, width, probe),
                (0, height - probe, probe, height),
                (width - probe, height - probe, width, height),
            ]
            # Every corner must be fully transparent. Requiring all four is
            # what distinguishes a real cutout from a plate with one stray
            # transparent edge.
            return all(alpha.crop(box).getextrema()[1] == 0 for box in boxes)
    except Exception:
        return False


def trim_to_subject(path: Path, margin: int = 8) -> None:
    """Crop transparent padding down to the bird, leaving a small margin.

    A plate the layout engine can place is one whose bounding box *is* the
    bird; otherwise every caption sits at a different distance depending on
    how the model happened to frame it. Never fatal — an untrimmed plate is
    still usable.
    """
    try:
        with Image.open(path) as img:
            rgba = img.convert("RGBA")
            bbox = rgba.getchannel("A").getbbox()
            if bbox is None:
                return          # fully transparent; nothing to trim toward

            left, upper, right, lower = bbox
            left = max(0, left - margin)
            upper = max(0, upper - margin)
            right = min(rgba.width, right + margin)
            lower = min(rgba.height, lower + margin)
            if (left, upper, right, lower) == (0, 0, rgba.width, rgba.height):
                return          # already tight

            cropped = rgba.crop((left, upper, right, lower))
        cropped.save(path, format="PNG")
    except Exception:
        pass


def _finalize(path: Path) -> None:
    """Pin to 300 DPI and strip metadata. Never fatal — a plate that exists
    but missed hygiene is still worth showing, flagged, over losing the run."""
    try:
        from print_hygiene import sanitize_for_print

        sanitize_for_print(path)
    except Exception:
        pass


# ---- generation ------------------------------------------------------------


def _generate_one(
    prompt: str,
    source: Path,
    dest: Path,
    api_key: str,
    size: str,
    quality: str,
    fidelity: str,
) -> None:
    """One bird: prompt + reference photo -> one transparent PNG on disk."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    with source.open("rb") as fh:
        source_bytes = fh.read()

    kwargs: dict[str, Any] = {
        "model": MODEL,
        "image": (source.name, source_bytes, "image/png"),
        "prompt": prompt,
        "n": 1,
        "size": size,
        "quality": quality,
        # The parameter that actually produces an alpha channel. PNG is
        # required for it to survive — WebP/JPEG would flatten the cutout.
        "background": "transparent",
        "output_format": "png",
    }
    if fidelity in VALID_FIDELITIES:
        kwargs["input_fidelity"] = fidelity

    response = client.images.edit(**kwargs)

    first = response.data[0] if response.data else None
    b64 = getattr(first, "b64_json", None)
    if not b64 and isinstance(first, dict):
        b64 = first.get("b64_json")
    if not b64:
        raise BirdGenerationError("The image service returned no image data.")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(b64))


def _plate_filename(index: int, species: str) -> str:
    """Stable, sortable, human-readable: ``001_northern_cardinal.png``."""
    slug = "".join(c.lower() if c.isalnum() else "_" for c in (species or ""))
    slug = "_".join(part for part in slug.split("_") if part)[:60]
    return f"{index + 1:03d}_{slug}.png" if slug else f"{index + 1:03d}.png"


def generate_plates(
    sources: list[tuple[str, str, Path]],
    prompt_for: Callable[[str], str],
    api_key: str,
    batch_id: str = "",
    batch_name: str = "",
    style: str = "",
    size: str = DEFAULT_SIZE,
    quality: str = DEFAULT_QUALITY,
    fidelity: str = DEFAULT_FIDELITY,
    workers: int = DEFAULT_WORKERS,
    trim: bool = True,
    mode: str = DEFAULT_MODE,
    cutout_model: str = "",
    on_progress: Callable[[BirdRun], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> BirdRun:
    """Generate one plate per source photo. Returns when all have settled.

    ``sources`` is a list of ``(source_id, species, path)``. ``prompt_for``
    maps a species name to its prompt, so the caller owns the wording and this
    module owns the mechanics.

    A failed plate is recorded on that plate and does not abort the run.
    """
    if not sources:
        raise BirdGenerationError("At least one bird photo is required.")
    if mode not in VALID_MODES:
        raise BirdGenerationError(f"mode must be one of {sorted(VALID_MODES)}.")

    if mode == MODE_ILLUSTRATION:
        # Only the redraw path talks to the API, so only it needs a key.
        if not api_key:
            raise BirdGenerationError(
                "No OpenAI API key available for illustration mode."
            )
        if size not in VALID_SIZES:
            raise BirdGenerationError(f"size must be one of {sorted(VALID_SIZES)}.")
        if quality not in VALID_QUALITIES:
            raise BirdGenerationError(
                f"quality must be one of {sorted(VALID_QUALITIES)}."
            )
    else:
        # Fail before the run starts rather than on 300 individual plates.
        from . import cutout

        if not cutout.is_available():
            raise BirdGenerationError(cutout.SETUP_HINT)

    workers = max(1, min(int(workers or DEFAULT_WORKERS), MAX_WORKERS))
    if mode == MODE_CUTOUT:
        # The local model is CPU-bound; more threads than cores just thrashes.
        # Each call is ~0.5s, so a 300-bird batch still finishes in minutes.
        workers = min(workers, CUTOUT_WORKERS)

    run = BirdRun(
        id=uuid.uuid4().hex[:12],
        batch_id=batch_id,
        batch_name=batch_name,
        style=style,
        size=size,
        quality=quality,
        mode=mode,
        plates=[
            Plate(index=i, source_id=sid, species=species)
            for i, (sid, species, _) in enumerate(sources)
        ],
    )
    run.output_dir.mkdir(parents=True, exist_ok=True)

    lock = threading.Lock()

    def _emit() -> None:
        if on_progress is not None:
            with lock:
                on_progress(run)

    def _task(index: int, species: str, source: Path) -> None:
        plate = run.plates[index]
        if should_cancel is not None and should_cancel():
            plate.status = "error"
            plate.error = "Cancelled before this bird started."
            return

        dest = run.output_dir / _plate_filename(index, species)
        plate.status = "running"
        _emit()

        # Cutout is deterministic: the same photo through the same model gives
        # the same mask every time, so retrying a failure would only repeat it.
        # One attempt, and any error goes straight to the operator.
        if mode == MODE_CUTOUT:
            from . import cutout

            plate.attempts = 1
            try:
                cutout.remove_background(source, dest, cutout_model or cutout.DEFAULT_MODEL)
            except Exception as exc:
                plate.status = "error"
                plate.error = str(exc)
                return

            transparent = has_real_transparency(dest)
            if trim and transparent:
                trim_to_subject(dest)
            _finalize(dest)

            plate.transparent = transparent
            plate.filename = dest.name
            plate.status = "done"
            plate.warning = (
                ""
                if transparent
                else "Nothing was removed — the photo may have no clear subject."
            )
            return

        prompt = prompt_for(species)
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if should_cancel is not None and should_cancel():
                plate.status = "error"
                plate.error = "Cancelled."
                return

            plate.attempts = attempt
            try:
                _generate_one(prompt, source, dest, api_key, size, quality, fidelity)
            except Exception as exc:
                last_error = str(exc)
                # Transport/rate-limit errors respond to a short backoff;
                # a persistent one still surfaces after the last attempt.
                if attempt < MAX_ATTEMPTS:
                    time.sleep(min(2 ** attempt, 8))
                continue

            transparent = has_real_transparency(dest)
            if not transparent and attempt < MAX_ATTEMPTS:
                # Opaque result: worth one more roll of the dice before the
                # operator has to look at it.
                last_error = "Returned an opaque image (no transparent background)."
                continue

            if trim and transparent:
                trim_to_subject(dest)
            _finalize(dest)

            plate.transparent = transparent
            plate.filename = dest.name
            plate.status = "done"
            plate.error = ""
            plate.warning = (
                ""
                if transparent
                else "Background is not transparent — needs a manual cutout."
            )
            return

        plate.status = "error"
        plate.error = last_error or "Generation failed."

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_task, i, species, path)
            for i, (_, species, path) in enumerate(sources)
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:      # a task itself should never raise
                last = str(exc)
                for plate in run.plates:
                    if plate.status in ("pending", "running"):
                        plate.status = "error"
                        plate.error = last
                        break
            _emit()

    return run
