"""Regressions for the trivia shortfall deadlock.

Every test here corresponds to a failure that reached production. The bug was
never one defect: a chapter that came up one question short discarded the
questions it had, aborted the rest of the book, and left the resolve path with
no way to either raise the supply or lower the requirement.
"""

from __future__ import annotations

import re

import pytest

from trivia import pipeline
from trivia.engine import BookConfig, ChapterConfig, ValidationGateError


def _sizes(calls):
    """Batch size requested by each trivia prompt, in order."""
    return [int(re.search(r"Write (\d+) ", c).group(1))
            for c in calls if "multiple-choice trivia questions" in c]


class TestBatchStarvation:
    """The deadlock: asking for exactly 1, then replaying it from cache.

    With batch_size = min(BATCH, need), a chapter one question short asked for
    a single question. When it collided, the exclusion list was unchanged, so
    the next prompt was byte-identical -- a cache hit that replayed the same
    rejected question until the round budget ran out.
    """

    def test_never_requests_a_batch_of_one(self, trivia_builder, trivia_cfg, make_chapter):
        cfg = trivia_cfg([ChapterConfig(chapter_number=1, chapter_title="C",
                                        chapter_scope="s", trivia_count=15,
                                        fact_count=1)])

        # A crowded book rejects the last question a chapter needs. Model that
        # exactly: once 14 are banked, every further candidate collides except
        # one specific "escape" seed the model only offers when it is asked for
        # more than one item. That is what the real failure looks like -- the
        # 15th slot is reachable only if the batch has room for alternatives.
        # A crowded book fills a chapter to one short, then rejects the first
        # candidate of every later batch. With batch_size = need that single
        # candidate IS the batch, so the round yields nothing and the next
        # prompt is identical -- a cache hit replaying the same rejection.
        def collide(parsed, prior, kind):
            # Round 1 lands 12. Round 2 must leave the chapter on exactly 14,
            # so one of its three candidates is rejected. From then on the
            # chapter needs 1, and every batch's first candidate collides --
            # which is fatal only if the batch has no second candidate.
            if len(prior) + len(parsed) <= 12:
                return set()
            return {q.id for q in parsed[:1]}

        b, calls, _ = trivia_builder(cfg, collide=collide)
        ch = make_chapter(number=1)
        b.book.chapters.append(ch)
        b.generate_chapter_trivia(cfg.chapters[0], ch)

        assert 1 not in _sizes(calls)[1:], (
            f"asked for a batch of 1, which leaves the model no alternative "
            f"when a question collides: {_sizes(calls)}")

    def test_never_resends_an_identical_prompt(self, trivia_builder, trivia_cfg, make_chapter):
        """An identical prompt is a cache hit, so it replays the rejection."""
        cfg = trivia_cfg([ChapterConfig(chapter_number=1, chapter_title="C",
                                        chapter_scope="s", trivia_count=15,
                                        fact_count=1)])

        # A crowded book fills a chapter to one short, then rejects the first
        # candidate of every later batch. With batch_size = need that single
        # candidate IS the batch, so the round yields nothing and the next
        # prompt is identical -- a cache hit replaying the same rejection.
        def collide(parsed, prior, kind):
            # Round 1 lands 12. Round 2 must leave the chapter on exactly 14,
            # so one of its three candidates is rejected. From then on the
            # chapter needs 1, and every batch's first candidate collides --
            # which is fatal only if the batch has no second candidate.
            if len(prior) + len(parsed) <= 12:
                return set()
            return {q.id for q in parsed[:1]}

        b, calls, _ = trivia_builder(cfg, collide=collide)
        ch = make_chapter(number=1)
        b.book.chapters.append(ch)
        b.generate_chapter_trivia(cfg.chapters[0], ch)

        assert len(calls) == len(set(calls)), (
            "the same prompt was sent twice; the raw-output cache would "
            "replay the identical rejected reply forever")

    def test_reaches_the_target_when_one_question_short(self, trivia_builder,
                                                        trivia_cfg, make_chapter):
        """The production case: 14 banked, 1 needed, and it must be gettable.

        Only the first candidate of the final batch is acceptable. With a
        batch of 1 that single candidate was the rejected one and the chapter
        stalled; with room for alternatives the chapter completes.
        """
        cfg = trivia_cfg([ChapterConfig(chapter_number=1, chapter_title="C",
                                        chapter_scope="s", trivia_count=15,
                                        fact_count=1)])

        def collide(parsed, prior, kind):
            # Everything lands until the chapter is one short; after that only
            # a later candidate in the batch is distinct enough to accept.
            if len(prior) < 14:
                return set()
            return {q.id for q in parsed[:1]}

        b, calls, _ = trivia_builder(cfg, collide=collide)
        ch = make_chapter(number=1)
        b.book.chapters.append(ch)
        b.generate_chapter_trivia(cfg.chapters[0], ch)

        assert len(ch.trivia) == 15, (
            "the chapter could not obtain its final question; a batch with no "
            "room for an alternative deadlocks on the rejected candidate")

    def test_rejected_seeds_reach_the_next_prompt(self, trivia_builder,
                                                  trivia_cfg, make_chapter):
        """Feeding rejects into the exclusion list is what breaks the loop.

        Over-requesting alone is not enough: if a rejected question's seed is
        not excluded, a later round can rebuild the identical prompt and the
        raw-output cache replays the same rejected reply. The seed of anything
        the gate refused must appear in the prompts that follow.
        """
        cfg = trivia_cfg([ChapterConfig(chapter_number=1, chapter_title="C",
                                        chapter_scope="s", trivia_count=15,
                                        fact_count=1)])
        rejected: list[str] = []

        # Reject every candidate for two rounds, so there is always a later
        # prompt in which the excluded seeds must appear.
        rounds = {"n": 0}

        def collide(parsed, prior, kind):
            rounds["n"] += 1
            if rounds["n"] > 2:
                return set()
            bad = {q.id for q in parsed}
            rejected.extend(q.fact_seed for q in parsed if q.id in bad)
            return bad

        b, calls, _ = trivia_builder(cfg, collide=collide)
        ch = make_chapter(number=1)
        b.book.chapters.append(ch)
        b.generate_chapter_trivia(cfg.chapters[0], ch)

        assert rejected, "the collision model never rejected anything"
        trivia_calls = [c for c in calls
                        if "multiple-choice trivia questions" in c]
        later = "\n".join(trivia_calls[1:])
        assert any(seed in later for seed in rejected), (
            "no rejected seed was excluded from a later prompt, so an "
            "identical prompt can recur and be served from cache")

    @pytest.mark.parametrize("target", [1, 3, 12, 15, 25, 50])
    def test_over_request_never_overshoots(self, trivia_builder, trivia_cfg,
                                           make_chapter, target):
        """Over-requesting must not deliver more than the configured count."""
        cfg = trivia_cfg([ChapterConfig(chapter_number=1, chapter_title="C",
                                        chapter_scope="s", trivia_count=target,
                                        fact_count=1)])
        b, _, _ = trivia_builder(cfg)
        ch = make_chapter(number=1)
        b.book.chapters.append(ch)
        b.generate_chapter_trivia(cfg.chapters[0], ch)

        assert len(ch.trivia) == target
        assert [q.id for q in ch.trivia] == [
            f"ch1_q{i:02d}" for i in range(1, target + 1)]


