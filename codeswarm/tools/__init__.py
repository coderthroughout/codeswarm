"""Tools — the agents' hands. All ops are confined to the sandbox workspace."""
from __future__ import annotations

from codeswarm.tools.base import Tool, ToolResult
from codeswarm.tools.fs import (
    ApplyPatchTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from codeswarm.tools.shell import RunTool
from codeswarm.tools.testing import RunTestsTool

__all__ = [
    "Tool",
    "ToolResult",
    "ReadFileTool",
    "WriteFileTool",
    "ApplyPatchTool",
    "ListDirTool",
    "RunTool",
    "RunTestsTool",
    "default_tools",
]


def default_tools(workspace) -> dict[str, "Tool"]:
    """Construct the standard toolset bound to a workspace, keyed by name."""
    tools: list[Tool] = [
        ReadFileTool(workspace),
        WriteFileTool(workspace),
        ApplyPatchTool(workspace),
        ListDirTool(workspace),
        RunTool(workspace),
        RunTestsTool(workspace),
    ]
    return {t.name: t for t in tools}
