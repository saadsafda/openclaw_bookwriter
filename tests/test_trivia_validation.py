"""The export gate.

validate_for_export is the last thing standing between a draft and a paid
export, so every rule it enforces needs a test: a rule that silently stops
working lets a malformed book reach KDP, and a rule that is too strict blocks
a book with no way for the operator to clear it.
"""

from __future__ import annotations

import pytest

from trivia import pipeline
from trivia.engine import BookConfig, ChapterConfig
from trivia.pipeline import TriviaBook


def _builder(book):
    b = pipeline.TriviaBuilder(book.config)
    b.book = book
    return b


@pytest.fixture
def book(make_chapter):
    def _make(questions=4, trivia_count=4, facts=2, fact_count=2):
        cfg = BookConfig(
            book_title="B", topic="t", illustrations=False,
            chapters=[ChapterConfig(chapter_number=1, chapter_title="C",
                                    chapter_scope="s",
                                    trivia_count=trivia_count,
                                    fact_count=fact_count)])
        bk = TriviaBook(config=cfg)
        bk.chapters.append(make_chapter(number=1, questions=questions,
                                        facts=facts))
        return bk

    return _make


class TestQuestionCount:
    def test_exact_match_passes(self, book):
        assert _builder(book()).validate_for_export() == []

    def test_one_short_is_blocked(self, book):
        errs = _builder(book(questions=3, trivia_count=4)).validate_for_export()
        assert any("3 questions, config requires 4" in e for e in errs), errs

    def test_zero_questions_is_blocked(self, book):
        """The exact message production showed."""
        errs = _builder(book(questions=0, trivia_count=15)).validate_for_export()
        assert any("0 questions, config requires 15" in e for e in errs), errs

    def test_over_quota_is_blocked(self, book):
        errs = _builder(book(questions=6, trivia_count=4)).validate_for_export()
        assert any("6 questions" in e for e in errs), errs


class TestFactCount:
    def test_slightly_under_is_tolerated(self, book):
        """Facts may run under quota when a scope is exhausted."""
        tol = pipeline.FACT_COUNT_TOLERANCE
        bk = book(facts=5 - tol, fact_count=5)
        assert _builder(bk).validate_for_export() == []

    def test_beyond_tolerance_is_blocked(self, book):
        tol = pipeline.FACT_COUNT_TOLERANCE
        bk = book(facts=5 - tol - 1, fact_count=5)
        errs = _builder(bk).validate_for_export()
        assert any("facts" in e for e in errs), errs

    def test_over_quota_is_blocked(self, book):
        errs = _builder(book(facts=6, fact_count=2)).validate_for_export()
        assert any("6 facts" in e for e in errs), errs


class TestQuestionShape:
    def test_missing_a_choice_is_blocked(self, book):
        bk = book()
        bk.chapters[0].trivia[0].choices.pop("D")
        errs = _builder(bk).validate_for_export()
        assert any("choices A-D" in e for e in errs), errs

    def test_answer_outside_abcd_is_blocked(self, book):
        bk = book()
        bk.chapters[0].trivia[0].correct_answer = "E"
        errs = _builder(bk).validate_for_export()
        assert any("correct_answer is not A-D" in e for e in errs), errs

    def test_answer_pointing_at_a_missing_choice_is_blocked(self, book):
        bk = book()
        q = bk.chapters[0].trivia[0]
        q.choices.pop("D")
        q.correct_answer = "D"
        errs = _builder(bk).validate_for_export()
        assert any("missing choice" in e for e in errs), errs


class TestAnswerDistribution:
    def test_clustered_answers_are_blocked(self, book):
        bk = book(questions=12, trivia_count=12)
        for q in bk.chapters[0].trivia:
            q.correct_answer = "A"
        errs = _builder(bk).validate_for_export()
        assert any("cluster" in e for e in errs), errs

    def test_even_spread_passes(self, book):
        bk = book(questions=12, trivia_count=12)
        for i, q in enumerate(bk.chapters[0].trivia):
            q.correct_answer = "ABCD"[i % 4]
        assert _builder(bk).validate_for_export() == []


class TestErrorReporting:
    def test_reports_every_failing_chapter(self, make_chapter):
        cfg = BookConfig(
            book_title="B", topic="t", illustrations=False,
            chapters=[ChapterConfig(chapter_number=i, chapter_title=f"C{i}",
                                    chapter_scope="s", trivia_count=3,
                                    fact_count=1) for i in range(1, 4)])
        bk = TriviaBook(config=cfg)
        for i in range(1, 4):
            bk.chapters.append(make_chapter(number=i, questions=1, facts=1))

        errs = _builder(bk).validate_for_export()
        for i in range(1, 4):
            assert any(f"Chapter {i}" in e for e in errs), (i, errs)
