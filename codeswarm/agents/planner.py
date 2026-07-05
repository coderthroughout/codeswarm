"""Planner agent: produce an ordered plan of concrete steps for the task."""
from __future__ import annotations

import json

from codeswarm.agents.base import AgentAction, AgentContext
from codeswarm.workflow.state import Step

_SYSTEM = (
    "You are the PLANNER agent in a multi-agent coding system. Produce a short, "
    "ordered plan of concrete steps that will make the task's tests pass. Reply "
    "with STRICT JSON of the form: "
    '{"plan": [{"id": "step-1", "description": "..."}]}. No prose outside the JSON.'
)


def _parse_plan(text: str) -> list[Step]:
    try:
        obj = json.loads(text)
        raw = obj.get("plan", [])
    except (json.JSONDecodeError, AttributeError):
        raw = []
    steps: list[Step] = []
    for i, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            steps.append(
                Step(
                    id=str(item.get("id") or f"step-{i}"),
                    description=str(item.get("description") or ""),
                )
            )
    if not steps:
        # Robust fallback so the engine always has at least one step to run.
        steps = [Step(id="step-1", description="Implement the solution so tests pass.")]
    return steps


class PlannerAgent:
    name = "planner"

    async def run(self, ctx: AgentContext) -> AgentAction:
        task = ctx.task
        user = (
            f"Task id: {getattr(task, 'id', '?')}\n"
            f"Prompt:\n{getattr(task, 'prompt', '')}\n\n"
            f"Solution files to edit: {list(getattr(task, 'files', {}) or {})}\n"
            "Return the JSON plan now."
        )
        resp = await ctx.llm.complete(_SYSTEM, [{"role": "user", "content": user}], tools=None)
        steps = _parse_plan(resp.text)
        ctx.recorder.append_event(
            kind="agent",
            step_id="plan",
            agent=self.name,
            payload={"plan": [{"id": s.id, "description": s.description} for s in steps]},
        )
        return AgentAction(agent=self.name, text=resp.text, data={"plan": steps})
