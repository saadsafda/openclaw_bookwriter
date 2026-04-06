from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docx.text.paragraph import Paragraph

CHAPTER_RE = re.compile(r"^Chapter\s+\d+(?:\s*[:\-–—]\s*.*)?$", re.IGNORECASE)
CHAPTER_LABEL_RE = re.compile(r"^CHAPTER\s+\d+$", re.IGNORECASE)
FRONT_BACK_RE = re.compile(r"^(Introduction|Conclusion|Epilogue|Foreword|Preface|Prologue)\b", re.IGNORECASE)
SUBHEADING_RE = re.compile(r"^(\d+\.\s+.+|-\s+.+|Focus:\s+.+)$", re.IGNORECASE)

# ── Smart heading identification patterns ──────────────────────────────────
_NUMBER_WORDS = (
    r"One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|"
    r"Eleven|Twelve|Thirteen|Fourteen|Fifteen|Sixteen|Seventeen|"
    r"Eighteen|Nineteen|Twenty|Thirty|Forty|Fifty"
)
_NUM_OR_WORD = rf"(?:\d+|[IVXLCDM]+|{_NUMBER_WORDS})"

# "Chapter One", "Chapter Twenty-Two: Title"
CHAPTER_WORD_RE = re.compile(
    rf"^Chapter\s+{_NUM_OR_WORD}(?:\s*[:\-–—]\s*.*)?$", re.IGNORECASE
)
# "Part I", "Part 2: The Journey"
PART_RE = re.compile(
    rf"^Part\s+{_NUM_OR_WORD}(?:\s*[:\-–—]\s*.*)?$", re.IGNORECASE
)
# "Act I", "Act 3 — Climax"
ACT_RE = re.compile(
    rf"^Act\s+{_NUM_OR_WORD}(?:\s*[:\-–—]\s*.*)?$", re.IGNORECASE
)
# "Book One", "Book 2: Return"
BOOK_PART_RE = re.compile(
    rf"^Book\s+{_NUM_OR_WORD}(?:\s*[:\-–—]\s*.*)?$", re.IGNORECASE
)
# "Section 3", "Section 3: Details"
SECTION_RE = re.compile(
    r"^Section\s+\d+(?:\s*[:\-–—]\s*.*)?$", re.IGNORECASE
)
# Roman-numeral headings: "III. The Battle"
ROMAN_HEADING_RE = re.compile(r"^([IVXLCDM]{1,7})\.\s+.+$")
# Bare number as chapter title: "1: The Beginning" or "3 — The Start"
NUMBERED_TITLE_RE = re.compile(r"^\d{1,3}\s*[:\-–—]\s+.+$")
# Extended front/back matter keywords
FRONT_BACK_EXTENDED_RE = re.compile(
    r"^(Acknowledgments?|About\s+the\s+Author|Dedication|Bibliography|"
    r"Glossary|Append(?:ix|ices)|Afterword|Author'?s?\s+Note|"
    r"Note\s+to\s+(?:the\s+)?Reader|Dear\s+Reader|References?|"
    r"Disclaimer|Contents|Index|Notes|Summary|Overview|Backstory)\b",
    re.IGNORECASE,
)

_MAX_HEADING_WORDS = 12
_MAX_HEADING_CHARS = 80


def _is_all_bold(paragraph: Paragraph) -> bool:
    """True when every non-empty run in the paragraph is bold."""
    runs_with_text = [r for r in paragraph.runs if (r.text or "").strip()]
    if not runs_with_text:
        return False
    return all(r.bold for r in runs_with_text)


def _is_all_caps_text(text: str) -> bool:
    """True when all alphabetic characters are uppercase."""
    alpha = [c for c in text if c.isalpha()]
    if len(alpha) < 2:
        return False
    return all(c.isupper() for c in alpha)


def _largest_font_pt(paragraph: Paragraph) -> float:
    """Return the largest explicit font size (pt) found in the paragraph's runs."""
    mx = 0.0
    for r in paragraph.runs:
        if r.font.size:
            mx = max(mx, r.font.size.pt)
    return mx


def _looks_like_heading_by_format(paragraph: Paragraph, text: str) -> bool:
    """Heuristic: short, bold/large/ALL-CAPS text that is likely a heading."""
    words = text.split()
    if not (1 <= len(words) <= _MAX_HEADING_WORDS and len(text) <= _MAX_HEADING_CHARS):
        return False
    # Already a heading style? Let Word's style decide.
    style_name = (paragraph.style.name or "").strip().lower()
    if style_name.startswith("heading"):
        return True
    # Short ALL-CAPS bold line
    if _is_all_caps_text(text) and _is_all_bold(paragraph):
        return True
    # Short bold line with larger-than-body font (>= 14 pt)
    if _is_all_bold(paragraph) and _largest_font_pt(paragraph) >= 14:
        return True
    # Centered bold short line
    if (
        paragraph.alignment == WD_ALIGN_PARAGRAPH.CENTER
        and _is_all_bold(paragraph)
        and len(words) <= 8
    ):
        return True
    return False


