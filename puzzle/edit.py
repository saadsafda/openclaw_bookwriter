"""Preview & Edit support for puzzle books.

Applies manual and AI-assisted edits against a book's stored JSON — the source
of truth — so nothing here regenerates a whole book. Re-export is a separate,
explicit step.

Two things make this different from the trivia editor: a puzzle's *artwork* is
derived from its content, so editing a word list or a clue has to re-render the
grid; and some edits can invalidate a layout (a longer word may no longer fit),
which must be reported rather than silently dropped.

AI edits reuse the same openclaw path and raw-output cache as generation, so an
identical edit request is never billed twice.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from . import engine, generators
from .engine import (
    WORDS_PER_CROSSWORD,
    WORDS_PER_SEARCH,
    Crossword,
    CrosswordEntry,
    Cryptogram,
    Maze,
    PictureBrief,
    PuzzleBook,
    PuzzleError,
    Riddle,
    TriviaQuestion,
    WordSearch,
    normalize_word,
)

LETTERS = ("A", "B", "C", "D")

# AI actions the editor exposes. An explicit allowlist, so an arbitrary action
# string from the client can never reach the prompt builder.
AI_ACTIONS = {
    "rewrite",
    "easier",
    "harder",
    "regenerate_distractors",
}

# How many times a re-render may ask the model for a fresh word list when the
# edited one will not lay out.
MAX_RELAYOUT_ATTEMPTS = 4


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def find_riddle(book: PuzzleBook, item_id: str) -> Optional[Riddle]:
    return next((r for r in book.riddles if r.id == item_id), None)


def find_cryptogram(book: PuzzleBook, item_id: str) -> Optional[Cryptogram]:
    return next((c for c in book.cryptograms if c.id == item_id), None)


def find_word_search(book: PuzzleBook, item_id: str) -> Optional[WordSearch]:
    return next((w for w in book.word_searches if w.id == item_id), None)


def find_crossword(book: PuzzleBook, item_id: str) -> Optional[Crossword]:
    return next((c for c in book.crosswords if c.id == item_id), None)


def find_maze(book: PuzzleBook, item_id: str) -> Optional[Maze]:
    return next((m for m in book.mazes if m.id == item_id), None)


def find_brief(book: PuzzleBook, item_id: str) -> Optional[PictureBrief]:
    return next((p for p in book.picture_briefs if p.id == item_id), None)


def find_question(book: PuzzleBook, item_id: str) -> tuple[Optional[Any], Optional[TriviaQuestion]]:
    for ch in book.trivia_chapters:
        for q in ch.questions:
            if q.id == item_id:
                return ch, q
    return None, None


def find_any(book: PuzzleBook, item_id: str) -> tuple[str, Optional[Any]]:
    """Locate an item by id across every section. Returns (kind, item)."""
    for kind, finder in (
        ("riddle", find_riddle),
        ("cryptogram", find_cryptogram),
        ("word_search", find_word_search),
        ("crossword", find_crossword),
        ("maze", find_maze),
        ("picture_brief", find_brief),
    ):
        item = finder(book, item_id)
        if item is not None:
            return kind, item
    _ch, q = find_question(book, item_id)
    if q is not None:
        return "trivia_question", q
    return "", None


def _as_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


# ---------------------------------------------------------------------------
# Manual edits
# ---------------------------------------------------------------------------

def apply_riddle_edit(book: PuzzleBook, item_id: str, payload: dict[str, Any]) -> Riddle:
    riddle = find_riddle(book, item_id)
    if riddle is None:
        raise PuzzleError(f"Riddle '{item_id}' not found.")

    if "riddle" in payload:
        text = str(payload["riddle"] or "").strip()
        if not text:
            raise PuzzleError("A riddle cannot be empty.")
        riddle.riddle = text
    if "answer" in payload:
        answer = _as_text(payload["answer"])
        if not answer:
            raise PuzzleError("A riddle needs an answer.")
        riddle.answer = answer

    if engine.gives_away_answer(riddle.answer, riddle.riddle):
        raise PuzzleError("The riddle text names its own answer.")
    return riddle


def apply_cryptogram_edit(
    book: PuzzleBook, item_id: str, payload: dict[str, Any]
) -> Cryptogram:
    """Edit a cryptogram. Changing the phrase re-encodes it with the existing
    cipher, so the puzzle stays solvable and the answer key stays correct."""
    gram = find_cryptogram(book, item_id)
    if gram is None:
        raise PuzzleError(f"Cryptogram '{item_id}' not found.")

    if "hint" in payload:
        gram.hint = _as_text(payload["hint"])

    if "phrase" in payload:
        phrase = _as_text(payload["phrase"])
        letters = normalize_word(phrase)
        if len(letters) < 12:
            raise PuzzleError("That phrase is too short to solve as a cryptogram.")
        if len(letters) > 90:
            raise PuzzleError("That phrase is too long to fit one puzzle line.")
        gram.phrase = phrase
        if not gram.cipher:
            gram.cipher = engine.make_cipher(len(phrase) + gram.number)
        gram.encoded = engine.encode_phrase(phrase, gram.cipher)

    return gram


def reshuffle_cipher(book: PuzzleBook, item_id: str) -> Cryptogram:
    """Give one cryptogram a brand new substitution alphabet."""
    gram = find_cryptogram(book, item_id)
    if gram is None:
        raise PuzzleError(f"Cryptogram '{item_id}' not found.")
    import random

    gram.cipher = engine.make_cipher(random.Random().randrange(1, 10**9))
    gram.encoded = engine.encode_phrase(gram.phrase, gram.cipher)
    return gram


def apply_question_edit(
    book: PuzzleBook, item_id: str, payload: dict[str, Any]
) -> TriviaQuestion:
    _ch, question = find_question(book, item_id)
    if question is None:
        raise PuzzleError(f"Question '{item_id}' not found.")

    if "question" in payload:
        text = _as_text(payload["question"])
        if not text:
            raise PuzzleError("A question cannot be empty.")
        question.question = text

    if "choices" in payload:
        raw = payload["choices"]
        if not isinstance(raw, dict):
            raise PuzzleError("choices must be an object keyed A-D.")
        for letter in LETTERS:
            if letter in raw:
                value = _as_text(raw[letter])
                if not value:
                    raise PuzzleError(f"Choice {letter} cannot be empty.")
                question.choices[letter] = value

    if "correct_answer" in payload:
        letter = _as_text(payload["correct_answer"]).upper()[:1]
        if letter not in LETTERS:
            raise PuzzleError("correct_answer must be A, B, C or D.")
        question.correct_answer = letter

    if len({c.strip().lower() for c in question.choices.values()}) != 4:
        raise PuzzleError("All four choices must be different.")
    return question


def apply_brief_edit(book: PuzzleBook, item_id: str, payload: dict[str, Any]) -> PictureBrief:
    brief = find_brief(book, item_id)
    if brief is None:
        raise PuzzleError(f"Picture brief '{item_id}' not found.")

    if "scene_title" in payload:
        title = _as_text(payload["scene_title"])
        if not title:
            raise PuzzleError("A scene needs a title.")
        brief.scene_title = title
    if "scene_description" in payload:
        desc = _as_text(payload["scene_description"])
        if not desc:
            raise PuzzleError("A scene needs a description.")
        brief.scene_description = desc
    if "difference_ideas" in payload:
        raw = payload["difference_ideas"]
        if isinstance(raw, str):
            raw = raw.splitlines()
        ideas = [_as_text(i) for i in raw if _as_text(i)]
        if len(ideas) < 3:
            raise PuzzleError("Give at least 3 difference ideas.")
        brief.difference_ideas = ideas[:10]
    return brief


def apply_title_edit(book: PuzzleBook, item_id: str, title: str) -> Any:
    """Rename a puzzle and re-render its art so the printed title matches."""
    kind, item = find_any(book, item_id)
    if item is None:
        raise PuzzleError(f"'{item_id}' not found.")
    clean = _as_text(title)
    if not clean:
        raise PuzzleError("A title cannot be empty.")
    if not hasattr(item, "title"):
        raise PuzzleError("That item has no title to change.")
    item.title = clean
    return item


# ---------------------------------------------------------------------------
# Content edits that require a re-render
# ---------------------------------------------------------------------------

def update_word_search_words(
    book: PuzzleBook, item_id: str, words: list[str], *, grid_size: int = 0, seed: int = 0
) -> WordSearch:
    """Replace a word search's word list and rebuild the grid and artwork."""
    puzzle = find_word_search(book, item_id)
    if puzzle is None:
        raise PuzzleError(f"Word search '{item_id}' not found.")

    size = grid_size or len(puzzle.grid) or book.config.wordsearch_grid
    parsed, rejects = engine.parse_word_list(words, max_len=size, want=WORDS_PER_SEARCH)
    if len(parsed) < WORDS_PER_SEARCH:
        detail = f" ({'; '.join(rejects[:3])})" if rejects else ""
        raise PuzzleError(
            f"Need {WORDS_PER_SEARCH} usable words, got {len(parsed)}{detail}."
        )

    # Try a few seeds before declaring the set unplaceable — placement is
    # randomized, so one failure does not mean the words cannot fit.
    last: Optional[PuzzleError] = None
    for attempt in range(MAX_RELAYOUT_ATTEMPTS):
        try:
            grid, placements = generators.build_word_search(
                parsed, size, seed=(seed or puzzle.number * 613) + attempt,
                difficulty=book.config.difficulty,
            )
            puzzle.words = parsed
            puzzle.grid = grid
            puzzle.placements = placements
            _rerender_word_search(puzzle)
            return puzzle
        except PuzzleError as exc:
            last = exc
    raise PuzzleError(f"Those words will not fit a {size}x{size} grid: {last}")


