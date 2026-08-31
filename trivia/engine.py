"""Trivia & Facts book generator — topic-agnostic content engine.

Royalty Media spec v1.0. Produces trivia/facts books for any subject: the
topic and chapter scheme are config inputs, never hardcoded. Nothing here
assumes chronology — chapters are named categories supplied by the operator.

Deliberately separate from the prose-book pipeline in openclaw_docx_writer.py:
that engine writes multi-hundred-word paragraphs under readability scoring,
while this one emits short-form structured objects that must survive a hard
no-overlap gate. Sharing code between them would couple two very different
validation models.

Text generation reuses the openclaw CLI, matching email_agent.py and
pub_listing_agent.py. Images reuse openclaw_image_maker.
"""

from __future__ import annotations

import json
import math
import random
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from openclaw_docx_writer import parse_openclaw_reply

DEFAULT_TIMEOUT = 600
# Each generator runs on its own agent so concurrent builds do not share a
# session store or a workspace: three books can build at once without their
# conversations, logs or caches interleaving. Operators can still override
# this per book from the "OpenClaw agent" field in the UI.
DEFAULT_AGENT = "trivia-agent-1"

# Batch sizes: the spec calls for 10-15 per request because asking for 50 at
# once measurably degrades question quality (models start recycling stems).
TRIVIA_BATCH = 12
FACT_BATCH = 20

# Section 8 requires answers spread across A-D. Models cluster hard on one
# letter, so we reshuffle when any letter's share exceeds this.
ANSWER_DISTRIBUTION_TOLERANCE = 0.40

# Jaccard token overlap above this marks two items as near-duplicates without
# needing a model call. Tuned to stand in for the spec's 0.85 cosine threshold.
NEAR_DUPLICATE_JACCARD = 0.62
# Pairs in this band are ambiguous — cheap heuristics can't call it, so they
# go to the LLM judge.
JUDGE_BAND_LOW = 0.34

ANSWER_KEY_END_OF_BOOK = "end_of_book"
ANSWER_KEY_END_OF_CHAPTER = "end_of_chapter"

_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "but",
    "is", "was", "were", "are", "be", "been", "which", "who", "whom", "whose",
    "what", "when", "where", "how", "why", "that", "this", "these", "those",
    "it", "its", "as", "by", "with", "from", "his", "her", "their", "did",
    "does", "do", "has", "have", "had", "first", "known", "became", "become",
}


class TriviaError(RuntimeError):
    """Generation or validation failure that should stop the build."""


class ValidationGateError(TriviaError):
    """A hard gate (Section 6 / Section 8) rejected the book for export."""


