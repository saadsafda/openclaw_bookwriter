"""Chapter illustration editing for trivia books.

Two kinds of edit:

  * AI edits  — OpenAI images.edit, optionally masked, so a specific region can
    be changed while the rest of the picture is preserved.
  * Local edits — deterministic Pillow operations (crop, rotate, brightness,
    contrast, saturation, sharpness) that cost nothing and are instant.

Every edit snapshots the previous file into a per-chapter history folder, so
any change can be undone. Section 9's constraints (no humans, no text) are
re-appended to every AI prompt here, exactly as at generation time — an
operator instruction cannot opt out of them.
"""

from __future__ import annotations

import base64
import io
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .engine import TriviaBook, TriviaError

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

SECTION9_CONSTRAINTS = (
    " Keep the result in strictly neutral grayscale: only deep black and a few "
    "distinct shades of cool gray on a pure white background. No color of any "
    "kind. Absolutely no people and no human figures. Absolutely no text, "
    "letters, numbers, words, labels, captions, signatures, watermarks, "
    "frames, or borders anywhere in the image."
)


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
# Paths and history
# ---------------------------------------------------------------------------

def chapter_image_path(book: TriviaBook, chapter_number: int) -> Path:
    from .edit import find_chapter

    ch = find_chapter(book, chapter_number)
    if ch is None:
        raise TriviaError(f"Chapter {chapter_number} not found.")
    if not ch.illustration_path:
        raise TriviaError(f"Chapter {chapter_number} has no illustration yet.")
    path = Path(ch.illustration_path)
    if not path.exists():
        raise TriviaError("The illustration file is missing from disk.")
    return path


