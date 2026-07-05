"""Runtime configuration for codeswarm.

Everything here is stdlib-only. ``api_key`` is read from the environment so the
package imports and runs (via MockClient) with no key present.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Anthropic's most capable widely-available model at time of writing. Overridable
# via env or the CLI. Only used on the real AnthropicClient path — the MockClient
# path ignores it, so an unfamiliar-looking default never blocks offline runs.
DEFAULT_MODEL = "claude-opus-4-8"


@dataclass
class Config:
    """Injected into the engine/clients. All fields have safe offline defaults."""

    model: str = DEFAULT_MODEL
    api_key: str | None = None
    max_iterations: int = 10  # bound on the number of plan steps executed
    max_retries: int = 3      # attempts per step (code -> test -> review loop)
    runs_dir: str = "runs"    # where CLI writes <run_id>.jsonl trajectories

    @classmethod
    def from_env(cls, **overrides: object) -> "Config":
        """Build a Config from environment variables, applying explicit overrides.

        Recognised env vars:
          - ANTHROPIC_API_KEY / CODESWARM_API_KEY
          - CODESWARM_MODEL
          - CODESWARM_MAX_ITERATIONS
          - CODESWARM_MAX_RETRIES
          - CODESWARM_RUNS_DIR
        """
        api_key = os.environ.get("CODESWARM_API_KEY") or os.environ.get(
            "ANTHROPIC_API_KEY"
        )
        cfg = cls(
            model=os.environ.get("CODESWARM_MODEL", DEFAULT_MODEL),
            api_key=api_key,
            max_iterations=int(os.environ.get("CODESWARM_MAX_ITERATIONS", "10")),
            max_retries=int(os.environ.get("CODESWARM_MAX_RETRIES", "3")),
            runs_dir=os.environ.get("CODESWARM_RUNS_DIR", "runs"),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg
