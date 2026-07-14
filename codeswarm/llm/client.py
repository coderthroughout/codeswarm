"""LLM client Protocol + implementations.

- ``LLMClient`` is the interface every agent talks to.
- ``MockClient`` returns deterministic canned responses that DRIVE a real
  plan -> code -> test -> review -> verify loop to completion, with NO network
  and NO API key. It is first-class: the whole system + its tests run offline.
- ``AnthropicClient`` wraps the real Claude SDK. It LAZY-imports ``anthropic``
  inside ``__init__`` so importing this module never requires the package.
- ``OpenAICompatibleClient`` speaks the OpenAI ``/chat/completions`` wire format
  over stdlib ``urllib`` (no SDK dependency — core stays stdlib-only). Default
  target: Nebius Token Factory. It maps codeswarm's Anthropic-shaped tool specs
  to OpenAI ``tools`` and OpenAI ``tool_calls`` back to codeswarm's
  ``{"name", "args"}`` shape, so agents are provider-agnostic.
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


def _to_openai_tools(tools: list[dict] | None) -> list[dict] | None:
    """Anthropic tool spec {name, description, input_schema} -> OpenAI tools format."""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


class OpenAICompatibleClient:
    """Real client for any OpenAI-compatible ``/chat/completions`` endpoint.

    Default target is Nebius Token Factory (``config.openai_base_url``); auth is a
    Bearer key from ``config.openai_api_key`` (env: CODESWARM_OPENAI_API_KEY or
    NEBIUS_API_KEY — codeswarm's secret convention is env vars, never files).

    Implemented on stdlib ``urllib`` so codeswarm core stays dependency-free.
    Non-streaming, single-shot completions — exactly what the agent loop uses
    (agents feed tool outputs back as plain text; see CoderAgent). Tool specs are
    mapped Anthropic->OpenAI on the way out and OpenAI ``tool_calls`` are mapped
    back to codeswarm's ``{"name", "args"}`` dicts on the way in.
    """

    def __init__(self, config) -> None:  # config: codeswarm.config.Config
        api_key = getattr(config, "openai_api_key", None)
        if not api_key:
            raise RuntimeError(
                "OpenAICompatibleClient needs an API key: set CODESWARM_OPENAI_API_KEY "
                "or NEBIUS_API_KEY in the environment."
            )
        base = (getattr(config, "openai_base_url", "") or "").rstrip("/")
        if not base:
            raise RuntimeError(
                "OpenAICompatibleClient needs a base URL (CODESWARM_OPENAI_BASE_URL)."
            )
        self._api_key = api_key
        self.endpoint = f"{base}/chat/completions"
        self.model = config.model
        self.max_tokens = int(
            getattr(config, "openai_max_tokens", 8192) or 8192
        )
        self.timeout = 600.0  # generous: large reasoning models think for a while

    # -- wire ------------------------------------------------------------------
    def _post(self, payload: dict) -> dict:
        """POST the payload; return the parsed JSON body. Split out for tests."""
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:2000]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                f"OpenAI-compatible endpoint returned HTTP {exc.code}: {body}"
            ) from exc

    @staticmethod
    def _parse(body: dict) -> LLMResponse:
        """Parse a /chat/completions body into an LLMResponse.

        Nebius Token Factory quirk (live-verified): a WRONG model id returns
        HTTP 200 with EMPTY ``choices`` and no error field — treat zero choices
        as a hard error instead of silently handing agents empty text.
        """
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(
                "OpenAI-compatible endpoint returned 200 with no choices — "
                "on Nebius Token Factory this usually means a wrong model id "
                f"(sent model exists? see GET /models). Body keys: {sorted(body)}"
            )
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        tool_calls: list[dict] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError, ValueError):
                args = {}
            tool_calls.append({"name": fn.get("name", ""), "args": args})
        return LLMResponse(text=text, tool_calls=tool_calls)

    # -- LLMClient interface -----------------------------------------------------
    async def complete(
        self,
        system: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        import asyncio

        payload: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        oa_tools = _to_openai_tools(tools)
        if oa_tools:
            payload["tools"] = oa_tools

        def _call() -> LLMResponse:
            return self._parse(self._post(payload))

        return await asyncio.to_thread(_call)


def build_real_client(config) -> "LLMClient":
    """Pick the real (non-mock) client from ``config.llm_provider``.

    "anthropic" (default) and "vertex" both live in AnthropicClient;
    "openai_compatible" is the OpenAI-wire client (Nebius Token Factory).
    """
    provider = getattr(config, "llm_provider", "anthropic")
    if provider == "openai_compatible":
        return OpenAICompatibleClient(config)
    return AnthropicClient(config)


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
