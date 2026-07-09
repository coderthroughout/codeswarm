"""Optional Omium integration — the single Omium seam (see DESIGN.md).

codeswarm never imports `omium` at module load; this file lazy-imports it and is
only activated by ``--omium`` / ``CODESWARM_OMIUM=1``. Everything here is
FAIL-SOFT: if Omium is unreachable, the codeswarm run continues unchanged — Omium
is observability + a recovery substrate, never a hard dependency of the local run.

When enabled, per run:
  * ``omium.init()`` + POST an execution "anchor" + ``set_execution_id`` so all
    data correlates under one execution (visible in the Omium dashboard),
  * each plan step opens an Omium span and writes a checkpoint tied to that
    execution (a real recovery point),
  * the execution is marked completed/failed at the end (best-effort).

The local Trajectory / JSONL is untouched; this adds a parallel, live view in
Omium and exercises the SDK/CLI/checkpoint path end-to-end.

Config/env:
  CODESWARM_OMIUM=1                 enable
  OMIUM_API_URL / OMIUM_API_KEY    read by the omium SDK (staging: api-staging.omium.ai)
  CODESWARM_OMIUM_WORKFLOW_ID       optional; auto-resolved by project name otherwise
"""
from __future__ import annotations

import logging
import os
import uuid

log = logging.getLogger("codeswarm.omium")

_PROJECT = "codeswarm"
# The checkpoint API validates agent_id as a UUID; use a stable one for "codeswarm".
_AGENT_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "codeswarm.omium.agent"))


def _mode(config=None) -> str:
    """Resolve the active Omium mode ("off" | "observability" | "corpus").

    Config wins; otherwise fall back to the CODESWARM_OMIUM_MODE env var, then the
    legacy CODESWARM_OMIUM=1 (== observability). Keeps every entrypoint (CLI, web,
    library) consistent without importing Config here.
    """
    if config is not None:
        m = str(getattr(config, "omium_mode", "") or "").strip().lower()
        if m in ("off", "observability", "corpus"):
            return m
        if getattr(config, "omium_enabled", False):
            return "observability"
    m = (os.environ.get("CODESWARM_OMIUM_MODE") or "").strip().lower()
    if m in ("off", "observability", "corpus"):
        return m
    if os.environ.get("CODESWARM_OMIUM", "").lower() in ("1", "true", "yes"):
        return "observability"
    return "off"


def omium_enabled(config=None) -> bool:
    """True when the OBSERVABILITY integration (Mode-1) should activate."""
    return _mode(config) == "observability"


def corpus_mode_enabled(config=None) -> bool:
    """True when the CORPUS integration (Mode-2) should activate."""
    return _mode(config) == "corpus"


