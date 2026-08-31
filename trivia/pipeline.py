"""Trivia book build pipeline — Section 7 of the Royalty Media spec.

Orchestrates: per-chapter trivia generation in batches, fact generation with
the trivia fact_seed exclusion list, the no-overlap gate with regeneration of
failures, a global cross-chapter dedup pass, illustrations, answer key, and
export to JSON / DOCX / KDP 6x9.

Kept separate from app.py's prose job runner on purpose — this pipeline's unit
of work is a validated structured object, not a paragraph of prose.
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
    Collision,
    DedupChecker,
    DidYouKnowFact,
    ProviderRejectionError,
    TriviaBook,
    TriviaError,
    TriviaQuestion,
    ValidationGateError,
)

# How many extra attempts we make to fill a chapter's quota when items are
# rejected by validation or the dedup gate. Each attempt is a fresh batch.
MAX_REFILL_ROUNDS = 6

# How many regenerate-then-resweep rounds the global dedup gate gets before it
# gives up. Each round is a full chapter refill, so this stays small; rounds
# that make no progress bail early regardless.
DEDUP_REPAIR_ROUNDS = 3

# How many facts a chapter may finish short of its configured quota.
#
# A narrow chapter scope holds a finite number of genuinely distinct facts. Once
# the refill loop has exhausted them, every further attempt returns claims the
# no-overlap gate has already rejected, so retrying cannot converge -- the only
# outcomes are this tolerance or discarding a book that is otherwise complete.
# Trading an exact per-chapter count for a shippable book is the right call at
# this margin; the shortfall is surfaced as a warning so it stays visible.
FACT_COUNT_TOLERANCE = 2

# Extra items requested per top-up round beyond the exact shortfall, so a
# chapter needing one more fact still gets a spread of candidates to find a
# non-colliding one among. Surplus past the shortfall is discarded.
REFILL_SURPLUS = 5

# How much of the underlying upstream error to fold into the failure message.
# The raw text carries a full command line and STDOUT/STDERR dump; the first
# line is the part that names the actual cause.
UPSTREAM_DETAIL_CHARS = 300


def _first_line(text: str) -> str:
    """The most informative single line of a multi-line error dump.

    OpenClaw failures arrive as a banner line followed by the command and a
    STDERR block. Surfacing the banner plus the first non-empty STDERR line
    tells the operator whether they hit a timeout, a refusal, or a crash —
    without pasting a screenful into the error box.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    head = lines[0]
    # Pull the first line after a STDERR:/STDOUT: marker, which is where the
    # real reason lives when the CLI itself failed.
    for i, ln in enumerate(lines):
        if ln.upper().startswith(("STDERR:", "STDOUT:")) and i + 1 < len(lines):
            detail = lines[i + 1]
            if detail and detail not in head:
                head = f"{head} — {detail}"
            break
    return head[:UPSTREAM_DETAIL_CHARS]


LogFn = Callable[[str], None]
ProgressFn = Callable[[str, float], None]


def _noop_log(_msg: str) -> None:
    return None


def _noop_progress(_stage: str, _pct: float) -> None:
    return None


