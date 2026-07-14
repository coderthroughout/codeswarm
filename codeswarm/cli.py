"""codeswarm CLI: ``python -m codeswarm`` / ``codeswarm``.

  run   --task <id> [--model ...] [--mock]   run one task, write runs/<run_id>.jsonl
  batch --tasks <glob|all> [--repeat N]      data-generation mode (many trajectories)
  tasks                                       list builtin tasks

--mock uses the offline MockClient (no network, no API key).
"""
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import sys
from pathlib import Path

from codeswarm.agents.coder import CoderAgent
from codeswarm.agents.planner import PlannerAgent
from codeswarm.agents.reviewer import ReviewerAgent
from codeswarm.agents.tester import TesterAgent
from codeswarm.config import Config
from codeswarm.llm.client import MockClient, build_real_client
from codeswarm.tasks import BUILTIN_TASKS, get_task, list_tasks
from codeswarm.tasks.spec import Task, TaskResult
from codeswarm.trace.types import Trajectory
from codeswarm.workflow.engine import WorkflowEngine
from codeswarm.workflow.executor import LocalExecutor


def _build_engine(config: Config, task: Task, use_mock: bool) -> WorkflowEngine:
    """Wire up the engine for a single task (mock or real LLM)."""
    if use_mock:
        # reference_solution is mock-only; the real client never sees it.
        llm = MockClient(solutions=task.reference_solution)
    else:
        llm = build_real_client(config)

    executor = LocalExecutor(
        coder=CoderAgent(),
        tester=TesterAgent(),
        reviewer=ReviewerAgent(),
        max_retries=config.max_retries,
    )
    return WorkflowEngine(
        config=config,
        llm=llm,
        planner=PlannerAgent(),
        executor=executor,
    )


