"""Runs a template's scoring script in a separate, restricted interpreter.

PROTOTYPE ISOLATION ONLY. A child process with a hard timeout, an emptied
environment, a whitelisted builtins set and imports disabled stops honest
mistakes (infinite loops, stray imports). It is NOT a security boundary
against hostile code - Python restrictions of this kind can be escaped.

Production replacement (see docs/ROADMAP.md): run each execution in a
gVisor / Firecracker micro-VM or a serverless function hosted in a Canadian
region, with no network, read-only filesystem and platform-enforced limits.
Keep the `execute(tpl, data, trace)` signature so the swap is local.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RUNNER = r'''
import json, sys, builtins
SAFE = {k: getattr(builtins, k) for k in (
    "abs","all","any","bool","dict","enumerate","filter","float","int","isinstance",
    "len","list","map","max","min","pow","range","round","set","sorted","str","sum",
    "tuple","zip","ValueError","KeyError","TypeError","ZeroDivisionError","Exception",
    "ImportError")}
def _no_import(name, *a, **k):
    raise ImportError(f"import of '{name}' is not allowed in the sandbox")
SAFE["__import__"] = _no_import
def _limit(mb):
    try:
        import resource
        b = mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (b, b))
    except Exception:
        pass  # Windows has no RLIMIT_AS; production limits come from the container
req = json.loads(sys.stdin.read())
_limit(req["memoryLimitMb"])
g = {"__builtins__": SAFE}
try:
    exec(compile(req["script"], "<template-script>", "exec"), g)
    out = g[req["entrypoint"]](req["data"], req["matrix"])
    print(json.dumps({"ok": True, "result": out}))
except Exception as e:
    import traceback
    tb = traceback.extract_tb(e.__traceback__)
    line = next((f.lineno for f in reversed(tb) if f.filename == "<template-script>"), None)
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "line": line}))
'''


def matrix_config(tpl):
    """Dimensions as a dict keyed by code - the shape scripts receive."""
    return {d["code"]: {"weight": d["weight"], "name": d["name"]}
            for d in tpl["evaluationMatrix"]["dimensions"]}


def execute(tpl, data, trace):
    eng = tpl["calculationEngine"]
    req = {"script": eng["script"], "entrypoint": eng.get("entrypoint", "calculate_scores"),
           "data": data, "matrix": matrix_config(tpl), "memoryLimitMb": eng.get("memoryLimitMb", 128)}
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "runner.py"
        runner.write_text(RUNNER, encoding="utf-8")
        t = time.perf_counter()
        try:
            p = subprocess.run([sys.executable, "-I", "-S", str(runner)], input=json.dumps(req),
                               capture_output=True, text=True, timeout=eng["timeoutMs"] / 1000,
                               cwd=tmp, env=env)
        except subprocess.TimeoutExpired:
            trace.log("sandbox", "ERROR", f"killed after {eng['timeoutMs']}ms timeout")
            return None
        ms = (time.perf_counter() - t) * 1000
    if p.returncode != 0 or not p.stdout.strip():
        trace.log("sandbox", "ERROR", f"runner exit {p.returncode}: {p.stderr.strip()[-300:]}")
        return None
    try:
        res = json.loads(p.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        trace.log("sandbox", "ERROR", "script output was not valid JSON")
        return None
    if not res["ok"]:
        where = f" (script line {res['line']})" if res.get("line") else ""
        trace.log("sandbox", "ERROR", res["error"] + where)
        return None
    trace.log("sandbox", "OK", f"{req['entrypoint']} returned in {ms:.0f}ms")
    return res["result"]
