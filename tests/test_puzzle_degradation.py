"""A failing section must degrade, not destroy the book.

Puzzle sections are independent: a word search whose list the provider refuses
should cost the book that one puzzle, not the whole run. This is what kept the
photography book building at 29% while two word searches failed.
"""

from __future__ import annotations

import pytest

from puzzle import pipeline
from puzzle.engine import BookConfig, ProviderRejectionError, PuzzleError


@pytest.fixture
def builder(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)

    def _make(**kw):
        cfg = BookConfig(book_title="B", topic="photography", **kw)
        b = pipeline.PuzzleBuilder(cfg, cache_dir=tmp_path / "cache")
        b.log = lambda *a, **k: None
        return b

    return _make


class TestWordSearchFailure:
    def test_a_refused_word_list_returns_none(self, builder, tmp_path):
        b = builder()
        b._ask = lambda _p: (_ for _ in ()).throw(
            ProviderRejectionError("provider rejected the request"))

        out = b._one_word_search(1, "lighting", 15, tmp_path)
        assert out is None

    def test_the_failure_is_recorded_as_a_warning(self, builder, tmp_path):
        b = builder()
        b._ask = lambda _p: (_ for _ in ()).throw(
            ProviderRejectionError("provider rejected the request"))
        b._one_word_search(1, "lighting", 15, tmp_path)

        assert any("lighting" in w for w in b.book.warnings), (
            "a puzzle vanished from the book with nothing to tell the "
            "operator why")

    def test_one_refusal_does_not_lose_the_puzzle(self, builder, tmp_path):
        """A transient refusal is retried inside the puzzle, not fatal to it."""
        b = builder()
        calls = {"n": 0}

        def sometimes(prompt):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ProviderRejectionError("provider rejected the request")
            return ["APERTURE", "SHUTTER", "TRIPOD", "LENS", "FLASH",
                    "FILTER", "ZOOM", "FOCUS", "PRISM"]

        b._ask = sometimes
        puzzle = b._one_word_search(1, "lighting", 15, tmp_path)

        assert puzzle is not None, "a single transient refusal lost the puzzle"
        assert len(puzzle.words) == 9

    def test_a_later_puzzle_still_builds_after_one_is_lost(self, builder, tmp_path):
        """The whole point: an unbuildable puzzle must not end the section."""
        b = builder()

        def per_subject(prompt):
            if "lighting" in prompt:
                raise ProviderRejectionError("provider rejected the request")
            return ["APERTURE", "SHUTTER", "TRIPOD", "LENS", "FLASH",
                    "FILTER", "ZOOM", "FOCUS", "PRISM"]

        b._ask = per_subject
        first = b._one_word_search(1, "lighting", 15, tmp_path)
        second = b._one_word_search(2, "composition", 15, tmp_path)

        assert first is None
        assert second is not None
        assert len(second.words) == 9

    def test_retries_with_a_different_prompt(self, builder, tmp_path):
        """Retrying an identical prompt would just hit the cache."""
        b = builder()
        seen: list[str] = []

        def record(prompt):
            seen.append(prompt)
            raise PuzzleError("no usable words")

        b._ask = record
        b._one_word_search(1, "lighting", 15, tmp_path)

        assert len(seen) > 1
        assert len(set(seen)) == len(seen), (
            "the same word-list prompt was resent; the cache would replay the "
            "same unusable reply")


class TestWarningsSurfaceToTheOperator:
    def test_warn_records_and_logs(self, builder):
        b = builder()
        lines: list[str] = []
        b.log = lines.append
        b._warn("something degraded")

        assert "something degraded" in b.book.warnings
        assert any("something degraded" in ln for ln in lines)
