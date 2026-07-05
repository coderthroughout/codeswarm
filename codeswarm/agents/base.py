"""Agent Protocol, AgentAction, and AgentContext (see DESIGN.md "Agents")."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from codeswarm.llm.client import LLMClient
from codeswarm.trace.recorder import TrajectoryRecorder
from codeswarm.tools.base import Tool


@dataclass
class AgentContext:
    """Everything an agent needs: shared state + the llm + the tools.

    Also carries the per-step working fields (current step, attempt number, and
    the reviewer's latest hint) so the code->test->review loop can thread context.
    """

    state: Any                    # WorkflowState (Any to avoid an import cycle)
    llm: LLMClient
    tools: dict[str, Tool]
    workspace: Any                # Workspace
    recorder: TrajectoryRecorder
    task: Any = None              # Task
    step: Any = None              # current Step
    attempt: int = 0              # 1-based attempt within the current step
    hint: str | None = None       # latest reviewer hint (fed back to the coder)
    last_failure: str | None = None  # latest failing test summary (fed to reviewer)


@dataclass
class AgentAction:
    """What an agent did on one turn."""

    agent: str
    text: str = ""
    tool_calls: list = field(default_factory=list)  # ToolCall records applied
    data: dict = field(default_factory=dict)        # structured output (plan/hint/...)


@runtime_checkable
class Agent(Protocol):
    name: str

    async def run(self, ctx: AgentContext) -> AgentAction:
        ...