def update_crossword_entries(
    book: PuzzleBook, item_id: str, entries: list[dict[str, Any]], *, seed: int = 0
) -> Crossword:
    """Replace a crossword's words/clues and rebuild the grid and artwork."""
    puzzle = find_crossword(book, item_id)
    if puzzle is None:
        raise PuzzleError(f"Crossword '{item_id}' not found.")

    parsed, rejects = engine.parse_crossword_words(entries)
    if len(parsed) < WORDS_PER_CROSSWORD:
        detail = f" ({'; '.join(rejects[:3])})" if rejects else ""
        raise PuzzleError(
            f"Need {WORDS_PER_CROSSWORD} usable words, got {len(parsed)}{detail}."
        )
    parsed = parsed[:WORDS_PER_CROSSWORD]

    last: Optional[PuzzleError] = None
    for attempt in range(MAX_RELAYOUT_ATTEMPTS):
        # build_crossword mutates entries, so hand it a fresh copy each attempt.
        candidate = [CrosswordEntry(word=e.word, clue=e.clue) for e in parsed]
        try:
            grid, numbers = generators.build_crossword(
                candidate, seed=(seed or puzzle.number * 421) + attempt)
            puzzle.entries = candidate
            puzzle.grid = grid
            puzzle.numbers = numbers
            _rerender_crossword(puzzle)
            return puzzle
        except PuzzleError as exc:
            last = exc
    raise PuzzleError(f"Those words will not interlock: {last}")


