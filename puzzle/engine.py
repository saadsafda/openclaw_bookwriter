"""Puzzle & Activity book generator — topic-agnostic content engine.

Implements the text-generation half of the Puzzle/Activity Book Production
Spec: riddles, word-search word lists, cryptogram phrases, trivia chapters and
questions, crossword word/clue sets, and the picture-puzzle scene briefs that
go to the human illustrators.

Deliberately separate from both the prose pipeline (openclaw_docx_writer.py)
and the trivia pipeline (trivia/engine.py). A puzzle book is a set of
*constraint-bearing* artifacts — a word search needs exactly 9 words that fit a
grid, a crossword needs 6 interlockable words — so generation here is always
"generate, then verify against the constraint, then refill what failed".
Sharing code with the trivia engine would couple two different validators.

Text generation reuses the openclaw CLI, matching trivia/engine.py and
email_agent.py. Grid rendering lives in generators.py and uses no third-party
service, replacing the spec's mazepuzzlemaker.com and Discovery Education
dependencies.
"""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from openclaw_docx_writer import parse_openclaw_reply

DEFAULT_TIMEOUT = 600
DEFAULT_AGENT = "main"

# --------------------------------------------------------------------------
# Spec constants (Puzzle_Book_Automation_Spec.docx)
# --------------------------------------------------------------------------

# Section 4: every word search uses exactly this many words.
WORDS_PER_SEARCH = 9
# Section 7: every crossword uses exactly this many words. The spec notes this
# constraint was added on a second reply in the reference chat — so it is in
# the prompt up front here.
WORDS_PER_CROSSWORD = 6
# Section 6: 6 themed chapters x 10 multiple-choice questions.
TRIVIA_QUESTIONS_PER_CHAPTER = 10

# Batch sizes. Asking for a whole section at once measurably degrades quality
# (models start recycling stems), same finding as the trivia engine.
RIDDLE_BATCH = 10
CRYPTOGRAM_BATCH = 10

# Two items whose token overlap exceeds this are treated as near-duplicates.
NEAR_DUPLICATE_JACCARD = 0.62

# Print requirements applied to every generated asset.
PRINT_DPI = 300
PAGE_W_IN = 6.0
PAGE_H_IN = 9.0

_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "but",
    "is", "was", "were", "are", "be", "been", "which", "who", "whom", "whose",
    "what", "when", "where", "how", "why", "that", "this", "these", "those",
    "it", "its", "as", "by", "with", "from", "his", "her", "their", "did",
    "does", "do", "has", "have", "had", "my", "i", "you", "your", "am",
}


class PuzzleError(RuntimeError):
    """Generation or validation failure that should stop the build."""


class ValidationGateError(PuzzleError):
    """A hard constraint (word counts, grid fit) rejected the book."""


class ProviderRejectionError(PuzzleError):
    """The upstream provider refused the request itself.

    Distinct from a content failure: retrying the identical prompt immediately
    cannot fix it, so callers back off rather than burning their whole refill
    budget on instant re-failures. Also never cached — see RawOutputCache.set.

    Mirrors trivia/engine.py, which hit exactly this failure first.
    """


# OpenClaw exits 0 for these: the CLI ran fine, the *provider* refused, and the
# refusal arrives as ordinary reply text. Matched on the message because there
# is no distinguishing status field to key on.
_PROVIDER_REJECTION_MARKERS = (
    "llm request failed",
    "provider rejected the request",
    "schema or tool payload",
    "request too large",
    "context length exceeded",
    "prompt is too long",
)

# These arrive the other way round: openclaw exits *non-zero* and the cause is
# only in stderr. The provider never ran the prompt, so they are rejections
# rather than content failures — and being unauthenticated or rate-limited
# persists, which is exactly what the outage breaker in pipeline._ask exists
# to short-circuit. Without this a build re-sent every prompt at full retry
# cost against a provider that could not answer any of them.
_PROVIDER_STDERR_MARKERS = (
    "oauth token refresh failed",
    "token refresh failed",
    "transcript compaction failed",
    "gatewayclientrequesterror",
    "401",
    "429",
    "rate limit",
    "quota",
    "503",
    "502",
    "upstream connect error",
)


def is_provider_stderr_failure(stderr: str) -> bool:
    """True when a non-zero openclaw exit was caused upstream, not by us.

    Deliberately narrow: a bad agent name or malformed flag must stay a plain
    PuzzleError so it surfaces immediately instead of being absorbed as a
    transient outage.
    """
    s = (stderr or "").lower()
    return any(marker in s for marker in _PROVIDER_STDERR_MARKERS)


def is_provider_rejection(reply: str) -> bool:
    """True when a reply is an upstream refusal rather than model output.

    Only meaningful for short replies — a legitimate riddle or clue batch could
    quote one of these phrases, but never in a one-line reply.
    """
    s = (reply or "").strip().lower()
    if not s or len(s) > 600:
        return False
    return any(marker in s for marker in _PROVIDER_REJECTION_MARKERS)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

# The topic is embedded in every prompt by _book_context(), so it is capped
# well below any model limit — see BookConfig.from_dict.
MAX_TOPIC_CHARS = 200

SECTION_PICTURE = "picture_puzzles"
SECTION_MAZES = "mazes"
SECTION_RIDDLES = "riddles"
SECTION_WORDSEARCH = "word_searches"
SECTION_CRYPTOGRAMS = "cryptograms"
SECTION_TRIVIA = "trivia"
SECTION_CROSSWORDS = "crosswords"

ALL_SECTIONS = (
    SECTION_PICTURE,
    SECTION_MAZES,
    SECTION_RIDDLES,
    SECTION_WORDSEARCH,
    SECTION_CRYPTOGRAMS,
    SECTION_TRIVIA,
    SECTION_CROSSWORDS,
)

