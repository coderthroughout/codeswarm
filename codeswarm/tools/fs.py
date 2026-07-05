"""Filesystem tools: read_file, write_file, apply_patch, list_dir.

All operations are confined to the sandbox workspace root via
``Workspace.resolve`` — no path can escape the sandbox.
"""
from __future__ import annotations

from codeswarm.tools.base import ToolResult


class ReadFileTool:
    name = "read_file"
    spec = {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path."}
            },
            "required": ["path"],
        },
    }

    def __init__(self, workspace) -> None:
        self.workspace = workspace

    def call(self, args: dict) -> ToolResult:
        path = args.get("path", "")
        try:
            content = self.workspace.read_file(path)
            return ToolResult(ok=True, output=content, data={"path": path})
        except Exception as exc:  # noqa: BLE001 - surface as a tool error
            return ToolResult(ok=False, output="", error=f"{type(exc).__name__}: {exc}")


class WriteFileTool:
    name = "write_file"
    spec = {
        "name": "write_file",
        "description": "Create or overwrite a UTF-8 text file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path."},
                "content": {"type": "string", "description": "Full file contents."},
            },
            "required": ["path", "content"],
        },
    }

    def __init__(self, workspace) -> None:
        self.workspace = workspace

    def call(self, args: dict) -> ToolResult:
        path = args.get("path", "")
        content = args.get("content", "")
        try:
            self.workspace.write_file(path, content)
            return ToolResult(
                ok=True,
                output=f"wrote {len(content)} bytes to {path}",
                data={"path": path, "bytes": len(content)},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output="", error=f"{type(exc).__name__}: {exc}")


class ApplyPatchTool:
    """Minimal patch tool: replace an exact substring in a file (unique match)."""

    name = "apply_patch"
    spec = {
        "name": "apply_patch",
        "description": (
            "Replace an exact, unique substring in a workspace file. Fails if "
            "'find' matches zero or more than one occurrence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "find": {"type": "string", "description": "Exact text to replace."},
                "replace": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "find", "replace"],
        },
    }

    def __init__(self, workspace) -> None:
        self.workspace = workspace

    def call(self, args: dict) -> ToolResult:
        path = args.get("path", "")
        find = args.get("find", "")
        replace = args.get("replace", "")
        try:
            content = self.workspace.read_file(path)
            count = content.count(find)
            if count != 1:
                return ToolResult(
                    ok=False,
                    output="",
                    error=f"apply_patch expects exactly 1 match, found {count}",
                )
            self.workspace.write_file(path, content.replace(find, replace, 1))
            return ToolResult(ok=True, output=f"patched {path}", data={"path": path})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output="", error=f"{type(exc).__name__}: {exc}")


class ListDirTool:
    name = "list_dir"
    spec = {
        "name": "list_dir",
        "description": "List files and directories under a workspace path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative dir (default '.').",
                }
            },
            "required": [],
        },
    }

    def __init__(self, workspace) -> None:
        self.workspace = workspace

    def call(self, args: dict) -> ToolResult:
        path = args.get("path", ".")
        try:
            entries = self.workspace.list_dir(path)
            return ToolResult(
                ok=True, output="\n".join(entries), data={"entries": entries}
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output="", error=f"{type(exc).__name__}: {exc}")
