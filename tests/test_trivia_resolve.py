"""Regressions for the resolve path.

Resolve exists so a book blocked by the export gate can be finished without
paying to rebuild it. The failure it was built for -- a chapter short on
questions -- was the one case it could not handle: it could neither generate
more questions nor accept a lowered requirement, so the button did nothing no
matter how many times it was pressed.
"""

from __future__ import annotations

import pytest
from flask import Flask

from trivia import pipeline, routes
from trivia.engine import BookConfig, ChapterConfig, TriviaError
from trivia.pipeline import TriviaBook


@pytest.fixture(scope="module")
def overrides_fn():
    """_apply_config_overrides is closure-scoped inside register()."""
    app = Flask(__name__)
    routes.register(app)
    view = app.view_functions["trivia_resolve"]
    for cell in view.__closure__ or []:
        val = cell.cell_contents
        if callable(val) and getattr(val, "__name__", "") == "_apply_config_overrides":
            return val
    pytest.fail("_apply_config_overrides not found on the resolve view")


@pytest.fixture
def blocked_book(make_chapter):
    """A draft blocked exactly the way production was: 14 of 15 questions."""
    def _make(questions=14, trivia_count=15, facts=2, fact_count=2, number=6):
        cfg = BookConfig(
            book_title="Blocked", topic="t", illustrations=False,
            chapters=[ChapterConfig(chapter_number=number, chapter_title="C",
                                    chapter_scope="s", trivia_count=trivia_count,
                                    fact_count=fact_count)],
        )
        book = TriviaBook(config=cfg)
        book.chapters.append(make_chapter(number=number, questions=questions,
                                          facts=facts))
        return book

    return _make


class TestTriviaCountOverride:
    """Lowering trivia_count is the escape hatch that did not exist.

    It was deliberately ignored, on the theory that questions are never
    auto-dropped. But the export gate demands an exact match, so a chapter
    short on questions could neither be filled nor accepted -- the operator
    typed 14, the server discarded it, and nothing happened.
    """

    def test_lowering_to_the_actual_count_clears_the_gate(self, overrides_fn,
                                                          blocked_book):
        book = blocked_book()
        notes = overrides_fn(book, {"chapters": [
            {"chapter_number": 6, "trivia_count": 14, "fact_count": 2}]})

        assert book.config.chapters[0].trivia_count == 14, (
            "the trivia_count override was ignored, so pressing Resolve after "
            "editing the field does nothing")
        assert any("trivia_count 15 -> 14" in n for n in notes), notes

        b = pipeline.TriviaBuilder(book.config)
        b.book = book
        assert b.validate_for_export() == []

    def test_lowering_below_current_trims_the_draft(self, overrides_fn, blocked_book):
        """Config alone is not enough: over-quota questions are a hard error."""
        book = blocked_book()
        overrides_fn(book, {"chapters": [
            {"chapter_number": 6, "trivia_count": 10, "fact_count": 2}]})

        assert len(book.chapters[0].trivia) == 10
        b = pipeline.TriviaBuilder(book.config)
        b.book = book
        assert b.validate_for_export() == []

    def test_trim_renumbers_questions_contiguously(self, overrides_fn, blocked_book):
        book = blocked_book()
        overrides_fn(book, {"chapters": [
            {"chapter_number": 6, "trivia_count": 10, "fact_count": 2}]})
        assert [q.id for q in book.chapters[0].trivia] == [
            f"ch6_q{i:02d}" for i in range(1, 11)]

    def test_trim_keeps_answers_spread_across_letters(self, overrides_fn,
                                                      make_chapter):
        """validate_for_export also fails a chapter whose answers cluster.

        Cutting the tail can skew the spread, so a trim that ignored this would
        swap one blocking error for another.
        """
        cfg = BookConfig(
            book_title="B", topic="t", illustrations=False,
            chapters=[ChapterConfig(chapter_number=1, chapter_title="C",
                                    chapter_scope="s", trivia_count=20,
                                    fact_count=0)])
        book = TriviaBook(config=cfg)
        ch = make_chapter(number=1, questions=20)
        # Force every answer in the surviving head onto one letter.
        for q in ch.trivia[:10]:
            q.correct_answer = "A"
        book.chapters.append(ch)

        overrides_fn(book, {"chapters": [
            {"chapter_number": 1, "trivia_count": 10}]})

        b = pipeline.TriviaBuilder(book.config)
        b.book = book
        assert b.validate_for_export() == [], (
            "trimming left the correct answers clustered on one letter, which "
            "the export gate rejects")

    def test_raising_is_left_to_the_top_up_pass(self, overrides_fn, blocked_book):
        """Raising must not fabricate questions here, only record the intent."""
        book = blocked_book()
        overrides_fn(book, {"chapters": [
            {"chapter_number": 6, "trivia_count": 20}]})
        assert book.config.chapters[0].trivia_count == 20
        assert len(book.chapters[0].trivia) == 14


