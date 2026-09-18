"""Images must be 300 DPI at the size they are actually printed.

A DPI tag is only a label. ``sanitize_for_print`` used to write "300 DPI" onto
whatever pixels it was given, so 1024px art placed 5.5in wide shipped tagged as
300 DPI while really printing at 186. KDP measures pixels against printed size,
so the tag alone never satisfied it.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from PIL import Image

import print_hygiene as ph
from print_hygiene import (
    PRINT_DPI,
    audit_image,
    effective_dpi,
    required_pixels,
    sanitize_for_print,
    upscale_for_print,
)


def _art(path: Path, w: int, h: int, mode: str = "RGB") -> Path:
    Image.new(mode, (w, h), "white" if mode != "RGBA" else (255, 255, 255, 0)).save(path)
    return path


class TestRequiredPixels:
    @pytest.mark.parametrize("width_in,expected", [
        (4.5, 1350), (5.5, 1650), (6.0, 1800), (3.4, 1020),
    ])
    def test_width_requirement(self, width_in, expected):
        assert required_pixels(width_in)[0] == expected

    def test_full_page_six_by_nine(self):
        assert ph.FULL_PAGE_PX == (1800, 2700)


class TestEffectiveDpi:
    def test_reports_the_real_printed_resolution(self, tmp_path):
        """The exact case from the client's screenshot."""
        p = _art(tmp_path / "a.png", 1024, 1536)
        assert round(effective_dpi(p, 5.5)) == 186

    def test_1024_at_four_and_a_half_inches(self, tmp_path):
        p = _art(tmp_path / "b.png", 1024, 1024)
        assert round(effective_dpi(p, 4.5)) == 228

    def test_in_house_grid_already_passes(self, tmp_path):
        p = _art(tmp_path / "c.png", 1800, 2700)
        assert round(effective_dpi(p, 6.0)) == 300


class TestAuditCatchesUnderResolution:
    def test_flags_art_that_is_too_small(self, tmp_path):
        p = _art(tmp_path / "a.png", 1024, 1536)
        sanitize_for_print(p)  # tag says 300 DPI
        problems = audit_image(p, width_in=5.5)
        assert problems, "a 186 DPI image must not pass the print check"
        assert "186" in problems[0]

    def test_tagged_300_is_not_enough(self, tmp_path):
        """Regression: the tag alone used to make this pass."""
        p = _art(tmp_path / "a.png", 1024, 1536)
        sanitize_for_print(p)
        with Image.open(p) as im:
            assert round(im.info["dpi"][0], 2) == 300.0  # tag is fine
        assert audit_image(p, width_in=5.5)              # reality is not

    def test_large_enough_art_passes(self, tmp_path):
        p = _art(tmp_path / "b.png", 1800, 2700)
        sanitize_for_print(p)
        assert audit_image(p, width_in=6.0) == []

    def test_no_width_means_no_resolution_check(self, tmp_path):
        p = _art(tmp_path / "c.png", 100, 100)
        sanitize_for_print(p)
        assert audit_image(p) == []


