"""Agents: the four LLM roles + the shared context/action contract."""
from __future__ import annotations

from codeswarm.agents.base import Agent, AgentAction, AgentContext
from codeswarm.agents.planner import PlannerAgent
from codeswarm.agents.coder import CoderAgent
from codeswarm.agents.tester import TesterAgent
from codeswarm.agents.reviewer import ReviewerAgent

__all__ = [
    "Agent",
    "AgentAction",
    "AgentContext",
    "PlannerAgent",
    "CoderAgent",
    "TesterAgent",
    "ReviewerAgent",
]