class TestOverrideValidation:
    def test_rejects_non_numeric(self, overrides_fn, blocked_book):
        book = blocked_book()
        with pytest.raises(TriviaError, match="whole number"):
            overrides_fn(book, {"chapters": [
                {"chapter_number": 6, "trivia_count": "many"}]})

    def test_rejects_negative(self, overrides_fn, blocked_book):
        book = blocked_book()
        with pytest.raises(TriviaError, match="negative"):
            overrides_fn(book, {"chapters": [
                {"chapter_number": 6, "trivia_count": -1}]})

    def test_matches_chapters_by_number_not_position(self, overrides_fn, make_chapter):
        """Reordering rows in the browser must not retarget an edit."""
        cfg = BookConfig(
            book_title="B", topic="t", illustrations=False,
            chapters=[
                ChapterConfig(chapter_number=1, chapter_title="A",
                              chapter_scope="a", trivia_count=5, fact_count=1),
                ChapterConfig(chapter_number=2, chapter_title="B",
                              chapter_scope="b", trivia_count=5, fact_count=1),
            ])
        book = TriviaBook(config=cfg)
        book.chapters.append(make_chapter(number=1, questions=5, facts=1))
        book.chapters.append(make_chapter(number=2, questions=5, facts=1))

        # Rows arrive in reverse order.
        overrides_fn(book, {"chapters": [
            {"chapter_number": 2, "trivia_count": 3},
            {"chapter_number": 1, "trivia_count": 4},
        ]})
        assert cfg.chapters[0].trivia_count == 4
        assert cfg.chapters[1].trivia_count == 3

    def test_empty_payload_is_a_no_op(self, overrides_fn, blocked_book):
        book = blocked_book()
        assert overrides_fn(book, None) == []
        assert book.config.chapters[0].trivia_count == 15

    def test_blank_fields_are_ignored(self, overrides_fn, blocked_book):
        """A cleared browser input must not be read as zero."""
        book = blocked_book()
        overrides_fn(book, {"chapters": [
            {"chapter_number": 6, "trivia_count": "", "fact_count": None}]})
        assert book.config.chapters[0].trivia_count == 15
        assert book.config.chapters[0].fact_count == 2


class TestTriviaTopUp:
    """The other escape hatch: generate the missing questions."""

    def test_fills_a_short_chapter(self, trivia_builder, blocked_book):
        book = blocked_book()
        b, _, _ = trivia_builder(book.config)
        b.book = book

        notes = b.top_up_short_trivia()
        assert len(book.chapters[0].trivia) == 15
        assert notes == []

    def test_keeps_the_questions_already_paid_for(self, trivia_builder, blocked_book):
        """Top-up appends; it must never regenerate the whole chapter."""
        book = blocked_book()
        original = [q.id for q in book.chapters[0].trivia]
        original_text = [q.question for q in book.chapters[0].trivia]
        b, _, _ = trivia_builder(book.config)
        b.book = book
        b.top_up_short_trivia()

        kept = [q.question for q in book.chapters[0].trivia[:len(original)]]
        assert kept == original_text, "existing questions were replaced, not kept"
        assert [q.id for q in book.chapters[0].trivia][:len(original)] == original

    def test_reports_a_chapter_it_cannot_fill(self, trivia_builder, blocked_book):
        book = blocked_book()
        b, _, _ = trivia_builder(book.config, supply=0)
        b.book = book
        notes = b.top_up_short_trivia()
        assert notes and "14 of 15" in notes[0], notes

    def test_does_nothing_when_already_full(self, trivia_builder, blocked_book):
        book = blocked_book(questions=15, trivia_count=15)
        b, calls, _ = trivia_builder(book.config)
        b.book = book
        assert b.top_up_short_trivia() == []
        assert calls == [], "spent a call on a chapter that was already full"

    def test_skips_chapters_configured_for_no_questions(self, trivia_builder,
                                                        blocked_book):
        book = blocked_book(questions=0, trivia_count=0)
        b, calls, _ = trivia_builder(book.config)
        b.book = book
        assert b.top_up_short_trivia() == []
        assert calls == []
