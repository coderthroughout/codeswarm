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


def omium_enabled(config=None) -> bool:
    """True when the Omium integration should activate (CLI flag or env)."""
    if config is not None and getattr(config, "omium_enabled", False):
        return True
    return os.environ.get("CODESWARM_OMIUM", "").lower() in ("1", "true", "yes")


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
        # The staging execution-engine's PATCH /executions/{id}/status currently
        # 500s (a server-side UUID->str bug; fix on main, not yet deployed) and
        # fires a Slack alert on every call. Skip the status update until that
        # deploys — the execution + checkpoints + traces still land; only the
        # final status label is deferred. Set CODESWARM_OMIUM_SET_STATUS=1 to
        # re-enable once the server fix is live.
        if os.environ.get("CODESWARM_OMIUM_SET_STATUS", "").lower() not in ("1", "true", "yes"):
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