SECTION_LABELS = {
    SECTION_PICTURE: "Picture Puzzles",
    SECTION_MAZES: "Mazes",
    SECTION_RIDDLES: "Riddles",
    SECTION_WORDSEARCH: "Word Searches",
    SECTION_CRYPTOGRAMS: "Cryptograms",
    SECTION_TRIVIA: "Trivia Questions",
    SECTION_CROSSWORDS: "Crossword Puzzles",
}

# Spec defaults for the pilot book (Crime Scene Puzzle Book, ages 8-12).
DEFAULT_COUNTS = {
    SECTION_PICTURE: 12,
    SECTION_MAZES: 12,
    SECTION_RIDDLES: 20,
    SECTION_WORDSEARCH: 10,
    SECTION_CRYPTOGRAMS: 10,
    SECTION_TRIVIA: 6,       # chapters, 10 questions each
    SECTION_CROSSWORDS: 14,
}

# Pages consumed per unit, used for the page-budget estimate in the spec's
# "Book Structure & Page Budget" table.
_PAGES_PER_UNIT = {
    SECTION_PICTURE: 2.0,            # left/right page pair
    SECTION_MAZES: 1.0,
    SECTION_RIDDLES: 0.5,            # 20 riddles -> 10 pages
    SECTION_WORDSEARCH: 1.0,
    SECTION_CRYPTOGRAMS: 1.0,
    SECTION_TRIVIA: 3.0,             # 6 chapters -> 18 pages
    SECTION_CROSSWORDS: 1.0,
}


@dataclass
class SectionConfig:
    """One section of the book. `count` means puzzles, except for trivia where
    it means chapters (each chapter holding TRIVIA_QUESTIONS_PER_CHAPTER)."""
    kind: str
    enabled: bool = True
    count: int = 0
    # Optional operator-supplied subjects, one per puzzle. When shorter than
    # `count`, the model invents the remainder from the book topic.
    subjects: list[str] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict[str, Any], kind: str) -> "SectionConfig":
        if not isinstance(d, dict):
            d = {}
        raw_count = d.get("count", DEFAULT_COUNTS.get(kind, 0))
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            raise PuzzleError(f"{SECTION_LABELS.get(kind, kind)}: count must be a whole number.")
        if count < 0:
            raise PuzzleError(f"{SECTION_LABELS.get(kind, kind)}: count cannot be negative.")

        subjects_raw = d.get("subjects") or []
        if isinstance(subjects_raw, str):
            subjects_raw = [s for s in subjects_raw.splitlines()]
        subjects = [str(s).strip() for s in subjects_raw if str(s).strip()]

        enabled = d.get("enabled", True)
        if not isinstance(enabled, bool):
            enabled = str(enabled).strip().lower() in {"1", "true", "yes", "on"}

        return SectionConfig(kind=kind, enabled=enabled, count=count, subjects=subjects)

    def page_estimate(self) -> float:
        if not self.enabled:
            return 0.0
        return self.count * _PAGES_PER_UNIT.get(self.kind, 1.0)


@dataclass
class BookConfig:
    book_title: str
    topic: str
    audience: str = "kids ages 8-12"
    difficulty: str = "medium"
    sections: dict[str, SectionConfig] = field(default_factory=dict)

    # Runtime knobs (not part of the operator-facing spec schema).
    agent: str = DEFAULT_AGENT
    thinking: str = ""
    local: bool = False
    timeout_s: int = DEFAULT_TIMEOUT
    # Grid rendering
    wordsearch_grid: int = 13
    maze_cols: int = 14
    maze_rows: int = 20
    maze_difficulty: str = "medium"
    # Picture puzzles stay human (Section 1); we only brief the illustrators.
    picture_briefs_only: bool = True

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "BookConfig":
        if not isinstance(d, dict):
            raise PuzzleError("Config must be a JSON object.")
        title = str(d.get("book_title") or "").strip()
        topic = str(d.get("topic") or "").strip()
        if not title:
            raise PuzzleError("book_title is required.")
        if not topic:
            raise PuzzleError("topic is required.")
        # A topic is pasted into every single prompt by _book_context(), so an
        # oversized one inflates each call until the provider rejects the
        # payload outright and the whole build's model budget is skipped.
        if len(topic) > MAX_TOPIC_CHARS:
            raise PuzzleError(
                f"topic is {len(topic)} characters, over the {MAX_TOPIC_CHARS} "
                "limit. It is included in every prompt, so a long list of "
                "keywords makes the provider reject the request. Give a short "
                "phrase describing the book instead, and put the keyword list "
                "in each section's subjects box."
            )

        raw_sections = d.get("sections") or {}
        if not isinstance(raw_sections, dict):
            raise PuzzleError("sections must be an object keyed by section name.")

        sections: dict[str, SectionConfig] = {}
        for kind in ALL_SECTIONS:
            sections[kind] = SectionConfig.from_dict(raw_sections.get(kind) or {}, kind)

        if not any(s.enabled and s.count > 0 for s in sections.values()):
            raise PuzzleError("At least one section must be enabled with a count above zero.")

        def _flag(key: str, default: bool) -> bool:
            v = d.get(key, default)
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in {"1", "true", "yes", "on"}

        def _int(key: str, default: int, lo: int, hi: int) -> int:
            try:
                v = int(d.get(key, default) or default)
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        return BookConfig(
            book_title=title,
            topic=topic,
            audience=str(d.get("audience") or "kids ages 8-12").strip(),
            difficulty=str(d.get("difficulty") or "medium").strip(),
            sections=sections,
            agent=str(d.get("agent") or DEFAULT_AGENT).strip() or DEFAULT_AGENT,
            thinking=str(d.get("thinking") or "").strip(),
            local=_flag("local", False),
            timeout_s=int(d.get("timeout_s") or DEFAULT_TIMEOUT),
            wordsearch_grid=_int("wordsearch_grid", 13, 10, 20),
            maze_cols=_int("maze_cols", 14, 6, 40),
            maze_rows=_int("maze_rows", 20, 6, 50),
            maze_difficulty=str(d.get("maze_difficulty") or "medium").strip(),
            picture_briefs_only=_flag("picture_briefs_only", True),
        )

    def section(self, kind: str) -> SectionConfig:
        return self.sections.get(kind) or SectionConfig(kind=kind, enabled=False, count=0)

    def page_estimate(self) -> dict[str, Any]:
        """Mirror of the spec's page budget table."""
        puzzle_pages = sum(s.page_estimate() for s in self.sections.values())
        # Answer key: solution images are printed at roughly half size, so two
        # fit per page. Text answers condense many per page. These ratios
        # reproduce the spec's 26-page answer key for the pilot book.
        ak = 0.0
        ak += self.section(SECTION_PICTURE).count * 0.5
        ak += self.section(SECTION_MAZES).count * 0.5
        ak += self.section(SECTION_WORDSEARCH).count * 0.5
        ak += self.section(SECTION_CROSSWORDS).count * 0.5
        ak += self.section(SECTION_RIDDLES).count / 20.0
        ak += self.section(SECTION_CRYPTOGRAMS).count / 10.0
        ak += self.section(SECTION_TRIVIA).count * 0.5
        enabled = [k for k in ALL_SECTIONS if self.section(k).enabled and self.section(k).count]
        # One title page per section plus a handful of front-matter pages.
        chrome = len(enabled) + 5
        return {
            "puzzle_pages": int(round(puzzle_pages)),
            "answer_key_pages": int(round(ak)),
            "title_pages": chrome,
            "total_pages": int(round(puzzle_pages + ak + chrome)),
            "per_section": {
                k: int(round(self.section(k).page_estimate())) for k in ALL_SECTIONS
            },
        }

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sections"] = {k: asdict(v) for k, v in self.sections.items()}
        return d


