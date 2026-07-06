"""WorkflowEngine — orchestrates plan -> loop(code -> test -> review) -> verify.

Injected with an Executor, an LLM client, a planner agent, and a tools factory.
It owns the sandbox lifecycle, checkpoints state before each step, and records
the full Trajectory. It never imports Omium and never changes when the Executor
implementation is swapped (the seam).
"""
from __future__ import annotations

import uuid
from typing import Callable

from codeswarm.agents.base import AgentContext
from codeswarm.config import Config
from codeswarm.llm.client import LLMClient
from codeswarm.sandbox.workspace import Workspace
from codeswarm.tools import default_tools
from codeswarm.trace.recorder import TrajectoryRecorder
from codeswarm.trace.types import Trajectory, Verdict
from codeswarm.workflow.executor import Executor
from codeswarm.workflow.state import WorkflowState


class WorkflowEngine:
    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        planner,
        executor: Executor,
        tools_factory: Callable[[Workspace], dict] | None = None,
        spec_agent=None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.planner = planner
        self.executor = executor
        self.tools_factory = tools_factory or default_tools
        # Writes the pytest oracle for FREE-FORM tasks (those with no test_files).
        if spec_agent is None:
            from codeswarm.agents.spec import SpecAgent

            spec_agent = SpecAgent()
        self.spec_agent = spec_agent

    async def run(self, task, *, run_id: str | None = None, on_event=None) -> Trajectory:
        """Run one task end-to-end and return its Trajectory.

        ``on_event`` is an optional live callback (event -> None) fired as each
        event is recorded — used by the web UI to stream progress.
        """
        run_id = run_id or f"{task.id}-{uuid.uuid4().hex[:8]}"
        recorder = TrajectoryRecorder(task_id=task.id, run_id=run_id, on_event=on_event)
        state = WorkflowState(task_id=task.id, task=task)

        with Workspace() as workspace:
            # Seed the sandbox with starter + test files.
            workspace.write_files(task.files)
            workspace.write_files(task.test_files)
            state.files = dict(task.files)

            tools = self.tools_factory(workspace)
            ctx = AgentContext(
                state=state,
                llm=self.llm,
                tools=tools,
                workspace=workspace,
                recorder=recorder,
                task=task,
            )

            # 0) Free-form task with no oracle: the spec agent writes the tests.
            if not getattr(task, "test_files", None):
                await self.spec_agent.run(ctx)

            # 1) Plan.
            plan_action = await self.planner.run(ctx)
            plan = plan_action.data.get("plan", [])
            state.plan = plan

            # 2) Execute steps (bounded by max_iterations), checkpointing each.
            for index, step in enumerate(plan[: self.config.max_iterations]):
                state.plan_index = index
                cp = state.snapshot(step.id)
                recorder.append_event(
                    kind="checkpoint",
                    step_id=step.id,
                    agent=None,
                    payload={"plan_index": index, "files": sorted(cp.files)},
                )
                # Reset per-step working context.
                ctx.hint = None
                ctx.last_failure = None
                result = await self.executor.run_step(step, ctx)
                if not result.ok:
                    # Step exhausted its retry budget; stop executing further steps
                    # but still run the final oracle to record an honest verdict.
                    break

            # 3) Verify with the task's oracle (source of truth for the verdict).
            verdict: Verdict = task.verify(workspace.root)
            recorder.append_event(
                kind="verdict",
                step_id="verify",
                agent=None,
                payload={"passed": verdict.passed, "signals": verdict.signals},
            )

            return recorder.build(verdict)
