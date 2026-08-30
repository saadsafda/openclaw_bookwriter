"""One-sentence paragraphs must not ship in Book Writer or Stories output.

A single sentence standing alone as its own paragraph reads as a pull quote on
a printed 6x9 page rather than as prose. The generators used to be *instructed*
to produce them ("or even one full sentence standing alone"), and nothing
checked for them, so a page could carry several.

A section's closing paragraph is the deliberate exception.
"""

from __future__ import annotations

import pytest

from openclaw_docx_writer import (
    find_lone_sentence_paragraphs,
    merge_lone_sentence_paragraphs,
)
from openclaw_docx_writer import split_sentences as dw_split
from stories import engine as se


# The passage from the client's screenshot.
SCREENSHOT = (
    "The dog wore goggles because the road across America could blind a "
    "passenger with dust.\n\n"
    "Bud was a pit bull picked up in Idaho in 1903, partway through Horatio "
    "Nelson Jackson's attempt to drive from San Francisco to New York. Jackson "
    "and his mechanic were making the first automobile crossing.\n\n"
    "Into that adventure came Bud.\n\n"
    "He became the car's third traveler, riding with the men as the journey "
    "pushed east. The alkali dust was hard on everyone.\n\n"
    "By the time the car reached New York, Bud had become one of the earliest "
    "great American road dogs. He simply rode along."
)


class TestSentenceSplitting:
    """Miscounting sentences would let real offenders through."""

    @pytest.mark.parametrize("splitter", [dw_split, se.split_sentences])
    def test_counts_plain_sentences(self, splitter):
        assert len(splitter("One thing happened. Then another did.")) == 2

    @pytest.mark.parametrize("splitter", [dw_split, se.split_sentences])
    def test_abbreviation_is_not_a_sentence_end(self, splitter):
        assert len(splitter("He joined the U.S. Army in 1903.")) == 1

    @pytest.mark.parametrize("splitter", [dw_split, se.split_sentences])
    def test_initial_is_not_a_sentence_end(self, splitter):
        assert len(splitter("Horatio N. Jackson drove west.")) == 1

    @pytest.mark.parametrize("splitter", [dw_split, se.split_sentences])
    def test_title_is_not_a_sentence_end(self, splitter):
        assert len(splitter("Dr. Smith went along.")) == 1

    @pytest.mark.parametrize("splitter", [dw_split, se.split_sentences])
    def test_question_and_exclamation_split(self, splitter):
        assert len(splitter("Was it hard? It was. They kept going!")) == 3


class TestDetection:
    def test_finds_the_screenshot_offenders(self):
        flagged = find_lone_sentence_paragraphs(SCREENSHOT)
        assert len(flagged) == 2
        assert flagged[0].startswith("The dog wore goggles")
        assert flagged[1] == "Into that adventure came Bud."

    def test_stories_finds_the_same_two(self):
        assert se.lone_sentence_paragraphs(SCREENSHOT) == [0, 2]

    def test_closing_paragraph_may_stand_alone(self):
        text = ("A real paragraph here. It has two sentences.\n\n"
                "And this one lands the point.")
        assert find_lone_sentence_paragraphs(text) == []
        assert se.lone_sentence_paragraphs(text) == []

    def test_long_single_sentence_is_exempt(self):
        """A sentence that fills several printed lines is not a stranded beat."""
        long_one = " ".join(["word"] * 40) + "."
        text = f"{long_one}\n\nSecond paragraph here. It has two sentences.\n\nEnd."
        assert find_lone_sentence_paragraphs(text) == []

    def test_clean_prose_is_not_flagged(self):
        text = ("First paragraph with two sentences. Here is the second.\n\n"
                "Second paragraph also has two. And here is its second.\n\n"
                "A closing line.")
        assert find_lone_sentence_paragraphs(text) == []


