"""Each generator must default to its own OpenClaw agent.

All three defaulted to "main", so three concurrent books shared one agent --
one session store, one workspace, one set of credentials refreshing the same
OAuth token. The UI templates prefilled "main" too, so even after the engine
defaults changed a book built from the form would still have gone to the
shared agent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from puzzle.engine import DEFAULT_AGENT as PUZZLE_AGENT
from stories.engine import DEFAULT_AGENT as STORIES_AGENT
from trivia.engine import DEFAULT_AGENT as TRIVIA_AGENT

ROOT = Path(__file__).resolve().parent.parent
AGENTS = {"puzzle": PUZZLE_AGENT, "trivia": TRIVIA_AGENT,
          "stories": STORIES_AGENT}


class TestDefaultsAreDistinct:
    def test_no_two_generators_share_an_agent(self):
        assert len(set(AGENTS.values())) == 3, (
            f"generators share an agent: {AGENTS}")

    def test_none_fell_back_to_main(self):
        for name, agent in AGENTS.items():
            assert agent != "main", (
                f"{name} is back on the shared 'main' agent")


class TestTemplatesMatchTheEngine:
    """A prefilled input that disagrees with the engine silently wins."""

    @pytest.mark.parametrize("page,expected", [
        ("puzzle", PUZZLE_AGENT), ("trivia", TRIVIA_AGENT),
        ("stories", STORIES_AGENT)])
    def test_agent_field_and_js_fallbacks_use_the_right_agent(self, page, expected):
        html = (ROOT / "templates" / f"{page}.html").read_text(encoding="utf-8")

        values = re.findall(r'id="agent"[^>]*value="([^"]*)"', html)
        assert values, f"no agent input found in {page}.html"
        for v in values:
            assert v == expected, f"{page}.html prefills {v!r}, engine says {expected!r}"

        for fallback in re.findall(r'\|\|\s*"([^"]*agent[^"]*|main)"', html):
            assert fallback == expected, (
                f"{page}.html has a JS fallback to {fallback!r}")

    @pytest.mark.parametrize("page", ["puzzle", "trivia", "stories"])
    def test_no_hardcoded_main_remains(self, page):
        html = (ROOT / "templates" / f"{page}.html").read_text(encoding="utf-8")
        assert '"main"' not in html, (
            f"{page}.html still hardcodes the shared 'main' agent")


class TestSavedConfigsWereMigrated:
    """A saved config carries its own agent and overrides the new default."""

    @pytest.mark.parametrize("folder,expected", [
        ("puzzle_configs", PUZZLE_AGENT), ("story_configs", STORIES_AGENT)])
    def test_no_saved_config_still_points_at_main(self, folder, expected):
        for path in (ROOT / folder).glob("*.json"):
            agent = json.loads(path.read_text(encoding="utf-8")).get("agent")
            if agent:
                assert agent == expected, (
                    f"{path.name} still builds on {agent!r}")
