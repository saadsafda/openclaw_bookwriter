"""Illustration editing for story books.

Two kinds of edit:

  * AI edits  — OpenAI images.edit, so the picture can be changed by
    instruction without regenerating it from scratch.
  * Local edits — deterministic Pillow operations (crop, rotate, flip,
    brightness, contrast, saturation, sharpness) that cost nothing.

Illustrations attach to either a story or a chapter, so every entry point takes
a target selector rather than a chapter number. Every edit snapshots the
previous file into a history folder, so any change can be undone.

The no-people / no-text constraints are re-appended to every AI prompt here,
exactly as at generation time — an operator instruction cannot opt out of them.
These books are about real, named people, so a generated face would read as a
depiction of that person.
"""

from __future__ import annotations

import base64
import io
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from .engine import Chapter, Story, StoryBook, StoryError

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_HISTORY = 12

# Sizes the images.edit endpoint accepts. A source image of another size is
# still fine — the API returns one of these — but we validate what we request.
VALID_EDIT_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}

# Local adjustment bounds. Factors are multipliers where 1.0 is unchanged.
ADJUST_LIMITS = {
    "brightness": (0.2, 2.5),
    "contrast": (0.2, 2.5),
    "saturation": (0.0, 3.0),
    "sharpness": (0.0, 4.0),
}

IMAGE_CONSTRAINTS = (
    " Keep the result in strictly neutral grayscale: only deep black and a few "
    "distinct shades of cool gray on a pure white background. No color of any "
    "kind. Absolutely no people and no human figures. Absolutely no text, "
    "letters, numbers, words, labels, captions, signatures, watermarks, "
    "frames, or borders anywhere in the image."
)

Target = Union[Story, Chapter]


@dataclass
class ImageEditResult:
    path: Path
    history_count: int
    operation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "history_count": self.history_count,
            "operation": self.operation,
        }


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def resolve_target(
    book: StoryBook,
    *,
    story_id: str = "",
    chapter_number: int = 0,
) -> tuple[Target, str]:
    """Find the story or chapter an image edit applies to.

    Returns the object plus a short slug used to key its history folder.
    """
    from .edit import find_chapter, require_story

    if story_id:
        _, story = require_story(book, story_id)
        return story, f"story_{story.number:03d}"

    chapter = find_chapter(book, chapter_number)
    if chapter is None:
        raise StoryError(f"Chapter {chapter_number} not found.")
    return chapter, f"chapter_{chapter.number}"


