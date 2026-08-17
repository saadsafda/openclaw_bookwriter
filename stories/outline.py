"""Outline import — turn an existing outline document into story entries.

The operator's outline already exists as a Word document or a pasted block of
text before this pipeline ever sees it. Retyping a hundred entries into a web
form is the slowest part of making one of these books, so this module reads the
document and produces the same story dicts the config expects.

The parser is deliberately forgiving. Outlines are written by people, not
generated, so it recognises several numbering styles and treats labelled lines
("Who:", "Year:", "Where:") as structured hints while folding everything else
into the free-form context box. Anything it cannot classify still lands in the
context rather than being dropped — losing the operator's research is far worse
than an untidy context field.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .engine import StoryError

# A numbered story heading: "1. Title", "1) Title", "Story 3 — Title".
_NUMBERED = re.compile(
    r"^\s*(?:story\s+)?(\d{1,3})\s*[.)\]:—–-]\s*(.+?)\s*$",
    re.IGNORECASE,
)

# A chapter heading: "Chapter 2: Bank Jobs", "Part One — ...", "Section 3".
_CHAPTER = re.compile(
    r"^\s*(?:chapter|part|section)\s+([\w]+)\s*[:.)—–-]?\s*(.*)$",
    re.IGNORECASE,
)

# "Who: ...", "**Year:** ...", "The Story: ..." — a labelled field line.
_LABELLED = re.compile(
    r"^\s*\**\s*(who|year|when|where|location|the story|story|context|summary|"
    r"details|notes|why it was so dumb|why|research sources|sources|source)\s*"
    r"\**\s*[:：]\s*(.*)$",
    re.IGNORECASE,
)

# Lines that introduce the document rather than a story, so a leading paragraph
# of preamble is not mistaken for the first story's context.
_SKIP_PREFIXES = (
    "book outline",
    "outline —",
    "outline -",
)

# Headings that end the story list and begin production notes about the book.
# Everything after one of these is guidance for the humans making the book, not
# facts about the last story — folding it into that story's context would feed
# the writer instructions as if they were research.
_TRAILER_HEADINGS = (
    "notes for the",
    "notes on the",
    "research & rewrite",
    "research and rewrite",
    "production notes",
    "general notes",
    "tone target",
    "mix check",
)


def _is_trailer(line: str) -> bool:
    lowered = line.lower()
    return any(lowered.startswith(prefix) for prefix in _TRAILER_HEADINGS)

# Which config field each recognised label feeds.
_LABEL_FIELDS = {
    "who": "who",
    "year": "year",
    "when": "year",
    "where": "where",
    "location": "where",
    "research sources": "sources",
    "sources": "sources",
    "source": "sources",
}

# Labels whose text belongs in the free-form context box. "Why it was so dumb"
# is the book's editorial angle, which the writer needs as much as the facts.
_CONTEXT_LABELS = {
    "the story", "story", "context", "summary", "details", "notes",
    "why it was so dumb", "why",
}


def _read_docx(path: Path) -> list[str]:
    """Paragraph text from a .docx, including table cells.

    Some outlines are laid out as a table with one row per story, so ignoring
    tables would silently return an empty outline for those documents.
    """
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise StoryError("python-docx is not installed.") from exc

    try:
        doc = Document(str(path))
    except Exception as exc:
        raise StoryError(f"Could not read that Word document: {exc}") from exc

    lines = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                lines.extend(p.text for p in cell.paragraphs)
    return lines


def _clean(text: str) -> str:
    """Strip markdown emphasis and bullet markers a heading may carry."""
    s = (text or "").strip()
    s = re.sub(r"^[-*•‣]\s*", "", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"^#{1,6}\s*", "", s)
    return s.strip()


def _is_noise(line: str) -> bool:
    lowered = line.lower()
    return any(lowered.startswith(prefix) for prefix in _SKIP_PREFIXES)


def parse_outline_lines(lines: list[str]) -> dict[str, Any]:
    """Group raw outline lines into stories, and stories into chapters.

    Returns the shape the config accepts: {"chapters": [...], "stories": [...]}
    with `stories` flattened for a UI that shows one list.
    """
    chapters: list[dict[str, Any]] = []
    current_chapter: Optional[dict[str, Any]] = None
    current_story: Optional[dict[str, Any]] = None
    # Where an unlabelled continuation line should go. Once "The Story:" has
    # been seen, following prose belongs to the context rather than restarting.
    context_parts: list[str] = []
    title_seen = False

    def _flush_story() -> None:
        nonlocal current_story, context_parts
        if current_story is None:
            return
        extra = "\n".join(p for p in context_parts if p.strip()).strip()
        if extra:
            existing = current_story.get("context", "")
            current_story["context"] = f"{existing}\n{extra}".strip() if existing else extra
        current_story["context"] = current_story.get("context", "").strip()
        if current_chapter is not None:
            current_chapter["stories"].append(current_story)
        current_story = None
        context_parts = []

    def _ensure_chapter() -> dict[str, Any]:
        nonlocal current_chapter
        if current_chapter is None:
            current_chapter = {
                "chapter_number": len(chapters) + 1,
                "chapter_title": "",
                "stories": [],
            }
            chapters.append(current_chapter)
        return current_chapter

    # Production notes found after the last story. Returned separately so the
    # UI can offer them as the book-wide context instead of losing them.
    trailer_notes: list[str] = []
    in_trailer = False

    for raw in lines:
        line = _clean(raw)
        if not line:
            continue

        # Once the production-notes section starts, no further story content
        # follows, so everything left is book-level guidance.
        if _is_trailer(line):
            _flush_story()
            in_trailer = True
        if in_trailer:
            trailer_notes.append(line)
            continue

        # A chapter heading closes the story before it. Checked ahead of the
        # numbered pattern so "Chapter 2: ..." is never read as story 2.
        chapter_match = _CHAPTER.match(line)
        if chapter_match and not _NUMBERED.match(line):
            _flush_story()
            title = _clean(chapter_match.group(2)) or f"Chapter {chapter_match.group(1)}"
            current_chapter = {
                "chapter_number": len(chapters) + 1,
                "chapter_title": title,
                "stories": [],
            }
            chapters.append(current_chapter)
            title_seen = True
            continue

        numbered = _NUMBERED.match(line)
        if numbered:
            candidate = _clean(numbered.group(2))
            # A numbered line that is really a labelled field ("1. Who: ...")
            # belongs to the story above it, not a new one.
            if candidate and not _LABELLED.match(candidate):
                _flush_story()
                _ensure_chapter()
                current_story = {
                    "number": int(numbered.group(1)),
                    "title": candidate,
                    "context": "",
                    "who": "",
                    "year": "",
                    "where": "",
                    "sources": "",
                }
                title_seen = True
                continue
            line = candidate or line

        labelled = _LABELLED.match(line)
        if labelled and current_story is not None:
            label = labelled.group(1).lower()
            value = _clean(labelled.group(2))
            field = _LABEL_FIELDS.get(label)
            if field:
                # A second "Sources:" line appends rather than overwrites, so a
                # multi-line source list survives intact.
                existing = current_story.get(field, "")
                current_story[field] = f"{existing}; {value}".strip("; ") if existing else value
            elif label in _CONTEXT_LABELS and value:
                context_parts.append(value)
            continue

        # Anything else is prose. It belongs to the open story's context, or is
        # document preamble to ignore.
        if current_story is not None:
            context_parts.append(line)
        elif not title_seen and _is_noise(line):
            continue

    _flush_story()

    # Drop chapters that ended up with no stories — an outline with a trailing
    # "Notes for the research phase" heading would otherwise produce an empty one.
    chapters = [c for c in chapters if c["stories"]]

    if not chapters:
        raise StoryError(
            "No stories were found in that outline. Each story needs a numbered "
            "heading like '1. The $1 Bank Robbery'."
        )

    # Renumber sequentially across the book. The source document may restart at
    # 1 in every chapter, which would collide once flattened.
    counter = 1
    flat: list[dict[str, Any]] = []
    for chapter in chapters:
        for story in chapter["stories"]:
            story["number"] = counter
            counter += 1
            flat.append(story)

    return {
        "chapters": chapters,
        "stories": flat,
        "story_count": len(flat),
        "chapter_count": len(chapters),
        "with_context": sum(1 for s in flat if s.get("context")),
        # Offered to the operator as the book-wide notes field rather than
        # applied automatically — it is guidance about the book, and only a
        # human can say which of it should steer the writer.
        "notes": "\n".join(trailer_notes).strip(),
    }


def parse_outline_text(text: str) -> dict[str, Any]:
    return parse_outline_lines((text or "").splitlines())


def parse_outline_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return parse_outline_lines(_read_docx(path))
    if suffix in {".txt", ".md"}:
        return parse_outline_text(path.read_text(encoding="utf-8", errors="replace"))
    raise StoryError("Outline must be a .docx, .txt or .md file.")
