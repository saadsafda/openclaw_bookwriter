"""
pub_listing_agent.py

Generates KDP publishing listing assets for a finished book:
  1. Subtitle ideas (5-10) using proven frameworks
  2. Book description (170-220 words) for the Amazon listing / back cover
  3. Category selection — 3 Kindle + 3 Paperback categories from CSV lists

Uses the OpenClaw CLI with the "pub-listing-agent-1" agent.

Usage (CLI):
  python pub_listing_agent.py /path/to/finished_book.docx
  python pub_listing_agent.py /path/to/finished_book.docx --agent pub-listing-agent-1
  python pub_listing_agent.py /path/to/finished_book.docx --title "My Book Title"

Usage (as module):
  from pub_listing_agent import generate_listing
  result = generate_listing(docx_path, title="My Book", agent_id="pub-listing-agent-1")
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from docx import Document

from openclaw_docx_writer import parse_openclaw_reply

# ── Constants ───────────────────────────────────────────────────────

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
EBOOK_CATEGORIES_CSV = DATA_DIR / "ebook_categories.csv"
PAPERBACK_CATEGORIES_CSV = DATA_DIR / "paperback_categories.csv"

DEFAULT_AGENT_ID = "pub-listing-agent-1"
DEFAULT_TIMEOUT = 180

# ── Data classes ────────────────────────────────────────────────────


@dataclass
class ListingResult:
    title: str
    subtitles: list[str] = field(default_factory=list)
    description: str = ""
    ebook_categories: list[str] = field(default_factory=list)
    paperback_categories: list[str] = field(default_factory=list)
    raw_subtitles_response: str = ""
    raw_description_response: str = ""
    raw_categories_response: str = ""


# ── Helpers: extract book content ───────────────────────────────────


def _extract_outline_and_intro(docx_path: Path) -> tuple[str, str, str]:
    """Extract the book title, chapter outline, and introduction text.

    Returns (title, outline_text, intro_text).
    """
    doc = Document(str(docx_path))

    title = ""
    outline_lines: list[str] = []
    intro_paragraphs: list[str] = []
    first_chapter_paragraphs: list[str] = []
    in_intro = False
    in_first_chapter = False
    intro_done = False
    heading_count = 0

    # Patterns that are KDP front-matter placeholders, not real content
    _PLACEHOLDER_RE = re.compile(
        r"^(Book\s+Title\s+Placeholder|TABLE\s+OF\s+CONTENTS|FREE\s+BONUS|ISBN:|Copyright|All\s+Rights\s+Reserved)",
        re.IGNORECASE,
    )

    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue

        style_name = (p.style.name or "").strip().lower()

        # Detect headings
        is_heading = style_name.startswith("heading")
        is_chapter = bool(re.match(
            r"^(Chapter\s+\d+|Introduction|Conclusion|Epilogue|Foreword|Preface|Prologue)\b",
            text, re.IGNORECASE
        ))

        if is_heading or is_chapter:
            # Skip KDP front-matter placeholder headings
            if _PLACEHOLDER_RE.match(text):
                continue
            heading_count += 1
            if heading_count == 1 and not title:
                title = text
            outline_lines.append(text)

            # Check if this heading starts the introduction
            if re.match(r"^Introduction\b", text, re.IGNORECASE):
                in_intro = True
                in_first_chapter = False
                intro_done = False
                continue

            # First chapter body as fallback intro
            if heading_count == 1 and not in_intro:
                in_first_chapter = True

            # Any heading after intro/first chapter means we're done collecting
            if in_intro and heading_count > 2:
                in_intro = False
                intro_done = True
            if in_first_chapter and heading_count > 1:
                in_first_chapter = False
            continue

        # Collect intro body text
        if in_intro and not intro_done:
            intro_paragraphs.append(text)

        # Collect first chapter body as fallback
        if in_first_chapter and len(first_chapter_paragraphs) < 10:
            first_chapter_paragraphs.append(text)

    outline_text = "\n".join(outline_lines)
    # Use introduction text if found, otherwise fall back to first chapter opening
    intro_text = "\n\n".join(intro_paragraphs) if intro_paragraphs else "\n\n".join(first_chapter_paragraphs)

    return title, outline_text, intro_text


# ── Helpers: load categories ────────────────────────────────────────


def _load_categories(csv_path: Path) -> list[str]:
    """Load category paths from a CSV file."""
    categories: list[str] = []
    if not csv_path.exists():
        return categories

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if row:
                cat = row[0].strip().strip('"')
                if cat:
                    categories.append(cat)
    return categories


def _filter_long_tail(categories: list[str], min_depth: int = 3) -> list[str]:
    """Filter to categories with at least min_depth levels (number of '>' separators + 1)."""
    return [c for c in categories if c.count(">") >= min_depth]


def _get_top_level_parent(category: str) -> str:
    """Extract the top-level parent from a category path like 'Books > Humor > ...'."""
    parts = [p.strip() for p in category.split(">")]
    # The first part is the store root (e.g. "Books" or "Kindle eBooks"),
    # the second part is the real top-level parent
    if len(parts) >= 2:
        return parts[1]
    return parts[0]


# ── Helpers: OpenClaw CLI ───────────────────────────────────────────


def _call_openclaw(agent_id: str, message: str, timeout_s: int = DEFAULT_TIMEOUT) -> str:
    """Call OpenClaw CLI and return the raw stdout."""
    cmd = ["openclaw", "agent", "--agent", agent_id, "--message", message, "--json"]
    if timeout_s > 0:
        cmd += ["--timeout", str(timeout_s)]

    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"OpenClaw failed (exit {p.returncode}).\n"
            f"STDERR:\n{p.stderr}\n"
            f"STDOUT:\n{p.stdout[:500]}"
        )
    return p.stdout


def _parse_reply(stdout: str) -> str:
    """Extract the text reply from OpenClaw JSON output.

    Delegates to the proven parser in openclaw_docx_writer which handles
    multi-line JSON, nested payloads, and various response shapes.
    """
    return parse_openclaw_reply(stdout)


# ── Step 1: Subtitle generation ─────────────────────────────────────


def generate_subtitles(
    title: str,
    outline: str,
    intro: str,
    agent_id: str = DEFAULT_AGENT_ID,
    timeout_s: int = DEFAULT_TIMEOUT,
    extra_context: str = "",
) -> tuple[list[str], str]:
    """Generate 5-10 subtitle ideas. Returns (subtitles_list, raw_response)."""

    context_block = ""
    if extra_context.strip():
        context_block = f"\n\nIMPORTANT CONTEXT FROM THE AUTHOR (must shape every subtitle):\n{extra_context.strip()}\n"

    prompt = f"""You are a KDP book subtitle specialist. Generate 5-10 subtitle ideas for this book.

