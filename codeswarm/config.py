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
# OpenAI-compatible provider (Nebius Token Factory by default). Token Factory
# model ids are HuggingFace-style "org/name" strings; MiniMax-M3 is the agreed
# swarm model (exact id live-verified in the account catalog 2026-07-13).
# Override with CODESWARM_MODEL / --model. CAUTION: Token Factory answers a
# WRONG model id with HTTP 200 + EMPTY choices — the client treats zero
# choices as a hard error instead of returning silent empty text.
DEFAULT_OPENAI_COMPAT_MODEL = "MiniMaxAI/MiniMax-M3"
DEFAULT_OPENAI_COMPAT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
# Reasoning models spend output budget on thinking; below ~8k they truncate
# mid-answer (known production scar). Keep >= 8192 unless you know better.
DEFAULT_OPENAI_COMPAT_MAX_TOKENS = 8192


@dataclass
class Config:
    """Injected into the engine/clients. All fields have safe offline defaults."""

    model: str = DEFAULT_MODEL
    api_key: str | None = None

    # LLM provider for the real (non-mock) path:
    #   "anthropic"         — direct Anthropic API key (default).
    #   "vertex"            — Claude on GCP Vertex AI (Application Default Credentials).
    #   "openai_compatible" — any OpenAI-compatible /chat/completions endpoint
    #                         (default: Nebius Token Factory). Bearer key from
    #                         CODESWARM_OPENAI_API_KEY or NEBIUS_API_KEY.
    llm_provider: str = "anthropic"
    vertex_project: str | None = None
    vertex_region: str = DEFAULT_VERTEX_REGION
    openai_base_url: str = DEFAULT_OPENAI_COMPAT_BASE_URL
    openai_api_key: str | None = None
    openai_max_tokens: int = DEFAULT_OPENAI_COMPAT_MAX_TOKENS
    max_iterations: int = 10  # bound on the number of plan steps executed
    max_retries: int = 3      # attempts per step (code -> test -> review loop)
    runs_dir: str = "runs"    # where CLI writes <run_id>.jsonl trajectories

    # Optional Omium integration (observability + recovery substrate). Off by
    # default; the standalone run never touches Omium. See workflow/omium_executor.py.
    #
    # omium_mode selects WHAT the integration does:
    #   "off"           — no Omium (default).
    #   "observability" — Mode-1: dashboard execution anchor + per-step spans +
    #                     checkpoints (emits omium.execution.completed). The legacy
    #                     ``--omium`` / CODESWARM_OMIUM=1 maps here.
    #   "corpus"        — Mode-2: drive the execution-engine so each FAILING codeswarm
    #                     task becomes a genuine omium.execution.failed carrying
    #                     codeswarm's real failure signature -> mints recovery corpus.
    # ``omium_enabled`` is kept as a derived convenience flag == (mode == "observability").
    omium_enabled: bool = False
    omium_mode: str = "off"
    omium_workflow_id: str | None = None

    @classmethod
    def from_env(cls, **overrides: object) -> "Config":
        """Build a Config from environment variables, applying explicit overrides.

        Recognised env vars:
          - ANTHROPIC_API_KEY / CODESWARM_API_KEY
          - CODESWARM_LLM_PROVIDER (anthropic|vertex|openai_compatible)
          - CODESWARM_OPENAI_API_KEY / NEBIUS_API_KEY (openai_compatible bearer key;
            codeswarm's secret convention is env vars, same as the Anthropic key —
            no key files)
          - CODESWARM_OPENAI_BASE_URL (default: Nebius Token Factory)
          - CODESWARM_OPENAI_MAX_TOKENS (default 8192; reasoning models truncate below)
          - CODESWARM_MODEL
          - CODESWARM_MAX_ITERATIONS
          - CODESWARM_MAX_RETRIES
          - CODESWARM_RUNS_DIR
          - CODESWARM_OMIUM (legacy enable=observability) / CODESWARM_OMIUM_MODE
            (off|observability|corpus) / CODESWARM_OMIUM_WORKFLOW_ID
        """
        api_key = os.environ.get("CODESWARM_API_KEY") or os.environ.get(
            "ANTHROPIC_API_KEY"
        )
        # Resolve the Omium mode: explicit CODESWARM_OMIUM_MODE wins; the legacy
        # CODESWARM_OMIUM=1 maps to "observability"; otherwise "off".
        mode_env = (os.environ.get("CODESWARM_OMIUM_MODE") or "").strip().lower()
        legacy_on = os.environ.get("CODESWARM_OMIUM", "").lower() in ("1", "true", "yes")
        if mode_env in ("off", "observability", "corpus"):
            omium_mode = mode_env
        elif legacy_on:
            omium_mode = "observability"
        else:
            omium_mode = "off"
        provider = os.environ.get("CODESWARM_LLM_PROVIDER", "anthropic").lower()
        # Model default depends on the provider (each uses different ids).
        model_env = os.environ.get("CODESWARM_MODEL")
        if model_env:
            model = model_env
        elif provider == "vertex":
            model = DEFAULT_VERTEX_MODEL
        elif provider == "openai_compatible":
            model = DEFAULT_OPENAI_COMPAT_MODEL
        else:
            model = DEFAULT_MODEL
        cfg = cls(
            model=model,
            api_key=api_key,
            llm_provider=provider,
            vertex_project=os.environ.get("CODESWARM_VERTEX_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT"),
            vertex_region=os.environ.get("CODESWARM_VERTEX_REGION", DEFAULT_VERTEX_REGION),
            openai_base_url=os.environ.get(
                "CODESWARM_OPENAI_BASE_URL", DEFAULT_OPENAI_COMPAT_BASE_URL
            ),
            openai_api_key=os.environ.get("CODESWARM_OPENAI_API_KEY")
            or os.environ.get("NEBIUS_API_KEY"),
            openai_max_tokens=int(
                os.environ.get(
                    "CODESWARM_OPENAI_MAX_TOKENS", str(DEFAULT_OPENAI_COMPAT_MAX_TOKENS)
                )
            ),
            max_iterations=int(os.environ.get("CODESWARM_MAX_ITERATIONS", "10")),
            max_retries=int(os.environ.get("CODESWARM_MAX_RETRIES", "3")),
            runs_dir=os.environ.get("CODESWARM_RUNS_DIR", "runs"),
            omium_mode=omium_mode,
            omium_enabled=(omium_mode == "observability"),
            omium_workflow_id=os.environ.get("CODESWARM_OMIUM_WORKFLOW_ID"),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        # Keep the derived observability flag consistent with the final mode
        # (an override may have changed omium_mode after construction).
        cfg.omium_enabled = (cfg.omium_mode == "observability")
        # Safety net: if the provider was switched via a CLI override but no
        # explicit model was given, use that provider's default id (the direct-API
        # default id does not exist on Vertex or Token Factory).
        if not model_env and cfg.model == DEFAULT_MODEL:
            if cfg.llm_provider == "vertex":
                cfg.model = DEFAULT_VERTEX_MODEL
            elif cfg.llm_provider == "openai_compatible":
                cfg.model = DEFAULT_OPENAI_COMPAT_MODEL
        return cfg
