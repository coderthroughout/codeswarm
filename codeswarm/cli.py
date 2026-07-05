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
from codeswarm.llm.client import AnthropicClient, MockClient
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
        llm = AnthropicClient(config)

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


def _cmd_run(args: argparse.Namespace) -> int:
    config = Config.from_env(
        model=args.model,
        runs_dir=args.runs_dir,
        max_retries=args.max_retries,
    )
    try:
        task = get_task(args.task)
    except KeyError:
        print(
            f"unknown task {args.task!r}. available: {', '.join(list_tasks())}",
            file=sys.stderr,
        )
        return 2

    result = asyncio.run(_run_one(config, task, use_mock=args.mock))
    out_path = _write_trajectory(config, result.trajectory)
    _print_result(result, out_path)
    return 0 if result.passed else 1


def _cmd_batch(args: argparse.Namespace) -> int:
    config = Config.from_env(
        model=args.model,
        runs_dir=args.runs_dir,
        max_retries=args.max_retries,
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
    common.add_argument("--mock", action="store_true", help="Use the offline MockClient.")
    common.add_argument("--runs-dir", default=None, help="Where to write trajectories.")
    common.add_argument("--max-retries", type=int, default=None, help="Attempts per step.")

    p_run = sub.add_parser("run", parents=[common], help="Run one task.")
    p_run.add_argument("--task", required=True, help="Builtin task id.")
    p_run.set_defaults(func=_cmd_run)

    p_batch = sub.add_parser("batch", parents=[common], help="Run many tasks (data-gen).")
    p_batch.add_argument("--tasks", default="all", help="'all' or a glob over task ids.")
    p_batch.add_argument("--repeat", type=int, default=1, help="Runs per task.")
    p_batch.set_defaults(func=_cmd_batch)

    p_tasks = sub.add_parser("tasks", help="List builtin tasks.")
    p_tasks.set_defaults(func=_cmd_tasks)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
