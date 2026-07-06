"""LLM client Protocol + implementations.

- ``LLMClient`` is the interface every agent talks to.
- ``MockClient`` returns deterministic canned responses that DRIVE a real
  plan -> code -> test -> review -> verify loop to completion, with NO network
  and NO API key. It is first-class: the whole system + its tests run offline.
- ``AnthropicClient`` wraps the real Claude SDK. It LAZY-imports ``anthropic``
  inside ``__init__`` so importing this module never requires the package.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class LLMResponse:
    """A single completion. ``tool_calls`` is a list of {"name", "args"} dicts."""

    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)


@runtime_checkable
class LLMClient(Protocol):
    """The interface agents depend on."""

    async def complete(
        self,
        system: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        ...


def _detect_role(system: str) -> str:
    """Infer which agent is calling from its system prompt."""
    s = system.lower()
    for role in ("planner", "coder", "tester", "reviewer"):
        if role in s:
            return role
    return "unknown"


class MockClient:
    """Deterministic offline client that drives the full loop to a passing verdict.

    It is seeded with the task's reference solution (mock-only; the real client
    never sees it). The coder's FIRST attempt writes a broken stub so the loop
    exercises the failure + recovery path; subsequent attempts write the correct
    solution so the run reaches a green verdict. This produces a trajectory with
    real failure/recovery/verdict events — exactly the corpus signal we want.
    """

    def __init__(self, solutions: dict[str, str] | None = None) -> None:
        # {path: correct_content}. Empty is fine (the coder then writes nothing).
        self.solutions = dict(solutions or {})
        self._coder_calls = 0
        self.calls: list[str] = []  # roles seen, for debugging/inspection

    async def complete(
        self,
        system: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        role = _detect_role(system)
        self.calls.append(role)

        if role == "planner":
            plan = {
                "plan": [
                    {
                        "id": "step-1",
                        "description": "Implement the solution so the tests pass.",
                    }
                ]
            }
            return LLMResponse(text=json.dumps(plan))

        if role == "coder":
            attempt = self._coder_calls
            self._coder_calls += 1
            if attempt == 0:
                # Deliberately broken first attempt -> tests fail -> recovery path.
                tool_calls = [
                    {
                        "name": "write_file",
                        "args": {
                            "path": path,
                            "content": "# not implemented yet\n",
                        },
                    }
                    for path in self.solutions
                ]
                return LLMResponse(
                    text="First attempt: writing stubs.", tool_calls=tool_calls
                )
            # Corrected attempt: write the real solution.
            tool_calls = [
                {"name": "write_file", "args": {"path": path, "content": content}}
                for path, content in self.solutions.items()
            ]
            return LLMResponse(text="Applying the fix.", tool_calls=tool_calls)

        if role == "reviewer":
            return LLMResponse(
                text=(
                    "The target functions are unimplemented; implement them exactly "
                    "as the task requires so the failing tests pass."
                )
            )

        return LLMResponse(text="ok")


class AnthropicClient:
    """Real Claude client. Lazy-imports the anthropic SDK in ``__init__`` so this
    module imports fine even when ``anthropic`` is not installed.
    """

    def __init__(self, config) -> None:  # config: codeswarm.config.Config
        try:
            import anthropic  # lazy import — see docstring
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised only with real key
            raise RuntimeError(
                "AnthropicClient requires the 'anthropic' package. "
                "Install it with: pip install 'codeswarm[anthropic]'"
            ) from exc

        self._anthropic = anthropic
        self.model = config.model
        self.max_tokens = 8000
        provider = getattr(config, "llm_provider", "anthropic")
        if provider == "vertex":
            # Claude on GCP Vertex AI. Auth via Application Default Credentials
            # (gcloud ADC / a service account) — no API key. Same messages + tool
            # API as the direct client, so complete() below is unchanged.
            self._client = anthropic.AnthropicVertex(
                project_id=config.vertex_project,
                region=config.vertex_region,
            )
        elif config.api_key:
            self._client = anthropic.Anthropic(api_key=config.api_key)
        else:
            # A bare client resolves credentials from the environment.
            self._client = anthropic.Anthropic()

    async def complete(
        self,
        system: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        import asyncio

        def _call() -> LLMResponse:
            kwargs: dict = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
            resp = self._client.messages.create(**kwargs)
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for block in resp.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    text_parts.append(getattr(block, "text", ""))
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "name": getattr(block, "name", ""),
                            "args": dict(getattr(block, "input", {}) or {}),
                        }
                    )
            return LLMResponse(text="".join(text_parts), tool_calls=tool_calls)

        return await asyncio.to_thread(_call)
