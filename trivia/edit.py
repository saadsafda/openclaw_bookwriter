"""Preview & Edit support for trivia books.

Applies manual and AI-assisted edits against a book's stored JSON — the source
of truth — so nothing here regenerates a whole book. Re-export is a separate,
explicit step (spec Section 11: restructure without re-running API calls).

AI edits reuse the same openclaw path and raw-output cache as generation, so an
identical edit request is never billed twice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from . import engine
from .engine import (
    BookConfig,
    Chapter,
    DidYouKnowFact,
    RawOutputCache,
    TriviaBook,
    TriviaError,
    TriviaQuestion,
    UsageLedger,
)

LETTERS = ("A", "B", "C", "D")

# AI actions the editor exposes. Kept as an explicit allowlist so an arbitrary
# action string from the client can never reach the prompt builder.
AI_ACTIONS = {
    "rewrite",
    "regenerate_distractors",
    "harder",
    "easier",
}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def find_chapter(book: TriviaBook, chapter_number: int) -> Optional[Chapter]:
    for ch in book.chapters:
        if ch.number == chapter_number:
            return ch
    return None


def find_question(book: TriviaBook, item_id: str) -> tuple[Optional[Chapter], Optional[TriviaQuestion]]:
    for ch in book.chapters:
        for q in ch.trivia:
            if q.id == item_id:
                return ch, q
    return None, None


def find_fact(book: TriviaBook, item_id: str) -> tuple[Optional[Chapter], Optional[DidYouKnowFact]]:
    for ch in book.chapters:
        for f in ch.facts:
            if f.id == item_id:
                return ch, f
    return None, None


# ---------------------------------------------------------------------------
# Manual edits
# ---------------------------------------------------------------------------

def apply_question_edit(
    book: TriviaBook,
    item_id: str,
    payload: dict[str, Any],
) -> TriviaQuestion:
    """Update a question in place. Validates the same structural rules the
    generator enforces, so a hand edit can't produce an unexportable book."""
    _, q = find_question(book, item_id)
    if q is None:
        raise TriviaError(f"Question '{item_id}' not found.")

    if "question" in payload:
        text = str(payload["question"] or "").strip()
        if not text:
            raise TriviaError("Question text cannot be empty.")
        q.question = text

    if "choices" in payload:
        raw = payload["choices"] or {}
        if not isinstance(raw, dict):
            raise TriviaError("choices must be an object keyed A-D.")
        new_choices: dict[str, str] = {}
        for letter in LETTERS:
            val = str(raw.get(letter, q.choices.get(letter, "")) or "").strip()
            if not val:
                raise TriviaError(f"Choice {letter} cannot be empty.")
            new_choices[letter] = val
        if len({c.lower() for c in new_choices.values()}) != 4:
            raise TriviaError("All four choices must be different.")
        q.choices = new_choices

    if "correct_answer" in payload:
        letter = str(payload["correct_answer"] or "").strip().upper()[:1]
        if letter not in LETTERS:
            raise TriviaError("correct_answer must be A, B, C or D.")
        q.correct_answer = letter

    # Keep the dedup key aligned with the edited claim, otherwise a later
    # overlap check would compare against a stale seed.
    q.fact_seed = engine.slugify_seed(q.claim_text())
    return q


def apply_fact_edit(book: TriviaBook, item_id: str, payload: dict[str, Any]) -> DidYouKnowFact:
    _, f = find_fact(book, item_id)
    if f is None:
        raise TriviaError(f"Fact '{item_id}' not found.")

    if "fact" in payload:
        text = str(payload["fact"] or "").strip()
        if not text:
            raise TriviaError("Fact text cannot be empty.")
        f.fact = text
        f.fact_seed = engine.slugify_seed(text)
    return f


def apply_chapter_edit(book: TriviaBook, chapter_number: int, payload: dict[str, Any]) -> Chapter:
    ch = find_chapter(book, chapter_number)
    if ch is None:
        raise TriviaError(f"Chapter {chapter_number} not found.")
    if "chapter_title" in payload:
        title = str(payload["chapter_title"] or "").strip()
        if not title:
            raise TriviaError("Chapter title cannot be empty.")
        ch.title = title
    if "chapter_scope" in payload:
        ch.scope = str(payload["chapter_scope"] or "").strip()
    return ch


FRONT_MATTER_FIELDS = ("introduction", "conclusion")


