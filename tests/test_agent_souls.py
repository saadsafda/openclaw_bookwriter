"""The per-agent SOUL files must stay consistent with what the code enforces.

These files shape craft, not hard constraints — the Python prompt builders and
validators own those. But where a SOUL file *does* state a rule, it must not
contradict the code, or the agent is told one thing and graded on another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SOULS = Path(__file__).resolve().parent.parent / "agent_souls"
FILES = {
    "writer": SOULS / "writer.md",
    "trivia": SOULS / "trivia.md",
    "stories": SOULS / "stories.md",
    "puzzle": SOULS / "puzzle.md",
}


@pytest.fixture(scope="module")
def text() -> dict[str, str]:
    return {k: p.read_text(encoding="utf-8") for k, p in FILES.items()}


class TestFilesExist:
    @pytest.mark.parametrize("name", sorted(FILES))
    def test_present_and_substantial(self, name):
        p = FILES[name]
        assert p.is_file(), f"{p} is missing"
        assert len(p.read_text(encoding="utf-8")) > 1500

    def test_deploy_script_is_present(self):
        assert (SOULS / "deploy.py").is_file()

    def test_readme_explains_the_split(self):
        readme = (SOULS / "README.md").read_text(encoding="utf-8")
        assert "authoritative" in readme.lower()


class TestAgreesWithTheCode:
    """A SOUL file that contradicts a validator trains the wrong behaviour."""

    def test_paragraph_minimum_matches_the_validator(self, text):
        from stories import engine as se
        assert se.MIN_SENTENCES_PER_PARAGRAPH == 2
        # Both prose agents must state the same floor the gate enforces.
        for name in ("writer", "stories"):
            assert "at least two complete sentences" in text[name].lower(), name

    def test_closing_paragraph_exemption_is_stated(self, text):
        for name in ("writer", "stories"):
            body = text[name].lower()
            assert "final paragraph" in body, name

    def test_story_word_band_matches_the_config(self, text):
        from stories import engine as se
        assert (se.DEFAULT_MIN_WORDS, se.DEFAULT_MAX_WORDS) == (300, 500)
        assert "300 to 500 words" in text["stories"]

    def test_stories_lists_the_banned_openers(self, text):
        from stories import engine as se
        body = text["stories"].lower()
        for phrase in se.BANNED_OPENERS:
            assert phrase.strip() in body, f"{phrase!r} not documented"

    def test_writer_word_target_matches_the_prompt(self, text):
        import openclaw_docx_writer as dw
        block = dw._paragraph_break_block(250, 320)
        assert "4 or 5 paragraphs" in block
        assert "250 to 320 words" in text["writer"]

    def test_puzzle_batch_sizes_match_the_engine(self, text):
        from puzzle import engine as pe
        assert pe.RIDDLE_BATCH == 10 and pe.CRYPTOGRAM_BATCH == 10
        assert pe.TRIVIA_QUESTIONS_PER_CHAPTER == 10
        body = text["puzzle"]
        assert "ten at a time" in body
        assert "Ten questions per chapter" in body

    def test_puzzle_word_counts_match_the_engine(self, text):
        from puzzle import engine as pe
        assert pe.WORDS_PER_SEARCH == 9 and pe.WORDS_PER_CROSSWORD == 6
        assert "nine for a search and six for a crossword" in text["puzzle"]

    def test_trivia_batch_sizes_match_the_engine(self, text):
        from trivia import engine as te
        assert te.TRIVIA_BATCH == 12 and te.FACT_BATCH == 20
        assert "batches of 12" in text["trivia"]
        assert "facts in batches of 20" in text["trivia"]

    def test_page_size_is_right(self, text):
        from puzzle import engine as pe
        assert (pe.PAGE_W_IN, pe.PAGE_H_IN) == (6.0, 9.0)
        assert "6x9" in text["puzzle"]


class TestNoSelfContradiction:
    """The defect the client reported came from a rule that asked for it."""

    @pytest.mark.parametrize("name", sorted(FILES))
    def test_never_invites_a_lone_sentence_paragraph(self, name, text):
        body = text[name].lower()
        # The exact phrasing that caused the original bug.
        assert "one full sentence standing alone" not in body
        assert "one complete sentence standing alone" not in body

    @pytest.mark.parametrize("name", sorted(FILES))
    def test_no_em_dashes_in_the_prose(self, name, text):
        """These files ban dashes as punctuation, so their prose must not use
        them. The H1 title is a heading, not prose, and is exempt."""
        body = [ln for ln in text[name].splitlines() if not ln.startswith("# ")]
        offenders = [ln for ln in body if "\u2014" in ln]
        assert not offenders, f"{name} uses an em dash it forbids: {offenders[:2]}"

    @pytest.mark.parametrize("name", sorted(FILES))
    def test_names_the_agent_it_deploys_to(self, name, text):
        assert "Deployed to:" in text[name]


class TestDeployScript:
    def test_every_agent_maps_to_a_real_source_file(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("deploy", SOULS / "deploy.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for filename in mod.DEPLOYMENTS:
            assert (SOULS / filename).is_file(), filename

    def test_covers_every_pipeline_default_agent(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("deploy", SOULS / "deploy.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        deployed = {a for agents in mod.DEPLOYMENTS.values() for a in agents}
        from puzzle import engine as pe
        from stories import engine as se
        from trivia import engine as te
        for default in (te.DEFAULT_AGENT, pe.DEFAULT_AGENT, se.DEFAULT_AGENT):
            assert default in deployed, f"{default} has no SOUL.md"

    def test_dry_run_writes_nothing(self, tmp_path):
        import subprocess
        agent = tmp_path / "trivia-agent-1"
        agent.mkdir()
        (agent / "SOUL.md").write_text("ORIGINAL", encoding="utf-8")
        subprocess.run(
            ["python3", str(SOULS / "deploy.py"),
             "--workspace", str(tmp_path), "--dry-run"],
            capture_output=True, check=False,
        )
        assert (agent / "SOUL.md").read_text(encoding="utf-8") == "ORIGINAL"
        assert not list(agent.glob("SOUL.md.bak-*"))
