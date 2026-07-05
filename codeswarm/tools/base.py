"""Tool Protocol + ToolResult (see DESIGN.md "Tools")."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class ToolResult:
    """Uniform result of a tool call."""

    ok: bool
    output: str
    error: str | None = None
    data: dict = field(default_factory=dict)  # structured extras (e.g. test counts)


@runtime_checkable
class Tool(Protocol):
    """Each tool has a name, a JSON-schema spec, and a synchronous call().

    ``spec`` is Anthropic-tool-shaped: {"name", "description", "input_schema"}.
    """

    name: str
    spec: dict

    def call(self, args: dict) -> ToolResult:
        ...
