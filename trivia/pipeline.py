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
        rounds = 0
        upstream_failures = 0

        while len(accepted) < target and rounds < MAX_REFILL_ROUNDS + target // engine.TRIVIA_BATCH:
            self._check_stop()
            rounds += 1
            need = target - len(accepted)
            batch_size = min(engine.TRIVIA_BATCH, need)

            # Exclusion context: seeds used anywhere in the book so far, plus
            # this chapter's accepted questions.
            avoid = [q.fact_seed for q in self._all_trivia() if q.fact_seed]
            avoid += [q.fact_seed for q in accepted if q.fact_seed]

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
                    "or trivia_count. Check the batch errors above."
                )
            elif upstream_failures:
                hint = (
                    f"{upstream_failures} of {rounds} attempts failed upstream; the "
                    "rest were rejected by validation or the dedup gate. Retry, and "
                    "if it persists widen the chapter scope or lower trivia_count."
                )
            else:
                hint = (
                    "Attempts succeeded but the content was rejected as duplicate or "
                    "invalid. Widen the chapter scope or lower trivia_count."
                )
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

        while len(accepted) < target and rounds < MAX_REFILL_ROUNDS + target // engine.FACT_BATCH:
            self._check_stop()
            rounds += 1
            need = target - len(accepted)
            batch_size = min(engine.FACT_BATCH, need)

            # Section 6 step 3 — every trivia claim in the book is excluded,
            # expressed as readable claims rather than bare slugs so the model
            # can actually reason about what to avoid.
            exclusions = [q.claim_text() for q in chapter.trivia]
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
                self.log(f"  batch failed ({exc}); retrying")
                continue

            parsed, rejects = engine.parse_fact_items(raw, ch_cfg.chapter_number)
            for reason in rejects[:5]:
                self.log(f"  rejected: {reason}")

            for i, item in enumerate(parsed):
                item.id = f"ch{ch_cfg.chapter_number}_f{len(accepted) + i + 1:03d}"

            parsed = engine.dedup_within(parsed)

            # Gate 1: facts must not restate trivia in this chapter.
            against_trivia = self.checker.find_collisions(
                parsed, chapter.trivia, "fact_vs_trivia"
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

        if len(accepted) < target:
            if upstream_failures == rounds:
                hint = (
                    "Every attempt failed before any content was generated, so this "
                    "is an AI service problem, not a problem with your chapter scope "
                    "or fact_count. Check the batch errors above."
                )
            elif upstream_failures:
                hint = (
                    f"{upstream_failures} of {rounds} attempts failed upstream; the "
                    "no-overlap gate rejected the rest. Retry, and if it persists "
                    "widen the scope or lower fact_count."
                )
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
            if len(chapter.facts) != ch_cfg.fact_count:
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

        total_steps = max(1, len(self.cfg.chapters) * 2 + 3)
        step = 0

        for ch_cfg in self.cfg.chapters:
            self._check_stop()
            chapter = Chapter(
                number=ch_cfg.chapter_number,
                title=ch_cfg.chapter_title,
                scope=ch_cfg.chapter_scope,
            )
            self.book.chapters.append(chapter)

            self.generate_chapter_trivia(ch_cfg, chapter)
            step += 1
            self.progress("trivia", step / total_steps)

            self.generate_chapter_facts(ch_cfg, chapter)
            step += 1
            self.progress("facts", step / total_steps)

        self._check_stop()
        collisions = self.global_dedup_pass()
        self.collisions = collisions
        self.resolve_collisions(collisions)

        # Re-check after regeneration; anything surviving blocks export.
        remaining = self.global_dedup_pass()
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
                f"content. {detail}"
            )

        self._check_stop()
        self.generate_illustrations(out_dir)
        step += 1
        self.progress("illustrations", step / total_steps)

        errors = self.validate_for_export()
        if errors:
            raise ValidationGateError(
                "Export blocked by validation:\n- " + "\n- ".join(errors[:12])
            )

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
