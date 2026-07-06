"""Task + TaskResult (see DESIGN.md "Tasks").

A Task bundles a prompt, starter files, pytest oracle files, and a ``verify()``
that runs the oracle and returns a Verdict. ``reference_solution`` is MOCK-ONLY:
it is used to seed the offline MockClient so the deterministic loop can reach a
passing verdict. The real AnthropicClient never receives it.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from codeswarm.trace.types import Trajectory, Verdict

_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped)")


@dataclass
class Task:
    id: str
    prompt: str
    files: dict[str, str] = field(default_factory=dict)        # starter solution files
    test_files: dict[str, str] = field(default_factory=dict)   # pytest oracle files
    reference_solution: dict[str, str] = field(default_factory=dict)  # MOCK-ONLY
    difficulty: str = "easy"

    @classmethod
    def from_prompt(cls, prompt: str, task_id: str = "task") -> "Task":
        """Build a FREE-FORM task from a plain-language description.

        No starter, no oracle, no reference solution — the spec agent writes the
        acceptance tests (test_solution.py) at run time, and ``verify()`` runs
        whatever tests then exist in the sandbox. This is the "real task from a
        user" path, distinct from the canned builtin tasks.
        """
        return cls(id=task_id, prompt=prompt, difficulty="freeform")

    def verify(self, root: str | Path) -> Verdict:
        """Run the pytest oracle in ``root`` and return a Verdict.

        This is the authoritative success criterion for the run — independent of
        anything the agents claimed.
        """
        root = Path(root)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except Exception as exc:  # noqa: BLE001
            return Verdict(passed=False, signals={"error": f"{type(exc).__name__}: {exc}"})

        output = (proc.stdout or "") + (proc.stderr or "")
        counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        for num, word in _COUNT_RE.findall(output):
            key = "errors" if word in ("error", "errors") else word
            counts[key] = counts.get(key, 0) + int(num)
        passed = proc.returncode == 0
        return Verdict(
            passed=passed,
            signals={
                "tests_passed": counts["passed"],
                "tests_failed": counts["failed"],
                "tests_errored": counts["errors"],
                "returncode": proc.returncode,
            },
        )


@dataclass
class TaskResult:
    """The outcome of running a task: its verdict + full trajectory."""

    task_id: str
    run_id: str
    verdict: Verdict | None
    trajectory: Trajectory

    @property
    def passed(self) -> bool:
        return bool(self.verdict and self.verdict.passed)
