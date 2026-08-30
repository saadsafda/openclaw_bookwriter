"""Researched-stories book build pipeline.

Orchestrates: one story per outline entry, the quality gate with rewrites,
a duplicate sweep across the finished book, illustrations, and export to
JSON / Markdown / DOCX / KDP 6x9.

The unit of work is one story, generated from its own title + context box and
kept independent of every other story. That independence is deliberate: a
hundred-story book must not fail wholesale because story 63 could not be
written, so a story that exhausts its attempts is recorded as a warning and the
build carries on.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from . import engine
from .engine import (
    BookConfig,
    Chapter,
    ChapterConfig,
    ProviderRejectionError,
    Story,
    StoryBook,
    StoryConfig,
    StoryError,
    ValidationGateError,
)

LogFn = Callable[[str], None]
ProgressFn = Callable[[str, float], None]


def _noop_log(_msg: str) -> None:
    return None


def _noop_progress(_stage: str, _pct: float) -> None:
    return None


class StoryBuilder:
    def __init__(
        self,
        cfg: BookConfig,
        *,
        log: Optional[LogFn] = None,
        progress: Optional[ProgressFn] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self.cfg = cfg
        self.log = log or _noop_log
        self.progress = progress or _noop_progress
        self.should_stop = should_stop or (lambda: False)
        # Raw outputs are cached on disk and usage is metered, so a re-run never
        # re-buys content and every book has unit economics.
        self.ledger = engine.UsageLedger()
        self.cache: Optional[engine.RawOutputCache] = None
        if cache_dir is not None:
            self.cache = engine.RawOutputCache(Path(cache_dir))
        # One throwaway session per build. Stories are independent one-shots;
        # letting them share the CLI's default session accumulates every prompt
        # and reply into one conversation that eventually exceeds what the
        # provider accepts, failing the build with an opaque rejection.
        self.session_id = f"stories-{uuid.uuid4().hex[:12]}"
        self.book = StoryBook(config=cfg)
        # Opener phrase -> how many stories already used it. Fed back into
        # prompts so the book does not settle into one rhythm.
        self.used_openers: dict[str, int] = {}
        self.failed: list[tuple[StoryConfig, str]] = []

    # -- helpers ---------------------------------------------------------

    def _check_stop(self) -> None:
        if self.should_stop():
            raise StoryError("Build stopped by operator.")

    def _call(self, prompt: str) -> str:
        return engine.call_openclaw_raw(
            self.cfg.agent,
            prompt,
            local=self.cfg.local,
            thinking=self.cfg.thinking,
            timeout_s=self.cfg.timeout_s,
            cache=self.cache,
            ledger=self.ledger,
            session_id=self.session_id,
            model=self.cfg.model,
            log=self.log,
        )

    def _note_opener(self, story: Story) -> None:
        key = engine.opener_key(story.body)
        if key:
            self.used_openers[key] = self.used_openers.get(key, 0) + 1

    # -- one story -------------------------------------------------------

    def generate_story(
        self,
        st: StoryConfig,
        chapter: Chapter,
        *,
        chapter_title: str = "",
    ) -> Optional[Story]:
        """Write one story, retrying against the quality gate.

        Returns None when every attempt failed — the caller records it as a gap
        rather than aborting the whole book.
        """
        lo, hi = st.word_band(self.cfg)
        label = f"Story {st.number} '{st.title[:48]}'"
        self.log(f"{label}: writing ({lo}-{hi} words)")

        retry_reason = ""
        last_problem = ""
        best_effort: Optional[Story] = None

        for attempt in range(1, engine.MAX_STORY_ATTEMPTS + 1):
            self._check_stop()

            prompt = engine.build_story_prompt(
                self.cfg,
                st,
                chapter_title=chapter_title,
                avoid_openers=self._crowded_openers(),
                retry_reason=retry_reason,
            )

            try:
                reply = self._call(prompt)
                raw = engine._extract_json_object(reply)
                story = engine.parse_story_reply(raw, st, chapter.number)
            except ProviderRejectionError as exc:
                # Re-sending the same prompt cannot clear an upstream refusal,
                # and the next story will hit the same wall, so stop the build.
                raise StoryError(
                    f"{label}: the AI provider refused the request — {exc}. "
                    "This is not a problem with your outline or context."
                ) from exc
            except StoryError as exc:
                last_problem = str(exc)
                retry_reason = f"the reply could not be read ({exc})"
                self.log(f"  attempt {attempt} failed: {exc}")
                continue

            problems = engine.check_story_quality(
                story, self.cfg, st, used_openers=self.used_openers
            )
            if not problems:
                self.log(f"  accepted ({story.word_count} words)")
                self._note_opener(story)
                return story

            last_problem = "; ".join(problems)
            retry_reason = last_problem
            self.log(f"  attempt {attempt} rejected: {last_problem}")

            # Keep the closest attempt. A story that is merely 40 words short is
            # far better than a hole in the book, so if every attempt fails the
            # gate we publish the best one with a warning attached.
            #
            # Only attempts clearing the absolute floor are eligible: anything
            # below it is rejected outright by the export gate, so keeping one
            # would trade a single missing story for a book that cannot be
            # exported at all.
            if story.word_count >= engine.ABSOLUTE_MIN_WORDS and (
                best_effort is None or story.word_count > best_effort.word_count
            ):
                best_effort = story
                best_effort.warnings = list(problems)

        if best_effort is not None:
            self.log(
                f"  kept best attempt with warnings ({best_effort.word_count} words)"
            )
            self._note_opener(best_effort)
            self.book.warnings.append(
                f"Story {st.number} '{st.title}' did not fully pass the quality "
                f"gate: {last_problem}. It was kept and flagged for review."
            )
            return best_effort

        self.log(f"  GAVE UP after {engine.MAX_STORY_ATTEMPTS} attempts")
        self.failed.append((st, last_problem))
        self.book.warnings.append(
            f"Story {st.number} '{st.title}' could not be generated: {last_problem}"
        )
        return None

    def _crowded_openers(self) -> list[str]:
        """Openers already used enough times to be worth steering away from."""
        return [
            key for key, n in self.used_openers.items()
            if n >= engine.MAX_OPENER_REPEATS
        ]

    # -- duplicate sweep -------------------------------------------------

    def duplicate_pass(self) -> list[tuple[Story, Story]]:
        """Find stories that retell the same event.

        Reported rather than auto-deleted: with an operator-supplied outline a
        near-duplicate usually means two outline entries covering one incident,
        which is an editorial call, not something to silently fix.
        """
        if not self.cfg.check_duplicates:
            return []

        self.log("Checking for duplicate stories")
        found: list[tuple[Story, Story]] = []
        seen: list[Story] = []
        for story in self.book.all_stories():
            twin = engine.find_duplicate(story, seen)
            if twin is not None:
                found.append((story, twin))
            seen.append(story)

        for story, twin in found:
            self.book.warnings.append(
                f"Story {story.number} '{story.title}' looks like a retelling of "
                f"story {twin.number} '{twin.title}'."
            )
        self.log(f"  {len(found)} possible duplicate(s)")
        return found

    # -- illustrations ---------------------------------------------------

    def generate_illustrations(self, out_dir: Path) -> None:
        """One image per chapter, or one per story when configured.

        No humans and no text of any kind: these books cover real people, and a
        generated face attached to a named real person is both a likeness
        problem and a factual claim the book cannot support.
        """
        if not self.cfg.illustrations:
            self.log("Illustrations disabled; skipping")
            return

        import openclaw_image_maker as image_maker

        api_key = image_maker.resolve_api_key(self.cfg.openai_api_key)
        if not api_key:
            self.book.warnings.append(
                "Illustrations were requested but no OpenAI API key was found; "
                "the book was exported without images."
            )
            self.log("WARNING: no OpenAI API key; skipping illustrations")
            return

        cache_dir = out_dir / "illustrations"
        cache_dir.mkdir(parents=True, exist_ok=True)

        from .image_edit import to_grayscale

        def _make(prompt: str, cache_key: str) -> str:
            path = image_maker.generate_image_openai(
                prompt=prompt,
                api_key=api_key,
                cache_dir=cache_dir,
                cache_key=cache_key,
                model=self.cfg.image_model,
                size=self.cfg.image_size,
                quality=self.cfg.image_quality,
            )
            # Interiors print black and white and models return a colour cast
            # even when asked for grayscale, so convert rather than trust the
            # prompt.
            try:
                to_grayscale(path)
            except Exception as exc:
                self.log(f"  grayscale conversion failed: {exc}")
            return str(path)

        if self.cfg.illustrate_every_story:
            targets = [
                (s, f"story_{s.number:03d}", s.title, s.title)
                for s in self.book.all_stories()
            ]
        else:
            targets = [
                (ch, f"chapter_{ch.number}", ch.title or self.cfg.topic,
                 ch.title or self.cfg.topic)
                for ch in self.book.chapters
            ]

        for target, cache_key, subject, _label in targets:
            self._check_stop()
            prompt = build_illustration_prompt(self.cfg, subject)
            target.illustration_prompt = prompt
            self.log(f"Illustration: {subject[:50]}")
            try:
                target.illustration_path = _make(prompt, cache_key)
            except Exception as exc:  # image failure must not lose the text
                self.log(f"  illustration failed: {exc}")
                self.book.warnings.append(f"Illustration for '{subject}' failed: {exc}")

    # -- export gate -----------------------------------------------------

    def validate_for_export(self) -> list[str]:
        """Hard failures that block export. Empty means exportable."""
        errors: list[str] = []
        stories = self.book.all_stories()

        if not stories:
            errors.append("The book contains no stories.")
            return errors

        for story in stories:
            if not story.body.strip():
                errors.append(f"Story {story.number} '{story.title}' has no text.")
            elif story.word_count < engine.ABSOLUTE_MIN_WORDS:
                errors.append(
                    f"Story {story.number} '{story.title}' is only "
                    f"{story.word_count} words, below the {engine.ABSOLUTE_MIN_WORDS}-word floor."
                )

        # A book missing most of its outline is not a book. One or two gaps are
        # reported as warnings and left to the operator; losing a quarter of the
        # outline means something systemic went wrong.
        requested = len(self.cfg.all_stories())
        if requested and len(stories) < requested * 0.75:
            errors.append(
                f"Only {len(stories)} of {requested} stories were generated. "
                "Too much of the outline is missing to export a usable book."
            )

        return errors

    # -- driver ----------------------------------------------------------

    def build(self, out_dir: Path) -> StoryBook:
        started = time.time()
        out_dir.mkdir(parents=True, exist_ok=True)

        # Default the raw-output cache alongside the book's other artifacts so
        # a resumed or repeated build reuses what was already paid for.
        if self.cache is None:
            self.cache = engine.RawOutputCache(out_dir / "raw_cache")

        total = max(1, len(self.cfg.all_stories()))
        done = 0

        for ch_cfg in self.cfg.chapters:
            self._check_stop()
            chapter = Chapter(
                number=ch_cfg.chapter_number,
                title=ch_cfg.chapter_title,
                intro=ch_cfg.chapter_intro,
            )
            self.book.chapters.append(chapter)

            if ch_cfg.chapter_title:
                self.log(f"Chapter {ch_cfg.chapter_number}: {ch_cfg.chapter_title}")

            for st in ch_cfg.stories:
                self._check_stop()
                story = self.generate_story(
                    st, chapter, chapter_title=ch_cfg.chapter_title
                )
                if story is not None:
                    chapter.stories.append(story)
                done += 1
                # Illustrations and export take the last slice of the bar.
                self.progress("writing", 0.85 * done / total)

        self._check_stop()
        self.duplicate_pass()
        self.progress("checking", 0.88)

        self._check_stop()
        self.generate_illustrations(out_dir)
        self.progress("illustrations", 0.95)

        errors = self.validate_for_export()
        if errors:
            raise ValidationGateError(
                "Export blocked by validation:\n- " + "\n- ".join(errors[:12])
            )

        self.progress("done", 1.0)

        u = self.ledger.to_dict()
        self.book.usage = u
        stories = self.book.all_stories()
        self.log(
            f"Usage: {u['calls']} call(s), {u['cache_hits']} cache hit(s), "
            f"{u['total_tokens']:,} tokens, ${u['cost_usd']:.4f}"
        )
        self.log(
            f"Wrote {len(stories)} story(ies), {self.book.total_words():,} words "
            f"in {time.time() - started:.1f}s"
        )
        return self.book


# --------------------------------------------------------------------------
# Outline generation — the "I don't have the list yet" path
# --------------------------------------------------------------------------

def generate_outline(
    book_title: str,
    topic: str,
    count: int,
    *,
    notes: str = "",
    agent: str = engine.DEFAULT_AGENT,
    local: bool = False,
    thinking: str = "",
    timeout_s: int = engine.DEFAULT_TIMEOUT,
    cache: Optional[engine.RawOutputCache] = None,
    ledger: Optional[engine.UsageLedger] = None,
    log: Optional[LogFn] = None,
) -> list[dict[str, Any]]:
    """Propose real, researchable stories with their context boxes pre-filled.

    Returned as plain dicts in the config's story shape, so the UI can drop them
    straight into the outline editor for the operator to correct.
    """
    say = log or _noop_log
    session_id = f"stories-outline-{uuid.uuid4().hex[:8]}"

    # Long outlines degrade in one shot — the model starts repeating itself
    # around the 25 mark — so ask in batches and accumulate.
    batch = 20
    proposed: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    while len(proposed) < count:
        need = min(batch, count - len(proposed))
        extra = notes
        if proposed:
            already = "; ".join(p["title"] for p in proposed[-40:])
            extra = (
                f"{notes}\n\nDo NOT repeat any of these stories already chosen: "
                f"{already}"
            ).strip()

        # No model override here: proposing real, researchable stories is a
        # recall-and-reasoning task, which is what the agent's default model is
        # already good at. The writing-strong model is used for the prose.
        reply = engine.call_openclaw_raw(
            agent,
            engine.build_outline_prompt(book_title, topic, need, extra),
            local=local,
            thinking=thinking,
            timeout_s=timeout_s,
            cache=cache,
            ledger=ledger,
            session_id=session_id,
            log=say,
        )
        raw = engine._extract_json_array(reply)

        added = 0
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title or title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())
            proposed.append({
                "number": len(proposed) + 1,
                "title": title,
                "who": str(item.get("who") or "").strip(),
                "year": str(item.get("year") or "").strip(),
                "where": str(item.get("where") or "").strip(),
                "context": str(item.get("context") or "").strip(),
                "sources": str(item.get("sources") or "").strip(),
            })
            added += 1
            if len(proposed) >= count:
                break

        say(f"  outline: {len(proposed)}/{count} stories proposed")
        if added == 0:
            # The model has run dry on distinct real events for this topic;
            # returning a short outline beats looping until the timeout.
            say("  no new stories in the last batch; stopping early")
            break

    return proposed


def build_illustration_prompt(cfg: BookConfig, subject: str) -> str:
    """Wordless, people-free grayscale art.

    These books are about real, named people, so a generated human figure would
    read as a depiction of that person. Objects and scenery only.
    """
    style = cfg.illustration_style_hint or (
        "clean line art with varied line weight and soft stipple or crosshatch "
        "texture, strong central subject, generous white space"
    )
    return (
        f"A completely wordless, text-free standalone book illustration for a "
        f"book about {cfg.topic}. Subject: {subject}. "
        f"Style: {style}. "
        "Strictly neutral grayscale: use only deep black and exactly 3 "
        "distinct shades of cool gray on a stark pure white background "
        "(#FFFFFF). Absolutely no color, no yellow, sepia, cream, or warm "
        "tones. "
        "Absolutely no people, no human figures, no faces, and no body parts. "
        "Absolutely no text, letters, numbers, words, labels, captions, "
        "signatures, watermarks, frames, or borders anywhere in the image; "
        "any books, signs, screens, or papers in the scene must be blank. "
        "Depict only objects, equipment, scenery, or symbolic items. "
        "Keep the full subject visible and centered; do not crop any edge."
    )


# --------------------------------------------------------------------------
# JSON source of truth
# --------------------------------------------------------------------------

def write_json(book: StoryBook, path: Path) -> Path:
    """The JSON is the source of truth — it must allow re-formatting the book
    without regenerating any content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(book.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_json(path: Path) -> StoryBook:
    """Rehydrate a book from its JSON so exports and edits can be re-run."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg = BookConfig.from_dict(data.get("config") or data)
    book = StoryBook(
        config=cfg,
        warnings=list(data.get("warnings") or []),
        usage=dict(data.get("usage") or {}),
    )

    for raw in data.get("chapters") or []:
        chapter = Chapter(
            number=int(raw.get("chapter_number") or 1),
            title=str(raw.get("chapter_title") or ""),
            intro=str(raw.get("chapter_intro") or ""),
            illustration_path=str(raw.get("illustration_path") or ""),
            illustration_prompt=str(raw.get("illustration_prompt") or ""),
        )
        for s in raw.get("stories") or []:
            body = str(s.get("body") or "")
            chapter.stories.append(Story(
                id=str(s.get("id") or ""),
                number=int(s.get("number") or 0),
                chapter=chapter.number,
                title=str(s.get("title") or ""),
                body=body,
                sidebar=str(s.get("sidebar") or ""),
                closer=str(s.get("closer") or ""),
                context=str(s.get("context") or ""),
                who=str(s.get("who") or ""),
                year=str(s.get("year") or ""),
                where=str(s.get("where") or ""),
                sources=str(s.get("sources") or ""),
                cited_sources=list(s.get("cited_sources") or []),
                uncertain_claims=list(s.get("uncertain_claims") or []),
                illustration_path=str(s.get("illustration_path") or ""),
                illustration_prompt=str(s.get("illustration_prompt") or ""),
                # Recomputed rather than trusted: a hand edit to the body in the
                # editor must not leave a stale count behind.
                word_count=engine.count_words(body),
                warnings=list(s.get("warnings") or []),
            ))
        book.chapters.append(chapter)

    return book
