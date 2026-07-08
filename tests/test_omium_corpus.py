"""Unit tests for the Mode-2 (corpus) Omium seam.

These exercise the pure payload/derivation helpers + the fail-soft OmiumCorpusRun
orchestration WITHOUT any network. httpx is stubbed via sys.modules so the tests run
fully offline (and even where httpx is not installed). The whole point of Mode-2 is
that a FAILING codeswarm task drives the execution-engine to emit a real
omium.execution.failed whose signature DERIVES FROM codeswarm's real failure — the
honesty invariant is asserted directly.
"""
from __future__ import annotations

import sys
import types

from codeswarm.config import Config
from codeswarm.tasks.spec import Task
from codeswarm.trace.types import Event, Trajectory, Verdict
from codeswarm.workflow import omium_executor as oe


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #
def _failing_trajectory() -> Trajectory:
    """A trajectory with a real dominant failure event (as executor.py records it)."""
    events = [
        Event(kind="checkpoint", step_id="s1", agent=None, payload={}, ts_index=0),
        Event(
            kind="failure",
            step_id="s1",
            agent="tester",
            payload={
                "attempt": 1,
                "summary": "2 failed, 0 errors, 1 passed (rc=1)",
                "error_type": "AssertionError",
                "signature_token": "AssertionError+TypeError::test_math_utils.py",
                "failing_tests": ["test_math_utils.py::test_add", "test_math_utils.py::test_mul"],
                "error_kinds": ["AssertionError", "TypeError"],
                "failed": 2,
                "errors": 0,
            },
            ts_index=1,
        ),
        Event(kind="verdict", step_id="verify", agent=None,
              payload={"passed": False}, ts_index=2),
    ]
    return Trajectory(
        task_id="math_utils", run_id="math_utils-abcd1234", events=events,
        verdict=Verdict(passed=False, signals={"tests_failed": 2}),
        failure_signature="deadbeef1234",
    )


def _passing_trajectory() -> Trajectory:
    events = [
        Event(kind="checkpoint", step_id="s1", agent=None, payload={}, ts_index=0),
        Event(kind="verdict", step_id="verify", agent=None,
              payload={"passed": True}, ts_index=1),
    ]
    return Trajectory(
        task_id="math_utils", run_id="math_utils-abcd1234", events=events,
        verdict=Verdict(passed=True, signals={"tests_passed": 3}),
        failure_signature=None,
    )


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def _install_fake_httpx(recorder: dict, status_code: int = 201) -> None:
    """Register a fake ``httpx`` module that records the POST and returns a fake resp."""
    mod = types.ModuleType("httpx")

    def _post(url, headers=None, json=None, timeout=None):  # noqa: A002 - mirror httpx
        recorder["url"] = url
        recorder["headers"] = headers
        recorder["json"] = json
        recorder["timeout"] = timeout
        return _FakeResponse(status_code, {"id": "exec-abc-123"})

    mod.post = _post
    sys.modules["httpx"] = mod


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_extract_dominant_failure_returns_payload():
    failure = oe.extract_dominant_failure(_failing_trajectory())
    assert failure is not None
    assert failure["signature_token"] == "AssertionError+TypeError::test_math_utils.py"
    assert failure["error_kinds"] == ["AssertionError", "TypeError"]


def test_extract_dominant_failure_none_when_passed():
    assert oe.extract_dominant_failure(_passing_trajectory()) is None


def test_message_embeds_real_signature_token_and_is_honest():
    failure = oe.extract_dominant_failure(_failing_trajectory())
    msg = oe.build_corpus_failure_message(failure, "math_utils")
    # Honesty: the message DERIVES from codeswarm's real failure token/kinds.
    assert "AssertionError+TypeError::test_math_utils.py" in msg
    assert "AssertionError+TypeError" in msg
    assert "math_utils" in msg
    # We never fabricate a transient/429/schema signal to trip the migrated router.
    for fake in ("429", "503", "rate limit", "schema violation", "timeout"):
        assert fake.lower() not in msg.lower()


def test_workflow_definition_has_force_error_node():
    wf = oe.build_corpus_workflow_definition(oe.CORPUS_NODE_NAME, "boom-msg")
    names = [n["name"] for n in wf["nodes"]]
    assert names == ["ingest", oe.CORPUS_NODE_NAME, "summarize"]
    fail_node = wf["nodes"][1]
    assert fail_node["force_error"] == "boom-msg"
    # START -> ingest -> fail -> summarize -> END wiring.
    froms = {(e["from"], e["to"]) for e in wf["edges"]}
    assert ("START", "ingest") in froms
    assert ("ingest", oe.CORPUS_NODE_NAME) in froms
    assert ("summarize", "END") in froms


def test_node_name_is_the_seeded_constant():
    # The failing node MUST equal the platform's seeded pinned-version node
    # (seed_codeswarm_corpus_workflow.sql) or RERUNNING can't reproduce the failure.
    failure = oe.extract_dominant_failure(_failing_trajectory())
    assert oe._sanitize_node_name(failure, "math_utils") == "cs_oracle"
    assert oe._sanitize_node_name({"failing_tests": []}, "My Task!") == "cs_oracle"
    assert oe.CORPUS_NODE_NAME == "cs_oracle"


def test_diversity_lives_in_the_message_not_the_node():
    # With the node constant, the signature's diversity axis is the message — it must
    # still embed the real per-task signature_token.
    failure = oe.extract_dominant_failure(_failing_trajectory())
    msg = oe.build_corpus_failure_message(failure, "math_utils")
    assert failure["signature_token"] in msg