class OmiumRun:
    """Per-run Omium context: init + execution anchor + checkpoints + finalize.

    Constructed once per codeswarm run. Every method is best-effort and never
    raises into the caller — an Omium outage must not fail a local run.
    """

    def __init__(self, config) -> None:
        self.config = config
        self.execution_id: str | None = None
        self._om = None                 # the omium module (None => inactive)
        self._client = None             # RemoteOmiumClient for explicit checkpoints
        self._base: str | None = None   # api base incl. /api/v1
        self._key: str | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self, task, run_id: str) -> "OmiumRun":
        try:
            import omium
            from omium.remote_client import RemoteOmiumClient
        except Exception as e:  # noqa: BLE001 - omium optional
            log.warning("omium SDK not available; tracing disabled: %s", e)
            return self
        try:
            omium.init(project=_PROJECT)  # reads OMIUM_API_URL/KEY from env + ~/.omium
            cfg = omium.get_current_config()
            self._om = omium
            self._base = cfg.api_base_url            # includes /api/v1
            self._key = cfg.api_key
            wf = self._resolve_workflow_id()

            import httpx

            body = {
                "agent_id": f"codeswarm-{task.id}",
                "input_data": {"task": task.id, "run_id": run_id},
                "metadata": {
                    "source": "codeswarm",
                    "task_id": task.id,
                    "run_id": run_id,
                    "difficulty": getattr(task, "difficulty", ""),
                },
            }
            if wf:
                body["workflow_id"] = wf
            r = httpx.post(
                f"{self._base}/executions",
                headers={"X-API-Key": self._key, "Content-Type": "application/json"},
                json=body,
                timeout=15,
            )
            if r.status_code in (200, 201):
                self.execution_id = r.json().get("id")
                omium.set_execution_id(self.execution_id)
                base_nov1 = self._base[:-7] if self._base.endswith("/api/v1") else self._base
                self._client = RemoteOmiumClient(api_key=self._key, api_url=base_nov1.rstrip("/"))
                self._client.set_execution_context(self.execution_id, agent_id="codeswarm")
                log.info("omium execution %s (workflow=%s)", self.execution_id, wf)
            else:
                log.warning("omium execution anchor failed: HTTP %s %s", r.status_code, r.text[:200])
        except Exception as e:  # noqa: BLE001 - fail-soft
            log.warning("omium start failed (continuing local run): %s", e)
        return self

    def _resolve_workflow_id(self) -> str | None:
        wf = getattr(self.config, "omium_workflow_id", None) or os.environ.get(
            "CODESWARM_OMIUM_WORKFLOW_ID"
        )
        if wf:
            return wf
        try:
            import httpx

            r = httpx.get(
                f"{self._base}/workflows",
                headers={"X-API-Key": self._key},
                params={"page": 1, "page_size": 50},
                timeout=15,
            )
            if r.status_code == 200:
                for w in r.json().get("workflows", []):
                    if w.get("name") == _PROJECT:
                        return w.get("id")
        except Exception as e:  # noqa: BLE001
            log.warning("omium workflow lookup failed: %s", e)
        return None

    async def checkpoint(self, name: str, state: dict) -> None:
        if not (self._client and self.execution_id):
            return
        try:
            await self._client.create_checkpoint(
                checkpoint_name=name,
                state=state or {},
                execution_id=self.execution_id,
                agent_id=_AGENT_ID,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("omium checkpoint '%s' failed: %s", name, e)

    def finish(self, verdict) -> None:
        if not (self.execution_id and self._base):
            return
        # Status finalize is ON by default (the server-side UUID->str 500 was
        # fixed + deployed 2026-07-06, fleet @66508dd). CODESWARM_OMIUM_SET_STATUS=0
        # remains as a kill-switch if the endpoint ever regresses (it used to fire
        # a Slack alert per call).
        if os.environ.get("CODESWARM_OMIUM_SET_STATUS", "1").lower() in ("0", "false", "no"):
            return
        try:
            import httpx

            passed = bool(verdict and verdict.passed)
            status = "completed" if passed else "failed"
            httpx.patch(
                f"{self._base}/executions/{self.execution_id}/status",
                headers={"X-API-Key": self._key, "Content-Type": "application/json"},
                json={
                    "status": status,
                    "output_data": (verdict.signals if verdict else {}),
                    "error_message": None if passed else "codeswarm task did not pass its oracle",
                },
                timeout=15,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("omium finalize failed: %s", e)

    @property
    def dashboard_url(self) -> str | None:
        if self.execution_id:
            return f"https://app.omium.ai/executions/{self.execution_id}"
        return None


class OmiumExecutor:
    """Wraps an inner Executor; per step opens an Omium span + writes a checkpoint.

    Satisfies the same ``run_step`` protocol as LocalExecutor and simply delegates,
    so the WorkflowEngine is unchanged. Inactive (pure passthrough) if the
    OmiumRun failed to start.
    """

    def __init__(self, inner, omium_run: OmiumRun) -> None:
        self._inner = inner
        self._run = omium_run

    async def run_step(self, step, ctx):
        om = self._run._om
        if om is None:
            return await self._inner.run_step(step, ctx)

        # Checkpoint the pre-step state (a real recovery point).
        state = {}
        try:
            st = getattr(ctx, "state", None)
            state = {
                "step_id": getattr(step, "id", None),
                "plan_index": getattr(st, "plan_index", None),
                "files": sorted(getattr(st, "files", {}) or {}),
            }
        except Exception:  # noqa: BLE001
            pass
        await self._run.checkpoint(f"before:{getattr(step, 'id', 'step')}", state)

        async def _delegate():
            return await self._inner.run_step(step, ctx)

        try:
            traced = om.trace(name=f"step:{getattr(step, 'id', 'step')}", span_type="task")(_delegate)
        except Exception:  # noqa: BLE001
            traced = _delegate
        return await traced()


# ===========================================================================
# Mode-2: CORPUS. Drive the execution-engine so each FAILING codeswarm task
# becomes a genuine ``omium.execution.failed`` carrying codeswarm's REAL failure
# signature, so the recovery-orchestrator mints a failures + recovery_attempt row
# keyed on that signature (diverse per task family). See DESIGN / the report.
#
# HOW IT WORKS (the seam, verified read-only against the platform):
#   * The execution-engine already RUNS an inline langgraph ``workflow_definition``
#     passed in ``metadata`` on POST /executions (public API, X-API-Key). A node
#     carrying ``force_error`` raises RuntimeError(message) — the SAME internal seam
#     scripts/stress/recovery_load.py uses to drive REAL failures.
#   * On the terminal-failure path EE calls ``_publish_execution_failed`` which emits
#     ``omium.execution.failed`` with ``failure_signature_hash = sha256(error_type |
#     NORMALIZE_v1(error.message) | failing_node)``. Because we put codeswarm's real
#     ``signature_token`` (exception kinds x failing test files) into the force_error
#     message, EE's signature is a DETERMINISTIC function of codeswarm's real failure
#     — honest and diverse (14+ task families -> many signature clusters).
#   * The recovery consumer INSERTs a ``failures`` row + ``recovery_attempt`` row for
#     any valid event (needs tenant_id + execution_id + signature — all present).
#
# HONESTY: the signature is derived ONLY from codeswarm's real task failure; we never
# fabricate a signal. If a run PASSES there is nothing to mint (returns None). We do
# NOT inject synthetic transient/429/schema tokens to trip the migrated-class router.
# The platform recognizes the "failed its pytest oracle" marker as the migrated
# LOW-risk class ``code_test_failure`` (twin routers + MIGRATED_CLASSES), and the
# seeded pinned workflow (seed_codeswarm_corpus_workflow.sql) makes the authoritative
# re-run reproduce the failure -> verified_failure -> a WORM reward row is minted.
# ===========================================================================

import re as _re


def extract_dominant_failure(trajectory) -> dict | None:
    """Return the dominant (first) ``failure`` event payload from a Trajectory, or None.

    None means the run had no recorded failure (it passed / never failed) — there is
    nothing honest to mint. The returned dict carries codeswarm's real, stable failure
    signal: ``signature_token`` (kinds x failing files), ``error_type``, ``error_kinds``,
    ``failing_tests``, ``summary``.
    """
    events = getattr(trajectory, "events", None) or []
    for ev in events:
        if getattr(ev, "kind", None) == "failure":
            payload = dict(getattr(ev, "payload", {}) or {})
            payload.setdefault("step_id", getattr(ev, "step_id", None))
            return payload
    return None


# The CONSTANT failing-node name, shared with the platform's seeded codeswarm workflow
# (Omium-platform/scripts/seed_codeswarm_corpus_workflow.sql, workflow id
# 8b628198-fd99-53ce-898d-2b53c647374d version 1). RERUNNING fetches THAT pinned def
# from workflow-manager — NOT this inline def — so the failing node must be the SAME
# name on both sides or checkpoint replay can't reproduce at the right node. The WHERE
# signal (failing test files) still reaches the signature via the MESSAGE: the
# signature_token (kinds x files) is embedded there and NORMALIZE_v1 preserves words.
CORPUS_NODE_NAME = "cs_oracle"

# ── GREEN (desirable) arm constants ─────────────────────────────────────────
# The seeded GREEN pool (Omium-platform/scripts/seed_codeswarm_corpus_green_pool.sql):
# ONE workflow id, N pinned versions, each version's cs_oracle node carrying
# `force_error_once` + a per-version `force_error_once_key` ("cs-green-<v>").
# Mechanics (mirrors the PROVEN scripts/stress/recovery_load.py --mode green +
# pr2_seed_l4_workflow_c9997.sql GREEN):
#   * the ORIGINAL run executes the INLINE def below -> force_error_once counter
#     (Redis, keyed GLOBALLY on force_error_once_key) increments to 1 -> raises ->
#     a real omium.execution.failed whose signature derives from codeswarm's real
#     failure message;
#   * the authoritative RERUN fetches the pinned (green workflow, version=v) def
#     from workflow-manager; its cs_oracle uses the SAME key -> counter hits 2 ->
#     SUCCEEDS -> emit_side_effect writes the real orders ground-truth row ->
#     Tier-1 signature_gone ∧ side-effect present -> verified_SUCCESS -> a
#     DESIRABLE WORM reward row.
# KEY EXHAUSTION: each key mints ONE green row, then its counter sits at 2 and a
# later original using it would SUCCEED first try (no failure -> nothing minted —
# a wasted run, never a fabricated row). EE bounds the counter with a 1h TTL set
# on first use, and the EE-pod helper
# (Omium-platform/scripts/stress/clear_codeswarm_green_keys.py) clears the pool
# explicitly between batches. GreenKeyAllocator below hands each version out AT
# MOST ONCE per clear-cycle so keys are never silently burned.
GREEN_WORKFLOW_NAME = "codeswarm-corpus-cs_oracle-green"
GREEN_KEY_PREFIX = "cs-green-"
DEFAULT_GREEN_POOL_SIZE = 40


def trajectory_recovered(trajectory) -> bool:
    """True ONLY when the trajectory shows the swarm ACTUALLY recovered.

    Eligibility for the GREEN (desirable) arm is honest by construction: there must
    be a REAL recorded failure event AND the final oracle verdict must have PASSED
    (the failure was genuinely recoverable — the swarm fixed it within budget).
    A run with no failure has nothing to mint; a run whose verdict failed is the
    RED (permanent) arm. Never fabricated (R12): polarity mirrors the trajectory's
    real outcome.
    """
    verdict = getattr(trajectory, "verdict", None)
    if not (verdict is not None and getattr(verdict, "passed", False)):
        return False
    return extract_dominant_failure(trajectory) is not None


def _sanitize_node_name(failure: dict, task_id: str) -> str:
    """The constant seeded failing-node name (see CORPUS_NODE_NAME above).

    Signature diversity is carried ENTIRELY by the force_error message (which embeds
    codeswarm's real signature_token); the node axis is pinned so the seeded pinned
    workflow version can reproduce the failure on the authoritative re-run.
    """
    return CORPUS_NODE_NAME


def build_corpus_failure_message(failure: dict, task_id: str) -> str:
    """The honest force_error message embedding codeswarm's REAL failure identity.

    EE hashes NORMALIZE_v1(message) into the signature, so this string IS the diversity
    lever — it must derive from the real failure and differ per failure family. We lead
    with the stable ``signature_token`` (kinds x failing files) which codeswarm already
    computes to be "stable AND discriminating".
    """
    token = (
        failure.get("signature_token")
        or failure.get("error_type")
        or failure.get("summary")
        or "unknown"
    )
    kinds = failure.get("error_kinds") or []
    kinds_str = "+".join(str(k) for k in kinds) if kinds else str(
        failure.get("error_type") or "test_failure"
    )
    return (
        f"codeswarm task {task_id} failed its pytest oracle "
        f"[{kinds_str}]: {token}"
    )


def build_corpus_workflow_definition(node_name: str, message: str) -> dict:
    """Inline langgraph def whose middle node PERMANENTLY force_errors ``message``.

    Mirrors the proven scripts/stress/recovery_load.py RED shape (ingest -> fail ->
    summarize). ``force_error`` makes EE's node raise RuntimeError(message) so the real
    terminal-failure path (and _publish_execution_failed) fires.
    """
    return {
        "name": f"codeswarm-corpus-{node_name}",
        "nodes": [
            {"name": "ingest", "function": "ingest_node"},
            {"name": node_name, "function": "process_node", "force_error": message},
            {"name": "summarize", "function": "summarize_node"},
        ],
        "edges": [
            {"from": "START", "to": "ingest"},
            {"from": "ingest", "to": node_name},
            {"from": node_name, "to": "summarize"},
            {"from": "summarize", "to": "END"},
        ],
    }


def build_corpus_execution_body(
    task,
    run_id: str,
    workflow_id: str,
    workflow_definition: dict,
    failure: dict,
) -> dict:
    """The POST /executions body that drives EE to a codeswarm-derived failure."""
    return {
        "workflow_id": workflow_id,
        "agent_id": f"codeswarm-{task.id}",
        "input_data": {"task": task.id, "run_id": run_id},
        "metadata": {
            "workflow_type": "langgraph",
            "workflow_definition": workflow_definition,
            # workflow_version rides onto the published event -> failures.workflow_version.
            # A real (>0) value is required for the RERUNNING re-run to pin a version once a
            # matching workflow is seeded in workflow-manager (else it escalates
            # read_model_miss). Forward-compatible default; 0 is fine for failures-only corpus.
            "workflow_version": 1,
            "source": "codeswarm-corpus",
            "corpus_polarity": "red",
            "task_id": task.id,
            "run_id": run_id,
            "difficulty": getattr(task, "difficulty", ""),
            # Carry codeswarm's real failure identity for downstream correlation.
            "codeswarm_signature_token": failure.get("signature_token"),
            "codeswarm_error_kinds": failure.get("error_kinds") or [],
            "codeswarm_failing_tests": failure.get("failing_tests") or [],
        },
    }


def _orders_postconditions(node_name: str) -> list[dict]:
    """The §3.7 programmatic orders db_row contract (same shape as the proven seeds)."""
    return [
        {
            "step_id": node_name,
            "effect_kind": "db_row",
            "assertion": {
                "store": "orders",
                "match": {"execution_id": "$execution_id", "status": "succeeded"},
                "expect": "exactly_one",
            },
            "live_probe": "db:orders?execution_id=$execution_id",
        }
    ]


def build_corpus_green_workflow_definition(
    node_name: str, message: str, force_key: str
) -> dict:
    """Inline def whose middle node fails ONCE (shared Redis counter) then heals.

    Mirrors the proven recovery_load.py ``_green_def`` shape. The ONLY binding
    contract with the seeded pinned version is (a) the same ``force_error_once_key``
    (the cross-attempt counter is keyed on it, NOT the workflow name) and (b) the
    same failing-node name so checkpoint replay reproduces at the right node. The
    message embeds codeswarm's REAL failure identity — same builder as RED, so the
    green rows land in the SAME signature cluster (the floor is per-signature:
    yield >= 200 AND desirable >= 50).
    """
    return {
        "name": GREEN_WORKFLOW_NAME,
        "nodes": [
            {"name": "ingest", "function": "ingest_node"},
            {
                "name": node_name,
                "function": "process_node",
                "force_error_once": message,
                "force_error_once_key": force_key,
                "emit_side_effect": "orders",
            },
            {"name": "summarize", "function": "summarize_node"},
        ],
        "edges": [
            {"from": "START", "to": "ingest"},
            {"from": "ingest", "to": node_name},
            {"from": node_name, "to": "summarize"},
            {"from": "summarize", "to": "END"},
        ],
        "postconditions": _orders_postconditions(node_name),
    }


def build_corpus_green_execution_body(
    task,
    run_id: str,
    workflow_id: str,
    workflow_definition: dict,
    failure: dict,
    *,
    version: int,
    force_key: str,
) -> dict:
    """POST /executions body for the GREEN (desirable) arm.

    ``workflow_version`` MUST be the seeded pool version whose pinned def carries
    ``force_key`` — the RERUNNING re-run fetches (workflow_id, version) from
    workflow-manager and heals only if its key matches the original's counter.
    """
    body = build_corpus_execution_body(task, run_id, workflow_id, workflow_definition, failure)
    body["metadata"]["workflow_version"] = int(version)
    body["metadata"]["corpus_polarity"] = "green"
    body["metadata"]["force_key"] = force_key
    return body


def _default_workflow_id() -> str:
    """A stable, valid UUID for codeswarm's corpus workflow.

    executions.workflow_id is a free-text VARCHAR (no FK), but the API model coerces it
    to a UUID, so it MUST be a valid UUID string. A configured/resolved id is preferred;
    this deterministic uuid5 is the fail-safe default so the anchor POST always validates.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "codeswarm.omium.corpus.workflow"))


def _default_green_workflow_id() -> str:
    """Deterministic id of the seeded GREEN pool workflow (same uuid5 derivation as RED).

    MUST equal the id in Omium-platform/scripts/seed_codeswarm_corpus_green_pool.sql:
    82a67367-4d6d-5abd-97d2-00d33a7ef863.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "codeswarm.omium.corpus.workflow.green"))


