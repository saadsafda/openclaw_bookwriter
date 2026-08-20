"""Puzzle book build pipeline.

Orchestrates every section of the Puzzle/Activity Book Production Spec:
picture-puzzle briefs (Section 1, human handoff), mazes (2), riddles (3), word
searches (4), cryptograms (5), trivia (6), crosswords (7), and the consolidated
answer key (8).

Two rules shape the design:

  * Text sections generate in batches, then every item is checked against the
    section's hard constraint and against everything already accepted. Failures
    are refilled rather than shipped.
  * Grid sections (word search, crossword) can fail on the *layout* even when
    the word list is valid — a word may not interlock. Those retry with a fresh
    word list, so a single stubborn word never sinks a build.

Kept separate from trivia/pipeline.py: its unit of work is a validated
question, this one's is a puzzle plus its rendered artwork and solution.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import engine, generators
from .engine import (
    SECTION_CROSSWORDS,
    SECTION_CRYPTOGRAMS,
    SECTION_MAZES,
    SECTION_PICTURE,
    SECTION_RIDDLES,
    SECTION_TRIVIA,
    SECTION_WORDSEARCH,
    TRIVIA_QUESTIONS_PER_CHAPTER,
    WORDS_PER_CROSSWORD,
    WORDS_PER_SEARCH,
    BookConfig,
    Crossword,
    Cryptogram,
    Maze,
    ProviderRejectionError,
    PuzzleBook,
    PuzzleError,
    TriviaChapter,
    WordSearch,
)

# Extra attempts to fill a section's quota when items are rejected.
MAX_REFILL_ROUNDS = 5
# Attempts to get a *layout* out of a valid word list before giving up.
MAX_LAYOUT_ATTEMPTS = 4
# A provider rejection is often transient (a momentary schema/payload fault),
# so one is retried with a short backoff before the section gives up. Without
# this a single blip took out a whole section — see build_riddles.
PROVIDER_RETRIES = 2
PROVIDER_BACKOFF_S = 5.0

LogFn = Callable[[str], None]
ProgressFn = Callable[[str, float], None]


def _noop_log(_msg: str) -> None:
    return None


def _noop_progress(_stage: str, _pct: float) -> None:
    return None


class PuzzleBuilder:
    def __init__(
        self,
        cfg: BookConfig,
        *,
        log: Optional[LogFn] = None,
        progress: Optional[ProgressFn] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        cache_dir: Optional[Path] = None,
        seed: int = 20260731,
    ) -> None:
        self.cfg = cfg
        self.log = log or _noop_log
        self.progress = progress or _noop_progress
        self.should_stop = should_stop or (lambda: False)
        self.seed = seed
        self.ledger = engine.UsageLedger()
        self.cache: Optional[engine.RawOutputCache] = None
        if cache_dir is not None:
            self.cache = engine.RawOutputCache(Path(cache_dir))
        self.book = PuzzleBook(config=cfg)

    # -- helpers ---------------------------------------------------------

    def _check_stop(self) -> None:
        if self.should_stop():
            raise PuzzleError("Build stopped by operator.")

    def _ask(self, prompt: str) -> list[Any]:
        """One model call returning a JSON array.

        Provider rejections get their own short retry with a backoff: they are
        usually transient, and the caller's refill loop cannot distinguish them
        from a content failure, so it would otherwise spend its whole budget
        re-sending a prompt that fails instantly every time.
        """
        last: Optional[ProviderRejectionError] = None
        for attempt in range(PROVIDER_RETRIES + 1):
            if attempt:
                delay = PROVIDER_BACKOFF_S * attempt
                self.log(f"  provider refused; retrying in {delay:.0f}s")
                time.sleep(delay)
                self._check_stop()
            try:
                reply = engine.call_openclaw_raw(
                    self.cfg.agent,
                    prompt,
                    local=self.cfg.local,
                    thinking=self.cfg.thinking,
                    timeout_s=self.cfg.timeout_s,
                    cache=self.cache,
                    ledger=self.ledger,
                )
            except ProviderRejectionError as exc:
                last = exc
                continue
            return engine.extract_json_array(reply)

        raise last if last is not None else PuzzleError("Model call failed.")

    def _warn(self, message: str) -> None:
        self.log(f"WARNING: {message}")
        self.book.warnings.append(message)

    def _subjects_for(self, kind: str, count: int) -> list[str]:
        """Operator-supplied subjects, topped up by the model when short.

        The spec sources these from the book outline; when the operator gives
        none we derive them from the book topic instead.
        """
        cfg_section = self.cfg.section(kind)
        subjects = list(cfg_section.subjects[:count])
        if len(subjects) >= count:
            return subjects

        missing = count - len(subjects)
        self.log(f"{engine.SECTION_LABELS[kind]}: inventing {missing} subject(s) from the topic")
        try:
            raw = self._ask(engine.build_section_subject_prompt(self.cfg, kind, missing))
            invented = engine.parse_string_list(raw, missing)
        except PuzzleError as exc:
            self._warn(f"{engine.SECTION_LABELS[kind]}: subject generation failed ({exc})")
            invented = []

        subjects.extend(invented)
        # Never leave a puzzle unnamed. The topic is frequently a long
        # comma-separated list, so mine it for real subjects before falling
        # back to a numbered label — pasting the whole list in as a "subject"
        # produces an unusable puzzle prompt and an unreadable warning.
        if len(subjects) < count:
            taken = {s.strip().lower() for s in subjects}
            for piece in engine.topic_keywords(self.cfg.topic):
                if len(subjects) >= count:
                    break
                if piece.lower() not in taken:
                    subjects.append(piece)
                    taken.add(piece.lower())

        short_topic = engine.short_topic(self.cfg.topic)
        while len(subjects) < count:
            subjects.append(f"{short_topic} {len(subjects) + 1}")
        return subjects[:count]

    # -- Section 1: picture puzzle briefs (human illustrators) -------------

    def build_picture_briefs(self) -> None:
        section = self.cfg.section(SECTION_PICTURE)
        if not section.enabled or section.count <= 0:
            return
        self.log(f"Picture puzzles: briefing {section.count} scene(s) for the illustrators")

        collected: list[Any] = []
        rounds = 0
        while len(collected) < section.count and rounds < MAX_REFILL_ROUNDS:
            self._check_stop()
            rounds += 1
            need = section.count - len(collected)
            subjects = self._subjects_for(SECTION_PICTURE, section.count)[len(collected):]
            prompt = engine.build_picture_brief_prompt(self.cfg, need, subjects)
            try:
                raw = self._ask(prompt)
            except PuzzleError as exc:
                self.log(f"  batch failed ({exc}); retrying")
                continue
            parsed, rejects = engine.parse_picture_briefs(raw, len(collected) + 1)
            for reason in rejects[:5]:
                self.log(f"  rejected: {reason}")
            collected.extend(parsed[:need])
            self.log(f"  {len(collected)}/{section.count} scene briefs")

        if len(collected) < section.count:
            self._warn(
                f"Picture puzzles: only {len(collected)} of {section.count} briefs generated."
            )
        self.book.picture_briefs = collected[:section.count]

    # -- Section 2: mazes --------------------------------------------------

    def build_mazes(self, out_dir: Path) -> None:
        section = self.cfg.section(SECTION_MAZES)
        if not section.enabled or section.count <= 0:
            return
        self.log(f"Mazes: generating {section.count} maze(s) at {generators.PRINT_DPI} DPI")

        maze_dir = out_dir / "mazes"
        # Ramp size across the section so the book gets harder as it goes.
        base_cols, base_rows = self.cfg.maze_cols, self.cfg.maze_rows
        for i in range(section.count):
            self._check_stop()
            n = i + 1
            grow = i // max(1, section.count // 3)
            cols = min(40, base_cols + grow * 2)
            rows = min(50, base_rows + grow * 2)
            seed = self.seed + n * 977

            grid = generators.generate_maze(cols, rows, seed)
            title = f"Maze {n}"
            puzzle_png = generators.render_maze(
                grid, maze_dir / f"maze_{n:02d}.png",
                title=title, subtitle="Find your way from START to END.",
            )
            solution_png = generators.render_maze(
                grid, maze_dir / f"maze_{n:02d}_solution.png",
                title=f"Maze {n} — Solution", solution=True,
            )
            self.book.mazes.append(Maze(
                id=f"maze{n:02d}", number=n, title=title, cols=cols, rows=rows,
                image_path=str(puzzle_png), solution_path=str(solution_png), seed=seed,
            ))
            self.log(f"  maze {n}/{section.count} ({cols}x{rows})")

    # -- Section 3: riddles ------------------------------------------------

    def build_riddles(self) -> None:
        section = self.cfg.section(SECTION_RIDDLES)
        if not section.enabled or section.count <= 0:
            return
        target = section.count
        self.log(f"Riddles: generating {target}")

        accepted: list[engine.Riddle] = []
        rounds = 0
        while len(accepted) < target and rounds < MAX_REFILL_ROUNDS + target // engine.RIDDLE_BATCH:
            self._check_stop()
            rounds += 1
            need = target - len(accepted)
            batch = min(engine.RIDDLE_BATCH, need)
            avoid = [r.answer for r in accepted]

            try:
                raw = self._ask(engine.build_riddle_prompt(self.cfg, batch, avoid))
            except PuzzleError as exc:
                self.log(f"  batch failed ({exc}); retrying")
                continue

            parsed, rejects = engine.parse_riddles(raw, len(accepted) + 1)
            for reason in rejects[:5]:
                self.log(f"  rejected: {reason}")

            # Drop any riddle reusing an answer we already have.
            used = {r.answer.strip().lower() for r in accepted}
            fresh = [r for r in parsed if r.answer.strip().lower() not in used]
            fresh = engine.dedup_by_text(fresh)
            dropped = len(parsed) - len(fresh)
            if dropped:
                self.log(f"  dropped {dropped} duplicate riddle(s)")

            accepted.extend(fresh[:need])
            self.log(f"  {len(accepted)}/{target} riddles")

        if len(accepted) < target:
            self._warn(f"Riddles: only {len(accepted)} of {target} generated.")

        for i, riddle in enumerate(accepted, start=1):
            riddle.number = i
            riddle.id = f"rid{i:02d}"
        self.book.riddles = accepted[:target]

    # -- Section 4: word searches -----------------------------------------

    def build_word_searches(self, out_dir: Path) -> None:
        section = self.cfg.section(SECTION_WORDSEARCH)
        if not section.enabled or section.count <= 0:
            return
        self.log(f"Word searches: generating {section.count}")

        subjects = self._subjects_for(SECTION_WORDSEARCH, section.count)
        ws_dir = out_dir / "word_searches"
        size = self.cfg.wordsearch_grid

        for i, subject in enumerate(subjects, start=1):
            self._check_stop()
            puzzle = self._one_word_search(i, subject, size, ws_dir)
            if puzzle is not None:
                self.book.word_searches.append(puzzle)
                self.log(f"  word search {i}/{section.count}: {subject}")

    def _one_word_search(
        self, number: int, subject: str, size: int, out_dir: Path
    ) -> Optional[WordSearch]:
        """Generate a word list and lay it out, retrying the whole pair.

        A list can be individually valid yet still fail to place, so a layout
        failure asks for a fresh list rather than retrying the same words.
        """
        for attempt in range(1, MAX_LAYOUT_ATTEMPTS + 1):
            prompt = engine.build_wordsearch_prompt(self.cfg, subject, size)
            if attempt > 1:
                # Vary the prompt so the cache misses and the model re-answers.
                prompt += f"\n\nAttempt {attempt}: give a different set of words, and prefer shorter ones."
            try:
                raw = self._ask(prompt)
            except PuzzleError as exc:
                self.log(f"  '{subject}': word list failed ({exc})")
                continue

            words, rejects = engine.parse_word_list(raw, max_len=size, want=WORDS_PER_SEARCH)
            for reason in rejects[:3]:
                self.log(f"    rejected: {reason}")
            if len(words) < WORDS_PER_SEARCH:
                self.log(
                    f"  '{subject}': got {len(words)} usable words, need "
                    f"{WORDS_PER_SEARCH} (attempt {attempt})"
                )
                continue

            try:
                grid, placements = generators.build_word_search(
                    words, size, seed=self.seed + number * 613 + attempt,
                    difficulty=self.cfg.difficulty,
                )
            except PuzzleError as exc:
                self.log(f"  '{subject}': layout failed ({exc}); new word list")
                continue

            puzzle = WordSearch(
                id=f"ws{number:02d}", number=number, title=subject,
                words=words, grid=grid, placements=placements,
            )
            puzzle.image_path = str(generators.render_word_search(
                puzzle, out_dir / f"wordsearch_{number:02d}.png"))
            puzzle.solution_path = str(generators.render_word_search(
                puzzle, out_dir / f"wordsearch_{number:02d}_solution.png", solution=True))
            return puzzle

        self._warn(f"Word search '{subject}' could not be built after {MAX_LAYOUT_ATTEMPTS} attempts.")
        return None

    # -- Section 5: cryptograms -------------------------------------------

    def build_cryptograms(self) -> None:
        section = self.cfg.section(SECTION_CRYPTOGRAMS)
        if not section.enabled or section.count <= 0:
            return
        target = section.count
        self.log(f"Cryptograms: generating {target}")

        accepted: list[Cryptogram] = []
        rounds = 0
        while len(accepted) < target and rounds < MAX_REFILL_ROUNDS + target // engine.CRYPTOGRAM_BATCH:
            self._check_stop()
            rounds += 1
            need = target - len(accepted)
            batch = min(engine.CRYPTOGRAM_BATCH, need)
            avoid = [c.phrase for c in accepted]

            try:
                raw = self._ask(engine.build_cryptogram_prompt(self.cfg, batch, avoid))
            except PuzzleError as exc:
                self.log(f"  batch failed ({exc}); retrying")
                continue

            parsed, rejects = engine.parse_cryptograms(raw, len(accepted) + 1)
            for reason in rejects[:5]:
                self.log(f"  rejected: {reason}")

            fresh = engine.dedup_by_text(parsed)
            accepted.extend(fresh[:need])
            self.log(f"  {len(accepted)}/{target} cryptograms")

        if len(accepted) < target:
            self._warn(f"Cryptograms: only {len(accepted)} of {target} generated.")

        for i, gram in enumerate(accepted, start=1):
            gram.number = i
            gram.id = f"cry{i:02d}"
        engine.apply_cryptogram_ciphers(accepted, seed_base=self.seed)
        self.book.cryptograms = accepted[:target]

    # -- Section 6: trivia -------------------------------------------------

    def build_trivia(self) -> None:
        section = self.cfg.section(SECTION_TRIVIA)
        if not section.enabled or section.count <= 0:
            return
        chapter_count = section.count
        self.log(
            f"Trivia: {chapter_count} chapter(s) x {TRIVIA_QUESTIONS_PER_CHAPTER} questions"
        )

        # Step 1 — chapter themes (spec Section 6, step 1).
        themes: list[tuple[str, str]] = []
        supplied = self.cfg.section(SECTION_TRIVIA).subjects
        if supplied:
            themes = [(t, "") for t in supplied[:chapter_count]]
        if len(themes) < chapter_count:
            try:
                raw = self._ask(engine.build_trivia_theme_prompt(
                    self.cfg, chapter_count - len(themes)))
                for item in raw:
                    if isinstance(item, dict):
                        title = str(item.get("chapter_title") or "").strip()
                        scope = str(item.get("chapter_scope") or "").strip()
                    else:
                        title, scope = str(item).strip(), ""
                    if title:
                        themes.append((title, scope))
            except PuzzleError as exc:
                self._warn(f"Trivia: theme generation failed ({exc})")
        while len(themes) < chapter_count:
            themes.append((f"{self.cfg.topic} Facts {len(themes) + 1}", ""))

        # Step 2 — questions per chapter.
        for idx, (title, scope) in enumerate(themes[:chapter_count], start=1):
            self._check_stop()
            chapter = TriviaChapter(number=idx, title=title)
            accepted: list[engine.TriviaQuestion] = []
            rounds = 0

            while len(accepted) < TRIVIA_QUESTIONS_PER_CHAPTER and rounds < MAX_REFILL_ROUNDS:
                rounds += 1
                need = TRIVIA_QUESTIONS_PER_CHAPTER - len(accepted)
                # Avoid repeating facts used anywhere in the book so far.
                avoid = [
                    q.claim_text()
                    for ch in self.book.trivia_chapters for q in ch.questions
                ] + [q.claim_text() for q in accepted]

                try:
                    raw = self._ask(engine.build_trivia_question_prompt(
                        self.cfg, title, scope, need, avoid))
                except PuzzleError as exc:
                    self.log(f"  chapter {idx} batch failed ({exc}); retrying")
                    continue

                parsed, rejects = engine.parse_trivia_questions(
                    raw, idx, len(accepted) + 1)
                for reason in rejects[:3]:
                    self.log(f"    rejected: {reason}")

                prior = accepted + [q for ch in self.book.trivia_chapters for q in ch.questions]
                fresh = [
                    q for q in parsed
                    if not any(engine.jaccard(q.claim_text(), p.claim_text())
                               >= engine.NEAR_DUPLICATE_JACCARD for p in prior)
                ]
                accepted.extend(fresh[:need])

            if len(accepted) < TRIVIA_QUESTIONS_PER_CHAPTER:
                self._warn(
                    f"Trivia chapter {idx} '{title}': only {len(accepted)} of "
                    f"{TRIVIA_QUESTIONS_PER_CHAPTER} questions."
                )

            for i, q in enumerate(accepted, start=1):
                q.number = i
                q.id = f"ch{idx}_q{i:02d}"
            # Keep the answer key from being guessable.
            engine.rebalance_answer_distribution(accepted)
            chapter.questions = accepted
            self.book.trivia_chapters.append(chapter)
            self.log(f"  chapter {idx}/{chapter_count} '{title}': {len(accepted)} questions")

    # -- Section 7: crosswords --------------------------------------------

    def build_crosswords(self, out_dir: Path) -> None:
        section = self.cfg.section(SECTION_CROSSWORDS)
        if not section.enabled or section.count <= 0:
            return
        self.log(f"Crosswords: generating {section.count}")

        subjects = self._subjects_for(SECTION_CROSSWORDS, section.count)
        cw_dir = out_dir / "crosswords"

        for i, subject in enumerate(subjects, start=1):
            self._check_stop()
            puzzle = self._one_crossword(i, subject, cw_dir)
            if puzzle is not None:
                self.book.crosswords.append(puzzle)
                self.log(f"  crossword {i}/{section.count}: {subject}")

    def _one_crossword(self, number: int, subject: str, out_dir: Path) -> Optional[Crossword]:
        for attempt in range(1, MAX_LAYOUT_ATTEMPTS + 1):
            prompt = engine.build_crossword_prompt(self.cfg, subject)
            if attempt > 1:
                prompt += (
                    f"\n\nAttempt {attempt}: give a different set of words that share "
                    "more letters with each other, so they interlock more easily."
                )
            try:
                raw = self._ask(prompt)
            except PuzzleError as exc:
                self.log(f"  '{subject}': word list failed ({exc})")
                continue

            entries, rejects = engine.parse_crossword_words(raw)
            for reason in rejects[:3]:
                self.log(f"    rejected: {reason}")
            if len(entries) < WORDS_PER_CROSSWORD:
                self.log(
                    f"  '{subject}': got {len(entries)} usable words, need "
                    f"{WORDS_PER_CROSSWORD} (attempt {attempt})"
                )
                continue
            entries = entries[:WORDS_PER_CROSSWORD]

            try:
                grid, numbers = generators.build_crossword(
                    entries, seed=self.seed + number * 421 + attempt)
            except PuzzleError as exc:
                self.log(f"  '{subject}': layout failed ({exc}); new word list")
                continue

            puzzle = Crossword(
                id=f"cw{number:02d}", number=number, title=subject,
                entries=entries, grid=grid, numbers=numbers,
            )
            puzzle.image_path = str(generators.render_crossword(
                puzzle, out_dir / f"crossword_{number:02d}.png"))
            puzzle.solution_path = str(generators.render_crossword(
                puzzle, out_dir / f"crossword_{number:02d}_solution.png", solution=True))
            return puzzle

        self._warn(f"Crossword '{subject}' could not be built after {MAX_LAYOUT_ATTEMPTS} attempts.")
        return None

    # -- orchestration -----------------------------------------------------

    def build(self, out_dir: Path) -> PuzzleBook:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        steps: list[tuple[str, Callable[[], None]]] = []
        if self.cfg.section(SECTION_PICTURE).enabled:
            steps.append(("picture briefs", self.build_picture_briefs))
        if self.cfg.section(SECTION_MAZES).enabled:
            steps.append(("mazes", lambda: self.build_mazes(out_dir)))
        if self.cfg.section(SECTION_RIDDLES).enabled:
            steps.append(("riddles", self.build_riddles))
        if self.cfg.section(SECTION_WORDSEARCH).enabled:
            steps.append(("word searches", lambda: self.build_word_searches(out_dir)))
        if self.cfg.section(SECTION_CRYPTOGRAMS).enabled:
            steps.append(("cryptograms", self.build_cryptograms))
        if self.cfg.section(SECTION_TRIVIA).enabled:
            steps.append(("trivia", self.build_trivia))
        if self.cfg.section(SECTION_CROSSWORDS).enabled:
            steps.append(("crosswords", lambda: self.build_crosswords(out_dir)))

        total = len(steps) + 1
        for i, (name, fn) in enumerate(steps):
            self.progress(name, i / total)
            fn()

        self.progress("finalizing", len(steps) / total)
        self.book.usage = self.ledger.to_dict()
        self.progress("done", 1.0)
        return self.book


def write_json(book: PuzzleBook, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(book.to_dict(), indent=2), encoding="utf-8")
    return path


def load_json(path: Path) -> PuzzleBook:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg = BookConfig.from_dict(data.get("config") or {})
    book = PuzzleBook(config=cfg)

    book.picture_briefs = [
        engine.PictureBrief(**{k: v for k, v in p.items() if k in engine.PictureBrief.__dataclass_fields__})
        for p in data.get("picture_briefs") or []
    ]
    book.mazes = [
        engine.Maze(**{k: v for k, v in m.items() if k in engine.Maze.__dataclass_fields__})
        for m in data.get("mazes") or []
    ]
    book.riddles = [
        engine.Riddle(**{k: v for k, v in r.items() if k in engine.Riddle.__dataclass_fields__})
        for r in data.get("riddles") or []
    ]
    book.word_searches = [
        engine.WordSearch(**{k: v for k, v in w.items() if k in engine.WordSearch.__dataclass_fields__})
        for w in data.get("word_searches") or []
    ]
    book.cryptograms = [
        engine.Cryptogram(**{k: v for k, v in c.items() if k in engine.Cryptogram.__dataclass_fields__})
        for c in data.get("cryptograms") or []
    ]
    for ch in data.get("trivia_chapters") or []:
        chapter = TriviaChapter(
            number=int(ch.get("chapter_number") or 0),
            title=str(ch.get("chapter_title") or ""),
        )
        chapter.questions = [
            engine.TriviaQuestion(**{
                k: v for k, v in q.items()
                if k in engine.TriviaQuestion.__dataclass_fields__
            })
            for q in ch.get("questions") or []
        ]
        book.trivia_chapters.append(chapter)
    for c in data.get("crosswords") or []:
        cw = Crossword(
            id=c.get("id", ""), number=int(c.get("number") or 0),
            title=c.get("title", ""), grid=c.get("grid") or [],
            numbers=c.get("numbers") or {},
            image_path=c.get("image_path", ""), solution_path=c.get("solution_path", ""),
        )
        cw.entries = [
            engine.CrosswordEntry(**{
                k: v for k, v in e.items()
                if k in engine.CrosswordEntry.__dataclass_fields__
            })
            for e in c.get("entries") or []
        ]
        book.crosswords.append(cw)

    book.warnings = list(data.get("warnings") or [])
    book.usage = dict(data.get("usage") or {})
    return book
