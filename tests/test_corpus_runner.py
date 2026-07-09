"""Tests for the bounded volume runner (codeswarm.corpus_runner) — fully offline.

The end-to-end test runs the REAL engine (MockClient, sandboxed pytest) for one
task with tiny targets and a fake httpx, asserting that:
  * a red-intent run (retry budget 1) genuinely fails -> a RED POST (force_error);
  * a green-intent run (default budget) genuinely recovers -> a GREEN POST
    (force_error_once via the seeded pool version);
  * both embed the SAME real signature message (one signature cluster).
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest

from codeswarm import corpus_runner as cr


# --------------------------------------------------------------------------- #
# Pure scheduling / guards
# --------------------------------------------------------------------------- #
def _progress(red=3, green=2, red_done=0, green_done=0) -> cr.TaskProgress:
    p = cr.TaskProgress(task_id="t", red_target=red, green_target=green)
    p.red_minted = red_done
    p.green_minted = green_done
    return p


def test_choose_polarity_prefers_larger_deficit():
    p = _progress(red=150, green=60)
    assert cr.choose_polarity(p, green_keys_available=True) == "red"
    p.red_minted = 149  # red deficit 1 < green deficit 60
    assert cr.choose_polarity(p, green_keys_available=True) == "green"


def test_choose_polarity_green_needs_keys():
    p = _progress(red=0, green=2)
    assert cr.choose_polarity(p, green_keys_available=True) == "green"
    assert cr.choose_polarity(p, green_keys_available=False) is None  # blocked, not red


def test_choose_polarity_done():
    p = _progress(red=1, green=1, red_done=1, green_done=1)
    assert p.done
    assert cr.choose_polarity(p, green_keys_available=True) is None


def test_refuse_prod_markers():
    assert cr._refuse_prod("https://api.omium.ai/api/v1") is not None
    assert cr._refuse_prod("https://x-650790810654.aws/api") is not None
    assert cr._refuse_prod("https://api-staging.omium.ai") is None
    assert cr._refuse_prod(None) is None


def test_main_rejects_unknown_task(capsys):
    rc = cr.main(["--tasks", "definitely_not_a_task", "--dry-run"])
    assert rc == 2


def test_main_rejects_desirable_above_yield():
    rc = cr.main([
        "--tasks", "math_utils", "--target-yield", "10", "--target-desirable", "20",
        "--dry-run",
    ])
    assert rc == 2


def test_main_requires_api_env_unless_dry_run(monkeypatch):
    monkeypatch.delenv("OMIUM_API_URL", raising=False)
    monkeypatch.delenv("OMIUM_API_KEY", raising=False)
    rc = cr.main(["--tasks", "math_utils"])
    assert rc == 2


def test_main_refuses_prod_env(monkeypatch):
    monkeypatch.setenv("OMIUM_API_URL", "https://api.omium.ai")
    monkeypatch.setenv("OMIUM_API_KEY", "om_x")
    rc = cr.main(["--tasks", "math_utils"])
    assert rc == 2


# --------------------------------------------------------------------------- #
# Offline end-to-end (real engine via MockClient + fake httpx)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def _install_fake_httpx(recorder: dict) -> None:
    mod = types.ModuleType("httpx")

    def _post(url, headers=None, json=None, timeout=None):  # noqa: A002
        recorder.setdefault("posts", []).append({"url": url, "json": json})
        return _FakeResponse(201, {"id": f"exec-{len(recorder['posts'])}"})

    def _get(url, headers=None, params=None, timeout=None):
        recorder.setdefault("gets", []).append(url)
        # The green's original terminalises 'completed' (verified_success path).
        return _FakeResponse(200, {"status": "completed"})

    mod.post = _post
    mod.get = _get
    sys.modules["httpx"] = mod


def test_runner_end_to_end_offline(tmp_path, monkeypatch):
    recorder: dict = {}
    _install_fake_httpx(recorder)
    monkeypatch.setenv("OMIUM_API_URL", "https://api-staging.omium.ai")
    monkeypatch.setenv("OMIUM_API_KEY", "om_test_key")
    try:
        rc = cr.main([
            "--tasks", "math_utils",
            "--target-yield", "2",
            "--target-desirable", "1",
            "--max-runs", "6",
            "--max-minutes", "5",
            "--settle", "0",
            "--pause-every", "0",
            "--poll-interval", "0",
            "--poll-timeout", "5",
            "--runs-dir", str(tmp_path / "runs"),
            "--pool-state", str(tmp_path / "pool.json"),
            "--manifest", str(tmp_path / "manifest.jsonl"),
        ])
        assert rc == 0
        posts = recorder["posts"]
        assert len(posts) == 2
        nodes = [p["json"]["metadata"]["workflow_definition"]["nodes"][1] for p in posts]
        red_nodes = [n for n in nodes if "force_error" in n]
        green_nodes = [n for n in nodes if "force_error_once" in n]
        assert len(red_nodes) == 1
        assert len(green_nodes) == 1
        # One signature cluster: identical real failure message on both arms.
        assert red_nodes[0]["force_error"] == green_nodes[0]["force_error_once"]
        assert "failed its pytest oracle" in red_nodes[0]["force_error"]
        # Green pinned to pool version 1 / key cs-green-1, green workflow id.
        assert green_nodes[0]["force_error_once_key"] == "cs-green-1"
        green_post = [p for p in posts
                      if p["json"]["metadata"].get("corpus_polarity") == "green"][0]
        assert green_post["json"]["workflow_id"] == "82a67367-4d6d-5abd-97d2-00d33a7ef863"
        assert green_post["json"]["metadata"]["workflow_version"] == 1
        # The green original was polled to terminal (key hygiene before clearing).
        assert recorder.get("gets")
        # Manifest exists and records both mints.
        manifest = (tmp_path / "manifest.jsonl").read_text(encoding="utf-8")
        assert manifest.count('"event": "minted"') == 2
    finally:
        sys.modules.pop("httpx", None)


def test_runner_dry_run_no_posts(tmp_path, monkeypatch):
    recorder: dict = {}
    _install_fake_httpx(recorder)
    monkeypatch.delenv("OMIUM_API_URL", raising=False)
    monkeypatch.delenv("OMIUM_API_KEY", raising=False)
    try:
        rc = cr.main([
            "--tasks", "math_utils",
            "--target-yield", "2",
            "--target-desirable", "1",
            "--max-runs", "4",
            "--settle", "0",
            "--pause-every", "0",
            "--dry-run",
            "--runs-dir", str(tmp_path / "runs"),
            "--pool-state", str(tmp_path / "pool.json"),
            "--manifest", str(tmp_path / "manifest.jsonl"),
        ])
        assert rc == 0
        assert "posts" not in recorder  # dry-run never POSTs
    finally:
        sys.modules.pop("httpx", None)


# --------------------------------------------------------------------------- #
# Import hygiene
# --------------------------------------------------------------------------- #
def test_runner_importable_without_omium(monkeypatch):
    # The runner must import (and the seam must run) with NO omium SDK installed.
    monkeypatch.setitem(sys.modules, "omium", None)  # import omium -> ImportError
    mod = importlib.reload(importlib.import_module("codeswarm.corpus_runner"))
    assert hasattr(mod, "CorpusRunner")
    assert hasattr(mod, "choose_polarity")
