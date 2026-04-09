"""
format_docx.py

Takes a .docx file produced by openclaw_docx_writer.py and applies
academic/formal formatting:

  - Cover page with title, subtitle, divider, page break
  - Chapter sections with CHAPTER X label, ornamental divider, page breaks
  - Introduction / Conclusion on their own pages, ALL CAPS centered
  - Subheadings (- lines) as Heading 2 (Thing X: gets accent colour)
  - Focus: lines as italic accent paragraphs
  - Body text justified with first-line indent
  - Georgia font throughout, navy/brown/gray palette
  - US Letter, 1-inch margins, footer with title + page number

Usage:
    python format_docx.py input.docx [output.docx]

If output is omitted, writes to input_formatted.docx.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn, nsdecls
from docx.shared import Cm, Emu, Inches, Pt, RGBColor
from docx.text.paragraph import Paragraph


# ── Colour constants ────────────────────────────────────────────────
NAVY        = RGBColor(0x1B, 0x3A, 0x5C)
BROWN       = RGBColor(0x8B, 0x45, 0x13)
DARK_GRAY   = RGBColor(0x2D, 0x2D, 0x2D)
BODY_BLACK  = RGBColor(0x11, 0x11, 0x11)
LIGHT_GRAY  = RGBColor(0x99, 0x99, 0x99)

FONT_NAME = "Georgia"

# ── Regex patterns (mirror the writer's detection) ──────────────────
CHAPTER_RE  = re.compile(r"^Chapter\s+(\d+)\s*[:\s–—-]\s*(.*)", re.IGNORECASE)
INTRO_CONCL = re.compile(r"^(Introduction|Conclusion|Epilogue|Foreword|Preface)\s*:?\s*(.*)", re.IGNORECASE)
THING_RE    = re.compile(r"^[\-•*–—]\s+Thing\s+(\d+)\s*:\s*(.*)", re.IGNORECASE)
BULLET_RE   = re.compile(r"^[\-•*–—]\s+(.+)")
FOCUS_RE    = re.compile(r"^Focus:\s*(.*)", re.IGNORECASE)
NUMBERED_RE = re.compile(r"^(\d+)\.\s+(.+)")  # "1. Some heading text"
HEADING_STYLE_RE = re.compile(r"^Heading\s+\d+$", re.IGNORECASE)
LIST_BULLET_STYLE_RE = re.compile(r"^List Bullet(?: \d+)?$", re.IGNORECASE)


# ── Low-level XML helpers ───────────────────────────────────────────

def _set_run_font(run, name: str, size_pt: float, color: RGBColor,
                  bold: bool = False, italic: bool = False):
    run.font.name = name
    run.font.size = Pt(size_pt)
    run.font.color.rgb = color
    run.bold = bold
    run.italic = italic
    # Ensure East-Asian / complex-script font also set
    rPr = run._r.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:ascii"), name)
    rFonts.set(qn("w:hAnsi"), name)
    rFonts.set(qn("w:cs"), name)


def _set_paragraph_spacing(para, before_pt: float = 0, after_pt: float = 0,
                           line_spacing: float | None = None):
    pPr = para._p.get_or_add_pPr()
    spacing = pPr.find(qn("w:spacing"))
    if spacing is None:
        spacing = OxmlElement("w:spacing")
        pPr.append(spacing)
    spacing.set(qn("w:before"), str(int(before_pt * 20)))
    spacing.set(qn("w:after"), str(int(after_pt * 20)))
    if line_spacing is not None:
        # line_spacing as multiplier (e.g. 1.5)
        spacing.set(qn("w:line"), str(int(line_spacing * 240)))
        spacing.set(qn("w:lineRule"), "auto")


def _set_first_line_indent(para, indent_pt: float = 24):
    pPr = para._p.get_or_add_pPr()
    ind = pPr.find(qn("w:ind"))
    if ind is None:
        ind = OxmlElement("w:ind")
        pPr.append(ind)
    ind.set(qn("w:firstLine"), str(int(indent_pt * 20)))


def _clear_first_line_indent(para):
    pPr = para._p.get_or_add_pPr()
    ind = pPr.find(qn("w:ind"))
    if ind is not None:
        ind.attrib.pop(qn("w:firstLine"), None)


def _add_page_break_before(para):
    """Set the 'page break before' property on a paragraph."""
    pPr = para._p.get_or_add_pPr()
    pb = OxmlElement("w:pageBreakBefore")
    pPr.append(pb)


def _remove_all_runs(para):
    """Remove all <w:r> elements from a paragraph."""
    for r in list(para._p.findall(qn("w:r"))):
        para._p.remove(r)


def _add_horizontal_line(doc, color_hex: str = "1B3A5C", thickness: float = 1.0,
                         space_before: int = 6, space_after: int = 6) -> Paragraph:
    """Insert a thin horizontal rule as a bottom-border paragraph."""
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(int(thickness * 8)))  # eighths of a point
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color_hex)
    pBdr.append(bottom)
    pPr.append(pBdr)
    _set_paragraph_spacing(p, before_pt=space_before, after_pt=space_after)
    return p


def _add_double_rule(doc, color_hex: str = "1B3A5C",
                     space_before: int = 4, space_after: int = 10) -> Paragraph:
    """Insert a double horizontal rule."""
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "double")
    bottom.set(qn("w:sz"), str(12))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color_hex)
    pBdr.append(bottom)
    pPr.append(pBdr)
    _set_paragraph_spacing(p, before_pt=space_before, after_pt=space_after)
    return p


def _add_empty_para(doc, height_pt: float = 12):
    p = doc.add_paragraph()
    _set_paragraph_spacing(p, before_pt=height_pt, after_pt=0)
    return p


# ── Footer helper ───────────────────────────────────────────────────

def _setup_footer(section, book_title: str):
    """
    Add a centred footer:  Book Title  ·  Page N
    """
    footer = section.footer
    footer.is_linked_to_previous = False
    # Remove existing paragraphs
    for p in footer.paragraphs:
        p.clear()

    if footer.paragraphs:
        fp = footer.paragraphs[0]
    else:
        fp = footer.add_paragraph()

    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Book title run
    r1 = fp.add_run(f"{book_title}  ·  Page ")
    _set_run_font(r1, FONT_NAME, 8, LIGHT_GRAY)

    # Page number field
    fld_char_begin = OxmlElement("w:fldChar")
    fld_char_begin.set(qn("w:fldCharType"), "begin")
    r2 = fp.add_run()
    r2._r.append(fld_char_begin)
    _set_run_font(r2, FONT_NAME, 8, LIGHT_GRAY)

    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    r3 = fp.add_run()
    r3._r.append(instr)
    _set_run_font(r3, FONT_NAME, 8, LIGHT_GRAY)

    fld_char_end = OxmlElement("w:fldChar")
    fld_char_end.set(qn("w:fldCharType"), "end")
    r4 = fp.add_run()
    r4._r.append(fld_char_end)
    _set_run_font(r4, FONT_NAME, 8, LIGHT_GRAY)


# ── Classify each paragraph ────────────────────────────────────────

def _is_bare_outline_topic(t: str) -> bool:
    """Detect short phrase-like outline items with no bullet/number prefix."""
    if not t:
        return False
    words = t.split()
    return 2 <= len(words) <= 15 and len(t) <= 120 and t[-1] != '.'


def classify(text: str, style_name: str):
    """Return a tag describing this paragraph's role."""
    t = text.strip()
    sn = (style_name or "").strip()

    if not t:
        return "empty"
    # Title style
    if sn.lower() == "title":
        return "title"
    # Word "List Bullet" style → treat as bullet subheading
    if LIST_BULLET_STYLE_RE.match(sn):
        return "bullet"
    # Heading style → check content
    if HEADING_STYLE_RE.match(sn):
        if CHAPTER_RE.match(t):
            return "chapter"
        if INTRO_CONCL.match(t):
            return "intro_concl"
        return "chapter"  # treat any heading-styled para as chapter-level
    # Plain text pattern matching
    if CHAPTER_RE.match(t):
        return "chapter"
    if INTRO_CONCL.match(t):
        return "intro_concl"
    if THING_RE.match(t):
        return "thing"
    if NUMBERED_RE.match(t):
        return "numbered"
    if BULLET_RE.match(t):
        return "bullet"
    if FOCUS_RE.match(t):
        return "focus"
    # Bare outline topic: short phrase that isn't a heading or body prose
    if _is_bare_outline_topic(t):
        return "bare_topic"
    return "body"