class ProviderRejectionError(TriviaError):
    """The upstream provider refused the request itself.

    Distinct from a content failure: retrying the identical prompt cannot fix
    it, so the refill loop must abort instead of burning its whole budget. Also
    never cached — see RawOutputCache.set.
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


def is_provider_rejection(reply: str) -> bool:
    """True when a reply is an upstream refusal rather than model output.

    Only meaningful for short replies — a legitimate trivia batch could quote
    one of these phrases inside a question, but never in a one-line answer.
    """
    s = (reply or "").strip().lower()
    if not s or len(s) > 600:
        return False
    return any(marker in s for marker in _PROVIDER_REJECTION_MARKERS)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class ChapterConfig:
    chapter_number: int
    chapter_title: str
    chapter_scope: str = ""
    trivia_count: int = 50
    fact_count: int = 100
    illustration_prompt_hint: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any], fallback_number: int) -> "ChapterConfig":
        try:
            number = int(d.get("chapter_number") or fallback_number)
        except (TypeError, ValueError):
            number = fallback_number
        title = str(d.get("chapter_title") or "").strip()
        if not title:
            raise TriviaError(f"Chapter {number} is missing chapter_title.")

        def _count(key: str, default: int) -> int:
            try:
                v = int(d.get(key, default))
            except (TypeError, ValueError):
                raise TriviaError(f"Chapter {number}: {key} must be a whole number.")
            if v < 0:
                raise TriviaError(f"Chapter {number}: {key} cannot be negative.")
            return v

        return ChapterConfig(
            chapter_number=number,
            chapter_title=title,
            chapter_scope=str(d.get("chapter_scope") or "").strip(),
            trivia_count=_count("trivia_count", 50),
            fact_count=_count("fact_count", 100),
            illustration_prompt_hint=str(d.get("illustration_prompt_hint") or "").strip(),
        )


@dataclass
class BookConfig:
    book_title: str
    topic: str
    audience: str = "general adult reader"
    difficulty: str = "medium"
    answer_key_position: str = ANSWER_KEY_END_OF_BOOK
    illustrations: bool = True
    editing_pass: bool = False          # Section 10: off for short-form content.
    chapters: list[ChapterConfig] = field(default_factory=list)

    # Runtime knobs (not part of the operator-facing spec schema).
    agent: str = DEFAULT_AGENT
    thinking: str = ""
    local: bool = False
    timeout_s: int = DEFAULT_TIMEOUT
    use_judge: bool = True
    image_model: str = "gpt-image-1"
    # Portrait: the page is 6x9, and 1024x1536 is the tallest gpt-image-1
    # offers. More real pixels before the print upscale has to make any up.
    image_size: str = "1024x1536"
    image_quality: str = "high"
    illustration_style_hint: str = ""
    openai_api_key: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "BookConfig":
        if not isinstance(d, dict):
            raise TriviaError("Config must be a JSON object.")
        title = str(d.get("book_title") or "").strip()
        topic = str(d.get("topic") or "").strip()
        if not title:
            raise TriviaError("book_title is required.")
        if not topic:
            raise TriviaError("topic is required.")

        raw_chapters = d.get("chapters") or []
        if not isinstance(raw_chapters, list) or not raw_chapters:
            raise TriviaError("At least one chapter is required.")
        chapters = [
            ChapterConfig.from_dict(c if isinstance(c, dict) else {}, i + 1)
            for i, c in enumerate(raw_chapters)
        ]

        seen: set[int] = set()
        for ch in chapters:
            if ch.chapter_number in seen:
                raise TriviaError(f"Duplicate chapter_number {ch.chapter_number}.")
            seen.add(ch.chapter_number)

        position = str(d.get("answer_key_position") or ANSWER_KEY_END_OF_BOOK).strip()
        if position not in {ANSWER_KEY_END_OF_BOOK, ANSWER_KEY_END_OF_CHAPTER}:
            raise TriviaError(
                "answer_key_position must be 'end_of_book' or 'end_of_chapter'."
            )

        def _flag(key: str, default: bool) -> bool:
            v = d.get(key, default)
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in {"1", "true", "yes", "on"}

        return BookConfig(
            book_title=title,
            topic=topic,
            audience=str(d.get("audience") or "general adult reader").strip(),
            difficulty=str(d.get("difficulty") or "medium").strip(),
            answer_key_position=position,
            illustrations=_flag("illustrations", True),
            editing_pass=_flag("editing_pass", False),
            chapters=chapters,
            agent=str(d.get("agent") or DEFAULT_AGENT).strip() or DEFAULT_AGENT,
            thinking=str(d.get("thinking") or "").strip(),
            local=_flag("local", False),
            timeout_s=int(d.get("timeout_s") or DEFAULT_TIMEOUT),
            use_judge=_flag("use_judge", True),
            image_model=str(d.get("image_model") or "gpt-image-1").strip(),
            image_size=str(d.get("image_size") or "1024x1536").strip(),
            image_quality=str(d.get("image_quality") or "high").strip(),
            illustration_style_hint=str(d.get("illustration_style_hint") or "").strip(),
            openai_api_key=str(d.get("openai_api_key") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("openai_api_key", None)
        return d


# --------------------------------------------------------------------------
# Content models
# --------------------------------------------------------------------------

@dataclass
class TriviaQuestion:
    id: str
    chapter: int
    question: str
    choices: dict[str, str]
    correct_answer: str
    fact_seed: str = ""

    def correct_text(self) -> str:
        return self.choices.get(self.correct_answer, "")

    def claim_text(self) -> str:
        """Question plus its correct answer — the actual factual claim, which
        is what must not be restated as a Did You Know bullet."""
        return f"{self.question} {self.correct_text()}".strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DidYouKnowFact:
    id: str
    chapter: int
    fact: str
    fact_seed: str = ""

    def claim_text(self) -> str:
        return self.fact

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Chapter:
    number: int
    title: str
    scope: str
    trivia: list[TriviaQuestion] = field(default_factory=list)
    facts: list[DidYouKnowFact] = field(default_factory=list)
    illustration_path: str = ""
    illustration_prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_number": self.number,
            "chapter_title": self.title,
            "chapter_scope": self.scope,
            "illustration_path": self.illustration_path,
            "illustration_prompt": self.illustration_prompt,
            "trivia": [q.to_dict() for q in self.trivia],
            "facts": [f.to_dict() for f in self.facts],
        }


@dataclass
class TriviaBook:
    config: BookConfig
    chapters: list[Chapter] = field(default_factory=list)
    # Authored front and back matter (Introduction / Conclusion prose).
    introduction: str = ""
    conclusion: str = ""
    warnings: list[str] = field(default_factory=list)
    # Token/cost totals for this build (Section 12).
    usage: dict[str, Any] = field(default_factory=dict)

    def answer_key(self) -> list[dict[str, Any]]:
        return [
            {
                "chapter_number": ch.number,
                "chapter_title": ch.title,
                "answers": [
                    {
                        "id": q.id,
                        "number": i + 1,
                        "correct_answer": q.correct_answer,
                        "correct_text": q.correct_text(),
                    }
                    for i, q in enumerate(ch.trivia)
                ],
            }
            for ch in self.chapters
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_title": self.config.book_title,
            "topic": self.config.topic,
            "audience": self.config.audience,
            "difficulty": self.config.difficulty,
            "answer_key_position": self.config.answer_key_position,
            "config": self.config.to_dict(),
            "introduction": self.introduction,
            "conclusion": self.conclusion,
            "chapters": [c.to_dict() for c in self.chapters],
            "answer_key": self.answer_key(),
            "warnings": list(self.warnings),
            "usage": dict(self.usage),
        }


# --------------------------------------------------------------------------
# Text normalization / similarity
# --------------------------------------------------------------------------

def slugify_seed(text: str, max_words: int = 6) -> str:
    """Short slug of a factual claim, used as the fact_seed exclusion key."""
    norm = unicodedata.normalize("NFKD", text or "")
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    words = re.findall(r"[a-z0-9]+", norm.lower())
    keep = [w for w in words if w not in _STOPWORDS] or words
    return "_".join(keep[:max_words])


def _tokens(text: str) -> set[str]:
    norm = unicodedata.normalize("NFKD", text or "")
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    words = re.findall(r"[a-z0-9]+", norm.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


# Containment on very short items is noisy: two unrelated one-line facts that
# happen to share a few common words score high simply because the denominator
# is tiny. Require a real overlap before containment is allowed to flag a pair.
MIN_CONTAINMENT_TOKENS = 5


def containment(a: str, b: str) -> float:
    """Overlap relative to the smaller item. Catches the case where a terse
    fact is fully contained in a longer question stem — Jaccard alone scores
    that low because the longer item dilutes the union."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    shared = len(ta & tb)
    if shared < MIN_CONTAINMENT_TOKENS:
        return 0.0
    return shared / min(len(ta), len(tb))


