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

    # A real model typically READS the files before it WRITES, which takes more
    # than one turn. Bound the tool loop so the coder keeps going (feeding tool
    # outputs back) until it makes an edit, stops asking for tools, or hits the cap.
    MAX_TOOL_TURNS = 5

    async def run(self, ctx: AgentContext) -> AgentAction:
        task = ctx.task
        specs = [
            ctx.tools[n].spec for n in _CODER_TOOL_NAMES if n in ctx.tools
        ]
        hint_block = f"\n\nReviewer hint from the last attempt:\n{ctx.hint}" if ctx.hint else ""
        # Show what's already in the workspace so the coder can discover the
        # acceptance tests (esp. for free-form tasks where the spec agent wrote
        # test_solution.py and the impl target is inferred from its imports).
        try:
            ws_files = ctx.workspace.list_dir(".")
        except Exception:  # noqa: BLE001
            ws_files = []
        user = (
            f"Task prompt:\n{getattr(task, 'prompt', '')}\n\n"
            f"Files currently in the workspace: {ws_files}\n"
            f"Files to implement: {list(getattr(task, 'files', {}) or {})}\n"
            f"Current step: {getattr(ctx.step, 'description', '')}"
            f"{hint_block}\n\n"
            "READ the test_*.py file(s) to learn the exact required interface "
            "(function/class names, signatures), then write_file the implementation "
            "module they import so every test passes. Do NOT edit the test files."
        )
        messages: list[dict] = [{"role": "user", "content": user}]

        applied: list[ToolCall] = []
        last_text = ""
        wrote = False

        for _turn in range(self.MAX_TOOL_TURNS):
            resp = await ctx.llm.complete(_SYSTEM, messages, tools=specs)
            last_text = resp.text or last_text
            if not resp.tool_calls:
                break  # model is done (produced only text / nothing to run)

            outputs: list[str] = []
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
                    if result.ok and name in ("write_file", "apply_patch"):
                        wrote = True
                        # Keep the state file-mirror in sync for checkpoints.
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
                outputs.append(
                    f"{name}({args}) -> ok={tc.ok}\n{(tc.output or tc.error or '')[:2000]}"
                )

            if wrote:
                break  # made an edit; let the tester run the suite

            # Feed the tool results back as plain text (we intentionally do NOT
            # replay raw tool_use blocks — a clean text history keeps the loop
            # provider-agnostic and avoids the tool_use/tool_result id protocol).
            messages.append({"role": "assistant", "content": last_text or "(reading files)"})
            messages.append(
                {
                    "role": "user",
                    "content": "Tool results:\n"
                    + "\n\n".join(outputs)
                    + "\n\nNow WRITE the implementation with write_file so the tests pass.",
                }
            )

        return AgentAction(agent=self.name, text=last_text, tool_calls=applied)