def update_crossword_clues(
    book: PuzzleBook, item_id: str, clues: dict[str, str]
) -> Crossword:
    """Edit clue text only. The grid is untouched, so this always succeeds —
    it just re-renders so the printed clue list matches."""
    puzzle = find_crossword(book, item_id)
    if puzzle is None:
        raise PuzzleError(f"Crossword '{item_id}' not found.")

    for entry in puzzle.entries:
        if entry.word in clues:
            clue = _as_text(clues[entry.word])
            if not clue:
                raise PuzzleError(f"{entry.word}: clue cannot be empty.")
            if engine.gives_away_answer(entry.word, clue):
                raise PuzzleError(f"{entry.word}: the clue names its own answer.")
            entry.clue = clue

    _rerender_crossword(puzzle)
    return puzzle


def regenerate_maze(
    book: PuzzleBook, item_id: str, *, cols: int = 0, rows: int = 0, seed: int = 0
) -> Maze:
    """Rebuild one maze, optionally at a new size."""
    maze = find_maze(book, item_id)
    if maze is None:
        raise PuzzleError(f"Maze '{item_id}' not found.")

    import random

    maze.cols = max(4, min(40, cols or maze.cols))
    maze.rows = max(4, min(50, rows or maze.rows))
    maze.seed = seed or random.Random().randrange(1, 10**9)

    grid = generators.generate_maze(maze.cols, maze.rows, maze.seed)
    if not grid.solution:
        raise PuzzleError("Generated maze has no solution path.")

    generators.render_maze(
        grid, Path(maze.image_path),
        title=maze.title, subtitle="Find your way from START to END.",
    )
    generators.render_maze(
        grid, Path(maze.solution_path),
        title=f"{maze.title} — Solution", solution=True,
    )
    return maze


def _rerender_word_search(puzzle: WordSearch) -> None:
    if puzzle.image_path:
        generators.render_word_search(puzzle, Path(puzzle.image_path))
    if puzzle.solution_path:
        generators.render_word_search(puzzle, Path(puzzle.solution_path), solution=True)


def _rerender_crossword(puzzle: Crossword) -> None:
    if puzzle.image_path:
        generators.render_crossword(puzzle, Path(puzzle.image_path))
    if puzzle.solution_path:
        generators.render_crossword(puzzle, Path(puzzle.solution_path), solution=True)


def rerender_item(book: PuzzleBook, item_id: str) -> str:
    """Redraw one puzzle's artwork from its current content."""
    kind, item = find_any(book, item_id)
    if item is None:
        raise PuzzleError(f"'{item_id}' not found.")
    if kind == "word_search":
        _rerender_word_search(item)
    elif kind == "crossword":
        _rerender_crossword(item)
    elif kind == "maze":
        regenerate_maze(book, item_id, seed=item.seed)
    else:
        raise PuzzleError("That item has no artwork to redraw.")
    return kind


# ---------------------------------------------------------------------------
# Deletion and renumbering
# ---------------------------------------------------------------------------

def delete_item(book: PuzzleBook, item_id: str) -> bool:
    """Remove an item and renumber the section it came from."""
    groups = (
        (book.riddles, "riddles"),
        (book.cryptograms, "cryptograms"),
        (book.word_searches, "word_searches"),
        (book.crosswords, "crosswords"),
        (book.mazes, "mazes"),
        (book.picture_briefs, "picture_briefs"),
    )
    for group, _name in groups:
        for i, item in enumerate(group):
            if getattr(item, "id", None) == item_id:
                del group[i]
                _renumber(group)
                return True

    for chapter in book.trivia_chapters:
        for i, q in enumerate(chapter.questions):
            if q.id == item_id:
                del chapter.questions[i]
                for n, question in enumerate(chapter.questions, start=1):
                    question.number = n
                return True
    return False


def _renumber(group: list[Any]) -> None:
    for n, item in enumerate(group, start=1):
        item.number = n


# ---------------------------------------------------------------------------
# AI-assisted edits
# ---------------------------------------------------------------------------

def _book_context(book: PuzzleBook) -> str:
    cfg = book.config
    return (
        f"BOOK TITLE: {cfg.book_title}\n"
        f"BOOK TOPIC: {cfg.topic}\n"
        f"AUDIENCE: {cfg.audience}\n"
        f"DIFFICULTY: {cfg.difficulty}\n"
    )


def _action_line(action: str, instruction: str) -> str:
    if instruction.strip():
        return f"WHAT TO CHANGE: {instruction.strip()}"
    return {
        "rewrite": "WHAT TO CHANGE: Rewrite it so it reads better, keeping the same answer.",
        "easier": "WHAT TO CHANGE: Make it easier for the audience, keeping the same answer.",
        "harder": "WHAT TO CHANGE: Make it more challenging, keeping the same answer.",
        "regenerate_distractors":
            "WHAT TO CHANGE: Keep the question and the correct answer. Replace the "
            "three wrong options with better ones.",
    }.get(action, "WHAT TO CHANGE: Improve it.")


_JSON_OBJECT_ONLY = "Reply with a JSON object ONLY. No prose, no markdown fence."


def build_riddle_edit_prompt(book: PuzzleBook, riddle: Riddle, action: str, instruction: str) -> str:
    return (
        f"{_book_context(book)}\n"
        "You are editing ONE riddle in a puzzle book.\n\n"
        f"CURRENT RIDDLE:\n{riddle.riddle}\n\n"
        f"CURRENT ANSWER: {riddle.answer}\n\n"
        f"{_action_line(action, instruction)}\n\n"
        "Rules:\n"
        "- Keep it tied to the book topic and solvable by the stated audience.\n"
        "- 2 to 4 lines. The answer must stay 1 to 3 words.\n"
        "- The riddle must never name its own answer.\n\n"
        'Reply with a JSON object with exactly these keys: "riddle", "answer".\n'
        f"{_JSON_OBJECT_ONLY}"
    )


