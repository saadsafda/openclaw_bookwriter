"""Trade-format page layout for puzzle books.

This module reproduces the interior format of a professionally typeset 6x9
children's puzzle book (Spotlight Media / ActivityWizo house style), which the
plain manuscript in :mod:`puzzle.export` did not attempt:

* roman-numeraled front matter — title page, copyright page, Table of Contents
  with dotted leaders
* one numbered ``Section N`` divider per section, each followed by a blank
  verso so every section opens on a recto
* arabic body page numbers in the outer header, starting at the first puzzle
* per-type page density matching the reference book rather than one puzzle per
  page: 2 riddles/page, 4 sudoku/page in a 2x2, 4 trivia questions/page,
  word banks in two ALL-CAPS columns, cryptograms with a CLUE block
* a consolidated Answer Key at the back that mirrors section order and packs
  solutions multi-up (4 maze/word-search/crossword solutions per page, riddle
  and trivia answers in two columns)

The section *order* is the reference book's, not the generator's historical
order: mazes, picture puzzles, riddles, word searches, cryptograms, sudoku,
trivia, crosswords, then the Answer Key.

:func:`build_formatted_docx` is a drop-in alternative to
``export.build_docx``. Because it sets its own page size, margins, headers and
page numbers, its output is already print-ready and must NOT be passed through
``kdp_docx_formatter``, which would re-paginate it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from print_hygiene import sanitize_for_print

from .engine import PAGE_H_IN, PAGE_W_IN, PRINT_DPI, PuzzleBook, SECTION_LABELS

# --------------------------------------------------------------------------
# Trim, margins and the derived live area
# --------------------------------------------------------------------------

# KDP 6x9 with a gutter sized for a ~150-page interior.
MARGIN_TOP_IN = 0.75
MARGIN_BOTTOM_IN = 0.75
MARGIN_OUTER_IN = 0.5
MARGIN_INNER_IN = 0.75          # gutter side

LIVE_W_IN = PAGE_W_IN - MARGIN_INNER_IN - MARGIN_OUTER_IN   # 4.75
LIVE_H_IN = PAGE_H_IN - MARGIN_TOP_IN - MARGIN_BOTTOM_IN    # 7.50

# Full-page puzzle art (mazes, word searches, crosswords, hidden pictures).
FULL_IMAGE_W_IN = LIVE_W_IN
# Answer-key art placed 2-up across, 2 rows down.
KEY_IMAGE_W_IN = 2.15

BODY_FONT = "Georgia"
DISPLAY_FONT = "Verdana"
MONO_FONT = "Courier New"

ANSWER_KEY_TITLE = "Answer Key"

# The reference book's section sequence.
SECTION_SEQUENCE = (
    "mazes",
    "picture_puzzles",
    "riddles",
    "word_searches",
    "cryptograms",
    "sudoku",
    "trivia",
    "crosswords",
)


# --------------------------------------------------------------------------
# Low-level docx helpers
# --------------------------------------------------------------------------

def _field(paragraph, instruction: str) -> None:
    """Insert a Word field code (used for PAGE numbers and TOC leaders)."""
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)


def _set_page_number_format(section, fmt: str, start: int | None = None) -> None:
    """Set numeral style ('lowerRoman' / 'decimal') and optional restart."""
    sect_pr = section._sectPr
    existing = sect_pr.find(qn("w:pgNumType"))
    if existing is not None:
        sect_pr.remove(existing)
    pg = OxmlElement("w:pgNumType")
    pg.set(qn("w:fmt"), fmt)
    if start is not None:
        pg.set(qn("w:start"), str(start))
    sect_pr.append(pg)


def _apply_page_setup(section) -> None:
    section.page_width = Inches(PAGE_W_IN)
    section.page_height = Inches(PAGE_H_IN)
    section.top_margin = Inches(MARGIN_TOP_IN)
    section.bottom_margin = Inches(MARGIN_BOTTOM_IN)
    section.left_margin = Inches(MARGIN_INNER_IN)
    section.right_margin = Inches(MARGIN_OUTER_IN)
    section.gutter = Inches(0)
    # Mirror margins so the gutter falls on the binding edge of both sides.
    sect_pr = section._sectPr
    existing = sect_pr.find(qn("w:pgMar"))
    if existing is not None:
        existing.set(qn("w:gutter"), "0")


def _enable_mirror_margins(doc: Document) -> None:
    settings = doc.settings.element
    if settings.find(qn("w:mirrorMargins")) is None:
        settings.append(OxmlElement("w:mirrorMargins"))


def _style_document(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 1.15
    rpr = normal.element.get_or_add_rPr().get_or_add_rFonts()
    rpr.set(qn("w:eastAsia"), BODY_FONT)
    rpr.set(qn("w:cs"), BODY_FONT)


def _para(
    doc_or_cell,
    text: str = "",
    *,
    size: float = 11,
    bold: bool = False,
    italic: bool = False,
    align=None,
    font: str = BODY_FONT,
    space_before: float = 0,
    space_after: float = 0,
    indent: float = 0,
    color: RGBColor | None = None,
    keep_with_next: bool = False,
):
    para = doc_or_cell.add_paragraph()
    pf = para.paragraph_format
    pf.space_before = Pt(space_before)
    pf.space_after = Pt(space_after)
    if indent:
        pf.left_indent = Inches(indent)
    if align is not None:
        para.alignment = align
    if keep_with_next:
        pf.keep_with_next = True
    if text:
        run = para.add_run(text)
        run.bold = bold
        run.italic = italic
        run.font.size = Pt(size)
        run.font.name = font
        if color is not None:
            run.font.color.rgb = color
    return para


def _page_break(doc: Document) -> None:
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def _blank_page(doc: Document) -> None:
    """An intentionally empty page (the verso after a section divider)."""
    _page_break(doc)


def _borderless(table) -> None:
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "none")
        el.set(qn("w:sz"), "0")
        borders.append(el)
    tbl_pr.append(borders)


def _grid(doc: Document, rows: int, cols: int, col_w_in: float):
    table = doc.add_table(rows=rows, cols=cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    _borderless(table)
    for row in table.rows:
        for cell in row.cells:
            cell.width = Inches(col_w_in)
            # Drop the empty paragraph python-docx seeds each cell with.
            cell.paragraphs[0]._element.getparent().remove(cell.paragraphs[0]._element)
    return table


def _add_image(container, image_path: str, width_in: float, *, align=WD_ALIGN_PARAGRAPH.CENTER) -> bool:
    if not image_path or not Path(image_path).exists():
        return False
    sanitize_for_print(image_path, PRINT_DPI)
    para = container.add_paragraph()
    para.alignment = align
    para.paragraph_format.space_before = Pt(0)
    para.paragraph_format.space_after = Pt(0)
    para.add_run().add_picture(image_path, width=Inches(width_in))
    return True


def _chunk(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# --------------------------------------------------------------------------
# Front matter
# --------------------------------------------------------------------------

def _title_page(doc: Document, book: PuzzleBook, author: str) -> None:
    cfg = book.config
    _para(doc, "", space_after=90)
    _para(doc, cfg.book_title, size=30, bold=True, font=DISPLAY_FONT,
          align=WD_ALIGN_PARAGRAPH.CENTER, space_after=18)
    blurb = _section_blurb(book)
    if blurb:
        _para(doc, blurb, size=12, italic=True, align=WD_ALIGN_PARAGRAPH.CENTER,
              space_after=140)
    _para(doc, author.upper(), size=13, bold=True, font=DISPLAY_FONT,
          align=WD_ALIGN_PARAGRAPH.CENTER)
    _page_break(doc)


def _section_blurb(book: PuzzleBook) -> str:
    """The reference book's cover line: the puzzle types, comma-listed."""
    names = [SECTION_LABELS.get(k, k.replace("_", " ").title())
             for k in SECTION_SEQUENCE if _section_items(book, k)]
    if not names:
        return ""
    return ", ".join(names) + ", and Much More!"


