"""Tester agent: run the task's tests and interpret pass/fail.

The tester interprets pass/fail deterministically from the structured
``run_tests`` ToolResult — no LLM call is needed, which keeps the loop
deterministic and replayable.
"""
from __future__ import annotations

from codeswarm.agents.base import AgentAction, AgentContext
from codeswarm.trace.types import ToolCall


class TesterAgent:
    name = "tester"

    async def run(self, ctx: AgentContext) -> AgentAction:
        tool = ctx.tools["run_tests"]
        result = tool.call({})
        tc = ToolCall(
            tool="run_tests", args={}, ok=result.ok, output=result.output,
            error=result.error, ms=0,
        )
        data = dict(result.data)
        ctx.recorder.append_event(
            kind="test",
            step_id=getattr(ctx.step, "id", "step"),
            agent=self.name,
            payload={
                "passed_all": data.get("passed_all", result.ok),
                "passed": data.get("passed", 0),
                "failed": data.get("failed", 0),
                "errors": data.get("errors", 0),
                "summary": data.get("summary", ""),
                "failing_tests": data.get("failing_tests", []),
                "error_kinds": data.get("error_kinds", []),
            },
        )
        return AgentAction(
            agent=self.name,
            text=data.get("summary", ""),
            tool_calls=[tc],
            data=data,
        )