class TestUpscaling:
    def test_upscales_to_meet_300_dpi(self, tmp_path):
        p = _art(tmp_path / "a.png", 1024, 1536)
        assert upscale_for_print(p, 5.5, 8.25) is True
        with Image.open(p) as im:
            assert im.size == (1650, 2475)
        assert round(effective_dpi(p, 5.5)) == 300

    def test_never_downscales(self, tmp_path):
        """Extra pixels are harmless; discarding them is irreversible."""
        p = _art(tmp_path / "b.png", 1800, 2700)
        assert upscale_for_print(p, 4.5) is False
        with Image.open(p) as im:
            assert im.size == (1800, 2700)

    def test_preserves_aspect_ratio(self, tmp_path):
        p = _art(tmp_path / "c.png", 1024, 1536)
        before = 1024 / 1536
        upscale_for_print(p, 6.0)
        with Image.open(p) as im:
            assert im.size[0] / im.size[1] == pytest.approx(before, rel=1e-3)

    def test_dpi_tag_survives_the_upscale(self, tmp_path):
        p = _art(tmp_path / "d.png", 1024, 1536)
        upscale_for_print(p, 5.5)
        with Image.open(p) as im:
            assert round(im.info["dpi"][0], 2) == 300.0

    def test_is_idempotent(self, tmp_path):
        p = _art(tmp_path / "e.png", 1024, 1536)
        upscale_for_print(p, 4.5)
        with Image.open(p) as im:
            size_after_first = im.size
        assert upscale_for_print(p, 4.5) is False
        with Image.open(p) as im:
            assert im.size == size_after_first

    def test_handles_transparency(self, tmp_path):
        p = _art(tmp_path / "f.png", 1024, 1024, mode="RGBA")
        assert upscale_for_print(p, 4.5) is True
        with Image.open(p) as im:
            assert im.size[0] >= 1350


class TestSanitizeAppliesResolution:
    def test_width_argument_upscales(self, tmp_path):
        p = _art(tmp_path / "a.png", 1024, 1536)
        sanitize_for_print(p, PRINT_DPI, width_in=4.5)
        assert round(effective_dpi(p, 4.5)) >= 300
        assert audit_image(p, width_in=4.5) == []

    def test_metadata_still_stripped_after_upscale(self, tmp_path):
        p = _art(tmp_path / "b.png", 1024, 1536)
        sanitize_for_print(p, PRINT_DPI, width_in=4.5)
        raw = p.read_bytes()
        for marker in (b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"caBX"):
            assert marker not in raw


class TestExportersEmbedHighResArt:
    """The real guarantee: what ends up inside the .docx is 300 DPI."""

    def test_trivia_embeds_upscaled_art(self, tmp_path):
        from trivia import export as ex
        from trivia.engine import (
            BookConfig, Chapter, ChapterConfig, DidYouKnowFact,
            TriviaBook, TriviaQuestion,
        )
        art = _art(tmp_path / "chap.png", 1024, 1536)
        cfg = BookConfig(
            book_title="T", topic="birds",
            chapters=[ChapterConfig.from_dict(
                {"chapter_title": "Owls", "chapter_scope": "owls"}, 1)],
        )
        book = TriviaBook(config=cfg, chapters=[Chapter(
            1, "Owls", "owls",
            trivia=[TriviaQuestion("q", 1, "Q?",
                                   {"A": "a", "B": "b", "C": "c", "D": "d"}, "B")],
            facts=[DidYouKnowFact("f", 1, "A fact.")],
            illustration_path=str(art),
        )])
        docx = tmp_path / "book.docx"
        ex.build_docx(book, docx, image_width_in=4.5)

        need = required_pixels(4.5)[0]
        with zipfile.ZipFile(docx) as z:
            media = [n for n in z.namelist() if n.startswith("word/media/")]
            assert media, "no image embedded"
            for name in media:
                out = tmp_path / Path(name).name
                out.write_bytes(z.read(name))
                with Image.open(out) as im:
                    assert im.size[0] >= need, (
                        f"{name} is {im.size[0]}px, needs {need}px for 300 DPI"
                    )


# -- the DOCX writer's own embed path --------------------------------------

def test_docx_writer_upscales_to_its_placed_width(tmp_path):
    """1024px art placed 5.5in wide prints at 186 DPI, not 300.

    ``prepare_image_for_print`` used to stamp the tag without the placed width,
    so the file claimed 300 DPI while KDP measured 186 and warned on every page.
    """
    import openclaw_docx_writer as w

    art = _art(tmp_path / "art.png", 1024, 1536)
    assert round(ph.effective_dpi(art, 5.5)) == 186

    w.prepare_image_for_print(art, width_inches=5.5)

    assert round(ph.effective_dpi(art, 5.5)) >= 300
    assert ph.audit_image(art, width_in=5.5) == []
    # 2:3 art on a 6x9 page must not be stretched to fit.
    with Image.open(art) as im:
        assert abs((im.size[1] / im.size[0]) - 1.5) < 0.01


