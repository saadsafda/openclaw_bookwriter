"""Regression tests for defects found during the QA pass.

Each class documents one bug: what broke, and what the failure cost. All five
were crash-or-silent-miss defects reachable from ordinary production input.
"""

from __future__ import annotations

import pathlib

import pytest
from docx import Document
from PIL import Image, ImageChops, ImageDraw

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

    # Every paragraph but "Alone." clears MIN_SENTENCES_PER_PARAGRAPH, so the
    # test stays pinned to the CRLF bug rather than the sentence floor.
    CRLF = ("One. Two. Three. Four.\r\n\r\n"
            "Alone.\r\n\r\n"
            "Five. Six. Seven. Eight.")

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
        # The other paragraphs clear MIN_SENTENCES_PER_PARAGRAPH so the only
        # thing this can flag is the ellipsis paragraph.
        body = ("He waited... then went.\n\n"
                "Real one. With two. And three. And four.\n\n"
                "End one. End two. End three. End four.")
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


class TestBug6BirdsControlChars:
    """birds/export.py takes plain tuples, so the dataclass walker missed it."""

    DIRTY = "Barn Owl\x07 rare\x0b"

    def _plate(self, tmp_path):
        p = tmp_path / "bird.png"
        Image.new("RGB", (1024, 1536), "white").save(p)
        return p

    def test_guide_survives_control_chars(self, tmp_path):
        import birds.export as bx
        out = tmp_path / "guide.docx"
        bx.build_docx([(self.DIRTY, self._plate(tmp_path))], out,
                      bx.GuideConfig(title=self.DIRTY, subtitle=self.DIRTY,
                                     author=self.DIRTY))
        Document(str(out))  # used to raise ValueError

    def test_species_name_is_cleaned_not_dropped(self, tmp_path):
        import birds.export as bx
        out = tmp_path / "guide.docx"
        bx.build_docx([(self.DIRTY, self._plate(tmp_path))], out,
                      bx.GuideConfig(title="Guide"))
        text = "\n".join(p.text for p in Document(str(out)).paragraphs)
        assert "Barn Owl rare" in text


class TestBug7EditorControlChars:
    """write_blocks is web-facing; a control char lost the user's edit."""

    def _doc(self, tmp_path):
        d = Document()
        d.add_paragraph("original text")
        p = tmp_path / "e.docx"
        d.save(str(p))
        return p

    def test_edit_with_control_chars_succeeds(self, tmp_path):
        import book_editor as be
        p = self._doc(tmp_path)
        result = be.write_blocks(p, [{"index": 0, "text": "clean \x07 edit"}])
        assert result["changed"] == 1

    def test_the_edit_is_actually_applied(self, tmp_path):
        import book_editor as be
        p = self._doc(tmp_path)
        be.write_blocks(p, [{"index": 0, "text": "clean \x07 edit"}])
        assert "clean  edit" in Document(str(p)).paragraphs[0].text

    def test_a_failed_edit_never_corrupts_the_file(self, tmp_path):
        import book_editor as be
        p = self._doc(tmp_path)
        try:
            be.write_blocks(p, [{"index": 999, "text": "out of range"}])
        except Exception:
            pass
        Document(str(p))  # must still open


class TestBug8CumulativeResampling:
    """Rebuilding at increasing widths re-resampled already-resampled pixels."""

    @staticmethod
    def _detailed(path):
        im = Image.new("RGB", (1024, 1536), (250, 250, 252))
        d = ImageDraw.Draw(im)
        for i in range(0, 1024, 16):
            d.line([(i, 0), (i, 1536)], fill=(90, 120, 170), width=1)
        im.save(path)
        return path

    @staticmethod
    def _mse(a, b):
        diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
        h = diff.histogram()
        px = a.size[0] * a.size[1]
        return sum(i * i * (h[i] + h[256 + i] + h[512 + i])
                   for i in range(256)) / (3 * px)

    def test_incremental_matches_single_pass(self, tmp_path):
        single = self._detailed(tmp_path / "a.png")
        ph.sanitize_for_print(single, 300, width_in=6.0)
        ref = Image.open(single).copy()

        stepped = self._detailed(tmp_path / "b.png")
        for w in (3.5, 4.0, 4.5, 5.0, 5.5, 6.0):
            ph.sanitize_for_print(stepped, 300, width_in=w)

        with Image.open(stepped) as got:
            assert got.size == ref.size
            assert self._mse(ref, got) < 1.0, "cumulative resampling degraded the plate"

    def test_repeat_at_same_width_is_a_no_op(self, tmp_path):
        p = self._detailed(tmp_path / "c.png")
        assert ph.upscale_for_print(p, 4.5) is True
        assert ph.upscale_for_print(p, 4.5) is False

    def test_a_smaller_width_never_shrinks_the_file(self, tmp_path):
        p = self._detailed(tmp_path / "d.png")
        ph.upscale_for_print(p, 6.0)
        with Image.open(p) as im:
            big = im.size
        assert ph.upscale_for_print(p, 3.0) is False
        with Image.open(p) as im:
            assert im.size == big

    def test_sidecar_is_not_treated_as_a_book_image(self, tmp_path):
        p = self._detailed(tmp_path / "e.png")
        ph.upscale_for_print(p, 4.5)
        assert ph._original_sidecar(p).exists()
        assert ph.is_original_sidecar(ph._original_sidecar(p))
        # audit_tree walks by raster suffix; the sidecar must not appear
        assert all(not ph.is_original_sidecar(k)
                   for k in ph.audit_tree(tmp_path, 300, width_in=4.5))


class TestBug9HandoffZipBadAsset:
    """One unreadable puzzle image aborted the formatter bundle."""

    def test_zip_completes_with_a_corrupt_asset(self, tmp_path):
        from puzzle.engine import BookConfig, Maze, PuzzleBook
        from puzzle import export as px
        good = tmp_path / "ok.png"
        Image.new("RGB", (1800, 2700), "white").save(good)
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not an image")

        book = PuzzleBook(config=BookConfig(book_title="P", topic="t"))
        book.mazes = [Maze("m", 1, "Maze", 10, 10, str(good), str(bad), 1)]
        zp = tmp_path / "handoff.zip"
        px.build_handoff_zip(book, tmp_path, zp)

        assert zp.exists()
        assert book.warnings, "an omitted asset must be reported"
