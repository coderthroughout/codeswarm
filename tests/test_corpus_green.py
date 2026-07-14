"""Unit tests for the GREEN (desirable) corpus arm — fully offline.

The honesty invariant under test: polarity mirrors the trajectory's REAL outcome.
A trajectory whose failure was ACTUALLY recovered (failure event + final PASS)
mints through the fail-once GREEN pool -> verified_success; a never-fixed one
stays RED; a recovered one with NO pool key available is SKIPPED (never red).
httpx is stubbed via sys.modules so no test touches the network.
"""
from __future__ import annotations

import sys
import types
import uuid

from codeswarm.config import Config
from codeswarm.tasks.spec import Task
from codeswarm.trace.types import Event, Trajectory, Verdict
from codeswarm.workflow import omium_executor as oe


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #
_FAILURE_PAYLOAD = {
    "attempt": 1,
    "summary": "2 failed, 0 errors, 1 passed (rc=1)",
    "error_type": "AssertionError",
    "signature_token": "AssertionError+TypeError::test_math_utils.py",
    "failing_tests": ["test_math_utils.py::test_add"],
    "error_kinds": ["AssertionError", "TypeError"],
    "failed": 2,
    "errors": 0,
}


def _recovered_trajectory() -> Trajectory:
    """Failure event followed by a final PASS — the swarm ACTUALLY recovered."""
    events = [
        Event(kind="checkpoint", step_id="s1", agent=None, payload={}, ts_index=0),
        Event(kind="failure", step_id="s1", agent="tester",
              payload=dict(_FAILURE_PAYLOAD), ts_index=1),
        Event(kind="recovery", step_id="s1", agent="reviewer",
              payload={"attempt": 1, "hint": "implement it"}, ts_index=2),
        Event(kind="verdict", step_id="verify", agent=None,
              payload={"passed": True}, ts_index=3),
    ]
    return Trajectory(
        task_id="math_utils", run_id="math_utils-green123", events=events,
        verdict=Verdict(passed=True, signals={"tests_passed": 3}),
        failure_signature="feedc0ffee12",
    )


def _unrecovered_trajectory() -> Trajectory:
    events = [
        Event(kind="failure", step_id="s1", agent="tester",
              payload=dict(_FAILURE_PAYLOAD), ts_index=0),
        Event(kind="verdict", step_id="verify", agent=None,
              payload={"passed": False}, ts_index=1),
    ]
    return Trajectory(
        task_id="math_utils", run_id="math_utils-red456", events=events,
        verdict=Verdict(passed=False, signals={"tests_failed": 2}),
        failure_signature="deadbeef1234",
    )


