"""codeswarm web UI — type a real task, watch the swarm work live.

A tiny Starlette app with Server-Sent Events. `GET /` serves a one-page UI;
`GET /stream?prompt=...` runs codeswarm on the free-form task and streams every
agent/tool/test/verdict event as it happens. Launched by `codeswarm serve`.

This is intentionally thin and stdlib-adjacent (starlette + sse-starlette, which
ship with the omium SDK deps). It reuses the same engine the CLI uses — the UI is
just a live window onto a real run.
"""
from __future__ import annotations

import asyncio
import json

from codeswarm.agents.coder import CoderAgent
from codeswarm.agents.planner import PlannerAgent
from codeswarm.agents.reviewer import ReviewerAgent
from codeswarm.agents.tester import TesterAgent
from codeswarm.config import Config
from codeswarm.llm.client import AnthropicClient
from codeswarm.tasks import get_task
from codeswarm.tasks.spec import Task
from codeswarm.tools import default_tools
from codeswarm.workflow.engine import WorkflowEngine
from codeswarm.workflow.executor import LocalExecutor


def _slug(text: str) -> str:
    import re

    return ("-".join(re.findall(r"[a-z0-9]+", text.lower())[:5]) or "task")[:40]


def _persist(config, traj) -> None:
    """Save the trajectory to runs/<run_id>.jsonl (the web path did not before)."""
    from pathlib import Path

    try:
        d = Path(config.runs_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{traj.run_id}.jsonl").write_text(traj.to_jsonl(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _summarize(ev) -> str:
    """One human-readable line for a trajectory event."""
    k, ag, p = ev.kind, ev.agent, ev.payload
    if k == "agent" and ag == "planner":
        return f"planner · plan with {len(p.get('plan', []))} step(s)"
    if k == "agent" and ag == "spec":
        return f"spec · wrote acceptance tests ({'ok' if p.get('wrote_tests') else 'none'})"
    if k == "agent" and ag == "reviewer":
        return "reviewer · " + (p.get("hint", "") or "")[:120]
    if k == "tool":
        path = (p.get("args") or {}).get("path", "")
        return f"{ag} · {p.get('tool')}({path}) {'✓' if p.get('ok') else '✗ ' + str(p.get('error', ''))[:80]}"
    if k == "test":
        return f"tester · {p.get('passed', 0)} passed / {p.get('failed', 0)} failed / {p.get('errors', 0)} err"
    if k == "failure":
        return f"failure · {p.get('error_type', '')} ({p.get('failed', 0)} failing)"
    if k == "recovery":
        return "recovery · retrying with reviewer hint"
    if k == "checkpoint":
        return f"checkpoint · {ev.step_id}"
    if k == "verdict":
        return f"verdict · {'PASS' if p.get('passed') else 'FAIL'} {p.get('signals', {})}"
    return f"{k} · {ag or ''}"


async def _run_stream(prompt: str, task_id: str | None):
    """Async generator of SSE dicts for one run."""
    config = Config.from_env()
    if prompt:
        task = Task.from_prompt(prompt, task_id=_slug(prompt))
        use_real = True
    elif task_id:
        task = get_task(task_id)
        use_real = True
    else:
        yield {"event": "error", "data": json.dumps({"error": "no prompt or task"})}
        return

    # Capture written file contents so we can show the final code (the sandbox is
    # torn down after the run). Non-invasive: we wrap write_file per run.
    captured: dict[str, str] = {}

    def tools_factory(ws):
        tools = default_tools(ws)
        wf = tools.get("write_file")
        if wf is not None:
            orig = wf.call

            def wrapped(args, _orig=orig):
                r = _orig(args)
                if getattr(r, "ok", False):
                    captured[args.get("path", "")] = args.get("content", "")
                return r

            wf.call = wrapped
        return tools

    llm = AnthropicClient(config)
    engine = WorkflowEngine(
        config=config,
        llm=llm,
        planner=PlannerAgent(),
        executor=LocalExecutor(
            coder=CoderAgent(), tester=TesterAgent(), reviewer=ReviewerAgent(),
            max_retries=config.max_retries,
        ),
        tools_factory=tools_factory,
    )

    # Stream the run into Omium too (execution + checkpoints + traces) so it shows
    # in the dashboard — the SAME seam the CLI's --omium uses. Fail-soft: if Omium
    # isn't configured, OmiumRun no-ops and the local run is unaffected.
    import uuid as _uuid

    from codeswarm.workflow.omium_executor import OmiumExecutor, OmiumRun

    run_id = f"{task.id}-{_uuid.uuid4().hex[:8]}"
    omium_run = OmiumRun(config).start(task, run_id)
    engine.executor = OmiumExecutor(engine.executor, omium_run)

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def on_event(ev):
        loop.call_soon_threadsafe(queue.put_nowait, ("event", ev))

    async def runner():
        try:
            traj = await engine.run(task, run_id=run_id, on_event=on_event)
            _persist(config, traj)
            omium_run.finish(traj.verdict)
            queue.put_nowait(("done", traj))
        except Exception as e:  # noqa: BLE001
            queue.put_nowait(("error", repr(e)))

    task_h = asyncio.create_task(runner())
    yield {"event": "start", "data": json.dumps({"task": task.id, "prompt": prompt or task_id})}
    while True:
        kind, payload = await queue.get()
        if kind == "event":
            yield {"event": "step", "data": json.dumps({
                "kind": payload.kind, "agent": payload.agent,
                "ts": payload.ts_index, "line": _summarize(payload),
            })}
        elif kind == "done":
            v = payload.verdict
            yield {"event": "done", "data": json.dumps({
                "passed": bool(v and v.passed),
                "signals": (v.signals if v else {}),
                "run_id": payload.run_id,
                "failure_signature": payload.failure_signature,
                "omium_url": omium_run.dashboard_url,
                "files": captured,
            })}
            break
        elif kind == "error":
            yield {"event": "error", "data": json.dumps({"error": payload})}
            break
    await task_h


def _build_app():
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Route
    from sse_starlette.sse import EventSourceResponse

    async def index(_request):
        return HTMLResponse(_INDEX_HTML)

    async def stream(request):
        prompt = request.query_params.get("prompt", "").strip()
        task_id = request.query_params.get("task")
        return EventSourceResponse(_run_stream(prompt, task_id))

    return Starlette(routes=[Route("/", index), Route("/stream", stream)])


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn

    print(f"codeswarm web UI → http://{host}:{port}")
    print("(uses the same LLM config as the CLI: set CODESWARM_LLM_PROVIDER=vertex etc.)")
    uvicorn.run(_build_app(), host=host, port=port, log_level="warning")


_INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>codeswarm</title>
<style>
  :root{color-scheme:dark}
  body{background:#0d1117;color:#c9d1d9;font:14px/1.5 ui-monospace,Menlo,Consolas,monospace;margin:0;padding:24px}
  h1{font-size:18px;margin:0 0 4px}.sub{color:#8b949e;margin:0 0 16px}
  .row{display:flex;gap:8px;margin-bottom:16px}
  input{flex:1;background:#161b22;border:1px solid #30363d;color:#c9d1d9;padding:10px;border-radius:6px;font:inherit}
  button{background:#238636;border:0;color:#fff;padding:10px 18px;border-radius:6px;cursor:pointer;font:inherit}
  button:disabled{background:#30363d;cursor:default}
  #log{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px;min-height:120px;white-space:pre-wrap}
  .ev{padding:2px 0;border-bottom:1px solid #21262d}
  .agent{color:#58a6ff}.tool{color:#8b949e}.test{color:#d29922}.failure{color:#f85149}.recovery{color:#db6d28}.verdict{color:#3fb950}
  #result{margin-top:16px}
  .pill{display:inline-block;padding:2px 10px;border-radius:20px;font-weight:600}
  .pass{background:#238636;color:#fff}.fail{background:#da3633;color:#fff}
  pre{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px;overflow:auto;max-height:340px}
  .muted{color:#8b949e}
</style></head>
<body>
  <h1>codeswarm</h1>
  <p class="sub">Give it a real coding task. The swarm writes its own tests, implements, and verifies — live.</p>
  <div class="row">
    <input id="prompt" placeholder="e.g. build a function that parses a CSV line handling quoted fields, with tests"
           value="build a function slugify(text) that lowercases, trims, and replaces runs of non-alphanumerics with single hyphens">
    <button id="go">Run</button>
  </div>
  <div id="log"></div>
  <div id="result"></div>
<script>
const log=document.getElementById('log'), res=document.getElementById('result'), go=document.getElementById('go'), inp=document.getElementById('prompt');
function line(cls,txt){const d=document.createElement('div');d.className='ev '+cls;d.textContent=txt;log.appendChild(d);log.scrollTop=log.scrollHeight;}
go.onclick=()=>{
  const p=inp.value.trim(); if(!p)return;
  log.innerHTML=''; res.innerHTML=''; go.disabled=true; go.textContent='Running…';
  const es=new EventSource('/stream?prompt='+encodeURIComponent(p));
  es.addEventListener('start',e=>line('muted','▶ '+JSON.parse(e.data).task));
  es.addEventListener('step',e=>{const d=JSON.parse(e.data);line(d.kind,'· '+d.line);});
  es.addEventListener('done',e=>{
    const d=JSON.parse(e.data);
    const pill=d.passed?'<span class="pill pass">PASS</span>':'<span class="pill fail">FAIL</span>';
    let html=pill+' <span class="muted">'+JSON.stringify(d.signals)+' · run '+d.run_id+'</span>';
    if(d.omium_url){html+=' <a href="'+d.omium_url+'" target="_blank" style="color:#58a6ff">↗ view in Omium</a>';}
    for(const [path,content] of Object.entries(d.files||{})){
      html+='<h3>'+path+'</h3><pre>'+content.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))+'</pre>';
    }
    res.innerHTML=html; go.disabled=false; go.textContent='Run'; es.close();
  });
  es.addEventListener('error',e=>{line('failure','error: '+(e.data?JSON.parse(e.data).error:'stream error'));go.disabled=false;go.textContent='Run';es.close();});
};
inp.addEventListener('keydown',e=>{if(e.key==='Enter')go.click();});
</script>
</body></html>
"""
