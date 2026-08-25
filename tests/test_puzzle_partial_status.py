"""A build that produced nothing must not report success.

In production a provider outage emptied almost every section, yet the job
still finished as "done / 100%" with a green badge. The counts were on screen
and the warnings were in the log, but the one signal an operator actually
reads -- the status -- said the book was fine.
"""

from __future__ import annotations

import pytest

from puzzle import routes
from puzzle.engine import (SECTION_CROSSWORDS, SECTION_MAZES, SECTION_RIDDLES,
                           BookConfig, SectionConfig)
from puzzle.pipeline import PuzzleBook


@pytest.fixture
def run_build(monkeypatch, tmp_path):
    """Drive the real _run_build with the model and exporters stubbed out."""
    def _make(requested, produced):
        cfg = BookConfig(
            book_title="B", topic="birds",
            sections={k: SectionConfig(kind=k, enabled=True, count=n)
                      for k, n in requested.items()})

        job = routes.PuzzleJob(id="job1", config=cfg)
        monkeypatch.setitem(routes.JOBS, "job1", job)
        monkeypatch.setattr(routes, "_job_dir", lambda _id: tmp_path)
        monkeypatch.setattr(routes.bookdb, "update_puzzle_book",
                            lambda *a, **k: None)

        book = PuzzleBook(config=cfg)
        monkeypatch.setattr(book, "counts", lambda: dict(produced))

        class _Builder:
            def __init__(self, *a, **k): pass
            def build(self, out_dir): return book

        monkeypatch.setattr(routes.pipeline, "PuzzleBuilder", _Builder)
        monkeypatch.setattr(routes.pipeline, "write_json",
                            lambda b, p: p)
        for name in ("write_markdown", "build_docx", "build_interior_docx",
                     "build_handoff_zip"):
            monkeypatch.setattr(routes.exporter, name,
                                lambda *a, **k: a[-1])
        monkeypatch.setattr(routes.exporter, "verify_print_images",
                            lambda b, d: [])
        monkeypatch.setattr(routes.exporter, "build_kdp_files",
                            lambda *a, **k: {"kindle": "k", "paperback": "p",
                                             "estimated_pages": 10})

        routes._run_build("job1")
        return job
    return _make


class TestEmptyBuildIsNotDone:
    def test_outage_shaped_build_reports_partial(self, run_build):
        """What production produced: 12 mazes, 6 trivia, nothing else."""
        job = run_build(
            requested={SECTION_MAZES: 12, SECTION_CROSSWORDS: 12,
                       SECTION_RIDDLES: 12},
            produced={SECTION_MAZES: 12, SECTION_CROSSWORDS: 0,
                      SECTION_RIDDLES: 0})
        assert job.status == "partial", (
            f"a build missing two whole sections reported {job.status!r}")

    def test_shortfall_is_named_in_the_warnings(self, run_build):
        job = run_build(
            requested={SECTION_MAZES: 12, SECTION_CROSSWORDS: 12},
            produced={SECTION_MAZES: 12, SECTION_CROSSWORDS: 0})
        assert job.warnings, "an incomplete book carried no warning at all"
        top = job.warnings[0].lower()
        assert "incomplete" in top and "0/12" in job.warnings[0]

    def test_one_short_still_counts_as_partial(self, run_build):
        job = run_build(requested={SECTION_MAZES: 12},
                        produced={SECTION_MAZES: 11})
        assert job.status == "partial"


class TestCompleteBuildStillReportsDone:
    def test_full_counts_are_done(self, run_build):
        job = run_build(requested={SECTION_MAZES: 12, SECTION_RIDDLES: 6},
                        produced={SECTION_MAZES: 12, SECTION_RIDDLES: 6})
        assert job.status == "done"
        assert job.progress == 1.0

    def test_disabled_section_does_not_force_partial(self, run_build, monkeypatch):
        """A section the operator turned off is not a shortfall."""
        job = run_build(requested={SECTION_MAZES: 12},
                        produced={SECTION_MAZES: 12, SECTION_CROSSWORDS: 0})
        assert job.status == "done", (
            "a disabled section was counted as missing content")
