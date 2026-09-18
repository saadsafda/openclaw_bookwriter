"""Provider-rejection handling in the puzzle pipeline.

A rejection is an upstream refusal, not a content failure: the provider returns
a well-formed JSON envelope whose payload text is an error sentence. Two things
must hold. It must be recognised as its own kind of failure so the caller backs
off instead of hammering an identical prompt, and it must never be cached --
caching one is permanent, because the next run keys off the same prompt, hits
the entry, and re-raises the failure without ever calling the provider.
"""

from __future__ import annotations

import pytest

from puzzle import engine
from puzzle.engine import PuzzleError, ProviderRejectionError, RawOutputCache


REJECTION_TEXTS = [
    "LLM request failed: provider rejected the request schema or tool payload.",
    "llm request failed",
    "Provider rejected the request.",
    "Request too large for this model.",
    "context length exceeded",
    "prompt is too long",
]


class TestRejectionDetection:
    @pytest.mark.parametrize("text", REJECTION_TEXTS)
    def test_recognises_upstream_refusals(self, text):
        assert engine.is_provider_rejection(text)

    @pytest.mark.parametrize("text", [
        '["CAMERA","LENS","SHUTTER"]',
        "APERTURE",
        "",
    ])
    def test_passes_real_content_through(self, text):
        assert not engine.is_provider_rejection(text)

    def test_long_replies_are_never_rejections(self):
        """A riddle batch may quote one of these phrases; a refusal never runs long."""
        body = "A legitimate riddle answer that mentions llm request failed. "
        assert not engine.is_provider_rejection(body * 20)

    def test_raises_a_distinct_type(self, rejection_stdout, monkeypatch, tmp_path):
        """The caller backs off only if this is not a generic PuzzleError."""
        import subprocess

        class Done:
            returncode = 0
            stdout = rejection_stdout()
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
        with pytest.raises(ProviderRejectionError):
            engine.call_openclaw_raw("main", "any prompt")

    def test_rejection_is_a_puzzle_error(self):
        """Callers that catch PuzzleError must still see it."""
        assert issubclass(ProviderRejectionError, PuzzleError)


