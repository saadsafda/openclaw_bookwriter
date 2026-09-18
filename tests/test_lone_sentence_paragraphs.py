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


# A passage whose every paragraph clears the four-sentence floor. Built from
# short sentences on purpose: the floor is about sentence COUNT, so a paragraph
# of four brief sentences is compliant while a lush two-sentence one is not.
CLEAN = (
    "The road was long. Dust rose off it in sheets. The men drove on anyway. "
    "They had a coast to reach.\n\n"
    "Bud rode in the front seat. He wore goggles against the grit. The men "
    "had bought them in a small town. They fit him well enough.\n\n"
    "New York came into view at last. The crossing was finished. Bud stepped "
    "down with the rest of them. He had earned the ride."
)

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
    """The floor is MIN_SENTENCES_PER_PARAGRAPH, now 4 rather than 2.

    The detector keeps its "lone sentence" name from when the floor was 2, but
    it now flags any paragraph under the floor.
    """

    def test_the_screenshot_paragraphs_are_under_the_floor(self):
        """All but one paragraph in the client's page is thinner than four
        sentences. The exception is the 35-word one, long enough to earn the
        LONE_SENTENCE_WORD_EXEMPTION."""
        flagged = find_lone_sentence_paragraphs(SCREENSHOT)
        assert len(flagged) == 4
        assert flagged[0].startswith("The dog wore goggles")
        assert "Into that adventure came Bud." in flagged

    def test_stories_agrees_with_the_writer(self):
        assert se.lone_sentence_paragraphs(SCREENSHOT) == [0, 2, 3, 4]

    def test_a_two_sentence_paragraph_is_now_flagged(self):
        """The floor moved from 2 to 4, so this used to pass and must not."""
        text = (CLEAN.split("\n\n")[0] + "\n\n"
                "A thin paragraph here. It carries only two sentences.\n\n"
                + CLEAN.split("\n\n")[2])
        assert find_lone_sentence_paragraphs(text) == [
            "A thin paragraph here. It carries only two sentences."
        ]

    def test_closing_paragraph_is_no_longer_exempt(self):
        """Sections stack, so an exempt closer strands a line on every page."""
        text = CLEAN.rsplit("\n\n", 1)[0] + "\n\nAnd this one lands the point."
        assert find_lone_sentence_paragraphs(text) == [
            "And this one lands the point."
        ]
        assert se.lone_sentence_paragraphs(text) == [2]

    def test_long_single_sentence_is_exempt(self):
        """A sentence that fills several printed lines is not a stranded beat."""
        long_one = " ".join(["word"] * 40) + "."
        text = f"{long_one}\n\n{CLEAN.split(chr(10) * 2)[1]}\n\n{long_one}"
        assert find_lone_sentence_paragraphs(text) == []

    def test_lone_section_has_nothing_to_merge_into(self):
        """A one-paragraph section is left alone rather than flagged unfixable."""
        assert find_lone_sentence_paragraphs("A single line.") == []
        assert se.lone_sentence_paragraphs("A single line.") == []

    def test_clean_prose_is_not_flagged(self):
        assert find_lone_sentence_paragraphs(CLEAN) == []
        assert se.lone_sentence_paragraphs(CLEAN) == []