def test_execution_body_shape():
    task = Task(id="math_utils", prompt="p", difficulty="easy")
    failure = oe.extract_dominant_failure(_failing_trajectory())
    wf = oe.build_corpus_workflow_definition("cs_x", "m")
    body = oe.build_corpus_execution_body(task, "run-1", "wf-uuid", wf, failure)
    assert body["workflow_id"] == "wf-uuid"
    assert body["agent_id"] == "codeswarm-math_utils"
    md = body["metadata"]
    assert md["workflow_type"] == "langgraph"
    assert md["workflow_definition"] is wf
    assert md["source"] == "codeswarm-corpus"
    assert md["workflow_version"] == 1
    assert md["codeswarm_signature_token"] == "AssertionError+TypeError::test_math_utils.py"
    assert md["codeswarm_error_kinds"] == ["AssertionError", "TypeError"]


def test_default_workflow_id_is_valid_uuid():
    import uuid

    wid = oe._default_workflow_id()
    # Must round-trip as a UUID (the API model coerces workflow_id -> UUID).
    assert str(uuid.UUID(wid)) == wid


# --------------------------------------------------------------------------- #
# OmiumCorpusRun orchestration (fail-soft, offline)
# --------------------------------------------------------------------------- #
def test_mint_noop_when_run_passed():
    task = Task(id="math_utils", prompt="p")
    run = oe.OmiumCorpusRun(Config(), api_base="http://x/api/v1", api_key="k")
    assert run.mint(task, "run-1", _passing_trajectory()) is None


def test_mint_noop_when_no_config(monkeypatch):
    # No injected base/key, no env, and force the omium SDK import to fail.
    monkeypatch.delenv("OMIUM_API_URL", raising=False)
    monkeypatch.delenv("OMIUM_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "omium", None)  # import omium -> ImportError
    task = Task(id="math_utils", prompt="p")
    run = oe.OmiumCorpusRun(Config())
    assert run.mint(task, "run-1", _failing_trajectory()) is None


def test_mint_posts_failing_execution_and_returns_id():
    recorder: dict = {}
    _install_fake_httpx(recorder, status_code=201)
    try:
        task = Task(id="math_utils", prompt="p", difficulty="easy")
        run = oe.OmiumCorpusRun(
            Config(), api_base="https://api-staging.omium.ai/api/v1",
            api_key="om_test_key", workflow_id="wf-uuid-1",
        )
        eid = run.mint(task, "run-1", _failing_trajectory())
        assert eid == "exec-abc-123"
        # Correct endpoint + auth header.
        assert recorder["url"].endswith("/api/v1/executions")
        assert recorder["headers"]["X-API-Key"] == "om_test_key"
        body = recorder["json"]
        # Drives a real langgraph run with an inline force_error definition.
        assert body["metadata"]["workflow_type"] == "langgraph"
        fail_node = body["metadata"]["workflow_definition"]["nodes"][1]
        # The force_error message carries codeswarm's REAL signature token (honest).
        assert "AssertionError+TypeError::test_math_utils.py" in fail_node["force_error"]
        assert body["workflow_id"] == "wf-uuid-1"
    finally:
        sys.modules.pop("httpx", None)


def test_mint_fail_soft_on_http_error():
    recorder: dict = {}
    _install_fake_httpx(recorder, status_code=500)
    try:
        task = Task(id="math_utils", prompt="p")
        run = oe.OmiumCorpusRun(
            Config(), api_base="http://x/api/v1", api_key="k", workflow_id="w",
        )
        # A 500 must not raise into the caller; returns None.
        assert run.mint(task, "run-1", _failing_trajectory()) is None
    finally:
        sys.modules.pop("httpx", None)


# --------------------------------------------------------------------------- #
# Mode resolution
# --------------------------------------------------------------------------- #
def test_config_mode_resolution(monkeypatch):
    for var in ("CODESWARM_OMIUM_MODE", "CODESWARM_OMIUM"):
        monkeypatch.delenv(var, raising=False)

    # Explicit corpus.
    monkeypatch.setenv("CODESWARM_OMIUM_MODE", "corpus")
    cfg = Config.from_env()
    assert cfg.omium_mode == "corpus"
    assert cfg.omium_enabled is False
    assert oe.corpus_mode_enabled(cfg) is True
    assert oe.omium_enabled(cfg) is False

    # Explicit observability.
    monkeypatch.setenv("CODESWARM_OMIUM_MODE", "observability")
    cfg = Config.from_env()
    assert cfg.omium_mode == "observability"
    assert cfg.omium_enabled is True
    assert oe.omium_enabled(cfg) is True
    assert oe.corpus_mode_enabled(cfg) is False

    # Legacy CODESWARM_OMIUM=1 -> observability.
    monkeypatch.delenv("CODESWARM_OMIUM_MODE", raising=False)
    monkeypatch.setenv("CODESWARM_OMIUM", "1")
    cfg = Config.from_env()
    assert cfg.omium_mode == "observability"
    assert cfg.omium_enabled is True

    # Nothing set -> off.
    monkeypatch.delenv("CODESWARM_OMIUM", raising=False)
    cfg = Config.from_env()
    assert cfg.omium_mode == "off"
    assert cfg.omium_enabled is False
    assert oe.omium_enabled(cfg) is False
    assert oe.corpus_mode_enabled(cfg) is False


def test_config_override_recomputes_enabled():
    # A direct override of omium_mode keeps omium_enabled consistent.
    cfg = Config.from_env(omium_mode="observability")
    assert cfg.omium_enabled is True
    cfg = Config.from_env(omium_mode="corpus")
    assert cfg.omium_enabled is False


def test_module_import_does_not_require_omium():
    # Importing the seam must not import the omium SDK at module load.
    import importlib

    mod = importlib.import_module("codeswarm.workflow.omium_executor")
    assert hasattr(mod, "OmiumCorpusRun")
    assert hasattr(mod, "build_corpus_failure_message")