def test_tree_audit_measures_pixels_not_just_the_tag(tmp_path):
    """A tag-only audit passes an under-sized image, which is the whole bug."""
    art = _art(tmp_path / "art.png", 1024, 1536)
    ph.sanitize_for_print(art)  # tags it 300 DPI, leaves the pixels alone

    assert ph.audit_tree(tmp_path) == {}, "tag-only audit should see nothing wrong"
    assert ph.audit_tree(tmp_path, width_in=5.5), "placed-size audit must flag it"


def test_tree_walks_skip_the_pristine_sidecar(tmp_path):
    """The pre-upscale backup never reaches the book, so auditing it would
    report a failure for every image that was correctly upscaled."""
    art = _art(tmp_path / "art.png", 1024, 1536)
    ph.sanitize_for_print(art, width_in=5.5)

    sidecars = [p for p in tmp_path.iterdir() if ph.is_original_sidecar(p)]
    assert sidecars, "upscale should keep the original"
    assert ph.audit_tree(tmp_path, width_in=5.5) == {}
    assert all(not ph.is_original_sidecar(p) for p in ph.sanitize_tree(tmp_path))


# -- every pipeline's final print check -------------------------------------

class _Warned:
    """Stands in for a book: verify_print_images only needs ``warnings``."""
    def __init__(self):
        self.warnings: list[str] = []


@pytest.mark.parametrize("module_name, width_in", [
    ("trivia.export", 4.5),
    ("stories.export", 4.5),
    ("puzzle.export", 4.75),
])
def test_every_pipeline_audit_measures_pixels(tmp_path, module_name, width_in):
    """A tag-only audit passed under-resolution art in all three pipelines."""
    import importlib

    module = importlib.import_module(module_name)
    art = _art(tmp_path / "art.png", 1024, 1536)
    ph.sanitize_for_print(art)  # tag claims 300 DPI; the pixels do not back it

    book = _Warned()
    problems = module.verify_print_images(book, tmp_path)

    assert problems, f"{module_name} must flag art that prints under 300 DPI"
    assert "effective" in problems[0]
    assert book.warnings, "the problem has to reach the build log"


def test_pipeline_audit_passes_correctly_sized_art(tmp_path):
    """The check must not cry wolf on art that is genuinely 300 DPI."""
    import trivia.export as tex

    art = _art(tmp_path / "art.png", 1350, 2025)
    ph.sanitize_for_print(art, width_in=4.5)
    assert tex.verify_print_images(_Warned(), tmp_path) == []


def test_strip_ai_docx_repairs_resolution_at_the_placed_width(tmp_path):
    """The book writer's Strip AI pass reads each image's real placed width
    from the drawing XML, so it can fix resolution rather than only re-tag."""
    from docx import Document
    from docx.shared import Inches

    art = _art(tmp_path / "art.png", 1024, 1536)
    doc = Document()
    doc.add_picture(str(art), width=Inches(5.5))
    docx_path = tmp_path / "book.docx"
    doc.save(str(docx_path))

    flagged = ph.strip_ai_docx(docx_path)
    assert flagged["images_flagged"] == 1
    assert "186 DPI" in flagged["image_details"][0]["problems"][0]

    ph.strip_ai_docx(docx_path, apply=True)
    assert ph.strip_ai_docx(docx_path)["images_flagged"] == 0

    with zipfile.ZipFile(docx_path) as z:
        name = next(n for n in z.namelist() if n.startswith("word/media/"))
        out = tmp_path / "embedded.png"
        out.write_bytes(z.read(name))
    assert round(ph.effective_dpi(out, 5.5)) >= 300