def _write_trajectory(config: Config, trajectory: Trajectory) -> Path:
    runs_dir = Path(config.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    out = runs_dir / f"{trajectory.run_id}.jsonl"
    out.write_text(trajectory.to_jsonl(), encoding="utf-8")
    return out


async def _run_one(config: Config, task: Task, use_mock: bool) -> TaskResult:
    engine = _build_engine(config, task, use_mock)

    from codeswarm.workflow.omium_executor import corpus_mode_enabled, omium_enabled

    omium_run = None
    if omium_enabled(config):
        # Mode-1 (observability): correlate the local run and the Omium execution
        # under one run_id; per-step spans + checkpoints; finalize status.
        import uuid

        from codeswarm.workflow.omium_executor import OmiumExecutor, OmiumRun

        run_id = f"{task.id}-{uuid.uuid4().hex[:8]}"
        omium_run = OmiumRun(config).start(task, run_id)
        engine.executor = OmiumExecutor(engine.executor, omium_run)
        trajectory = await engine.run(task, run_id=run_id)
        omium_run.finish(trajectory.verdict)
        if omium_run.dashboard_url:
            print(f"  omium: {omium_run.dashboard_url}")
    elif corpus_mode_enabled(config):
        # Mode-2 (corpus): run locally as usual, then drive the execution-engine so a
        # FAILING task mints a real omium.execution.failed carrying codeswarm's
        # signature. The local run/trajectory is unchanged.
        import uuid

        from codeswarm.workflow.omium_executor import OmiumCorpusRun

        run_id = f"{task.id}-{uuid.uuid4().hex[:8]}"
        trajectory = await engine.run(task, run_id=run_id)
        corpus = OmiumCorpusRun(config)
        execution_id = corpus.mint(task, run_id, trajectory)
        if execution_id:
            print(
                f"  omium corpus: minted {corpus.polarity or 'red'} execution {execution_id}"
            )
    else:
        trajectory = await engine.run(task)

    return TaskResult(
        task_id=task.id,
        run_id=trajectory.run_id,
        verdict=trajectory.verdict,
        trajectory=trajectory,
    )


def _print_result(result: TaskResult, out_path: Path) -> None:
    v = result.verdict
    status = "PASS" if result.passed else "FAIL"
    signals = v.signals if v else {}
    print(
        f"[{status}] task={result.task_id} run={result.run_id} "
        f"signals={signals} sig={result.trajectory.failure_signature} "
        f"-> {out_path}"
    )


def _slug(text: str) -> str:
    """A short, filesystem-safe id derived from a free-form prompt."""
    import re

    words = re.findall(r"[a-z0-9]+", text.lower())[:5]
    return ("-".join(words) or "task")[:40]


def _resolve_omium_mode(args: argparse.Namespace) -> str | None:
    """--omium-mode wins; else legacy --omium maps to observability; else unset."""
    if args.omium_mode:
        return args.omium_mode
    if args.omium:
        return "observability"
    return None


def _cmd_run(args: argparse.Namespace) -> int:
    config = Config.from_env(
        model=args.model,
        runs_dir=args.runs_dir,
        max_retries=args.max_retries,
        llm_provider=args.provider,
        omium_mode=_resolve_omium_mode(args),
    )
    if args.prompt:
        if args.mock:
            print("--prompt (free-form) needs a real LLM; drop --mock.", file=sys.stderr)
            return 2
        task = Task.from_prompt(args.prompt, task_id=_slug(args.prompt))
    elif args.task:
        try:
            task = get_task(args.task)
        except KeyError:
            print(
                f"unknown task {args.task!r}. available: {', '.join(list_tasks())}",
                file=sys.stderr,
            )
            return 2
    else:
        print("provide --task <id> or --prompt \"<description>\".", file=sys.stderr)
        return 2

    result = asyncio.run(_run_one(config, task, use_mock=args.mock))
    out_path = _write_trajectory(config, result.trajectory)
    _print_result(result, out_path)
    return 0 if result.passed else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    from codeswarm.web.app import serve

    serve(host=args.host, port=args.port)
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    config = Config.from_env(
        model=args.model,
        runs_dir=args.runs_dir,
        max_retries=args.max_retries,
        llm_provider=args.provider,
        omium_mode=_resolve_omium_mode(args),
    )
    if args.tasks == "all":
        selected = list_tasks()
    else:
        selected = [t for t in list_tasks() if fnmatch.fnmatch(t, args.tasks)]
    if not selected:
        print(f"no builtin tasks match {args.tasks!r}", file=sys.stderr)
        return 2

    passed = 0
    total = 0
    for task_id in selected:
        task = BUILTIN_TASKS[task_id]
        for _ in range(args.repeat):
            total += 1
            result = asyncio.run(_run_one(config, task, use_mock=args.mock))
            out_path = _write_trajectory(config, result.trajectory)
            _print_result(result, out_path)
            passed += 1 if result.passed else 0
    print(f"batch complete: {passed}/{total} passed")
    return 0 if passed == total else 1


def _cmd_tasks(_args: argparse.Namespace) -> int:
    for task_id in list_tasks():
        task = BUILTIN_TASKS[task_id]
        print(f"{task_id}\t[{task.difficulty}]\t{task.prompt.splitlines()[0]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codeswarm", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=None, help="Model id (real runs only).")
    common.add_argument(
        "--provider",
        default=None,
        choices=["anthropic", "vertex", "openai_compatible"],
        help=(
            "Real LLM provider: anthropic (API key), vertex (Claude on GCP), or "
            "openai_compatible (Nebius Token Factory / any OpenAI-wire endpoint)."
        ),
    )
    common.add_argument("--mock", action="store_true", help="Use the offline MockClient.")
    common.add_argument("--runs-dir", default=None, help="Where to write trajectories.")
    common.add_argument("--max-retries", type=int, default=None, help="Attempts per step.")
    common.add_argument(
        "--omium",
        action="store_true",
        help="Mode-1: stream this run into Omium (execution + traces + checkpoints).",
    )
    common.add_argument(
        "--omium-mode",
        default=None,
        choices=["off", "observability", "corpus"],
        help=(
            "Omium integration mode (overrides --omium). "
            "'corpus' drives the execution-engine so a FAILING run mints real "
            "recovery-training corpus (omium.execution.failed)."
        ),
    )

    p_run = sub.add_parser("run", parents=[common], help="Run one task (builtin id or free-form prompt).")
    p_run.add_argument("--task", default=None, help="Builtin task id.")
    p_run.add_argument("--prompt", default=None, help="Free-form task in plain language (the real path).")
    p_run.set_defaults(func=_cmd_run)

    p_batch = sub.add_parser("batch", parents=[common], help="Run many tasks (data-gen).")
    p_batch.add_argument("--tasks", default="all", help="'all' or a glob over task ids.")
    p_batch.add_argument("--repeat", type=int, default=1, help="Runs per task.")
    p_batch.set_defaults(func=_cmd_batch)

    p_tasks = sub.add_parser("tasks", help="List builtin tasks.")
    p_tasks.set_defaults(func=_cmd_tasks)

    p_serve = sub.add_parser("serve", parents=[common], help="Launch the web UI.")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host.")
    p_serve.add_argument("--port", type=int, default=8787, help="Bind port.")
    p_serve.set_defaults(func=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