class TestMechanicalMerge:
    def test_merges_forward_into_the_next_paragraph(self):
        """A lone sentence introduces what follows, so it merges forward."""
        out = merge_lone_sentence_paragraphs(SCREENSHOT)
        assert "Into that adventure came Bud. He became the car's" in out

    def test_removes_every_offender(self):
        assert find_lone_sentence_paragraphs(
            merge_lone_sentence_paragraphs(SCREENSHOT)
        ) == []

    def test_preserves_the_closing_paragraph(self):
        out = merge_lone_sentence_paragraphs(SCREENSHOT)
        assert out.split("\n\n")[-1].startswith("By the time the car reached")

    def test_loses_no_words(self):
        before = SCREENSHOT.split()
        after = merge_lone_sentence_paragraphs(SCREENSHOT).split()
        assert before == after

    def test_is_idempotent(self):
        once = merge_lone_sentence_paragraphs(SCREENSHOT)
        assert merge_lone_sentence_paragraphs(once) == once

    def test_clean_text_is_untouched(self):
        text = ("First paragraph here. It has two sentences.\n\n"
                "Second paragraph too. Also two here.\n\n"
                "A closing beat.")
        assert merge_lone_sentence_paragraphs(text) == text

    def test_consecutive_lone_sentences_all_merge(self):
        text = ("One sentence here.\n\nAnother one here.\n\n"
                "A real paragraph now. With a second sentence.\n\nThe end.")
        out = merge_lone_sentence_paragraphs(text)
        assert find_lone_sentence_paragraphs(out) == []
        assert out.split("\n\n")[0].startswith("One sentence here. Another one here.")

    def test_single_paragraph_input_is_safe(self):
        assert merge_lone_sentence_paragraphs("Just one line.") == "Just one line."

    def test_empty_input_is_safe(self):
        assert merge_lone_sentence_paragraphs("") == ""


class TestStoriesQualityGate:
    """The stories pipeline rejects and regenerates rather than merging."""

    def _story_and_cfg(self, body: str):
        cfg = se.BookConfig(book_title="B", topic="t")
        st = se.StoryConfig.from_dict({"title": "Bud Across America"}, 1)
        story = se.Story(
            id="s1", number=1, chapter=1, title="Bud Across America", body=body,
        )
        return story, cfg, st

    def test_lone_sentence_paragraph_is_rejected(self):
        body = ("Into that adventure came Bud.\n\n"
                + " ".join(["word"] * 320) + ". And a second sentence here.\n\n"
                "A closing beat that lands.")
        story, cfg, st = self._story_and_cfg(body)
        problems = se.check_story_quality(story, cfg, st)
        assert any("one-sentence paragraph" in p for p in problems), problems

    def test_clean_body_passes_the_paragraph_check(self):
        body = (" ".join(["word"] * 200) + ". Second sentence here.\n\n"
                + " ".join(["word"] * 180) + ". Another second sentence.\n\n"
                "A closing beat.")
        story, cfg, st = self._story_and_cfg(body)
        problems = se.check_story_quality(story, cfg, st)
        assert not any("one-sentence paragraph" in p for p in problems), problems


class TestPromptsNoLongerAskForThem:
    """The generators were being *instructed* to produce these."""

    def test_book_writer_prompt_forbids_them(self):
        """It used to say "or even one full sentence standing alone"."""
        import openclaw_docx_writer as dw
        block = dw._paragraph_break_block(250, 320)
        assert "one full sentence standing alone" not in block
        assert "AT LEAST TWO complete sentences" in block

    def test_soul_md_forbids_them(self):
        from pathlib import Path
        soul = Path(__file__).resolve().parent.parent / "SOUL.md"
        text = soul.read_text(encoding="utf-8")
        assert "or one complete sentence standing alone" not in text
        assert "at least two complete sentences" in text.lower()

    def test_stories_prompt_requires_two_sentences(self):
        assert se.MIN_SENTENCES_PER_PARAGRAPH == 2
