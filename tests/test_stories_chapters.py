"""Chapters must survive the trip from outline to formatted book.

An outline for one of these books is two levels deep: a handful of chapters
("Bank Jobs Gone Wrong") with tens of stories under each. The parser always
produced that shape, and the exporter always consumed it, but the browser sat
between them and sent only the flattened `stories` list — so every book arrived
at the engine as one untitled chapter. The chapter titles vanished, the story
titles were promoted to Heading 1, and the per-chapter illustration had nothing
to attach to.

These tests pin the contract the editor now has to honour: chapters carry their
titles through config parsing, stories stay one heading level below them, and
illustrations are made per chapter rather than per story.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stories import engine as se
from stories import outline as so


OUTLINE = """World's Dumbest Criminals
True stories of plans that fell apart

Chapter 1: Bank Jobs Gone Wrong

1. The $1 Bank Robbery
Who: James Verone
Year: 2011
The Story: He handed the teller a note demanding one dollar.

2. The Note on the Deposit Slip
Year: 2005
The Story: He wrote the demand on his own deposit slip.

Chapter 2: Criminals Who Called the Police

1. The Complaint About Short Weight
Year: 2009
The Story: He called police to report his dealer had shorted him.
"""


@pytest.fixture
def parsed() -> dict:
    return so.parse_outline_text(OUTLINE)


class TestOutlineParsing:
    """The parser is the source of the structure the UI must not discard."""

    def test_finds_both_chapters(self, parsed):
        assert parsed["chapter_count"] == 2

    def test_keeps_chapter_titles(self, parsed):
        titles = [c["chapter_title"] for c in parsed["chapters"]]
        assert titles == ["Bank Jobs Gone Wrong", "Criminals Who Called the Police"]

    def test_groups_stories_under_their_chapter(self, parsed):
        counts = [len(c["stories"]) for c in parsed["chapters"]]
        assert counts == [2, 1]

    def test_renumbers_continuously_across_chapters(self, parsed):
        """The source restarts at 1 per chapter; numbers are book-wide ids."""
        assert [s["number"] for s in parsed["stories"]] == [1, 2, 3]

    def test_flat_list_is_a_view_not_the_structure(self, parsed):
        """Both shapes are returned — the UI must prefer `chapters`."""
        assert len(parsed["stories"]) == 3
        assert len(parsed["chapters"]) == 2


class TestConfigPreservesChapters:
    """What the browser posts is what the engine builds the book from."""

    def _cfg(self, payload: dict) -> se.BookConfig:
        return se.BookConfig.from_dict({
            "book_title": "World's Dumbest Criminals",
            "topic": "crimes undone by their own plans",
            **payload,
        })

    def test_chapters_survive_config_parsing(self, parsed):
        cfg = self._cfg({"chapters": parsed["chapters"]})
        assert [c.chapter_title for c in cfg.chapters] == [
            "Bank Jobs Gone Wrong", "Criminals Who Called the Police"
        ]

    def test_stories_stay_in_their_chapter(self, parsed):
        cfg = self._cfg({"chapters": parsed["chapters"]})
        assert [len(c.stories) for c in cfg.chapters] == [2, 1]

    def test_chapter_intro_is_carried(self):
        cfg = self._cfg({"chapters": [{
            "chapter_title": "Bank Jobs Gone Wrong",
            "chapter_intro": "Six ways to fail at the simplest crime.",
            "stories": [{"title": "The $1 Bank Robbery"}],
        }]})
        assert cfg.chapters[0].chapter_intro == "Six ways to fail at the simplest crime."

    def test_posting_only_the_flat_list_loses_the_grouping(self, parsed):
        """The old behaviour, pinned so the regression is legible.

        Nothing downstream can recover chapter titles from a flat list, which
        is exactly why the editor must send `chapters`.
        """
        cfg = self._cfg({"stories": parsed["stories"]})
        assert len(cfg.chapters) == 1
        assert cfg.chapters[0].chapter_title == ""

    def test_chapters_win_when_both_shapes_are_sent(self, parsed):
        """The editor posts both; the structured one must take precedence."""
        cfg = self._cfg({
            "chapters": parsed["chapters"],
            "stories": parsed["stories"],
        })
        assert len(cfg.chapters) == 2


class TestExportHeadingLevels:
    """Chapters are Heading 1, stories Heading 2 — the KDP formatter keys the
    chapter-opener page break and the TOC off exactly that."""

    def _book(self, chapter_titles: list[str]) -> se.StoryBook:
        cfg = se.BookConfig.from_dict({
            "book_title": "World's Dumbest Criminals",
            "topic": "crimes undone by their own plans",
            "chapters": [
                {"chapter_title": t, "stories": [{"title": f"Story in {t}"}]}
                for t in chapter_titles
            ],
        })
        book = se.StoryBook(config=cfg)
        n = 1
        for ch_cfg in cfg.chapters:
            chapter = se.Chapter(
                number=ch_cfg.chapter_number, title=ch_cfg.chapter_title
            )
            for st in ch_cfg.stories:
                chapter.stories.append(se.Story(
                    id=f"s{n:03d}", number=n, title=st.title,
                    chapter=chapter.number,
                    body="A paragraph of the story body.",
                ))
                n += 1
            book.chapters.append(chapter)
        return book

    def _headings(self, book: se.StoryBook, tmp_path: Path):
        from docx import Document
        from stories.export import build_docx

        path = build_docx(book, tmp_path / "book.docx")
        doc = Document(str(path))
        return [
            (p.style.name, p.text)
            for p in doc.paragraphs
            if (p.style.name or "").lower().startswith("heading")
        ]

    def test_chapter_titles_are_heading_1(self, tmp_path):
        headings = self._headings(
            self._book(["Bank Jobs Gone Wrong", "Criminals Who Called the Police"]),
            tmp_path,
        )
        h1 = [text for style, text in headings if style.startswith("Heading 1")]
        assert h1 == ["Bank Jobs Gone Wrong", "Criminals Who Called the Police"]

    def test_story_titles_are_heading_2_under_a_chapter(self, tmp_path):
        headings = self._headings(self._book(["Bank Jobs Gone Wrong"]), tmp_path)
        h2 = [text for style, text in headings if style.startswith("Heading 2")]
        assert h2 == ["1. Story in Bank Jobs Gone Wrong"]

    def test_flat_book_promotes_stories_to_heading_1(self, tmp_path):
        """With no chapter titles there is nothing to nest under, so the
        stories become the top level and the TOC still lists them."""
        headings = self._headings(self._book([""]), tmp_path)
        assert all(style.startswith("Heading 1") for style, _ in headings)


class TestIllustrationTargets:
    """Sixty stories must not mean sixty images — one per chapter is the
    default, and only chapters get an illustration slot."""

    def test_chapter_carries_the_illustration_by_default(self):
        cfg = se.BookConfig.from_dict({
            "book_title": "B", "topic": "t", "illustrations": True,
            "chapters": [{"chapter_title": "Bank Jobs Gone Wrong",
                          "stories": [{"title": "One"}, {"title": "Two"}]}],
        })
        assert cfg.illustrations is True
        assert cfg.illustrate_every_story is False

    def test_per_story_images_are_opt_in(self):
        cfg = se.BookConfig.from_dict({
            "book_title": "B", "topic": "t", "illustrations": True,
            "illustrate_every_story": True,
            "chapters": [{"chapter_title": "C", "stories": [{"title": "One"}]}],
        })
        assert cfg.illustrate_every_story is True
