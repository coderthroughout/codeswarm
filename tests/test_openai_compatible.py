"""Unit tests for OpenAICompatibleClient (Nebius Token Factory path).

All HTTP is mocked — no network, no key. Covers: auth header, endpoint,
model/max_tokens propagation, system-message placement, tool-spec mapping,
response parsing (text + tool_calls), the zero-choices guard (Token Factory
answers a wrong model id with HTTP 200 + empty choices), provider selection,
and Config env plumbing.
"""
from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.request

import pytest

from codeswarm.config import (
    DEFAULT_MODEL,
    DEFAULT_OPENAI_COMPAT_BASE_URL,
    DEFAULT_OPENAI_COMPAT_MODEL,
    Config,
)
from codeswarm.llm.client import (
    OpenAICompatibleClient,
    _to_openai_tools,
    build_real_client,
)


def _cfg(**over) -> Config:
    base = dict(
        llm_provider="openai_compatible",
        model=DEFAULT_OPENAI_COMPAT_MODEL,
        openai_api_key="test-key-not-real",
    )
    base.update(over)
    return Config(**base)


def _ok_body(text="hello", tool_calls=None) -> dict:
    msg: dict = {"role": "assistant", "content": text}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": msg}]}


class _FakeHTTP:
    """Patches urllib.request.urlopen; captures the Request; returns canned JSON."""

    def __init__(self, monkeypatch, body: dict) -> None:
        self.request: urllib.request.Request | None = None

        def fake_urlopen(req, timeout=None):
            self.request = req
            resp = io.BytesIO(json.dumps(body).encode("utf-8"))
            resp.__enter__ = lambda *a: resp  # type: ignore[attr-defined]
            resp.__exit__ = lambda *a: False  # type: ignore[attr-defined]
            return resp

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    @property
    def payload(self) -> dict:
        assert self.request is not None, "no HTTP call was made"
        return json.loads(self.request.data.decode("utf-8"))


# -- construction ---------------------------------------------------------------

def test_requires_api_key():
    with pytest.raises(RuntimeError, match="NEBIUS_API_KEY"):
        OpenAICompatibleClient(_cfg(openai_api_key=None))


def test_endpoint_built_from_base_url_with_and_without_trailing_slash():
    for base in ("https://api.tokenfactory.nebius.com/v1/", "https://api.tokenfactory.nebius.com/v1"):
        client = OpenAICompatibleClient(_cfg(openai_base_url=base))
        assert client.endpoint == "https://api.tokenfactory.nebius.com/v1/chat/completions"


# -- request shape ----------------------------------------------------------------

def test_auth_header_model_and_max_tokens_propagate(monkeypatch):
    http = _FakeHTTP(monkeypatch, _ok_body())
    client = OpenAICompatibleClient(_cfg(openai_max_tokens=8192))

    resp = asyncio.run(client.complete("SYSTEM PROMPT", [{"role": "user", "content": "hi"}]))

    assert resp.text == "hello"
    req = http.request
    assert req.get_header("Authorization") == "Bearer test-key-not-real"
    assert req.get_header("Content-type") == "application/json"
    assert req.full_url == "https://api.tokenfactory.nebius.com/v1/chat/completions"
    payload = http.payload
    assert payload["model"] == "MiniMaxAI/MiniMax-M3"
    assert payload["max_tokens"] == 8192
    # System prompt travels as the FIRST message, role=system.
    assert payload["messages"][0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert payload["messages"][1] == {"role": "user", "content": "hi"}
    assert "tools" not in payload


def test_max_tokens_default_is_8192_reasoning_floor():
    # Known production scar: reasoning models truncate below ~8k output budget.
    client = OpenAICompatibleClient(_cfg())
    assert client.max_tokens == 8192


def test_anthropic_tool_specs_map_to_openai_tools(monkeypatch):
    http = _FakeHTTP(monkeypatch, _ok_body())
    client = OpenAICompatibleClient(_cfg())
    anthropic_spec = {
        "name": "write_file",
        "description": "Write a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    }

    asyncio.run(
        client.complete("coder", [{"role": "user", "content": "x"}], tools=[anthropic_spec])
    )

    tools = http.payload["tools"]
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a file.",
                "parameters": anthropic_spec["input_schema"],
            },
        }
    ]


def test_to_openai_tools_none_and_empty():
    assert _to_openai_tools(None) is None
    assert _to_openai_tools([]) is None


# -- response parsing --------------------------------------------------------------

def test_parses_openai_tool_calls_into_name_args_dicts(monkeypatch):
    _FakeHTTP(
        monkeypatch,
        _ok_body(
            text="writing now",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "a.py", "content": "x = 1\n"}),
                    },
                }
            ],
        ),
    )
    client = OpenAICompatibleClient(_cfg())

    resp = asyncio.run(client.complete("coder", [{"role": "user", "content": "go"}]))

    assert resp.text == "writing now"
    assert resp.tool_calls == [
        {"name": "write_file", "args": {"path": "a.py", "content": "x = 1\n"}}
    ]


def test_malformed_tool_arguments_become_empty_args(monkeypatch):
    _FakeHTTP(
        monkeypatch,
        _ok_body(
            tool_calls=[
                {"type": "function", "function": {"name": "write_file", "arguments": "{not json"}}
            ]
        ),
    )
    client = OpenAICompatibleClient(_cfg())
    resp = asyncio.run(client.complete("coder", [{"role": "user", "content": "go"}]))
    assert resp.tool_calls == [{"name": "write_file", "args": {}}]