def apply_front_matter_edit(book: TriviaBook, section: str, payload: dict[str, Any]) -> str:
    """Hand-edit the Introduction or Conclusion prose.

    Blank is allowed and meaningful: it clears the authored text so the export
    falls back to its generic paragraph, which is the only way to undo a bad
    generation without rebuilding the book.
    """
    if section not in FRONT_MATTER_FIELDS:
        raise TriviaError(f"'{section}' is not front matter.")
    if "text" not in payload:
        raise TriviaError("No text supplied.")
    text = engine.clean_prose_reply(str(payload["text"] or ""))
    setattr(book, section, text)
    return text


def delete_item(book: TriviaBook, item_id: str) -> bool:
    """Remove a question or fact. Chapter counts drift from the config after
    this, which the export check reports — deliberate, since the operator is
    making a manual editorial choice."""
    for ch in book.chapters:
        before = len(ch.trivia) + len(ch.facts)
        ch.trivia = [q for q in ch.trivia if q.id != item_id]
        ch.facts = [f for f in ch.facts if f.id != item_id]
        if len(ch.trivia) + len(ch.facts) != before:
            return True
    return False


def renumber_chapter(ch: Chapter) -> None:
    """Restore sequential ids after a deletion so exports stay tidy."""
    for i, q in enumerate(ch.trivia, start=1):
        q.id = f"ch{ch.number}_q{i:02d}"
    for i, f in enumerate(ch.facts, start=1):
        f.id = f"ch{ch.number}_f{i:03d}"


# ---------------------------------------------------------------------------
# AI-assisted edits
# ---------------------------------------------------------------------------

def build_question_edit_prompt(
    cfg: BookConfig,
    ch: Chapter,
    q: TriviaQuestion,
    action: str,
    instruction: str = "",
) -> str:
    context = (
        f"BOOK TOPIC: {cfg.topic}\n"
        f"CHAPTER: {ch.title}\n"
        f"CHAPTER SCOPE: {ch.scope or ch.title}\n"
        f"AUDIENCE: {cfg.audience}\n\n"
        f"CURRENT QUESTION: {q.question}\n"
        + "".join(f"  {L}. {q.choices.get(L, '')}\n" for L in LETTERS)
        + f"CORRECT ANSWER: {q.correct_answer}\n\n"
    )

    if action == "rewrite":
        task = (
            "Reword the question so it reads better, while testing the exact "
            "same fact. The correct answer must stay factually the same."
        )
    elif action == "regenerate_distractors":
        task = (
            "Keep the question and the correct answer exactly as they are. "
            "Replace the three incorrect choices with fresh ones that are "
            "plausible to a casual reader but unambiguously wrong — same "
            "category and period as the correct answer."
        )
    elif action == "harder":
        task = (
            "Make this question harder by tightening the distractors so they "
            "are closer to the correct answer, without making any of them "
            "arguably correct. Keep the same underlying fact."
        )
    elif action == "easier":
        task = (
            "Make this question easier by making the distractors more clearly "
            "wrong to a casual reader. Keep the same underlying fact."
        )
    else:
        raise TriviaError(f"Unsupported AI action: {action}")

    extra = f"\nADDITIONAL INSTRUCTION FROM THE EDITOR: {instruction}\n" if instruction.strip() else ""

    return (
        f"{context}TASK: {task}\n{extra}"
        "\nHARD REQUIREMENTS: exactly four choices A-D, exactly one correct, "
        "all four choices different, no 'all of the above' or 'none of the above'.\n"
        "\nReturn ONLY a JSON object, no prose, no markdown fence:\n"
        '{"question": "...", "choices": {"A": "...", "B": "...", "C": "...", '
        '"D": "..."}, "correct_answer": "B"}\n'
    )


