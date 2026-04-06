from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loony_dev.agents.base import Agent
from loony_dev.agents.coding import CodingAgent
from loony_dev.agents.null_agent import NullAgent
from loony_dev.agents.planning import PlanningAgent
from loony_dev.plugins.base import AgentPlugin

if TYPE_CHECKING:
    from loony_dev.config import Settings


class ClaudeAgentPlugin(AgentPlugin):
    """Built-in agent plugin that registers the Claude Code and planning agents."""

    @property
    def name(self) -> str:
        return "claude"

    def create_agents(self, work_dir: Path, settings: Settings) -> list[Agent]:
        return [
            NullAgent(),
            CodingAgent(work_dir=work_dir),
            PlanningAgent(work_dir=work_dir),
        ]