class TestRejectionsAreNeverCached:
    """Caching a refusal makes it permanent and free, so it can never heal."""

    def test_set_refuses_to_store_one(self, tmp_path, rejection_stdout):
        cache = RawOutputCache(tmp_path)
        key = cache.key_for("main", "a prompt")
        cache.set(key, rejection_stdout(), prompt="a prompt")
        assert cache.get(key) is None, (
            "a provider rejection was cached; every later run would replay it "
            "at zero cost and never call the provider")

    def test_set_stores_real_content(self, tmp_path, good_stdout):
        cache = RawOutputCache(tmp_path)
        key = cache.key_for("main", "a prompt")
        cache.set(key, good_stdout(), prompt="a prompt")
        assert cache.get(key) is not None

    def test_set_ignores_empty_output(self, tmp_path):
        cache = RawOutputCache(tmp_path)
        key = cache.key_for("main", "a prompt")
        cache.set(key, "   ", prompt="a prompt")
        assert cache.get(key) is None

    def test_a_poisoned_entry_heals_on_read(self, tmp_path, rejection_stdout,
                                            good_stdout, monkeypatch):
        """Caches written before set() screened rejections must self-repair.

        Otherwise an existing poisoned cache needs a manual rm before the build
        can ever succeed again.
        """
        import subprocess

        cache = RawOutputCache(tmp_path)
        key = cache.key_for("main", "a prompt")
        # Write the poison directly, as an older build would have.
        (tmp_path / f"{key}.json").write_text(rejection_stdout(), encoding="utf-8")
        assert cache.get(key) is not None

        class Done:
            returncode = 0
            stdout = good_stdout()
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
        reply = engine.call_openclaw_raw("main", "a prompt", cache=cache)

        assert "CAMERA" in reply, "the poisoned entry was replayed"
        assert cache.get(key) is not None
        assert not engine.is_provider_rejection(
            engine.parse_openclaw_reply(cache.get(key)))

    def test_a_poisoned_entry_is_evicted_not_replayed(self, tmp_path,
                                                      rejection_stdout,
                                                      monkeypatch):
        """The poisoned entry must be dropped even if the retry also fails.

        Overwriting it with a later success would mask a missing evict; the
        entry has to be gone the moment it is recognised as a rejection, so a
        build that keeps failing still heals its own cache.
        """
        import subprocess

        cache = RawOutputCache(tmp_path)
        key = cache.key_for("main", "a prompt")
        (tmp_path / f"{key}.json").write_text(rejection_stdout(), encoding="utf-8")

        class Refused:
            returncode = 0
            stdout = rejection_stdout()
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Refused())
        with pytest.raises(ProviderRejectionError):
            engine.call_openclaw_raw("main", "a prompt", cache=cache)

        assert cache.get(key) is None, (
            "the poisoned cache entry survived; it would be replayed forever "
            "at zero cost and the build could never call the provider again")

    def test_a_cache_hit_on_poison_still_calls_the_provider(self, tmp_path,
                                                            rejection_stdout,
                                                            good_stdout,
                                                            monkeypatch):
        """A poisoned hit must fall through to a real call, not short-circuit."""
        import subprocess

        cache = RawOutputCache(tmp_path)
        key = cache.key_for("main", "a prompt")
        (tmp_path / f"{key}.json").write_text(rejection_stdout(), encoding="utf-8")

        calls = {"n": 0}

        class Done:
            returncode = 0
            stdout = good_stdout()
            stderr = ""

        def counting_run(*a, **k):
            calls["n"] += 1
            return Done()

        monkeypatch.setattr(subprocess, "run", counting_run)
        engine.call_openclaw_raw("main", "a prompt", cache=cache)

        assert calls["n"] == 1, (
            "the poisoned entry short-circuited the call, so the build could "
            "never recover on its own")

    def test_evict_removes_entry_and_prompt(self, tmp_path, good_stdout):
        cache = RawOutputCache(tmp_path)
        key = cache.key_for("main", "a prompt")
        cache.set(key, good_stdout(), prompt="a prompt")
        cache.evict(key)
        assert cache.get(key) is None
        assert not (tmp_path / f"{key}.prompt.txt").exists()

    def test_evict_is_safe_when_absent(self, tmp_path):
        RawOutputCache(tmp_path).evict("never-written")


class TestRetryAndBackoff:
    """A refusal is usually transient, so it earns a bounded retry."""

    def test_retries_then_gives_up_with_the_provider_error(self, tmp_path, monkeypatch):
        from puzzle import pipeline
        monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
        from puzzle.engine import BookConfig

        cfg = BookConfig(book_title="B", topic="t")
        b = pipeline.PuzzleBuilder(cfg)

        attempts = {"n": 0}

        def always_refuse(*a, **k):
            attempts["n"] += 1
            raise ProviderRejectionError("provider rejected the request")

        b.log = lambda *a, **k: None
        import puzzle.engine as pe
        original = pe.call_openclaw_raw
        pe.call_openclaw_raw = always_refuse
        try:
            with pytest.raises(ProviderRejectionError):
                b._ask("a prompt")
        finally:
            pe.call_openclaw_raw = original

        assert attempts["n"] == pipeline.PROVIDER_RETRIES + 1, (
            f"expected {pipeline.PROVIDER_RETRIES + 1} attempts, "
            f"got {attempts['n']}")

    def test_succeeds_when_a_retry_clears(self, tmp_path, monkeypatch):
        from puzzle import pipeline
        monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
        from puzzle.engine import BookConfig

        cfg = BookConfig(book_title="B", topic="t")
        b = pipeline.PuzzleBuilder(cfg)
        b.log = lambda *a, **k: None

        calls = {"n": 0}

        def refuse_once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ProviderRejectionError("provider rejected the request")
            return '["CAMERA","LENS"]'

        import puzzle.engine as pe
        original = pe.call_openclaw_raw
        pe.call_openclaw_raw = refuse_once
        try:
            assert b._ask("a prompt") == ["CAMERA", "LENS"]
        finally:
            pe.call_openclaw_raw = original
