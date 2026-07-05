"""Coder agent: propose a code edit via tools (write_file / apply_patch / read_file)."""
from __future__ import annotations

from codeswarm.agents.base import AgentAction, AgentContext
from codeswarm.trace.types import ToolCall

_SYSTEM = (
    "You are the CODER agent in a multi-agent coding system. Edit files in the "
    "workspace so the task's tests pass. Use the provided tools (write_file, "
    "read_file, apply_patch) to make changes. Make the smallest change that "
    "satisfies the tests."
)

# Tools the coder is allowed to drive.
_CODER_TOOL_NAMES = ("read_file", "write_file", "apply_patch", "list_dir")


class CoderAgent:
    name = "coder"

    async def run(self, ctx: AgentContext) -> AgentAction:
        task = ctx.task
        specs = [
            ctx.tools[n].spec for n in _CODER_TOOL_NAMES if n in ctx.tools
        ]
        hint_block = f"\n\nReviewer hint from the last attempt:\n{ctx.hint}" if ctx.hint else ""
        user = (
            f"Task prompt:\n{getattr(task, 'prompt', '')}\n\n"
            f"Files to implement: {list(getattr(task, 'files', {}) or {})}\n"
            f"Current step: {getattr(ctx.step, 'description', '')}"
            f"{hint_block}\n\nMake the edits now using the tools."
        )
        resp = await ctx.llm.complete(
            _SYSTEM, [{"role": "user", "content": user}], tools=specs
        )

        applied: list[ToolCall] = []
        for call in resp.tool_calls:
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            tool = ctx.tools.get(name)
            if tool is None:
                tc = ToolCall(
                    tool=name, args=args, ok=False, output="",
                    error=f"unknown tool: {name}", ms=0,
                )
            else:
                result = tool.call(args)
                tc = ToolCall(
                    tool=name, args=args, ok=result.ok, output=result.output,
                    error=result.error, ms=0,
                )
                # Keep the state file-mirror in sync for checkpoints.
                if result.ok and name in ("write_file", "apply_patch"):
                    path = args.get("path")
                    if path:
                        try:
                            ctx.state.files[path] = ctx.workspace.read_file(path)
                        except Exception:  # noqa: BLE001
                            pass
            applied.append(tc)
            ctx.recorder.append_event(
                kind="tool",
                step_id=getattr(ctx.step, "id", "step"),
                agent=self.name,
                payload={
                    "tool": tc.tool, "args": tc.args, "ok": tc.ok,
                    "error": tc.error,
                },
            )

        return AgentAction(agent=self.name, text=resp.text, tool_calls=applied)