@dataclass
class HeadingEntry:
    paragraph: Paragraph
    title: str
    bookmark: str


def paragraph_has_image(paragraph: Paragraph) -> bool:
    return bool(paragraph._p.xpath(".//w:drawing"))


def _ensure_run_font(run, font_name: str, size_pt: float) -> None:
    run.font.name = font_name
    run.font.size = Pt(size_pt)
    r_pr = run._r.get_or_add_rPr()
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.insert(0, r_fonts)
    r_fonts.set(qn("w:ascii"), font_name)
    r_fonts.set(qn("w:hAnsi"), font_name)
    r_fonts.set(qn("w:cs"), font_name)


def _apply_heading_one_run_style(run) -> None:
    _ensure_run_font(run, "Tahoma", 24)
    run.bold = True
    run.font.color.rgb = RGBColor(0, 0, 0)


def _apply_heading_one_paragraph_style(paragraph: Paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if paragraph.runs:
        for run in paragraph.runs:
            _apply_heading_one_run_style(run)
    else:
        run = paragraph.add_run("")
        _apply_heading_one_run_style(run)


def _apply_heading_two_run_style(run) -> None:
    _ensure_run_font(run, "Tahoma", 20)
    run.bold = True
    run.font.color.rgb = RGBColor(0, 0, 0)


def _apply_heading_two_paragraph_style(paragraph: Paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if paragraph.runs:
        for run in paragraph.runs:
            _apply_heading_two_run_style(run)
    else:
        run = paragraph.add_run("")
        _apply_heading_two_run_style(run)


def _set_section_vertical_alignment_top(section) -> None:
    sect_pr = section._sectPr
    v_align = sect_pr.find(qn("w:vAlign"))
    if v_align is None:
        v_align = OxmlElement("w:vAlign")
        sect_pr.append(v_align)
    v_align.set(qn("w:val"), "top")


def _set_section_vertical_alignment_center(section) -> None:
    sect_pr = section._sectPr
    v_align = sect_pr.find(qn("w:vAlign"))
    if v_align is None:
        v_align = OxmlElement("w:vAlign")
        sect_pr.append(v_align)
    v_align.set(qn("w:val"), "center")


def _set_page_number_start(section, start: int) -> None:
    sect_pr = section._sectPr
    pg_num_type = sect_pr.find(qn("w:pgNumType"))
    if pg_num_type is None:
        pg_num_type = OxmlElement("w:pgNumType")
        sect_pr.append(pg_num_type)
    pg_num_type.set(qn("w:start"), str(start))


def _add_section_break(paragraph: Paragraph, break_type: str = "nextPage") -> None:
    """Add a section break to a paragraph. break_type: 'nextPage' or 'oddPage'."""
    p_pr = paragraph._p.get_or_add_pPr()
    sect_pr = p_pr.find(qn("w:sectPr"))
    if sect_pr is None:
        sect_pr = OxmlElement("w:sectPr")
        p_pr.append(sect_pr)
    sect_type = sect_pr.find(qn("w:type"))
    if sect_type is None:
        sect_type = OxmlElement("w:type")
        sect_pr.append(sect_type)
    sect_type.set(qn("w:val"), break_type)


def _add_next_page_section_break(paragraph: Paragraph) -> None:
    _add_section_break(paragraph, "nextPage")


def _add_bookmark(paragraph: Paragraph, name: str, bookmark_id: int) -> None:
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), name)

    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))

    paragraph._p.insert(0, start)
    paragraph._p.append(end)


