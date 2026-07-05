"""Smoke test: run the FULL engine on a builtin task via MockClient, offline.

Asserts a Trajectory with a passing verdict is produced, and that the
failure/recovery path was exercised (the mock's first coder attempt fails on
purpose, so the corpus row carries real failure signal).
"""
from __future__ import annotations

import asyncio
import json

from codeswarm.agents.coder import CoderAgent
from codeswarm.agents.planner import PlannerAgent
from codeswarm.agents.reviewer import ReviewerAgent
from codeswarm.agents.tester import TesterAgent
from codeswarm.config import Config
from codeswarm.llm.client import MockClient
from codeswarm.tasks import get_task, list_tasks
from codeswarm.trace.types import Trajectory
from codeswarm.workflow.engine import WorkflowEngine
from codeswarm.workflow.executor import LocalExecutor


def _engine(config: Config, task) -> WorkflowEngine:
    executor = LocalExecutor(
        coder=CoderAgent(),
        tester=TesterAgent(),
        reviewer=ReviewerAgent(),
        max_retries=config.max_retries,
    )
    return WorkflowEngine(
        config=config,
        llm=MockClient(solutions=task.reference_solution),
        planner=PlannerAgent(),
        executor=executor,
    )


def test_engine_end_to_end_via_mock():
    config = Config()
    task = get_task("math_utils")
    engine = _engine(config, task)

    trajectory: Trajectory = asyncio.run(engine.run(task))

    # A verdict was produced, and it passed.
    assert trajectory.verdict is not None
    assert trajectory.verdict.passed is True
    assert trajectory.verdict.signals.get("tests_passed", 0) >= 2

    kinds = [e.kind for e in trajectory.events]
    # The full loop ran: plan -> checkpoint -> code(tool) -> test -> verdict.
    assert "agent" in kinds       # planner / reviewer
    assert "checkpoint" in kinds
    assert "tool" in kinds        # coder wrote files
    assert "test" in kinds
    assert "verdict" in kinds
    # The mock's first attempt fails on purpose -> failure + recovery recorded.
    assert "failure" in kinds
    assert "recovery" in kinds
    assert trajectory.failure_signature is not None

    # ts_index is monotonic and contiguous (no wall clock).
    indices = [e.ts_index for e in trajectory.events]
    assert indices == list(range(len(indices)))


def test_to_jsonl_is_valid_and_roundtrips():
    config = Config()
    task = get_task("strutils")
    engine = _engine(config, task)
    trajectory = asyncio.run(engine.run(task))

    lines = trajectory.to_jsonl().strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert records[0]["type"] == "meta"
    assert records[0]["task_id"] == "strutils"
    assert records[0]["verdict"]["passed"] is True
    assert all(r["type"] == "event" for r in records[1:])
    assert records[0]["event_count"] == len(records) - 1


def test_all_builtin_tasks_pass_via_mock():
    config = Config()
    for task_id in list_tasks():
        task = get_task(task_id)
        engine = _engine(config, task)
        trajectory = asyncio.run(engine.run(task))
        assert trajectory.verdict is not None, task_id
        assert trajectory.verdict.passed is True, task_id


def test_importable_without_anthropic_package():
    # Importing the client module must not require the anthropic SDK.
    import importlib

    mod = importlib.import_module("codeswarm.llm.client")
    assert hasattr(mod, "AnthropicClient")
    assert hasattr(mod, "MockClient")