class TriviaBuilder:
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
        # Section 12: raw outputs are cached on disk and usage is metered, so a
        # re-run never re-buys content and every book has unit economics.
        self.ledger = engine.UsageLedger()
        self.cache: Optional[engine.RawOutputCache] = None
        if cache_dir is not None:
            self.cache = engine.RawOutputCache(Path(cache_dir))
        # One throwaway session per build. Batches are independent one-shots;
        # letting them share the CLI's default session accumulates every prompt
        # and reply into one conversation that eventually exceeds what the
        # provider accepts, failing the build with an opaque rejection.
        self.session_id = f"trivia-{uuid.uuid4().hex[:12]}"
        self.checker = DedupChecker(
            cfg,
            log=self.log,
            cache=self.cache,
            ledger=self.ledger,
            session_id=self.session_id,
        )
        self.book = TriviaBook(config=cfg)
        self.collisions: list[Collision] = []

    # -- helpers ---------------------------------------------------------

    def _check_stop(self) -> None:
        if self.should_stop():
            raise TriviaError("Build stopped by operator.")

    def _generate_batch(self, prompt: str) -> list[Any]:
        reply = engine.call_openclaw_raw(
            self.cfg.agent,
            prompt,
            local=self.cfg.local,
            thinking=self.cfg.thinking,
            timeout_s=self.cfg.timeout_s,
            cache=self.cache,
            ledger=self.ledger,
            session_id=self.session_id,
            log=self.log,
        )
        return engine._extract_json_array(reply)

    def _generate_prose(self, prompt: str) -> str:
        reply = engine.call_openclaw_raw(
            self.cfg.agent,
            prompt,
            local=self.cfg.local,
            thinking=self.cfg.thinking,
            timeout_s=self.cfg.timeout_s,
            cache=self.cache,
            ledger=self.ledger,
            session_id=self.session_id,
            log=self.log,
        )
        return engine.clean_prose_reply(reply)

    def _generate_front_matter(self, label: str, prompt: str) -> str:
        """One piece of authored prose, retried once if it lands short.

        Asking for 300-500 words tends to produce a tidy 150-word paragraph on
        the first try. A single retry that names the shortfall is enough to
        pull it up, and it costs one call rather than failing the build.
        """
        text = self._generate_prose(prompt)
        count = engine.word_count(text)

        if count < engine.FRONT_MATTER_MIN_WORDS:
            self.log(
                f"  {label} came back at {count} words; asking again for "
                f"{engine.FRONT_MATTER_MIN_WORDS}-{engine.FRONT_MATTER_MAX_WORDS}"
            )
            retry = (
                f"{prompt}\n\nYour previous attempt was only {count} words, "
                f"which is far too short. Write the full "
                f"{engine.FRONT_MATTER_MIN_WORDS} to "
                f"{engine.FRONT_MATTER_MAX_WORDS} words this time. Develop the "
                "ideas with real specifics instead of adding filler.\n"
            )
            longer = self._generate_prose(retry)
            if engine.word_count(longer) > count:
                text, count = longer, engine.word_count(longer)

        if not text:
            self.book.warnings.append(f"{label} could not be generated.")
        elif count < engine.FRONT_MATTER_MIN_WORDS:
            self.book.warnings.append(
                f"{label} is {count} words, short of the "
                f"{engine.FRONT_MATTER_MIN_WORDS}-word target."
            )
        else:
            self.log(f"  {label}: {count} words")
        return text

    def generate_front_matter(self) -> None:
        """Introduction and Conclusion, written once the chapters are known.

        Both run after generation so the prose can speak to the book that
        actually exists, not the one the config asked for. Neither is worth
        failing a finished book over, so a provider refusal is recorded as a
        warning and the export falls back to its generic paragraph.
        """
        for label, builder, attr in (
            ("Introduction", engine.build_introduction_prompt, "introduction"),
            ("Conclusion", engine.build_conclusion_prompt, "conclusion"),
        ):
            self._check_stop()
            try:
                prompt = builder(self.cfg, self.cfg.chapters)
                setattr(self.book, attr, self._generate_front_matter(label, prompt))
            except ProviderRejectionError as exc:
                self.book.warnings.append(f"{label} refused by provider: {exc}")
                self.log(f"  {label} refused by provider; using the default text")

    def _all_trivia(self) -> list[TriviaQuestion]:
        return [q for ch in self.book.chapters for q in ch.trivia]

    def _all_facts(self) -> list[DidYouKnowFact]:
        return [f for ch in self.book.chapters for f in ch.facts]

    # -- trivia ----------------------------------------------------------

    def generate_chapter_trivia(self, ch_cfg: ChapterConfig, chapter: Chapter) -> None:
        """Step 2a-2b: batched generation, structural validation, dedup against
        everything already in the book."""
        target = ch_cfg.trivia_count
        if target <= 0:
            return

        self.log(f"Chapter {ch_cfg.chapter_number}: generating {target} trivia questions")
        accepted: list[TriviaQuestion] = []
        rejected_seeds: list[str] = []
        rounds = 0
        upstream_failures = 0
        last_upstream_error = ""

        while len(accepted) < target and rounds < MAX_REFILL_ROUNDS + target // engine.TRIVIA_BATCH:
            self._check_stop()
            rounds += 1
            need = target - len(accepted)
            # Over-request. Asking for exactly `need` gives the model no room
            # to offer an alternative when a question collides, so a chapter
            # one short would ask for 1, have it rejected, and ask again.
            batch_size = min(engine.TRIVIA_BATCH, need + REFILL_SURPLUS)

            # Exclusion context: seeds used anywhere in the book so far, plus
            # this chapter's accepted questions and the ones the dedup gate has
            # already rejected. Feeding rejects back in is what makes the next
            # prompt differ: an identical prompt is a cache hit, so it would
            # replay the same rejected question forever without ever reaching
            # the provider.
            avoid = [q.fact_seed for q in self._all_trivia() if q.fact_seed]
            avoid += [q.fact_seed for q in accepted if q.fact_seed]
            avoid += rejected_seeds

            prompt = engine.build_trivia_prompt(self.cfg, ch_cfg, batch_size, avoid)
            try:
                raw = self._generate_batch(prompt)
            except ProviderRejectionError as exc:
                # Re-sending the same prompt cannot clear an upstream refusal.
                raise TriviaError(
                    f"Chapter {ch_cfg.chapter_number}: the AI provider refused the "
                    f"request, so no trivia could be generated — {exc}. This is not "
                    "a problem with your chapter scope or trivia_count."
                ) from exc
            except TriviaError as exc:
                upstream_failures += 1
                last_upstream_error = str(exc)
                self.log(f"  batch failed ({exc}); retrying")
                continue

            parsed, rejects = engine.parse_trivia_items(raw, ch_cfg.chapter_number)
            for reason in rejects[:5]:
                self.log(f"  rejected: {reason}")

            # Assign provisional ids so the dedup checker can reference them.
            for i, item in enumerate(parsed):
                item.id = f"ch{ch_cfg.chapter_number}_q{len(accepted) + i + 1:02d}"

            parsed = engine.dedup_within(parsed)

            prior = self._all_trivia() + accepted
            colliding = self.checker.find_collisions(parsed, prior, "trivia_vs_trivia")
            rejected_seeds.extend(
                q.fact_seed for q in parsed if q.id in colliding and q.fact_seed
            )
            fresh = [q for q in parsed if q.id not in colliding]
            if colliding:
                self.log(f"  dropped {len(colliding)} duplicate question(s)")

            accepted.extend(fresh[:need])
            self.log(f"  chapter {ch_cfg.chapter_number}: {len(accepted)}/{target} questions")

        if len(accepted) < target:
            # Blaming the scope is actively misleading when every batch died
            # upstream — the operator would rewrite a config that was fine.
            if upstream_failures == rounds:
                hint = (
                    "Every attempt failed before any content was generated, so this "
                    "is an AI service problem, not a problem with your chapter scope "
                    "or trivia_count. Nothing was generated, so retrying costs "
                    "nothing extra."
                )
                if last_upstream_error:
                    hint += f" Last error: {_first_line(last_upstream_error)}"
            elif upstream_failures:
                hint = (
                    f"{upstream_failures} of {rounds} attempts failed upstream; the "
                    "rest were rejected by validation or the dedup gate. Retry, and "
                    "if it persists widen the chapter scope or lower trivia_count."
                )
                if last_upstream_error:
                    hint += f" Last upstream error: {_first_line(last_upstream_error)}"
            else:
                hint = (
                    "Attempts succeeded but the content was rejected as duplicate or "
                    "invalid. Widen the chapter scope or lower trivia_count."
                )
            # Keep what was generated. These questions were paid for, and the
            # resolve path can only top a chapter up (or let the operator lower
            # trivia_count) if they are actually on the chapter -- discarding
            # them here is what stranded a blocked book with 0 questions and
            # no way forward.
            for i, q in enumerate(accepted, start=1):
                q.id = f"ch{ch_cfg.chapter_number}_q{i:02d}"
            if engine.rebalance_answer_distribution(accepted):
                self.log("  rebalanced correct-answer distribution across A-D")
            chapter.trivia = accepted
            raise ValidationGateError(
                f"Chapter {ch_cfg.chapter_number}: only produced {len(accepted)} of "
                f"{target} required trivia questions after {rounds} attempts. {hint}"
            )

        # Final numbering, stable and sequential.
        for i, q in enumerate(accepted, start=1):
            q.id = f"ch{ch_cfg.chapter_number}_q{i:02d}"

        if engine.rebalance_answer_distribution(accepted):
            self.log("  rebalanced correct-answer distribution across A-D")

        chapter.trivia = accepted

    # -- facts -----------------------------------------------------------

    def generate_chapter_facts(self, ch_cfg: ChapterConfig, chapter: Chapter) -> None:
        """Step 2c-2e: exclusion list from trivia seeds, then the hard gate."""
        target = ch_cfg.fact_count
        if target <= 0:
            return

        self.log(f"Chapter {ch_cfg.chapter_number}: generating {target} facts")
        accepted: list[DidYouKnowFact] = []
        rounds = 0
        upstream_failures = 0
        last_upstream_error = ""

        while len(accepted) < target and rounds < MAX_REFILL_ROUNDS + target // engine.FACT_BATCH:
            self._check_stop()
            rounds += 1
            need = target - len(accepted)
            batch_size = min(engine.FACT_BATCH, need)

            # Section 6 step 3 — every trivia claim in the book is excluded,
            # expressed as readable claims rather than bare slugs so the model
            # can actually reason about what to avoid.
            # The dedup gate compares facts against every chapter's trivia, so
            # the exclusion list has to span the whole book too. Showing only
            # this chapter's questions lets a regenerated fact collide with a
            # later chapter's question the model was never told to avoid.
            exclusions = [q.claim_text() for q in self._all_trivia()]
            exclusions += [f.fact for f in accepted]

            prompt = engine.build_facts_prompt(self.cfg, ch_cfg, batch_size, exclusions)
            try:
                raw = self._generate_batch(prompt)
            except ProviderRejectionError as exc:
                raise TriviaError(
                    f"Chapter {ch_cfg.chapter_number}: the AI provider refused the "
                    f"request, so no facts could be generated — {exc}. This is not a "
                    "problem with your chapter scope or fact_count."
                ) from exc
            except TriviaError as exc:
                upstream_failures += 1
                last_upstream_error = str(exc)
                self.log(f"  batch failed ({exc}); retrying")
                continue

            parsed, rejects = engine.parse_fact_items(raw, ch_cfg.chapter_number)
            for reason in rejects[:5]:
                self.log(f"  rejected: {reason}")

            for i, item in enumerate(parsed):
                item.id = f"ch{ch_cfg.chapter_number}_f{len(accepted) + i + 1:03d}"

            parsed = engine.dedup_within(parsed)

            # Gate 1: facts must not restate trivia anywhere in the book. This
            # has to match global_dedup_pass, which sweeps book-wide; a narrower
            # check here just defers the failure to the export gate.
            against_trivia = self.checker.find_collisions(
                parsed, self._all_trivia(), "fact_vs_trivia"
            )
            # Gate 2: facts must not repeat other facts anywhere in the book.
            against_facts = self.checker.find_collisions(
                parsed, self._all_facts() + accepted, "fact_vs_fact"
            )
            bad = against_trivia | against_facts
            fresh = [f for f in parsed if f.id not in bad]
            if bad:
                self.log(
                    f"  dropped {len(bad)} overlapping fact(s) "
                    f"({len(against_trivia)} vs trivia, {len(against_facts)} vs facts)"
                )

            accepted.extend(fresh[:need])
            self.log(f"  chapter {ch_cfg.chapter_number}: {len(accepted)}/{target} facts")

        shortfall = target - len(accepted)
        if 0 < shortfall <= FACT_COUNT_TOLERANCE and upstream_failures < rounds:
            # The scope is simply exhausted, not broken. Keep the chapter.
            self.book.warnings.append(
                f"Chapter {ch_cfg.chapter_number}: {len(accepted)} facts instead of "
                f"{target}. The chapter scope did not yield more non-duplicate "
                f"facts. Widen the scope or lower fact_count to avoid this."
            )
            self.log(
                f"  chapter {ch_cfg.chapter_number}: accepting {len(accepted)}/{target} "
                f"facts -- scope exhausted, within tolerance of {FACT_COUNT_TOLERANCE}"
            )
        elif len(accepted) < target:
            if upstream_failures == rounds:
                hint = (
                    "Every attempt failed before any content was generated, so this "
                    "is an AI service problem, not a problem with your chapter scope "
                    "or fact_count. Nothing was generated, so retrying costs nothing "
                    "extra."
                )
                if last_upstream_error:
                    hint += f" Last error: {_first_line(last_upstream_error)}"
            elif upstream_failures:
                hint = (
                    f"{upstream_failures} of {rounds} attempts failed upstream; the "
                    "no-overlap gate rejected the rest. Retry, and if it persists "
                    "widen the scope or lower fact_count."
                )
                if last_upstream_error:
                    hint += f" Last upstream error: {_first_line(last_upstream_error)}"
            else:
                hint = (
                    "The no-overlap gate rejected the rest. Widen the chapter scope "
                    "or lower fact_count."
                )
            raise ValidationGateError(
                f"Chapter {ch_cfg.chapter_number}: only produced {len(accepted)} of "
                f"{target} required facts after {rounds} attempts. {hint}"
            )

        for i, f in enumerate(accepted, start=1):
            f.id = f"ch{ch_cfg.chapter_number}_f{i:03d}"

        chapter.facts = accepted

    # -- global pass -----------------------------------------------------

    def global_dedup_pass(self) -> list[Collision]:
        """Step 3: cross-chapter sweep. Chapters are generated with knowledge of
        earlier ones, but a late chapter can still echo an early one, and this
        is the last chance to catch it before export."""
        self.log("Global dedup pass across all chapters")
        found: list[Collision] = []

        all_trivia = self._all_trivia()
        all_facts = self._all_facts()

        self.checker.collisions = []
        # Compare every fact against every trivia claim, book-wide.
        self.checker.find_collisions(all_facts, all_trivia, "fact_vs_trivia")
        found.extend(self.checker.collisions)

        self.checker.collisions = []
        self.checker.find_collisions(all_facts, all_facts, "fact_vs_fact")
        found.extend(self.checker.collisions)

        self.checker.collisions = []
        self.checker.find_collisions(all_trivia, all_trivia, "trivia_vs_trivia")
        found.extend(self.checker.collisions)

        # Self-pairs are an artifact of comparing a list against itself.
        found = [c for c in found if c.left_id != c.right_id]
        self.log(f"  {len(found)} collision(s) found")
        return found

    def resolve_collisions(self, collisions: list[Collision]) -> None:
        """Remove and regenerate colliding facts. Trivia collisions are reported
        rather than auto-dropped, since removing a question would break the
        chapter's configured count."""
        if not collisions:
            return

        drop_ids = {
            c.left_id for c in collisions
            if c.kind in {"fact_vs_trivia", "fact_vs_fact"}
        }
        if not drop_ids:
            return

        self.log(f"Regenerating {len(drop_ids)} colliding fact(s)")
        for ch_cfg, chapter in zip(self.cfg.chapters, self.book.chapters):
            removed = [f for f in chapter.facts if f.id in drop_ids]
            if not removed:
                continue
            chapter.facts = [f for f in chapter.facts if f.id not in drop_ids]
            # Regenerate back up to the configured count.
            self.generate_chapter_facts(ch_cfg, chapter)

    def top_up_short_trivia(self) -> list[str]:
        """Extend chapters sitting under their configured trivia count.

        The counterpart of top_up_short_chapters for questions. Without this a
        chapter that came up short on trivia could never be finished: the
        resolve path had no way to make more questions, and validate_for_export
        requires an exact match, so the book was stranded.

        Appends to what the chapter already has -- existing questions and their
        seeds go into the exclusion list -- so nothing already paid for is
        re-bought. Returns a note per chapter still short afterwards.
        """
        notes: list[str] = []
        for ch_cfg, chapter in zip(self.cfg.chapters, self.book.chapters):
            if ch_cfg.trivia_count <= 0:
                continue
            missing = ch_cfg.trivia_count - len(chapter.trivia)
            if missing <= 0:
                continue

            self.log(
                f"Chapter {chapter.number}: {len(chapter.trivia)}/"
                f"{ch_cfg.trivia_count} questions; topping up {missing}"
            )
            added = self._extend_chapter_trivia(ch_cfg, chapter, missing)
            self.log(
                f"  chapter {chapter.number}: added {added}; now "
                f"{len(chapter.trivia)}/{ch_cfg.trivia_count}"
            )

            still = ch_cfg.trivia_count - len(chapter.trivia)
            if still > 0:
                notes.append(
                    f"Chapter {chapter.number}: {len(chapter.trivia)} of "
                    f"{ch_cfg.trivia_count} questions. The scope did not yield "
                    f"more distinct questions."
                )
        return notes

    def _extend_chapter_trivia(
        self, ch_cfg: ChapterConfig, chapter: Chapter, missing: int
    ) -> int:
        """Append up to `missing` new questions, keeping what the chapter has.

        Mirrors _extend_chapter_facts: every round widens the exclusion list
        with the kept questions and the ones the dedup gate just rejected, so
        the prompt changes and the raw-output cache cannot replay an identical
        reply back at us.
        """
        added: list[TriviaQuestion] = []
        rejected_seeds: list[str] = []
        rounds = 0

        while len(added) < missing and rounds < MAX_REFILL_ROUNDS:
            self._check_stop()
            rounds += 1
            need = missing - len(added)
            batch_size = min(engine.TRIVIA_BATCH, need + REFILL_SURPLUS)

            avoid = [q.fact_seed for q in self._all_trivia() if q.fact_seed]
            avoid += [q.fact_seed for q in added if q.fact_seed]
            avoid += rejected_seeds

            prompt = engine.build_trivia_prompt(self.cfg, ch_cfg, batch_size, avoid)
            try:
                raw = self._generate_batch(prompt)
            except ProviderRejectionError:
                # Re-sending an identical prompt cannot clear a refusal.
                raise
            except TriviaError as exc:
                self.log(f"  top-up batch failed ({exc}); retrying")
                continue

            parsed, _rejects = engine.parse_trivia_items(raw, ch_cfg.chapter_number)
            base = len(chapter.trivia) + len(added)
            for i, item in enumerate(parsed):
                item.id = f"ch{ch_cfg.chapter_number}_q{base + i + 1:02d}"
            parsed = engine.dedup_within(parsed)

            prior = self._all_trivia() + added
            colliding = self.checker.find_collisions(parsed, prior, "trivia_vs_trivia")
            rejected_seeds.extend(
                q.fact_seed for q in parsed if q.id in colliding and q.fact_seed
            )
            fresh = [q for q in parsed if q.id not in colliding]
            if not fresh:
                self.log(
                    f"  no new distinct questions this round "
                    f"({len(colliding)} rejected)"
                )
            added.extend(fresh[:need])

        if added:
            chapter.trivia = list(chapter.trivia) + added
            for i, q in enumerate(chapter.trivia, start=1):
                q.id = f"ch{ch_cfg.chapter_number}_q{i:02d}"
            if engine.rebalance_answer_distribution(chapter.trivia):
                self.log("  rebalanced correct-answer distribution across A-D")
        return len(added)

    def top_up_short_chapters(self) -> list[str]:
        """Extend chapters sitting under their configured fact count.

        Used by the resolve path, where a build was blocked with content already
        generated and paid for. This appends to the facts a chapter already has
        rather than regenerating it: the existing facts go into the exclusion
        list, so the model is asked only for genuinely new material and the
        draft never re-buys what it already holds.

        Returns a note per chapter that is still short afterwards.
        """
        notes: list[str] = []
        for ch_cfg, chapter in zip(self.cfg.chapters, self.book.chapters):
            missing = ch_cfg.fact_count - len(chapter.facts)
            if missing <= 0:
                continue

            self.log(
                f"Chapter {chapter.number}: {len(chapter.facts)}/{ch_cfg.fact_count} "
                f"facts; topping up {missing}"
            )
            added = self._extend_chapter_facts(ch_cfg, chapter, missing)
            self.log(
                f"  chapter {chapter.number}: added {added}; now "
                f"{len(chapter.facts)}/{ch_cfg.fact_count}"
            )

            still = ch_cfg.fact_count - len(chapter.facts)
            if still > 0:
                notes.append(
                    f"Chapter {chapter.number}: {len(chapter.facts)} of "
                    f"{ch_cfg.fact_count} facts. The scope did not yield more "
                    f"distinct facts."
                )
        return notes

    def _extend_chapter_facts(
        self, ch_cfg: ChapterConfig, chapter: Chapter, missing: int
    ) -> int:
        """Append up to `missing` new facts to a chapter, keeping what it has.

        Every attempt widens the exclusion list with both the kept facts and
        the claims the gate just rejected. Without that the prompt would repeat
        verbatim, the raw-output cache would replay the identical reply, and the
        retry would be spent without ever reaching the provider.
        """
        added: list[DidYouKnowFact] = []
        rejected_claims: list[str] = []
        rounds = 0

        while len(added) < missing and rounds < MAX_REFILL_ROUNDS:
            self._check_stop()
            rounds += 1
            need = missing - len(added)
            # Over-request: asking for exactly the shortfall gives the model no
            # room to offer an alternative when a claim collides.
            batch_size = min(engine.FACT_BATCH, need + REFILL_SURPLUS)

            exclusions = [q.claim_text() for q in self._all_trivia()]
            exclusions += [f.fact for f in self._all_facts()]
            exclusions += [f.fact for f in added]
            exclusions += rejected_claims

            prompt = engine.build_facts_prompt(self.cfg, ch_cfg, batch_size, exclusions)
            try:
                raw = self._generate_batch(prompt)
            except TriviaError as exc:
                self.log(f"  top-up batch failed ({exc}); retrying")
                continue

            parsed, _rejects = engine.parse_fact_items(raw, ch_cfg.chapter_number)
            base = len(chapter.facts) + len(added)
            for i, item in enumerate(parsed):
                item.id = f"ch{ch_cfg.chapter_number}_f{base + i + 1:03d}"
            parsed = engine.dedup_within(parsed)

            existing = self._all_facts() + added
            bad = (
                self.checker.find_collisions(parsed, self._all_trivia(), "fact_vs_trivia")
                | self.checker.find_collisions(parsed, existing, "fact_vs_fact")
            )
            rejected_claims.extend(f.fact for f in parsed if f.id in bad)
            fresh = [f for f in parsed if f.id not in bad]
            if not fresh:
                self.log(f"  no new distinct facts this round ({len(bad)} rejected)")
            added.extend(fresh[:need])

        if added:
            chapter.facts = list(chapter.facts) + added
            for i, f in enumerate(chapter.facts, start=1):
                f.id = f"ch{ch_cfg.chapter_number}_f{i:03d}"
        return len(added)

    # -- illustrations ---------------------------------------------------

    def generate_illustrations(self, out_dir: Path) -> None:
        """Section 9: one image per chapter. No humans, no text of any kind."""
        if not self.cfg.illustrations:
            self.log("Illustrations disabled; skipping")
            return

        import openclaw_image_maker as image_maker

        api_key = image_maker.resolve_api_key(self.cfg.openai_api_key)
        if not api_key:
            self.book.warnings.append(
                "Illustrations were requested but no OpenAI API key was found; "
                "chapters exported without images."
            )
            self.log("WARNING: no OpenAI API key; skipping illustrations")
            return

        cache_dir = out_dir / "illustrations"
        cache_dir.mkdir(parents=True, exist_ok=True)

        for ch_cfg, chapter in zip(self.cfg.chapters, self.book.chapters):
            self._check_stop()
            prompt = build_illustration_prompt(self.cfg, ch_cfg)
            chapter.illustration_prompt = prompt
            cache_key = f"ch{ch_cfg.chapter_number}_illustration"
            self.log(f"Chapter {ch_cfg.chapter_number}: generating illustration")
            try:
                path = image_maker.generate_image_openai(
                    prompt=prompt,
                    api_key=api_key,
                    cache_dir=cache_dir,
                    cache_key=cache_key,
                    model=self.cfg.image_model,
                    size=self.cfg.image_size,
                    quality=self.cfg.image_quality,
                )
                # Interiors print black and white and models return a colour
                # cast even when asked for grayscale, so convert rather than
                # trust the prompt.
                from .image_edit import to_grayscale
                try:
                    to_grayscale(path)
                except Exception as exc:
                    self.log(f"  grayscale conversion failed: {exc}")
                chapter.illustration_path = str(path)
            except Exception as exc:  # image failure must not lose the text
                self.log(f"  illustration failed: {exc}")
                self.book.warnings.append(
                    f"Chapter {ch_cfg.chapter_number} illustration failed: {exc}"
                )

    # -- export gate -----------------------------------------------------

    def validate_for_export(self) -> list[str]:
        """Section 8. Returns the list of hard failures; empty means exportable."""
        errors: list[str] = []

        for ch_cfg, chapter in zip(self.cfg.chapters, self.book.chapters):
            if len(chapter.trivia) != ch_cfg.trivia_count:
                errors.append(
                    f"Chapter {chapter.number}: {len(chapter.trivia)} questions, "
                    f"config requires {ch_cfg.trivia_count}."
                )
            # Facts may run slightly under quota when a chapter scope is
            # exhausted; see FACT_COUNT_TOLERANCE. Over quota is still a bug.
            if not (
                ch_cfg.fact_count - FACT_COUNT_TOLERANCE
                <= len(chapter.facts)
                <= ch_cfg.fact_count
            ):
                errors.append(
                    f"Chapter {chapter.number}: {len(chapter.facts)} facts, "
                    f"config requires {ch_cfg.fact_count}."
                )

            for q in chapter.trivia:
                if len(q.choices) != 4 or set(q.choices) != {"A", "B", "C", "D"}:
                    errors.append(f"{q.id}: does not have exactly choices A-D.")
                if q.correct_answer not in {"A", "B", "C", "D"}:
                    errors.append(f"{q.id}: correct_answer is not A-D.")
                elif q.correct_answer not in q.choices:
                    errors.append(f"{q.id}: correct_answer points at a missing choice.")

            if chapter.trivia:
                dist = engine.answer_distribution(chapter.trivia)
                worst = max(dist.values()) / len(chapter.trivia)
                if worst > engine.ANSWER_DISTRIBUTION_TOLERANCE + 0.15:
                    errors.append(
                        f"Chapter {chapter.number}: correct answers cluster on one "
                        f"letter ({dist})."
                    )

        return errors

    # -- driver ----------------------------------------------------------

    def build(self, out_dir: Path) -> TriviaBook:
        started = time.time()
        out_dir.mkdir(parents=True, exist_ok=True)

        # Default the raw-output cache alongside the book's other artifacts so
        # a resumed or repeated build reuses what was already paid for.
        if self.cache is None:
            self.cache = engine.RawOutputCache(out_dir / "raw_cache")
            self.checker.cache = self.cache

        total_steps = max(1, len(self.cfg.chapters) * 2 + 4)
        step = 0

        # A chapter that comes up short must not abort the run. Chapters after
        # it would never be generated at all, and the saved draft would be a
        # stub the resolve path could not finish. Collect the shortfalls and
        # report them together once every chapter has been attempted.
        gate_failures: list[str] = []

        for ch_cfg in self.cfg.chapters:
            self._check_stop()
            chapter = Chapter(
                number=ch_cfg.chapter_number,
                title=ch_cfg.chapter_title,
                scope=ch_cfg.chapter_scope,
            )
            self.book.chapters.append(chapter)

            try:
                self.generate_chapter_trivia(ch_cfg, chapter)
            except ValidationGateError as exc:
                # generate_chapter_trivia keeps its partial output on the
                # chapter before raising, so the draft stays resolvable.
                gate_failures.append(str(exc))
                self.log(f"  {exc}")
            step += 1
            self.progress("trivia", step / total_steps)

            try:
                self.generate_chapter_facts(ch_cfg, chapter)
            except ValidationGateError as exc:
                gate_failures.append(str(exc))
                self.log(f"  {exc}")
            step += 1
            self.progress("facts", step / total_steps)

        self._check_stop()
        # Regenerate and re-sweep. One pass is not enough: a fact rewritten to
        # dodge one question can land on another, and until the exclusion list
        # went book-wide a cross-chapter collision could survive indefinitely.
        # Bounded so a scope too narrow to yield a distinct fact still fails
        # rather than burning tokens forever.
        remaining = self.global_dedup_pass()
        for attempt in range(DEDUP_REPAIR_ROUNDS):
            if not remaining:
                break
            self.collisions = remaining
            before = len(remaining)
            self.resolve_collisions(remaining)
            remaining = self.global_dedup_pass()
            if len(remaining) >= before:
                # No forward progress; more rounds will not help.
                self.log(
                    f"  repair round {attempt + 1} made no progress; stopping"
                )
                break

        self.collisions = remaining
        step += 1
        self.progress("dedup", step / total_steps)

        blocking = [c for c in remaining if c.kind == "fact_vs_trivia"]
        if blocking:
            detail = "; ".join(
                f"{c.left_id} repeats {c.right_id}" for c in blocking[:5]
            )
            raise ValidationGateError(
                f"No-overlap gate failed: {len(blocking)} fact(s) restate trivia "
                f"content. {detail}. These survived "
                f"{DEDUP_REPAIR_ROUNDS} regeneration round(s), so the chapter "
                "scope is likely too narrow to yield a fact distinct from its "
                "questions — widen the scope or lower fact_count for the "
                "chapters named above."
                + (
                    "\n\nAlso: " + "; ".join(gate_failures[:12])
                    if gate_failures else ""
                )
            )

        self._check_stop()
        self.generate_illustrations(out_dir)
        step += 1
        self.progress("illustrations", step / total_steps)

        self._check_stop()
        self.generate_front_matter()
        step += 1
        self.progress("front matter", step / total_steps)

        errors = self.validate_for_export()
        if errors or gate_failures:
            # validate_for_export already reports the resulting counts, so the
            # per-chapter shortfall messages are appended as context (they
            # explain *why*) rather than duplicated as separate failures.
            detail = "Export blocked by validation:\n- " + "\n- ".join(
                (errors or ["chapter generation fell short"])[:12]
            )
            if gate_failures:
                detail += "\n\nDetails:\n- " + "\n- ".join(gate_failures[:12])
            raise ValidationGateError(detail)

        step += 1
        self.progress("done", 1.0)

        # Section 12: per-book unit economics.
        u = self.ledger.to_dict()
        self.book.usage = u
        self.log(
            f"Usage: {u['calls']} call(s), {u['cache_hits']} cache hit(s), "
            f"{u['total_tokens']:,} tokens, ${u['cost_usd']:.4f}"
        )
        self.log(f"Build finished in {time.time() - started:.1f}s")
        return self.book