# --------------------------------------------------------------------------
# Content models
# --------------------------------------------------------------------------

@dataclass
class Riddle:
    id: str
    number: int
    riddle: str
    answer: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def claim_text(self) -> str:
        return f"{self.riddle} {self.answer}"


@dataclass
class WordSearch:
    id: str
    number: int
    title: str
    words: list[str] = field(default_factory=list)
    grid: list[list[str]] = field(default_factory=list)
    # placements: word -> {row, col, dr, dc}
    placements: dict[str, dict[str, int]] = field(default_factory=dict)
    image_path: str = ""
    solution_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Cryptogram:
    id: str
    number: int
    phrase: str
    hint: str = ""
    cipher: dict[str, str] = field(default_factory=dict)
    encoded: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def claim_text(self) -> str:
        return self.phrase


@dataclass
class TriviaQuestion:
    id: str
    chapter: int
    number: int
    question: str
    choices: dict[str, str] = field(default_factory=dict)
    correct_answer: str = "A"

    def correct_text(self) -> str:
        return self.choices.get(self.correct_answer, "")

    def claim_text(self) -> str:
        return f"{self.question} {self.correct_text()}".strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TriviaChapter:
    number: int
    title: str
    questions: list[TriviaQuestion] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_number": self.number,
            "chapter_title": self.title,
            "questions": [q.to_dict() for q in self.questions],
        }


@dataclass
class CrosswordEntry:
    word: str
    clue: str
    row: int = -1
    col: int = -1
    direction: str = ""     # "across" | "down"
    number: int = 0


@dataclass
class Crossword:
    id: str
    number: int
    title: str
    entries: list[CrosswordEntry] = field(default_factory=list)
    grid: list[list[str]] = field(default_factory=list)
    numbers: dict[str, int] = field(default_factory=dict)   # "r,c" -> number
    image_path: str = ""
    solution_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "number": self.number,
            "title": self.title,
            "entries": [asdict(e) for e in self.entries],
            "grid": self.grid,
            "numbers": self.numbers,
            "image_path": self.image_path,
            "solution_path": self.solution_path,
        }


@dataclass
class Maze:
    id: str
    number: int
    title: str
    cols: int = 0
    rows: int = 0
    image_path: str = ""
    solution_path: str = ""
    seed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PictureBrief:
    """Section 1 stays human. This is the illustrator brief only."""
    id: str
    number: int
    scene_title: str
    scene_description: str
    difference_ideas: list[str] = field(default_factory=list)
    specs: str = f"grayscale, {PAGE_W_IN:g}x{PAGE_H_IN:g} in, {PRINT_DPI} DPI"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PuzzleBook:
    config: BookConfig
    picture_briefs: list[PictureBrief] = field(default_factory=list)
    mazes: list[Maze] = field(default_factory=list)
    riddles: list[Riddle] = field(default_factory=list)
    word_searches: list[WordSearch] = field(default_factory=list)
    cryptograms: list[Cryptogram] = field(default_factory=list)
    trivia_chapters: list[TriviaChapter] = field(default_factory=list)
    crosswords: list[Crossword] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            SECTION_PICTURE: len(self.picture_briefs),
            SECTION_MAZES: len(self.mazes),
            SECTION_RIDDLES: len(self.riddles),
            SECTION_WORDSEARCH: len(self.word_searches),
            SECTION_CRYPTOGRAMS: len(self.cryptograms),
            SECTION_TRIVIA: len(self.trivia_chapters),
            SECTION_CROSSWORDS: len(self.crosswords),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_title": self.config.book_title,
            "topic": self.config.topic,
            "audience": self.config.audience,
            "difficulty": self.config.difficulty,
            "config": self.config.to_dict(),
            "counts": self.counts(),
            "page_estimate": self.config.page_estimate(),
            "picture_briefs": [p.to_dict() for p in self.picture_briefs],
            "mazes": [m.to_dict() for m in self.mazes],
            "riddles": [r.to_dict() for r in self.riddles],
            "word_searches": [w.to_dict() for w in self.word_searches],
            "cryptograms": [c.to_dict() for c in self.cryptograms],
            "trivia_chapters": [t.to_dict() for t in self.trivia_chapters],
            "crosswords": [c.to_dict() for c in self.crosswords],
            "warnings": list(self.warnings),
            "usage": dict(self.usage),
        }


