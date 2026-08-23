"""A refusing provider must fail fast, not grind through every section.

_ask already retries a refusal with backoff. When each caller ALSO retried on
top of that, one refused subject cost MAX_LAYOUT_ATTEMPTS x (PROVIDER_RETRIES
+ 1) provider calls and four rounds of backoff sleeping -- while producing
nothing. Across a book of word searches, crosswords, riddles, cryptograms and
trivia that turned a provider outage into many minutes of billed waiting.
"""

from __future__ import annotations

import pytest

from puzzle import pipeline
from puzzle.engine import BookConfig, ProviderRejectionError, PuzzleError


@pytest.fixture
def builder(tmp_path, monkeypatch):
    """A builder whose backoff sleeps are instant and whose calls are counted."""
    slept: list[float] = []
    monkeypatch.setattr(pipeline.time, "sleep", lambda d: slept.append(d))

    def _make(**kw):
        cfg = BookConfig(book_title="B", topic="photography", **kw)
        b = pipeline.PuzzleBuilder(cfg, cache_dir=tmp_path / "cache")
        b.log = lambda *a, **k: None
        return b

    _make.slept = slept
    return _make


def _always_refuse(counter):
    def _call(*a, **k):
        counter["n"] += 1
        raise ProviderRejectionError(
            "LLM request failed: provider rejected the request schema or "
            "tool payload.")
    return _call


class TestRefusalDoesNotBurnLayoutAttempts:
    """The production waste: 12 calls and ~60s of sleeping for one subject."""

    def test_word_search_stops_well_short_of_the_full_budget(
            self, builder, tmp_path, monkeypatch):
        b = builder()
        calls = {"n": 0}
        monkeypatch.setattr(pipeline.engine, "call_openclaw_raw",
                            _always_refuse(calls))

        assert b._one_word_search(1, "lighting", 15, tmp_path) is None

        worst_case = (pipeline.MAX_LAYOUT_ATTEMPTS
                      * (pipeline.PROVIDER_RETRIES + 1))
        assert calls["n"] < worst_case, (
            f"a refused subject still cost {calls['n']} provider calls; the "
            f"layout budget is being spent on an upstream refusal")

    def test_the_puzzle_itself_limits_refusals(self, builder, tmp_path,
                                               monkeypatch):
        """Isolated from the circuit breaker, which would otherwise mask this.

        With the breaker disabled, a refused subject must still stop after
        PROVIDER_REFUSALS_PER_PUZZLE exhausted asks rather than spending the
        whole layout budget on an upstream refusal.
        """
        b = builder()
        calls = {"n": 0}
        monkeypatch.setattr(pipeline.engine, "call_openclaw_raw",
                            _always_refuse(calls))
        # Pin the breaker open so only the per-puzzle limit is under test.
        monkeypatch.setattr(pipeline, "PROVIDER_OUTAGE_STREAK", 10_000)

        assert b._one_word_search(1, "lighting", 15, tmp_path) is None

        expected = (pipeline.PROVIDER_REFUSALS_PER_PUZZLE
                    * (pipeline.PROVIDER_RETRIES + 1))
        assert calls["n"] == expected, (
            f"expected {expected} provider calls for a refused subject, got "
            f"{calls['n']}; the layout budget is being spent on refusals")

    def test_crossword_stops_well_short_too(self, builder, tmp_path, monkeypatch):
        b = builder()
        calls = {"n": 0}
        monkeypatch.setattr(pipeline.engine, "call_openclaw_raw",
                            _always_refuse(calls))

        assert b._one_crossword(1, "composition", tmp_path) is None

        worst_case = (pipeline.MAX_LAYOUT_ATTEMPTS
                      * (pipeline.PROVIDER_RETRIES + 1))
        assert calls["n"] < worst_case

    def test_crossword_limits_refusals_on_its_own(self, builder, tmp_path,
                                                  monkeypatch):
        """As above, with the circuit breaker pinned open."""
        b = builder()
        calls = {"n": 0}
        monkeypatch.setattr(pipeline.engine, "call_openclaw_raw",
                            _always_refuse(calls))
        monkeypatch.setattr(pipeline, "PROVIDER_OUTAGE_STREAK", 10_000)

        assert b._one_crossword(1, "composition", tmp_path) is None

        expected = (pipeline.PROVIDER_REFUSALS_PER_PUZZLE
                    * (pipeline.PROVIDER_RETRIES + 1))
        assert calls["n"] == expected, (
            f"expected {expected} provider calls, got {calls['n']}")

    def test_a_transient_refusal_still_does_not_lose_the_puzzle(
            self, builder, tmp_path, monkeypatch):
        """Failing fast must not mean giving up on one blip."""
        b = builder()
        state = {"n": 0}

        def refuse_then_answer(*a, **k):
            state["n"] += 1
            # Exhaust the first _ask entirely, then start answering.
            if state["n"] <= pipeline.PROVIDER_RETRIES + 1:
                raise ProviderRejectionError("provider rejected the request")
            return '["APERTURE","SHUTTER","TRIPOD","LENS","FLASH","FILTER",' \
                   '"ZOOM","FOCUS","PRISM"]'

        monkeypatch.setattr(pipeline.engine, "call_openclaw_raw",
                            refuse_then_answer)
        puzzle = b._one_word_search(1, "lighting", 15, tmp_path)

        assert puzzle is not None, (
            "one exhausted _ask lost the puzzle; a refusal that clears on the "
            "next attempt should still produce content")

    def test_content_failures_still_get_the_full_budget(
            self, builder, tmp_path, monkeypatch):
        """Layout attempts exist for unusable word lists; that must still work."""
        b = builder()
        calls = {"n": 0}

        def unusable(*a, **k):
            calls["n"] += 1
            return '["AB","CD"]'          # too short, never enough words

        monkeypatch.setattr(pipeline.engine, "call_openclaw_raw", unusable)
        assert b._one_word_search(1, "lighting", 15, tmp_path) is None
        assert calls["n"] == pipeline.MAX_LAYOUT_ATTEMPTS, (
            "a content failure no longer gets its retries")


