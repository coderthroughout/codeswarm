# codeswarm

A **real, standalone multi-agent software-engineering swarm**. Given a coding task
with a verifiable success criterion (a pytest oracle), a small team of LLM agents
plans, edits code, runs tests, and iterates until the task passes or a budget is
exhausted — all inside an isolated sandbox. Every run emits a structured
**Trajectory** (steps, tool calls, outcomes, failures, the final verified verdict):
the dense-per-failure-signature raw material a recovery/training plane feeds on.

See [`DESIGN.md`](DESIGN.md) for the authoritative architecture and interface
contract.

## Hard principle: independent now, connectable later

- **v1 has ZERO Omium dependency.** It runs entirely on its own with a
  `LocalExecutor` (in-process step execution + naive local retry).
- **The ONLY seam** where Omium plugs in later is
  `codeswarm/workflow/executor.py::Executor`. A future `OmiumExecutor` (added in a
  separate module at connection time) routes step execution + failure recovery
  through the Omium SDK. Nothing else imports or knows about Omium.

## Install

```bash
cd ~/workspace/Omium/codeswarm
python -m pip install -e .            # core is stdlib-only
# optional extras:
python -m pip install -e '.[test]'      # pytest, for the test suite
python -m pip install -e '.[anthropic]' # the real Claude SDK (real runs)
```

## Run offline (no network, no API key)

```bash
python -m codeswarm tasks                       # list builtin tasks
python -m codeswarm run --task math_utils --mock
python -m codeswarm batch --tasks all --mock --repeat 2
```

`--mock` uses the deterministic `MockClient`, which drives a real
plan → code → test → review → verify loop to a passing verdict — including a
deliberate first-attempt failure so the trajectory carries failure/recovery signal.
Trajectories are written to `runs/<run_id>.jsonl`.

## Run for real (Claude)

Provide credentials (`ANTHROPIC_API_KEY`, or an `ant auth login` profile) and drop
`--mock`:

```bash
python -m codeswarm run --task math_utils --model claude-opus-4-8
```

The default model is `claude-opus-4-8` (override with `--model` or `CODESWARM_MODEL`).

## Run for real (Nebius Token Factory / any OpenAI-compatible endpoint)

Provider `openai_compatible` speaks the OpenAI `/chat/completions` wire format
over stdlib (no extra install). Default endpoint is **Nebius Token Factory**
(`https://api.tokenfactory.nebius.com/v1/`), default model **`MiniMaxAI/MiniMax-M3`**.

codeswarm's secret convention is **environment variables, never files**. The
Token Factory key lives in AWS Secrets Manager (`omium/staging/nebius-token-factory`,
JSON `{"api_key": ...}`); fetch it into the env for local runs:

```bash
export NEBIUS_API_KEY="$(aws secretsmanager get-secret-value \
  --profile omium-stg --secret-id omium/staging/nebius-token-factory \
  --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"

python -m codeswarm run --task math_utils --provider openai_compatible
# or via env instead of the flag:
CODESWARM_LLM_PROVIDER=openai_compatible python -m codeswarm run --task math_utils
```

Knobs (all env-overridable): `CODESWARM_OPENAI_API_KEY` (wins over
`NEBIUS_API_KEY`), `CODESWARM_OPENAI_BASE_URL`, `CODESWARM_MODEL` /
`--model`, `CODESWARM_OPENAI_MAX_TOKENS` (default **8192** — reasoning models
truncate below that; don't lower it).

Gotcha (live-verified): Token Factory answers a **wrong model id with HTTP 200
and empty `choices`** — the client raises on zero choices instead of returning
silent empty text.

## Test

```bash
python -m pytest -q     # green with NO API key (MockClient path)
```

## Layout

```
codeswarm/
  pyproject.toml  README.md  DESIGN.md
  codeswarm/ __init__.py __main__.py cli.py config.py
    llm/     client.py            # LLMClient Protocol, MockClient, AnthropicClient, OpenAICompatibleClient
    trace/   types.py recorder.py # Event/StepResult/Verdict/Trajectory + recorder
    tools/   base.py fs.py shell.py testing.py
    sandbox/ workspace.py         # ephemeral, path-confined temp dir per run
    agents/  base.py planner.py coder.py tester.py reviewer.py
    workflow/ executor.py engine.py state.py   # executor.py = the Omium seam
    tasks/   spec.py builtin/     # real self-contained pytest tasks
  tests/ test_smoke.py            # full engine on a builtin task via MockClient
```

## The trajectory (corpus row)

Each run yields a `Trajectory{task_id, run_id, events[], verdict, failure_signature}`
serialized as JSONL: a `meta` line followed by one line per `Event`
(`kind ∈ agent|tool|test|checkpoint|failure|recovery|verdict`). Ordering is a
monotonic `ts_index` — **no wall clock in core**, so runs are deterministic and
replayable.
