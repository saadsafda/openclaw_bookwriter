"""Preview & Edit support for story books.

Applies manual and AI-assisted edits against a book's stored JSON — the source
of truth — so nothing here regenerates a whole book. Re-export is a separate,
explicit step.

AI edits reuse the same openclaw path and raw-output cache as generation, so an
identical edit request is never billed twice.
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import engine, pipeline
from .engine import (
    Chapter,
    RawOutputCache,
    Story,
    StoryBook,
    StoryConfig,
    StoryError,
    UsageLedger,
)

# AI actions the editor exposes. An explicit allowlist so an arbitrary action
# string from the client can never reach the prompt builder.
AI_ACTIONS = {
    "rewrite",       # same facts, fresh prose
    "expand",        # longer, more detail
    "shorten",       # tighter
    "punchier",      # livelier voice
    "simplify",      # plainer language
    "custom",        # operator's own instruction
}

ACTION_INSTRUCTIONS = {
    "rewrite": (
        "Rewrite this story from scratch in a fresh voice. Keep every factual "
        "claim exactly as it is — same people, dates, places, numbers and "
        "outcome. Change only the prose."
    ),
    "expand": (
        "Expand this story with more scene and detail. You may only expand on "
        "facts already present or that you are confident are real. Do not "
        "invent names, dates, numbers, quotes or places to fill space."
    ),
    "shorten": (
        "Tighten this story. Cut padding and repetition while keeping every "
        "factual claim and the narrative arc intact."
    ),
    "punchier": (
        "Rewrite this story with a livelier, more energetic voice and a "
        "stronger opening hook. Keep every factual claim unchanged."
    ),
    "simplify": (
        "Rewrite this story in plainer, simpler language for an easier read. "
        "Keep every factual claim unchanged."
    ),
}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def find_chapter(book: StoryBook, chapter_number: int) -> Optional[Chapter]:
    for ch in book.chapters:
        if ch.number == chapter_number:
            return ch
    return None


def find_story(book: StoryBook, item_id: str) -> tuple[Optional[Chapter], Optional[Story]]:
    for ch in book.chapters:
        for s in ch.stories:
            if s.id == item_id:
                return ch, s
    return None, None


def require_story(book: StoryBook, item_id: str) -> tuple[Chapter, Story]:
    ch, s = find_story(book, item_id)
    if ch is None or s is None:
        raise StoryError(f"Story '{item_id}' not found.")
    return ch, s


# ---------------------------------------------------------------------------
# Manual edits
# ---------------------------------------------------------------------------

def apply_story_edit(book: StoryBook, item_id: str, payload: dict[str, Any]) -> Story:
    """Update a story in place from the editor's form fields."""
    _, story = require_story(book, item_id)

    if "title" in payload:
        title = str(payload["title"] or "").strip()
        if not title:
            raise StoryError("Story title cannot be empty.")
        story.title = title

    if "body" in payload:
        body = str(payload["body"] or "").strip()
        if not body:
            raise StoryError("Story text cannot be empty.")
        story.body = body
        # The count drives the editor's length readout and the export gate, so
        # it has to track a hand edit immediately.
        story.word_count = engine.count_words(body)

    for field_name in ("sidebar", "closer", "context", "who", "year", "where", "sources"):
        if field_name in payload:
            setattr(story, field_name, str(payload[field_name] or "").strip())

    if "uncertain_claims" in payload:
        raw = payload["uncertain_claims"]
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]
        story.uncertain_claims = [str(c).strip() for c in (raw or []) if str(c).strip()]

    return story


def apply_chapter_edit(book: StoryBook, chapter_number: int, payload: dict[str, Any]) -> Chapter:
    ch = find_chapter(book, chapter_number)
    if ch is None:
        raise StoryError(f"Chapter {chapter_number} not found.")
    if "chapter_title" in payload:
        ch.title = str(payload["chapter_title"] or "").strip()
    if "chapter_intro" in payload:
        ch.intro = str(payload["chapter_intro"] or "").strip()
    return ch


def delete_story(book: StoryBook, item_id: str) -> bool:
    for ch in book.chapters:
        before = len(ch.stories)
        ch.stories = [s for s in ch.stories if s.id != item_id]
        if len(ch.stories) != before:
            return True
    return False


def reorder_stories(book: StoryBook, ordered_ids: list[str]) -> None:
    """Reorder the whole book from a flat list of story ids.

    Ids not mentioned keep their relative order at the end of their chapter, so
    a partial list from the UI can never silently drop a story.
    """
    rank = {sid: i for i, sid in enumerate(ordered_ids)}
    for ch in book.chapters:
        ch.stories.sort(key=lambda s: rank.get(s.id, len(rank)))
    renumber(book)


def renumber(book: StoryBook) -> None:
    """Restore a sequential reader-facing numbering across the whole book.

    Ids are left alone on purpose: they key the raw-output cache and the
    editor's open panes, and renumbering them would orphan both.
    """
    n = 1
    for ch in book.chapters:
        for s in ch.stories:
            s.number = n
            n += 1


# ---------------------------------------------------------------------------
# AI edits
# ---------------------------------------------------------------------------

def build_edit_prompt(
    book: StoryBook,
    story: Story,
    action: str,
    instruction: str,
) -> str:
    cfg = book.config
    if action == "custom":
        directive = instruction.strip()
        if not directive:
            raise StoryError("A custom edit needs an instruction.")
    else:
        directive = ACTION_INSTRUCTIONS[action]
        if instruction.strip():
            directive = f"{directive}\n\nAlso: {instruction.strip()}"

    lo, hi = cfg.min_words, cfg.max_words
    if action == "expand":
        # Expanding into the same band it already satisfies is a no-op, so aim
        # at the top of the band and allow overshoot.
        lo, hi = story.word_count + 60, max(hi, story.word_count + 220)
    elif action == "shorten":
        lo, hi = max(engine.ABSOLUTE_MIN_WORDS, story.word_count - 220), max(
            engine.ABSOLUTE_MIN_WORDS + 40, story.word_count - 60
        )

    context_block = ""
    if story.context or story.who or story.year or story.where:
        known = "\n".join(
            f"{label}: {value}" for label, value in (
                ("WHO", story.who), ("YEAR", story.year), ("WHERE", story.where),
                ("CONTEXT", story.context),
            ) if value
        )
        context_block = (
            "\nVERIFIED RESEARCH NOTES for this story — every fact here is "
            f"correct and must survive the edit:\n{known}\n"
        )

    extras: list[str] = []
    if cfg.sidebar_enabled:
        extras.append(f'  "sidebar": "the {cfg.sidebar_label} block, edited to match",')
    if cfg.closer_enabled:
        extras.append(f'  "closer": "the {cfg.closer_label} line, edited to match",')
    extras_block = ("\n" + "\n".join(extras)) if extras else ""

    return (
        f"BOOK TITLE: {cfg.book_title}\n"
        f"BOOK TOPIC: {cfg.topic}\n"
        f"AUDIENCE: {cfg.audience}\n"
        f"TONE: {cfg.tone}\n"
        f"STORY TITLE: {story.title}\n"
        f"{context_block}"
        f"\nCURRENT STORY:\n{story.body}\n"
        + (f"\nCURRENT {cfg.sidebar_label.upper()}:\n{story.sidebar}\n"
           if story.sidebar else "")
        + (f"\nCURRENT {cfg.closer_label.upper()}:\n{story.closer}\n"
           if story.closer else "")
        + f"\nYOUR TASK: {directive}\n"
        f"\nHARD REQUIREMENTS:\n"
        f"1. Between {lo} and {hi} words.\n"
        "2. Never invent a fact. Do not add names, dates, dollar amounts, "
        "locations or quotes that are not in the story or the research notes "
        "already.\n"
        "3. Flowing prose. No bullet lists, no headings, no markdown.\n"
        f"4. Match the tone: {cfg.tone}.\n"
        "\nReturn ONLY a JSON object, no prose outside it, no markdown fence:\n"
        "{\n"
        '  "title": "the story title",\n'
        '  "body": "the edited story, paragraphs separated by \\n\\n",'
        f"{extras_block}\n"
        '  "uncertain_claims": ["any detail a fact-checker should verify"]\n'
        "}\n"
    )


def ai_edit_story(
    book: StoryBook,
    item_id: str,
    action: str,
    instruction: str,
    *,
    cache: Optional[RawOutputCache] = None,
    ledger: Optional[UsageLedger] = None,
) -> Story:
    """Apply one AI action to a single story, in place."""
    if action not in AI_ACTIONS:
        raise StoryError(f"Unknown edit action '{action}'.")

    _, story = require_story(book, item_id)
    cfg = book.config

    prompt = build_edit_prompt(book, story, action, instruction)
    reply = engine.call_openclaw_raw(
        cfg.agent,
        prompt,
        local=cfg.local,
        thinking=cfg.thinking,
        timeout_s=cfg.timeout_s,
        cache=cache,
        ledger=ledger,
        session_id=f"stories-edit-{uuid.uuid4().hex[:8]}",
    )
    raw = engine._extract_json_object(reply)

    body = engine.clean_body(str(raw.get("body") or ""))
    if not body:
        raise StoryError("The AI reply contained no story text.")

    title = str(raw.get("title") or "").strip()
    if title:
        story.title = title
    story.body = body
    story.word_count = engine.count_words(body)

    if cfg.sidebar_enabled and raw.get("sidebar"):
        story.sidebar = engine.clean_body(str(raw["sidebar"]))
    if cfg.closer_enabled and raw.get("closer"):
        story.closer = engine.clean_body(str(raw["closer"]))
    if raw.get("uncertain_claims"):
        story.uncertain_claims = engine._string_list(raw["uncertain_claims"])

    # The old build warnings described prose that no longer exists.
    story.warnings = []
    return story


