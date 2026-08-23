"""Shared fixtures for the generator test suites.

The pipelines talk to a paid provider through a single seam -- the method that
issues one model call. Every test here replaces that seam with a scripted fake,
so the suite exercises the real control flow (batching, dedup, refill, caching,
validation) without spending anything or depending on the network.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# -- trivia ---------------------------------------------------------------

@pytest.fixture
def trivia_cfg():
    """A two-chapter book, small counts so tests stay fast and readable."""
    from trivia.engine import BookConfig, ChapterConfig

    def _make(chapters=None, **kw):
        chapters = chapters if chapters is not None else [
            ChapterConfig(chapter_number=1, chapter_title="C1",
                          chapter_scope="scope one", trivia_count=3, fact_count=2),
        ]
        return BookConfig(
            book_title=kw.pop("book_title", "Test Book"),
            topic=kw.pop("topic", "testing"),
            illustrations=kw.pop("illustrations", False),
            chapters=chapters,
            **kw,
        )

    return _make


@pytest.fixture
def trivia_builder(trivia_cfg):
    """A TriviaBuilder whose provider seam is a scripted fake.

    Returns (builder, calls) where `calls` records every prompt sent, so tests
    can assert on batch sizes and on whether an identical prompt was resent --
    the signature of the cache-replay deadlock.
    """
    from trivia import pipeline

    def _make(cfg=None, supply=None, collide=None):
        cfg = cfg or trivia_cfg()
        b = pipeline.TriviaBuilder(cfg)
        calls: list[str] = []

        # `supply` caps how many distinct items the fake scope can yield, which
        # is how a genuine "scope exhausted" shortfall is simulated.
        budget = {"left": supply if supply is not None else 10_000}

        def fake_ask(prompt: str):
            calls.append(prompt)
            n = _requested_count(prompt)
            take = min(n, budget["left"])
            budget["left"] -= take
            seq = len(calls)
            if _is_trivia_prompt(prompt):
                return [
                    {
                        "question": f"Q{seq}_{i}?",
                        "choices": {"A": "a", "B": "b", "C": "c", "D": "d"},
                        "correct_answer": "ABCD"[i % 4],
                        "fact_seed": f"seed{seq}_{i}",
                    }
                    for i in range(take)
                ]
            return [{"fact": f"Fact {seq}_{i} about the subject"} for i in range(take)]

        b._generate_batch = fake_ask
        b.checker.find_collisions = collide or (lambda *a, **k: set())
        return b, calls, budget

    return _make


def _is_trivia_prompt(prompt: str) -> bool:
    """The two prompts are distinguished the way the real ones differ."""
    return "multiple-choice trivia questions" in prompt


def _requested_count(prompt: str) -> int:
    m = re.search(r"Write (\d+) ", prompt)
    return int(m.group(1)) if m else 1


@pytest.fixture
def make_chapter():
    """A Chapter prefilled with n valid questions and m valid facts."""
    from trivia.engine import DidYouKnowFact, TriviaQuestion
    from trivia.pipeline import Chapter

    def _make(number=1, questions=0, facts=0, title="C", scope="s"):
        ch = Chapter(number=number, title=title, scope=scope)
        for i in range(questions):
            ch.trivia.append(TriviaQuestion(
                id=f"ch{number}_q{i + 1:02d}", chapter=number,
                question=f"Q{i}?",
                choices={"A": "a", "B": "b", "C": "c", "D": "d"},
                correct_answer="ABCD"[i % 4], fact_seed=f"s{number}_{i}",
            ))
        for i in range(facts):
            ch.facts.append(DidYouKnowFact(
                id=f"ch{number}_f{i + 1:03d}", chapter=number,
                fact=f"Fact {i} about chapter {number}",
            ))
        return ch

    return _make


# -- puzzle ---------------------------------------------------------------

@pytest.fixture
def rejection_stdout():
    """A provider-rejection envelope, byte-shaped like a real one.

    This is the exact failure seen in production: a well-formed JSON reply
    whose payload text is an error sentence rather than content.
    """
    import json

    def _make(text="LLM request failed: provider rejected the request schema "
                   "or tool payload."):
        return json.dumps({
            "status": "ok",
            "result": {"payloads": [{"text": text, "mediaUrl": None}]},
        })

    return _make


@pytest.fixture
def good_stdout():
    import json

    def _make(text='["CAMERA","LENS","SHUTTER"]'):
        return json.dumps({
            "status": "ok",
            "result": {"payloads": [{"text": text, "mediaUrl": None}]},
        })

    return _make
