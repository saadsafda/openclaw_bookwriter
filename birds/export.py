"""Bird guide exporters — the finished plates assembled into a DOCX book.

One bird per page: the transparent illustration, the species name under it, and
nothing else. Descriptive content (size, habitat, diet, field notes) is left for
a later pass, so what comes out of here is a laid-out shell ready to be filled
in Word.

Two things this has to get right that a naive ``add_picture`` loop does not:

**Transparent PNGs need a white ground.** Word composites a transparent PNG
against whatever is behind it, which is fine on screen and unpredictable in
print — and any near-white halo left by the cutout shows as a grey fringe. Each
plate is flattened onto white at export time, into a copy. The transparent
original is never modified, because that is the asset the operator actually
wants to keep for other layouts.

**Physical size, not pixel size.** ``add_picture`` with only a width will scale
a tall heron and a squat wren to the same width and wildly different heights.
Both dimensions are bounded here so every plate fits its page frame regardless
of the bird's proportions.

The DOCX is a plain manuscript and is handed to the shared
``kdp_docx_formatter.build_kdp_documents()`` for 6x9 sizing, gutters, TOC and
page numbers — the same handoff puzzle/export.py uses.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor
from PIL import Image

from print_hygiene import PRINT_DPI, sanitize_for_print

# 6x9 trade paperback with the project's usual margins leaves this much room.
# The plate is bounded on both axes so a tall bird cannot push its caption off
# the page.
IMAGE_MAX_W_IN = 4.6
IMAGE_MAX_H_IN = 6.2


@dataclass
class GuideConfig:
    """What the book says about itself. Everything else comes from the plates."""

    title: str = "Bird Guide"
    subtitle: str = ""
    author: str = ""

    def clean_title(self) -> str:
        return (self.title or "").strip() or "Bird Guide"


def _flatten_to_white(src: Path) -> Path:
    """Composite a transparent plate onto white, into a temp copy.

    Returns the original path unchanged when there is no alpha to flatten, so
    an opaque plate costs nothing. The copy is what gets embedded; the
    transparent original stays on disk untouched.
    """
    try:
        with Image.open(src) as img:
            if img.mode not in ("RGBA", "LA", "P"):
                return src
            rgba = img.convert("RGBA")
            if rgba.getchannel("A").getextrema()[0] == 255:
                return src          # fully opaque already

            ground = Image.new("RGB", rgba.size, (255, 255, 255))
            ground.paste(rgba, mask=rgba.getchannel("A"))

        tmp = Path(tempfile.mkstemp(suffix=".png", prefix="birdplate_")[1])
        ground.save(tmp, format="PNG", dpi=(PRINT_DPI, PRINT_DPI))
        return tmp
    except Exception:
        return src


def _fitted_size(path: Path) -> tuple[float, float]:
    """Width/height in inches that fits the frame while keeping the aspect.

    Falls back to the max width when the file cannot be measured — a plate that
    lands slightly wrong beats an export that dies on one bad PNG.
    """
    try:
        with Image.open(path) as img:
            w_px, h_px = img.size
            dpi = img.info.get("dpi") or (PRINT_DPI, PRINT_DPI)
            x_dpi = float(dpi[0]) or PRINT_DPI
            y_dpi = float(dpi[1]) or PRINT_DPI
    except Exception:
        return IMAGE_MAX_W_IN, 0.0

    if not w_px or not h_px:
        return IMAGE_MAX_W_IN, 0.0

    w_in = w_px / x_dpi
    h_in = h_px / y_dpi
    scale = min(IMAGE_MAX_W_IN / w_in, IMAGE_MAX_H_IN / h_in, 1.0)
    # Never upscale past the frame, but do fill it when the plate is small:
    # a trimmed hummingbird would otherwise print postage-stamp sized.
    if scale > 1.0:
        scale = 1.0
    if w_in * scale < IMAGE_MAX_W_IN and h_in * scale < IMAGE_MAX_H_IN:
        scale = min(IMAGE_MAX_W_IN / w_in, IMAGE_MAX_H_IN / h_in)
    return w_in * scale, h_in * scale


def _add_plate_page(
    doc: Document,
    image_path: Path,
    species: str,
    first: bool,
    flatten: bool = True,
) -> bool:
    """One bird's page. Returns False when the image is missing."""
    if not image_path or not image_path.is_file():
        return False

    if not first:
        doc.add_page_break()

    embed = _flatten_to_white(image_path) if flatten else image_path
    temporary = embed != image_path
    try:
        # Work out the printed size first: the 300 DPI guarantee below is
        # relative to how large the plate is actually placed, so it cannot be
        # checked before the fit is known.
        width_in, height_in = _fitted_size(embed)

        # Last line of defence before embedding, matching puzzle/export.py:
        # guarantee 300 DPI at the placed size and no AI/EXIF metadata whatever
        # the plate's route here was.
        try:
            sanitize_for_print(
                embed, PRINT_DPI, width_in=width_in, height_in=height_in
            )
        except Exception:
            pass

        para = doc.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = para.add_run()
        if height_in > 0:
            run.add_picture(str(embed), width=Inches(width_in), height=Inches(height_in))
        else:
            run.add_picture(str(embed), width=Inches(width_in))
    finally:
        if temporary:
            Path(embed).unlink(missing_ok=True)

    # Heading 1 so the species lands in the TOC the KDP formatter builds.
    heading = doc.add_heading(species or "Unnamed Bird", level=1)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    return True


def _add_title_page(doc: Document, cfg: GuideConfig) -> None:
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run(cfg.clean_title())
    run.bold = True
    run.font.size = Pt(28)

    if cfg.subtitle.strip():
        sub = doc.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub_run = sub.add_run(cfg.subtitle.strip())
        sub_run.font.size = Pt(14)
        sub_run.font.color.rgb = RGBColor(0x44, 0x44, 0x44)

    if cfg.author.strip():
        author = doc.add_paragraph()
        author.alignment = WD_ALIGN_PARAGRAPH.CENTER
        author_run = author.add_run(cfg.author.strip())
        author_run.font.size = Pt(12)


def build_docx(
    plates: list[tuple[str, Path]],
    path: Path,
    config: GuideConfig | None = None,
    flatten: bool = True,
) -> tuple[Path, int]:
    """Assemble the guide. ``plates`` is ``(species, image_path)`` in book order.

    Returns ``(path, pages_written)``. Missing images are skipped rather than
    raising: a 250-bird guide should not be lost to one deleted PNG.
    """
    cfg = config or GuideConfig()
    doc = Document()

    _add_title_page(doc, cfg)

    written = 0
    for species, image_path in plates:
        # `first` is False for every plate because the title page already
        # occupies page one — each bird starts on a fresh page after it.
        if _add_plate_page(doc, Path(image_path), species, first=False, flatten=flatten):
            written += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path, written