def _copyright_page(doc: Document, book: PuzzleBook, author: str,
                    isbn: str, publisher: str, support_email: str) -> None:
    lines = []
    if isbn:
        lines.append(f"ISBN: {isbn}")
    lines.append(f"Copyright {_year()}.")
    if publisher:
        lines.append(f"{publisher.upper()}.")
    if support_email:
        lines.append("For questions, please reach out to:")
        lines.append(support_email)
    lines.append("All Rights Reserved.")
    lines.append("")
    lines.append(
        "No part of this book may be reproduced or transmitted in any form or "
        "by any means, electronic or mechanical, including photocopying, "
        "recording, or by any other form without written permission from the "
        "publisher."
    )
    _para(doc, "", space_after=60)
    for line in lines:
        _para(doc, line, size=9.5, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=3)
    _page_break(doc)


def _year() -> int:
    from datetime import date
    return date.today().year


def _toc_page(doc: Document, entries: list[tuple[str, int]]) -> None:
    """Table of Contents with dotted leaders and the real body page numbers."""
    _para(doc, "Table of Contents", size=20, bold=True, font=DISPLAY_FONT,
          align=WD_ALIGN_PARAGRAPH.CENTER, space_after=22)

    for label, page in entries:
        para = doc.add_paragraph()
        pf = para.paragraph_format
        pf.space_after = Pt(9)
        # Right tab at the live-width edge, dotted leader up to it.
        tab = OxmlElement("w:tabs")
        tab_el = OxmlElement("w:tab")
        tab_el.set(qn("w:val"), "right")
        tab_el.set(qn("w:leader"), "dot")
        tab_el.set(qn("w:pos"), str(int(LIVE_W_IN * 1440)))
        tab.append(tab_el)
        para._p.get_or_add_pPr().append(tab)

        run = para.add_run(label)
        run.font.size = Pt(11)
        run.font.name = BODY_FONT
        num = para.add_run(f"\t{page}")
        num.font.size = Pt(11)
        num.font.name = BODY_FONT
    _page_break(doc)


# --------------------------------------------------------------------------
# Section dividers and headers
# --------------------------------------------------------------------------

def _section_divider(doc: Document, index: int, label: str) -> None:
    """``Section N`` + title on a recto, then a blank verso."""
    _para(doc, "", space_after=150)
    _para(doc, f"Section {index}", size=13, font=DISPLAY_FONT,
          align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10,
          color=RGBColor(0x55, 0x55, 0x55))
    _para(doc, label, size=26, bold=True, font=DISPLAY_FONT,
          align=WD_ALIGN_PARAGRAPH.CENTER)
    _page_break(doc)
    _blank_page(doc)


def _body_header(section) -> None:
    """Page number in the outer header, alternating left/right."""
    section.different_first_page_header_footer = False
    _enable_odd_even(section)

    for header, align in (
        (section.header, WD_ALIGN_PARAGRAPH.RIGHT),        # odd/recto -> outer right
        (section.even_page_header, WD_ALIGN_PARAGRAPH.LEFT),  # even/verso -> outer left
    ):
        para = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
        para.alignment = align
        for run in list(para.runs):
            run._r.getparent().remove(run._r)
        _field(para, " PAGE ")
        for run in para.runs:
            run.font.size = Pt(10)
            run.font.name = DISPLAY_FONT


def _enable_odd_even(section) -> None:
    doc_settings = section.part.document.settings.element
    if doc_settings.find(qn("w:evenAndOddHeaders")) is None:
        doc_settings.append(OxmlElement("w:evenAndOddHeaders"))


