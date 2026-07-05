"""The Omium seam (see DESIGN.md "The Omium seam").

``Executor`` is a Protocol with a single method, ``run_step``. ``LocalExecutor``
is the ONLY executor in v1: it runs the step's agents in-process and, on failure,
retries up to ``max_retries`` with the reviewer's hint, recording failure +
recovery events. It has NO Omium dependency.

Later, an ``OmiumExecutor(Executor)`` in a SEPARATE, connection-time module will
wrap ``run_step`` to submit the step to Omium's execution-engine + recovery loop
via the SDK — so failures are recovered by Omium instead of the naive local
retry. The ``WorkflowEngine`` takes an ``Executor`` by injection and never
changes. Nothing else in this codebase imports or knows about Omium.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from codeswarm.agents.base import AgentContext
from codeswarm.trace.types import StepResult
from codeswarm.workflow.state import Step

# StepContext is the context passed to run_step. In v1 it is exactly the
# AgentContext the agents share; aliased for the DESIGN.md signature.
StepContext = AgentContext


@runtime_checkable
class Executor(Protocol):
    """The single, clean connection point for Omium (added later, elsewhere)."""

    async def run_step(self, step: Step, ctx: StepContext) -> StepResult:
        ...


class LocalExecutor:
    """v1 executor: in-process code -> test -> review retry loop.

    Injected with the coder/tester/reviewer agents. On each attempt it runs the
    coder, then the tester; if tests fail it records a failure event, runs the
    reviewer for a hint, records a recovery event, and retries up to
    ``max_retries`` times.
    """

    def __init__(self, coder, tester, reviewer, max_retries: int = 3) -> None:
        self.coder = coder
        self.tester = tester
        self.reviewer = reviewer
        self.max_retries = max(1, int(max_retries))

    async def run_step(self, step: Step, ctx: StepContext) -> StepResult:
        recorder = ctx.recorder
        start = len(recorder.events)
        ctx.step = step

        ok = False
        error: str | None = None
        attempts = 0

        for attempt in range(1, self.max_retries + 1):
            attempts = attempt
            ctx.attempt = attempt

            # 1) Coder proposes an edit.
            await self.coder.run(ctx)

            # 2) Tester runs the suite and interprets it.
            test_action = await self.tester.run(ctx)
            if test_action.data.get("passed_all"):
                ok = True
                error = None
                break

            # 3) Failure is first-class: record it.
            summary = test_action.data.get("summary", "tests failed")
            error = summary
            ctx.last_failure = summary
            recorder.append_event(
                kind="failure",
                step_id=step.id,
                agent="tester",
                payload={
                    "attempt": attempt,
                    "summary": summary,
                    # Real, task-specific failure detail (see tools/testing.py) —
                    # drives a meaningful, discriminating failure_signature.
                    "error_type": test_action.data.get("error_type") or "test_failure",
                    "signature_token": test_action.data.get("signature_token"),
                    "failing_tests": test_action.data.get("failing_tests", []),
                    "error_kinds": test_action.data.get("error_kinds", []),
                    "failed": test_action.data.get("failed", 0),
                    "errors": test_action.data.get("errors", 0),
                },
            )

            # 4) Attempt local recovery with a reviewer hint (if budget remains).
            if attempt < self.max_retries:
                review_action = await self.reviewer.run(ctx)
                ctx.hint = review_action.data.get("hint")
                recorder.append_event(
                    kind="recovery",
                    step_id=step.id,
                    agent="reviewer",
                    payload={"attempt": attempt, "hint": ctx.hint},
                )

        events = recorder.events[start:]
        result = StepResult(
            step_id=step.id, ok=ok, attempts=attempts, error=error, events=events
        )
        ctx.state.history.append(result)
        return result
