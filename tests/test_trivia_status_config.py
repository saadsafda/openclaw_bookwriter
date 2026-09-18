"""The status payload must carry the book's own config.

The resolve button posts the browser's chapter rows back as config overrides.
If the form is not first loaded with the book being resolved, those rows are
whatever was last typed -- possibly another book's, or the blank 50/100
template -- and resolving silently rewrites the book's quotas. The server side
of that guard is exposing the real config on the status payload.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from flask import Flask

from trivia import routes
from trivia.engine import BookConfig, ChapterConfig
from trivia.routes import TriviaJob


TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "trivia.html"


@pytest.fixture
def cfg():
    return BookConfig(
        book_title="B", topic="t",
        chapters=[ChapterConfig(chapter_number=1, chapter_title="C",
                                chapter_scope="s", trivia_count=15,
                                fact_count=20)])


class TestLiveJobStatus:
    def test_status_includes_the_config(self, cfg):
        job = TriviaJob(id="abc", config=cfg)
        status = job.to_status()
        assert "config" in status, (
            "without the config the browser cannot load the book's real "
            "chapter counts before resolving")

    def test_config_carries_the_real_counts(self, cfg):
        status = TriviaJob(id="abc", config=cfg).to_status()
        ch = status["config"]["chapters"][0]
        assert ch["trivia_count"] == 15
        assert ch["fact_count"] == 20

    def test_config_is_json_serialisable(self, cfg):
        """It is returned through jsonify, so it must survive a round trip."""
        status = TriviaJob(id="abc", config=cfg).to_status()
        assert json.loads(json.dumps(status))["config"]["chapters"][0][
            "trivia_count"] == 15


class TestPersistedBookStatus:
    """A book reopened from the library has no live job, so it reads the DB."""

    def test_status_includes_config_from_the_database(self, cfg, monkeypatch):
        app = Flask(__name__)
        routes.register(app)

        row = {
            "status": "error", "stage": "validation-failed", "progress": 0.32,
            "title": "B", "topic": "t", "error": "blocked",
            "json_path": "/tmp/x.json",
            "warnings_json": "[]", "usage_json": "{}",
            "config_json": json.dumps(cfg.to_dict()),
        }
        monkeypatch.setattr(routes.bookdb, "get_trivia_book", lambda _id: row)

        with app.test_client() as c:
            data = c.get("/api/trivia/jobs/deadbeef/status").get_json()

        assert data["config"] is not None
        assert data["config"]["chapters"][0]["trivia_count"] == 15

    def test_missing_config_is_null_not_an_error(self, monkeypatch):
        """Books saved before configs were stored must still open."""
        app = Flask(__name__)
        routes.register(app)
        row = {"status": "error", "stage": "validation-failed", "progress": 0,
               "title": "B", "topic": "t", "error": "",
               "warnings_json": "[]", "usage_json": "{}", "config_json": ""}
        monkeypatch.setattr(routes.bookdb, "get_trivia_book", lambda _id: row)

        with app.test_client() as c:
            resp = c.get("/api/trivia/jobs/deadbeef/status")
        assert resp.status_code == 200
        assert resp.get_json()["config"] is None


class TestBrowserGuard:
    """The template must refuse to resolve with a form it cannot vouch for.

    Asserted against the template source: this is the one place the guard
    lives, and losing it silently reintroduces the corruption.
    """

    @pytest.fixture
    def source(self):
        return TEMPLATE.read_text(encoding="utf-8")

    def test_tracks_which_book_the_form_describes(self, source):
        assert "formBookId" in source

    def test_resolve_checks_the_form_matches_the_book(self, source):
        assert re.search(r"formBookId\s*!==\s*resolveJobId", source), (
            "resolve does not verify the form belongs to the book being "
            "resolved, so it can post another book's chapter counts")

    def test_applyconfig_stamps_the_book_id(self, source):
        assert re.search(r"function applyConfig\(cfg,\s*bookId", source)
        assert re.search(r"formBookId\s*=\s*bookId", source)

    def test_status_config_is_captured_for_resolve(self, source):
        assert re.search(r"resolveConfig\s*=\s*s\.config", source)