def _add_internal_hyperlink(paragraph: Paragraph, text: str, anchor: str) -> None:
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("w:anchor"), anchor)
    hyperlink.set(qn("w:history"), "1")

    run = OxmlElement("w:r")
    r_pr = OxmlElement("w:rPr")
    r_style = OxmlElement("w:rStyle")
    r_style.set(qn("w:val"), "Hyperlink")
    r_pr.append(r_style)
    run.append(r_pr)

    text_node = OxmlElement("w:t")
    text_node.text = text
    run.append(text_node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _insert_page_number_field(paragraph: Paragraph) -> None:
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")

    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "

    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")

    r1 = paragraph.add_run()
    r1._r.append(begin)
    r2 = paragraph.add_run()
    r2._r.append(instr)
    r3 = paragraph.add_run()
    r3._r.append(end)


def _insert_pageref_field(paragraph: Paragraph, bookmark_name: str) -> None:
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")

    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f" PAGEREF {bookmark_name} \\h "

    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")

    r1 = paragraph.add_run()
    r1._r.append(begin)
    r2 = paragraph.add_run()
    r2._r.append(instr)
    r3 = paragraph.add_run()
    r3._r.append(end)


def _insert_toc_field(paragraph: Paragraph, levels: str = "1-3") -> None:
    """Insert a native Word TOC field (Smart Identification style).

    Switches:
      \\o "1-3"  — include Heading 1 through 3
      \\h        — make entries clickable hyperlinks
      \\z        — hide tab leader and page numbers in Web Layout
      \\u        — use applied paragraph outline level
    """
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    begin.set(qn("w:dirty"), "true")

    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f' TOC \\o "{levels}" \\h \\z \\u '

    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")

    placeholder_text = OxmlElement("w:t")
    placeholder_text.text = "Update this field to see Table of Contents."

    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")

    r1 = paragraph.add_run()
    r1._r.append(begin)
    r2 = paragraph.add_run()
    r2._r.append(instr)
    r3 = paragraph.add_run()
    r3._r.append(separate)
    r4 = paragraph.add_run()
    r4._r.append(placeholder_text)
    r5 = paragraph.add_run()
    r5._r.append(end)


def _exclude_paragraph_from_toc(paragraph: Paragraph) -> None:
    """Set outline level to 'body text' so this heading won't appear in the TOC field."""
    p_pr = paragraph._p.get_or_add_pPr()
    outline_lvl = p_pr.find(qn("w:outlineLvl"))
    if outline_lvl is None:
        outline_lvl = OxmlElement("w:outlineLvl")
        p_pr.append(outline_lvl)
    # Level 9 = body text, excluded from TOC
    outline_lvl.set(qn("w:val"), "9")


def _enable_update_fields_on_open(doc: Document) -> None:
    """Tell Word to auto-update all fields (including TOC) when the file is opened."""
    settings_element = doc.settings.element
    update_fields = settings_element.find(qn("w:updateFields"))
    if update_fields is None:
        update_fields = OxmlElement("w:updateFields")
        settings_element.append(update_fields)
    update_fields.set(qn("w:val"), "true")


def _set_footer_page_number(footer, alignment: WD_ALIGN_PARAGRAPH) -> None:
    if footer.paragraphs:
        fp = footer.paragraphs[0]
        fp.clear()
    else:
        fp = footer.add_paragraph()
    fp.alignment = alignment
    _insert_page_number_field(fp)


def _estimate_page_count(doc: Document) -> int:
    words = 0
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            words += len(t.split())
    image_count = len(doc.inline_shapes)
    estimated = round(words / 280) + int(round(image_count * 0.35))
    return max(24, estimated)


def _inside_margin_for_page_count(page_count: int) -> float:
    if page_count <= 150:
        return 0.375
    if page_count <= 300:
        return 0.5
    if page_count <= 500:
        return 0.625
    if page_count <= 700:
        return 0.75
    return 0.875


def _normalize_heading_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _canonical_title_key(text: str) -> str:
    cleaned = _normalize_heading_text(text or "")
    cleaned = cleaned.strip(":-_ ")
    return cleaned.upper()


def _drop_duplicate_title_heading(headings: list[HeadingEntry], title_placeholder: str) -> list[HeadingEntry]:
    if not headings:
        return headings

    title_key = _canonical_title_key(title_placeholder)
    if not title_key:
        return headings

    first = headings[0]
    first_key = _canonical_title_key(first.title)
    if first_key != title_key:
        return headings

    try:
        first.paragraph._p.getparent().remove(first.paragraph._p)
    except Exception:
        first.paragraph.text = ""

    return headings[1:]


def _set_heading_styles_and_collect_bookmarks(doc: Document, body_start_idx: int) -> list[HeadingEntry]:
    """Smart identification: detect headings by pattern, Word style, and formatting."""
    entries: list[HeadingEntry] = []
    pending_chapter_label = False
    pending_chapter_label_text = ""
    bookmark_id = 100

    for p in doc.paragraphs[body_start_idx:]:
        if paragraph_has_image(p):
            continue

        text = (p.text or "").strip()
        if not text:
            continue

        style_name = (p.style.name or "").strip().lower()

        # ── pending "CHAPTER X" label line ──────────────────────────────
        if CHAPTER_LABEL_RE.match(text):
            pending_chapter_label = True
            pending_chapter_label_text = text.strip()
            continue

        # ── Heading 1 detection (smart) ─────────────────────────────────
        is_main_heading = (
            pending_chapter_label
            or style_name.startswith("heading 1")
            or CHAPTER_RE.match(text) is not None
            or CHAPTER_WORD_RE.match(text) is not None
            or PART_RE.match(text) is not None
            or ACT_RE.match(text) is not None
            or BOOK_PART_RE.match(text) is not None
            or FRONT_BACK_RE.match(text) is not None
            or FRONT_BACK_EXTENDED_RE.match(text) is not None
            or ROMAN_HEADING_RE.match(text) is not None
            or NUMBERED_TITLE_RE.match(text) is not None
        )

        # Format-based fallback: short bold/large/caps line
        if not is_main_heading and _looks_like_heading_by_format(p, text):
            is_main_heading = True

        if is_main_heading:
            p.style = doc.styles["Heading 1"]
            p.paragraph_format.page_break_before = len(entries) > 0
            title = _normalize_heading_text(text)

            # If preceded by a "CHAPTER X" label, combine them
            if pending_chapter_label and pending_chapter_label_text:
                ch_match = CHAPTER_RE.match(title) or CHAPTER_WORD_RE.match(title)
                if not ch_match:
                    combined = f"{pending_chapter_label_text}: {title}"
                    title = _normalize_heading_text(combined)
                    for run in p.runs:
                        run.text = ""
                    if p.runs:
                        p.runs[0].text = title
                    else:
                        p.add_run(title)

            bookmark = f"chap_{len(entries)+1:03d}"
            _add_bookmark(p, bookmark, bookmark_id)
            bookmark_id += 1
            entries.append(HeadingEntry(paragraph=p, title=title, bookmark=bookmark))
            pending_chapter_label = False
            pending_chapter_label_text = ""
            continue

        # ── Heading 2 detection (smart) ─────────────────────────────────
        is_sub_heading = (
            style_name.startswith("heading 2")
            or SUBHEADING_RE.match(text) is not None
            or SECTION_RE.match(text) is not None
        )

        if is_sub_heading:
            p.style = doc.styles["Heading 2"]
            title = _normalize_heading_text(text)
            bookmark = f"sub_{len(entries)+1:03d}"
            _add_bookmark(p, bookmark, bookmark_id)
            bookmark_id += 1
            entries.append(HeadingEntry(paragraph=p, title=title, bookmark=bookmark))
            pending_chapter_label = False
        else:
            pending_chapter_label = False

    return entries


def _apply_base_text_styles(doc: Document, body_start_idx: int, font_name: str, body_size_pt: float, kindle_mode: bool) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = font_name
    normal.font.size = Pt(body_size_pt)

    h1 = doc.styles["Heading 1"]
    h1.font.name = "Tahoma"
    h1.font.size = Pt(24)
    h1.font.bold = True
    h1.font.color.rgb = RGBColor(0, 0, 0)

    h2 = doc.styles["Heading 2"]
    h2.font.name = "Tahoma"
    h2.font.size = Pt(20)
    h2.font.bold = True
    h2.font.color.rgb = RGBColor(0, 0, 0)

    for p in doc.paragraphs[body_start_idx:]:
        if paragraph_has_image(p):
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            continue

        text = (p.text or "").strip()
        if not text:
            continue

        style_name = (p.style.name or "").strip().lower()
        if style_name.startswith("heading"):
            if style_name.startswith("heading 1"):
                _apply_heading_one_paragraph_style(p)
            elif style_name.startswith("heading 2"):
                _apply_heading_two_paragraph_style(p)
            else:
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                for r in p.runs:
                    _ensure_run_font(r, font_name, 12)
            continue

        p.alignment = WD_ALIGN_PARAGRAPH.LEFT if kindle_mode else WD_ALIGN_PARAGRAPH.JUSTIFY
        p.paragraph_format.first_line_indent = Inches(0 if kindle_mode else 0.2)
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.line_spacing = 1.2 if kindle_mode else 1.15
        for r in p.runs:
            _ensure_run_font(r, font_name, body_size_pt)


def _resize_inline_images_to_fit(doc: Document, max_width_inches: float, max_height_inches: float = 0) -> None:
    """Scale every inline image to fit within the printable area (width AND height).

    If max_height_inches is 0 it is auto-calculated from the first section's
    page height minus top/bottom margins.  Images are scaled proportionally —
    they never stretch or crop.
    """
    if max_height_inches <= 0:
        sec = doc.sections[0] if doc.sections else None
        if sec and sec.page_height and sec.top_margin is not None and sec.bottom_margin is not None:
            max_height_inches = (sec.page_height - sec.top_margin - sec.bottom_margin) / 914400
            # Leave room for heading text above image (~1.2 in)
            max_height_inches = max(2.0, max_height_inches - 1.2)
        else:
            # Default for 6x9 page with 0.5in margins: 9 - 0.5 - 0.5 - 1.2 = 6.8
            max_height_inches = 6.5

    max_w = Inches(max_width_inches)
    max_h = Inches(max_height_inches)

    for shape in doc.inline_shapes:
        orig_w = shape.width
        orig_h = shape.height
        if orig_w <= 0 or orig_h <= 0:
            continue

        scale = 1.0
        if orig_w > max_w:
            scale = min(scale, max_w / orig_w)
        if orig_h > max_h:
            scale = min(scale, max_h / orig_h)

        if scale < 1.0:
            shape.width = int(orig_w * scale)
            shape.height = int(orig_h * scale)


def _keep_heading_with_following_image(doc: Document) -> None:
    """Ensure every heading stays on the same page as its following image."""
    paragraphs = doc.paragraphs
    for i, p in enumerate(paragraphs[:-1]):
        style_name = (p.style.name or "").strip().lower()
        if not style_name.startswith("heading"):
            continue

        # Always keep heading together with whatever follows
        p.paragraph_format.keep_together = True
        p.paragraph_format.keep_with_next = True

        j = i + 1
        while j < len(paragraphs):
            nxt = paragraphs[j]
            if paragraph_has_image(nxt):
                # Chain all paragraphs between heading and image
                for k in range(i + 1, j + 1):
                    mid = paragraphs[k]
                    mid.paragraph_format.keep_with_next = True
                    mid.paragraph_format.keep_together = True
                # Also keep image with next paragraph (caption, etc.)
                nxt.paragraph_format.keep_together = True
                break

            if (nxt.text or "").strip():
                break
            # Blank line — chain it
            nxt.paragraph_format.keep_with_next = True
            j += 1


def _isolate_image_pages(doc: Document) -> None:
    """Insert a page break after every image paragraph so no body text shares the page."""
    paragraphs = doc.paragraphs
    for i, p in enumerate(paragraphs):
        if not paragraph_has_image(p):
            continue
        # Don't add a break if this is the last paragraph
        if i >= len(paragraphs) - 1:
            continue
        # Don't add if the next paragraph is a heading (it already has its own page break)
        nxt = paragraphs[i + 1]
        nxt_style = (nxt.style.name or "").strip().lower()
        if nxt_style.startswith("heading"):
            continue
        # Don't add if next paragraph is also an image
        if paragraph_has_image(nxt):
            continue
        # Set page break before the next paragraph so text starts on a new page
        nxt.paragraph_format.page_break_before = True


def _force_recto_chapter_starts(doc: Document) -> None:
    """Force every Heading 1 (chapter opener) to start on a right (odd/recto) page.

    Inserts an oddPage section break before each Heading 1, then links
    headers/footers to the previous section so they stay consistent.
    Skips the very first Heading 1 if it's preceded by a section break already (front matter).
    """
    body = doc.element.body
    heading_paragraphs = []

    for p in doc.paragraphs:
        style_name = (p.style.name or "").strip().lower()
        if style_name.startswith("heading 1"):
            heading_paragraphs.append(p)

    for idx, hp in enumerate(heading_paragraphs):
        # Remove page_break_before — section break handles it
        hp.paragraph_format.page_break_before = False

        # Check if the paragraph before this heading already has a section break
        prev_elem = hp._p.getprevious()
        if prev_elem is not None:
            prev_pPr = prev_elem.find(qn("w:pPr"))
            if prev_pPr is not None and prev_pPr.find(qn("w:sectPr")) is not None:
                # Already has a section break — change it to oddPage
                existing_sect = prev_pPr.find(qn("w:sectPr"))
                sect_type = existing_sect.find(qn("w:type"))
                if sect_type is None:
                    sect_type = OxmlElement("w:type")
                    existing_sect.append(sect_type)
                sect_type.set(qn("w:val"), "oddPage")
                continue

        # Insert a new empty paragraph before this heading with an oddPage section break
        spacer = OxmlElement("w:p")
        spacer_pPr = OxmlElement("w:pPr")
        spacer_sect = OxmlElement("w:sectPr")
        spacer_type = OxmlElement("w:type")
        spacer_type.set(qn("w:val"), "oddPage")
        spacer_sect.append(spacer_type)
        spacer_pPr.append(spacer_sect)
        spacer.append(spacer_pPr)
        body.insert(list(body).index(hp._p), spacer)


def _estimated_wrapped_lines(text: str, chars_per_line: int) -> int:
    cleaned = (text or "").strip()
    if not cleaned:
        return 1
    return max(1, (len(cleaned) + chars_per_line - 1) // chars_per_line)


def _leading_blank_lines(target_total_lines: int, content_lines: int, min_lines: int, max_lines: int) -> int:
    blank_lines = (target_total_lines - content_lines) // 2
    if blank_lines < min_lines:
        return min_lines
    if blank_lines > max_lines:
        return max_lines
    return blank_lines


def _insert_front_matter_and_toc(
    doc: Document,
    anchor: Paragraph,
    headings: list[HeadingEntry],
    kindle_mode: bool,
    title_placeholder: str,
    author_placeholder: str,
) -> None:
    # Page 1: title
    title_text = (title_placeholder or "").upper()
    subtitle_text = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor.".upper()
    author_text = ("\n" + (author_placeholder or "")).upper()

    title_content_lines = (
        _estimated_wrapped_lines(title_text, chars_per_line=18)
        + _estimated_wrapped_lines(subtitle_text, chars_per_line=28)
        + _estimated_wrapped_lines(author_text, chars_per_line=22)
        + 2
    )
    title_top_blank_lines = _leading_blank_lines(
        target_total_lines=26,
        content_lines=title_content_lines,
        min_lines=2,
        max_lines=10,
    )

    p_title = anchor.insert_paragraph_before("")
    p_title.style = doc.styles["Normal"]
    p_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    top_spacer = p_title.add_run("\n" * title_top_blank_lines)
    _ensure_run_font(top_spacer, "Tahoma", 1)
    title_run = p_title.add_run(title_text)
    _ensure_run_font(title_run, "Tahoma", 28)
    title_run.bold = True
    p_title.add_run("\n\n")
    subtitle_run = p_title.add_run(subtitle_text)
    _ensure_run_font(subtitle_run, "Tahoma", 18)
    subtitle_run.bold = False
    p_title.add_run("\n\n")
    author_run = p_title.add_run(author_text)
    _ensure_run_font(author_run, "Tahoma", 18)
    author_run.bold = True
    p_title.add_run().add_break(WD_BREAK.PAGE)

    # Page 2: copyright
    copyright_content_lines = (
        5
        + _estimated_wrapped_lines(
            "No part of this book may be reproduced or transmitted in any form or by any means, electronic or mechanical, including photocopying, recording, or by any other form without written permission from the publisher.",
            chars_per_line=60,
        )
    )
    copyright_top_blank_lines = _leading_blank_lines(
        target_total_lines=30,
        content_lines=copyright_content_lines,
        min_lines=2,
        max_lines=12,
    )

    p_copyright = anchor.insert_paragraph_before("")
    copyright_spacer = p_copyright.add_run("\n" * copyright_top_blank_lines)
    _ensure_run_font(copyright_spacer, "Tahoma", 1)
    isbn_run = p_copyright.add_run("ISBN: 978-1-957590-50-9\n\n")
    _ensure_run_font(isbn_run, "Trebuchet MS", 12)

    support_run = p_copyright.add_run("For questions, email: Support@AwesomeReads.org\n\n")
    _ensure_run_font(support_run, "Cambira", 12)

    review_prompt_run = p_copyright.add_run("Please consider writing a review!\n\n")
    _ensure_run_font(review_prompt_run, "Georgia", 12)
    review_prompt_run.bold = True

    review_link_run = p_copyright.add_run("Just visit: AwesomeReads.org/review\n\n")
    _ensure_run_font(review_link_run, "Cambira", 12)

    copyright_run = p_copyright.add_run("Copyright 2025. All Rights Reserved.\n\n")
    _ensure_run_font(copyright_run, "Georgia", 12)
    copyright_run.bold = True

    legal_run = p_copyright.add_run(
        "No part of this book may be reproduced or transmitted in any form or by any means, electronic or mechanical, including photocopying, recording, or by any other form without written permission from the publisher."
    )
    _ensure_run_font(legal_run, "Cambira", 12)
    p_copyright.style = doc.styles["Normal"]
    p_copyright.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_copyright.add_run().add_break(WD_BREAK.PAGE)

    # Page 3: bonus page
    bonus_content_lines = 5
    bonus_top_blank_lines = _leading_blank_lines(
        target_total_lines=26,
        content_lines=bonus_content_lines,
        min_lines=4,
        max_lines=14,
    )

    p_bonus = anchor.insert_paragraph_before("")
    bonus_spacer = p_bonus.add_run("\n" * bonus_top_blank_lines)
    _ensure_run_font(bonus_spacer, "Tahoma", 1)
    bonus_line1 = p_bonus.add_run("FREE BONUS")
    bonus_line1.font.size = Pt(36)
    p_bonus.add_run("\n")
    bonus_line2 = p_bonus.add_run("GET OUR NEXT BOOK")
    bonus_line2.font.size = Pt(20)
    p_bonus.add_run("\n")
    bonus_line3 = p_bonus.add_run("FOR FREE")
    bonus_line3.font.size = Pt(20)
    p_bonus.style = doc.styles["Normal"]
    p_bonus.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _add_next_page_section_break(p_bonus)

    # TOC page — native Word TOC field (Smart Identification)
    # Clickable hyperlinks, dot leaders, multi-level, auto-updated by Word
    p_toc_heading = anchor.insert_paragraph_before("TABLE OF CONTENTS")
    p_toc_heading.style = doc.styles["Heading 1"]
    _apply_heading_one_paragraph_style(p_toc_heading)
    # Exclude this heading from the TOC itself
    _exclude_paragraph_from_toc(p_toc_heading)

    _drop_duplicate_title_heading(headings, title_placeholder)

    # Insert native TOC field — Word will populate from Heading 1-3 styles
    p_toc = anchor.insert_paragraph_before("")
    p_toc.style = doc.styles["Normal"]
    _insert_toc_field(p_toc, levels="1-3")

    p_end = anchor.insert_paragraph_before("")
    if kindle_mode:
        p_end.add_run().add_break(WD_BREAK.PAGE)
    else:
        # oddPage break so the first chapter always starts on a right (recto) page
        _add_section_break(p_end, "oddPage")


def _apply_kindle_layout(doc: Document) -> None:
    for idx, sec in enumerate(doc.sections):
        sec.page_width = Inches(6)
        sec.page_height = Inches(9)
        sec.top_margin = Inches(0.5)
        sec.bottom_margin = Inches(0.5)
        sec.left_margin = Inches(0.5)
        sec.right_margin = Inches(0.5)
        if idx == 0:
            _set_section_vertical_alignment_center(sec)
        else:
            _set_section_vertical_alignment_top(sec)


def _find_first_body_section_idx(doc: Document) -> int:
    """Find the section index that contains the first real chapter heading.

    Skips the TOC heading (outlineLvl=9) and any front matter.
    Page numbering should start here — not on the TOC page.
    """
    body = doc.element.body
    section_idx = 0
    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "p":
            pPr = child.find(qn("w:pPr"))

            # Check if this is a Heading 1 style
            is_h1 = False
            if pPr is not None:
                p_style = pPr.find(qn("w:pStyle"))
                if p_style is not None:
                    val = p_style.get(qn("w:val"), "")
                    is_h1 = val.lower().replace(" ", "") in ("heading1", "heading 1")

            # Skip headings excluded from TOC (outlineLvl=9) — that's the TOC heading
            if is_h1 and pPr is not None:
                outline_lvl = pPr.find(qn("w:outlineLvl"))
                if outline_lvl is not None and outline_lvl.get(qn("w:val")) == "9":
                    is_h1 = False

            if is_h1:
                return section_idx

            # Track section breaks
            if pPr is not None and pPr.find(qn("w:sectPr")) is not None:
                section_idx += 1

    # Fallback: use section after TOC (typically 2 if 3+ sections)
    return 2 if len(doc.sections) >= 3 else 1


def _apply_paperback_layout(doc: Document, estimated_pages: int, book_title: str, author_name: str) -> float:
    inside_margin = _inside_margin_for_page_count(estimated_pages)
    outside_margin = 0.25
    doc.settings.odd_and_even_pages_header_footer = True
    first_numbered_idx = _find_first_body_section_idx(doc)

    for idx, sec in enumerate(doc.sections):
        sec.page_width = Inches(6)
        sec.page_height = Inches(9)
        sec.top_margin = Inches(0.5)
        sec.bottom_margin = Inches(0.5)
        sec.left_margin = Inches(inside_margin)
        sec.right_margin = Inches(outside_margin)
        if idx == 0:
            _set_section_vertical_alignment_center(sec)
        else:
            _set_section_vertical_alignment_top(sec)

        sec.header.is_linked_to_previous = False
        sec.even_page_header.is_linked_to_previous = False
        sec.footer.is_linked_to_previous = False
        sec.even_page_footer.is_linked_to_previous = False

        for header in (sec.header, sec.even_page_header):
            if header.paragraphs:
                hp = header.paragraphs[0]
                hp.clear()
            else:
                hp = header.add_paragraph()
            hp.text = ""
            hp.alignment = WD_ALIGN_PARAGRAPH.CENTER

        if idx >= first_numbered_idx:
            if idx == first_numbered_idx:
                _set_page_number_start(sec, 1)
            # Odd pages on the right, even pages on the left.
            _set_footer_page_number(sec.footer, WD_ALIGN_PARAGRAPH.RIGHT)
            _set_footer_page_number(sec.even_page_footer, WD_ALIGN_PARAGRAPH.LEFT)
        else:
            for footer in (sec.footer, sec.even_page_footer):
                if footer.paragraphs:
                    fp = footer.paragraphs[0]
                    fp.clear()
                else:
                    fp = footer.add_paragraph()
                fp.alignment = WD_ALIGN_PARAGRAPH.CENTER

    return inside_margin


def _derive_title_author(doc: Document) -> tuple[str, str]:
    title = "Book Title Placeholder"
    author = "Author Name"

    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            title = t[:120]
            break

    return title, author


def build_kdp_documents(
    source_docx: Path,
    kindle_output: Path,
    paperback_output: Path,
    estimated_pages: int = 0,
    title_placeholder: str = "Book Title Placeholder",
    author_placeholder: str = "Author Name",
) -> tuple[Path, Path, int, float]:
    if not source_docx.exists():
        raise FileNotFoundError(f"Source .docx not found: {source_docx}")

    # Kindle variant
    shutil.copy2(source_docx, kindle_output)
    kindle_doc = Document(str(kindle_output))
    if not kindle_doc.paragraphs:
        kindle_doc.add_paragraph("")
    kindle_anchor = kindle_doc.paragraphs[0]
    kindle_body_idx = 0
    kindle_headings = _set_heading_styles_and_collect_bookmarks(kindle_doc, kindle_body_idx)
    _apply_base_text_styles(kindle_doc, kindle_body_idx, font_name="Times New Roman", body_size_pt=11.5, kindle_mode=True)
    _insert_front_matter_and_toc(
        kindle_doc,
        anchor=kindle_anchor,
        headings=kindle_headings,
        kindle_mode=True,
        title_placeholder=title_placeholder,
        author_placeholder=author_placeholder,
    )
    # Apply layout AFTER front matter is inserted so all sections exist
    _apply_kindle_layout(kindle_doc)
    # Resize images AFTER layout sets page dimensions (so height calc works)
    _resize_inline_images_to_fit(kindle_doc, max_width_inches=5.0)
    _keep_heading_with_following_image(kindle_doc)
    _isolate_image_pages(kindle_doc)
    _enable_update_fields_on_open(kindle_doc)
    kindle_doc.save(str(kindle_output))

    # Paperback variant
    shutil.copy2(source_docx, paperback_output)
    paperback_doc = Document(str(paperback_output))
    if not paperback_doc.paragraphs:
        paperback_doc.add_paragraph("")
    paperback_anchor = paperback_doc.paragraphs[0]
    paperback_body_idx = 0
    paperback_headings = _set_heading_styles_and_collect_bookmarks(paperback_doc, paperback_body_idx)
    _apply_base_text_styles(paperback_doc, paperback_body_idx, font_name="Times New Roman", body_size_pt=11, kindle_mode=False)

    title, author = _derive_title_author(paperback_doc)
    if title_placeholder and title_placeholder != "Book Title Placeholder":
        title = title_placeholder
    if author_placeholder and author_placeholder != "Author Name":
        author = author_placeholder

    estimated = estimated_pages if estimated_pages > 0 else _estimate_page_count(paperback_doc)
    _insert_front_matter_and_toc(
        paperback_doc,
        anchor=paperback_anchor,
        headings=paperback_headings,
        kindle_mode=False,
        title_placeholder=title,
        author_placeholder=author,
    )
    _resize_inline_images_to_fit(paperback_doc, max_width_inches=4.9)
    _keep_heading_with_following_image(paperback_doc)
    _isolate_image_pages(paperback_doc)
    _force_recto_chapter_starts(paperback_doc)
    # Apply layout AFTER recto breaks are inserted, so section indices are final
    inside_margin = _apply_paperback_layout(paperback_doc, estimated, title, author)
    _enable_update_fields_on_open(paperback_doc)
    paperback_doc.save(str(paperback_output))

    return kindle_output, paperback_output, estimated, inside_margin


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate KDP-ready Kindle and Paperback DOCX files from a source manuscript."
    )
    ap.add_argument("input", help="Source .docx path (ideally post-image manuscript)")
    ap.add_argument("--kindle-output", default="", help="Output path for Kindle .docx")
    ap.add_argument("--paperback-output", default="", help="Output path for Paperback .docx")
    ap.add_argument("--estimated-pages", type=int, default=0, help="Optional manual page count for gutter sizing")
    ap.add_argument("--title-placeholder", default="Book Title Placeholder", help="Placeholder title for page 1")
    ap.add_argument("--author-placeholder", default="Author Name", help="Placeholder author for front matter and header")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"ERROR: Source file not found: {in_path}")
        return 2

    kindle_out = Path(args.kindle_output) if args.kindle_output else in_path.with_stem(in_path.stem + "_kindle")
    paperback_out = (
        Path(args.paperback_output)
        if args.paperback_output
        else in_path.with_stem(in_path.stem + "_paperback")
    )

    kindle_out.parent.mkdir(parents=True, exist_ok=True)
    paperback_out.parent.mkdir(parents=True, exist_ok=True)

    kindle_path, paperback_path, estimated, inside = build_kdp_documents(
        source_docx=in_path,
        kindle_output=kindle_out,
        paperback_output=paperback_out,
        estimated_pages=args.estimated_pages,
        title_placeholder=args.title_placeholder,
        author_placeholder=args.author_placeholder,
    )

    print(f"Kindle file: {kindle_path}")
    print(f"Paperback file: {paperback_path}")
    print(f"Estimated pages: {estimated}")
    print(f"Paperback inside margin (no bleed): {inside:.3f} in")
    print("Note: If Word prompts, update fields/links before final upload checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
