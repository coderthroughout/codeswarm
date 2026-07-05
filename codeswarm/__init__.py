"""codeswarm — a real, standalone multi-agent software-engineering swarm.

Given a coding task with a verifiable success criterion (a pytest oracle), a small
team of LLM agents plans, edits code, runs tests, and iterates until the task passes
or a budget is exhausted — all inside an isolated sandbox. Every run emits a
structured Trajectory (the corpus row).

Hard principle: v1 has ZERO Omium dependency. The single future seam where Omium
plugs in is ``codeswarm.workflow.executor.Executor``.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