def similarity(a: str, b: str) -> float:
    return max(jaccard(a, b), containment(a, b) * 0.9)


# --------------------------------------------------------------------------
# Raw-output cache and usage ledger (spec Section 12)
#
# Every model reply is written to disk keyed by a hash of the exact prompt, so
# a re-run of the same build never pays twice for content we already have.
# Cost is priced from OpenClaw's own lastCallUsage block via the writer's
# proven parser rather than being re-derived here.
# --------------------------------------------------------------------------

@dataclass
class UsageLedger:
    """Per-book token/cost accounting, for unit-economics tracking."""
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def record(self, stdout: str) -> None:
        from openclaw_docx_writer import (
            _iter_json_objects,
            parse_openclaw_usage_cost,
        )

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
    """Stores raw model stdout keyed by prompt hash.

    Spec Section 12: never regenerate content we already paid for. Keyed on the
    full prompt, so a changed exclusion list correctly misses the cache — two
    different prompts must never share an entry.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
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
        the provider — so the build can never recover on its own.
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


# --------------------------------------------------------------------------
# LLM plumbing
# --------------------------------------------------------------------------

def call_openclaw_raw(
    agent_id: str,
    message: str,
    *,
    local: bool = False,
    thinking: str = "",
    timeout_s: int = DEFAULT_TIMEOUT,
    cache: Optional["RawOutputCache"] = None,
    ledger: Optional["UsageLedger"] = None,
    session_id: str = "",
    log: Optional[Callable[[str], None]] = None,
) -> str:
    """One openclaw agent call, same shape as email_agent._call_openclaw.

    When a cache is supplied, an identical prompt is served from disk instead
    of being re-billed (Section 12).

    Each batch is an independent one-shot request, so callers pass a per-build
    session_id. Without one the CLI files every call under the shared default
    session key, where hundreds of batches and their replies pile up into a
    single conversation until the provider refuses the request outright.
    """
    from openclaw_docx_writer import (
        OPENCLAW_BACKOFF_BASE_S,
        OPENCLAW_BACKOFF_CAP_S,
        OPENCLAW_MAX_ATTEMPTS,
        _is_retryable_openclaw_failure,
    )

    say = log or (lambda _m: None)
    key = RawOutputCache.key_for(agent_id, message) if cache is not None else ""
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            replay = parse_openclaw_reply(hit)
            # Caches written before set() screened rejections still hold
            # poisoned entries; drop them and pay for a real call instead.
            if is_provider_rejection(replay):
                say("  discarding cached provider rejection; re-requesting")
                cache.evict(key)
            else:
                if ledger is not None:
                    ledger.note_cache_hit()
                return replay

    cmd = ["openclaw", "agent", "--agent", agent_id, "--message", message, "--json"]
    if local:
        cmd.append("--local")
    if thinking:
        cmd += ["--thinking", thinking]
    if timeout_s > 0:
        cmd += ["--timeout", str(timeout_s)]
    if session_id:
        cmd += ["--session-id", session_id]

    for attempt in range(1, OPENCLAW_MAX_ATTEMPTS + 1):
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0:
            break

        retryable = _is_retryable_openclaw_failure(p.stdout, p.stderr)
        if not retryable or attempt == OPENCLAW_MAX_ATTEMPTS:
            reason = (
                f"still failing after {attempt} attempts"
                if retryable
                else "not a retryable error"
            )
            raise TriviaError(
                f"OpenClaw call failed ({reason}).\n"
                f"Command: {' '.join(cmd[:6])} ...\n\n"
                f"STDERR:\n{p.stderr[:2000]}"
            )

        # Full jitter, matching the prose writer: parallel builds hitting one
        # overloaded gateway must not retry in lockstep.
        delay = min(OPENCLAW_BACKOFF_CAP_S, OPENCLAW_BACKOFF_BASE_S * (2 ** (attempt - 1)))
        delay = random.uniform(0, delay)
        say(f"  AI service busy (attempt {attempt}/{OPENCLAW_MAX_ATTEMPTS}); retrying in {delay:.1f}s")
        time.sleep(delay)

    reply = parse_openclaw_reply(p.stdout)

    # Exit code 0 with a refusal in the payload. Real spend may still have been
    # incurred, so meter it, but never cache it and never let the caller retry
    # an identical prompt that cannot succeed.
    if is_provider_rejection(reply):
        if ledger is not None:
            ledger.record(p.stdout)
        raise ProviderRejectionError(reply.strip())

    if ledger is not None:
        ledger.record(p.stdout)
    if cache is not None:
        cache.set(key, p.stdout, prompt=message)
    return reply


def _extract_json_array(text: str) -> list[Any]:
    """Pull a JSON array out of a model reply that may be fenced or prefaced."""
    s = (text or "").strip()
    if not s:
        raise TriviaError("Model returned an empty reply.")

    fenced = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fenced:
        s = fenced.group(1).strip()

    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        start = s.find("[")
        end = s.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise TriviaError(f"Could not find a JSON array in reply: {s[:400]}")
        try:
            parsed = json.loads(s[start:end + 1])
        except json.JSONDecodeError as exc:
            raise TriviaError(f"Malformed JSON in reply: {exc}") from exc

    if isinstance(parsed, dict):
        for key in ("questions", "trivia", "facts", "items", "data"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        raise TriviaError("Model returned an object with no recognizable array.")
    if not isinstance(parsed, list):
        raise TriviaError("Model reply was not a JSON array.")
    return parsed


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

def _book_context(cfg: BookConfig, ch: ChapterConfig) -> str:
    scope = ch.chapter_scope or ch.chapter_title
    return (
        f"BOOK TOPIC: {cfg.topic}\n"
        f"BOOK TITLE: {cfg.book_title}\n"
        f"AUDIENCE: {cfg.audience}\n"
        f"DIFFICULTY: {cfg.difficulty}\n"
        f"CHAPTER {ch.chapter_number}: {ch.chapter_title}\n"
        f"CHAPTER SCOPE: {scope}\n"
    )


def build_trivia_prompt(
    cfg: BookConfig,
    ch: ChapterConfig,
    count: int,
    avoid: list[str],
) -> str:
    avoid_block = ""
    if avoid:
        shown = avoid[-220:]
        avoid_block = (
            "\nDO NOT ask about any of these subjects — they are already used "
            "in this book. Pick genuinely different facts:\n"
            + "\n".join(f"- {a}" for a in shown)
            + "\n"
        )

    return (
        f"{_book_context(cfg, ch)}"
        f"\nWrite {count} multiple-choice trivia questions strictly within the "
        f"chapter scope above.\n"
        "\nHARD REQUIREMENTS:\n"
        "1. Every question has exactly four choices, labeled A, B, C, D.\n"
        "2. Exactly one choice is correct and factually verifiable.\n"
        "3. The three wrong choices must be plausible and clearly wrong to an "
        "informed reader — same category and era as the right answer, never "
        "absurd filler, never arguably also correct.\n"
        "4. Spread correct answers evenly across A, B, C and D. Do not favour "
        "any one letter.\n"
        "5. Every question must be answerable from the question text alone. No "
        "'all of the above', no 'none of the above', no images referenced.\n"
        "6. Each question covers a DIFFERENT fact. No two questions may test "
        "the same underlying claim.\n"
        f"{avoid_block}"
        "\nReturn ONLY a JSON array, no prose, no markdown fence. Each element:\n"
        '{"question": "...", "choices": {"A": "...", "B": "...", "C": "...", '
        '"D": "..."}, "correct_answer": "B", '
        '"fact_seed": "short_snake_case_slug_of_the_core_fact"}\n'
    )


def build_facts_prompt(
    cfg: BookConfig,
    ch: ChapterConfig,
    count: int,
    exclusions: list[str],
) -> str:
    """Section 6 step 3: the trivia fact_seed list goes in as a hard exclusion."""
    exclusion_block = ""
    if exclusions:
        shown = exclusions[-260:]
        exclusion_block = (
            "\nCRITICAL EXCLUSION LIST — these facts are ALREADY used as trivia "
            "questions in this book. Writing any of them again, even reworded "
            "or from another angle, is a defect that fails the book:\n"
            + "\n".join(f"- {e}" for e in shown)
            + "\n"
        )

    return (
        f"{_book_context(cfg, ch)}"
        f"\nWrite {count} 'Did You Know' facts strictly within the chapter scope.\n"
        "\nHARD REQUIREMENTS:\n"
        "1. One self-contained sentence each. No lead-in like 'Did you know'.\n"
        "2. Specific and verifiable — names, places, numbers. Never vague "
        "commentary or opinion.\n"
        "3. Each fact is genuinely surprising or little-known to a casual fan.\n"
        "4. No two facts may restate the same underlying claim.\n"
        f"{exclusion_block}"
        "\nReturn ONLY a JSON array, no prose, no markdown fence. Each element:\n"
        '{"fact": "...", "fact_seed": "short_snake_case_slug_of_the_core_fact"}\n'
    )


FRONT_MATTER_MIN_WORDS = 300
FRONT_MATTER_MAX_WORDS = 500


def _chapter_roster(chapters: list[ChapterConfig]) -> str:
    return "\n".join(
        f"- Chapter {c.chapter_number}: {c.chapter_title}"
        + (f" — {c.chapter_scope}" if c.chapter_scope else "")
        for c in chapters
    )


def _front_matter_rules(cfg: BookConfig) -> str:
    """Shared rules for the two pieces of authored prose in the book.

    The trivia and facts prompts demand JSON; these two want flowing prose, so
    they have to say so explicitly or the agent answers in the book's house
    format out of habit.
    """
    return (
        f"\nHARD REQUIREMENTS:\n"
        f"1. Between {FRONT_MATTER_MIN_WORDS} and {FRONT_MATTER_MAX_WORDS} "
        f"words. This is the one place in the book that runs long, so do not "
        f"stop at a paragraph.\n"
        "2. Flowing prose in three to five paragraphs. No headings, no bullet "
        "lists, no numbered lists, no questions with lettered choices.\n"
        "3. Speak to the reader as an author writing a real book. Never "
        "mention AI, generation, prompts, models, or that this is a "
        "collection assembled from anything.\n"
        f"4. Stay concrete about {cfg.topic}. Name real specifics from the "
        "subject rather than writing generic filler that would fit any book.\n"
        "5. Do not repeat any trivia question or state any answer.\n"
        "\nReturn ONLY the prose itself. No title, no heading, no preamble, "
        "no markdown fence, no commentary about the task.\n"
    )


def build_introduction_prompt(cfg: BookConfig, chapters: list[ChapterConfig]) -> str:
    """The Introduction a reader meets before Chapter 1."""
    key_note = (
        "Answers are collected in the answer key at the back of the book."
        if cfg.answer_key_position == ANSWER_KEY_END_OF_BOOK
        else "Answers wait at the end of each chapter."
    )
    return (
        f"BOOK TITLE: {cfg.book_title}\n"
        f"BOOK TOPIC: {cfg.topic}\n"
        f"AUDIENCE: {cfg.audience}\n"
        f"DIFFICULTY: {cfg.difficulty}\n"
        f"CHAPTERS:\n{_chapter_roster(chapters)}\n"
        f"\nWrite the Introduction for this trivia book.\n"
        "\nCover, in your own order and phrasing: why this subject rewards a "
        "curious reader, what makes the questions here worth sitting with, how "
        "the book is arranged, and how someone should use it — alone, or "
        "reading aloud with other people. "
        f"{key_note}\n"
        "\nOpen with something specific and surprising about the subject, not "
        "with a definition and not with the book's own title. Earn the "
        "reader's attention in the first sentence.\n"
        f"{_front_matter_rules(cfg)}"
    )


def build_conclusion_prompt(cfg: BookConfig, chapters: list[ChapterConfig]) -> str:
    """The closing note after the last chapter."""
    return (
        f"BOOK TITLE: {cfg.book_title}\n"
        f"BOOK TOPIC: {cfg.topic}\n"
        f"AUDIENCE: {cfg.audience}\n"
        f"CHAPTERS:\n{_chapter_roster(chapters)}\n"
        f"\nWrite the Conclusion for this trivia book. The reader has just "
        "finished every chapter.\n"
        "\nSend them off well: reflect on what the whole subject looks like "
        "once these pieces sit together, point to where a curious reader can "
        "keep going on their own, and close warmly without gushing.\n"
        "\nDo not summarize the chapters one by one, and do not congratulate "
        "the reader on finishing. Write the last page of a book someone chose "
        "to read, not a wrap-up of a task they completed.\n"
        f"{_front_matter_rules(cfg)}"
    )


def clean_prose_reply(text: str) -> str:
    """Strip the wrappers a model adds around prose it was told not to wrap.

    Fences and a restated "Introduction" heading are the two that survive the
    instruction most often, and both would print verbatim in the DOCX.
    """
    s = (text or "").strip()

    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()

    s = re.sub(
        r"^#{1,6}\s*(introduction|conclusion)\s*:?\s*\n+",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"^\*{0,2}(introduction|conclusion)\*{0,2}\s*:?\s*\n+",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return s.strip()


def split_paragraphs(text: str) -> list[str]:
    """Blank-line paragraphs, falling back to single newlines.

    Models return prose both ways, and a book page needs the breaks either way.
    """
    s = clean_prose_reply(text)
    if not s:
        return []
    parts = [p.strip() for p in re.split(r"\n\s*\n", s) if p.strip()]
    if len(parts) == 1:
        parts = [p.strip() for p in parts[0].split("\n") if p.strip()]
    return parts


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def build_judge_prompt(pairs: list[tuple[str, str]]) -> str:
    listing = "\n".join(
        f"{i + 1}. ITEM A: {a}\n   ITEM B: {b}" for i, (a, b) in enumerate(pairs)
    )
    return (
        "You are checking a trivia book for repeated content. For each numbered "
        "pair, decide whether the two items convey the SAME core factual claim "
        "such that a reader would notice the repetition.\n\n"
        "Answer true only if the underlying fact is the same. Two items about "
        "the same person or subject but different facts are NOT duplicates.\n\n"
        f"{listing}\n\n"
        "Return ONLY a JSON array of objects, one per pair, in order:\n"
        '[{"n": 1, "duplicate": true}, {"n": 2, "duplicate": false}]\n'
    )


# --------------------------------------------------------------------------
# Parsing / validation of generated items
# --------------------------------------------------------------------------

_LETTERS = ("A", "B", "C", "D")


def _clean_choice(value: Any) -> str:
    text = str(value or "").strip()
    # Models sometimes echo the label back inside the choice text.
    return re.sub(r"^[A-D][)\.:\-]\s*", "", text).strip()


def parse_trivia_items(raw: list[Any], chapter: int) -> tuple[list[TriviaQuestion], list[str]]:
    """Turn raw model output into questions, dropping ones that fail Section 8
    structural rules. Returns (valid, rejection_reasons)."""
    out: list[TriviaQuestion] = []
    rejects: list[str] = []

    for item in raw:
        if not isinstance(item, dict):
            rejects.append("non-object entry")
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            rejects.append("missing question text")
            continue

        raw_choices = item.get("choices")
        choices: dict[str, str] = {}
        if isinstance(raw_choices, dict):
            for letter in _LETTERS:
                val = _clean_choice(raw_choices.get(letter) or raw_choices.get(letter.lower()))
                if val:
                    choices[letter] = val
        elif isinstance(raw_choices, list) and len(raw_choices) == 4:
            for letter, val in zip(_LETTERS, raw_choices):
                cleaned = _clean_choice(val)
                if cleaned:
                    choices[letter] = cleaned

        if len(choices) != 4:
            rejects.append(f"needs exactly 4 choices: {question[:60]}")
            continue
        if len({c.lower() for c in choices.values()}) != 4:
            rejects.append(f"duplicate choice text: {question[:60]}")
            continue

        correct = str(item.get("correct_answer") or "").strip().upper()[:1]
        if correct not in _LETTERS:
            rejects.append(f"correct_answer not A-D: {question[:60]}")
            continue

        lowered = question.lower()
        if "all of the above" in lowered or "none of the above" in lowered:
            rejects.append(f"banned stem: {question[:60]}")
            continue
        if any("all of the above" in c.lower() or "none of the above" in c.lower()
               for c in choices.values()):
            rejects.append(f"banned choice: {question[:60]}")
            continue

        seed = str(item.get("fact_seed") or "").strip()
        if not seed:
            seed = slugify_seed(f"{question} {choices[correct]}")

        out.append(TriviaQuestion(
            id="",
            chapter=chapter,
            question=question,
            choices=choices,
            correct_answer=correct,
            fact_seed=seed,
        ))

    return out, rejects


def parse_fact_items(raw: list[Any], chapter: int) -> tuple[list[DidYouKnowFact], list[str]]:
    out: list[DidYouKnowFact] = []
    rejects: list[str] = []

    for item in raw:
        text = ""
        if isinstance(item, dict):
            text = str(item.get("fact") or item.get("text") or "").strip()
            seed = str(item.get("fact_seed") or "").strip()
        elif isinstance(item, str):
            text = item.strip()
            seed = ""
        else:
            rejects.append("non-object entry")
            continue

        if not text:
            rejects.append("empty fact")
            continue
        text = re.sub(r"^(did you know[,:]?\s*)", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"^[-*•]\s*", "", text).strip()
        if len(text) < 15:
            rejects.append(f"too short: {text[:40]}")
            continue

        out.append(DidYouKnowFact(
            id="",
            chapter=chapter,
            fact=text,
            fact_seed=seed or slugify_seed(text),
        ))

    return out, rejects


def rebalance_answer_distribution(questions: list[TriviaQuestion]) -> bool:
    """Section 8: reshuffle so no letter dominates. Rewrites choice mappings
    rather than regenerating content — the question text is unaffected because
    the choices move with their labels.

    Returns True if anything was changed."""
    if len(questions) < 4:
        return False

    target = len(questions) / 4.0
    counts = {letter: 0 for letter in _LETTERS}
    for q in questions:
        counts[q.correct_answer] += 1

    worst = max(counts.values()) / len(questions)
    if worst <= ANSWER_DISTRIBUTION_TOLERANCE:
        return False

    # Walk the list assigning each question the letter that is currently
    # furthest below quota, then swap the correct choice into that slot.
    assigned = {letter: 0 for letter in _LETTERS}
    changed = False
    for idx, q in enumerate(questions):
        wanted = min(_LETTERS, key=lambda L: (assigned[L], _LETTERS.index(L)))
        if assigned[wanted] >= math.ceil(target):
            wanted = q.correct_answer
        if wanted != q.correct_answer:
            correct_text = q.choices[q.correct_answer]
            displaced = q.choices[wanted]
            q.choices[wanted] = correct_text
            q.choices[q.correct_answer] = displaced
            q.correct_answer = wanted
            changed = True
        assigned[q.correct_answer] += 1

    return changed


def answer_distribution(questions: list[TriviaQuestion]) -> dict[str, int]:
    counts = {letter: 0 for letter in _LETTERS}
    for q in questions:
        if q.correct_answer in counts:
            counts[q.correct_answer] += 1
    return counts


# --------------------------------------------------------------------------
# Dedup gate (Section 6)
# --------------------------------------------------------------------------

@dataclass
class Collision:
    kind: str          # "fact_vs_trivia" | "fact_vs_fact" | "trivia_vs_trivia"
    left_id: str
    right_id: str
    score: float
    method: str        # "heuristic" | "judge"
    left_text: str = ""
    right_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DedupChecker:
    """Two-stage collision detection.

    Stage 1 is deterministic and free: seed equality plus token-overlap
    similarity. Stage 2 sends only the ambiguous middle band to an LLM judge,
    which is where paraphrases that share little vocabulary get caught.
    """

    def __init__(
        self,
        cfg: BookConfig,
        log: Optional[Callable[[str], None]] = None,
        cache: Optional["RawOutputCache"] = None,
        ledger: Optional["UsageLedger"] = None,
        session_id: str = "",
    ) -> None:
        self.cfg = cfg
        self.log = log or (lambda _m: None)
        self.cache = cache
        self.ledger = ledger
        self.session_id = session_id

    def _judge(self, pairs: list[tuple[str, str]]) -> list[bool]:
        if not pairs:
            return []
        verdicts = [False] * len(pairs)
        for start in range(0, len(pairs), 25):
            chunk = pairs[start:start + 25]
            try:
                reply = call_openclaw_raw(
                    self.cfg.agent,
                    build_judge_prompt(chunk),
                    local=self.cfg.local,
                    thinking=self.cfg.thinking,
                    timeout_s=self.cfg.timeout_s,
                    cache=self.cache,
                    ledger=self.ledger,
                    session_id=self.session_id,
                    log=self.log,
                )
                parsed = _extract_json_array(reply)
            except ProviderRejectionError:
                # Not a judgement failure — the provider is refusing calls
                # outright. Marking the chunk as duplicates would silently
                # delete good content, so surface it to the build instead.
                raise
            except TriviaError as exc:
                # A judge failure must not silently pass content through the
                # gate — treat the whole chunk as colliding so it regenerates.
                self.log(f"  judge call failed, treating chunk as collisions: {exc}")
                for i in range(len(chunk)):
                    verdicts[start + i] = True
                continue

            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                try:
                    n = int(entry.get("n", 0))
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= len(chunk):
                    verdicts[start + n - 1] = bool(entry.get("duplicate"))
        return verdicts

    def find_collisions(
        self,
        candidates: list[Any],
        existing: list[Any],
        kind: str,
    ) -> set[str]:
        """Return ids of candidates that collide with `existing` (or, when
        `existing` is the same list, with an earlier candidate)."""
        colliding: set[str] = set()
        self.collisions: list[Collision] = getattr(self, "collisions", [])

        existing_seeds: dict[str, Any] = {}
        for item in existing:
            if item.fact_seed:
                existing_seeds.setdefault(item.fact_seed, item)

        judge_queue: list[tuple[Any, Any, float]] = []

        for cand in candidates:
            hit = False

            twin = existing_seeds.get(cand.fact_seed) if cand.fact_seed else None
            if twin is not None and twin.id != cand.id:
                self.collisions.append(Collision(
                    kind=kind, left_id=cand.id, right_id=twin.id,
                    score=1.0, method="heuristic",
                    left_text=cand.claim_text(), right_text=twin.claim_text(),
                ))
                colliding.add(cand.id)
                continue

            best_score = 0.0
            best_item = None
            for item in existing:
                if item.id == cand.id:
                    continue
                score = similarity(cand.claim_text(), item.claim_text())
                if score > best_score:
                    best_score, best_item = score, item

            if best_item is None:
                continue

            if best_score >= NEAR_DUPLICATE_JACCARD:
                self.collisions.append(Collision(
                    kind=kind, left_id=cand.id, right_id=best_item.id,
                    score=round(best_score, 3), method="heuristic",
                    left_text=cand.claim_text(), right_text=best_item.claim_text(),
                ))
                colliding.add(cand.id)
                hit = True
            elif best_score >= JUDGE_BAND_LOW and self.cfg.use_judge:
                judge_queue.append((cand, best_item, best_score))

            if hit:
                continue

        if judge_queue:
            self.log(f"  judging {len(judge_queue)} borderline pair(s)")
            verdicts = self._judge(
                [(c.claim_text(), o.claim_text()) for c, o, _ in judge_queue]
            )
            for (cand, other, score), is_dup in zip(judge_queue, verdicts):
                if is_dup:
                    self.collisions.append(Collision(
                        kind=kind, left_id=cand.id, right_id=other.id,
                        score=round(score, 3), method="judge",
                        left_text=cand.claim_text(), right_text=other.claim_text(),
                    ))
                    colliding.add(cand.id)

        return colliding


def dedup_within(items: list[Any]) -> list[Any]:
    """Drop later items that repeat an earlier one, by seed or high overlap.
    Deterministic only — used as a fast pre-filter before the full gate."""
    kept: list[Any] = []
    seen_seeds: set[str] = set()
    for item in items:
        if item.fact_seed and item.fact_seed in seen_seeds:
            continue
        if any(similarity(item.claim_text(), k.claim_text()) >= NEAR_DUPLICATE_JACCARD
               for k in kept):
            continue
        if item.fact_seed:
            seen_seeds.add(item.fact_seed)
        kept.append(item)
    return kept
