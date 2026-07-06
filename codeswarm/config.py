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
# Claude on Vertex AI (GCP). Model ids + regions differ from the direct API.
DEFAULT_VERTEX_MODEL = "claude-sonnet-4-5"
DEFAULT_VERTEX_REGION = "us-east5"


@dataclass
class Config:
    """Injected into the engine/clients. All fields have safe offline defaults."""

    model: str = DEFAULT_MODEL
    api_key: str | None = None

    # LLM provider for the real (non-mock) path: "anthropic" (direct API key) or
    # "vertex" (Claude on GCP Vertex AI, auth via Application Default Credentials).
    llm_provider: str = "anthropic"
    vertex_project: str | None = None
    vertex_region: str = DEFAULT_VERTEX_REGION
    max_iterations: int = 10  # bound on the number of plan steps executed
    max_retries: int = 3      # attempts per step (code -> test -> review loop)
    runs_dir: str = "runs"    # where CLI writes <run_id>.jsonl trajectories

    # Optional Omium integration (observability + recovery substrate). Off by
    # default; the standalone run never touches Omium. See workflow/omium_executor.py.
    omium_enabled: bool = False
    omium_workflow_id: str | None = None

    @classmethod
    def from_env(cls, **overrides: object) -> "Config":
        """Build a Config from environment variables, applying explicit overrides.

        Recognised env vars:
          - ANTHROPIC_API_KEY / CODESWARM_API_KEY
          - CODESWARM_MODEL
          - CODESWARM_MAX_ITERATIONS
          - CODESWARM_MAX_RETRIES
          - CODESWARM_RUNS_DIR
          - CODESWARM_OMIUM (enable) / CODESWARM_OMIUM_WORKFLOW_ID
        """
        api_key = os.environ.get("CODESWARM_API_KEY") or os.environ.get(
            "ANTHROPIC_API_KEY"
        )
        provider = os.environ.get("CODESWARM_LLM_PROVIDER", "anthropic").lower()
        # Model default depends on the provider (Vertex uses different ids).
        model_env = os.environ.get("CODESWARM_MODEL")
        if model_env:
            model = model_env
        elif provider == "vertex":
            model = DEFAULT_VERTEX_MODEL
        else:
            model = DEFAULT_MODEL
        cfg = cls(
            model=model,
            api_key=api_key,
            llm_provider=provider,
            vertex_project=os.environ.get("CODESWARM_VERTEX_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT"),
            vertex_region=os.environ.get("CODESWARM_VERTEX_REGION", DEFAULT_VERTEX_REGION),
            max_iterations=int(os.environ.get("CODESWARM_MAX_ITERATIONS", "10")),
            max_retries=int(os.environ.get("CODESWARM_MAX_RETRIES", "3")),
            runs_dir=os.environ.get("CODESWARM_RUNS_DIR", "runs"),
            omium_enabled=os.environ.get("CODESWARM_OMIUM", "").lower()
            in ("1", "true", "yes"),
            omium_workflow_id=os.environ.get("CODESWARM_OMIUM_WORKFLOW_ID"),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        # Safety net: if the provider ended up "vertex" (e.g. via a CLI override)
        # but no explicit model was given, use the Vertex model id (the direct-API
        # default id does not exist on Vertex).
        if cfg.llm_provider == "vertex" and not model_env and cfg.model == DEFAULT_MODEL:
            cfg.model = DEFAULT_VERTEX_MODEL
        return cfg
