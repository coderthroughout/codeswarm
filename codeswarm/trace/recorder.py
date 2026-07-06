"""TrajectoryRecorder — appends Events with a monotonic ts_index and builds the
final Trajectory. No wall clock anywhere.
"""
from __future__ import annotations

from typing import Callable

from codeswarm.trace.types import (
    Event,
    Trajectory,
    Verdict,
    failure_signature,
)


class TrajectoryRecorder:
    """Accumulates the ordered event log for a single run.

    ``ts_index`` is a monotonic counter, incremented per appended event, so the
    trajectory is deterministic and replayable regardless of timing.

    ``on_event`` is an optional live callback fired for each appended event — used
    by the web UI to stream the swarm's progress in real time. It must never raise
    into the recorder (errors are swallowed) so telemetry can't break a run.
    """

    def __init__(
        self,
        task_id: str,
        run_id: str,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.events: list[Event] = []
        self._ts_index = 0
        self._on_event = on_event

    def append_event(
        self,
        kind: str,
        step_id: str,
        agent: str | None,
        payload: dict | None = None,
    ) -> Event:
        """Record one event and return it (also stored in ``self.events``)."""
        event = Event(
            kind=kind,
            step_id=step_id,
            agent=agent,
            payload=dict(payload or {}),
            ts_index=self._ts_index,
        )
        self._ts_index += 1
        self.events.append(event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001 - live callback must not break a run
                pass
        return event

    def build(self, verdict: Verdict | None) -> Trajectory:
        """Materialize the Trajectory, computing the dominant failure signature."""
        return Trajectory(
            task_id=self.task_id,
            run_id=self.run_id,
            events=list(self.events),
            verdict=verdict,
            failure_signature=failure_signature(self.events),
        )
