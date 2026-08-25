"""A non-zero openclaw exit caused upstream must trip the outage breaker.

Production hit this with an expired OpenAI refresh token: openclaw exited
non-zero and the 401 was only in stderr, so call_openclaw_raw raised a plain
PuzzleError. _ask only counts ProviderRejectionError toward its refusal
streak, so the breaker never tripped -- every section retried the same dead
provider to exhaustion, and the build still reported "done / 100%".
"""

from __future__ import annotations

import subprocess

import pytest

from puzzle import engine, pipeline
from puzzle.engine import BookConfig, ProviderRejectionError, PuzzleError


# Real stderr from the failed build, colour codes and migration noise included.
AUTH_STDERR = (
    "\x1b[32m[state-migrations]\x1b[39m \x1b[33mLegacy state migration "
    "warnings:\x1b[39m\n"
    "\x1b[33m- Left plugin install index in place because shared SQLite "
    "state has conflicting plugin install metadata for: brave, codex\x1b[39m\n"
    "GatewayClientRequestError: Error: CLI transcript compaction failed for "
    "openai/gpt-5.5: OAuth token refresh failed for openai: OpenAI Codex "
    "token refresh failed (401): { \"error\": { \"message\": \"Your refresh "
    "token has already been used to generate a new access tok...\n"
)


def _exit(code, stderr="", stdout=""):
    def _run(*a, **k):
        return subprocess.CompletedProcess(a[0], code, stdout, stderr)
    return _run


class TestUpstreamFailureIsClassifiedAsRejection:
    def test_auth_failure_raises_provider_rejection(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _exit(1, AUTH_STDERR))
        with pytest.raises(ProviderRejectionError):
            engine.call_openclaw_raw("main", "hi")

    def test_message_names_the_cause_not_the_prompt(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _exit(1, AUTH_STDERR))
        with pytest.raises(ProviderRejectionError) as ei:
            engine.call_openclaw_raw("main", "PROMPT-" + "x" * 900)
        msg = str(ei.value)
        assert "401" in msg or "token refresh failed" in msg.lower()
        assert "PROMPT-" not in msg, "the prompt echo is back in the error"
        assert "\x1b[" not in msg, "ANSI codes leaked into the operator log"
        assert len(msg) < 300, f"error is still a wall of text ({len(msg)})"

    def test_a_genuine_local_error_stays_a_plain_puzzle_error(self, monkeypatch):
        """A bad flag or missing agent must surface, not look like an outage."""
        monkeypatch.setattr(
            subprocess, "run", _exit(1, "error: unknown agent 'nope'"))
        with pytest.raises(PuzzleError) as ei:
            engine.call_openclaw_raw("nope", "hi")
        assert not isinstance(ei.value, ProviderRejectionError)


class TestBreakerTripsOnAuthFailure:
    """The production waste: every section retried a provider that was down."""

    def test_builder_stops_calling_after_the_streak(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline.time, "sleep", lambda d: None)
        monkeypatch.setattr(subprocess, "run", _exit(1, AUTH_STDERR))

        calls = {"n": 0}
        real = engine.call_openclaw_raw

        def _counted(*a, **k):
            calls["n"] += 1
            return real(*a, **k)

        monkeypatch.setattr(pipeline.engine, "call_openclaw_raw", _counted)

        cfg = BookConfig(book_title="B", topic="birds")
        b = pipeline.PuzzleBuilder(cfg, cache_dir=tmp_path / "cache")
        b.log = lambda *a, **k: None

        for _ in range(pipeline.PROVIDER_OUTAGE_STREAK):
            with pytest.raises(PuzzleError):
                b._ask("give me a list")

        assert b._provider_down, "breaker never tripped on an auth failure"

        before = calls["n"]
        with pytest.raises(ProviderRejectionError):
            b._ask("another prompt entirely")
        assert calls["n"] == before, (
            "a downed provider was called again after the breaker tripped")