def build_fact_edit_prompt(
    cfg: BookConfig,
    ch: Chapter,
    f: DidYouKnowFact,
    action: str,
    instruction: str = "",
    avoid: Optional[list[str]] = None,
) -> str:
    avoid_block = ""
    if avoid:
        avoid_block = (
            "\nThe replacement must NOT restate any of these, which already "
            "appear in this book:\n" + "\n".join(f"- {a}" for a in avoid[:120]) + "\n"
        )

    if action == "rewrite":
        task = "Reword this fact so it reads better while stating the same information."
    elif action in {"harder", "easier"}:
        task = (
            "Replace this with a more obscure, more surprising fact from the same scope."
            if action == "harder"
            else "Replace this with a more approachable, widely-appealing fact from the same scope."
        )
    else:
        raise TriviaError(f"Unsupported AI action for a fact: {action}")

    extra = f"\nADDITIONAL INSTRUCTION FROM THE EDITOR: {instruction}\n" if instruction.strip() else ""

    return (
        f"BOOK TOPIC: {cfg.topic}\n"
        f"CHAPTER: {ch.title}\n"
        f"CHAPTER SCOPE: {ch.scope or ch.title}\n\n"
        f"CURRENT FACT: {f.fact}\n\n"
        f"TASK: {task}\n{extra}{avoid_block}"
        "\nOne self-contained sentence. Specific and verifiable. No lead-in "
        "like 'Did you know'.\n"
        "\nReturn ONLY a JSON object, no prose, no markdown fence:\n"
        '{"fact": "..."}\n'
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if not s:
        raise TriviaError("Model returned an empty reply.")
    fenced = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fenced:
        s = fenced.group(1).strip()
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            raise TriviaError(f"Could not find a JSON object in reply: {s[:300]}")
        try:
            parsed = json.loads(s[start:end + 1])
        except json.JSONDecodeError as exc:
            raise TriviaError(f"Malformed JSON in reply: {exc}") from exc
    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    if not isinstance(parsed, dict):
        raise TriviaError("Model reply was not a JSON object.")
    return parsed


def ai_edit_question(
    book: TriviaBook,
    item_id: str,
    action: str,
    instruction: str = "",
    cache: Optional[RawOutputCache] = None,
    ledger: Optional[UsageLedger] = None,
) -> TriviaQuestion:
    if action not in AI_ACTIONS:
        raise TriviaError(f"Unsupported AI action: {action}")
    ch, q = find_question(book, item_id)
    if ch is None or q is None:
        raise TriviaError(f"Question '{item_id}' not found.")

    cfg = book.config
    prompt = build_question_edit_prompt(cfg, ch, q, action, instruction)
    reply = engine.call_openclaw_raw(
        cfg.agent, prompt,
        local=cfg.local, thinking=cfg.thinking, timeout_s=cfg.timeout_s,
        cache=cache, ledger=ledger, model=cfg.model,
    )
    data = _extract_json_object(reply)

    # Route the result through the generator's own validator so an AI edit is
    # held to exactly the same structural standard as generated content.
    parsed, rejects = engine.parse_trivia_items([data], ch.number)
    if not parsed:
        raise TriviaError(
            "The AI edit failed validation: " + ("; ".join(rejects) or "unknown reason")
        )

    new_q = parsed[0]
    if action == "regenerate_distractors":
        # Preserve the original correct answer text; only distractors change.
        original_correct = q.correct_text()
        letter = new_q.correct_answer
        new_q.choices[letter] = original_correct

    q.question = new_q.question
    q.choices = new_q.choices
    q.correct_answer = new_q.correct_answer
    q.fact_seed = engine.slugify_seed(q.claim_text())
    return q


def ai_edit_fact(
    book: TriviaBook,
    item_id: str,
    action: str,
    instruction: str = "",
    cache: Optional[RawOutputCache] = None,
    ledger: Optional[UsageLedger] = None,
) -> DidYouKnowFact:
    if action not in AI_ACTIONS:
        raise TriviaError(f"Unsupported AI action: {action}")
    ch, f = find_fact(book, item_id)
    if ch is None or f is None:
        raise TriviaError(f"Fact '{item_id}' not found.")

    # Everything else in the book, so a replacement can't duplicate it.
    avoid = [q.claim_text() for q in ch.trivia]
    avoid += [o.fact for o in ch.facts if o.id != f.id]

    cfg = book.config
    prompt = build_fact_edit_prompt(cfg, ch, f, action, instruction, avoid)
    reply = engine.call_openclaw_raw(
        cfg.agent, prompt,
        local=cfg.local, thinking=cfg.thinking, timeout_s=cfg.timeout_s,
        cache=cache, ledger=ledger, model=cfg.model,
    )
    data = _extract_json_object(reply)

    parsed, rejects = engine.parse_fact_items([data], ch.number)
    if not parsed:
        raise TriviaError(
            "The AI edit failed validation: " + ("; ".join(rejects) or "unknown reason")
        )

    f.fact = parsed[0].fact
    f.fact_seed = engine.slugify_seed(f.fact)
    return f


# ---------------------------------------------------------------------------
# Illustrations
# ---------------------------------------------------------------------------

def regenerate_illustration(
    book: TriviaBook,
    chapter_number: int,
    out_dir: Path,
    prompt_hint: str = "",
    style_hint: str = "",
) -> Chapter:
    """Re-run a chapter image. Section 9's no-humans / no-text constraints are
    reapplied here rather than trusted to the operator's hint."""
    import openclaw_image_maker as image_maker
    from . import pipeline
    from .engine import ChapterConfig

    ch = find_chapter(book, chapter_number)
    if ch is None:
        raise TriviaError(f"Chapter {chapter_number} not found.")

    cfg = book.config
    api_key = image_maker.resolve_api_key(cfg.openai_api_key)
    if not api_key:
        raise TriviaError("No OpenAI API key available for image generation.")

    if style_hint.strip():
        cfg.illustration_style_hint = style_hint.strip()

    ch_cfg = ChapterConfig(
        chapter_number=ch.number,
        chapter_title=ch.title,
        chapter_scope=ch.scope,
        illustration_prompt_hint=prompt_hint.strip(),
    )
    prompt = pipeline.build_illustration_prompt(cfg, ch_cfg)

    cache_dir = out_dir / "illustrations"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = image_maker.generate_image_openai(
        prompt=prompt,
        api_key=api_key,
        cache_dir=cache_dir,
        cache_key=f"ch{ch.number}_illustration",
        model=cfg.image_model,
        size=cfg.image_size,
        quality=cfg.image_quality,
    )
    # Match the black-and-white interior, same as generation.
    try:
        from .image_edit import to_grayscale
        to_grayscale(path)
    except Exception:
        pass

    ch.illustration_path = str(path)
    ch.illustration_prompt = prompt
    return ch


def set_illustration_from_upload(
    book: TriviaBook,
    chapter_number: int,
    file_storage: Any,
    out_dir: Path,
) -> Chapter:
    """Replace a chapter image with an operator-supplied file."""
    ch = find_chapter(book, chapter_number)
    if ch is None:
        raise TriviaError(f"Chapter {chapter_number} not found.")

    filename = (getattr(file_storage, "filename", "") or "").lower()
    suffix = Path(filename).suffix
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise TriviaError("Image must be a .png, .jpg or .webp file.")

    cache_dir = out_dir / "illustrations"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"ch{ch.number}_illustration{suffix}"
    for old in cache_dir.glob(f"ch{ch.number}_illustration.*"):
        if old.is_file() and old.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            old.unlink(missing_ok=True)
    file_storage.save(str(target))

    # Uploads join the same black-and-white interior as generated art.
    try:
        from .image_edit import to_grayscale
        to_grayscale(target)
    except Exception:
        pass
    try:
        import openclaw_image_maker as image_maker
        image_maker.prepare_image_for_print(target)
    except Exception:
        # Print DPI tagging is best-effort; the upload still stands.
        pass

    ch.illustration_path = str(target)
    ch.illustration_prompt = "(uploaded by operator)"
    return ch


# ---------------------------------------------------------------------------
# Post-edit validation
# ---------------------------------------------------------------------------

def validate_edited_book(book: TriviaBook) -> dict[str, Any]:
    """Re-run the structural and overlap checks over an edited book so the
    editor can show whether it is still exportable."""
    errors: list[str] = []

    for ch in book.chapters:
        for q in ch.trivia:
            if set(q.choices) != {"A", "B", "C", "D"}:
                errors.append(f"{q.id}: must have exactly choices A-D.")
            elif q.correct_answer not in q.choices:
                errors.append(f"{q.id}: correct answer points at a missing choice.")
            if len({c.strip().lower() for c in q.choices.values()}) != 4:
                errors.append(f"{q.id}: choices are not all different.")
        if ch.trivia:
            dist = engine.answer_distribution(ch.trivia)
            worst = max(dist.values()) / len(ch.trivia)
            if worst > engine.ANSWER_DISTRIBUTION_TOLERANCE + 0.15:
                errors.append(
                    f"Chapter {ch.number}: correct answers cluster on one letter ({dist})."
                )

    # Section 6 still applies after editing: a hand-written fact must not
    # restate a question. Heuristics only here — the editor is interactive and
    # must stay responsive, so no model calls.
    overlaps: list[dict[str, Any]] = []
    for ch in book.chapters:
        for f in ch.facts:
            for q in ch.trivia:
                score = engine.similarity(f.claim_text(), q.claim_text())
                if score >= engine.NEAR_DUPLICATE_JACCARD:
                    overlaps.append({
                        "fact_id": f.id,
                        "question_id": q.id,
                        "score": round(score, 3),
                    })
                    break

    return {
        "ok": not errors and not overlaps,
        "errors": errors,
        "overlaps": overlaps,
        "counts": [
            {
                "chapter_number": ch.number,
                "chapter_title": ch.title,
                "trivia": len(ch.trivia),
                "facts": len(ch.facts),
            }
            for ch in book.chapters
        ],
    }