class GreenKeyAllocator:
    """File-backed round-robin allocator over the seeded GREEN pool versions 1..N.

    Each seeded version v carries force_error_once_key "cs-green-<v>" whose global
    Redis counter mints EXACTLY ONE green row and is then exhausted (a later original
    on the same key succeeds first-try -> nothing minted, a wasted run) until the
    EE-pod key-clear helper resets it (or its 1h TTL lapses). This allocator hands
    each version out AT MOST ONCE per clear-cycle:

      * ``allocate()``     -> version int, or None when the cycle is exhausted
                              (caller must SKIP the green mint — never fall back to
                              red for a recovered trajectory);
      * ``release(v)``     -> return v to the pool (ONLY when the POST never reached
                              EE, i.e. the counter was never touched);
      * ``mark_cleared()`` -> start a fresh cycle AFTER the EE-pod helper ran.

    State is a small JSON file (atomic tmp+rename write) so sequential CLI runs and
    the volume runner share one cycle. Single-writer by design (the volume runner is
    one process); concurrent writers at worst waste runs, never fabricate rows.
    """

    def __init__(self, state_path: str, pool_size: int | None = None) -> None:
        self.state_path = str(state_path)
        env_size = os.environ.get("CODESWARM_GREEN_POOL_SIZE")
        self.pool_size = int(pool_size or env_size or DEFAULT_GREEN_POOL_SIZE)
        self._state = self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> dict:
        import json

        try:
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
            return {
                "next": int(st.get("next", 1)),
                "freed": [int(v) for v in st.get("freed", [])],
            }
        except Exception:  # noqa: BLE001 — missing/corrupt state starts a fresh cycle
            return {"next": 1, "freed": []}

    def _save(self) -> None:
        import json
        import os as _os

        tmp = f"{self.state_path}.tmp"
        try:
            _os.makedirs(_os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"next": self._state["next"], "freed": self._state["freed"],
                           "pool_size": self.pool_size}, fh)
            _os.replace(tmp, self.state_path)
        except Exception as e:  # noqa: BLE001 — persistence is best-effort
            log.warning("green pool state save failed (%s): %s", self.state_path, e)

    # -- API -----------------------------------------------------------------
    @property
    def remaining(self) -> int:
        return max(0, self.pool_size - self._state["next"] + 1) + len(self._state["freed"])

    def allocate(self) -> int | None:
        if self._state["freed"]:
            v = self._state["freed"].pop(0)
        elif self._state["next"] <= self.pool_size:
            v = self._state["next"]
            self._state["next"] += 1
        else:
            return None
        self._save()
        return int(v)

    def release(self, version: int) -> None:
        """Return a version whose POST never reached EE (counter untouched)."""
        v = int(version)
        if v not in self._state["freed"]:
            self._state["freed"].append(v)
            self._save()

    def mark_cleared(self) -> None:
        """Start a fresh cycle. Call ONLY after the EE-pod key-clear helper succeeded
        (clearing a key with an in-flight green recovery would flip its re-run back
        to a failure — the volume runner guards this by settling greens first)."""
        self._state = {"next": 1, "freed": []}
        self._save()