BOOK TITLE: {title}{context_block}

BOOK OUTLINE:
{outline}

INTRODUCTION EXCERPT:
{intro[:2000]}

Use these proven subtitle frameworks:

1. "Discover" Framework — "Discover ___, ___, ___, and More!"
   Example: "Discover Amazing Facts, Wild Stories, and Mind-Blowing Records"

2. "Everything You Need to…" Framework — "Everything You Need to ___, ___, and ___"
   Example: "Everything You Need to Know About Drawing, Sketching, and Creating"

3. "Learn, Discover, Master" Framework — "Learn ___, Discover ___, and Master ___"
   Example: "Learn Skills, Discover Secrets, and Master the Game"

You may also create variations or mix frameworks if they fit the book better.

Return ONLY the numbered list of subtitle ideas, one per line. Example format:
1. Discover ..., ..., and More!
2. Everything You Need to ...
(etc.)"""

    raw = _call_openclaw(agent_id, prompt, timeout_s)
    reply = _parse_reply(raw)

    # Parse numbered lines
    subtitles: list[str] = []
    for line in reply.splitlines():
        line = line.strip()
        m = re.match(r"^\d+[\.\)]\s*(.+)", line)
        if m:
            subtitles.append(m.group(1).strip())

    return subtitles, reply


# ── Step 2: Description generation ──────────────────────────────────


def generate_description(
    title: str,
    outline: str,
    intro: str,
    agent_id: str = DEFAULT_AGENT_ID,
    timeout_s: int = DEFAULT_TIMEOUT,
    extra_context: str = "",
) -> tuple[str, str]:
    """Generate a 170-220 word Amazon book description. Returns (description, raw_response)."""

    context_block = ""
    if extra_context.strip():
        context_block = f"\n\nIMPORTANT CONTEXT FROM THE AUTHOR (must shape tone, voice, and audience of the description):\n{extra_context.strip()}\n"

    prompt = f"""You are writing an Amazon KDP book description. It MUST be 170-220 words.