# ── Main formatting pipeline ───────────────────────────────────────

def format_document(in_path: Path, out_path: Path) -> None:
    src = Document(str(in_path))

    # Collect raw paragraph data (text + original style name)
    raw: list[tuple[str, str]] = []
    for p in src.paragraphs:
        try:
            sn = p.style.name
        except Exception:
            sn = ""
        raw.append((p.text or "", sn or ""))

    # Detect book title: first non-empty paragraph
    book_title = "Untitled"
    for txt, _ in raw:
        if txt.strip():
            book_title = txt.strip()
            break

    # Build a new document from scratch
    doc = Document()

    # ── Page setup ──────────────────────────────────────────────
    section = doc.sections[0]
    section.page_width  = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin    = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin   = Inches(1)
    section.right_margin  = Inches(1)
    section.orientation   = WD_ORIENT.PORTRAIT

    # Footer for first section
    _setup_footer(section, book_title)

    # Remove the default empty paragraph that python-docx creates
    if doc.paragraphs:
        dp = doc.paragraphs[0]._p
        dp.getparent().remove(dp)

    # ── Cover page ──────────────────────────────────────────────
    cover_done = False
    idx = 0

    # Skip leading empties
    while idx < len(raw) and not raw[idx][0].strip():
        idx += 1

    if idx < len(raw):
        title_text = raw[idx][0].strip()
        idx += 1

        # Spacer
        _add_empty_para(doc, 100)

        # Title
        tp = doc.add_paragraph()
        tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        tr = tp.add_run(title_text)
        _set_run_font(tr, FONT_NAME, 32, NAVY, bold=True)
        _set_paragraph_spacing(tp, after_pt=4)

        # Subtitle line
        sp = doc.add_paragraph()
        sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sr = sp.add_run("— A Comprehensive Guide —")
        _set_run_font(sr, FONT_NAME, 14, BROWN, italic=True)
        _set_paragraph_spacing(sp, before_pt=8, after_pt=12)

        # Single-rule divider
        _add_horizontal_line(doc, color_hex="8B4513", thickness=1.5,
                             space_before=12, space_after=24)

        cover_done = True

    # ── Process remaining paragraphs ────────────────────────────
    first_section_content = True

    while idx < len(raw):
        text, sn = raw[idx]
        tag = classify(text, sn)
        t = text.strip()
        idx += 1

        if tag == "empty":
            continue

        if tag == "title":
            # Already handled as cover; skip duplicates
            continue

        # ── Chapter ─────────────────────────────────────────────
        if tag == "chapter":
            m = CHAPTER_RE.match(t)
            if m:
                ch_num = m.group(1)
                ch_title = m.group(2).strip()
            else:
                ch_num = ""
                ch_title = t

            # Combined chapter heading (no separate label)
            label_text = f"Chapter {ch_num}" if ch_num else "Chapter"
            title_text = ch_title if ch_title else t
            combined = f"{label_text}: {title_text}" if title_text else label_text

            ch_p = doc.add_paragraph()
            _add_page_break_before(ch_p)
            ch_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            cr = ch_p.add_run(combined)
            _set_run_font(cr, FONT_NAME, 22, NAVY, bold=True)
            _set_paragraph_spacing(ch_p, before_pt=36, after_pt=4)

            # Double-rule divider
            _add_double_rule(doc, color_hex="1B3A5C",
                             space_before=4, space_after=16)
            continue

        # ── Introduction / Conclusion / etc ─────────────────────
        if tag == "intro_concl":
            m = INTRO_CONCL.match(t)
            label = m.group(1).upper() if m else t.upper()

            ic_p = doc.add_paragraph()
            _add_page_break_before(ic_p)
            ic_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            ic_r = ic_p.add_run(label)
            _set_run_font(ic_r, FONT_NAME, 22, NAVY, bold=True)
            _set_paragraph_spacing(ic_p, before_pt=60, after_pt=16)

            _add_double_rule(doc, color_hex="1B3A5C",
                             space_before=4, space_after=16)
            continue

        # ── Thing X: subheading ─────────────────────────────────
        if tag == "thing":
            m = THING_RE.match(t)
            num = m.group(1)
            title = m.group(2).strip()

            th_p = doc.add_paragraph()
            th_p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            _set_paragraph_spacing(th_p, before_pt=18, after_pt=6)

            # Number in brown
            nr = th_p.add_run(f"Thing {num}: ")
            _set_run_font(nr, FONT_NAME, 14, BROWN, bold=True)

            # Title in dark gray
            tr_ = th_p.add_run(title)
            _set_run_font(tr_, FONT_NAME, 14, DARK_GRAY, bold=True)
            continue

        # ── Numbered subheading (1. text, 2. text) ────────────
        if tag == "numbered":
            m = NUMBERED_RE.match(t)
            num = m.group(1)
            content = m.group(2).strip() if m else t

            np_ = doc.add_paragraph()
            np_.alignment = WD_ALIGN_PARAGRAPH.LEFT
            _set_paragraph_spacing(np_, before_pt=18, after_pt=6)

            # Number in brown
            nr = np_.add_run(f"{num}. ")
            _set_run_font(nr, FONT_NAME, 14, BROWN, bold=True)

            # Heading text in dark gray
            nt = np_.add_run(content)
            _set_run_font(nt, FONT_NAME, 14, DARK_GRAY, bold=True)
            continue

        # ── Bullet subheading ───────────────────────────────────
        if tag == "bullet":
            m = BULLET_RE.match(t)
            content = m.group(1).strip() if m else t

            bp = doc.add_paragraph()
            bp.alignment = WD_ALIGN_PARAGRAPH.LEFT
            _set_paragraph_spacing(bp, before_pt=18, after_pt=6)
            br_ = bp.add_run(content)
            _set_run_font(br_, FONT_NAME, 14, NAVY, bold=True)
            continue

        # ── Focus line ──────────────────────────────────────────
        if tag == "focus":
            m = FOCUS_RE.match(t)
            content = m.group(1).strip() if m else t

            fp = doc.add_paragraph()
            fp.alignment = WD_ALIGN_PARAGRAPH.LEFT
            _set_paragraph_spacing(fp, before_pt=10, after_pt=10)
            fr = fp.add_run(f"Focus: {content}")
            _set_run_font(fr, FONT_NAME, 12, BROWN, italic=True)
            continue
        # ── Bare outline topic (plain-text subheading) ──────────
        if tag == "bare_topic":
            btp = doc.add_paragraph()
            btp.alignment = WD_ALIGN_PARAGRAPH.LEFT
            _set_paragraph_spacing(btp, before_pt=18, after_pt=6)
            btr = btp.add_run(t)
            _set_run_font(btr, FONT_NAME, 14, NAVY, bold=True)
            continue
        # ── Body text ───────────────────────────────────────────
        bp = doc.add_paragraph()
        bp.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        _set_paragraph_spacing(bp, before_pt=0, after_pt=6, line_spacing=1.5)
        _set_first_line_indent(bp, indent_pt=24)
        br_ = bp.add_run(t)
        _set_run_font(br_, FONT_NAME, 12, BODY_BLACK)

    # ── Ensure all sections have correct page setup + footer ────
    for sec in doc.sections:
        sec.page_width    = Inches(8.5)
        sec.page_height   = Inches(11)
        sec.top_margin    = Inches(1)
        sec.bottom_margin = Inches(1)
        sec.left_margin   = Inches(1)
        sec.right_margin  = Inches(1)
        _setup_footer(sec, book_title)

    # ── Save ────────────────────────────────────────────────────
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    print(f"Formatted document saved: {out_path}")

    # ── Basic validation ────────────────────────────────────────
    try:
        check = Document(str(out_path))
        para_count = len(check.paragraphs)
        print(f"Validation OK — {para_count} paragraphs in output.")
    except Exception as e:
        print(f"WARNING: validation failed — {e}", file=sys.stderr)


# ── CLI ─────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Format an openclaw-generated .docx with academic/formal styling."
    )
    ap.add_argument("input", help="Input .docx path")
    ap.add_argument("output", nargs="?", default=None,
                    help="Output .docx path (default: <input>_formatted.docx)")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"ERROR: file not found: {in_path}", file=sys.stderr)
        return 2

    if args.output:
        out_path = Path(args.output)
    else:
        out_path = in_path.with_stem(in_path.stem + "_formatted")

    format_document(in_path, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
