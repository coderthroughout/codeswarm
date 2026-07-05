"""Reviewer agent: diagnose a test failure and propose a correction hint."""
from __future__ import annotations

from codeswarm.agents.base import AgentAction, AgentContext

_SYSTEM = (
    "You are the REVIEWER agent in a multi-agent coding system. Given a failing "
    "test run, diagnose the likely cause and propose a concise, actionable hint "
    "the coder can act on next. Reply with the hint text only."
)


class ReviewerAgent:
    name = "reviewer"

    async def run(self, ctx: AgentContext) -> AgentAction:
        failure = ctx.last_failure or ""
        user = (
            f"Task prompt:\n{getattr(ctx.task, 'prompt', '')}\n\n"
            f"Failing test summary:\n{failure or 'tests failed'}\n\n"
            "Give the coder one concrete hint to fix it."
        )
        resp = await ctx.llm.complete(_SYSTEM, [{"role": "user", "content": user}], tools=None)
        hint = resp.text.strip() or "Re-check the implementation against the tests."
        ctx.recorder.append_event(
            kind="agent",
            step_id=getattr(ctx.step, "id", "step"),
            agent=self.name,
            payload={"hint": hint},
        )
        return AgentAction(agent=self.name, text=hint, data={"hint": hint})
