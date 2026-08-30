"""Puzzle book exporters — Markdown, DOCX, KDP 6x9 print files, and the
formatter handoff ZIP.

The DOCX built here is a plain manuscript: Heading 1 per section, Heading 2 per
puzzle, images placed at print size. It is then handed to the existing
kdp_docx_formatter.build_kdp_documents() for 6x9 sizing, gutters, TOC and page
numbers — that module is shared as-is and untouched.

Section 8 of the spec requires every solution to live in one consolidated
Answer Key at the back, so no solution image or answer is ever emitted beside
its puzzle.

Final Assembly in the spec is a zip handed to a professional formatter; that is
build_handoff_zip() below.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

from kdp_docx_formatter import BODY_TEXT_STYLE, ensure_body_text_style
from print_hygiene import (
    PrintHygieneError,
    audit_tree,
    sanitize_for_print,
    strip_control_chars,
)

from .engine import (
    PAGE_H_IN,
    PAGE_W_IN,
    PRINT_DPI,
    PuzzleBook,
    SECTION_LABELS,
)

LETTERS = ("A", "B", "C", "D")

# Puzzle art is rendered full-page; inside a 6x9 book with margins this is the
# usable width.
IMAGE_WIDTH_IN = 4.6

ANSWER_KEY_TITLE = "Answer Key"


def _section_title(kind: str) -> str:
    return SECTION_LABELS.get(kind, kind.replace("_", " ").title())


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

def to_markdown(book: PuzzleBook) -> str:
    cfg = book.config
    lines: list[str] = [f"# {cfg.book_title}", ""]
    lines += [f"*A puzzle and activity book about {cfg.topic}, for {cfg.audience}.*", ""]

    if book.picture_briefs:
        lines += [f"## {_section_title('picture_puzzles')} (illustrator briefs)", ""]
        lines += ["> Section 1 is drawn by human illustrators. These are the briefs.", ""]
        for brief in book.picture_briefs:
            lines += [f"### {brief.number}. {brief.scene_title}", "",
                      brief.scene_description, "",
                      f"*Specs: {brief.specs}*", "", "Differences to hide:", ""]
            lines += [f"{i}. {d}" for i, d in enumerate(brief.difference_ideas, start=1)]
            lines.append("")

    if book.mazes:
        lines += [f"## {_section_title('mazes')}", ""]
        for maze in book.mazes:
            lines += [f"### {maze.title}", "",
                      f"![{maze.title}]({maze.image_path})", ""]

    if book.riddles:
        lines += [f"## {_section_title('riddles')}", ""]
        for riddle in book.riddles:
            body = riddle.riddle.replace("\n", "  \n")
            lines += [f"**{riddle.number}.** {body}", ""]

    if book.word_searches:
        lines += [f"## {_section_title('word_searches')}", ""]
        for ws in book.word_searches:
            lines += [f"### {ws.number}. {ws.title}", "",
                      f"![{ws.title}]({ws.image_path})", "",
                      "Words: " + ", ".join(ws.words), ""]

    if book.cryptograms:
        lines += [f"## {_section_title('cryptograms')}", ""]
        for gram in book.cryptograms:
            lines += [f"### {gram.number}.", "", f"`{gram.encoded}`", ""]
            if gram.hint:
                lines += [f"*Hint: {gram.hint}*", ""]

    if book.trivia_chapters:
        lines += [f"## {_section_title('trivia')}", ""]
        for chapter in book.trivia_chapters:
            lines += [f"### Chapter {chapter.number} — {chapter.title}", ""]
            for q in chapter.questions:
                lines += [f"**{q.number}. {q.question}**", ""]
                lines += [f"{L}. {q.choices[L]}" for L in LETTERS if L in q.choices]
                lines.append("")

    if book.crosswords:
        lines += [f"## {_section_title('crosswords')}", ""]
        for cw in book.crosswords:
            lines += [f"### {cw.number}. {cw.title}", "",
                      f"![{cw.title}]({cw.image_path})", ""]
            for label in ("across", "down"):
                group = [e for e in cw.entries if e.direction == label]
                if not group:
                    continue
                lines += [f"**{label.upper()}**", ""]
                lines += [f"{e.number}. {e.clue}" for e in sorted(group, key=lambda x: x.number)]
                lines.append("")

    # Section 8 — consolidated answer key.
    lines += [f"## {ANSWER_KEY_TITLE}", ""]

    if book.picture_briefs:
        lines += ["### Picture Puzzles", "",
                  "Solution images are supplied by the illustrator, with the "
                  "differences circled.", ""]
    if book.mazes:
        lines += ["### Mazes", ""]
        for maze in book.mazes:
            lines += [f"**{maze.title}**", "",
                      f"![{maze.title} solution]({maze.solution_path})", ""]
    if book.riddles:
        lines += ["### Riddles", ""]
        lines += [f"{r.number}. {r.answer}" for r in book.riddles]
        lines.append("")
    if book.word_searches:
        lines += ["### Word Searches", ""]
        for ws in book.word_searches:
            lines += [f"**{ws.number}. {ws.title}**", "",
                      f"![{ws.title} solution]({ws.solution_path})", ""]
    if book.cryptograms:
        lines += ["### Cryptograms", ""]
        lines += [f"{g.number}. {g.phrase}" for g in book.cryptograms]
        lines.append("")
    if book.trivia_chapters:
        lines += ["### Trivia", ""]
        for chapter in book.trivia_chapters:
            lines += [f"**Chapter {chapter.number} — {chapter.title}**", ""]
            lines += [
                f"{q.number}. {q.correct_answer} — {q.correct_text()}"
                for q in chapter.questions
            ]
            lines.append("")
    if book.crosswords:
        lines += ["### Crosswords", ""]
        for cw in book.crosswords:
            lines += [f"**{cw.number}. {cw.title}**", "",
                      f"![{cw.title} solution]({cw.solution_path})", ""]

    return "\n".join(lines).rstrip() + "\n"


def write_markdown(book: PuzzleBook, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(book), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------

def _add_image(doc: Document, image_path: str, width_in: float = IMAGE_WIDTH_IN) -> bool:
    """Center an image, skipping silently when the file is missing.

    A missing render must not sink the whole export — the warning is already
    recorded on the book.
    """
    if not image_path or not Path(image_path).exists():
        return False
    # Last line of defence before an image is embedded: guarantee 300 DPI and
    # no AI/EXIF metadata even if the file arrived from outside the renderer
    # (hand-drawn art, a re-run, an edited replacement).
    # width_in is the size it is actually placed at, so this also upscales
    # anything with too few pixels to be a true 300 DPI there.
    # A corrupt asset must not abort the export; _add_image already reports
    # "no image" via its return value, which callers handle.
    try:
        sanitize_for_print(image_path, PRINT_DPI, width_in=width_in)
    except PrintHygieneError:
        return False
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para.add_run().add_picture(image_path, width=Inches(width_in))
    return True


def _body_paragraph(doc: Document, text: str = ""):
    """A paragraph the KDP formatter must treat as body text, not a heading.

    Puzzle content collides with the formatter's heading heuristics the same way
    trivia does: a numbered riddle or clue ("1. ...") reads as a subheading, a
    short answer choice or word list reads as an outline topic, and a choice
    lettered C or D reads as a roman-numeral chapter title.
    """
    para = doc.add_paragraph(text)
    para.style = doc.styles[BODY_TEXT_STYLE]
    return para


def _section_heading(doc: Document, text: str) -> None:
    doc.add_page_break()
    doc.add_heading(text, level=1)


def build_docx(book: PuzzleBook, path: Path) -> Path:
    # OOXML forbids control characters; one aborts the whole export.
    strip_control_chars(book)
    cfg = book.config
    doc = Document()
    ensure_body_text_style(doc)

    # Title page.
    title_para = doc.add_paragraph()
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title_para.add_run(cfg.book_title)
    run.bold = True
    run.font.size = Pt(28)

    # Declared body text: a short unpunctuated line like this otherwise reads
    # as an outline topic and becomes a 20pt subheading.
    sub = _body_paragraph(doc)
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub_run = sub.add_run(f"A puzzle and activity book about {cfg.topic}")
    sub_run.italic = True

    doc.add_page_break()
    doc.add_heading("How to Use This Book", level=1)
    doc.add_paragraph(
        f"This book is packed with puzzles about {cfg.topic}. Work through the "
        "sections in any order you like. Some puzzles are quick, others take a "
        "little longer, and every one of them has an answer waiting for you at "
        "the back of the book in the Answer Key."
    )

    # -- Section 1: picture puzzles (briefs only) -------------------------
    if book.picture_briefs and cfg.picture_briefs_only:
        _section_heading(doc, f"{_section_title('picture_puzzles')} — Illustrator Briefs")
        note = _body_paragraph(doc)
        note.add_run(
            "This section is drawn by hand. Each brief below becomes a "
            "left/right page pair of near-identical images, plus a solution "
            f"image with the differences circled. Specs: grayscale, "
            f"{PAGE_W_IN:g}x{PAGE_H_IN:g} in, {PRINT_DPI} DPI."
        ).italic = True
        for brief in book.picture_briefs:
            doc.add_heading(f"{brief.number}. {brief.scene_title}", level=2)
            _body_paragraph(doc, brief.scene_description)
            _body_paragraph(doc, "Differences to hide:")
            for i, idea in enumerate(brief.difference_ideas, start=1):
                para = _body_paragraph(doc, f"{i}. {idea}")
                para.paragraph_format.left_indent = Inches(0.25)

    # -- Section 2: mazes -------------------------------------------------
    if book.mazes:
        _section_heading(doc, _section_title("mazes"))
        for maze in book.mazes:
            doc.add_heading(maze.title, level=2)
            _add_image(doc, maze.image_path)
            doc.add_page_break()

    # -- Section 3: riddles -----------------------------------------------
    if book.riddles:
        _section_heading(doc, _section_title("riddles"))
        for riddle in book.riddles:
            para = _body_paragraph(doc)
            para.paragraph_format.space_after = Pt(10)
            para.add_run(f"{riddle.number}. ").bold = True
            para.add_run(riddle.riddle.replace("\n", "  "))

    # -- Section 4: word searches -----------------------------------------
    if book.word_searches:
        _section_heading(doc, _section_title("word_searches"))
        for ws in book.word_searches:
            doc.add_heading(f"{ws.number}. {ws.title}", level=2)
            if not _add_image(doc, ws.image_path):
                # Fall back to a text word list when the render is missing.
                _body_paragraph(doc, ", ".join(ws.words))
            doc.add_page_break()

    # -- Section 5: cryptograms -------------------------------------------
    if book.cryptograms:
        _section_heading(doc, _section_title("cryptograms"))
        doc.add_paragraph(
            "Each letter has been swapped for another letter. Crack the code "
            "to reveal the phrase."
        )
        for gram in book.cryptograms:
            doc.add_heading(f"Cryptogram {gram.number}", level=2)
            code = _body_paragraph(doc)
            code_run = code.add_run(gram.encoded)
            code_run.font.name = "Courier New"
            code_run.font.size = Pt(14)
            code_run.bold = True
            if gram.hint:
                hint = _body_paragraph(doc)
                hint.add_run(f"Hint: {gram.hint}").italic = True

    # -- Section 6: trivia ------------------------------------------------
    if book.trivia_chapters:
        _section_heading(doc, _section_title("trivia"))
        for chapter in book.trivia_chapters:
            # Not "Chapter N — Title": that shape is matched as a chapter
            # opener and promoted to Heading 1, breaking the section nesting.
            doc.add_heading(f"{chapter.title} (Chapter {chapter.number})", level=2)
            for q in chapter.questions:
                q_para = _body_paragraph(doc)
                q_para.paragraph_format.space_after = Pt(4)
                q_para.paragraph_format.keep_with_next = True
                # Only the number is bold; a whole question in bold reads as a
                # wall of heavy text on the page.
                q_para.add_run(f"{q.number}. ").bold = True
                q_para.add_run(q.question)
                for letter in LETTERS:
                    if letter not in q.choices:
                        continue
                    c_para = _body_paragraph(doc)
                    c_para.paragraph_format.left_indent = Inches(0.3)
                    c_para.paragraph_format.space_after = Pt(0)
                    c_para.paragraph_format.keep_with_next = letter != LETTERS[-1]
                    c_para.add_run(f"{letter}. {q.choices[letter]}")
                _body_paragraph(doc).paragraph_format.space_after = Pt(6)

    # -- Section 7: crosswords --------------------------------------------
    if book.crosswords:
        _section_heading(doc, _section_title("crosswords"))
        for cw in book.crosswords:
            doc.add_heading(f"{cw.number}. {cw.title}", level=2)
            if not _add_image(doc, cw.image_path):
                for label in ("across", "down"):
                    group = [e for e in cw.entries if e.direction == label]
                    if not group:
                        continue
                    _body_paragraph(doc, label.upper())
                    for e in sorted(group, key=lambda x: x.number):
                        _body_paragraph(doc, f"{e.number}. {e.clue}")
            doc.add_page_break()

    # -- Section 8: consolidated answer key -------------------------------
    _build_answer_key(doc, book)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def _build_answer_key(doc: Document, book: PuzzleBook) -> None:
    """Section 8 — every solution in the book, in section order."""
    _section_heading(doc, ANSWER_KEY_TITLE)

    if book.picture_briefs:
        doc.add_heading("Picture Puzzles", level=2)
        doc.add_paragraph(
            "Solution images are supplied by the illustrator, with each "
            "difference circled."
        )

    if book.mazes:
        doc.add_heading("Mazes", level=2)
        for maze in book.mazes:
            doc.add_heading(maze.title, level=3)
            _add_image(doc, maze.solution_path, width_in=3.4)

    if book.riddles:
        doc.add_heading("Riddles", level=2)
        for riddle in book.riddles:
            para = _body_paragraph(doc)
            para.paragraph_format.space_after = Pt(2)
            para.add_run(f"{riddle.number}. ").bold = True
            para.add_run(riddle.answer)

    if book.word_searches:
        doc.add_heading("Word Searches", level=2)
        for ws in book.word_searches:
            doc.add_heading(f"{ws.number}. {ws.title}", level=3)
            if not _add_image(doc, ws.solution_path, width_in=3.4):
                _body_paragraph(doc, ", ".join(ws.words))

    if book.cryptograms:
        doc.add_heading("Cryptograms", level=2)
        for gram in book.cryptograms:
            para = _body_paragraph(doc)
            para.paragraph_format.space_after = Pt(2)
            para.add_run(f"{gram.number}. ").bold = True
            para.add_run(gram.phrase)

    if book.trivia_chapters:
        doc.add_heading("Trivia", level=2)
        for chapter in book.trivia_chapters:
            doc.add_heading(f"{chapter.title} (Chapter {chapter.number})", level=3)
            for q in chapter.questions:
                para = _body_paragraph(doc)
                para.paragraph_format.space_after = Pt(2)
                para.add_run(f"{q.number}. ").bold = True
                para.add_run(f"{q.correct_answer} — {q.correct_text()}")

    if book.crosswords:
        doc.add_heading("Crosswords", level=2)
        for cw in book.crosswords:
            doc.add_heading(f"{cw.number}. {cw.title}", level=3)
            if not _add_image(doc, cw.solution_path, width_in=3.4):
                for e in sorted(cw.entries, key=lambda x: x.number):
                    _body_paragraph(doc, f"{e.number} {e.direction}. {e.word}")


# --------------------------------------------------------------------------
# Print-readiness verification
# --------------------------------------------------------------------------

def verify_print_images(book: PuzzleBook, job_dir: Path) -> list[str]:
    """Confirm every rendered image in ``job_dir`` is 300 DPI and metadata-free.

    Returns a list of human-readable problems and appends them to
    ``book.warnings``, so a bad asset shows up in the build log rather than
    reaching KDP unnoticed. An empty list means the whole tree is clean.
    """
    problems: list[str] = []
    for path, issues in audit_tree(job_dir, PRINT_DPI).items():
        rel = path.relative_to(job_dir) if path.is_relative_to(job_dir) else path
        problems.append(f"{rel}: {'; '.join(issues)}")

    for message in problems:
        book.warnings.append(f"Print check — {message}")
    return problems


# --------------------------------------------------------------------------
# KDP print files
# --------------------------------------------------------------------------

def build_kdp_files(
    book: PuzzleBook,
    source_docx: Path,
    out_dir: Path,
    *,
    author_placeholder: str = "Author Name",
) -> dict[str, str]:
    """Run the existing KDP formatter over the puzzle manuscript for 6x9 print."""
    from kdp_docx_formatter import build_kdp_documents

    from kdp_docx_formatter import canonical_title_key

    stem = source_docx.stem
    kindle_out = out_dir / f"{stem}_kindle.docx"
    paperback_out = out_dir / f"{stem}_paperback.docx"

    # The manuscript writes every real heading with doc.add_heading(), so the
    # headings already in the file are the authoritative outline. Passing them
    # turns the formatter's outline guard on, so a line that merely looks like a
    # heading stays body text.
    outline_topics = {
        canonical_title_key(p.text)
        for p in Document(str(source_docx)).paragraphs
        if (p.style.name or "").lower().startswith("heading") and p.text.strip()
    }
    outline_topics.discard("")

    kindle_path, paperback_path, estimated, inside = build_kdp_documents(
        source_docx=source_docx,
        kindle_output=kindle_out,
        paperback_output=paperback_out,
        estimated_pages=0,
        title_placeholder=book.config.book_title,
        author_placeholder=author_placeholder,
        outline_topics=outline_topics,
    )
    return {
        "kindle": str(kindle_path),
        "paperback": str(paperback_path),
        "estimated_pages": str(estimated),
        "inside_margin_in": f"{inside:.3f}",
    }


# --------------------------------------------------------------------------
# Final assembly — the formatter handoff ZIP
# --------------------------------------------------------------------------

def build_handoff_zip(book: PuzzleBook, job_dir: Path, zip_path: Path) -> Path:
    """Bundle everything a professional formatter needs.

    Puzzle art and solution art are kept in separate top-level folders so the
    formatter can place the answer key without hunting for which image belongs
    where.
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    job_dir = Path(job_dir)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Manuscripts and structured content.
        for name in ("puzzle_book.docx", "puzzle_book.md", "puzzle_book.json"):
            f = job_dir / name
            if f.exists():
                zf.write(f, f"manuscript/{name}")
        for f in job_dir.glob("*_kindle.docx"):
            zf.write(f, f"manuscript/{f.name}")
        for f in job_dir.glob("*_paperback.docx"):
            zf.write(f, f"manuscript/{f.name}")

        def _add(path_str: str, arcname: str) -> None:
            p = Path(path_str)
            if path_str and p.exists():
                # The formatter's copy must carry the same guarantees as the
                # manuscript's: exactly 300 DPI, no AI metadata.
                sanitize_for_print(p, PRINT_DPI)
                zf.write(p, arcname)

        for maze in book.mazes:
            _add(maze.image_path, f"puzzles/mazes/{Path(maze.image_path).name}")
            _add(maze.solution_path, f"solutions/mazes/{Path(maze.solution_path).name}")
        for ws in book.word_searches:
            _add(ws.image_path, f"puzzles/word_searches/{Path(ws.image_path).name}")
            _add(ws.solution_path, f"solutions/word_searches/{Path(ws.solution_path).name}")
        for cw in book.crosswords:
            _add(cw.image_path, f"puzzles/crosswords/{Path(cw.image_path).name}")
            _add(cw.solution_path, f"solutions/crosswords/{Path(cw.solution_path).name}")

        zf.writestr("README.txt", _handoff_readme(book))
        if book.picture_briefs:
            zf.writestr("illustrator_briefs.md", _illustrator_brief_doc(book))

    return zip_path


