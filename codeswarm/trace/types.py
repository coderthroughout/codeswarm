"""Core types — THE contract (see DESIGN.md "Core types").

A Trajectory is the corpus row: it maps cleanly onto a recovery-attempt/verdict
shape later. There is deliberately NO wall-clock time in core — ordering is a
monotonic ``ts_index`` so runs are deterministic and replayable.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """One invocation of a Tool by an agent."""

    tool: str
    args: dict
    ok: bool
    output: str
    error: str | None
    ms: int  # nominal cost units; NOT wall clock (kept 0 in core for determinism)


@dataclass
class Event:
    """One recorded thing that happened during a run."""

    # "agent" | "tool" | "test" | "checkpoint" | "failure" | "recovery" | "verdict"
    kind: str
    step_id: str
    agent: str | None
    payload: dict
    ts_index: int  # monotonic index (NO wall clock in core)


@dataclass
class StepResult:
    """Outcome of executing a single plan step (possibly over several attempts)."""

    step_id: str
    ok: bool
    attempts: int
    error: str | None
    events: list[Event] = field(default_factory=list)


@dataclass
class Verdict:
    """The final, verified outcome of a run."""

    passed: bool
    signals: dict = field(default_factory=dict)  # e.g. {"tests_passed": n, "tests_failed": m}


@dataclass
class Trajectory:
    """THE corpus row — the dense-per-failure-signature raw material."""

    task_id: str
    run_id: str
    events: list[Event]
    verdict: Verdict | None
    failure_signature: str | None  # stable hash of the dominant failure

    def to_jsonl(self) -> str:
        """Serialize as JSONL: a meta line, then one line per event.

        The result is valid JSONL (one JSON object per line) and fully
        round-trippable without any wall-clock or environment dependence.
        """
        lines: list[str] = []
        meta: dict[str, Any] = {
            "type": "meta",
            "task_id": self.task_id,
            "run_id": self.run_id,
            "verdict": asdict(self.verdict) if self.verdict is not None else None,
            "failure_signature": self.failure_signature,
            "event_count": len(self.events),
        }
        lines.append(json.dumps(meta, sort_keys=True))
        for ev in self.events:
            record = {"type": "event", **asdict(ev)}
            lines.append(json.dumps(record, sort_keys=True))
        return "\n".join(lines) + "\n"


def failure_signature(events: list[Event]) -> str | None:
    """Return a stable short hash of the dominant failure, or None if clean.

    The dominant failure is the first recorded ``failure`` event. The signature
    combines the failing step id with a normalized token. Preference order:
    ``signature_token`` (exception kinds × failing test files — stable AND
    discriminating), then ``error_type``, then the free-text summary. Using the
    token first is what stops every failure from collapsing to one hash.
    """
    for ev in events:
        if ev.kind == "failure":
            token = (
                ev.payload.get("signature_token")
                or ev.payload.get("error_type")
                or ev.payload.get("error")
                or ev.payload.get("summary")
                or "unknown"
            )
            basis = f"{ev.step_id}:{token}".strip()
            return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    return None