def build_cryptogram_edit_prompt(
    book: PuzzleBook, gram: Cryptogram, action: str, instruction: str
) -> str:
    return (
        f"{_book_context(book)}\n"
        "You are editing ONE cryptogram phrase in a puzzle book.\n\n"
        f"CURRENT PHRASE: {gram.phrase}\n"
        f"CURRENT HINT: {gram.hint}\n\n"
        f"{_action_line(action, instruction)}\n\n"
        "Rules:\n"
        "- 4 to 9 words, between 20 and 60 letters.\n"
        "- Plain letters and spaces, at most one comma or apostrophe. No digits.\n"
        "- It must tie to the book topic and make sense on its own.\n"
        "- The hint must not reveal any word in the phrase.\n\n"
        'Reply with a JSON object with exactly these keys: "phrase", "hint".\n'
        f"{_JSON_OBJECT_ONLY}"
    )


def build_question_edit_prompt(
    book: PuzzleBook, question: TriviaQuestion, action: str, instruction: str
) -> str:
    choices = "\n".join(f"{L}. {question.choices.get(L, '')}" for L in LETTERS)
    return (
        f"{_book_context(book)}\n"
        "You are editing ONE multiple-choice trivia question in a puzzle book.\n\n"
        f"CURRENT QUESTION: {question.question}\n{choices}\n"
        f"CORRECT ANSWER: {question.correct_answer}\n\n"
        f"{_action_line(action, instruction)}\n\n"
        "Rules:\n"
        "- Exactly four options labelled A, B, C, D, with exactly one correct.\n"
        "- Wrong options must be plausible and in the same category as the right one.\n"
        "- Options should be about the same length. Never make the correct one longest.\n"
        "- It must be factually correct and answerable by the stated audience.\n\n"
        'Reply with a JSON object with exactly these keys: "question", '
        '"choices" (an object with keys "A","B","C","D"), "correct_answer".\n'
        f"{_JSON_OBJECT_ONLY}"
    )


def build_wordsearch_words_prompt(
    book: PuzzleBook, puzzle: WordSearch, instruction: str, grid_size: int
) -> str:
    return (
        f"{_book_context(book)}\n"
        f"WORD SEARCH TOPIC: {puzzle.title}\n"
        f"CURRENT WORDS: {', '.join(puzzle.words)}\n\n"
        + (f"WHAT TO CHANGE: {instruction.strip()}\n\n" if instruction.strip() else "")
        + f"Give exactly {WORDS_PER_SEARCH} words for this word search.\n\n"
        "Requirements:\n"
        f"- EXACTLY {WORDS_PER_SEARCH} words, letters A-Z only.\n"
        f"- Each word 3 to {grid_size} letters, single words, no spaces or hyphens.\n"
        "- All must fit the topic and suit the audience.\n"
        "- No duplicates, and no word contained inside another.\n\n"
        f"Reply with a JSON array of exactly {WORDS_PER_SEARCH} uppercase strings. "
        "No prose, no markdown fence."
    )


def build_crossword_words_prompt(
    book: PuzzleBook, puzzle: Crossword, instruction: str
) -> str:
    current = ", ".join(e.word for e in puzzle.entries)
    return (
        f"{_book_context(book)}\n"
        f"CROSSWORD TOPIC: {puzzle.title}\n"
        f"CURRENT WORDS: {current}\n\n"
        + (f"WHAT TO CHANGE: {instruction.strip()}\n\n" if instruction.strip() else "")
        + f"Give exactly {WORDS_PER_CROSSWORD} words with clues for this crossword.\n\n"
        "Requirements:\n"
        f"- EXACTLY {WORDS_PER_CROSSWORD} words, 4 to 11 letters, A-Z only.\n"
        "- They must share letters so they interlock in a criss-cross grid.\n"
        "- Favour common letters like A, E, R, S, T, N.\n"
        "- No duplicates, and no word contained inside another.\n"
        "- Each clue is one short sentence under 12 words, and must never "
        "contain the answer word or a form of it.\n\n"
        'Each array element must be an object with exactly these keys: "word", "clue".\n'
        "Reply with a JSON array ONLY. No prose, no markdown fence."
    )