def _handoff_readme(book: PuzzleBook) -> str:
    cfg = book.config
    counts = book.counts()
    est = cfg.page_estimate()
    lines = [
        cfg.book_title,
        "=" * len(cfg.book_title),
        "",
        f"Topic:    {cfg.topic}",
        f"Audience: {cfg.audience}",
        "",
        "CONTENTS",
        "--------",
    ]
    for kind, label in SECTION_LABELS.items():
        n = counts.get(kind, 0)
        if n:
            suffix = " chapters (10 questions each)" if kind == "trivia" else ""
            lines.append(f"  {label}: {n}{suffix}")
    lines += [
        "",
        f"Estimated length: ~{est['total_pages']} pages "
        f"({est['puzzle_pages']} puzzle + {est['answer_key_pages']} answer key "
        f"+ {est['title_pages']} title/front matter)",
        "",
        "FOLDERS",
        "-------",
        "  manuscript/  DOCX + Markdown + JSON. The DOCX already contains the",
        "               full book including the consolidated Answer Key.",
        "  puzzles/     Puzzle artwork, grayscale PNG at 300 DPI, 6x9 in.",
        "  solutions/   Matching solution artwork for the Answer Key section.",
        "",
        "NOTES",
        "-----",
        "  - All artwork is grayscale, 300 DPI, sized for a 6x9 in trim.",
        "  - Every solution belongs in the Answer Key at the back of the book,",
        "    never beside its puzzle.",
    ]
    if book.picture_briefs:
        lines += [
            "  - Picture puzzles are NOT included as artwork. See",
            "    illustrator_briefs.md — those scenes are drawn by hand and",
            "    supplied separately as left/right page pairs plus a circled",
            "    solution image.",
        ]
    if book.warnings:
        lines += ["", "BUILD WARNINGS", "--------------"]
        lines += [f"  - {w}" for w in book.warnings]
    return "\n".join(lines) + "\n"


