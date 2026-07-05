"""Sandboxed shell tool: run a command with cwd pinned to the workspace root."""
from __future__ import annotations

import subprocess

from codeswarm.tools.base import ToolResult


class RunTool:
    name = "run"
    spec = {
        "name": "run",
        "description": (
            "Run a shell command inside the sandbox workspace and return its "
            "combined stdout/stderr and exit code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command line to execute (run via the shell).",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 60).",
                },
            },
            "required": ["command"],
        },
    }

    def __init__(self, workspace) -> None:
        self.workspace = workspace

    def call(self, args: dict) -> ToolResult:
        command = args.get("command", "")
        timeout = int(args.get("timeout", 60))
        if not command:
            return ToolResult(ok=False, output="", error="empty command")
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.workspace.root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            return ToolResult(
                ok=proc.returncode == 0,
                output=output,
                error=None if proc.returncode == 0 else f"exit code {proc.returncode}",
                data={"returncode": proc.returncode},
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                ok=False, output="", error=f"timed out after {timeout}s"
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output="", error=f"{type(exc).__name__}: {exc}")
