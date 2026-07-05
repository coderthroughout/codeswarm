"""WorkflowState + Step.

The state mirrors the plan and the current file contents, and keeps checkpoints
(snapshots taken before each step) so a step can be retried/recovered from a
known-good point — the exact shape Omium's recovery operates on.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Step:
    """One concrete, ordered unit of work produced by the planner."""

    id: str
    description: str


@dataclass
class Checkpoint:
    """A snapshot of state taken before executing a step."""

    step_id: str
    files: dict[str, str]
    plan_index: int


@dataclass
class WorkflowState:
    """Mutable run state, threaded through the engine and executor."""

    task_id: str
    task: Any = None                      # the Task being solved
    plan: list[Step] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)   # mirror of workspace files
    history: list[Any] = field(default_factory=list)      # StepResults, in order
    checkpoints: list[Checkpoint] = field(default_factory=list)
    plan_index: int = 0

    def snapshot(self, step_id: str) -> Checkpoint:
        """Capture a checkpoint of the current files + plan position."""
        cp = Checkpoint(
            step_id=step_id,
            files=copy.deepcopy(self.files),
            plan_index=self.plan_index,
        )
        self.checkpoints.append(cp)
        return cp

    def restore(self, checkpoint: Checkpoint, workspace=None) -> None:
        """Restore files + plan position from a checkpoint.

        If ``workspace`` is provided, the checkpointed files are written back to
        disk so retries resume from the known-good point.
        """
        self.files = copy.deepcopy(checkpoint.files)
        self.plan_index = checkpoint.plan_index
        if workspace is not None:
            for relpath, content in self.files.items():
                workspace.write_file(relpath, content)