def build_illustration_prompt(cfg: BookConfig, ch: ChapterConfig) -> str:
    """Section 9 constraints are non-negotiable: no humans, no text at all.

    Interiors print in black and white, so these match the grayscale house
    style used by the prose books (openclaw_image_maker's rich-scene-no-text
    variant): pure black plus a few cool grays on white, never color.
    """
    hint = ch.illustration_prompt_hint or ch.chapter_title
    style = cfg.illustration_style_hint or (
        "clean line art with varied line weight and soft stipple or crosshatch "
        "texture, strong central subject, generous white space"
    )
    return (
        f"A completely wordless, text-free standalone book illustration for a "
        f"chapter about {ch.chapter_title} in the context of {cfg.topic}. "
        f"Subject: {hint}. "
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


def write_json(book: TriviaBook, path: Path) -> Path:
    """The JSON is the source of truth — it must allow re-formatting the book
    without regenerating any content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(book.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_json(path: Path) -> TriviaBook:
    """Rehydrate a book from its JSON so exports can be re-run."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg = BookConfig.from_dict(data.get("config") or data)
    book = TriviaBook(
        config=cfg,
        introduction=str(data.get("introduction") or ""),
        conclusion=str(data.get("conclusion") or ""),
        warnings=list(data.get("warnings") or []),
        usage=dict(data.get("usage") or {}),
    )

    for raw in data.get("chapters") or []:
        chapter = Chapter(
            number=int(raw.get("chapter_number") or 0),
            title=str(raw.get("chapter_title") or ""),
            scope=str(raw.get("chapter_scope") or ""),
            illustration_path=str(raw.get("illustration_path") or ""),
            illustration_prompt=str(raw.get("illustration_prompt") or ""),
        )
        for q in raw.get("trivia") or []:
            chapter.trivia.append(TriviaQuestion(
                id=str(q.get("id") or ""),
                chapter=chapter.number,
                question=str(q.get("question") or ""),
                choices=dict(q.get("choices") or {}),
                correct_answer=str(q.get("correct_answer") or ""),
                fact_seed=str(q.get("fact_seed") or ""),
            ))
        for f in raw.get("facts") or []:
            chapter.facts.append(DidYouKnowFact(
                id=str(f.get("id") or ""),
                chapter=chapter.number,
                fact=str(f.get("fact") or ""),
                fact_seed=str(f.get("fact_seed") or ""),
            ))
        book.chapters.append(chapter)

    return book
