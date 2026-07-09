"""corpus_runner — bounded VOLUME runner for the Wave-A training floor.

Repeatedly runs 1-3 chosen builtin tasks with --omium-mode=corpus semantics,
mixing the RED (verified_failure) and GREEN (verified_success) arms CONCENTRATED
on those tasks' signatures so a real failure-signature cluster crosses the
per-signature training floor: yield >= 200 AND desirable >= 50.

HONESTY (R12): the runner never forces polarity. It only picks the run's RETRY
BUDGET (a legitimate run parameter):
  * red-intent  -> --max-retries 1: the mock swarm's first (broken) attempt is
    its ONLY attempt, so the failure genuinely goes unfixed -> verdict FAILED ->
    OmiumCorpusRun routes it to the RED arm;
  * green-intent -> the default budget (3): the swarm fails once then actually
    fixes it -> verdict PASSED -> routed to the GREEN (fail-once pool) arm.
Either way OmiumCorpusRun.mint derives polarity from the trajectory's REAL
outcome, and the signature from the REAL first failure — identical between the
two intents for a given task, so both arms land in ONE signature cluster.

COST NOTE: repeat signatures get CHEAPER over time — after warm-up the P-R3
exact/NEAR-match short-circuits DIAGNOSING to 0 LLM calls, so a concentrated
run is the cheap way to volume.

GREEN pacing + key hygiene: each green mint is polled to a terminal status
(resume_parent flips the original to 'completed' on verified_success) BEFORE the
next run, so when the pool exhausts there are no in-flight green recoveries and
clearing the Redis fail-once counters is safe. Keys are cleared via
--clear-keys-cmd (the kubectl pipe into the EE pod) or manually between runs.

Typical staging invocation (parent runs this; see the module __main__ help):

  OMIUM_API_URL=https://api-staging.omium.ai OMIUM_API_KEY=om_... \
  python -m codeswarm.corpus_runner \
      --tasks math_utils,fix_avg,lru_cache \
      --target-yield 210 --target-desirable 60 \
      --max-runs 800 --max-minutes 300 \
      --clear-keys-cmd 'cat .../clear_codeswarm_green_keys.py | kubectl -n omium exec -i "$EE" -c execution-engine -- python - --pool-size 40'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field

from codeswarm.cli import _build_engine, _write_trajectory
from codeswarm.config import Config
from codeswarm.tasks import get_task, list_tasks
from codeswarm.workflow.omium_executor import (
    DEFAULT_GREEN_POOL_SIZE,
    GreenKeyAllocator,
    OmiumCorpusRun,
    build_corpus_failure_message,
    extract_dominant_failure,
    trajectory_recovered,
)

# Never point volume at prod. Staging is api-staging.omium.ai; the prod account id
# and the prod API host are refused outright (mirrors recovery_load's guard).
_PROD_MARKERS = ("650790810654", "//api.omium.ai")


@dataclass
class TaskProgress:
    """Per-task (== per-signature-cluster) targets + live counters."""

    task_id: str
    red_target: int
    green_target: int
    runs: int = 0
    red_minted: int = 0
    green_minted: int = 0
    green_completed: int = 0        # original flipped to 'completed' (verified_success path)
    green_poll_timeouts: int = 0
    mint_failures: int = 0
    signature_message: str | None = None  # the EE-side force_error message (grep key)

    @property
    def red_remaining(self) -> int:
        return max(0, self.red_target - self.red_minted)

    @property
    def green_remaining(self) -> int:
        return max(0, self.green_target - self.green_minted)

    @property
    def done(self) -> bool:
        return self.red_remaining == 0 and self.green_remaining == 0

    def summary(self) -> dict:
        return {
            "task": self.task_id,
            "runs": self.runs,
            "red_minted": f"{self.red_minted}/{self.red_target}",
            "green_minted": f"{self.green_minted}/{self.green_target}",
            "green_completed": self.green_completed,
            "green_poll_timeouts": self.green_poll_timeouts,
            "mint_failures": self.mint_failures,
            "signature_message": self.signature_message,
        }


def choose_polarity(progress: TaskProgress, green_keys_available: bool) -> str | None:
    """Pick the next run's intent for a task: 'green', 'red', or None (done/blocked).

    Prefers the arm with the LARGER remaining deficit so red/green interleave
    naturally (a 150/60 split runs roughly 2.5 red per green). Green requires an
    available pool key; when the pool is dry the task keeps minting red and the
    caller decides when to clear keys.
    """
    green_ok = progress.green_remaining > 0 and green_keys_available
    red_ok = progress.red_remaining > 0
    if green_ok and red_ok:
        return "red" if progress.red_remaining >= progress.green_remaining else "green"
    if green_ok:
        return "green"
    if red_ok:
        return "red"
    return None


def _refuse_prod(base: str | None) -> str | None:
    for marker in _PROD_MARKERS:
        if base and marker in base:
            return f"REFUSING: OMIUM_API_URL looks like prod ({marker})"
    return None


class CorpusRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.allocator = GreenKeyAllocator(args.pool_state, pool_size=args.green_pool_size)
        self.progress: dict[str, TaskProgress] = {}
        red_target = max(0, args.target_yield - args.target_desirable)
        for task_id in args.task_ids:
            self.progress[task_id] = TaskProgress(
                task_id=task_id, red_target=red_target, green_target=args.target_desirable
            )
        self.total_runs = 0
        self.consecutive_mint_failures = 0
        self.unsettled_greens = 0  # greens not seen terminal since the last key clear
        self.clears_run = 0
        self.deadline = time.time() + args.max_minutes * 60
        self._manifest = None
        if args.manifest:
            os.makedirs(os.path.dirname(args.manifest) or ".", exist_ok=True)
            self._manifest = open(args.manifest, "a", encoding="utf-8")
        # Resolved lazily off the first successful mint (for green status polling).
        self._api_base: str | None = None
        self._api_key: str | None = None

    # -- plumbing ------------------------------------------------------------
    def _log(self, record: dict) -> None:
        record["ts"] = int(time.time())
        line = json.dumps(record, sort_keys=True)
        print(f"[corpus] {line}", flush=True)
        if self._manifest:
            self._manifest.write(line + "\n")
            self._manifest.flush()

    def _bounds_exceeded(self) -> str | None:
        if self.total_runs >= self.args.max_runs:
            return f"max-runs reached ({self.args.max_runs})"
        if time.time() >= self.deadline:
            return f"max-minutes reached ({self.args.max_minutes})"
        if self.consecutive_mint_failures >= self.args.stop_after_failures:
            return (
                f"{self.consecutive_mint_failures} consecutive mint failures "
                "(Omium unreachable / auth / seed missing?)"
            )
        return None

    # -- one run ---------------------------------------------------------------
    def _run_task_once(self, task_id: str, intent: str) -> None:
        """One full codeswarm run + honest mint. ``intent`` only sets the retry budget."""
        prog = self.progress[task_id]
        task = get_task(task_id)
        max_retries = 1 if intent == "red" else self.args.green_retries
        config = Config.from_env(
            omium_mode="corpus",
            runs_dir=self.args.runs_dir,
            max_retries=max_retries,
        )
        run_id = f"{task_id}-{uuid.uuid4().hex[:8]}"
        engine = _build_engine(config, task, use_mock=not self.args.real)
        trajectory = asyncio.run(engine.run(task, run_id=run_id))
        _write_trajectory(config, trajectory)
        prog.runs += 1
        self.total_runs += 1

        failure = extract_dominant_failure(trajectory)
        if failure is not None and prog.signature_message is None:
            prog.signature_message = build_corpus_failure_message(failure, task_id)
            self._log({
                "event": "signature",
                "task": task_id,
                "signature_message": prog.signature_message,
                "local_failure_signature": trajectory.failure_signature,
            })
        recovered = trajectory_recovered(trajectory)

        if self.args.dry_run:
            would_mint = "green" if recovered else ("red" if failure is not None else None)
            # Simulate the counters so dry-run terminates on targets, not bounds.
            if would_mint == "green":
                prog.green_minted += 1
            elif would_mint == "red":
                prog.red_minted += 1
            self._log({
                "event": "dry_run",
                "task": task_id,
                "intent": intent,
                "would_mint": would_mint,
                "run_id": run_id,
            })
            return

        corpus = OmiumCorpusRun(config, green_allocator=self.allocator)
        execution_id = corpus.mint(task, run_id, trajectory)
        if execution_id is None:
            prog.mint_failures += 1
            self.consecutive_mint_failures += 1
            self._log({
                "event": "mint_failed",
                "task": task_id,
                "intent": intent,
                "recovered": recovered,
                "run_id": run_id,
                "green_keys_remaining": self.allocator.remaining,
            })
            return

        self.consecutive_mint_failures = 0
        self._api_base = self._api_base or corpus._base
        self._api_key = self._api_key or corpus._key
        polarity = corpus.polarity or "red"
        outcome: str | None = None
        if polarity == "green":
            prog.green_minted += 1
            self.unsettled_greens += 1
            outcome = self._poll_terminal(execution_id)
            if outcome == "completed":
                prog.green_completed += 1
            elif outcome == "poll_timeout":
                prog.green_poll_timeouts += 1
            if outcome in ("completed", "failed", "cancelled", "error"):
                # Terminal (either way) -> its fail-once key is no longer in flight.
                self.unsettled_greens -= 1
        else:
            prog.red_minted += 1
        self._log({
            "event": "minted",
            "task": task_id,
            "intent": intent,
            "polarity": polarity,
            "execution_id": execution_id,
            "run_id": run_id,
            "green_version": corpus.green_version,
            "original_outcome": outcome,
            "totals": prog.summary(),
        })

    def _poll_terminal(self, execution_id: str) -> str:
        """Poll the ORIGINAL execution until terminal (green pacing + key hygiene).

        'completed' == resume_parent after verified_success (the desirable row's
        happy path); 'failed' == the recovery terminalised the other way. This is a
        PACING signal — the authoritative count is the WORM/export the parent reads.
        """
        if not (self._api_base and self._api_key):
            return "poll_skipped"
        terminal = {"completed", "failed", "cancelled", "error"}
        deadline = time.time() + self.args.poll_timeout
        try:
            import httpx
        except Exception:  # noqa: BLE001
            return "poll_skipped"
        while time.time() < deadline:
            time.sleep(self.args.poll_interval)
            try:
                r = httpx.get(
                    f"{self._api_base}/executions/{execution_id}",
                    headers={"X-API-Key": self._api_key},
                    timeout=30,
                )
                status = str((r.json() or {}).get("status") or "").lower()
                if status in terminal:
                    return status
            except Exception:  # noqa: BLE001
                pass
        return "poll_timeout"

    # -- key clearing ----------------------------------------------------------
    def _maybe_clear_keys(self) -> bool:
        """Run --clear-keys-cmd when the pool is dry. True if a fresh cycle started.

        SAFETY: refuses while any green is unsettled (an in-flight recovery whose
        fail-once counter a clear would reset, flipping its heal back to a failure).
        """
        if not self.args.clear_keys_cmd:
            return False
        if self.clears_run >= self.args.max_clears:
            self._log({"event": "clear_skipped", "reason": f"max-clears ({self.args.max_clears})"})
            return False
        if self.unsettled_greens > 0:
            self._log({
                "event": "clear_skipped",
                "reason": f"{self.unsettled_greens} green(s) not terminal (poll timeouts?) — "
                          "clearing now could flip an in-flight heal to a failure",
            })
            return False
        self._log({"event": "clearing_keys", "cmd": self.args.clear_keys_cmd})
        proc = subprocess.run(
            self.args.clear_keys_cmd, shell=True, capture_output=True, text=True, timeout=600
        )
        if proc.returncode == 0:
            self.allocator.mark_cleared()
            self.clears_run += 1
            self._log({"event": "keys_cleared", "stdout": (proc.stdout or "")[-300:]})
            return True
        self._log({
            "event": "clear_failed",
            "rc": proc.returncode,
            "stderr": (proc.stderr or "")[-300:],
        })
        return False

    # -- main loop ---------------------------------------------------------------
    def run(self) -> int:
        self._log({
            "event": "start",
            "tasks": self.args.task_ids,
            "target_yield": self.args.target_yield,
            "target_desirable": self.args.target_desirable,
            "green_pool_size": self.allocator.pool_size,
            "green_keys_remaining": self.allocator.remaining,
            "max_runs": self.args.max_runs,
            "max_minutes": self.args.max_minutes,
            "dry_run": self.args.dry_run,
        })
        stop_reason = None
        while stop_reason is None:
            stop_reason = self._bounds_exceeded()
            if stop_reason:
                break
            pending = [p for p in self.progress.values() if not p.done]
            if not pending:
                stop_reason = "all per-signature targets met"
                break
            keys_available = self.allocator.remaining > 0
            if not keys_available and any(p.green_remaining > 0 for p in pending):
                if self._maybe_clear_keys():
                    keys_available = True
            made_progress = False
            for prog in pending:  # round-robin across the concentrated tasks
                if self._bounds_exceeded():
                    break
                intent = choose_polarity(prog, keys_available or self.args.dry_run)
                if intent is None:
                    continue
                self._run_task_once(prog.task_id, intent)
                made_progress = True
                keys_available = self.allocator.remaining > 0
                if self.args.settle > 0:
                    time.sleep(self.args.settle)
                if (
                    self.args.pause_every > 0
                    and self.total_runs > 0
                    and self.total_runs % self.args.pause_every == 0
                ):
                    self._log({
                        "event": "pause",
                        "after_runs": self.total_runs,
                        "seconds": self.args.pause_secs,
                    })
                    time.sleep(self.args.pause_secs)
            if not made_progress:
                # Every pending task is green-blocked with a dry pool and no usable
                # clear command — stop with instructions instead of spinning.
                stop_reason = (
                    "green pool exhausted and no --clear-keys-cmd (or clear refused); "
                    "clear the fail-once keys in the EE pod, then re-run (state file "
                    f"{self.args.pool_state} resumes the targets)"
                )
        summary = {
            "event": "done",
            "stop_reason": stop_reason,
            "total_runs": self.total_runs,
            "green_keys_remaining": self.allocator.remaining,
            "unsettled_greens": self.unsettled_greens,
            "tasks": [p.summary() for p in self.progress.values()],
        }
        self._log(summary)
        if self._manifest:
            self._manifest.close()
        all_done = all(p.done for p in self.progress.values())
        return 0 if all_done else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="codeswarm.corpus_runner", description=__doc__)
    ap.add_argument(
        "--tasks", required=True,
        help="Comma-separated builtin task ids to CONCENTRATE on (1-3 recommended).",
    )
    ap.add_argument(
        "--target-yield", type=int, default=210,
        help="Per-signature TOTAL rows to mint (floor: >=200; default 210 for margin).",
    )
    ap.add_argument(
        "--target-desirable", type=int, default=60,
        help="Per-signature GREEN rows (floor: >=50; default 60 for margin).",
    )
    ap.add_argument("--max-runs", type=int, default=800, help="Hard cap on total runs.")
    ap.add_argument("--max-minutes", type=float, default=300, help="Hard wall-clock cap.")
    ap.add_argument(
        "--stop-after-failures", type=int, default=8,
        help="Stop after this many CONSECUTIVE mint failures.",
    )
    ap.add_argument("--settle", type=float, default=2.0, help="Sleep between runs (s).")
    ap.add_argument(
        "--pause-every", type=int, default=25,
        help="Every N runs, pause to let the recovery loop drain (0 disables).",
    )
    ap.add_argument("--pause-secs", type=float, default=30.0, help="Length of that pause.")
    ap.add_argument(
        "--green-retries", type=int, default=3,
        help="Retry budget for green-intent runs (mock heals on attempt 2).",
    )
    ap.add_argument(
        "--green-pool-size", type=int, default=DEFAULT_GREEN_POOL_SIZE,
        help="Seeded green pool size (versions/keys). Keep in sync with the seed SQL.",
    )
    ap.add_argument(
        "--pool-state", default="runs/.cs_green_pool.json",
        help="GreenKeyAllocator state file (shared across resumed runs).",
    )
    ap.add_argument(
        "--clear-keys-cmd", default=None,
        help="Shell command that clears the fail-once keys in the EE pod (the kubectl "
             "pipe). Run automatically when the pool exhausts; safe only because "
             "greens are settled first.",
    )
    ap.add_argument(
        "--max-clears", type=int, default=10,
        help="Bound on automatic key-clear cycles.",
    )
    ap.add_argument("--poll-interval", type=float, default=5.0, help="Green status poll (s).")
    ap.add_argument(
        "--poll-timeout", type=float, default=300.0,
        help="Max wait for a green's original execution to terminalise (s).",
    )
    ap.add_argument("--runs-dir", default="runs", help="Where trajectories are written.")
    ap.add_argument(
        "--manifest", default=None,
        help="JSONL progress manifest (default runs/corpus_runner-<ts>.jsonl).",
    )
    ap.add_argument(
        "--real", action="store_true",
        help="Use the real LLM instead of the offline MockClient (costs money; the "
             "mock is the concentrated volume path).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Run the swarm + report would-be polarity WITHOUT posting to Omium.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
    unknown = [t for t in args.task_ids if t not in list_tasks()]
    if unknown:
        print(f"unknown task(s): {', '.join(unknown)}. available: {', '.join(list_tasks())}",
              file=sys.stderr)
        return 2
    if not args.task_ids:
        print("provide at least one task id via --tasks", file=sys.stderr)
        return 2
    if len(args.task_ids) > 3:
        print("WARNING: >3 tasks dilutes concentration; the floor is per-signature.",
              file=sys.stderr)
    if args.target_desirable > args.target_yield:
        print("--target-desirable cannot exceed --target-yield", file=sys.stderr)
        return 2
    reason = _refuse_prod(os.environ.get("OMIUM_API_URL"))
    if reason:
        print(reason, file=sys.stderr)
        return 2
    if not args.dry_run and not (
        os.environ.get("OMIUM_API_URL") and os.environ.get("OMIUM_API_KEY")
    ):
        print("OMIUM_API_URL + OMIUM_API_KEY are required (or use --dry-run).",
              file=sys.stderr)
        return 2
    if args.manifest is None:
        args.manifest = os.path.join(args.runs_dir, f"corpus_runner-{int(time.time())}.jsonl")
    return CorpusRunner(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