# --------------------------------------------------------------------------
# Text normalization / similarity
# --------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    norm = unicodedata.normalize("NFKD", text or "")
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    words = re.findall(r"[a-z0-9]+", norm.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    union = len(ta | tb)
    return len(ta & tb) / union if union else 0.0


def normalize_word(word: str) -> str:
    """Uppercase A-Z only — the form used inside every grid."""
    norm = unicodedata.normalize("NFKD", word or "")
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z]", "", norm).upper()


def gives_away_answer(answer: str, text: str) -> bool:
    """True when `text` names the answer outright.

    Compares whole words, not a collapsed substring: stripping spaces first
    would make "a clue nearby" appear to contain CLUENEARBY, and would flag
    any answer that happens to sit inside a longer unrelated word. A stem
    match is still caught ("fingerprints" gives away FINGERPRINT).
    """
    target = normalize_word(answer)
    if not target:
        return False
    for token in re.findall(r"[A-Za-z]+", text or ""):
        token = token.upper()
        if token == target:
            return True
        # Simple plural/participle forms of the answer still give it away.
        if token.startswith(target) and token[len(target):] in {"S", "ES", "ED", "ING", "D"}:
            return True
    return False


# --------------------------------------------------------------------------
# Usage ledger and raw-output cache
#
# Same contract as trivia/engine.py: every model reply is cached on disk keyed
# by prompt hash so a re-run never re-buys content, and cost is priced from
# OpenClaw's own lastCallUsage block via the writer's proven parser.
# --------------------------------------------------------------------------

@dataclass
class UsageLedger:
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def record(self, stdout: str) -> None:
        from openclaw_docx_writer import _iter_json_objects, parse_openclaw_usage_cost

        self.calls += 1
        self.cost_usd += parse_openclaw_usage_cost(stdout)
        for obj in _iter_json_objects(stdout or ""):
            try:
                usage = obj["result"]["meta"]["agentMeta"]["lastCallUsage"]
            except (KeyError, TypeError):
                continue
            try:
                self.input_tokens += int(usage.get("input", 0) or 0)
                self.output_tokens += int(usage.get("output", 0) or 0)
                self.cache_read_tokens += int(usage.get("cacheRead", 0) or 0)
                self.cache_write_tokens += int(usage.get("cacheWrite", 0) or 0)
            except (TypeError, ValueError):
                pass
            break

    def note_cache_hit(self) -> None:
        self.cache_hits += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


