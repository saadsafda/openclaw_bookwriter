"""Paperback output must use mirrored margins with the gutter at the spine.

KDP prints and binds double-sided. Without ``w:mirrorMargins`` the wide inside
margin stays on the left of *every* page, so on a left-hand (verso) page it
lands on the outer edge and the narrow outside margin ends up against the
spine — text runs into the binding. This is the difference between a
default-margin file and a professionally set book.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest
from docx import Document

from kdp_docx_formatter import (
    PAPERBACK_OUTSIDE_MARGIN_IN,
    _inside_margin_for_page_count,
    build_kdp_documents,
)

# KDP's published minimum inside margin (gutter) by page count.
KDP_MIN_INSIDE = [(24, 0.375), (150, 0.375), (151, 0.5), (300, 0.5),
                  (301, 0.625), (500, 0.625), (501, 0.75), (700, 0.75), (701, 0.875)]
KDP_MIN_OUTSIDE = 0.25


@pytest.fixture(scope="module")
def manuscript(tmp_path_factory) -> Path:
    """A minimal but realistic manuscript with headings and body text."""
    out = tmp_path_factory.mktemp("kdp_margins")
    doc = Document()
    doc.add_heading("Chapter 1 — Beginnings", level=1)
    for i in range(40):
        doc.add_paragraph(
            f"Body paragraph {i} with enough words in it to occupy a "
            "reasonable amount of a printed line on a six by nine page."
        )
    doc.add_heading("Chapter 2 — Endings", level=1)
    for i in range(40):
        doc.add_paragraph(f"Second chapter paragraph {i} carrying on the text.")
    path = out / "src.docx"
    doc.save(str(path))
    return path


def _build(manuscript: Path, pages: int):
    out = manuscript.parent
    k = out / f"m{pages}_kindle.docx"
    p = out / f"m{pages}_paperback.docx"
    build_kdp_documents(
        source_docx=manuscript, kindle_output=k, paperback_output=p,
        estimated_pages=pages, title_placeholder="T", author_placeholder="A",
        outline_topics=set(),
    )
    return k, p


def _settings_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("word/settings.xml").decode()


class TestMirroredMargins:
    def test_paperback_declares_mirror_margins(self, manuscript):
        _, pb = _build(manuscript, 200)
        assert re.search(r"<w:mirrorMargins\b", _settings_xml(pb)), \
            "paperback must set w:mirrorMargins so the gutter follows the spine"

    def test_mirror_margins_is_not_switched_off(self, manuscript):
        """A w:val of 0/false would silently disable the toggle."""
        _, pb = _build(manuscript, 200)
        m = re.search(r"<w:mirrorMargins([^/>]*)/?>", _settings_xml(pb))
        assert m is not None
        assert not re.search(r'w:val="(0|false)"', m.group(1))

    def test_kindle_does_not_mirror(self, manuscript):
        """Reflowable ebooks have no spine; mirroring there is meaningless."""
        k, _ = _build(manuscript, 200)
        assert not re.search(r"<w:mirrorMargins\b", _settings_xml(k))

    def test_kindle_margins_stay_symmetric(self, manuscript):
        k, _ = _build(manuscript, 200)
        for sec in Document(str(k)).sections:
            assert sec.left_margin.inches == sec.right_margin.inches

    @pytest.mark.parametrize("pages", [50, 200, 400, 600, 800])
    def test_every_section_is_mirrored_consistently(self, manuscript, pages):
        """A single unmirrored section would print with the wrong gutter."""
        _, pb = _build(manuscript, pages)
        doc = Document(str(pb))
        inside = _inside_margin_for_page_count(pages)
        # Word stores margins in twips, so a target that isn't a whole number of
        # twips (1.14" = 1641.6) comes back rounded. Compare at that resolution.
        for i, sec in enumerate(doc.sections):
            assert sec.left_margin.inches == pytest.approx(inside, abs=1 / 1440), f"section {i}"
            assert sec.right_margin.inches == pytest.approx(PAPERBACK_OUTSIDE_MARGIN_IN, abs=1 / 1440)

    @pytest.mark.parametrize("pages,min_inside", KDP_MIN_INSIDE)
    def test_inside_margin_meets_kdp_minimum(self, pages, min_inside):
        assert _inside_margin_for_page_count(pages) >= min_inside

    def test_outside_margin_clears_kdp_minimum(self):
        """At exactly 0.25in, print tolerance can trim visible text."""
        assert PAPERBACK_OUTSIDE_MARGIN_IN >= KDP_MIN_OUTSIDE

    @pytest.mark.parametrize("pages", [50, 200, 400, 600])
    def test_text_block_fits_the_page(self, manuscript, pages):
        _, pb = _build(manuscript, pages)
        for sec in Document(str(pb)).sections:
            used = sec.left_margin.inches + sec.right_margin.inches
            assert used < sec.page_width.inches
            assert sec.top_margin.inches + sec.bottom_margin.inches < sec.page_height.inches

    def test_gutter_attribute_does_not_double_count(self, manuscript):
        """w:gutter adds to the inside margin; the inside margin already has it."""
        _, pb = _build(manuscript, 400)
        with zipfile.ZipFile(pb) as z:
            body = z.read("word/document.xml").decode()
        for g in re.findall(r'w:gutter="(\d+)"', body):
            assert int(g) == 0

    def test_trim_size_is_six_by_nine(self, manuscript):
        _, pb = _build(manuscript, 200)
        for sec in Document(str(pb)).sections:
            assert sec.page_width.inches == pytest.approx(6.0)
            assert sec.page_height.inches == pytest.approx(9.0)

    def test_gutter_widens_with_page_count(self):
        """A thicker book needs more spine allowance."""
        widths = [_inside_margin_for_page_count(n) for n in (100, 200, 400, 600, 800)]
        assert widths == sorted(widths)
        assert widths[0] < widths[-1]

    @pytest.mark.parametrize("pages", [24, 100, 200, 400, 800])
    def test_inside_margin_carries_a_real_binding_allowance(self, pages):
        """The inside margin must exceed the outside, or there is no gutter.

        Meeting KDP's inside minimum is not enough: at the short-book tier that
        minimum equals the outside margin, which mirrors a zero-width gutter and
        prints text hard against the spine.
        """
        inside = _inside_margin_for_page_count(pages)
        assert inside > PAPERBACK_OUTSIDE_MARGIN_IN
        assert inside - PAPERBACK_OUTSIDE_MARGIN_IN >= 0.5


class TestImagesFitTextColumn:
    """Images are sized before the margins are applied, so the width they are
    capped at must track the gutter rather than a hardcoded constant."""

    @pytest.fixture(scope="class")
    def illustrated(self, tmp_path_factory) -> Path:
        from PIL import Image
        out = tmp_path_factory.mktemp("kdp_images")
        img = out / "plate.png"
        Image.new("RGB", (2400, 1800), "white").save(img)
        doc = Document()
        doc.add_heading("Chapter 1", level=1)
        for i in range(20):
            doc.add_paragraph(f"Paragraph {i} of body text before the plate.")
        doc.add_picture(str(img))
        doc.add_paragraph("Text after the plate.")
        path = out / "src.docx"
        doc.save(str(path))
        return path

    @pytest.mark.parametrize("pages", [24, 200, 800])
    def test_no_image_exceeds_the_text_column(self, illustrated, pages):
        _, pb = _build(illustrated, pages)
        doc = Document(str(pb))
        for sec in doc.sections:
            # Subtracting Length objects yields plain EMUs, not a Length.
            column = (sec.page_width - sec.left_margin - sec.right_margin) / 914400
            for shape in doc.inline_shapes:
                assert shape.width.inches <= column + 1 / 1440
