"""Regression tests for defects found during the QA pass.

Each class documents one bug: what broke, and what the failure cost. All five
were crash-or-silent-miss defects reachable from ordinary production input.
"""

from __future__ import annotations

import pathlib

import pytest
from docx import Document
from PIL import Image

import print_hygiene as ph
from openclaw_docx_writer import (
    find_lone_sentence_paragraphs,
    merge_lone_sentence_paragraphs,
    split_sentences,
)
from stories import engine as se


def _img(path, w, h):
    Image.new("RGB", (w, h), "white").save(path)
    return path


def _trivia_book(illustration="", text="Owls"):
    from trivia.engine import (
        BookConfig, Chapter, ChapterConfig, DidYouKnowFact,
        TriviaBook, TriviaQuestion,
    )
    cfg = BookConfig(
        book_title=text, topic=text,
        chapters=[ChapterConfig.from_dict(
            {"chapter_title": "C", "chapter_scope": "s"}, 1)],
    )
    return TriviaBook(config=cfg, chapters=[Chapter(
        1, text, "s",
        trivia=[TriviaQuestion("q", 1, text,
                               {"A": text, "B": "b", "C": "c", "D": "d"}, "B")],
        facts=[DidYouKnowFact("f", 1, text)],
        illustration_path=illustration,
    )])


class TestBug1DecompressionBomb:
    """A degenerate aspect ratio scaled to 911MP and raised DecompressionBombError."""

    def test_extreme_aspect_does_not_raise(self, tmp_path):
        p = _img(tmp_path / "strip.png", 10, 5000)
        ph.upscale_for_print(p, 4.5)  # used to raise

    def test_result_stays_within_the_cap(self, tmp_path):
        p = _img(tmp_path / "strip.png", 10, 5000)
        ph.upscale_for_print(p, 4.5)
        with Image.open(p) as im:
            assert im.size[0] <= ph.MAX_UPSCALE_PX[0]
            assert im.size[1] <= ph.MAX_UPSCALE_PX[1]

    def test_normal_art_is_unaffected_by_the_cap(self, tmp_path):
        p = _img(tmp_path / "normal.png", 1024, 1536)
        assert ph.upscale_for_print(p, 4.5) is True
        with Image.open(p) as im:
            assert im.size == (1350, 2025)

    @pytest.mark.parametrize("width_in", [0, -4.5])
    def test_non_positive_width_is_a_no_op(self, tmp_path, width_in):
        p = _img(tmp_path / "n.png", 1024, 1024)
        assert ph.upscale_for_print(p, width_in) is False


class TestBug2CorruptImageAbortsExport:
    """One unreadable illustration killed the whole book export."""

    @pytest.mark.parametrize("data", [
        b"", b"not an image", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", b"\x00" * 256,
    ])
    def test_sanitize_raises_the_module_error_type(self, tmp_path, data):
        p = tmp_path / "bad.png"
        p.write_bytes(data)
        with pytest.raises(ph.PrintHygieneError):
            ph.sanitize_for_print(p, width_in=4.5)

    def test_trivia_export_survives_a_corrupt_illustration(self, tmp_path):
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not an image")
        book = _trivia_book(illustration=str(bad))
        out = tmp_path / "b.docx"
        from trivia import export as ex
        ex.build_docx(book, out)  # used to raise UnidentifiedImageError
        assert out.exists()
        assert book.warnings, "a skipped illustration must be reported"

    def test_the_book_content_is_still_complete(self, tmp_path):
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not an image")
        book = _trivia_book(illustration=str(bad))
        out = tmp_path / "b.docx"
        from trivia import export as ex
        ex.build_docx(book, out)
        text = "\n".join(p.text for p in Document(str(out)).paragraphs)
        assert "Answer Key" in text