BOOK TITLE: {title}{context_block}

BOOK OUTLINE:
{outline}

INTRODUCTION EXCERPT:
{intro[:2000]}

Follow this EXACT 5-section structure:

SECTION 1 — THE HOOK (2-3 sentences)
Open by speaking directly to the reader's situation. Name the challenge or life change they're facing so they feel seen. End by surfacing the core problem: overwhelm, confusion, or not knowing where to begin.

SECTION 2 — THE BOOK INTRODUCTION + PROMISE (2 sentences)
Introduce the book title (mention it ONCE — this is its only appearance). Clearly state what the book delivers. Transition into the bullet list with "Inside, you'll learn:" or similar.

SECTION 3 — KEY TAKEAWAYS (bullet list)
List 5-7 specific things the reader will learn or gain. Keep them concrete and varied enough to show the book's range. Mix practical how-tos with emotional or informational topics. Use ● as the bullet character.

SECTION 4 — THE EMOTIONAL PAYOFF (2-3 sentences)
Paint a picture of the positive transformation waiting on the other side. Remind the reader why this matters and position the book as their starting point.

SECTION 5 — CALL TO ACTION (exactly 2 lines)
A short, energizing question followed by a direct purchase instruction.
Example: "Are you ready to [benefit]? Scroll up, click on "Buy Now with 1-Click," and get your copy now!"

IMPORTANT: The total word count MUST be between 170 and 220 words. Do NOT include section labels in the output. Write it as one continuous description."""

    raw = _call_openclaw(agent_id, prompt, timeout_s)
    reply = _parse_reply(raw)

    return reply, raw


# ── Step 3: Category selection ──────────────────────────────────────


def select_categories(
    title: str,
    outline: str,
    intro: str,
    agent_id: str = DEFAULT_AGENT_ID,
    timeout_s: int = DEFAULT_TIMEOUT,
    extra_context: str = "",
) -> tuple[list[str], list[str], str]:
    """Select 3 ebook + 3 paperback categories. Returns (ebook_cats, paperback_cats, raw_response)."""

    # Load and filter categories
    ebook_all = _load_categories(EBOOK_CATEGORIES_CSV)
    paperback_all = _load_categories(PAPERBACK_CATEGORIES_CSV)

    ebook_deep = _filter_long_tail(ebook_all, min_depth=3)
    paperback_deep = _filter_long_tail(paperback_all, min_depth=3)

    # Build a condensed list for the prompt (send only deep categories to save tokens)
    ebook_list = "\n".join(ebook_deep)
    paperback_list = "\n".join(paperback_deep)

    context_block = ""
    if extra_context.strip():
        context_block = f"\n\nIMPORTANT CONTEXT FROM THE AUTHOR (must shape category choices — audience, genre, age group, etc.):\n{extra_context.strip()}\n"

    prompt = f"""You are a KDP category specialist. Select the best categories for this book.

BOOK TITLE: {title}{context_block}

BOOK OUTLINE:
{outline}

INTRODUCTION EXCERPT:
{intro[:1500]}

YOUR TASK: Pick exactly 3 Kindle ebook categories AND 3 Paperback categories from the lists below.

TWO MANDATORY RULES:

RULE 1 — ALWAYS GO LONG-TAIL:
Never select broad top-level categories. Go as deep as possible while staying relevant.
BAD: "Books > Business & Money" (too broad)
GOOD: "Books > Business & Money > Economics > Unemployment" (4 levels deep, much smaller bestseller list)

RULE 2 — SPREAD ACROSS DIFFERENT TOP-LEVEL PARENTS:
Each of your 3 categories MUST be under a DIFFERENT top-level parent category.
BAD: All 3 under "Humor & Entertainment" (same shoppers see you 3 times)
GOOD: One in "Humor & Entertainment", one in "Reference", one in "Science & Math" (3 different customer pools)

Every category must still genuinely fit the book's content.

=== KINDLE EBOOK CATEGORIES ===
{ebook_list}

=== PAPERBACK CATEGORIES ===
{paperback_list}

Return your answer in this EXACT format (copy the full category path from the lists above):

EBOOK CATEGORIES:
1. [full category path]
2. [full category path]
3. [full category path]

PAPERBACK CATEGORIES:
1. [full category path]
2. [full category path]
3. [full category path]"""

    raw = _call_openclaw(agent_id, prompt, timeout_s)
    reply = _parse_reply(raw)

    # Parse the response
    ebook_cats: list[str] = []
    paperback_cats: list[str] = []
    current_section: Optional[str] = None

    for line in reply.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^EBOOK\s+CATEGOR", line, re.IGNORECASE):
            current_section = "ebook"
            continue
        if re.match(r"^PAPERBACK\s+CATEGOR", line, re.IGNORECASE):
            current_section = "paperback"
            continue

        m = re.match(r"^\d+[\.\)]\s*(.+)", line)
        if m:
            cat = m.group(1).strip()
            if current_section == "ebook":
                ebook_cats.append(cat)
            elif current_section == "paperback":
                paperback_cats.append(cat)

    # Validate categories exist in the actual lists (normalize whitespace around '>')
    def _norm(cat: str) -> str:
        return re.sub(r"\s*>\s*", " > ", cat.strip())

    ebook_norm_map = {_norm(c): c for c in ebook_all}
    paperback_norm_map = {_norm(c): c for c in paperback_all}

    validated_ebook: list[str] = []
    for c in ebook_cats:
        key = _norm(c)
        if key in ebook_norm_map:
            validated_ebook.append(ebook_norm_map[key])

    validated_paperback: list[str] = []
    for c in paperback_cats:
        key = _norm(c)
        if key in paperback_norm_map:
            validated_paperback.append(paperback_norm_map[key])

    return validated_ebook, validated_paperback, reply


# ── Main orchestrator ───────────────────────────────────────────────


def generate_listing(
    docx_path: Path,
    title: str = "",
    agent_id: str = DEFAULT_AGENT_ID,
    timeout_s: int = DEFAULT_TIMEOUT,
    callback=None,
    extra_context: str = "",
) -> ListingResult:
    """Run all three listing steps and return the combined result.

    Args:
        docx_path: Path to the finished book .docx
        title: Override title (if empty, extracted from the doc)
        agent_id: OpenClaw agent id
        timeout_s: Timeout per API call
        callback: Optional callable(step_name, message) for progress updates
        extra_context: Optional free-form author guidance (audience, tone, etc.)
            applied to subtitles, description, and category selection.
    """
    if not docx_path.exists():
        raise FileNotFoundError(f"Book not found: {docx_path}")

    def _log(step: str, msg: str) -> None:
        if callback:
            callback(step, msg)
        else:
            print(f"[{step}] {msg}")

    # Extract content from the book
    _log("extract", "Reading book content...")
    doc_title, outline, intro = _extract_outline_and_intro(docx_path)
    book_title = title or doc_title or "Untitled Book"

    _log("extract", f"Title: {book_title}")
    _log("extract", f"Outline: {len(outline.splitlines())} headings found")
    _log("extract", f"Introduction: {len(intro.split())} words")
    if extra_context.strip():
        _log("extract", f"Author guidance: {extra_context.strip()[:120]}")

    result = ListingResult(title=book_title)

    # Step 1: Subtitles
    _log("subtitles", "Generating subtitle ideas...")
    subtitles, raw_sub = generate_subtitles(
        book_title, outline, intro, agent_id, timeout_s, extra_context=extra_context
    )
    result.subtitles = subtitles
    result.raw_subtitles_response = raw_sub
    _log("subtitles", f"Generated {len(subtitles)} subtitle ideas")

    # Step 2: Description
    _log("description", "Writing book description (170-220 words)...")
    description, raw_desc = generate_description(
        book_title, outline, intro, agent_id, timeout_s, extra_context=extra_context
    )
    result.description = description
    result.raw_description_response = raw_desc
    word_count = len(description.split())
    _log("description", f"Description: {word_count} words")

    # Step 3: Categories
    _log("categories", "Selecting categories (3 ebook + 3 paperback)...")
    ebook_cats, pb_cats, raw_cats = select_categories(
        book_title, outline, intro, agent_id, timeout_s, extra_context=extra_context
    )
    result.ebook_categories = ebook_cats
    result.paperback_categories = pb_cats
    result.raw_categories_response = raw_cats
    _log("categories", f"Ebook categories: {len(ebook_cats)}, Paperback categories: {len(pb_cats)}")

    return result


def _format_result(result: ListingResult) -> str:
    """Format the listing result as a readable text block."""
    lines: list[str] = []
    lines.append(f"{'='*60}")
    lines.append(f"PUBLISHING LISTING — {result.title}")
    lines.append(f"{'='*60}")

    lines.append("\n── SUBTITLE IDEAS ──")
    for i, sub in enumerate(result.subtitles, 1):
        lines.append(f"  {i}. {sub}")
    if not result.subtitles:
        lines.append("  (none generated)")

    lines.append("\n── BOOK DESCRIPTION ──")
    lines.append(result.description or "(none generated)")
    word_count = len(result.description.split()) if result.description else 0
    lines.append(f"\n  [Word count: {word_count}]")

    lines.append("\n── EBOOK CATEGORIES ──")
    for i, cat in enumerate(result.ebook_categories, 1):
        lines.append(f"  {i}. {cat}")
    if not result.ebook_categories:
        lines.append("  (none selected)")

    lines.append("\n── PAPERBACK CATEGORIES ──")
    for i, cat in enumerate(result.paperback_categories, 1):
        lines.append(f"  {i}. {cat}")
    if not result.paperback_categories:
        lines.append("  (none selected)")

    lines.append(f"\n{'='*60}")
    return "\n".join(lines)


# ── CLI entry point ─────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate KDP publishing listing: subtitles, description, and categories."
    )
    ap.add_argument("input", help="Path to the finished book .docx")
    ap.add_argument("--title", default="", help="Override book title")
    ap.add_argument("--agent", default=DEFAULT_AGENT_ID, help=f"OpenClaw agent id (default: {DEFAULT_AGENT_ID})")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Timeout per API call in seconds")
    ap.add_argument("--context", default="", help="Optional author guidance (audience, tone, etc.)")
    ap.add_argument("--json-output", default="", help="Save result as JSON to this path")
    args = ap.parse_args()

    docx_path = Path(args.input)
    if not docx_path.exists():
        print(f"ERROR: File not found: {docx_path}", file=sys.stderr)
        return 2

    result = generate_listing(
        docx_path=docx_path,
        title=args.title,
        agent_id=args.agent,
        timeout_s=args.timeout,
        extra_context=args.context,
    )

    print(_format_result(result))

    if args.json_output:
        out_path = Path(args.json_output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps({
                "title": result.title,
                "subtitles": result.subtitles,
                "description": result.description,
                "ebook_categories": result.ebook_categories,
                "paperback_categories": result.paperback_categories,
            }, indent=2),
            encoding="utf-8",
        )
        print(f"\nJSON saved to: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
