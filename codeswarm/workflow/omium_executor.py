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
            "task_id": task.id,
            "run_id": run_id,
            "difficulty": getattr(task, "difficulty", ""),
            # Carry codeswarm's real failure identity for downstream correlation.
            "codeswarm_signature_token": failure.get("signature_token"),
            "codeswarm_error_kinds": failure.get("error_kinds") or [],
            "codeswarm_failing_tests": failure.get("failing_tests") or [],
        },
    }


def _default_workflow_id() -> str:
    """A stable, valid UUID for codeswarm's corpus workflow.

    executions.workflow_id is a free-text VARCHAR (no FK), but the API model coerces it
    to a UUID, so it MUST be a valid UUID string. A configured/resolved id is preferred;
    this deterministic uuid5 is the fail-safe default so the anchor POST always validates.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "codeswarm.omium.corpus.workflow"))


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
    ) -> None:
        self.config = config
        self._base = api_base
        self._key = api_key
        self._workflow_id = workflow_id
        self.execution_id: str | None = None

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

    # -- minting -----------------------------------------------------------
    def mint(self, task, run_id: str, trajectory) -> str | None:
        """Drive EE to emit omium.execution.failed for a FAILED codeswarm run.

        Returns the created execution_id, or None when the run passed (nothing to mint)
        or Omium is unreachable. Never raises into the caller.
        """
        failure = extract_dominant_failure(trajectory)
        if failure is None:
            log.info("omium corpus: run %s passed (no failure) — nothing to mint", run_id)
            return None
        if not self._resolve_config():
            log.warning("omium corpus: no API base/key — skipping mint for %s", run_id)
            return None

        workflow_id = self._resolve_workflow_id()
        node_name = _sanitize_node_name(failure, task.id)
        message = build_corpus_failure_message(failure, task.id)
        wf_def = build_corpus_workflow_definition(node_name, message)
        body = build_corpus_execution_body(task, run_id, workflow_id, wf_def, failure)

        try:
            import httpx

            r = httpx.post(
                f"{self._base}/executions",
                headers={"X-API-Key": self._key, "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            if r.status_code in (200, 201):
                self.execution_id = r.json().get("id")
                log.info(
                    "omium corpus: minted failing execution %s (task=%s sig_token=%s)",
                    self.execution_id, task.id, failure.get("signature_token"),
                )
                return self.execution_id
            log.warning(
                "omium corpus: POST /executions failed HTTP %s %s",
                r.status_code, r.text[:200],
            )
        except Exception as e:  # noqa: BLE001 — fail-soft
            log.warning("omium corpus: mint failed (continuing local run): %s", e)
        return None
