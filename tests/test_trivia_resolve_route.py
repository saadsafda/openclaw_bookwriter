"""End-to-end resolve route.

The unit tests cover the pieces; this covers the wiring. A resolve that loads
the draft, applies overrides and validates -- but forgets to call the top-up --
still returns 409 forever, which is exactly what the button did in production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from flask import Flask

from trivia import pipeline, routes
from trivia.engine import BookConfig, ChapterConfig
from trivia.pipeline import TriviaBook


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    """A resolve endpoint backed by a real draft on disk and a fake DB row."""
    def _make(questions, trivia_count, facts=2, fact_count=2, number=6,
              supply=10_000):
        cfg = BookConfig(
            book_title="Blocked", topic="t", illustrations=False,
            chapters=[ChapterConfig(chapter_number=number, chapter_title="C",
                                    chapter_scope="s",
                                    trivia_count=trivia_count,
                                    fact_count=fact_count)])
        book = TriviaBook(config=cfg)
        from trivia.engine import DidYouKnowFact, TriviaQuestion
        from trivia.pipeline import Chapter
        ch = Chapter(number=number, title="C", scope="s")
        for i in range(questions):
            ch.trivia.append(TriviaQuestion(
                id=f"ch{number}_q{i + 1:02d}", chapter=number, question=f"Q{i}?",
                choices={"A": "a", "B": "b", "C": "c", "D": "d"},
                correct_answer="ABCD"[i % 4], fact_seed=f"s{i}"))
        for i in range(facts):
            ch.facts.append(DidYouKnowFact(
                id=f"ch{number}_f{i + 1:03d}", chapter=number,
                fact=f"Fact {i} about things"))
        book.chapters.append(ch)

        json_path = tmp_path / "draft.json"
        pipeline.write_json(book, json_path)

        row = {"id": "bk1", "title": "Blocked", "json_path": str(json_path),
               "usage_json": "{}", "config_json": json.dumps(cfg.to_dict())}
        saved: dict = {}
        monkeypatch.setattr(routes.bookdb, "get_trivia_book", lambda _i: row)
        monkeypatch.setattr(routes.bookdb, "update_trivia_book",
                            lambda _i, **kw: saved.update(kw))

        # Keep the provider out of it; the fake yields fresh items on demand.
        # Questions and facts get separate budgets so a test that starves one
        # does not accidentally starve the other.
        budget = {"trivia": supply, "facts": 10_000}
        seq = {"n": 0}

        def fake_batch(self, prompt):
            seq["n"] += 1
            import re
            m = re.search(r"Write (\d+) ", prompt)
            n = int(m.group(1)) if m else 1
            is_trivia = "multiple-choice trivia questions" in prompt
            slot = "trivia" if is_trivia else "facts"
            take = min(n, budget[slot])
            budget[slot] -= take
            if is_trivia:
                return [{"question": f"Unique question {seq['n']}-{i} here?",
                         "choices": {"A": f"a{seq['n']}{i}", "B": f"b{seq['n']}{i}",
                                     "C": f"c{seq['n']}{i}", "D": f"d{seq['n']}{i}"},
                         "correct_answer": "ABCD"[i % 4],
                         "fact_seed": f"newseed{seq['n']}x{i}"} for i in range(take)]
            return [{"fact": f"Unique fact {seq['n']}-{i} about distinct things"}
                    for i in range(take)]

        monkeypatch.setattr(pipeline.TriviaBuilder, "_generate_batch", fake_batch)
        monkeypatch.setattr(pipeline.DedupChecker, "find_collisions",
                            lambda self, a, b, k: set())
        # Exports are exercised elsewhere; keep this test about the gate.
        monkeypatch.setattr(routes.exporter, "write_markdown",
                            lambda b, p: Path(p))
        monkeypatch.setattr(routes.exporter, "build_docx", lambda b, p: Path(p))
        monkeypatch.setattr(routes.exporter, "verify_print_images",
                            lambda b, d: [])
        monkeypatch.setattr(routes.exporter, "build_kdp_files",
                            lambda b, d, o: {"kindle": "k", "paperback": "p"})

        app = Flask(__name__)
        routes.register(app)
        return app.test_client(), saved, json_path

    return _make


class TestResolveFinishesAShortChapter:
    def test_a_chapter_short_on_questions_is_topped_up(self, app_client):
        """The production case: 14 of 15, resolve must finish it."""
        client, saved, json_path = app_client(questions=14, trivia_count=15)
        resp = client.post("/api/trivia/books/bk1/resolve", json={})

        assert resp.status_code == 200, resp.get_json()
        book = pipeline.load_json(json_path)
        assert len(book.chapters[0].trivia) == 15
        assert saved.get("status") == "done"

    def test_a_chapter_with_no_questions_is_rebuilt(self, app_client):
        """A draft saved by the old code shows 0; resolve must still finish."""
        client, saved, json_path = app_client(questions=0, trivia_count=15)
        resp = client.post("/api/trivia/books/bk1/resolve", json={})

        assert resp.status_code == 200, resp.get_json()
        assert len(pipeline.load_json(json_path).chapters[0].trivia) == 15

    def test_lowering_the_count_finishes_without_generating(self, app_client):
        """The other escape hatch, end to end."""
        client, saved, json_path = app_client(questions=14, trivia_count=15)
        resp = client.post("/api/trivia/books/bk1/resolve", json={
            "chapters": [{"chapter_number": 6, "trivia_count": 14,
                          "fact_count": 2}]})

        assert resp.status_code == 200, resp.get_json()
        book = pipeline.load_json(json_path)
        assert len(book.chapters[0].trivia) == 14
        assert saved.get("status") == "done"

    def test_reports_when_the_scope_cannot_be_filled(self, app_client):
        """An unfillable chapter must say so, not fail silently."""
        client, _, _ = app_client(questions=14, trivia_count=15, supply=0)
        resp = client.post("/api/trivia/books/bk1/resolve", json={})

        assert resp.status_code == 409
        assert "14 of 15" in resp.get_json()["error"]

    def test_progress_is_saved_even_when_still_blocked(self, app_client):
        """Content paid for during a failed resolve must not be discarded."""
        client, _, json_path = app_client(questions=10, trivia_count=15,
                                          supply=3)
        client.post("/api/trivia/books/bk1/resolve", json={})

        book = pipeline.load_json(json_path)
        assert len(book.chapters[0].trivia) == 13, (
            "the questions this pass generated were thrown away, so the next "
            "resolve would have to buy them again")


class TestResolvePreconditions:
    def test_missing_draft_is_a_clear_error(self, monkeypatch):
        app = Flask(__name__)
        routes.register(app)
        monkeypatch.setattr(routes.bookdb, "get_trivia_book",
                            lambda _i: {"id": "x", "json_path": "/no/such.json"})
        with app.test_client() as c:
            resp = c.post("/api/trivia/books/x/resolve", json={})
        assert resp.status_code == 400
        assert "no saved draft" in resp.get_json()["error"].lower()

    def test_unknown_book_is_404(self, monkeypatch):
        app = Flask(__name__)
        routes.register(app)
        monkeypatch.setattr(routes.bookdb, "get_trivia_book", lambda _i: None)
        with app.test_client() as c:
            assert c.post("/api/trivia/books/x/resolve", json={}).status_code == 404