class TestBug3CrlfBypassedTheCheck:
    """Splitting on "\\n\\n" missed every paragraph break in a CRLF file."""

    CRLF = "One. Two.\r\n\r\nAlone.\r\n\r\nEnd."

    def test_docx_writer_detects_it(self):
        assert find_lone_sentence_paragraphs(self.CRLF) == ["Alone."]

    def test_stories_detects_it(self):
        assert se.lone_sentence_paragraphs(self.CRLF) == [1]

    def test_merge_fixes_it(self):
        assert find_lone_sentence_paragraphs(
            merge_lone_sentence_paragraphs(self.CRLF)) == []

    def test_lf_and_crlf_agree(self):
        lf = self.CRLF.replace("\r\n", "\n")
        assert (len(find_lone_sentence_paragraphs(self.CRLF))
                == len(find_lone_sentence_paragraphs(lf)))


class TestBug4EllipsisMiscounted:
    """"He waited... then went." counted as 2 sentences, so a lone one escaped."""

    @pytest.mark.parametrize("text", [
        "He waited... then went.",
        "He waited… then went.",
        "Wait...  Then go.",
    ])
    def test_ellipsis_is_one_sentence(self, text):
        assert len(split_sentences(text)) == 1

    def test_stories_splitter_agrees(self):
        assert len(se.split_sentences("He waited... then went.")) == 1

    def test_a_lone_ellipsis_paragraph_is_caught(self):
        body = "He waited... then went.\n\nReal one. With two.\n\nEnd."
        assert find_lone_sentence_paragraphs(body) == ["He waited... then went."]

    def test_real_sentence_breaks_still_split(self):
        assert len(split_sentences("One. Two. Three.")) == 3


class TestBug5ControlCharsAbortedExport:
    """A single \\x07 made python-docx raise mid-write, losing the book."""

    DIRTY = "bell\x07 vert\x0b ff\x0c ok"

    def test_xml_safe_strips_them(self):
        assert ph.xml_safe(self.DIRTY) == "bell vert ff ok"

    def test_xml_safe_keeps_tab_and_newline(self):
        assert ph.xml_safe("a\tb\nc") == "a\tb\nc"

    def test_strip_control_chars_walks_a_book(self):
        book = _trivia_book(text=self.DIRTY)
        changed = ph.strip_control_chars(book)
        assert changed > 0
        assert "\x07" not in book.config.book_title

    @pytest.mark.parametrize("pipeline", ["trivia", "puzzle", "stories"])
    def test_every_exporter_survives_control_chars(self, tmp_path, pipeline):
        out = tmp_path / f"{pipeline}.docx"
        if pipeline == "trivia":
            from trivia import export as ex
            ex.build_docx(_trivia_book(text=self.DIRTY), out)
        elif pipeline == "puzzle":
            from puzzle.engine import BookConfig, PuzzleBook, Riddle
            from puzzle import export as ex
            b = PuzzleBook(config=BookConfig(
                book_title=self.DIRTY, topic=self.DIRTY))
            b.riddles = [Riddle("r", 1, self.DIRTY, self.DIRTY)]
            ex.build_docx(b, out)
        else:
            from stories.engine import BookConfig, Chapter, Story, StoryBook
            from stories import export as ex
            st = Story(id="s", number=1, chapter=1, title=self.DIRTY,
                       body=self.DIRTY + ". Second sentence here.")
            b = StoryBook(
                config=BookConfig(book_title=self.DIRTY, topic=self.DIRTY),
                chapters=[Chapter(number=1, title=self.DIRTY,
                                  intro=self.DIRTY, stories=[st])])
            ex.build_docx(b, out)
        assert out.exists()
        Document(str(out))  # must be a readable docx


class TestIdempotenceAndSafety:
    def test_repeated_sanitize_converges(self, tmp_path):
        p = _img(tmp_path / "a.png", 1024, 1536)
        for _ in range(4):
            ph.sanitize_for_print(p, width_in=4.5)
        assert ph.audit_image(p, width_in=4.5) == []

    def test_filename_sanitiser_blocks_traversal(self):
        from trivia.routes import _safe_stem
        for evil in ("../../etc/passwd", "..\\..\\windows", "a/b/c"):
            stem = _safe_stem(evil)
            assert "/" not in stem and "\\" not in stem and ".." not in stem