def regenerate_story(
    book: StoryBook,
    item_id: str,
    *,
    context_override: str = "",
    cache: Optional[RawOutputCache] = None,
    ledger: Optional[UsageLedger] = None,
) -> Story:
    """Rewrite a story from its context box, as the build would.

    The operator can supply a replacement context here — the main repair path
    when a story came out wrong because its original context was too thin.
    """
    chapter, story = require_story(book, item_id)
    cfg = book.config

    if context_override.strip():
        story.context = context_override.strip()

    st_cfg = StoryConfig(
        number=story.number,
        title=story.title,
        context=story.context,
        who=story.who,
        year=story.year,
        where=story.where,
        sources=story.sources,
    )

    prompt = engine.build_story_prompt(cfg, st_cfg, chapter_title=chapter.title)
    reply = engine.call_openclaw_raw(
        cfg.agent,
        prompt,
        local=cfg.local,
        thinking=cfg.thinking,
        timeout_s=cfg.timeout_s,
        cache=cache,
        ledger=ledger,
        session_id=f"stories-regen-{uuid.uuid4().hex[:8]}",
    )
    raw = engine._extract_json_object(reply)
    fresh = engine.parse_story_reply(raw, st_cfg, chapter.number)

    # Overwrite content, keep identity: the id keys the editor's open pane and
    # the story's place in the book.
    story.title = fresh.title
    story.body = fresh.body
    story.sidebar = fresh.sidebar
    story.closer = fresh.closer
    story.cited_sources = fresh.cited_sources
    story.uncertain_claims = fresh.uncertain_claims
    story.word_count = fresh.word_count
    story.warnings = []
    return story


def add_story(
    book: StoryBook,
    payload: dict[str, Any],
    *,
    cache: Optional[RawOutputCache] = None,
    ledger: Optional[UsageLedger] = None,
) -> Story:
    """Write and append one new story from a title + context box.

    The path for "I forgot one" and for filling a gap left by a story that
    failed during the build.
    """
    cfg = book.config
    title = str(payload.get("title") or "").strip()
    if not title:
        raise StoryError("A new story needs a title.")

    try:
        chapter_number = int(payload.get("chapter_number") or 0)
    except (TypeError, ValueError):
        raise StoryError("chapter_number must be a number.")

    chapter = find_chapter(book, chapter_number) if chapter_number else None
    if chapter is None:
        if not book.chapters:
            raise StoryError("This book has no chapters to add a story to.")
        chapter = book.chapters[-1]

    existing = {s.number for s in book.all_stories()}
    number = max(existing, default=0) + 1

    st_cfg = StoryConfig(
        number=number,
        title=title,
        context=str(payload.get("context") or "").strip(),
        who=str(payload.get("who") or "").strip(),
        year=str(payload.get("year") or "").strip(),
        where=str(payload.get("where") or "").strip(),
        sources=str(payload.get("sources") or "").strip(),
    )

    prompt = engine.build_story_prompt(cfg, st_cfg, chapter_title=chapter.title)
    reply = engine.call_openclaw_raw(
        cfg.agent,
        prompt,
        local=cfg.local,
        thinking=cfg.thinking,
        timeout_s=cfg.timeout_s,
        cache=cache,
        ledger=ledger,
        session_id=f"stories-add-{uuid.uuid4().hex[:8]}",
    )
    raw = engine._extract_json_object(reply)
    story = engine.parse_story_reply(raw, st_cfg, chapter.number)

    # Ids must stay unique even after deletions have freed up numbers.
    taken = {s.id for s in book.all_stories()}
    if story.id in taken:
        story.id = f"s{number:03d}_{uuid.uuid4().hex[:4]}"

    chapter.stories.append(story)
    return story


# ---------------------------------------------------------------------------
# Illustrations
# ---------------------------------------------------------------------------