def target_image_path(target: Target) -> Path:
    if not target.illustration_path:
        raise StoryError("This item has no illustration yet.")
    path = Path(target.illustration_path)
    if not path.exists():
        raise StoryError("The illustration file is missing from disk.")
    return path


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def _history_dir(image_path: Path, slug: str) -> Path:
    d = image_path.parent / "history" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot(image_path: Path, slug: str) -> Path:
    """Copy the current image into history before it is overwritten."""
    hist = _history_dir(image_path, slug)
    stamp = f"{time.time():.6f}".replace(".", "")
    target = hist / f"{stamp}{image_path.suffix}"
    shutil.copy2(image_path, target)

    # Trim oldest beyond the cap so history can't grow without bound.
    snaps = sorted(p for p in hist.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    for old in snaps[:-MAX_HISTORY]:
        old.unlink(missing_ok=True)
    return target


def history_list(image_path: Path, slug: str) -> list[Path]:
    hist = _history_dir(image_path, slug)
    return sorted(
        (p for p in hist.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES),
        reverse=True,
    )


def to_grayscale(image_path: Path) -> None:
    """Force an image to true neutral gray.

    The prompt asks for grayscale, but image models routinely return a slight
    colour cast. Interiors print black and white, so convert rather than trust:
    this is what actually guarantees a neutral page.
    """
    from PIL import Image

    with Image.open(image_path) as src:
        gray = src.convert("L").convert("RGB")
    gray.save(image_path)


def _finalize(image_path: Path, *, grayscale: bool = True) -> None:
    """Re-apply grayscale and print DPI/metadata stripping after a change."""
    if grayscale:
        try:
            to_grayscale(image_path)
        except Exception:
            # A conversion failure must not lose the edit itself.
            pass
    try:
        import openclaw_image_maker as image_maker
        image_maker.prepare_image_for_print(image_path)
    except Exception:
        # Best-effort: a DPI tagging failure must not lose the edit itself.
        pass


def _result(image_path: Path, slug: str, operation: str) -> ImageEditResult:
    return ImageEditResult(
        path=image_path,
        history_count=len(history_list(image_path, slug)),
        operation=operation,
    )


def image_info(book: StoryBook, *, story_id: str = "", chapter_number: int = 0) -> dict[str, Any]:
    """Dimensions and undo depth, for the editor's image pane."""
    from PIL import Image

    target, slug = resolve_target(book, story_id=story_id, chapter_number=chapter_number)
    if not target.illustration_path or not Path(target.illustration_path).exists():
        return {"has_image": False}

    path = Path(target.illustration_path)
    with Image.open(path) as img:
        width, height = img.size

    return {
        "has_image": True,
        "path": str(path),
        "width": width,
        "height": height,
        "history_count": len(history_list(path, slug)),
        "prompt": target.illustration_prompt,
    }


def undo(book: StoryBook, *, story_id: str = "", chapter_number: int = 0) -> ImageEditResult:
    """Restore the most recent snapshot, putting the current image back on the
    stack so undo is itself reversible."""
    target, slug = resolve_target(book, story_id=story_id, chapter_number=chapter_number)
    image_path = target_image_path(target)
    snaps = history_list(image_path, slug)
    if not snaps:
        raise StoryError("There is nothing to undo for this image.")

    latest = snaps[0]
    # Park the current state as a redo point, then restore the snapshot.
    hist = _history_dir(image_path, slug)
    redo_stamp = f"{time.time():.6f}".replace(".", "")
    shutil.copy2(image_path, hist / f"{redo_stamp}{image_path.suffix}")

    shutil.copy2(latest, image_path)
    latest.unlink(missing_ok=True)

    _finalize(image_path)
    return _result(image_path, slug, "undo")


# ---------------------------------------------------------------------------
# Local (Pillow) edits — free and instant
# ---------------------------------------------------------------------------

def adjust_image(
    book: StoryBook,
    *,
    story_id: str = "",
    chapter_number: int = 0,
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    sharpness: float = 1.0,
) -> ImageEditResult:
    from PIL import Image, ImageEnhance

    for name, value in (
        ("brightness", brightness), ("contrast", contrast),
        ("saturation", saturation), ("sharpness", sharpness),
    ):
        lo, hi = ADJUST_LIMITS[name]
        if not (lo <= value <= hi):
            raise StoryError(f"{name} must be between {lo} and {hi}.")

    target, slug = resolve_target(book, story_id=story_id, chapter_number=chapter_number)
    image_path = target_image_path(target)
    snapshot(image_path, slug)

    with Image.open(image_path) as src:
        img = src.convert("RGB")
    for enhancer_cls, value in (
        (ImageEnhance.Brightness, brightness),
        (ImageEnhance.Contrast, contrast),
        (ImageEnhance.Color, saturation),
        (ImageEnhance.Sharpness, sharpness),
    ):
        if value != 1.0:
            img = enhancer_cls(img).enhance(value)
    img.save(image_path)

    _finalize(image_path)
    return _result(image_path, slug, "adjust")


def crop_image(
    book: StoryBook,
    left: float,
    top: float,
    right: float,
    bottom: float,
    *,
    story_id: str = "",
    chapter_number: int = 0,
) -> ImageEditResult:
    """Crop using fractional coordinates (0-1), so the client doesn't need to
    know the pixel dimensions."""
    from PIL import Image

    for name, v in (("left", left), ("top", top), ("right", right), ("bottom", bottom)):
        if not (0.0 <= v <= 1.0):
            raise StoryError(f"{name} must be between 0 and 1.")
    if right - left < 0.05 or bottom - top < 0.05:
        raise StoryError("The crop region is too small.")

    target, slug = resolve_target(book, story_id=story_id, chapter_number=chapter_number)
    image_path = target_image_path(target)
    snapshot(image_path, slug)

    with Image.open(image_path) as src:
        img = src.convert("RGB")
        w, h = img.size
        img = img.crop((int(left * w), int(top * h), int(right * w), int(bottom * h)))
    img.save(image_path)

    _finalize(image_path)
    return _result(image_path, slug, "crop")


def rotate_image(
    book: StoryBook,
    degrees: int,
    *,
    story_id: str = "",
    chapter_number: int = 0,
) -> ImageEditResult:
    from PIL import Image

    if degrees % 90 != 0:
        raise StoryError("Rotation must be a multiple of 90 degrees.")

    target, slug = resolve_target(book, story_id=story_id, chapter_number=chapter_number)
    image_path = target_image_path(target)
    snapshot(image_path, slug)

    with Image.open(image_path) as src:
        # expand=True keeps the whole picture when a 90/270 turn swaps the axes.
        img = src.convert("RGB").rotate(-degrees, expand=True, fillcolor="white")
    img.save(image_path)

    _finalize(image_path)
    return _result(image_path, slug, "rotate")


def flip_image(
    book: StoryBook,
    axis: str,
    *,
    story_id: str = "",
    chapter_number: int = 0,
) -> ImageEditResult:
    from PIL import Image

    axis = (axis or "").strip().lower()
    if axis not in {"horizontal", "vertical"}:
        raise StoryError("axis must be 'horizontal' or 'vertical'.")

    target, slug = resolve_target(book, story_id=story_id, chapter_number=chapter_number)
    image_path = target_image_path(target)
    snapshot(image_path, slug)

    method = (
        Image.Transpose.FLIP_LEFT_RIGHT if axis == "horizontal"
        else Image.Transpose.FLIP_TOP_BOTTOM
    )
    with Image.open(image_path) as src:
        img = src.convert("RGB").transpose(method)
    img.save(image_path)

    _finalize(image_path)
    return _result(image_path, slug, f"flip-{axis}")


# ---------------------------------------------------------------------------
# AI edit
# ---------------------------------------------------------------------------

def ai_edit_image(
    book: StoryBook,
    instruction: str,
    *,
    story_id: str = "",
    chapter_number: int = 0,
    size: str = "auto",
    quality: str = "high",
) -> ImageEditResult:
    """Natural-language image edit through OpenAI images.edit."""
    instruction = (instruction or "").strip()
    if not instruction:
        raise StoryError("An image edit needs an instruction.")
    if size not in VALID_EDIT_SIZES:
        raise StoryError(f"size must be one of {sorted(VALID_EDIT_SIZES)}.")

    import openclaw_image_maker as image_maker

    api_key = image_maker.resolve_api_key(book.config.openai_api_key)
    if not api_key:
        raise StoryError("No OpenAI API key is configured for image editing.")

    target, slug = resolve_target(book, story_id=story_id, chapter_number=chapter_number)
    image_path = target_image_path(target)
    snapshot(image_path, slug)

    prompt = instruction + IMAGE_CONSTRAINTS

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise StoryError("The openai package is not installed.") from exc

    client = OpenAI(api_key=api_key)
    try:
        with open(image_path, "rb") as fh:
            result = client.images.edit(
                model=book.config.image_model,
                image=fh,
                prompt=prompt,
                size=size,
                quality=quality,
            )
    except Exception as exc:  # noqa: BLE001 - surface the upstream message
        raise StoryError(f"The image edit failed: {exc}") from exc

    payload = getattr(result, "data", None)
    if not payload:
        raise StoryError("The image service returned no image.")

    b64 = getattr(payload[0], "b64_json", None)
    if not b64:
        raise StoryError("The image service returned no image data.")

    from PIL import Image
    with Image.open(io.BytesIO(base64.b64decode(b64))) as img:
        img.convert("RGB").save(image_path)

    _finalize(image_path)
    target.illustration_prompt = f"{target.illustration_prompt}\n\nEdit: {instruction}".strip()
    return _result(image_path, slug, "ai-edit")