class TestOutageCircuitBreaker:
    """Once the provider is plainly down, stop calling it."""

    def test_further_asks_fail_immediately(self, builder, monkeypatch):
        b = builder()
        calls = {"n": 0}
        monkeypatch.setattr(pipeline.engine, "call_openclaw_raw",
                            _always_refuse(calls))

        for _ in range(pipeline.PROVIDER_OUTAGE_STREAK):
            with pytest.raises(ProviderRejectionError):
                b._ask("a prompt")
        spent = calls["n"]

        with pytest.raises(ProviderRejectionError):
            b._ask("another prompt")
        assert calls["n"] == spent, (
            "the build kept calling a provider already known to be refusing")

    def test_the_outage_is_reported_to_the_operator(self, builder, monkeypatch):
        b = builder()
        calls = {"n": 0}
        monkeypatch.setattr(pipeline.engine, "call_openclaw_raw",
                            _always_refuse(calls))
        for _ in range(pipeline.PROVIDER_OUTAGE_STREAK):
            with pytest.raises(ProviderRejectionError):
                b._ask("p")

        assert b.book.warnings, "an outage passed with nothing said to the operator"
        joined = " ".join(b.book.warnings).lower()
        assert "provider" in joined
        assert "not a problem with your book settings" in joined, (
            "the warning blames the book instead of naming the outage")

    def test_a_success_clears_the_streak(self, builder, monkeypatch):
        """A recovered provider must not inherit an old outage."""
        b = builder()
        state = {"n": 0}

        def refuse_twice_then_work(*a, **k):
            state["n"] += 1
            if state["n"] <= pipeline.PROVIDER_RETRIES + 1:
                raise ProviderRejectionError("provider rejected the request")
            return '["ONE","TWO"]'

        monkeypatch.setattr(pipeline.engine, "call_openclaw_raw",
                            refuse_twice_then_work)

        with pytest.raises(ProviderRejectionError):
            b._ask("p")          # streak -> 1
        assert b._ask("q") == ["ONE", "TWO"]
        assert b._refusal_streak == 0
        assert not b._provider_down

    def test_backoff_sleeping_is_bounded(self, builder, tmp_path, monkeypatch):
        """The visible symptom was a minute of sleeping per subject."""
        b = builder()
        calls = {"n": 0}
        monkeypatch.setattr(pipeline.engine, "call_openclaw_raw",
                            _always_refuse(calls))
        b._one_word_search(1, "lighting", 15, tmp_path)

        worst = (pipeline.MAX_LAYOUT_ATTEMPTS * pipeline.PROVIDER_RETRIES
                 * pipeline.PROVIDER_BACKOFF_S * 2)
        assert sum(builder.slept) < worst, (
            f"slept {sum(builder.slept)}s on a single refused subject")