def regenerate_illustration(
    book: StoryBook,
    *,
    story_id: str = "",
    chapter_number: int = 0,
    prompt_hint: str = "",
    style_hint: str = "",
    out_dir: Optional[Path] = None,
) -> str:
    """Regenerate one image, for either a story or a chapter.

    Returns the prompt that was used, so the editor can show what produced the
    result and the operator can refine it.
    """
    import openclaw_image_maker as image_maker

    cfg = book.config
    api_key = image_maker.resolve_api_key(cfg.openai_api_key)
    if not api_key:
        raise StoryError("No OpenAI API key is configured for image generation.")

    if story_id:
        _, target = require_story(book, story_id)
        subject = prompt_hint or target.title
        cache_key = f"story_{target.number:03d}_{uuid.uuid4().hex[:6]}"
    else:
        found = find_chapter(book, chapter_number)
        if found is None:
            raise StoryError(f"Chapter {chapter_number} not found.")
        target = found
        subject = prompt_hint or target.title or cfg.topic
        cache_key = f"chapter_{target.number}_{uuid.uuid4().hex[:6]}"

    if style_hint:
        cfg.illustration_style_hint = style_hint

    base = out_dir or Path(".")
    cache_dir = base / "illustrations"
    cache_dir.mkdir(parents=True, exist_ok=True)

    prompt = pipeline.build_illustration_prompt(cfg, subject)
    path = image_maker.generate_image_openai(
        prompt=prompt,
        api_key=api_key,
        cache_dir=cache_dir,
        cache_key=cache_key,
        model=cfg.image_model,
        size=cfg.image_size,
        quality=cfg.image_quality,
    )

    from .image_edit import to_grayscale
    try:
        to_grayscale(path)
    except Exception:
        # A colour cast is a cosmetic defect; losing the image is not.
        pass

    target.illustration_path = str(path)
    target.illustration_prompt = prompt
    return prompt


def set_illustration_from_upload(
    book: StoryBook,
    file: Any,
    out_dir: Path,
    *,
    story_id: str = "",
    chapter_number: int = 0,
) -> str:
    """Store an operator-supplied image against a story or chapter."""
    if story_id:
        _, target = require_story(book, story_id)
        stem = f"story_{target.number:03d}"
    else:
        found = find_chapter(book, chapter_number)
        if found is None:
            raise StoryError(f"Chapter {chapter_number} not found.")
        target = found
        stem = f"chapter_{target.number}"

    suffix = Path(str(file.filename or "")).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise StoryError("Image must be a PNG, JPG or WEBP file.")

    cache_dir = out_dir / "illustrations"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{stem}_upload_{int(time.time())}{suffix}"
    file.save(str(path))

    target.illustration_path = str(path)
    target.illustration_prompt = "Uploaded by the operator."
    return str(path)


# ---------------------------------------------------------------------------
# Post-edit validation
# ---------------------------------------------------------------------------

def validate_edited_book(book: StoryBook) -> dict[str, Any]:
    """Report on the edited book without blocking anything.

    Advisory by design: the operator is making deliberate editorial choices in
    the editor, so this surfaces what changed rather than refusing to save.
    """
    cfg = book.config
    stories = book.all_stories()
    problems: list[str] = []
    short: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

    floor = int(cfg.min_words * (1 - engine.LENGTH_TOLERANCE))
    ceiling = int(cfg.max_words * (1 + engine.LENGTH_TOLERANCE))

    for s in stories:
        if not s.body.strip():
            problems.append(f"Story {s.number} '{s.title}' has no text.")
            continue
        if s.word_count < floor or s.word_count > ceiling:
            short.append({
                "id": s.id, "number": s.number, "title": s.title,
                "word_count": s.word_count,
            })
        if s.uncertain_claims:
            flagged.append({
                "id": s.id, "number": s.number, "title": s.title,
                "claims": s.uncertain_claims,
            })

    duplicates: list[dict[str, Any]] = []
    seen: list[Story] = []
    for s in stories:
        twin = engine.find_duplicate(s, seen)
        if twin is not None:
            duplicates.append({
                "id": s.id, "number": s.number, "title": s.title,
                "duplicate_of_id": twin.id, "duplicate_of_title": twin.title,
            })
        seen.append(s)

    return {
        "ok": not problems,
        "problems": problems,
        "story_count": len(stories),
        "total_words": book.total_words(),
        "target_band": [cfg.min_words, cfg.max_words],
        "out_of_band": short,
        "flagged_claims": flagged,
        "possible_duplicates": duplicates,
    }
