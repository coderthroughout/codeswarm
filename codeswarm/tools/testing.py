"""run_tests tool: run pytest in the sandbox and return a structured pass/fail.

Deterministic and self-contained: shells out to ``python -m pytest`` in the
workspace root and parses the terminal summary for passed/failed counts.
"""
from __future__ import annotations

import re
import subprocess
import sys

from codeswarm.tools.base import ToolResult

_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped)")
# Short-summary lines (forced with -rfE): "FAILED test_x.py::test_y - AssertionError: ..."
# or "ERROR test_x.py - ImportError: ...". Captures the node id + optional message tail.
_SUMMARY_RE = re.compile(r"(?m)^(?:FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*?))?\s*$")
# Traceback error lines: "E   ImportError: cannot import name ...".
_ETYPE_RE = re.compile(r"(?m)^E\s+([A-Za-z_][\w.]*(?:Error|Exception))\b")
# A message tail that begins with an exception class, e.g. "TypeError: bad".
_EXC_TAIL_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception))\b")


def _parse_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for num, word in _COUNT_RE.findall(output):
        key = "errors" if word in ("error", "errors") else word
        counts[key] = counts.get(key, 0) + int(num)
    return counts


def _extract_failures(output: str) -> tuple[list[str], list[str]]:
    """Parse pytest output into (failing node ids, exception kinds).

    Both are sorted + de-duplicated so they are STABLE across runs (no counts,
    paths, or wall-clock) yet task/failure-specific — the basis for a meaningful
    failure signature. Exception kinds come from the short-summary tails and the
    ``E   <ExcType>:`` traceback lines; a rewritten ``assert`` counts as
    ``AssertionError``.
    """
    nodes: set[str] = set()
    kinds: set[str] = set()
    for node, tail in _SUMMARY_RE.findall(output):
        nodes.add(node)
        tail = (tail or "").strip()
        if not tail:
            continue
        m = _EXC_TAIL_RE.match(tail)
        if m:
            kinds.add(m.group(1).split(".")[-1])
        elif tail.startswith("assert"):
            kinds.add("AssertionError")
    for exc in _ETYPE_RE.findall(output):
        kinds.add(exc.split(".")[-1])
    return sorted(nodes), sorted(kinds)


class RunTestsTool:
    name = "run_tests"
    spec = {
        "name": "run_tests",
        "description": (
            "Run the task's pytest suite in the workspace and return structured "
            "pass/fail counts plus captured output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Optional test path/pattern (default: whole workspace).",
                }
            },
            "required": [],
        },
    }

    def __init__(self, workspace) -> None:
        self.workspace = workspace

    def call(self, args: dict) -> ToolResult:
        target = args.get("path")
        # -rfE forces the "short test summary" FAILED/ERROR lines we parse for
        # the failure signature (node ids + exception kinds).
        cmd = [sys.executable, "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider"]
        if target:
            cmd.append(target)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.workspace.root),
                capture_output=True,
                text=True,
                timeout=int(args.get("timeout", 120)),
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                ok=False,
                output="",
                error="pytest timed out",
                data={"passed": 0, "failed": 0, "errors": 1, "returncode": -1},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, output="", error=f"{type(exc).__name__}: {exc}")

        output = (proc.stdout or "") + (proc.stderr or "")
        counts = _parse_counts(output)
        passed_all = proc.returncode == 0
        failing_tests, error_kinds = _extract_failures(output)
        data = {
            "passed": counts["passed"],
            "failed": counts["failed"],
            "errors": counts["errors"],
            "skipped": counts["skipped"],
            "returncode": proc.returncode,
            "passed_all": passed_all,
            "failing_tests": failing_tests,
            "error_kinds": error_kinds,
        }
        # A short, stable summary token for failure signatures.
        if passed_all:
            summary = f"{counts['passed']} passed"
            data["error_type"] = None
            data["signature_token"] = None
        else:
            summary = (
                f"{counts['failed']} failed, {counts['errors']} errors, "
                f"{counts['passed']} passed (rc={proc.returncode})"
            )
            # Dominant exception kind (single) or a stable "mixed:" join.
            kinds = error_kinds or ["test_failure"]
            data["error_type"] = kinds[0] if len(kinds) == 1 else "mixed:" + "+".join(kinds)
            # STABLE, DISCRIMINATING token: exception kinds × failing test FILES
            # (per-file, not per-function, so a partial fix doesn't churn the id).
            token_files = sorted({t.split("::", 1)[0] for t in failing_tests}) or ["unknown"]
            data["signature_token"] = "+".join(kinds) + "::" + ",".join(token_files)
        data["summary"] = summary
        return ToolResult(
            ok=passed_all,
            output=output,
            error=None if passed_all else summary,
            data=data,
        )
