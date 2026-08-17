"""Researched-stories book generator — topic-agnostic content engine.

Produces books of short, real, researchable stories: dumb criminals, famous
cats, record fishing catches. The topic and outline are config inputs, never
hardcoded.

The unit of work is a Story: a title plus a free-form `context` box. The
context is whatever the operator could gather — a full who/year/where/sources
breakdown, or a single sentence. The prompt adapts to how much is present, so
a thin context produces a story that leans on the model's own research while a
rich one is treated as authoritative ground truth.

Deliberately separate from the trivia engine: that one emits short structured
objects policed by a no-overlap gate, while this one emits 300-500 word prose
policed by length, tone and repetition checks. Sharing code between them would
couple two very different validation models.

Text generation reuses the openclaw CLI, matching trivia/engine.py and
email_agent.py. Images reuse openclaw_image_maker.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Optional

from openclaw_docx_writer import parse_openclaw_reply

DEFAULT_TIMEOUT = 600
DEFAULT_AGENT = "main"

# The spec's target from the video: "a 300 to 500 word little story".
DEFAULT_MIN_WORDS = 300
DEFAULT_MAX_WORDS = 500

# Hard floor/ceiling regardless of what the operator configures. Below the
# floor there is no story; above the ceiling it stops being a micro-story and
# the book's page budget blows out.
ABSOLUTE_MIN_WORDS = 120
ABSOLUTE_MAX_WORDS = 1500

# How much slack the length gate allows around the configured band before a
# story is sent back for a rewrite. Models routinely land a little outside a
# stated range, and rejecting a 290-word story from a 300-500 band would burn
# calls for no editorial gain.
LENGTH_TOLERANCE = 0.12

# Attempts per story before the build gives up on it. Each attempt is a fresh
# call, with the previous failure's reason fed back in.
MAX_STORY_ATTEMPTS = 4

# Stories that reuse the same opening formula read as machine-written, which is
# the single most visible defect in a 100-story book. Openers are compared on
# their first few words.
OPENER_WORDS = 6
MAX_OPENER_REPEATS = 2

# Jaccard token overlap above which two stories are considered to be retelling
# the same event.
#
# Measured on real prose rather than guessed: two stories about genuinely
# different events score near 0.0 even when they share a topic and vocabulary
# ("bank", "teller", "police"), because the names, places and specifics differ.
# A true retelling of the same incident in different words lands around 0.54.
# The gap between those two populations is enormous, so the threshold sits well
# below the retelling score to catch paraphrases, and still far above what
# unrelated stories ever reach.
NEAR_DUPLICATE_JACCARD = 0.45

_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "but",
    "is", "was", "were", "are", "be", "been", "which", "who", "whom", "whose",
    "what", "when", "where", "how", "why", "that", "this", "these", "those",
    "it", "its", "as", "by", "with", "from", "his", "her", "their", "did",
    "does", "do", "has", "have", "had", "he", "she", "they", "them", "him",
    "into", "out", "up", "down", "over", "after", "before", "then", "than",
    "so", "not", "no", "his", "hers", "there", "here", "would", "could",
}


class StoryError(RuntimeError):
    """Generation or validation failure that should stop the build."""


class ValidationGateError(StoryError):
    """A hard gate rejected the book for export."""


class ProviderRejectionError(StoryError):
    """The upstream provider refused the request itself.

    Distinct from a content failure: retrying the identical prompt cannot fix
    it, so the retry loop must abort instead of burning its whole budget. Also
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

    Only meaningful for short replies — a legitimate story could quote one of
    these phrases, but never in a one-line answer.
    """
    s = (reply or "").strip().lower()
    if not s or len(s) > 600:
        return False
    return any(marker in s for marker in _PROVIDER_REJECTION_MARKERS)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class StoryConfig:
    """One outline entry: a sub-chapter title plus its context box.

    Only `title` is required. Everything else is optional because the operator
    frequently cannot supply a year, a location, or sources — the whole point
    of the free-form context box.
    """

    number: int
    title: str
    context: str = ""
    # Optional structured hints. Present in a rich outline like the Dumbest
    # Criminals one, absent in a thin outline like "the tuna caught in Florida".
    # They are folded into the prompt when set and silently skipped when not.
    who: str = ""
    year: str = ""
    where: str = ""
    sources: str = ""
    illustration_prompt_hint: str = ""
    # Per-story overrides of the book's word band, for a headline story that
    # deserves more room than the rest.
    min_words: int = 0
    max_words: int = 0

    def context_block(self) -> str:
        """Everything the operator told us about this story, as prompt text.

        Structured hints are emitted only when non-empty, so a thin outline
        never produces "Year: (unknown)" lines that invite the model to
        hallucinate a filler value.
        """
        parts: list[str] = []
        if self.who:
            parts.append(f"WHO: {self.who}")
        if self.year:
            parts.append(f"YEAR: {self.year}")
        if self.where:
            parts.append(f"WHERE: {self.where}")
        if self.context:
            parts.append(f"CONTEXT: {self.context}")
        if self.sources:
            parts.append(f"RESEARCH SOURCES: {self.sources}")
        return "\n".join(parts)

    def has_context(self) -> bool:
        return bool(self.context_block().strip())

    def word_band(self, cfg: "BookConfig") -> tuple[int, int]:
        lo = self.min_words or cfg.min_words
        hi = self.max_words or cfg.max_words
        return lo, hi

    @staticmethod
    def from_dict(d: dict[str, Any], fallback_number: int) -> "StoryConfig":
        try:
            number = int(d.get("number") or d.get("story_number") or fallback_number)
        except (TypeError, ValueError):
            number = fallback_number

        title = str(d.get("title") or d.get("story_title") or "").strip()
        if not title:
            raise StoryError(f"Story {number} is missing a title.")

        def _words(key: str) -> int:
            raw = d.get(key)
            if raw in (None, "", 0, "0"):
                return 0
            try:
                v = int(raw)
            except (TypeError, ValueError):
                raise StoryError(f"Story {number}: {key} must be a whole number.")
            if v < 0:
                raise StoryError(f"Story {number}: {key} cannot be negative.")
            return v

        return StoryConfig(
            number=number,
            title=title,
            context=str(d.get("context") or "").strip(),
            who=str(d.get("who") or "").strip(),
            year=str(d.get("year") or "").strip(),
            where=str(d.get("where") or "").strip(),
            sources=str(d.get("sources") or d.get("research_sources") or "").strip(),
            illustration_prompt_hint=str(d.get("illustration_prompt_hint") or "").strip(),
            min_words=_words("min_words"),
            max_words=_words("max_words"),
        )


@dataclass
class ChapterConfig:
    """A group of stories.

    Books can be flat (100 stories, no chapters) or grouped ("Bank Jobs Gone
    Wrong", "Burglary Blunders"). A flat book is modelled as a single chapter
    with an empty title, which the exporters omit from the output.
    """

    chapter_number: int
    chapter_title: str = ""
    chapter_intro: str = ""
    stories: list[StoryConfig] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict[str, Any], fallback_number: int, start_at: int) -> "ChapterConfig":
        try:
            number = int(d.get("chapter_number") or fallback_number)
        except (TypeError, ValueError):
            number = fallback_number

        raw_stories = d.get("stories") or []
        if not isinstance(raw_stories, list):
            raise StoryError(f"Chapter {number}: 'stories' must be a list.")

        stories = [
            StoryConfig.from_dict(s if isinstance(s, dict) else {"title": str(s)},
                                  start_at + i)
            for i, s in enumerate(raw_stories)
        ]
        return ChapterConfig(
            chapter_number=number,
            chapter_title=str(d.get("chapter_title") or "").strip(),
            chapter_intro=str(d.get("chapter_intro") or "").strip(),
            stories=stories,
        )


@dataclass
class BookConfig:
    book_title: str
    topic: str
    audience: str = "general adult reader"
    # Free-form, because "comedic true-crime, family-friendly" and "warm,
    # heartfelt" are both valid and neither belongs in an enum.
    tone: str = "engaging, warm, and lightly humorous"
    min_words: int = DEFAULT_MIN_WORDS
    max_words: int = DEFAULT_MAX_WORDS

    # Optional per-story extras, matching the Dumbest Criminals outline's
    # "Dumb Move Breakdown" sidebar and "Lesson Not Learned" closer. Generic
    # names with configurable labels so any book can use them.
    sidebar_enabled: bool = False
    sidebar_label: str = "The Breakdown"
    sidebar_instruction: str = ""
    closer_enabled: bool = False
    closer_label: str = "The Takeaway"
    closer_instruction: str = ""

    # Global instructions appended to every story prompt — the "add to the
    # prompt" box from the video, applied book-wide.
    global_context: str = ""

    illustrations: bool = False
    illustrate_every_story: bool = False   # otherwise one image per chapter
    chapters: list[ChapterConfig] = field(default_factory=list)

    # Runtime knobs (not part of the operator-facing schema).
    agent: str = DEFAULT_AGENT
    thinking: str = ""
    local: bool = False
    timeout_s: int = DEFAULT_TIMEOUT
    check_duplicates: bool = True
    image_model: str = "gpt-image-1"
    image_size: str = "1024x1024"
    image_quality: str = "high"
    illustration_style_hint: str = ""
    openai_api_key: str = ""

    def all_stories(self) -> list[StoryConfig]:
        return [s for ch in self.chapters for s in ch.stories]

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "BookConfig":
        if not isinstance(d, dict):
            raise StoryError("Config must be a JSON object.")

        title = str(d.get("book_title") or "").strip()
        topic = str(d.get("topic") or "").strip()
        if not title:
            raise StoryError("book_title is required.")
        if not topic:
            raise StoryError("topic is required.")

        def _flag(key: str, default: bool) -> bool:
            v = d.get(key, default)
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in {"1", "true", "yes", "on"}

        def _int(key: str, default: int) -> int:
            raw = d.get(key, default)
            if raw in (None, ""):
                return default
            try:
                return int(raw)
            except (TypeError, ValueError):
                raise StoryError(f"{key} must be a whole number.")

        min_words = _int("min_words", DEFAULT_MIN_WORDS)
        max_words = _int("max_words", DEFAULT_MAX_WORDS)
        if min_words < ABSOLUTE_MIN_WORDS:
            raise StoryError(f"min_words cannot be below {ABSOLUTE_MIN_WORDS}.")
        if max_words > ABSOLUTE_MAX_WORDS:
            raise StoryError(f"max_words cannot exceed {ABSOLUTE_MAX_WORDS}.")
        if min_words > max_words:
            raise StoryError("min_words cannot be greater than max_words.")

        # Two accepted outline shapes. A flat `stories` list is the common case
        # (the video's "just a list of sub-chapters"); `chapters` is for books
        # that group them. Supporting both here keeps the UI free to send
        # whichever it built.
        raw_chapters = d.get("chapters")
        raw_stories = d.get("stories")
        chapters: list[ChapterConfig] = []

        if isinstance(raw_chapters, list) and raw_chapters:
            counter = 1
            for i, c in enumerate(raw_chapters):
                ch = ChapterConfig.from_dict(
                    c if isinstance(c, dict) else {}, i + 1, counter
                )
                counter += len(ch.stories)
                chapters.append(ch)
        elif isinstance(raw_stories, list) and raw_stories:
            chapters = [ChapterConfig.from_dict({"stories": raw_stories}, 1, 1)]
        else:
            raise StoryError("At least one story is required.")

        if not any(ch.stories for ch in chapters):
            raise StoryError("At least one story is required.")

        # Story numbers are the reader-facing sequence and the id key, so a
        # duplicate would make two entries indistinguishable in the editor.
        seen: set[int] = set()
        for ch in chapters:
            for s in ch.stories:
                if s.number in seen:
                    raise StoryError(f"Duplicate story number {s.number}.")
                seen.add(s.number)

        return BookConfig(
            book_title=title,
            topic=topic,
            audience=str(d.get("audience") or "general adult reader").strip(),
            tone=str(d.get("tone") or "engaging, warm, and lightly humorous").strip(),
            min_words=min_words,
            max_words=max_words,
            sidebar_enabled=_flag("sidebar_enabled", False),
            sidebar_label=str(d.get("sidebar_label") or "The Breakdown").strip(),
            sidebar_instruction=str(d.get("sidebar_instruction") or "").strip(),
            closer_enabled=_flag("closer_enabled", False),
            closer_label=str(d.get("closer_label") or "The Takeaway").strip(),
            closer_instruction=str(d.get("closer_instruction") or "").strip(),
            global_context=str(d.get("global_context") or "").strip(),
            illustrations=_flag("illustrations", False),
            illustrate_every_story=_flag("illustrate_every_story", False),
            chapters=chapters,
            agent=str(d.get("agent") or DEFAULT_AGENT).strip() or DEFAULT_AGENT,
            thinking=str(d.get("thinking") or "").strip(),
            local=_flag("local", False),
            timeout_s=_int("timeout_s", DEFAULT_TIMEOUT),
            check_duplicates=_flag("check_duplicates", True),
            image_model=str(d.get("image_model") or "gpt-image-1").strip(),
            image_size=str(d.get("image_size") or "1024x1024").strip(),
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
class Story:
    """One written story, plus the provenance needed to fact-check it later."""

    id: str
    number: int
    chapter: int
    title: str
    body: str = ""
    sidebar: str = ""
    closer: str = ""
    # Echoed back from the config so the editor and the JSON stay self-contained
    # — a reviewer checking a claim needs the source context beside the prose.
    context: str = ""
    who: str = ""
    year: str = ""
    where: str = ""
    sources: str = ""
    # Sources the model itself cites. Kept apart from operator-supplied
    # `sources` because model-cited references are unverified by definition and
    # a fact-checker must know which is which.
    cited_sources: list[str] = field(default_factory=list)
    # Claims the model flagged as uncertain. Surfaced in the editor so a human
    # can check them before publication.
    uncertain_claims: list[str] = field(default_factory=list)
    illustration_path: str = ""
    illustration_prompt: str = ""
    word_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def claim_text(self) -> str:
        """What the duplicate checker compares. Title plus body: two stories
        about the same event share both."""
        return f"{self.title} {self.body}".strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Chapter:
    number: int
    title: str = ""
    intro: str = ""
    stories: list[Story] = field(default_factory=list)
    illustration_path: str = ""
    illustration_prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_number": self.number,
            "chapter_title": self.title,
            "chapter_intro": self.intro,
            "illustration_path": self.illustration_path,
            "illustration_prompt": self.illustration_prompt,
            "stories": [s.to_dict() for s in self.stories],
        }


@dataclass
class StoryBook:
    config: BookConfig
    chapters: list[Chapter] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def all_stories(self) -> list[Story]:
        return [s for ch in self.chapters for s in ch.stories]

    def total_words(self) -> int:
        return sum(s.word_count for s in self.all_stories())

    def to_dict(self) -> dict[str, Any]:
        stories = self.all_stories()
        return {
            "book_title": self.config.book_title,
            "topic": self.config.topic,
            "audience": self.config.audience,
            "tone": self.config.tone,
            "config": self.config.to_dict(),
            "chapters": [c.to_dict() for c in self.chapters],
            "story_count": len(stories),
            "total_words": self.total_words(),
            "warnings": list(self.warnings),
            "usage": dict(self.usage),
        }


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def count_words(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


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


def opener_key(text: str) -> str:
    """First few meaningful words of a story, lowercased.

    Used to catch the "It was a cold morning in..." formula repeating across a
    hundred stories — the most visible tell of machine-written filler.
    """
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    return " ".join(words[:OPENER_WORDS])


# --------------------------------------------------------------------------
# Raw-output cache and usage ledger
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

    Never regenerate content we already paid for. Keyed on the full prompt, so
    a changed context box correctly misses the cache — two different prompts
    must never share an entry.
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
    """One openclaw agent call, same shape as trivia.engine.call_openclaw_raw.

    When a cache is supplied, an identical prompt is served from disk instead
    of being re-billed.

    Each story is an independent one-shot request, so callers pass a per-build
    session_id. Without one the CLI files every call under the shared default
    session key, where a hundred stories and their replies pile up into a
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
            raise StoryError(
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


def _extract_json_object(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model reply that may be fenced or prefaced."""
    s = (text or "").strip()
    if not s:
        raise StoryError("Model returned an empty reply.")

    fenced = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fenced:
        s = fenced.group(1).strip()

    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        start = s.find("{")
        end = s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise StoryError(f"Could not find a JSON object in reply: {s[:400]}")
        try:
            parsed = json.loads(s[start:end + 1])
        except json.JSONDecodeError as exc:
            raise StoryError(f"Malformed JSON in reply: {exc}") from exc

    if isinstance(parsed, list):
        # A model asked for one object occasionally wraps it in an array.
        if len(parsed) == 1 and isinstance(parsed[0], dict):
            return parsed[0]
        raise StoryError("Model returned an array where one object was expected.")
    if not isinstance(parsed, dict):
        raise StoryError("Model reply was not a JSON object.")
    return parsed


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

def _book_context(cfg: BookConfig) -> str:
    lines = [
        f"BOOK TITLE: {cfg.book_title}",
        f"BOOK TOPIC: {cfg.topic}",
        f"AUDIENCE: {cfg.audience}",
        f"TONE: {cfg.tone}",
    ]
    if cfg.global_context:
        lines.append(f"BOOK-WIDE NOTES: {cfg.global_context}")
    return "\n".join(lines)


def build_story_prompt(
    cfg: BookConfig,
    st: StoryConfig,
    *,
    chapter_title: str = "",
    avoid_openers: Optional[list[str]] = None,
    retry_reason: str = "",
) -> str:
    """The core prompt: one researched micro-story from a title + context box.

    Two modes, chosen by how much context the operator supplied:

    - Rich context: treat it as verified ground truth, expand into prose, and
      do not invent facts beyond it.
    - Thin context (a title, maybe one line): the model must rely on what it
      actually knows about the real event, and flag anything it is unsure of
      rather than inventing plausible detail.

    The distinction matters because these books are sold as true stories. A
    fabricated name or date in a "real stories" book is the defect that gets a
    title pulled, so uncertainty is surfaced instead of smoothed over.
    """
    lo, hi = st.word_band(cfg)
    context = st.context_block()

    where_line = f"CHAPTER: {chapter_title}\n" if chapter_title else ""

    if context:
        grounding = (
            "The context below was gathered by the book's researcher. Treat "
            "every fact in it as verified and correct, and build the story "
            "around it.\n\n"
            f"{context}\n\n"
            "You may add real, well-established detail you are confident about "
            "to bring the scene to life. You must NOT invent names, dates, "
            "dollar amounts, locations, or quotes that you are not confident "
            "are real. If the context is thin, write a shorter, tighter story "
            "rather than padding it with invented specifics."
        )
    else:
        grounding = (
            "No research notes were supplied for this story, so work from what "
            "you actually know about this real event.\n\n"
            "You must NOT invent names, dates, dollar amounts, locations, or "
            "quotes. If you are not confident about a specific detail, either "
            "leave it out or describe it in general terms ('a small town in "
            "the Midwest' rather than a town you are guessing at). List every "
            "detail you are unsure about in the uncertain_claims field — a "
            "human researcher will verify them before publication."
        )

    # Optional extra fields, described inline in the JSON shape so the model
    # sees the instruction exactly where it has to produce the value.
    extra_fields: list[str] = []
    if cfg.sidebar_enabled:
        instruction = cfg.sidebar_instruction or (
            "two or three sentences picking apart the single most striking "
            "thing about this story"
        )
        extra_fields.append(
            f'  "sidebar": "{cfg.sidebar_label} — {instruction}",'
        )
    if cfg.closer_enabled:
        instruction = cfg.closer_instruction or (
            "one or two sentences that land the story with a wry final "
            "observation"
        )
        extra_fields.append(
            f'  "closer": "{cfg.closer_label} — {instruction}",'
        )
    extras_block = ("\n" + "\n".join(extra_fields)) if extra_fields else ""

    avoid_block = ""
    if avoid_openers:
        shown = avoid_openers[-40:]
        avoid_block = (
            "\nOTHER STORIES IN THIS BOOK ALREADY OPEN WITH THESE PHRASES. "
            "Open yours differently — a book where every story starts the same "
            "way reads as machine-written:\n"
            + "\n".join(f"- {o}" for o in shown)
            + "\n"
        )

    retry_block = ""
    if retry_reason:
        retry_block = (
            f"\nYOUR PREVIOUS ATTEMPT WAS REJECTED: {retry_reason}\n"
            "Fix exactly that problem in this attempt.\n"
        )

    return (
        f"{_book_context(cfg)}\n"
        f"{where_line}"
        f"STORY TITLE: {st.title}\n"
        f"\n{grounding}\n"
        f"{avoid_block}"
        f"{retry_block}"
        f"\nWrite this story as {lo}-{hi} words of finished prose.\n"
        "\nHARD REQUIREMENTS:\n"
        f"1. Between {lo} and {hi} words. This is a hard requirement.\n"
        "2. Real events only. Never fabricate a fact to make the story better.\n"
        "3. Open with the specific scene or the hook, not with a throat-clearing "
        "preamble. Never begin with 'In the world of', 'Picture this', 'Imagine', "
        "'It was a', or a dictionary-style definition.\n"
        "4. Plain, vivid, conversational prose. Short paragraphs. No bullet "
        "lists, no headings, no markdown inside the story body.\n"
        "5. Do not address the reader as 'you', and do not editorialize about "
        "the book itself.\n"
        f"6. Match the tone: {cfg.tone}.\n"
        "7. Do not restate the story title as your first sentence.\n"
        "\nReturn ONLY a JSON object, no prose outside it, no markdown fence:\n"
        "{\n"
        '  "title": "the story title, lightly polished if it reads awkwardly",\n'
        '  "body": "the full story text, paragraphs separated by \\n\\n",'
        f"{extras_block}\n"
        '  "cited_sources": ["publication or outlet you are drawing on, if any"],\n'
        '  "uncertain_claims": ["any detail a fact-checker should verify"]\n'
        "}\n"
    )


def build_outline_prompt(cfg_title: str, topic: str, count: int, notes: str = "") -> str:
    """Ask the model to propose an outline of real, researchable stories.

    This is the "I have a book idea but not the list yet" path. Every suggestion
    comes back with a context box already filled in, so the operator edits
    rather than researches from scratch.
    """
    notes_block = f"\nADDITIONAL DIRECTION: {notes}\n" if notes else ""
    return (
        f"BOOK TITLE: {cfg_title}\n"
        f"BOOK TOPIC: {topic}\n"
        f"{notes_block}"
        f"\nPropose {count} real, documented stories for this book.\n"
        "\nHARD REQUIREMENTS:\n"
        "1. Every story must be a REAL, documented event with news coverage or "
        "published records available for fact-checking. No invented stories.\n"
        "2. Each entry needs a short punchy title suitable as a sub-chapter "
        "heading.\n"
        "3. Fill in who / year / where only when you are confident they are "
        "correct. Leave them as empty strings when you are not — a wrong date "
        "is worse than a missing one.\n"
        "4. The context field is 2-4 sentences summarizing what actually "
        "happened and why it belongs in this book.\n"
        "5. Every story must be about a different event. No two entries may "
        "cover the same incident.\n"
        "\nReturn ONLY a JSON array, no prose, no markdown fence. Each element:\n"
        '{"title": "...", "who": "...", "year": "...", "where": "...", '
        '"context": "...", "sources": "outlets that covered it"}\n'
    )


def _extract_json_array(text: str) -> list[Any]:
    """Array counterpart to _extract_json_object, used by the outline builder."""
    s = (text or "").strip()
    if not s:
        raise StoryError("Model returned an empty reply.")

    fenced = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fenced:
        s = fenced.group(1).strip()

    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        start = s.find("[")
        end = s.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise StoryError(f"Could not find a JSON array in reply: {s[:400]}")
        try:
            parsed = json.loads(s[start:end + 1])
        except json.JSONDecodeError as exc:
            raise StoryError(f"Malformed JSON in reply: {exc}") from exc

    if isinstance(parsed, dict):
        for key in ("stories", "outline", "items", "data"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        raise StoryError("Model returned an object with no recognizable array.")
    if not isinstance(parsed, list):
        raise StoryError("Model reply was not a JSON array.")
    return parsed


# --------------------------------------------------------------------------
# Parsing / validation of generated stories
# --------------------------------------------------------------------------

# Openings that mark generic AI filler rather than a specific true story.
BANNED_OPENERS = (
    "in the world of",
    "in a world",
    "picture this",
    "imagine ",
    "it was a cold",
    "it was a dark",
    "it was a quiet",
    "have you ever",
    "let me tell you",
    "there are many",
    "throughout history",
    "since the dawn",
)


def clean_body(text: str) -> str:
    """Strip markdown scaffolding a model may have wrapped around the prose."""
    s = (text or "").strip()
    # Models sometimes emit the story title as a heading despite being told not
    # to; the exporters render the title themselves, so a duplicate must go.
    s = re.sub(r"^#{1,6}\s+.*\n+", "", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", s)
    # Collapse runs of blank lines into a single paragraph break.
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _string_list(value: Any, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text.lower() not in {"none", "n/a", "unknown"}:
            out.append(text[:400])
        if len(out) >= limit:
            break
    return out


def parse_story_reply(
    raw: dict[str, Any],
    st: StoryConfig,
    chapter_number: int,
) -> Story:
    """Turn one model reply into a Story. Raises StoryError if it is unusable."""
    body = clean_body(str(raw.get("body") or raw.get("story") or raw.get("text") or ""))
    if not body:
        raise StoryError("reply contained no story body")

    title = str(raw.get("title") or "").strip() or st.title

    return Story(
        id=f"s{st.number:03d}",
        number=st.number,
        chapter=chapter_number,
        title=title,
        body=body,
        sidebar=clean_body(str(raw.get("sidebar") or "")),
        closer=clean_body(str(raw.get("closer") or "")),
        context=st.context,
        who=st.who,
        year=st.year,
        where=st.where,
        sources=st.sources,
        cited_sources=_string_list(raw.get("cited_sources") or raw.get("sources")),
        uncertain_claims=_string_list(raw.get("uncertain_claims")),
        word_count=count_words(body),
    )


def check_story_quality(
    story: Story,
    cfg: BookConfig,
    st: StoryConfig,
    *,
    used_openers: Optional[dict[str, int]] = None,
) -> list[str]:
    """Return the reasons this story should be rewritten. Empty means accepted.

    Length is checked against the configured band with tolerance; openers are
    checked against a banned list and against what other stories already used.
    """
    problems: list[str] = []
    lo, hi = st.word_band(cfg)

    floor = int(lo * (1 - LENGTH_TOLERANCE))
    ceiling = int(hi * (1 + LENGTH_TOLERANCE))
    if story.word_count < floor:
        problems.append(
            f"too short — {story.word_count} words, needs {lo}-{hi}"
        )
    elif story.word_count > ceiling:
        problems.append(
            f"too long — {story.word_count} words, needs {lo}-{hi}"
        )

    lowered = story.body.lstrip().lower()
    for banned in BANNED_OPENERS:
        if lowered.startswith(banned):
            problems.append(f"opens with the banned phrase '{banned.strip()}'")
            break

    # A story that opens by restating its own heading wastes the hook.
    title_tokens = _tokens(story.title)
    first_sentence = re.split(r"(?<=[.!?])\s", story.body.strip())[0] if story.body else ""
    if title_tokens and _tokens(first_sentence) >= title_tokens and len(title_tokens) >= 3:
        problems.append("first sentence just restates the story title")

    if used_openers is not None:
        key = opener_key(story.body)
        if key and used_openers.get(key, 0) >= MAX_OPENER_REPEATS:
            problems.append(
                "opens with the same phrasing as other stories in this book"
            )

    if re.search(r"^\s*[-*•]\s", story.body, re.MULTILINE):
        problems.append("contains a bullet list; the body must be flowing prose")

    return problems


def find_duplicate(story: Story, others: list[Story]) -> Optional[Story]:
    """The story in `others` that retells the same event, if any.

    Deterministic only. Unlike trivia, there is no LLM judge here: near-total
    vocabulary overlap across 400 words of prose is already conclusive, and a
    judge call per story pair would cost more than the book.
    """
    best: Optional[Story] = None
    best_score = 0.0
    for other in others:
        if other.id == story.id:
            continue
        score = jaccard(story.claim_text(), other.claim_text())
        if score > best_score:
            best_score, best = score, other
    return best if best_score >= NEAR_DUPLICATE_JACCARD else None
