"""Command line: local debugging and the API server.

    python -m evalcore validate <template.json>
    python -m evalcore run      <template.json> <payload.json> [--trace]
    python -m evalcore serve    [--host 127.0.0.1] [--port 8700] [--db eval.db] [--templates templates]

EVAL_API_KEY in the environment turns on API key auth for `serve`.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from .api import create_server, seed_templates
from .crm.dispatcher import Dispatcher
from .pipeline import evaluate
from .residency import Policy, ResidencyError
from .store import Store
from .trace import Trace
from .validation import validate_template


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evalcore", description="Headless evaluation engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="validate a template file")
    v.add_argument("template")
    r = sub.add_parser("run", help="dry-run a template against a payload file")
    r.add_argument("template")
    r.add_argument("payload")
    r.add_argument("--trace", action="store_true", help="include the trace in the JSON output")
    s = sub.add_parser("serve", help="start the REST API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8700)
    s.add_argument("--db", default="eval.db")
    s.add_argument("--templates", default="templates", help="seed templates from this folder on start")
    a = ap.parse_args(argv)

    if a.cmd == "validate":
        return 0 if validate_template(_load(a.template), Trace(echo=True)) else 1

    if a.cmd == "run":
        trace = Trace(echo=True)
        result, failed = evaluate(_load(a.template), _load(a.payload), trace)
        out = {"status": "FAILED" if failed else "SUCCEEDED", "failedStage": failed, "result": result}
        if a.trace:
            out["trace"] = trace.events
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 1 if failed else 0

    policy = Policy.from_env()
    try:
        policy.check_storage()
    except ResidencyError as e:
        print(f"refusing to start: {e}", file=sys.stderr)
        return 2
    store = Store(a.db)
    if Path(a.templates).is_dir():
        seed_templates(store, a.templates)
    key = os.environ.get("EVAL_API_KEY")
    dispatcher = Dispatcher(store, policy)
    dispatcher.start()
    srv = create_server(store, a.host, a.port, key, dispatcher=dispatcher, policy=policy,
                        trust_proxy=os.environ.get("EVAL_TRUST_PROXY") == "1")
    print(f"evalcore listening on http://{a.host}:{a.port} (auth {'on' if key else 'OFF - dev only'}; "
          f"residency {policy.residency}, region {policy.region}; CRM dispatcher running)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dispatcher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
