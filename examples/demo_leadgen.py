"""End-to-end demo: a prospect takes the 3-minute cyber check on an MSP's website.

    python examples/demo_leadgen.py

Starts the API on a random port with a local mock HubSpot, submits the two
sample leads, and writes what each party receives to examples/outputs/.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from evalcore.api import create_server, seed_templates  # noqa: E402
from evalcore.crm.dispatcher import Dispatcher  # noqa: E402
from evalcore.residency import Policy  # noqa: E402
from evalcore.store import Store  # noqa: E402
from mock_crm import MockHubSpot  # noqa: E402

OUT = ROOT / "examples" / "outputs"
TID = "tmpl_cyber_risk_smb"


def call(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def main():
    os.environ["DEMO_HUBSPOT_TOKEN"] = "demo-token"
    hs = MockHubSpot(token="demo-token")
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "demo.db")
    seed_templates(store, ROOT / "templates")
    disp = Dispatcher(store, Policy(), poll_interval=0.05)
    disp.start()
    srv = create_server(store, port=0, quiet=True, dispatcher=disp, policy=Policy())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    key = call(base, "POST", "/v1/forms", {"templateId": TID, "allowedOrigins": ["https://northbeam.example"]})["key"]
    call(base, "POST", "/v1/crm/destinations", {"name": "Northbeam HubSpot (CA)", "kind": "HUBSPOT",
                                                "region": "ca", "config": {"tokenEnv": "DEMO_HUBSPOT_TOKEN",
                                                                           "baseUrl": hs.url}})
    OUT.mkdir(parents=True, exist_ok=True)
    for name, lang in (("cyber_submit_smb", "en-CA"), ("cyber_submit_enterprise_fr", "fr-CA")):
        body = json.loads((ROOT / "examples" / "payloads" / f"{name}.json").read_text(encoding="utf-8"))
        t = time.perf_counter()
        public = call(base, "POST", f"/public/v1/assessments/{TID}/submit", body,
                      {"X-Form-Key": key, "Accept-Language": lang})
        ms = (time.perf_counter() - t) * 1000
        sid = public["submissionId"]
        for _ in range(200):
            sub = call(base, "GET", f"/v1/submissions/{sid}")
            if all(d["status"] not in ("PENDING", "SENDING") for d in sub["deliveries"]):
                break
            time.sleep(0.05)
        email = sub["agency"]["lead"]["corporate_email"]
        hubspot = {"contact": hs.contacts[email]["properties"],
                   "company": next(c["properties"] for c in hs.companies.values()
                                   if c["properties"]["domain"] == sub["agency"]["lead"]["email_domain"]),
                   "noteHtml": hs.notes[-1]["properties"]["hs_note_body"] if sub["agency"]["salesHooks"] else None}
        tag = name.replace("cyber_submit_", "")
        for suffix, obj in (("1_public_response", public), ("2_agency_payload", sub["agency"]),
                            ("3_hubspot_received", hubspot)):
            (OUT / f"{tag}.{suffix}.json").write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
                                                      encoding="utf-8")
        print(f"{tag:<14} {lang}  public response in {ms:5.0f} ms  score={public['score']:>3} risk={public['risk']:<6}"
              f" tags={sub['agency']['tags']}  CRM: {sub['deliveries'][0]['status']}")
    disp.stop()
    srv.shutdown()
    hs.close()
    tmp.cleanup()


if __name__ == "__main__":
    main()