def _illustrator_brief_doc(book: PuzzleBook) -> str:
    lines = [
        f"# {book.config.book_title} — Picture Puzzle Briefs",
        "",
        f"{len(book.picture_briefs)} spot-the-difference scenes.",
        "",
        "For each scene, draw two near-identical images (a left/right page "
        "pair) plus a third solution image with every difference circled.",
        "",
        f"**Specs: grayscale, {PAGE_W_IN:g}x{PAGE_H_IN:g} in, {PRINT_DPI} DPI.**",
        "",
    ]
    for brief in book.picture_briefs:
        lines += [
            f"## {brief.number}. {brief.scene_title}",
            "",
            brief.scene_description,
            "",
            "Differences to hide:",
            "",
        ]
        lines += [f"{i}. {d}" for i, d in enumerate(brief.difference_ideas, start=1)]
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Print-ready interior (reference trade format)
# --------------------------------------------------------------------------

def build_interior_docx(
    book: PuzzleBook,
    path: Path,
    *,
    author: str = "Author Name",
    isbn: str = "",
    publisher: str = "",
    support_email: str = "",
) -> Path:
    """Write the 6x9 interior in the reference book's trade format.

    Unlike :func:`build_docx`, which produces a plain manuscript for the KDP
    formatter, this file already carries its own trim size, mirrored margins,
    section dividers, page numbers and Table of Contents — so it is handed to
    the printer as-is and must not be passed to ``build_kdp_files()``.
    """
    from .layout import build_formatted_docx

    return build_formatted_docx(
        book,
        path,
        author=author,
        isbn=isbn,
        publisher=publisher,
        support_email=support_email,
    )