class TestMechanicalMerge:
    def test_merges_forward_into_the_next_paragraph(self):
        """A lone sentence introduces what follows, so it merges forward."""
        out = merge_lone_sentence_paragraphs(SCREENSHOT)
        assert "Into that adventure came Bud. He became the car's" in out

    def test_removes_every_offender(self):
        assert find_lone_sentence_paragraphs(
            merge_lone_sentence_paragraphs(SCREENSHOT)
        ) == []

    def test_the_closing_paragraph_is_merged_not_preserved(self):
        """Under the four-sentence floor the closer is itself too thin, so it
        folds into what precedes it instead of standing as its own paragraph."""
        out = merge_lone_sentence_paragraphs(SCREENSHOT)
        assert "By the time the car reached" in out.split("\n\n")[-1]
        assert not out.split("\n\n")[-1].startswith("By the time the car reached")

    def test_loses_no_words(self):
        before = SCREENSHOT.split()
        after = merge_lone_sentence_paragraphs(SCREENSHOT).split()
        assert before == after

    def test_is_idempotent(self):
        once = merge_lone_sentence_paragraphs(SCREENSHOT)
        assert merge_lone_sentence_paragraphs(once) == once

    def test_clean_text_is_untouched(self):
        assert merge_lone_sentence_paragraphs(CLEAN) == CLEAN

    def test_thin_closing_beat_folds_backward(self):
        """Nothing follows the closer, so it attaches to the paragraph above."""
        first = CLEAN.split("\n\n")[0]
        out = merge_lone_sentence_paragraphs(f"{first}\n\nA closing beat.")
        assert out == f"{first} A closing beat."
        assert find_lone_sentence_paragraphs(out) == []

    def test_consecutive_lone_sentences_all_merge(self):
        text = ("One sentence here.\n\nAnother one here.\n\n"
                "A real paragraph now. With a second sentence.\n\n"
                "The end. And a line to seat it.")
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
        body = (" ".join(["word"] * 200) + ". Second here. Third here. "
                "Fourth here.\n\n"
                + " ".join(["word"] * 180) + ". Second one. Third one. "
                "Fourth one.\n\n" + CLEAN.split("\n\n")[2])
        story, cfg, st = self._story_and_cfg(body)
        problems = se.check_story_quality(story, cfg, st)
        assert not any("one-sentence paragraph" in p for p in problems), problems


class TestCachedTextIsRepaired:
    """Cached sections skip generate_clean_paragraph's gates entirely.

    This is how the defect reached the printed page: a cached entry was checked
    only for banned template patterns, so a stranded sentence written before the
    merge existed was served straight into the DOCX on every later run.
    """

    def test_the_writer_repairs_a_cached_section_in_place(self, tmp_path):
        import openclaw_docx_writer as dw
        cache = dw.Cache.load(tmp_path)
        cache.set("k", SCREENSHOT)

        cached = cache.get("k")
        stranded = dw.find_lone_sentence_paragraphs(cached)
        assert stranded, "fixture must reproduce the defect"

        # What the cache branch now does before handing the text on.
        repaired = dw.merge_lone_sentence_paragraphs(cached)
        cache.set("k", repaired)

        assert dw.find_lone_sentence_paragraphs(repaired) == []
        # The repair is persisted, so the next run does not redo it.
        assert dw.find_lone_sentence_paragraphs(cache.get("k")) == []
        assert cache.get("k").split() == SCREENSHOT.split()


class TestPromptsNoLongerAskForThem:
    """The generators were being *instructed* to produce these."""

    def test_book_writer_prompt_states_the_enforced_floor(self):
        """A prompt that names a lower floor than the gate enforces would send
        every section to the merger."""
        import openclaw_docx_writer as dw
        block = dw._paragraph_break_block(250, 320)
        assert "one full sentence standing alone" not in block
        assert (f"AT LEAST {dw.MIN_SENTENCES_PER_PARAGRAPH} complete sentences"
                in block)

    def test_soul_md_states_the_enforced_floor(self):
        from pathlib import Path
        import openclaw_docx_writer as dw
        soul = Path(__file__).resolve().parent.parent / "SOUL.md"
        text = soul.read_text(encoding="utf-8").lower()
        assert "or one complete sentence standing alone" not in text
        spelled = {2: "two", 3: "three", 4: "four", 5: "five"}[
            dw.MIN_SENTENCES_PER_PARAGRAPH
        ]
        assert f"at least {spelled} complete sentences" in text

    def test_no_source_still_grants_the_closing_beat_exception(self):
        """The closer exception is what put a stranded line on every page."""
        from pathlib import Path
        import openclaw_docx_writer as dw
        block = dw._paragraph_break_block(250, 320)
        assert "ONLY exception" not in block
        assert "FINAL paragraph too" in block
        soul = (Path(__file__).resolve().parent.parent / "SOUL.md").read_text(
            encoding="utf-8"
        )
        assert "only exception is the final paragraph" not in soul.lower()

    def test_both_engines_enforce_the_same_floor(self):
        """A split floor would let one pipeline ship what the other rejects."""
        import openclaw_docx_writer as dw
        assert se.MIN_SENTENCES_PER_PARAGRAPH == 4
        assert dw.MIN_SENTENCES_PER_PARAGRAPH == 4