class RawOutputCache:
    """Raw model stdout keyed by prompt hash."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(agent_id: str, message: str) -> str:
        import hashlib
        h = hashlib.sha256()
        h.update(agent_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(message.encode("utf-8"))
        return h.hexdigest()[:32]

    def get(self, key: str) -> Optional[str]:
        f = self.path / f"{key}.json"
        if f.exists() and f.stat().st_size > 0:
            return f.read_text(encoding="utf-8")
        return None

    def set(self, key: str, stdout: str, prompt: str = "") -> None:
        """Store a reply, but only one worth replaying.

        A provider rejection is a well-formed JSON envelope carrying an error
        sentence as its payload text, so an emptiness check alone lets it
        through. Caching one is permanent: the next run keys off the identical
        prompt, hits this entry, and re-raises the failure without ever calling
        the provider — so the build can never recover on its own, and reports
        cache hits at zero cost while producing nothing.
        """
        if not (stdout or "").strip():
            return
        if is_provider_rejection(parse_openclaw_reply(stdout)):
            return
        (self.path / f"{key}.json").write_text(stdout, encoding="utf-8")
        if prompt:
            (self.path / f"{key}.prompt.txt").write_text(prompt, encoding="utf-8")

    def evict(self, key: str) -> None:
        """Drop an entry that turned out to be unusable.

        Covers caches written before set() screened rejections, so an existing
        poisoned cache heals on the next run instead of needing a manual rm.
        """
        (self.path / f"{key}.json").unlink(missing_ok=True)
        (self.path / f"{key}.prompt.txt").unlink(missing_ok=True)


def _first_cause(stderr: str) -> str:
    """The one line of stderr worth showing an operator.

    openclaw prefixes every failure with routine state-migration warnings and
    ANSI colour codes; echoing the lot (or the prompt that triggered it) made
    the build log unreadable and hid the single line that names the cause.
    """
    plain = re.sub(r"\x1b\[[0-9;]*m", "", stderr or "")
    lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
    for ln in lines:
        low = ln.lower()
        if low.startswith("- ") or "state migration" in low or "left " == low[:5]:
            continue
        if any(m in low for m in _PROVIDER_STDERR_MARKERS):
            return ln[:200]
    return (lines[-1][:200] if lines else "no error detail")


def call_openclaw_raw(
    agent_id: str,
    message: str,
    *,
    local: bool = False,
    thinking: str = "",
    timeout_s: int = DEFAULT_TIMEOUT,
    cache: Optional[RawOutputCache] = None,
    ledger: Optional[UsageLedger] = None,
    session_id: str = "",
) -> str:
    """One openclaw agent call, same shape as trivia/engine.call_openclaw_raw.

    ``session_id`` isolates the build's calls in their own conversation. Every
    prompt is self-contained, so sharing the agent's long-lived default session
    only accumulates history: a book makes dozens of calls, and each one then
    carries every earlier reply along with it. Left unbounded that grew to
    hundreds of messages and the provider began rejecting the payload outright
    — the request never reached the model and no tokens were billed.
    """
    key = RawOutputCache.key_for(agent_id, message) if cache is not None else ""
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            replay = parse_openclaw_reply(hit)
            # A cache written before set() screened rejections would otherwise
            # replay the failure forever at zero cost. Drop it and fall through
            # to a real call so the build can heal itself.
            if is_provider_rejection(replay):
                cache.evict(key)
            else:
                if ledger is not None:
                    ledger.note_cache_hit()
                return replay

    cmd = ["openclaw", "agent", "--agent", agent_id, "--message", message, "--json"]
    if session_id:
        cmd += ["--session-id", session_id]
    if local:
        cmd.append("--local")
    if thinking:
        cmd += ["--thinking", thinking]
    if timeout_s > 0:
        cmd += ["--timeout", str(timeout_s)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        # An upstream cause (expired auth, rate limit, gateway error) is not
        # something this prompt can fix, so raise the type the retry/outage
        # logic understands. The full prompt is left out: it is identical on
        # every retry and buried the actual cause under a wall of text.
        if is_provider_stderr_failure(p.stderr):
            raise ProviderRejectionError(
                f"provider unavailable: {_first_cause(p.stderr)}"
            )
        raise PuzzleError(
            "OpenClaw call failed.\n"
            f"Command: {' '.join(cmd[:6])} ...\n\n"
            f"STDERR:\n{p.stderr[:2000]}"
        )

    if ledger is not None:
        ledger.record(p.stdout)
    if cache is not None:
        cache.set(key, p.stdout, prompt=message)

    reply = parse_openclaw_reply(p.stdout)
    # Surfaced as its own type so refill loops can back off instead of
    # re-sending an identical prompt that the provider just refused.
    if is_provider_rejection(reply):
        _dump_rejection(message, p.stdout, reply)
        raise ProviderRejectionError(reply.strip())
    return reply


def _dump_rejection(message: str, stdout: str, reply: str) -> None:
    """Persist the raw envelope behind a refusal.

    The reply text alone ("provider rejected the request schema or tool
    payload") names no cause, so without the surrounding JSON there is nothing
    to diagnose from after a failed build.
    """
    try:
        import time
        d = Path("puzzle_outputs") / "_rejections"
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (d / f"{stamp}-{abs(hash(message)) % 10**8}.txt").write_text(
            f"=== REPLY ===\n{reply}\n\n"
            f"=== PROMPT ({len(message)} chars) ===\n{message}\n\n"
            f"=== RAW STDOUT ===\n{stdout[:20000]}\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def extract_json_array(text: str) -> list[Any]:
    """Pull a JSON array out of a model reply that may be fenced or prefaced."""
    s = (text or "").strip()
    if not s:
        raise PuzzleError("Model returned an empty reply.")

    fenced = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fenced:
        s = fenced.group(1).strip()

    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        start = s.find("[")
        end = s.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise PuzzleError(f"Could not find a JSON array in reply: {s[:400]}")
        try:
            parsed = json.loads(s[start:end + 1])
        except json.JSONDecodeError as exc:
            raise PuzzleError(f"Malformed JSON in reply: {exc}") from exc

    if isinstance(parsed, dict):
        for key in ("riddles", "words", "phrases", "questions", "puzzles",
                    "chapters", "themes", "items", "data", "results"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        raise PuzzleError("Model returned an object with no recognizable array.")
    if not isinstance(parsed, list):
        raise PuzzleError("Model reply was not a JSON array.")
    return parsed


# --------------------------------------------------------------------------
# Prompts
#
# Each mirrors the reference chats cited in the spec, with the constraints that
# were discovered mid-conversation there (6 crossword words, 9 search words)
# stated up front.
# --------------------------------------------------------------------------

def _book_context(cfg: BookConfig) -> str:
    return (
        f"BOOK TITLE: {cfg.book_title}\n"
        f"BOOK TOPIC: {cfg.topic}\n"
        f"AUDIENCE: {cfg.audience}\n"
        f"DIFFICULTY: {cfg.difficulty}\n"
    )


_JSON_ONLY = (
    "Reply with a JSON array ONLY. No prose, no explanation, no markdown fence."
)


def build_picture_brief_prompt(cfg: BookConfig, count: int, subjects: list[str]) -> str:
    """Section 1 automation touchpoint: themed spot-the-difference scene ideas
    to brief the human illustrators."""
    subject_block = ""
    if subjects:
        subject_block = (
            "Use these scene subjects in order, one per puzzle:\n"
            + "\n".join(f"- {s}" for s in subjects[:count])
            + "\n"
        )
    return (
        f"{_book_context(cfg)}\n"
        f"Generate {count} spot-the-difference picture puzzle scene ideas for this book.\n"
        f"{subject_block}\n"
        "Each scene is drawn twice by a human illustrator, with small differences "
        "between the two versions for the reader to find.\n\n"
        "Requirements per scene:\n"
        "- The scene must fit the book topic and be recognizable to the audience.\n"
        "- It must be busy enough to hide 8-10 differences: multiple objects, "
        "characters, and background detail.\n"
        "- Describe it concretely enough that an illustrator can draw it without "
        "asking follow-up questions.\n"
        "- List 8 specific, concrete difference ideas (an object added, removed, "
        "recolored, resized, moved, or flipped). Name the exact object each time.\n"
        "- The art is grayscale line art, so never rely on color alone for a "
        "difference. If you mention shade, say light/dark pattern instead.\n\n"
        "Each array element must be an object with exactly these keys:\n"
        '  "scene_title": short name for the scene\n'
        '  "scene_description": 2-3 sentences an illustrator can draw from\n'
        '  "difference_ideas": array of 8 short strings\n\n'
        f"{_JSON_ONLY}"
    )


def build_riddle_prompt(cfg: BookConfig, count: int, avoid: list[str]) -> str:
    """Section 3 — themed riddles with answers."""
    avoid_block = ""
    if avoid:
        avoid_block = (
            "Do NOT reuse these answers, already used earlier in the book:\n"
            + ", ".join(sorted(set(avoid))[:80])
            + "\n\n"
        )
    return (
        f"{_book_context(cfg)}\n"
        f"Write {count} riddles for this book.\n\n"
        f"{avoid_block}"
        "Requirements:\n"
        "- Every riddle must tie to the book topic. The answer should be a person, "
        "object, place, or idea that belongs in this subject.\n"
        "- Written for the stated audience: plain everyday words, no obscure "
        "vocabulary, and solvable by a reader who knows the topic casually.\n"
        "- 2 to 4 lines each. First-person riddles ('I have...', 'I am...') are "
        "fine but must not be every single one — vary the form.\n"
        "- The answer must be one to three words and unambiguous. A reader who "
        "gets it should feel certain, not merely close.\n"
        "- Every answer in the batch must be different.\n"
        "- No riddle may give the answer away by naming it in the text.\n\n"
        "Each array element must be an object with exactly these keys:\n"
        '  "riddle": the riddle text (use \\n between lines)\n'
        '  "answer": the answer, 1-3 words\n\n'
        f"{_JSON_ONLY}"
    )


def build_wordsearch_prompt(cfg: BookConfig, title: str, grid_size: int) -> str:
    """Section 4 — exactly 9 words per puzzle, sized to fit the grid."""
    return (
        f"{_book_context(cfg)}\n"
        f"WORD SEARCH TOPIC: {title}\n\n"
        f"Give exactly {WORDS_PER_SEARCH} words for a word search puzzle on this topic.\n\n"
        "Requirements:\n"
        f"- EXACTLY {WORDS_PER_SEARCH} words. Not more, not fewer.\n"
        "- Single words only. No spaces, hyphens, apostrophes, digits, or accents.\n"
        f"- Each word is 3 to {grid_size} letters long so it fits the grid.\n"
        "- Letters A-Z only.\n"
        "- Every word must clearly belong to the topic and be understandable to "
        "the stated audience.\n"
        "- No duplicates, and no word may be contained inside another word in "
        "the list.\n"
        "- Prefer concrete nouns a reader can picture.\n\n"
        "Reply with a JSON array of exactly "
        f"{WORDS_PER_SEARCH} uppercase strings. No prose, no markdown fence."
    )


def build_cryptogram_prompt(cfg: BookConfig, count: int, avoid: list[str]) -> str:
    """Section 5 — themed phrases to encode as letter-substitution puzzles."""
    avoid_block = ""
    if avoid:
        avoid_block = (
            "Do NOT reuse these phrases, already used earlier in the book:\n"
            + "\n".join(f"- {p}" for p in avoid[:40])
            + "\n\n"
        )
    return (
        f"{_book_context(cfg)}\n"
        f"Write {count} short phrases to be encoded as letter-substitution "
        "cryptogram puzzles.\n\n"
        f"{avoid_block}"
        "Requirements:\n"
        "- Each phrase ties to the book topic: a saying, a tip, a fun fact, or a "
        "line of advice that fits the subject.\n"
        "- 4 to 9 words, and between 20 and 60 letters. Long enough that letter "
        "frequency helps the solver, short enough to fit one puzzle line.\n"
        "- Plain letters, spaces, and at most one comma or apostrophe. No other "
        "punctuation, no digits, no quotation marks.\n"
        "- Written for the stated audience: everyday words the reader knows.\n"
        "- Each phrase must make complete sense on its own.\n"
        "- Give a short hint that nudges the solver toward the topic without "
        "revealing any word in the phrase.\n"
        "- All phrases in the batch must be different from each other.\n\n"
        "Each array element must be an object with exactly these keys:\n"
        '  "phrase": the phrase to encode\n'
        '  "hint": a short hint\n\n'
        f"{_JSON_ONLY}"
    )


def build_trivia_theme_prompt(cfg: BookConfig, count: int) -> str:
    """Section 6 step 1 — brainstorm chapter themes."""
    return (
        f"{_book_context(cfg)}\n"
        f"Propose {count} chapter themes for the trivia section of this book.\n\n"
        "Requirements:\n"
        "- Each theme is a distinct slice of the book topic. Together they should "
        "cover the subject broadly with no overlap between them.\n"
        f"- Each theme must support {TRIVIA_QUESTIONS_PER_CHAPTER} varied "
        "multiple-choice questions without running thin.\n"
        "- Short, concrete titles the audience would find inviting. No colons, no "
        "subtitle clauses.\n"
        "- Order them so the easier, more familiar themes come first.\n\n"
        "Each array element must be an object with exactly these keys:\n"
        '  "chapter_title": the theme title\n'
        '  "chapter_scope": one sentence on what it covers\n\n'
        f"{_JSON_ONLY}"
    )


def build_trivia_question_prompt(
    cfg: BookConfig, chapter_title: str, scope: str, count: int, avoid: list[str]
) -> str:
    """Section 6 step 2 — multiple-choice questions for one chapter."""
    avoid_block = ""
    if avoid:
        avoid_block = (
            "Do NOT repeat these facts, already used earlier in the book:\n"
            + "\n".join(f"- {a}" for a in avoid[:60])
            + "\n\n"
        )
    return (
        f"{_book_context(cfg)}\n"
        f"TRIVIA CHAPTER: {chapter_title}\n"
        f"CHAPTER SCOPE: {scope or chapter_title}\n\n"
        f"Write {count} multiple-choice trivia questions for this chapter.\n\n"
        f"{avoid_block}"
        "Requirements:\n"
        "- Every question must be answerable by the stated audience and must be "
        "factually correct.\n"
        "- Exactly four options labelled A, B, C, D. Exactly one is right.\n"
        "- The three wrong options must be plausible and belong to the same "
        "category as the right one. No joke options, and none that are obviously "
        "impossible.\n"
        "- Options must be roughly the same length. Do not make the correct "
        "answer the longest or the most detailed one.\n"
        "- Spread the correct letter across A, B, C and D through the batch.\n"
        "- One fact per question. Do not ask two things at once.\n"
        "- Keep each question under 25 words.\n\n"
        "Each array element must be an object with exactly these keys:\n"
        '  "question": the question text\n'
        '  "choices": an object with keys "A", "B", "C", "D"\n'
        '  "correct_answer": one of "A", "B", "C", "D"\n\n'
        f"{_JSON_ONLY}"
    )


def build_crossword_prompt(cfg: BookConfig, title: str) -> str:
    """Section 7 — exactly 6 words plus clues, stated up front."""
    return (
        f"{_book_context(cfg)}\n"
        f"CROSSWORD TOPIC: {title}\n\n"
        f"Give exactly {WORDS_PER_CROSSWORD} words with clues for a crossword "
        "puzzle on this topic.\n\n"
        "Requirements:\n"
        f"- EXACTLY {WORDS_PER_CROSSWORD} words. Not more, not fewer.\n"
        "- Single words only. No spaces, hyphens, apostrophes, digits, or accents.\n"
        "- Each word is 4 to 11 letters, letters A-Z only.\n"
        "- The words must share letters with each other so they can interlock in "
        "a criss-cross grid. Favour common letters like A, E, R, S, T, N.\n"
        "- No duplicates, and no word may be contained inside another.\n"
        "- Each clue is one short sentence, under 12 words, written for the "
        "stated audience.\n"
        "- The clue must never contain the answer word or a form of it.\n\n"
        "Each array element must be an object with exactly these keys:\n"
        '  "word": the answer word, uppercase\n'
        '  "clue": the clue\n\n'
        f"{_JSON_ONLY}"
    )


# Longest a derived subject may be. A subject becomes a puzzle title and is
# pasted into later prompts, so an overlong one breaks both.
MAX_SUBJECT_CHARS = 60


def topic_keywords(topic: str) -> list[str]:
    """Split a topic into usable per-puzzle subjects.

    Operators routinely paste a long comma-separated keyword list into the
    topic field. Each entry is exactly the kind of short noun phrase the
    subject slot wants, so mine them before resorting to a numbered label.
    """
    pieces: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,;\n|/]+", topic or ""):
        piece = re.sub(r"\s+", " ", chunk).strip(" .-–—")
        # One or two stray words make a poor puzzle subject; so does an essay.
        if not piece or len(piece) > MAX_SUBJECT_CHARS or len(piece) < 3:
            continue
        key = piece.lower()
        if key in seen:
            continue
        seen.add(key)
        pieces.append(piece)
    return pieces


def short_topic(topic: str) -> str:
    """A topic trimmed to something usable as a title fragment."""
    s = re.sub(r"\s+", " ", topic or "").strip()
    if not s:
        return "Puzzle"
    first = topic_keywords(s)
    if first:
        return first[0]
    if len(s) > MAX_SUBJECT_CHARS:
        s = s[:MAX_SUBJECT_CHARS].rsplit(" ", 1)[0].strip(" .,-") or s[:MAX_SUBJECT_CHARS]
    return s


def build_section_subject_prompt(cfg: BookConfig, kind: str, count: int) -> str:
    """Invent per-puzzle subjects when the operator supplied none.

    The spec pulls these from the book outline; when no outline topics are
    given we derive them from the book topic instead.
    """
    label = SECTION_LABELS.get(kind, kind)
    return (
        f"{_book_context(cfg)}\n"
        f"Propose {count} subjects for the {label} section of this book.\n\n"
        "Requirements:\n"
        "- Each subject is a distinct slice of the book topic, with no overlap.\n"
        "- Each must have enough concrete vocabulary to build a puzzle from.\n"
        "- Short noun phrases of two or three words. No colons, no sentences.\n\n"
        "Reply with a JSON array of exactly "
        f"{count} strings. No prose, no markdown fence."
    )


# --------------------------------------------------------------------------
# Parsers — every one enforces the section's hard constraint
# --------------------------------------------------------------------------

def _as_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_string_list(raw: list[Any], count: int) -> list[str]:
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            for key in ("subject", "title", "topic", "name", "chapter_title"):
                if item.get(key):
                    item = item[key]
                    break
        text = _as_text(item)
        # A subject becomes a puzzle title and is pasted into later prompts,
        # so a model that answers with a sentence must not poison both.
        if len(text) > MAX_SUBJECT_CHARS:
            continue
        if text and text not in out:
            out.append(text)
    return out[:count]


def parse_picture_briefs(raw: list[Any], start_number: int) -> tuple[list[PictureBrief], list[str]]:
    briefs: list[PictureBrief] = []
    rejects: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            rejects.append("not an object")
            continue
        title = _as_text(item.get("scene_title"))
        desc = _as_text(item.get("scene_description"))
        if not title or not desc:
            rejects.append(f"missing title or description: {str(item)[:80]}")
            continue
        ideas_raw = item.get("difference_ideas") or []
        ideas = [_as_text(i) for i in ideas_raw if _as_text(i)] if isinstance(ideas_raw, list) else []
        if len(ideas) < 3:
            rejects.append(f"'{title}': fewer than 3 difference ideas")
            continue
        n = start_number + len(briefs)
        briefs.append(PictureBrief(
            id=f"pic{n:02d}", number=n, scene_title=title,
            scene_description=desc, difference_ideas=ideas[:10],
        ))
    return briefs, rejects


def parse_riddles(raw: list[Any], start_number: int) -> tuple[list[Riddle], list[str]]:
    riddles: list[Riddle] = []
    rejects: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            rejects.append("not an object")
            continue
        text = str(item.get("riddle") or "").strip()
        answer = _as_text(item.get("answer"))
        if not text or not answer:
            rejects.append(f"missing riddle or answer: {str(item)[:80]}")
            continue
        if len(answer.split()) > 4:
            rejects.append(f"answer too long: {answer!r}")
            continue
        # A riddle that names its own answer is unsolvable-as-intended.
        if gives_away_answer(answer, text):
            rejects.append(f"riddle gives away the answer: {answer!r}")
            continue
        n = start_number + len(riddles)
        riddles.append(Riddle(id=f"rid{n:02d}", number=n, riddle=text, answer=answer))
    return riddles, rejects


def parse_word_list(raw: list[Any], *, max_len: int, want: int) -> tuple[list[str], list[str]]:
    """Words for a search grid. Enforces the letters-only, length, and
    no-substring rules the renderer depends on."""
    words: list[str] = []
    rejects: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            for key in ("word", "text", "answer"):
                if item.get(key):
                    item = item[key]
                    break
        word = normalize_word(_as_text(item))
        if not word:
            rejects.append(f"not a usable word: {str(item)[:40]}")
            continue
        if len(word) < 3:
            rejects.append(f"{word}: shorter than 3 letters")
            continue
        if len(word) > max_len:
            rejects.append(f"{word}: longer than {max_len} letters")
            continue
        if word in words:
            rejects.append(f"{word}: duplicate")
            continue
        if any(word in w or w in word for w in words):
            rejects.append(f"{word}: contained in another word")
            continue
        words.append(word)
    return words[:want], rejects


def parse_cryptograms(raw: list[Any], start_number: int) -> tuple[list[Cryptogram], list[str]]:
    grams: list[Cryptogram] = []
    rejects: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            rejects.append("not an object")
            continue
        phrase = _as_text(item.get("phrase"))
        if not phrase:
            rejects.append(f"missing phrase: {str(item)[:80]}")
            continue
        letters = normalize_word(phrase)
        if len(letters) < 12:
            rejects.append(f"phrase too short to solve: {phrase!r}")
            continue
        if len(letters) > 90:
            rejects.append(f"phrase too long for one line: {phrase!r}")
            continue
        n = start_number + len(grams)
        grams.append(Cryptogram(
            id=f"cry{n:02d}", number=n, phrase=phrase,
            hint=_as_text(item.get("hint")),
        ))
    return grams, rejects


_LETTERS = ("A", "B", "C", "D")


def parse_trivia_questions(
    raw: list[Any], chapter: int, start_number: int
) -> tuple[list[TriviaQuestion], list[str]]:
    questions: list[TriviaQuestion] = []
    rejects: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            rejects.append("not an object")
            continue
        text = _as_text(item.get("question"))
        choices_raw = item.get("choices")
        if not text or not isinstance(choices_raw, dict):
            rejects.append(f"missing question or choices: {str(item)[:80]}")
            continue

        choices = {L: _as_text(choices_raw.get(L)) for L in _LETTERS}
        if any(not v for v in choices.values()):
            rejects.append(f"'{text[:40]}': not all four choices present")
            continue
        if len({v.lower() for v in choices.values()}) != 4:
            rejects.append(f"'{text[:40]}': duplicate choices")
            continue

        correct = _as_text(item.get("correct_answer")).upper()[:1]
        if correct not in _LETTERS:
            rejects.append(f"'{text[:40]}': correct_answer not A-D")
            continue

        n = start_number + len(questions)
        questions.append(TriviaQuestion(
            id=f"ch{chapter}_q{n:02d}", chapter=chapter, number=n,
            question=text, choices=choices, correct_answer=correct,
        ))
    return questions, rejects


def parse_crossword_words(raw: list[Any]) -> tuple[list[CrosswordEntry], list[str]]:
    entries: list[CrosswordEntry] = []
    rejects: list[str] = []
    seen: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            rejects.append("not an object")
            continue
        word = normalize_word(_as_text(item.get("word")))
        clue = _as_text(item.get("clue"))
        if not word or not clue:
            rejects.append(f"missing word or clue: {str(item)[:60]}")
            continue
        if len(word) < 3 or len(word) > 12:
            rejects.append(f"{word}: length out of range")
            continue
        if word in seen or any(word in w or w in word for w in seen):
            rejects.append(f"{word}: duplicate or contained in another word")
            continue
        # A clue containing its own answer is not a clue.
        if gives_away_answer(word, clue):
            rejects.append(f"{word}: clue contains the answer")
            continue
        seen.append(word)
        entries.append(CrosswordEntry(word=word, clue=clue))
    return entries, rejects


def rebalance_answer_distribution(questions: list[TriviaQuestion]) -> bool:
    """Spread correct answers across A-D.

    Models cluster hard on one letter. Rotating the correct option into a
    target slot keeps the answer key from being guessable.
    """
    if not questions:
        return False
    changed = False
    for i, q in enumerate(questions):
        target = _LETTERS[i % 4]
        if q.correct_answer == target:
            continue
        current = q.correct_answer
        q.choices[current], q.choices[target] = q.choices[target], q.choices[current]
        q.correct_answer = target
        changed = True
    return changed


def answer_distribution(questions: list[TriviaQuestion]) -> dict[str, int]:
    dist = {L: 0 for L in _LETTERS}
    for q in questions:
        if q.correct_answer in dist:
            dist[q.correct_answer] += 1
    return dist


def dedup_by_text(items: list[Any], *, threshold: float = NEAR_DUPLICATE_JACCARD) -> list[Any]:
    """Drop near-duplicate items, comparing each against those already kept."""
    kept: list[Any] = []
    for item in items:
        text = item.claim_text() if hasattr(item, "claim_text") else str(item)
        if any(jaccard(text, k.claim_text() if hasattr(k, "claim_text") else str(k)) >= threshold
               for k in kept):
            continue
        kept.append(item)
    return kept


# --------------------------------------------------------------------------
# Cryptogram cipher
# --------------------------------------------------------------------------

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def make_cipher(seed: int) -> dict[str, str]:
    """A random derangement of the alphabet — no letter maps to itself, which
    would leak a free answer to the solver."""
    import random

    rng = random.Random(seed)
    letters = list(_ALPHABET)
    for _ in range(200):
        shuffled = letters[:]
        rng.shuffle(shuffled)
        if all(a != b for a, b in zip(_ALPHABET, shuffled)):
            return dict(zip(_ALPHABET, shuffled))
    # Fallback: a rotation is always a derangement.
    shift = 1 + (seed % 25)
    return {c: _ALPHABET[(i + shift) % 26] for i, c in enumerate(_ALPHABET)}


def encode_phrase(phrase: str, cipher: dict[str, str]) -> str:
    return "".join(cipher.get(c.upper(), c) if c.isalpha() else c for c in phrase)


def apply_cryptogram_ciphers(grams: list[Cryptogram], seed_base: int = 7919) -> None:
    for i, gram in enumerate(grams):
        gram.cipher = make_cipher(seed_base + i * 31 + len(gram.phrase))
        gram.encoded = encode_phrase(gram.phrase, gram.cipher)
