"""Spec agent: turn a free-form task into a concrete pytest oracle.

A task a user just invented ("build a rate limiter") has no test file. The spec
agent writes one — encoding the acceptance criteria as pytest tests that import
the implementation module the coder will create. That generated test file becomes
the authoritative verdict for the run (exactly like a builtin task's oracle).

Convention (kept simple + predictable so the coder knows the target):
  * implementation lives in ``solution.py``
  * tests live in ``test_solution.py`` and ``from solution import ...``
"""
from __future__ import annotations

from codeswarm.agents.base import AgentAction, AgentContext
from codeswarm.trace.types import ToolCall

_SYSTEM = (
    "You are the SPEC agent in a multi-agent coding system. Given a coding task in "
    "plain language, write a THOROUGH pytest test file that pins down the acceptance "
    "criteria — normal cases AND edge cases. The implementation will live in a module "
    "named `solution`, so your tests must `from solution import ...`. Write the tests "
    "with the write_file tool to path 'test_solution.py'. Use plain `assert` (and "
    "`import pytest` only if you need pytest.raises). Do NOT implement the solution — "
    "only the tests. Do not write any file other than test_solution.py."
)

_MAX_TOOL_TURNS = 4


class SpecAgent:
    name = "spec"

    async def run(self, ctx: AgentContext) -> AgentAction:
        task = ctx.task
        write = ctx.tools.get("write_file")
        specs = [write.spec] if write else []
        user = (
            f"Task:\n{getattr(task, 'prompt', '')}\n\n"
            "Write test_solution.py now (tests import from the `solution` module)."
        )
        messages: list[dict] = [{"role": "user", "content": user}]

        applied: list[ToolCall] = []
        last_text = ""
        wrote_tests = False

        for _turn in range(_MAX_TOOL_TURNS):
            resp = await ctx.llm.complete(_SYSTEM, messages, tools=specs)
            last_text = resp.text or last_text
            if not resp.tool_calls:
                break

            outputs: list[str] = []
            for call in resp.tool_calls:
                name = call.get("name", "")
                args = call.get("args", {}) or {}
                tool = ctx.tools.get(name)
                if tool is None:
                    tc = ToolCall(tool=name, args=args, ok=False, output="",
                                  error=f"unknown tool: {name}", ms=0)
                else:
                    result = tool.call(args)
                    tc = ToolCall(tool=name, args=args, ok=result.ok,
                                  output=result.output, error=result.error, ms=0)
                    if result.ok and name == "write_file":
                        path = args.get("path", "")
                        if path.startswith("test_"):
                            wrote_tests = True
                        try:
                            ctx.state.files[path] = ctx.workspace.read_file(path)
                        except Exception:  # noqa: BLE001
                            pass
                applied.append(tc)
                ctx.recorder.append_event(
                    kind="tool", step_id="spec", agent=self.name,
                    payload={"tool": tc.tool, "args": tc.args, "ok": tc.ok, "error": tc.error},
                )
                outputs.append(f"{name}({args.get('path', '')}) -> ok={tc.ok} {tc.error or ''}")

            if wrote_tests:
                break
            messages.append({"role": "assistant", "content": last_text or "(writing tests)"})
            messages.append({"role": "user", "content": "Results:\n" + "\n".join(outputs)
                             + "\n\nWrite test_solution.py with write_file now."})

        ctx.recorder.append_event(
            kind="agent", step_id="spec", agent=self.name,
            payload={"wrote_tests": wrote_tests, "summary": last_text[:500]},
        )
        return AgentAction(agent=self.name, text=last_text, tool_calls=applied,
                           data={"wrote_tests": wrote_tests})