# --------------------------------------------------------------------------
# Per-section page builders
#
# Each returns the number of body pages it consumed, so the TOC can be built
# with real page numbers on a first pass.
# --------------------------------------------------------------------------

def _pages_for(book: PuzzleBook, kind: str) -> int:
    """Body pages a section occupies: 2 divider pages + its content pages."""
    items = _section_items(book, kind)
    if not items:
        return 0
    n = len(items)
    if kind == "mazes":
        content = n
    elif kind == "picture_puzzles":
        content = n * 2 if not book.config.picture_briefs_only else _brief_pages(items)
    elif kind == "riddles":
        content = -(-n // 2)          # 2 per page
    elif kind == "word_searches":
        content = n
    elif kind == "cryptograms":
        content = 2 + n               # 2 how-to-solve pages, then 1 per page
    elif kind == "sudoku":
        content = -(-n // 4)          # 4 per page
    elif kind == "trivia":
        content = sum(-(-len(ch.questions) // 4) for ch in items)
    elif kind == "crosswords":
        content = n
    else:
        content = n
    return content + 2


def _brief_pages(briefs: Sequence[Any]) -> int:
    # Briefs run ~3 to a page in the handoff manuscript.
    return -(-len(briefs) // 3)


def _section_items(book: PuzzleBook, kind: str) -> Sequence[Any]:
    return {
        "mazes": book.mazes,
        "picture_puzzles": book.picture_briefs,
        "riddles": book.riddles,
        "word_searches": book.word_searches,
        "cryptograms": book.cryptograms,
        "sudoku": getattr(book, "sudoku", []) or [],
        "trivia": book.trivia_chapters,
        "crosswords": book.crosswords,
    }.get(kind, [])


def _puzzle_title(doc: Document, text: str) -> None:
    _para(doc, text, size=15, bold=True, font=DISPLAY_FONT,
          align=WD_ALIGN_PARAGRAPH.CENTER, space_after=12, keep_with_next=True)


def _render_mazes(doc: Document, mazes: Sequence[Any]) -> None:
    for i, maze in enumerate(mazes):
        _puzzle_title(doc, maze.title or f"Maze {maze.number}")
        _add_image(doc, maze.image_path, FULL_IMAGE_W_IN)
        if i < len(mazes) - 1:
            _page_break(doc)


def _render_picture_briefs(doc: Document, briefs: Sequence[Any]) -> None:
    _para(doc, "These scenes are drawn by hand. Each brief becomes a "
               "left/right page pair plus a solution image with every "
               "difference circled.",
          size=10, italic=True, space_after=14)
    for i, brief in enumerate(briefs):
        _para(doc, f"{brief.number}. {brief.scene_title}", size=13, bold=True,
              font=DISPLAY_FONT, space_after=6, keep_with_next=True)
        _para(doc, brief.scene_description, size=10.5, space_after=6)
        _para(doc, "Differences to hide:", size=10.5, bold=True, space_after=3)
        for idea in brief.difference_ideas:
            _para(doc, f"• {idea}", size=10, indent=0.25, space_after=2)
        _para(doc, f"Specs: {brief.specs}", size=9, italic=True, space_after=16)
        if (i + 1) % 3 == 0 and i < len(briefs) - 1:
            _page_break(doc)


def _render_riddles(doc: Document, riddles: Sequence[Any]) -> None:
    """Two riddles per page, verse lines kept intact."""
    for page_idx, pair in enumerate(_chunk(list(riddles), 2)):
        for slot, riddle in enumerate(pair):
            _para(doc, f"{riddle.number}.", size=13, bold=True, font=DISPLAY_FONT,
                  space_after=6, keep_with_next=True)
            for line in str(riddle.riddle).splitlines():
                line = line.strip()
                if line:
                    _para(doc, line, size=11.5, space_after=2)
            if slot == 0 and len(pair) > 1:
                _para(doc, "", space_after=34)
        if page_idx < (len(riddles) + 1) // 2 - 1:
            _page_break(doc)


def _render_word_searches(doc: Document, searches: Sequence[Any]) -> None:
    """Grid image, then the word bank in two ALL-CAPS columns."""
    for i, ws in enumerate(searches):
        _puzzle_title(doc, ws.title or f"Word Search {ws.number}")
        _add_image(doc, ws.image_path, FULL_IMAGE_W_IN)
        words = [str(w).upper() for w in ws.words]
        if words:
            _para(doc, "", space_after=10)
            half = -(-len(words) // 2)
            table = _grid(doc, rows=half, cols=2, col_w_in=LIVE_W_IN / 2)
            for r in range(half):
                for c in range(2):
                    idx = r + c * half
                    if idx < len(words):
                        _para(table.cell(r, c), words[idx], size=11,
                              font=DISPLAY_FONT, align=WD_ALIGN_PARAGRAPH.CENTER,
                              space_after=3)
        if i < len(searches) - 1:
            _page_break(doc)


CRYPTOGRAM_HOWTO = [
    ("Start with the short words.",
     "One-letter words are almost always A or I. Two- and three-letter words "
     "are usually IS, IT, AN, THE, AND, or YOU."),
    ("Look for apostrophes.",
     "Words like CAN'T or DAD'S give big hints. The letter after an "
     "apostrophe is usually S or T."),
    ("Pencil it in first.",
     "Don't be afraid to make guesses. Use a pencil or jot your guesses in "
     "the margins. You'll be wrong sometimes, and that's part of the fun."),
    ("Double letters are clues.",
     "If you see repeat letters like XX, it might be LL or EE. English has "
     "patterns that can help you spot the right answer."),
    ("The same code runs all the way through a puzzle.",
     "Once you know that X means E, it means E everywhere in that puzzle."),
]


def _render_cryptograms(doc: Document, grams: Sequence[Any]) -> None:
    # Two how-to-solve pages, as in the reference book.
    _puzzle_title(doc, "CRYPTOGRAMS")
    _para(doc, "Every letter in these quotes has been swapped for a different "
               "letter. Crack the code and reveal what was said. Here is how "
               "to start.",
          size=11, space_after=14)
    for idx, (head, body) in enumerate(CRYPTOGRAM_HOWTO):
        _para(doc, head, size=11.5, bold=True, space_after=3, keep_with_next=True)
        _para(doc, body, size=11, space_after=12)
        if idx == 1:
            _page_break(doc)
    _page_break(doc)

    for i, gram in enumerate(grams):
        title = (gram.hint or "").strip() or f"Cryptogram {gram.number}"
        _para(doc, f"{gram.number}. {title}", size=14, bold=True,
              font=DISPLAY_FONT, space_after=16, keep_with_next=True)
        for line in _wrap_cipher(gram.encoded):
            _para(doc, line, size=13, bold=True, font=MONO_FONT,
                  align=WD_ALIGN_PARAGRAPH.CENTER, space_after=6)
        _para(doc, "", space_after=16)
        clues = _cipher_clues(gram)
        if clues:
            _para(doc, "CLUE:", size=11, bold=True, font=DISPLAY_FONT, space_after=4)
            for cipher_ch, plain_ch in clues:
                _para(doc, f"{cipher_ch} means {plain_ch}.", size=11, indent=0.2,
                      space_after=2)
        if i < len(grams) - 1:
            _page_break(doc)


def _wrap_cipher(encoded: str, width: int = 34) -> list[str]:
    """Break the cipher text into centered lines without splitting a word."""
    words = str(encoded or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _cipher_clues(gram: Any, count: int = 3) -> list[tuple[str, str]]:
    """Three cipher->plain mappings for the most common letters in the quote.

    ``gram.cipher`` maps plain letter -> cipher letter; the puzzle needs the
    reverse, and only for letters that actually appear.
    """
    cipher = getattr(gram, "cipher", None) or {}
    if not cipher:
        return []
    phrase = str(getattr(gram, "phrase", "") or "").upper()
    freq: dict[str, int] = {}
    for ch in phrase:
        if ch.isalpha():
            freq[ch] = freq.get(ch, 0) + 1
    ranked = sorted(freq, key=lambda c: (-freq[c], c))
    clues: list[tuple[str, str]] = []
    for plain in ranked:
        enc = cipher.get(plain) or cipher.get(plain.lower())
        if enc:
            clues.append((str(enc).upper(), plain))
        if len(clues) == count:
            break
    return clues


def _render_sudoku(doc: Document, puzzles: Sequence[Any]) -> None:
    """Four puzzles per page in a 2x2 grid, each captioned below."""
    items = list(puzzles)
    for page_idx, quad in enumerate(_chunk(items, 4)):
        table = _grid(doc, rows=2, cols=2, col_w_in=LIVE_W_IN / 2)
        for slot, puz in enumerate(quad):
            cell = table.cell(slot // 2, slot % 2)
            label = _sudoku_label(puz)
            _para(cell, label, size=10.5, bold=True, font=DISPLAY_FONT,
                  align=WD_ALIGN_PARAGRAPH.CENTER, space_after=4)
            path = getattr(puz, "image_path", "") or ""
            if not _add_image(cell, path, 1.9):
                _render_sudoku_text(cell, getattr(puz, "grid", []) or [])
            _para(cell, "", space_after=18)
        if page_idx < -(-len(items) // 4) - 1:
            _page_break(doc)


def _sudoku_label(puz: Any) -> str:
    number = getattr(puz, "number", "")
    difficulty = str(getattr(puz, "difficulty", "") or "").title()
    return f"Puzzle {number} - {difficulty}" if difficulty else f"Puzzle {number}"


def _render_sudoku_text(cell, grid: Sequence[Sequence[Any]]) -> None:
    """Text fallback when a sudoku image is missing."""
    for row in grid:
        cells = [str(v) if v not in (0, None, "", ".") else "·" for v in row]
        _para(cell, "  ".join(cells), size=11, font=MONO_FONT,
              align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)


def _render_trivia(doc: Document, chapters: Sequence[Any]) -> None:
    """Chapter heading, then 4 questions per page with a) b) c) d) options."""
    for ch_idx, chapter in enumerate(chapters):
        _para(doc, f"Chapter {chapter.number}: {chapter.title}", size=15,
              bold=True, font=DISPLAY_FONT, space_after=16, keep_with_next=True)
        questions = list(chapter.questions)
        for page_idx, batch in enumerate(_chunk(questions, 4)):
            for q in batch:
                _para(doc, f"{q.number}. {q.question}", size=11.5, bold=True,
                      space_after=5, keep_with_next=True)
                for letter in ("A", "B", "C", "D"):
                    if letter not in q.choices:
                        continue
                    _para(doc, f"{letter.lower()}) {q.choices[letter]}",
                          size=11, indent=0.28, space_after=3)
                _para(doc, "", space_after=12)
            last_batch = page_idx == -(-len(questions) // 4) - 1
            if not (last_batch and ch_idx == len(chapters) - 1):
                _page_break(doc)


def _render_crosswords(doc: Document, crosswords: Sequence[Any]) -> None:
    for i, cw in enumerate(crosswords):
        _puzzle_title(doc, cw.title or f"Crossword {cw.number}")
        _add_image(doc, cw.image_path, FULL_IMAGE_W_IN)
        _para(doc, "", space_after=10)
        table = _grid(doc, rows=1, cols=2, col_w_in=LIVE_W_IN / 2)
        for col, direction in enumerate(("across", "down")):
            cell = table.cell(0, col)
            _para(cell, direction.upper(), size=11, bold=True, font=DISPLAY_FONT,
                  space_after=5)
            group = sorted((e for e in cw.entries if e.direction == direction),
                           key=lambda x: x.number)
            for entry in group:
                _para(cell, f"{entry.number}. {entry.clue}", size=9.5,
                      space_after=3)
        if i < len(crosswords) - 1:
            _page_break(doc)


# --------------------------------------------------------------------------
# Answer key
# --------------------------------------------------------------------------

def _image_key_block(doc: Document, items: Sequence[Any], label_of, path_of) -> None:
    """Solution images 4-up (2x2) with captions, as in the reference key."""
    entries = [(label_of(it), path_of(it)) for it in items]
    for page_idx, quad in enumerate(_chunk(entries, 4)):
        table = _grid(doc, rows=2, cols=2, col_w_in=LIVE_W_IN / 2)
        for slot, (label, path) in enumerate(quad):
            cell = table.cell(slot // 2, slot % 2)
            _para(cell, label, size=10, bold=True, font=DISPLAY_FONT,
                  align=WD_ALIGN_PARAGRAPH.CENTER, space_after=4)
            _add_image(cell, path, KEY_IMAGE_W_IN)
            _para(cell, "", space_after=14)
        if page_idx < -(-len(entries) // 4) - 1:
            _page_break(doc)


def _two_column_list(doc: Document, lines: Sequence[str], size: float = 10.5) -> None:
    if not lines:
        return
    half = -(-len(lines) // 2)
    table = _grid(doc, rows=half, cols=2, col_w_in=LIVE_W_IN / 2)
    for r in range(half):
        for c in range(2):
            idx = r + c * half
            if idx < len(lines):
                _para(table.cell(r, c), lines[idx], size=size, space_after=3)


def _key_heading(doc: Document, text: str) -> None:
    _para(doc, text, size=14, bold=True, font=DISPLAY_FONT, space_after=10,
          keep_with_next=True)


def _build_answer_key(doc: Document, book: PuzzleBook, section_no: int) -> None:
    _para(doc, "", space_after=150)
    _para(doc, ANSWER_KEY_TITLE, size=26, bold=True, font=DISPLAY_FONT,
          align=WD_ALIGN_PARAGRAPH.CENTER)
    _page_break(doc)
    _blank_page(doc)

    order = [k for k in SECTION_SEQUENCE if _section_items(book, k)]
    for idx, kind in enumerate(order, start=1):
        items = _section_items(book, kind)
        label = SECTION_LABELS.get(kind, kind.replace("_", " ").title())
        _key_heading(doc, f"Section {idx} - {label}")

        if kind == "mazes":
            _image_key_block(doc, items,
                             lambda m: m.title or f"Maze {m.number}",
                             lambda m: m.solution_path)
        elif kind == "picture_puzzles":
            _para(doc, "Solution images are supplied by the illustrator, with "
                       "every difference circled.", size=10.5, italic=True,
                  space_after=10)
        elif kind == "riddles":
            _two_column_list(doc, [f"{r.number}. {r.answer}" for r in items])
        elif kind == "word_searches":
            _image_key_block(doc, items,
                             lambda w: w.title or f"Word Search {w.number}",
                             lambda w: w.solution_path)
        elif kind == "cryptograms":
            for gram in items:
                title = (gram.hint or "").strip() or f"Cryptogram {gram.number}"
                _para(doc, f"{gram.number}. {title}:", size=10.5, bold=True,
                      space_after=2, keep_with_next=True)
                _para(doc, f"“{gram.phrase}”", size=10.5, space_after=8)
        elif kind == "sudoku":
            _image_key_block(doc, items, _sudoku_label,
                             lambda s: getattr(s, "solution_path", ""))
        elif kind == "trivia":
            for chapter in items:
                _para(doc, f"Chapter {chapter.number}: {chapter.title}",
                      size=11.5, bold=True, space_after=5, keep_with_next=True)
                _two_column_list(
                    doc,
                    [f"{q.number}. {q.correct_answer.lower()}) {q.correct_text()}"
                     for q in chapter.questions],
                    size=10,
                )
                _para(doc, "", space_after=10)
        elif kind == "crosswords":
            _image_key_block(doc, items,
                             lambda c: c.title or f"Crossword {c.number}",
                             lambda c: c.solution_path)

        if idx < len(order):
            _page_break(doc)


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

_RENDERERS = {
    "mazes": _render_mazes,
    "picture_puzzles": _render_picture_briefs,
    "riddles": _render_riddles,
    "word_searches": _render_word_searches,
    "cryptograms": _render_cryptograms,
    "sudoku": _render_sudoku,
    "trivia": _render_trivia,
    "crosswords": _render_crosswords,
}


def _count_page_breaks(doc: Document) -> int:
    """Explicit page breaks currently in the document body."""
    total = 0
    for br in doc.element.body.iter(qn("w:br")):
        if br.get(qn("w:type")) == "page":
            total += 1
    return total


def _measure_section_starts(book: PuzzleBook, order: Sequence[str]) -> list[int]:
    """Body page each section (and the Answer Key) opens on.

    The body is rendered into a scratch document purely to count breaks; the
    scratch document is discarded. Returns one entry per section plus a final
    entry for the Answer Key.

    Note this counts *explicit* breaks, so a section whose content overflows
    its own estimate still lands correctly, while text that reflows across a
    page inside Word can still shift later sections. Word recalculates the
    field-driven page numbers in the header on open; these TOC values are the
    printed reference.
    """
    scratch = Document()
    _style_document(scratch)
    starts: list[int] = []
    for kind in order:
        starts.append(_count_page_breaks(scratch) + 1)
        _section_divider(scratch, 1, "x")
        _RENDERERS[kind](scratch, _section_items(book, kind))
        _page_break(scratch)
    starts.append(_count_page_breaks(scratch) + 1)
    return starts


def build_formatted_docx(
    book: PuzzleBook,
    path: Path,
    *,
    author: str = "Author Name",
    isbn: str = "",
    publisher: str = "",
    support_email: str = "",
) -> Path:
    """Write a print-ready 6x9 interior in the reference book's format.

    The result already carries its own trim size, mirrored margins, page
    numbers and Table of Contents, so it must not be re-processed by
    ``kdp_docx_formatter``.
    """
    doc = Document()
    _style_document(doc)
    _enable_mirror_margins(doc)

    front = doc.sections[0]
    _apply_page_setup(front)
    _set_page_number_format(front, "lowerRoman", start=1)

    # --- front matter (roman, unnumbered on the page) ---------------------
    _title_page(doc, book, author)
    _copyright_page(doc, book, author, isbn, publisher, support_email)

    # Page numbers for the TOC. Rather than estimate page density, render the
    # body once into a throwaway document and count the page breaks each
    # section actually emits — exact for short sections that under-fill a page
    # as well as for full-length ones.
    order = [k for k in SECTION_SEQUENCE if _section_items(book, k)]
    starts = _measure_section_starts(book, order)

    toc: list[tuple[str, int]] = []
    for idx, kind in enumerate(order, start=1):
        label = SECTION_LABELS.get(kind, kind.replace("_", " ").title())
        toc.append((f"Section {idx} – {label}", starts[idx - 1]))
    toc.append((ANSWER_KEY_TITLE, starts[-1]))
    _toc_page(doc, toc)

    # --- body (arabic, restarting at 1, page numbers in the header) -------
    body = doc.add_section(WD_SECTION.ODD_PAGE)
    _apply_page_setup(body)
    _set_page_number_format(body, "decimal", start=1)
    body.header.is_linked_to_previous = False
    _body_header(body)

    for idx, kind in enumerate(order, start=1):
        label = SECTION_LABELS.get(kind, kind.replace("_", " ").title())
        _section_divider(doc, idx, label)
        _RENDERERS[kind](doc, _section_items(book, kind))
        _page_break(doc)

    _build_answer_key(doc, book, len(order) + 1)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path