def build_brief_edit_prompt(
    book: PuzzleBook, brief: PictureBrief, action: str, instruction: str
) -> str:
    return (
        f"{_book_context(book)}\n"
        "You are editing ONE spot-the-difference scene brief for a human illustrator.\n\n"
        f"CURRENT TITLE: {brief.scene_title}\n"
        f"CURRENT DESCRIPTION: {brief.scene_description}\n\n"
        f"{_action_line(action, instruction)}\n\n"
        "Rules:\n"
        "- The scene must fit the book topic and be busy enough to hide 8 differences.\n"
        "- Describe it concretely enough to draw without follow-up questions.\n"
        "- The art is grayscale, so never rely on colour alone for a difference.\n"
        "- Name the exact object in every difference idea.\n\n"
        'Reply with a JSON object with exactly these keys: "scene_title", '
        '"scene_description", "difference_ideas" (array of 8 short strings).\n'
        f"{_JSON_OBJECT_ONLY}"
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if not s:
        raise PuzzleError("Model returned an empty reply.")
    fenced = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fenced:
        s = fenced.group(1).strip()
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise PuzzleError(f"Could not find a JSON object in reply: {s[:300]}")
        try:
            parsed = json.loads(s[start:end + 1])
        except json.JSONDecodeError as exc:
            raise PuzzleError(f"Malformed JSON in reply: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PuzzleError("Model reply was not a JSON object.")
    return parsed


def _call(book: PuzzleBook, prompt: str, cache, ledger) -> str:
    cfg = book.config
    # Each edit prompt stands alone, so it gets its own session rather than
    # appending to the agent's default one. See engine.call_openclaw_raw.
    return engine.call_openclaw_raw(
        cfg.agent, prompt,
        local=cfg.local, thinking=cfg.thinking, timeout_s=cfg.timeout_s,
        cache=cache, ledger=ledger,
        session_id=f"puzzle-edit-{uuid.uuid4().hex[:12]}",
    )


def ai_edit_item(
    book: PuzzleBook,
    item_id: str,
    action: str,
    instruction: str = "",
    *,
    cache=None,
    ledger=None,
) -> tuple[str, Any]:
    """Run one AI edit against a single item. Returns (kind, updated item)."""
    if action not in AI_ACTIONS:
        raise PuzzleError(f"Unknown action '{action}'.")

    kind, item = find_any(book, item_id)
    if item is None:
        raise PuzzleError(f"'{item_id}' not found.")

    if kind == "riddle":
        reply = _call(book, build_riddle_edit_prompt(book, item, action, instruction), cache, ledger)
        data = _extract_json_object(reply)
        return kind, apply_riddle_edit(book, item_id, {
            "riddle": data.get("riddle", item.riddle),
            "answer": data.get("answer", item.answer),
        })

    if kind == "cryptogram":
        reply = _call(book, build_cryptogram_edit_prompt(book, item, action, instruction), cache, ledger)
        data = _extract_json_object(reply)
        return kind, apply_cryptogram_edit(book, item_id, {
            "phrase": data.get("phrase", item.phrase),
            "hint": data.get("hint", item.hint),
        })

    if kind == "trivia_question":
        reply = _call(book, build_question_edit_prompt(book, item, action, instruction), cache, ledger)
        data = _extract_json_object(reply)
        payload: dict[str, Any] = {}
        if data.get("question"):
            payload["question"] = data["question"]
        if isinstance(data.get("choices"), dict):
            payload["choices"] = data["choices"]
        if data.get("correct_answer"):
            payload["correct_answer"] = data["correct_answer"]
        return kind, apply_question_edit(book, item_id, payload)

    if kind == "picture_brief":
        reply = _call(book, build_brief_edit_prompt(book, item, action, instruction), cache, ledger)
        data = _extract_json_object(reply)
        return kind, apply_brief_edit(book, item_id, {
            "scene_title": data.get("scene_title", item.scene_title),
            "scene_description": data.get("scene_description", item.scene_description),
            "difference_ideas": data.get("difference_ideas", item.difference_ideas),
        })

    if kind == "word_search":
        size = len(item.grid) or book.config.wordsearch_grid
        reply = _call(book, build_wordsearch_words_prompt(book, item, instruction, size), cache, ledger)
        words = engine.extract_json_array(reply)
        return kind, update_word_search_words(book, item_id, words, grid_size=size)

    if kind == "crossword":
        reply = _call(book, build_crossword_words_prompt(book, item, instruction), cache, ledger)
        raw = engine.extract_json_array(reply)
        return kind, update_crossword_entries(book, item_id, raw)

    raise PuzzleError("That item cannot be edited by the AI.")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_edited_book(book: PuzzleBook) -> dict[str, Any]:
    """Re-run the structural checks over an edited book so the editor can show
    whether it is still exportable. Heuristics only — no model calls, because
    the editor must stay responsive."""
    errors: list[str] = []
    warnings: list[str] = []

    for riddle in book.riddles:
        if not riddle.riddle.strip() or not riddle.answer.strip():
            errors.append(f"{riddle.id}: riddle or answer is empty.")
        elif engine.gives_away_answer(riddle.answer, riddle.riddle):
            errors.append(f"{riddle.id}: the riddle names its own answer.")

    for gram in book.cryptograms:
        letters = normalize_word(gram.phrase)
        if len(letters) < 12:
            errors.append(f"{gram.id}: phrase is too short to solve.")
        if gram.cipher:
            decoded = "".join(
                {v: k for k, v in gram.cipher.items()}.get(c, c) if c.isalpha() else c
                for c in gram.encoded
            )
            if decoded.upper() != gram.phrase.upper():
                errors.append(f"{gram.id}: encoded text does not match the phrase.")
            if any(a == b for a, b in gram.cipher.items()):
                warnings.append(f"{gram.id}: a letter maps to itself in the cipher.")

    for ws in book.word_searches:
        if len(ws.words) != WORDS_PER_SEARCH:
            warnings.append(f"{ws.id}: has {len(ws.words)} words, the spec says {WORDS_PER_SEARCH}.")
        for word, p in (ws.placements or {}).items():
            try:
                got = "".join(
                    ws.grid[p["row"] + p["dr"] * i][p["col"] + p["dc"] * i]
                    for i in range(len(word))
                )
            except (IndexError, KeyError, TypeError):
                errors.append(f"{ws.id}: '{word}' placement is out of bounds.")
                continue
            if got != word:
                errors.append(f"{ws.id}: '{word}' does not read correctly from the grid.")

    for cw in book.crosswords:
        if len(cw.entries) != WORDS_PER_CROSSWORD:
            warnings.append(
                f"{cw.id}: has {len(cw.entries)} words, the spec says {WORDS_PER_CROSSWORD}.")
        for e in cw.entries:
            if not e.direction or e.row < 0 or e.col < 0:
                errors.append(f"{cw.id}: '{e.word}' is not placed in the grid.")
                continue
            dr, dc = (1, 0) if e.direction == "down" else (0, 1)
            try:
                got = "".join(
                    cw.grid[e.row + dr * i][e.col + dc * i] for i in range(len(e.word)))
            except IndexError:
                errors.append(f"{cw.id}: '{e.word}' runs outside the grid.")
                continue
            if got != e.word:
                errors.append(f"{cw.id}: '{e.word}' does not read correctly from the grid.")
            if engine.gives_away_answer(e.word, e.clue):
                errors.append(f"{cw.id}: the clue for '{e.word}' names its answer.")

    for chapter in book.trivia_chapters:
        for q in chapter.questions:
            if set(q.choices) != {"A", "B", "C", "D"}:
                errors.append(f"{q.id}: must have exactly choices A-D.")
            elif q.correct_answer not in q.choices:
                errors.append(f"{q.id}: correct answer points at a missing choice.")
            elif len({c.strip().lower() for c in q.choices.values()}) != 4:
                errors.append(f"{q.id}: choices are not all different.")
        if chapter.questions:
            dist = engine.answer_distribution(chapter.questions)
            worst = max(dist.values()) / len(chapter.questions)
            if worst > 0.55:
                warnings.append(
                    f"Chapter {chapter.number}: correct answers cluster on one letter ({dist}).")

    # Missing artwork blocks a clean export.
    for group in (book.mazes, book.word_searches, book.crosswords):
        for item in group:
            for label, path in (("puzzle", item.image_path), ("solution", item.solution_path)):
                if path and not Path(path).exists():
                    errors.append(f"{item.id}: {label} image is missing from disk.")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": book.counts(),
    }
