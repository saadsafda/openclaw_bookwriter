"""Trivia KDP output must stay body text, not headings.

The KDP formatter identifies headings by shape, which is right for prose but
wrong for trivia: a numbered question reads as a subheading, a short answer
choice reads as an outline topic, and a choice lettered C or D reads as a
roman-numeral chapter title. Before the fix every question, every choice and
every fact was promoted to a 20-24pt bold heading and listed in the TOC.
"""

from __future__ import annotations

import pytest
from docx import Document

from kdp_docx_formatter import BODY_TEXT_STYLE
from trivia import export as ex
from trivia.engine import (
    BookConfig,
    Chapter,
    ChapterConfig,
    DidYouKnowFact,
    TriviaBook,
    TriviaQuestion,
)

BODY_SIZE_PT = {"kindle": 11.5, "paperback": 11.0}


def _book() -> TriviaBook:
    cfg = BookConfig(
        book_title="Bird Trivia",
        topic="birds",
        chapters=[
            ChapterConfig.from_dict({"chapter_title": "Owls", "chapter_scope": "owls"}, 1)
        ],
    )
    questions = [
        TriviaQuestion(
            "q1", 1,
            "Which owl species is the largest by wingspan in North America?",
            {"A": "Great Horned Owl", "B": "Great Gray Owl",
             "C": "Barn Owl", "D": "Snowy Owl"},
            "B",
        ),
        TriviaQuestion(
            "q2", 1, "How many neck vertebrae does an owl have?",
            {"A": "7", "B": "10", "C": "14", "D": "21"}, "C",
        ),
    ]
    facts = [DidYouKnowFact("f1", 1, "Owls cannot move their eyeballs in their sockets.")]
    return TriviaBook(
        config=cfg,
        chapters=[Chapter(1, "Owls", "owls", trivia=questions, facts=facts)],
    )


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict[str, str]:
    out = tmp_path_factory.mktemp("trivia_kdp")
    book = _book()
    source = out / "bird.docx"
    ex.build_docx(book, source)
    return ex.build_kdp_files(book, source, out)


def _paragraphs(built, variant):
    return Document(built[variant]).paragraphs


def _find(built, variant, prefix):
    for p in _paragraphs(built, variant):
        if p.text.strip().startswith(prefix):
            return p
    raise AssertionError(f"no paragraph starting {prefix!r} in {variant}")


@pytest.mark.parametrize("variant", ["kindle", "paperback"])
class TestTriviaStaysBodyText:
    def test_questions_are_not_headings(self, built, variant):
        q = _find(built, variant, "1. Which owl")
        assert q.style.name == BODY_TEXT_STYLE

    def test_choices_are_not_headings(self, built, variant):
        # "C." and "D." are roman numerals and were promoted to Heading 1.
        for prefix in ("A. Great Horned", "B. Great Gray", "C. Barn Owl", "D. Snowy Owl"):
            assert _find(built, variant, prefix).style.name == BODY_TEXT_STYLE

    def test_facts_are_not_headings(self, built, variant):
        assert _find(built, variant, "• Owls cannot").style.name == BODY_TEXT_STYLE

    def test_question_text_is_body_size(self, built, variant):
        q = _find(built, variant, "1. Which owl")
        for run in q.runs:
            assert run.font.size.pt == BODY_SIZE_PT[variant]

    def test_only_the_number_is_bold(self, built, variant):
        """The whole question in bold is what made the page look like a wall."""
        q = _find(built, variant, "1. Which owl")
        bolded = "".join(r.text for r in q.runs if r.bold)
        assert bolded.strip() == "1."

    def test_toc_holds_only_real_headings(self, built, variant):
        headings = {
            p.text.strip()
            for p in _paragraphs(built, variant)
            if (p.style.name or "").lower().startswith("heading") and p.text.strip()
        }
        assert "Chapter 1 — Owls" in headings
        assert "Trivia" in headings
        for leaked in ("C. Barn Owl", "D. Snowy Owl", "A. 7"):
            assert leaked not in headings
        assert not any(h.startswith("1. Which owl") for h in headings)

    def test_answer_key_chapter_is_a_subheading(self, built, variant):
        """Answer-key sections must not become top-level chapters in the TOC."""
        for p in _paragraphs(built, variant):
            if p.text.strip() == "Owls (Chapter 1)":
                assert p.style.name == "Heading 2"
                return
        raise AssertionError("answer key chapter heading missing")