class OmiumCorpusRun:
    """Per-run Mode-2 context: turn a failing codeswarm task into real recovery corpus.

    Best-effort + fail-soft (an Omium outage never fails the local run). Constructed
    per run; ``mint`` is a no-op that returns None when the run passed or Omium is
    unreachable. ``api_base``/``api_key``/``workflow_id`` may be injected (tests, or an
    env-only deployment) to bypass the omium SDK entirely.
    """

    def __init__(
        self,
        config,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        workflow_id: str | None = None,
        green_workflow_id: str | None = None,
        green_allocator: "GreenKeyAllocator | None" = None,
    ) -> None:
        self.config = config
        self._base = api_base
        self._key = api_key
        self._workflow_id = workflow_id
        self._green_workflow_id = green_workflow_id
        self._green_allocator_inst = green_allocator
        self.execution_id: str | None = None
        # Set on a successful mint: "red" (verified_failure arm) or "green"
        # (verified_success arm). None when nothing was minted.
        self.polarity: str | None = None
        # The seeded pool version used by a green mint (for logs/manifests).
        self.green_version: int | None = None

    # -- config resolution -------------------------------------------------
    def _resolve_config(self) -> bool:
        """Populate base URL + API key. Prefer injected/env values, then the omium SDK.

        Returns True when both a base and a key are known. Fail-soft: never raises.
        """
        if not self._base or not self._key:
            env_base = os.environ.get("OMIUM_API_URL")
            env_key = os.environ.get("OMIUM_API_KEY")
            if env_base and env_key:
                # Normalise to include the /api/v1 suffix the executions route lives under.
                base = env_base.rstrip("/")
                if not base.endswith("/api/v1"):
                    base = base + "/api/v1"
                self._base = self._base or base
                self._key = self._key or env_key
        if not self._base or not self._key:
            try:
                import omium

                omium.init(project=_PROJECT)  # reads OMIUM_API_URL/KEY + ~/.omium
                cfg = omium.get_current_config()
                self._base = self._base or cfg.api_base_url  # includes /api/v1
                self._key = self._key or cfg.api_key
            except Exception as e:  # noqa: BLE001 — omium optional; fail-soft
                log.warning("omium corpus: SDK config unavailable: %s", e)
        return bool(self._base and self._key)

    def _resolve_workflow_id(self) -> str:
        if self._workflow_id:
            return self._workflow_id
        wf = getattr(self.config, "omium_workflow_id", None) or os.environ.get(
            "CODESWARM_OMIUM_WORKFLOW_ID"
        )
        return wf or _default_workflow_id()

    def _resolve_green_workflow_id(self) -> str:
        if self._green_workflow_id:
            return self._green_workflow_id
        return os.environ.get("CODESWARM_OMIUM_GREEN_WORKFLOW_ID") or _default_green_workflow_id()

    def _green_allocator(self) -> "GreenKeyAllocator":
        if self._green_allocator_inst is None:
            state = os.environ.get("CODESWARM_GREEN_POOL_STATE") or os.path.join(
                getattr(self.config, "runs_dir", "runs") or "runs", ".cs_green_pool.json"
            )
            self._green_allocator_inst = GreenKeyAllocator(state)
        return self._green_allocator_inst

    # -- minting -----------------------------------------------------------
    def mint(self, task, run_id: str, trajectory) -> str | None:
        """Turn a codeswarm trajectory into an honest corpus row via EE.

        Polarity MIRRORS the trajectory's REAL outcome (R12 — never fabricated):
          * no failure event            -> nothing to mint (None);
          * failure + final verdict PASSED (the swarm actually recovered) -> GREEN
            arm: fail-once seeded workflow -> the authoritative re-run HEALS ->
            verified_success (desirable);
          * failure + final verdict FAILED (never fixed within budget) -> RED arm:
            permanent force_error -> the re-run reproduces -> verified_failure.

        Returns the created execution_id or None. Never raises into the caller.
        """
        failure = extract_dominant_failure(trajectory)
        if failure is None:
            log.info("omium corpus: run %s passed (no failure) — nothing to mint", run_id)
            return None
        if not self._resolve_config():
            log.warning("omium corpus: no API base/key — skipping mint for %s", run_id)
            return None
        if trajectory_recovered(trajectory):
            return self._mint_green(task, run_id, failure)
        return self._mint_red(task, run_id, failure)

    def _post_execution(self, body: dict) -> str | None:
        """POST /executions; returns the execution id or None. Fail-soft."""
        try:
            import httpx

            r = httpx.post(
                f"{self._base}/executions",
                headers={"X-API-Key": self._key, "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            if r.status_code in (200, 201):
                return r.json().get("id")
            log.warning(
                "omium corpus: POST /executions failed HTTP %s %s",
                r.status_code, r.text[:200],
            )
        except Exception as e:  # noqa: BLE001 — fail-soft
            log.warning("omium corpus: mint failed (continuing local run): %s", e)
        return None

    def _mint_red(self, task, run_id: str, failure: dict) -> str | None:
        workflow_id = self._resolve_workflow_id()
        node_name = _sanitize_node_name(failure, task.id)
        message = build_corpus_failure_message(failure, task.id)
        wf_def = build_corpus_workflow_definition(node_name, message)
        body = build_corpus_execution_body(task, run_id, workflow_id, wf_def, failure)
        eid = self._post_execution(body)
        if eid:
            self.execution_id = eid
            self.polarity = "red"
            log.info(
                "omium corpus: minted RED (verified_failure) execution %s (task=%s sig_token=%s)",
                eid, task.id, failure.get("signature_token"),
            )
        return eid

    def _mint_green(self, task, run_id: str, failure: dict) -> str | None:
        """GREEN arm: the swarm recovered, so mint through the fail-once pool.

        A recovered trajectory is NEVER minted red — if no pool key is available the
        mint is SKIPPED (honesty over volume) and the caller is told to clear keys.
        """
        allocator = self._green_allocator()
        version = allocator.allocate()
        if version is None:
            log.warning(
                "omium corpus: GREEN pool exhausted — run the EE-pod key-clear helper "
                "(Omium-platform/scripts/stress/clear_codeswarm_green_keys.py) then reset "
                "the cycle (GreenKeyAllocator.mark_cleared / corpus_runner --clear-keys-cmd). "
                "SKIPPING green mint for %s (a recovered trajectory is never minted red).",
                run_id,
            )
            return None
        force_key = f"{GREEN_KEY_PREFIX}{version}"
        # SAME message builder as RED -> red+green land in the SAME signature cluster
        # (the training floor is per-signature: yield >= 200 AND desirable >= 50).
        message = build_corpus_failure_message(failure, task.id)
        node_name = _sanitize_node_name(failure, task.id)
        wf_def = build_corpus_green_workflow_definition(node_name, message, force_key)
        body = build_corpus_green_execution_body(
            task, run_id, self._resolve_green_workflow_id(), wf_def, failure,
            version=version, force_key=force_key,
        )
        eid = self._post_execution(body)
        if eid:
            self.execution_id = eid
            self.polarity = "green"
            self.green_version = version
            log.info(
                "omium corpus: minted GREEN (verified_success) execution %s "
                "(task=%s version=%s key=%s sig_token=%s)",
                eid, task.id, version, force_key, failure.get("signature_token"),
            )
            return eid
        # The POST never reached EE -> the key's counter is untouched; reuse it.
        allocator.release(version)
        return None
