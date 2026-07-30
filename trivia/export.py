"""Trivia book exporters — Markdown, DOCX, and KDP 6x9 print files.

The DOCX built here is a plain manuscript: Heading 1 for chapters, Heading 2
for sub-sections, body text for items. It is then handed to the existing
kdp_docx_formatter.build_kdp_documents() for 6x9 sizing, gutters, TOC and page
numbers — that module is untouched and shared as-is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

from .engine import (
    ANSWER_KEY_END_OF_BOOK,
    ANSWER_KEY_END_OF_CHAPTER,
    Chapter,
    TriviaBook,
)

LETTERS = ("A", "B", "C", "D")

TRIVIA_SECTION_TITLE = "Trivia"
FACTS_SECTION_TITLE = "Did You Know"
ANSWER_KEY_TITLE = "Answer Key"


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

def to_markdown(book: TriviaBook) -> str:
    cfg = book.config
    lines: list[str] = [f"# {cfg.book_title}", ""]

    if cfg.topic:
        lines += [f"*A trivia and facts collection about {cfg.topic}.*", ""]

    for chapter in book.chapters:
        lines += [f"## Chapter {chapter.number} — {chapter.title}", ""]
        if chapter.illustration_path:
            lines += [f"![Chapter {chapter.number} illustration]({chapter.illustration_path})", ""]

        if chapter.trivia:
            lines += [f"### {TRIVIA_SECTION_TITLE}", ""]
            for i, q in enumerate(chapter.trivia, start=1):
                lines.append(f"**{i}. {q.question}**")
                lines.append("")
                for letter in LETTERS:
                    if letter in q.choices:
                        lines.append(f"{letter}. {q.choices[letter]}")
                lines.append("")

        if chapter.facts:
            lines += [f"### {FACTS_SECTION_TITLE}", ""]
            for f in chapter.facts:
                lines.append(f"- {f.fact}")
            lines.append("")

        if cfg.answer_key_position == ANSWER_KEY_END_OF_CHAPTER and chapter.trivia:
            lines += [f"### {ANSWER_KEY_TITLE} — Chapter {chapter.number}", ""]
            for i, q in enumerate(chapter.trivia, start=1):
                lines.append(f"{i}. {q.correct_answer} - {q.correct_text()}")
            lines.append("")

    if book.config.answer_key_position == ANSWER_KEY_END_OF_BOOK:
        lines += [f"## {ANSWER_KEY_TITLE}", ""]
        for chapter in book.chapters:
            if not chapter.trivia:
                continue
            lines += [f"### Chapter {chapter.number} — {chapter.title}", ""]
            for i, q in enumerate(chapter.trivia, start=1):
                lines.append(f"{i}. {q.correct_answer} - {q.correct_text()}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_markdown(book: TriviaBook, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(book), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------

def _add_heading(doc: Document, text: str, level: int) -> None:
    doc.add_heading(text, level=level)


def _add_answer_key_block(doc: Document, chapters: list[Chapter], *, heading_level: int) -> None:
    for chapter in chapters:
        if not chapter.trivia:
            continue
        _add_heading(doc, f"Chapter {chapter.number} — {chapter.title}", heading_level)
        for i, q in enumerate(chapter.trivia, start=1):
            para = doc.add_paragraph()
            para.paragraph_format.space_after = Pt(2)
            para.add_run(f"{i}. ").bold = True
            para.add_run(f"{q.correct_answer} - {q.correct_text()}")


def build_docx(book: TriviaBook, path: Path, *, image_width_in: float = 4.5) -> Path:
    """Plain manuscript DOCX. Styling/sizing is left to the KDP formatter."""
    cfg = book.config
    doc = Document()

    # Front matter: title page.
    title_para = doc.add_paragraph()
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_para.add_run(cfg.book_title)
    title_run.bold = True
    title_run.font.size = Pt(28)

    if cfg.topic:
        sub = doc.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub_run = sub.add_run(f"A trivia and facts collection about {cfg.topic}")
        sub_run.italic = True
        sub_run.font.size = Pt(13)

    doc.add_page_break()

    # Introduction — short and generic so it fits any subject.
    _add_heading(doc, "Introduction", 1)
    doc.add_paragraph(
        f"This book collects trivia questions and surprising facts about "
        f"{cfg.topic}. Each chapter opens with a round of multiple-choice "
        f"questions, then a set of Did You Know facts. "
        + (
            "Answers for every chapter are gathered in the answer key at the "
            "back of the book."
            if cfg.answer_key_position == ANSWER_KEY_END_OF_BOOK
            else "Answers appear at the end of each chapter."
        )
    )
    doc.add_page_break()

    for chapter in book.chapters:
        _add_heading(doc, f"Chapter {chapter.number} — {chapter.title}", 1)

        if chapter.illustration_path and Path(chapter.illustration_path).exists():
            pic_para = doc.add_paragraph()
            pic_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pic_para.add_run().add_picture(
                str(chapter.illustration_path), width=Inches(image_width_in)
            )

        if chapter.trivia:
            _add_heading(doc, TRIVIA_SECTION_TITLE, 2)
            for i, q in enumerate(chapter.trivia, start=1):
                q_para = doc.add_paragraph()
                q_para.paragraph_format.space_after = Pt(4)
                q_para.add_run(f"{i}. {q.question}").bold = True
                for letter in LETTERS:
                    if letter not in q.choices:
                        continue
                    c_para = doc.add_paragraph()
                    c_para.paragraph_format.left_indent = Inches(0.3)
                    c_para.paragraph_format.space_after = Pt(0)
                    c_para.add_run(f"{letter}. {q.choices[letter]}")
                doc.add_paragraph().paragraph_format.space_after = Pt(6)

        if chapter.facts:
            _add_heading(doc, FACTS_SECTION_TITLE, 2)
            for f in chapter.facts:
                para = doc.add_paragraph(f.fact, style="List Bullet")
                para.paragraph_format.space_after = Pt(3)

        if cfg.answer_key_position == ANSWER_KEY_END_OF_CHAPTER and chapter.trivia:
            _add_heading(doc, f"{ANSWER_KEY_TITLE} — Chapter {chapter.number}", 2)
            for i, q in enumerate(chapter.trivia, start=1):
                para = doc.add_paragraph()
                para.paragraph_format.space_after = Pt(2)
                para.add_run(f"{i}. ").bold = True
                para.add_run(f"{q.correct_answer} - {q.correct_text()}")

        doc.add_page_break()

    if cfg.answer_key_position == ANSWER_KEY_END_OF_BOOK:
        _add_heading(doc, ANSWER_KEY_TITLE, 1)
        _add_answer_key_block(doc, book.chapters, heading_level=2)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def build_kdp_files(
    book: TriviaBook,
    source_docx: Path,
    out_dir: Path,
    *,
    author_placeholder: str = "Author Name",
) -> dict[str, str]:
    """Run the existing KDP formatter over the trivia manuscript for 6x9 print."""
    from kdp_docx_formatter import build_kdp_documents

    stem = source_docx.stem
    kindle_out = out_dir / f"{stem}_kindle.docx"
    paperback_out = out_dir / f"{stem}_paperback.docx"

    kindle_path, paperback_path, estimated, inside = build_kdp_documents(
        source_docx=source_docx,
        kindle_output=kindle_out,
        paperback_output=paperback_out,
        estimated_pages=0,
        title_placeholder=book.config.book_title,
        author_placeholder=author_placeholder,
        outline_topics=set(),
    )
    return {
        "kindle": str(kindle_path),
        "paperback": str(paperback_path),
        "estimated_pages": str(estimated),
        "inside_margin_in": f"{inside:.3f}",
    }