def _clean_pass_trajectory() -> Trajectory:
    events = [
        Event(kind="verdict", step_id="verify", agent=None,
              payload={"passed": True}, ts_index=0),
    ]
    return Trajectory(
        task_id="math_utils", run_id="math_utils-clean", events=events,
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
    mod = types.ModuleType("httpx")

    def _post(url, headers=None, json=None, timeout=None):  # noqa: A002
        recorder.setdefault("posts", []).append(
            {"url": url, "headers": headers, "json": json}
        )
        return _FakeResponse(status_code, {"id": f"exec-{len(recorder['posts'])}"})

    mod.post = _post
    sys.modules["httpx"] = mod


# --------------------------------------------------------------------------- #
# Recovery detection (the polarity gate)
# --------------------------------------------------------------------------- #
def test_trajectory_recovered_requires_failure_and_pass():
    assert oe.trajectory_recovered(_recovered_trajectory()) is True
    assert oe.trajectory_recovered(_unrecovered_trajectory()) is False
    # A clean pass has no failure -> NOT green-eligible (nothing honest to mint).
    assert oe.trajectory_recovered(_clean_pass_trajectory()) is False


def test_trajectory_recovered_no_verdict():
    t = _recovered_trajectory()
    t.verdict = None
    assert oe.trajectory_recovered(t) is False


# --------------------------------------------------------------------------- #
# Green builders
# --------------------------------------------------------------------------- #
def test_green_workflow_id_matches_seed():
    wid = oe._default_green_workflow_id()
    assert wid == "82a67367-4d6d-5abd-97d2-00d33a7ef863"
    assert str(uuid.UUID(wid)) == wid
    assert wid == str(uuid.uuid5(uuid.NAMESPACE_DNS, "codeswarm.omium.corpus.workflow.green"))


def test_green_definition_shape():
    wf = oe.build_corpus_green_workflow_definition(oe.CORPUS_NODE_NAME, "boom-msg", "cs-green-7")
    names = [n["name"] for n in wf["nodes"]]
    assert names == ["ingest", "cs_oracle", "summarize"]
    node = wf["nodes"][1]
    # Fail-ONCE (not permanent), keyed to the seeded per-version counter.
    assert node["force_error_once"] == "boom-msg"
    assert "force_error" not in node
    assert node["force_error_once_key"] == "cs-green-7"
    # The heal writes the real ground-truth row the Tier-1 probe reads.
    assert node["emit_side_effect"] == "orders"
    pc = wf["postconditions"][0]
    assert pc["step_id"] == "cs_oracle"
    assert pc["assertion"]["store"] == "orders"
    assert pc["assertion"]["expect"] == "exactly_one"


def test_green_body_pins_pool_version_and_key():
    task = Task(id="math_utils", prompt="p", difficulty="easy")
    failure = oe.extract_dominant_failure(_recovered_trajectory())
    wf = oe.build_corpus_green_workflow_definition("cs_oracle", "m", "cs-green-3")
    body = oe.build_corpus_green_execution_body(
        task, "run-1", "green-wf-id", wf, failure, version=3, force_key="cs-green-3"
    )
    md = body["metadata"]
    # The RERUN pins (workflow_id, version) — the version MUST match the key.
    assert body["workflow_id"] == "green-wf-id"
    assert md["workflow_version"] == 3
    assert md["force_key"] == "cs-green-3"
    assert md["corpus_polarity"] == "green"
    assert md["codeswarm_signature_token"] == _FAILURE_PAYLOAD["signature_token"]


def test_red_and_green_share_the_signature_message():
    # Same message builder => the SAME EE signature cluster for both polarities
    # (the floor is per-signature: yield >= 200 AND desirable >= 50).
    failure = oe.extract_dominant_failure(_recovered_trajectory())
    msg = oe.build_corpus_failure_message(failure, "math_utils")
    assert _FAILURE_PAYLOAD["signature_token"] in msg
    assert "failed its pytest oracle" in msg  # the code_test_failure class marker


# --------------------------------------------------------------------------- #
# GreenKeyAllocator (key-exhaustion handling)
# --------------------------------------------------------------------------- #
def test_allocator_round_robin_then_exhausted(tmp_path):
    a = oe.GreenKeyAllocator(str(tmp_path / "pool.json"), pool_size=3)
    assert [a.allocate() for _ in range(3)] == [1, 2, 3]
    assert a.allocate() is None  # exhausted — caller must SKIP, never fall back
    assert a.remaining == 0


def test_allocator_release_reuses_untouched_key(tmp_path):
    a = oe.GreenKeyAllocator(str(tmp_path / "pool.json"), pool_size=2)
    v = a.allocate()
    a.release(v)  # POST never reached EE -> counter untouched
    assert a.allocate() == v


def test_allocator_persists_across_instances(tmp_path):
    path = str(tmp_path / "pool.json")
    a = oe.GreenKeyAllocator(path, pool_size=5)
    assert a.allocate() == 1
    b = oe.GreenKeyAllocator(path, pool_size=5)
    assert b.allocate() == 2  # continues the same clear-cycle


def test_allocator_mark_cleared_resets_cycle(tmp_path):
    a = oe.GreenKeyAllocator(str(tmp_path / "pool.json"), pool_size=2)
    a.allocate()
    a.allocate()
    assert a.allocate() is None
    a.mark_cleared()
    assert a.allocate() == 1


def test_allocator_version_node_interleave():
    # version -> node is the fixed residue interleave the seed mirrors.
    n = len(oe.CORPUS_NODE_NAMES)
    for v in range(1, 2 * n + 1):
        assert oe.GreenKeyAllocator.version_node(v) == oe.CORPUS_NODE_NAMES[(v - 1) % n]


def test_allocator_for_node_stays_in_sub_pool(tmp_path):
    n = len(oe.CORPUS_NODE_NAMES)
    a = oe.GreenKeyAllocator(str(tmp_path / "pool.json"), pool_size=3 * n)
    for name in oe.CORPUS_NODE_NAMES:
        v1 = a.allocate_for_node(name)
        v2 = a.allocate_for_node(name)
        assert v1 is not None and v2 is not None
        assert oe.GreenKeyAllocator.version_node(v1) == name
        assert oe.GreenKeyAllocator.version_node(v2) == name


def test_allocator_for_node_exhausts_independently(tmp_path):
    # A node with exactly one version in range exhausts after one draw; other nodes stay.
    a = oe.GreenKeyAllocator(str(tmp_path / "pool.json"), pool_size=len(oe.CORPUS_NODE_NAMES))
    first = oe.CORPUS_NODE_NAMES[0]
    second = oe.CORPUS_NODE_NAMES[1]
    assert a.allocate_for_node(first) == 1
    assert a.allocate_for_node(first) is None  # sub-pool spent
    assert a.allocate_for_node(second) == 2    # a different node still has capacity


def test_allocator_legacy_state_migrates(tmp_path):
    import json

    path = tmp_path / "pool.json"
    # Legacy on-disk format (next/freed) must migrate to the used-set model.
    path.write_text(json.dumps({"next": 4, "freed": [2]}), encoding="utf-8")
    a = oe.GreenKeyAllocator(str(path), pool_size=10)
    # Legacy next=4 => versions 1,2,3 handed out; freed=[2] => used {1,3}.
    assert a.allocate() == 2  # smallest unused
    assert a.allocate() == 4  # now used {1,2,3} -> next unused is 4


# --------------------------------------------------------------------------- #
# mint(): honest polarity routing
# --------------------------------------------------------------------------- #
def _corpus_run(tmp_path, pool_size=4) -> oe.OmiumCorpusRun:
    allocator = oe.GreenKeyAllocator(str(tmp_path / "pool.json"), pool_size=pool_size)
    return oe.OmiumCorpusRun(
        Config(),
        api_base="https://api-staging.omium.ai/api/v1",
        api_key="om_test_key",
        workflow_id="red-wf-id",
        green_workflow_id="green-wf-id",
        green_allocator=allocator,
    )


def test_mint_routes_recovered_trajectory_to_green(tmp_path):
    recorder: dict = {}
    _install_fake_httpx(recorder)
    try:
        run = _corpus_run(tmp_path)
        task = Task(id="math_utils", prompt="p", difficulty="easy")
        # The fixture's signature_token deterministically selects one node from the set;
        # the green version drawn MUST belong to that node's sub-pool.
        expected_node = oe.select_corpus_node(dict(_FAILURE_PAYLOAD), task.id)
        eid = run.mint(task, "run-1", _recovered_trajectory())
        assert eid == "exec-1"
        assert run.polarity == "green"
        version = run.green_version
        # version -> node interleave keeps the green row on the chosen node.
        assert oe.GreenKeyAllocator.version_node(version) == expected_node
        body = recorder["posts"][0]["json"]
        assert body["workflow_id"] == "green-wf-id"
        node = body["metadata"]["workflow_definition"]["nodes"][1]
        assert node["name"] == expected_node
        assert node["force_error_once_key"] == f"cs-green-{version}"
        assert "force_error" not in node
        # The message still embeds codeswarm's REAL failure identity.
        assert _FAILURE_PAYLOAD["signature_token"] in node["force_error_once"]
        assert body["metadata"]["workflow_version"] == version
        assert body["metadata"]["codeswarm_failing_node"] == expected_node
    finally:
        sys.modules.pop("httpx", None)


def test_mint_routes_unrecovered_trajectory_to_red(tmp_path):
    recorder: dict = {}
    _install_fake_httpx(recorder)
    try:
        run = _corpus_run(tmp_path)
        task = Task(id="math_utils", prompt="p")
        eid = run.mint(task, "run-1", _unrecovered_trajectory())
        assert eid == "exec-1"
        assert run.polarity == "red"
        body = recorder["posts"][0]["json"]
        assert body["workflow_id"] == "red-wf-id"
        node = body["metadata"]["workflow_definition"]["nodes"][1]
        assert "force_error" in node
        assert "force_error_once" not in node
        assert body["metadata"]["corpus_polarity"] == "red"
    finally:
        sys.modules.pop("httpx", None)


def test_mint_green_skips_when_pool_exhausted_never_red(tmp_path):
    recorder: dict = {}
    _install_fake_httpx(recorder)
    try:
        task = Task(id="math_utils", prompt="p")
        node = oe.select_corpus_node(dict(_FAILURE_PAYLOAD), task.id)
        # Size the pool so the CHOSEN node's sub-pool holds exactly ONE version
        # (the first version carrying that node under the fixed interleave).
        n = len(oe.CORPUS_NODE_NAMES)
        pool_size = oe.CORPUS_NODE_NAMES.index(node) + 1
        assert sum(1 for v in range(1, pool_size + 1)
                   if oe.GreenKeyAllocator.version_node(v) == node) == 1
        run = _corpus_run(tmp_path, pool_size=pool_size)
        assert run.mint(task, "run-1", _recovered_trajectory()) == "exec-1"
        # That node's sub-pool now exhausted: a recovered trajectory is SKIPPED, never red.
        run2 = oe.OmiumCorpusRun(
            Config(), api_base="https://api-staging.omium.ai/api/v1", api_key="k",
            workflow_id="red-wf-id", green_workflow_id="green-wf-id",
            green_allocator=oe.GreenKeyAllocator(str(tmp_path / "pool.json"), pool_size=pool_size),
        )
        assert run2.mint(task, "run-2", _recovered_trajectory()) is None
        assert run2.polarity is None
        assert len(recorder["posts"]) == 1  # no second POST happened
        assert n >= 1  # sanity: the node set is non-empty
    finally:
        sys.modules.pop("httpx", None)


def test_mint_green_releases_key_on_post_failure(tmp_path):
    recorder: dict = {}
    _install_fake_httpx(recorder, status_code=500)
    try:
        task = Task(id="math_utils", prompt="p")
        node = oe.select_corpus_node(dict(_FAILURE_PAYLOAD), task.id)
        # Pool must include a version carrying the chosen node.
        pool_size = oe.CORPUS_NODE_NAMES.index(node) + 1
        allocator = oe.GreenKeyAllocator(str(tmp_path / "pool.json"), pool_size=pool_size)
        before = allocator.remaining
        run = oe.OmiumCorpusRun(
            Config(), api_base="http://x/api/v1", api_key="k",
            green_workflow_id="g", green_allocator=allocator,
        )
        assert run.mint(task, "run-1", _recovered_trajectory()) is None
        # The POST never reached EE -> the key was returned to the pool (unchanged count).
        assert allocator.remaining == before
    finally:
        sys.modules.pop("httpx", None)


def test_mint_clean_pass_still_mints_nothing(tmp_path):
    run = _corpus_run(tmp_path)
    task = Task(id="math_utils", prompt="p")
    assert run.mint(task, "run-1", _clean_pass_trajectory()) is None
    assert run.polarity is None