class TestPartialContentPreserved:
    """A genuine shortfall must keep what it generated.

    The original code raised without assigning chapter.trivia, so 14 paid-for
    questions were discarded and the saved draft showed 0 -- which is what made
    the failure unresolvable rather than merely inconvenient.
    """

    def test_keeps_questions_when_scope_is_exhausted(self, trivia_builder,
                                                     trivia_cfg, make_chapter):
        cfg = trivia_cfg([ChapterConfig(chapter_number=6, chapter_title="C",
                                        chapter_scope="s", trivia_count=15,
                                        fact_count=1)])
        # Only 14 distinct questions exist in this scope, ever.
        b, _, _ = trivia_builder(cfg, supply=14)
        ch = make_chapter(number=6)
        b.book.chapters.append(ch)

        with pytest.raises(ValidationGateError):
            b.generate_chapter_trivia(cfg.chapters[0], ch)

        assert len(ch.trivia) == 14, (
            "the questions generated before the shortfall were discarded; "
            "the saved draft would show 0 and could never be resolved")

    def test_kept_questions_are_numbered_and_valid(self, trivia_builder,
                                                   trivia_cfg, make_chapter):
        """Preserved content must be export-shaped, not a half-built list."""
        cfg = trivia_cfg([ChapterConfig(chapter_number=6, chapter_title="C",
                                        chapter_scope="s", trivia_count=15,
                                        fact_count=1)])
        b, _, _ = trivia_builder(cfg, supply=14)
        ch = make_chapter(number=6)
        b.book.chapters.append(ch)
        with pytest.raises(ValidationGateError):
            b.generate_chapter_trivia(cfg.chapters[0], ch)

        assert [q.id for q in ch.trivia] == [
            f"ch6_q{i:02d}" for i in range(1, 15)]
        for q in ch.trivia:
            assert set(q.choices) == {"A", "B", "C", "D"}
            assert q.correct_answer in q.choices


class TestBuildContinuesPastShortChapter:
    """One short chapter must not abandon the rest of the book.

    The build aborted at the first shortfall, so a 14-chapter book that stalled
    on chapter 6 never generated chapters 7-14 at all -- the saved draft was a
    stub, and "resolving" it would have meant rebuilding most of the book.
    """

    def test_all_chapters_exist_after_a_shortfall(self, trivia_builder,
                                                  trivia_cfg, tmp_path):
        chapters = [ChapterConfig(chapter_number=i, chapter_title=f"C{i}",
                                  chapter_scope=f"scope{i}", trivia_count=2,
                                  fact_count=1) for i in range(1, 6)]
        cfg = trivia_cfg(chapters)
        b, _, _ = trivia_builder(cfg)

        # Chapter 3's scope yields nothing.
        real = b._generate_batch

        def starved(prompt):
            return [] if "scope3" in prompt else real(prompt)

        b._generate_batch = starved

        with pytest.raises(ValidationGateError):
            b.build(tmp_path)

        assert len(b.book.chapters) == 5, (
            "the build aborted at the starved chapter; later chapters were "
            "never generated")
        healthy = {c.number: len(c.trivia) for c in b.book.chapters
                   if c.number != 3}
        assert all(n == 2 for n in healthy.values()), healthy

    def test_error_names_every_short_chapter(self, trivia_builder, trivia_cfg, tmp_path):
        """The operator needs all the failures at once, not the first one."""
        chapters = [ChapterConfig(chapter_number=i, chapter_title=f"C{i}",
                                  chapter_scope=f"scope{i}", trivia_count=2,
                                  fact_count=1) for i in range(1, 6)]
        cfg = trivia_cfg(chapters)
        b, _, _ = trivia_builder(cfg)
        real = b._generate_batch

        def starved(prompt):
            if "scope2" in prompt or "scope4" in prompt:
                return []
            return real(prompt)

        b._generate_batch = starved

        with pytest.raises(ValidationGateError) as exc:
            b.build(tmp_path)

        msg = str(exc.value)
        assert "Chapter 2" in msg and "Chapter 4" in msg, msg
