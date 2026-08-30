"""Story generation must ask for craft, not only forbid mistakes.

The stories prompt was 1,900 characters of almost entirely prohibitions: it told
the model what to avoid and never what to aim for. It also inherited the agent's
default model, which is tuned for reasoning and code rather than prose.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from stories import engine as se


@pytest.fixture
def prompt() -> str:
    cfg = se.BookConfig(book_title="Road Dogs", topic="dogs", tone="warm")
    st = se.StoryConfig.from_dict(
        {"title": "Bud Across America", "context": "1903 road trip"}, 1)
    return se.build_story_prompt(cfg, st)


class TestPromptTeachesCraft:
    def test_has_a_craft_section(self, prompt):
        assert "HOW TO WRITE IT:" in prompt

    def test_craft_comes_before_the_prohibitions(self, prompt):
        """Aim first, then constraints — not a wall of don'ts."""
        assert prompt.index("HOW TO WRITE IT:") < prompt.index("HARD REQUIREMENTS:")

    @pytest.mark.parametrize("topic,marker", [
        ("sentence variety", "Vary your sentences"),
        ("paragraph variety", "Vary your paragraphs"),
        ("endings", "Let the ending land"),
        ("specificity", "Use specific detail"),
        ("openings", "Open on something concrete"),
        ("restraint", "Trust the events"),
    ])
    def test_covers_the_craft_gaps(self, prompt, topic, marker):
        assert marker in prompt, f"no guidance on {topic}"

    def test_still_carries_every_hard_constraint(self, prompt):
        for rule in ("Between 300 and 500 words", "Real events only",
                     "at least 2 complete sentences", "Match the tone"):
            assert rule in prompt

    def test_bans_comma_splices_and_dashes(self, prompt):
        assert "only a comma" in prompt
        assert "dash as punctuation" in prompt


class TestModelSelection:
    def test_default_is_a_writing_model(self):
        assert se.DEFAULT_MODEL == "anthropic/claude-opus-4-6"

    def test_config_carries_the_default(self):
        cfg = se.BookConfig(book_title="B", topic="t")
        assert cfg.model == se.DEFAULT_MODEL

    def test_config_can_override_it(self):
        base = {"book_title": "B", "topic": "t", "stories": [{"title": "S"}]}
        cfg = se.BookConfig.from_dict({**base, "model": "openai/gpt-5.5-pro"})
        assert cfg.model == "openai/gpt-5.5-pro"

    def test_empty_string_defers_to_the_agent(self):
        """Explicitly blank means "use whatever the agent is configured with"."""
        base = {"book_title": "B", "topic": "t", "stories": [{"title": "S"}]}
        assert se.BookConfig.from_dict({**base, "model": ""}).model == ""

    def test_model_reaches_the_command_line(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            class Result:
                returncode = 0
                stdout = '{"reply": "ok"}'
                stderr = ""
            return Result()

        with patch.object(subprocess, "run", fake_run):
            try:
                se.call_openclaw_raw("stories-agent-1", "hi",
                                     model="anthropic/claude-opus-4-6")
            except Exception:
                pass
        cmd = captured.get("cmd", [])
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "anthropic/claude-opus-4-6"

    def test_no_model_flag_when_blank(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            class Result:
                returncode = 0
                stdout = '{"reply": "ok"}'
                stderr = ""
            return Result()

        with patch.object(subprocess, "run", fake_run):
            try:
                se.call_openclaw_raw("stories-agent-1", "hi", model="")
            except Exception:
                pass
        assert "--model" not in captured.get("cmd", [])


class TestEveryProseCallUsesTheModel:
    """Generation, rewrite, regenerate and add must all use the same model."""

    def test_pipeline_passes_it(self):
        src = (__import__("pathlib").Path("stories/pipeline.py")
               .read_text(encoding="utf-8"))
        assert "model=self.cfg.model" in src

    def test_all_three_edit_paths_pass_it(self):
        src = (__import__("pathlib").Path("stories/edit.py")
               .read_text(encoding="utf-8"))
        assert src.count("model=cfg.model") == 3

    def test_outline_deliberately_does_not(self):
        """Proposing researchable stories is recall, not prose."""
        src = (__import__("pathlib").Path("stories/pipeline.py")
               .read_text(encoding="utf-8"))
        assert "No model override here" in src