def _history_dir(image_path: Path, chapter_number: int) -> Path:
    d = image_path.parent / "history" / f"ch{chapter_number}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot(image_path: Path, chapter_number: int) -> Path:
    """Copy the current image into history before it is overwritten."""
    hist = _history_dir(image_path, chapter_number)
    stamp = f"{time.time():.6f}".replace(".", "")
    target = hist / f"{stamp}{image_path.suffix}"
    shutil.copy2(image_path, target)

    # Trim oldest beyond the cap so history can't grow without bound.
    snaps = sorted(p for p in hist.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    for old in snaps[:-MAX_HISTORY]:
        old.unlink(missing_ok=True)
    return target


def history_list(image_path: Path, chapter_number: int) -> list[Path]:
    hist = _history_dir(image_path, chapter_number)
    return sorted(
        (p for p in hist.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES),
        reverse=True,
    )


def undo(book: TriviaBook, chapter_number: int) -> ImageEditResult:
    """Restore the most recent snapshot, putting the current image back on the
    stack so undo is itself reversible."""
    image_path = chapter_image_path(book, chapter_number)
    snaps = history_list(image_path, chapter_number)
    if not snaps:
        raise TriviaError("There is nothing to undo for this chapter.")

    latest = snaps[0]
    # Park the current state as a redo point, then restore the snapshot.
    hist = _history_dir(image_path, chapter_number)
    redo_stamp = f"{time.time():.6f}".replace(".", "")
    redo_point = hist / f"{redo_stamp}{image_path.suffix}"
    shutil.copy2(image_path, redo_point)

    shutil.copy2(latest, image_path)
    latest.unlink(missing_ok=True)

    _finalize(image_path)
    return ImageEditResult(
        path=image_path,
        history_count=len(history_list(image_path, chapter_number)),
        operation="undo",
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


# ---------------------------------------------------------------------------
# Local (Pillow) edits — free and instant
# ---------------------------------------------------------------------------

def adjust_image(
    book: TriviaBook,
    chapter_number: int,
    *,
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
            raise TriviaError(f"{name} must be between {lo} and {hi}.")

    image_path = chapter_image_path(book, chapter_number)
    snapshot(image_path, chapter_number)

    with Image.open(image_path) as src:
        img = src.convert("RGB")
    for enhancer_cls, value in (
        (ImageEnhance.Brightness, brightness),
        (ImageEnhance.Contrast, contrast),
        (ImageEnhance.Color, saturation),
        (ImageEnhance.Sharpness, sharpness),
    ):
        if abs(value - 1.0) > 1e-6:
            img = enhancer_cls(img).enhance(value)

    img.save(image_path)
    _finalize(image_path)
    return ImageEditResult(
        path=image_path,
        history_count=len(history_list(image_path, chapter_number)),
        operation="adjust",
    )


def crop_image(
    book: TriviaBook,
    chapter_number: int,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> ImageEditResult:
    """Crop using fractional coordinates (0-1), so the client doesn't need to
    know the pixel dimensions."""
    from PIL import Image

    for name, v in (("left", left), ("top", top), ("right", right), ("bottom", bottom)):
        if not (0.0 <= v <= 1.0):
            raise TriviaError(f"{name} must be between 0 and 1.")
    if right - left < 0.05 or bottom - top < 0.05:
        raise TriviaError("The crop region is too small.")

    image_path = chapter_image_path(book, chapter_number)
    snapshot(image_path, chapter_number)

    with Image.open(image_path) as src:
        img = src.convert("RGB")
        w, h = img.size
        box = (int(left * w), int(top * h), int(right * w), int(bottom * h))
        img = img.crop(box)

    img.save(image_path)
    _finalize(image_path)
    return ImageEditResult(
        path=image_path,
        history_count=len(history_list(image_path, chapter_number)),
        operation="crop",
    )


def rotate_image(book: TriviaBook, chapter_number: int, degrees: int) -> ImageEditResult:
    from PIL import Image

    if degrees not in (90, 180, 270, -90):
        raise TriviaError("Rotation must be 90, 180 or 270 degrees.")

    image_path = chapter_image_path(book, chapter_number)
    snapshot(image_path, chapter_number)

    with Image.open(image_path) as src:
        img = src.convert("RGB").rotate(-degrees, expand=True)

    img.save(image_path)
    _finalize(image_path)
    return ImageEditResult(
        path=image_path,
        history_count=len(history_list(image_path, chapter_number)),
        operation="rotate",
    )


def flip_image(book: TriviaBook, chapter_number: int, axis: str) -> ImageEditResult:
    from PIL import Image, ImageOps

    if axis not in {"horizontal", "vertical"}:
        raise TriviaError("axis must be 'horizontal' or 'vertical'.")

    image_path = chapter_image_path(book, chapter_number)
    snapshot(image_path, chapter_number)

    with Image.open(image_path) as src:
        img = src.convert("RGB")
        img = ImageOps.mirror(img) if axis == "horizontal" else ImageOps.flip(img)

    img.save(image_path)
    _finalize(image_path)
    return ImageEditResult(
        path=image_path,
        history_count=len(history_list(image_path, chapter_number)),
        operation="flip",
    )


# ---------------------------------------------------------------------------
# AI edits — OpenAI images.edit
# ---------------------------------------------------------------------------

def _decode_mask(mask_data_url: str, size: tuple[int, int]) -> bytes:
    """Turn a client-drawn mask into the RGBA PNG the API expects.

    OpenAI treats *transparent* pixels as the region to regenerate. The client
    paints the area to change in opaque white on a transparent canvas, so the
    alpha channel must be inverted here.
    """
    from PIL import Image

    if "," in mask_data_url:
        mask_data_url = mask_data_url.split(",", 1)[1]
    try:
        raw = base64.b64decode(mask_data_url)
    except Exception as exc:
        raise TriviaError(f"Could not decode the mask image: {exc}") from exc

    with Image.open(io.BytesIO(raw)) as m:
        mask = m.convert("RGBA")
    if mask.size != size:
        mask = mask.resize(size, Image.LANCZOS)

    # Painted (alpha > 0) becomes transparent -> the API edits there.
    alpha = mask.getchannel("A").point(lambda a: 0 if a > 8 else 255)
    out = Image.new("RGBA", size, (0, 0, 0, 255))
    out.putalpha(alpha)

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def ai_edit_image(
    book: TriviaBook,
    chapter_number: int,
    instruction: str,
    *,
    mask_data_url: str = "",
    size: str = "auto",
    quality: str = "high",
) -> ImageEditResult:
    """Edit an existing illustration with a natural-language instruction.

    With a mask, only the painted region changes. Without one, the whole image
    is reinterpreted against the instruction.
    """
    from PIL import Image

    instruction = (instruction or "").strip()
    if not instruction:
        raise TriviaError("Describe the change you want to make.")
    if size not in VALID_EDIT_SIZES:
        raise TriviaError(f"size must be one of {sorted(VALID_EDIT_SIZES)}.")

    import openclaw_image_maker as image_maker

    cfg = book.config
    api_key = image_maker.resolve_api_key(cfg.openai_api_key)
    if not api_key:
        raise TriviaError("No OpenAI API key available for image editing.")

    image_path = chapter_image_path(book, chapter_number)

    # images.edit requires PNG input; convert if the current file is not.
    with Image.open(image_path) as src:
        rgba = src.convert("RGBA")
        dimensions = rgba.size
        png_buf = io.BytesIO()
        rgba.save(png_buf, format="PNG")
    png_bytes = png_buf.getvalue()

    prompt = instruction + SECTION9_CONSTRAINTS

    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    kwargs: dict[str, Any] = {
        "model": "gpt-image-1",
        "image": ("illustration.png", png_bytes, "image/png"),
        "prompt": prompt,
        "n": 1,
        "size": size,
        "quality": quality,
    }
    if mask_data_url:
        kwargs["mask"] = (
            "mask.png", _decode_mask(mask_data_url, dimensions), "image/png",
        )

    try:
        response = client.images.edit(**kwargs)
    except Exception as exc:
        raise TriviaError(f"Image edit failed: {exc}") from exc

    first = response.data[0] if response.data else None
    b64 = getattr(first, "b64_json", None)
    if not b64 and isinstance(first, dict):
        b64 = first.get("b64_json")
    if not b64:
        raise TriviaError("The image service returned no image data.")

    snapshot(image_path, chapter_number)
    image_path.write_bytes(base64.b64decode(b64))
    _finalize(image_path)

    # Record what was asked, alongside the generation prompt already stored.
    from .edit import find_chapter
    ch = find_chapter(book, chapter_number)
    if ch is not None:
        ch.illustration_prompt = f"{ch.illustration_prompt} | EDIT: {instruction}"

    return ImageEditResult(
        path=image_path,
        history_count=len(history_list(image_path, chapter_number)),
        operation="ai_edit",
    )


def image_info(book: TriviaBook, chapter_number: int) -> dict[str, Any]:
    """Dimensions and history depth, for the editor UI."""
    from PIL import Image

    try:
        image_path = chapter_image_path(book, chapter_number)
    except TriviaError:
        return {"has_image": False}

    with Image.open(image_path) as img:
        w, h = img.size
        dpi = img.info.get("dpi", (72, 72))

    return {
        "has_image": True,
        "width": w,
        "height": h,
        "dpi": int(dpi[0]) if dpi else 72,
        "history_count": len(history_list(image_path, chapter_number)),
        "filename": image_path.name,
    }