def test_null_content_parses_as_empty_text(monkeypatch):
    # OpenAI wire format sends content: null on pure tool-call turns.
    _FakeHTTP(monkeypatch, {"choices": [{"message": {"role": "assistant", "content": None}}]})
    client = OpenAICompatibleClient(_cfg())
    resp = asyncio.run(client.complete("s", [{"role": "user", "content": "x"}]))
    assert resp.text == ""
    assert resp.tool_calls == []


def test_zero_choices_is_a_hard_error_not_silent_empty(monkeypatch):
    # Live-verified Token Factory quirk: wrong model id -> HTTP 200, empty choices.
    _FakeHTTP(monkeypatch, {"choices": [], "usage": {}})
    client = OpenAICompatibleClient(_cfg())
    with pytest.raises(RuntimeError, match="no choices"):
        asyncio.run(client.complete("s", [{"role": "user", "content": "x"}]))


def test_missing_choices_key_is_also_a_hard_error(monkeypatch):
    _FakeHTTP(monkeypatch, {"id": "x"})
    client = OpenAICompatibleClient(_cfg())
    with pytest.raises(RuntimeError, match="no choices"):
        asyncio.run(client.complete("s", [{"role": "user", "content": "x"}]))


def test_http_error_surfaces_status_and_body(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"bad key"}')
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = OpenAICompatibleClient(_cfg())
    with pytest.raises(RuntimeError, match="HTTP 401"):
        asyncio.run(client.complete("s", [{"role": "user", "content": "x"}]))


# -- provider selection --------------------------------------------------------------

def test_build_real_client_selects_openai_compatible():
    client = build_real_client(_cfg())
    assert isinstance(client, OpenAICompatibleClient)
    assert client.model == "MiniMaxAI/MiniMax-M3"


def test_build_real_client_default_provider_is_anthropic_path():
    # Must NOT return the OpenAI client for the default provider. The Anthropic
    # path lazy-imports the SDK; absence of the package raises RuntimeError,
    # which still proves openai_compatible was not chosen.
    try:
        client = build_real_client(Config(api_key="k"))
    except RuntimeError as exc:
        assert "anthropic" in str(exc)
    else:
        assert not isinstance(client, OpenAICompatibleClient)


# -- config / env plumbing ------------------------------------------------------------

def test_from_env_openai_compatible(monkeypatch):
    monkeypatch.setenv("CODESWARM_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("NEBIUS_API_KEY", "nb-test-not-real")
    monkeypatch.delenv("CODESWARM_MODEL", raising=False)
    monkeypatch.delenv("CODESWARM_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODESWARM_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("CODESWARM_OPENAI_MAX_TOKENS", raising=False)

    cfg = Config.from_env()

    assert cfg.llm_provider == "openai_compatible"
    assert cfg.model == DEFAULT_OPENAI_COMPAT_MODEL == "MiniMaxAI/MiniMax-M3"
    assert cfg.openai_api_key == "nb-test-not-real"
    assert cfg.openai_base_url == DEFAULT_OPENAI_COMPAT_BASE_URL
    assert cfg.openai_max_tokens == 8192


def test_from_env_codeswarm_key_wins_over_nebius(monkeypatch):
    monkeypatch.setenv("CODESWARM_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("NEBIUS_API_KEY", "nb-test-not-real")
    monkeypatch.setenv("CODESWARM_OPENAI_API_KEY", "cs-test-not-real")
    cfg = Config.from_env()
    assert cfg.openai_api_key == "cs-test-not-real"


def test_from_env_base_url_model_and_max_tokens_overridable(monkeypatch):
    monkeypatch.setenv("CODESWARM_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("NEBIUS_API_KEY", "nb-test-not-real")
    monkeypatch.setenv("CODESWARM_OPENAI_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("CODESWARM_MODEL", "some-org/Other-Model")
    monkeypatch.setenv("CODESWARM_OPENAI_MAX_TOKENS", "16384")
    cfg = Config.from_env()
    assert cfg.openai_base_url == "http://localhost:8000/v1"
    assert cfg.model == "some-org/Other-Model"
    assert cfg.openai_max_tokens == 16384


def test_from_env_provider_override_fixes_model_default(monkeypatch):
    # CLI-style override: env provider unset, --provider openai_compatible passed
    # as an explicit override, no model given -> model must switch off the
    # Anthropic default (that id does not exist on Token Factory).
    monkeypatch.delenv("CODESWARM_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CODESWARM_MODEL", raising=False)
    cfg = Config.from_env(llm_provider="openai_compatible")
    assert cfg.model == DEFAULT_OPENAI_COMPAT_MODEL


def test_from_env_defaults_unchanged_without_provider(monkeypatch):
    # Default behavior untouched: no provider env -> anthropic + Claude model.
    monkeypatch.delenv("CODESWARM_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CODESWARM_MODEL", raising=False)
    cfg = Config.from_env()
    assert cfg.llm_provider == "anthropic"
    assert cfg.model == DEFAULT_MODEL


def test_cli_accepts_openai_compatible_provider():
    from codeswarm.cli import build_parser

    args = build_parser().parse_args(
        ["run", "--task", "math_utils", "--provider", "openai_compatible"]
    )
    assert args.provider == "openai_compatible"
