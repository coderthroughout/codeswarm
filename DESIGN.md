# codeswarm — a multi-agent software-engineering swarm

`codeswarm` is a **real, standalone multi-agent coding system**. Given a coding task with a
verifiable success criterion (a test oracle), a small team of LLM agents plans, edits code,
runs tests, and iterates until the task passes or a budget is exhausted — all inside an
isolated sandbox.

It exists for three jobs at once:
1. **A genuine product** — a working agentic coder that solves real tasks.
2. **A data engine** — every run emits a structured **trajectory** (steps, tool calls,
   outcomes, failures, the final verified verdict). This is exactly the raw material a
   recovery/training plane feeds on: real, verifiable, dense-per-failure-signature.
3. **An integration test** — it is built **independent of Omium**. Later we connect it to
   Omium *at one seam* (the `Executor`), which exercises Omium's SDK/CLI/recovery end-to-end.

## Hard principle: independent now, connectable later

- **v1 has ZERO Omium dependency.** It runs perfectly on its own with a `LocalExecutor`
  (in-process step execution + naive local retry).
- **The ONLY seam** where Omium will later plug in is `workflow/executor.py::Executor`. A
  future `OmiumExecutor` (added at connection time, in a separate module) will route step
  execution + failure recovery through the Omium SDK. Nothing else in the codebase imports or
  knows about Omium. Keep it that way.

## The multi-agent workflow (the failure surface)

A task runs as a checkpointed loop over four LLM agent roles:

```
Planner  → produce an ordered plan of concrete steps for the task
  loop over steps (bounded by max_iterations):
    Coder   → propose a code edit (via tools: read/write/patch files)
    Tester  → run the task's tests; interpret pass/fail + failure output
    if tests fail:
      Reviewer → diagnose the failure + propose a correction hint
      (feed the hint back to Coder; retry the step)
  Verify → run the task's oracle one final time → Verdict {passed, signals}
```

Every agent call, tool call, and test run is an **Event** appended to the run's
**Trajectory**. Steps are **checkpointed** (state snapshot before each step) so a step can be
retried/recovered from a known-good point — this is the exact shape Omium's recovery operates
on. Failures are first-class: a tool error, a test failure, or an agent producing an invalid
action are all recorded as failure events, and the workflow attempts local recovery (retry
with the reviewer's hint) up to a budget.

## Core types (the contract — `trace/types.py`)

```python
@dataclass
class ToolCall:      tool: str; args: dict; ok: bool; output: str; error: str | None; ms: int
@dataclass
class Event:         # one recorded thing that happened
    kind: str        # "agent" | "tool" | "test" | "checkpoint" | "failure" | "recovery" | "verdict"
    step_id: str; agent: str | None; payload: dict; ts_index: int   # monotonic index (NO wall clock in core)
@dataclass
class StepResult:    step_id: str; ok: bool; attempts: int; error: str | None; events: list[Event]
@dataclass
class Verdict:       passed: bool; signals: dict            # e.g. {"tests_passed": n, "tests_failed": m}
@dataclass
class Trajectory:    # THE corpus row — maps cleanly onto a recovery-attempt/verdict shape later
    task_id: str; run_id: str; events: list[Event]; verdict: Verdict | None
    failure_signature: str | None   # a stable hash of the dominant failure (error type + failing step)
    def to_jsonl(self) -> str: ...
```

`ts_index` is a **monotonic counter**, not wall-clock (keeps runs deterministic + replayable).

## The Omium seam (the contract — `workflow/executor.py`)

```python
class Executor(Protocol):
    async def run_step(self, step: Step, ctx: StepContext) -> StepResult: ...

class LocalExecutor:            # v1 — the ONLY executor now
    # runs the step's agent+tools in-process; on failure, retries up to `max_retries`
    # with the reviewer hint; records failure + recovery events. No Omium.
```

Later: `OmiumExecutor(Executor)` (separate module, connection-time) wraps `run_step` to submit
the step to Omium's execution-engine + recovery loop via the SDK, so failures are recovered by
Omium instead of the naive local retry. The `WorkflowEngine` takes an `Executor` by injection
and never changes.

## LLM client (`llm/client.py`)

```python
class LLMClient(Protocol):
    async def complete(self, system: str, messages: list[dict], *, tools: list[dict] | None) -> LLMResponse: ...

class AnthropicClient(LLMClient):  # real Claude (anthropic SDK); model from config
class MockClient(LLMClient):       # deterministic canned responses — runs the whole system with NO api key
```

`MockClient` is first-class: the entire system (and its tests) run end-to-end offline via the
mock, so `pytest` is green without network. Real runs use `AnthropicClient`.

## Tools (`tools/`) — the agent's hands
`read_file`, `write_file`, `apply_patch`, `list_dir` (fs.py); `run` (shell.py, sandboxed);
`run_tests` (testing.py — pytest in the sandbox → structured pass/fail). Each is a `Tool` with
`name`, JSON-schema `spec`, and `call(args) -> ToolResult`. All file/shell ops are confined to
the sandbox workspace root (no escaping).

## Sandbox (`sandbox/workspace.py`)
An ephemeral temp dir per run: the task's starter files are copied in, all edits + tests run
there, and it's torn down after (results captured). Nothing touches the repo or the host.

## Tasks (`tasks/`)
A `Task` = `{id, prompt, files: dict[path,str] (starter), test_files: dict, verify() -> Verdict}`.
`tasks/builtin/` ships a handful of REAL self-contained Python tasks with pytest oracles
(varying difficulty, designed to fail in diverse ways so the corpus has signal). A
SWE-bench-style loader is a later add.

## CLI (`cli.py`, `python -m codeswarm`)
- `run --task <id> [--model ...] [--mock]` — run one task, print the result, write the
  trajectory to `runs/<run_id>.jsonl`.
- `batch --tasks <glob|all> [--repeat N]` — run many (this is the DATA-GENERATION mode: volume
  of trajectories → the corpus).
- `--mock` uses `MockClient` (offline).

## Layout
```
codeswarm/
  pyproject.toml  README.md  DESIGN.md
  codeswarm/ __init__.py __main__.py cli.py config.py
    llm/ client.py
    trace/ types.py recorder.py
    tools/ base.py fs.py shell.py testing.py
    sandbox/ workspace.py
    agents/ base.py planner.py coder.py tester.py reviewer.py
    workflow/ executor.py engine.py state.py
    tasks/ spec.py builtin/ (real tasks)
  tests/ (framework tests — run green via MockClient, no network)
```

## Quality bar
- `pip install -e .` then `pytest` is **green with no API key** (MockClient path).
- `python -m codeswarm run --task <id> --mock` completes a full plan→code→test→review→verify
  loop and writes a valid trajectory JSONL.
- With a real key, `AnthropicClient` actually solves easy tasks.
- Zero Omium imports anywhere. The `Executor` seam is the single, clean connection point.
