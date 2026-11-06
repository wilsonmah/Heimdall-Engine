import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from evalcore.api import create_server, seed_templates
from evalcore.crm.dispatcher import Dispatcher
from evalcore.residency import Policy
from evalcore.store import Store

from helpers import ROOT, payload
from mock_crm import MockHubSpot, MockWebhook

KEY = "admin-key"
TID = "tmpl_cyber_risk_smb"
ORIGIN = "https://northbeam.example"


class Server:
    def __init__(self, rate_limits=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        seed_templates(self.store, ROOT / "templates")
        self.dispatcher = Dispatcher(self.store, Policy(), poll_interval=0.05, base_delay=0.05, max_attempts=4)
        self.dispatcher.start()
        self.srv = create_server(self.store, port=0, api_key=KEY, quiet=True, dispatcher=self.dispatcher,
                                 policy=Policy(), rate_limits=rate_limits or {"submit": (200, 600), "read": (500, 600)})
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.dispatcher.stop()
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, key=KEY, headers=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("X-API-Key", key)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None), dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                raw = e.read()
                return e.code, (json.loads(raw) if raw else None), dict(e.headers)


class LeadGenApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["TEST_HS_TOKEN"] = "test-hubspot-token"
        os.environ["TEST_HOOK_SECRET"] = "whsec_test"
        cls.s = Server()
        cls.hs = MockHubSpot()
        _, out, _ = cls.s.call("POST", "/v1/forms", {"templateId": TID, "allowedOrigins": [ORIGIN]})
        cls.form = out["key"]

    @classmethod
    def tearDownClass(cls):
        cls.s.close()
        cls.hs.close()

    def setUp(self):
        for d in self.s.store.list_destinations():  # each test configures its own destinations
            self.s.store.set_destination_enabled(d["id"], False)
        self.hs.fail, self.hs.delay = {}, 0.0

    # -------------------------------------------------------------- helpers
    def pub(self, method, path, body=None, form=None, headers=None):
        return self.s.call(method, f"/public/v1/assessments/{TID}{path}", body, key=None,
                           headers={"X-Form-Key": form or self.form, **(headers or {})})

    def hubspot_dest(self, region="ca-central-1", token_env="TEST_HS_TOKEN", **extra):
        _, d, _ = self.s.call("POST", "/v1/crm/destinations", {
            "name": "HubSpot", "kind": "HUBSPOT", "region": region,
            "config": {"tokenEnv": token_env, "baseUrl": self.hs.url}, **extra})
        return d

    def submit(self, name="cyber_submit_smb", email=None, headers=None):
        body = payload(name)
        if email:
            body["lead"]["corporate_email"] = email
        return self.pub("POST", "/submit", body, headers=headers)

    def wait_deliveries(self, sid, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            rows = self.s.store.list_outbox(submission_id=sid)
            if rows and all(r["status"] not in ("PENDING", "SENDING") for r in rows):
                return rows
            time.sleep(0.05)
        self.fail(f"deliveries for {sid} did not settle: {[dict(r) for r in rows]}")

    # ---------------------------------------------------------------- tests
    def test_full_flow_public_payload_then_silent_hubspot_sync(self):
        self.hubspot_dest()
        self.hs.delay = 0.4  # slow CRM: 5 calls ~ 2 s

        s, intro, h = self.pub("GET", "", headers={"Origin": ORIGIN, "Accept-Language": "fr-CA"})
        self.assertEqual(s, 200)
        self.assertEqual(h["Access-Control-Allow-Origin"], ORIGIN)
        self.assertEqual(intro["name"], "Bilan de cybersécurité en 3 minutes")
        self.assertFalse(intro["marketingConsent"]["defaultChecked"])
        self.assertEqual(intro["next"]["question"]["id"], "company_size")

        _, nxt, _ = self.pub("POST", "/next", {"answers": {"company_size": 8, "industry": "other",
                                                          "it_budget": "5k_25k", "has_firewall": False}})
        self.assertEqual(nxt["question"]["id"], "mfa_coverage")

        t = time.perf_counter()
        s, public, _ = self.submit(email="priya.flow@sandhucpa.example")
        elapsed = time.perf_counter() - t
        self.assertEqual(s, 201)
        self.assertLess(elapsed, 1.0, "submit must not wait for the CRM")
        self.assertEqual((public["risk"], public["score"]), ("HIGH", 22))
        self.assertNotIn("salesHooks", public)

        rows = self.wait_deliveries(public["submissionId"])
        self.assertEqual(rows[0]["status"], "DELIVERED", rows[0]["last_error"])
        contact = self.hs.contacts["priya.flow@sandhucpa.example"]["properties"]
        self.assertEqual(contact["cyber_risk_level"], "HIGH")
        self.assertIsInstance(contact["cyber_risk_score"], float)
        self.assertEqual(set(contact["cyber_risk_tags"].split(";")),
                         {"smb_lead", "critical_vulnerability", "backup_gap", "hot_lead"})
        self.assertEqual(contact["cyber_casl_consent"], "true")
        self.assertNotIn("cyber_governance_score", contact)  # not applicable -> not sent, never blanked
        self.assertTrue(contact["cyber_sales_hooks"].startswith("Lead lacks basic perimeter defence"))
        company = next(c for c in self.hs.companies.values() if c["properties"]["domain"] == "sandhucpa.example")
        self.assertEqual(company["properties"]["numberofemployees"], 8)
        self.assertIn((self.hs.contacts["priya.flow@sandhucpa.example"]["id"], company["id"]), self.hs.associations)
        self.assertIn("managed firewall", self.hs.notes[-1]["properties"]["hs_note_body"])

        _, sub, _ = self.s.call("GET", f"/v1/submissions/{public['submissionId']}")
        self.assertEqual(sub["deliveries"][0]["status"], "DELIVERED")
        self.assertEqual(sub["agency"]["salesHooks"][0]["id"], "no_firewall")

    def test_transient_errors_are_retried_without_duplicates(self):
        self.hubspot_dest()
        self.hs.fail = {r"/crm/v3/objects/notes": 2}  # contact+company succeed, note fails twice
        _, public, _ = self.submit(email="retry@sandhucpa.example")
        row = self.wait_deliveries(public["submissionId"])[0]
        self.assertEqual((row["status"], row["attempts"]), ("DELIVERED", 3))
        upserts = [c for c in self.hs.calls if c[1].endswith("contacts/batch/upsert")
                   and c[2]["inputs"][0]["id"] == "retry@sandhucpa.example"]
        self.assertEqual(len(upserts), 1, "completed steps must not repeat on retry")

    def test_permanent_failure_then_manual_retry(self):
        self.hubspot_dest(token_env="TEST_HS_TOKEN_MISSING")
        _, public, _ = self.submit(email="perm@sandhucpa.example")
        row = self.wait_deliveries(public["submissionId"])[0]
        self.assertEqual(row["status"], "FAILED")
        self.assertIn("not set", row["last_error"])
        os.environ["TEST_HS_TOKEN_MISSING"] = "test-hubspot-token"
        try:
            s, out, _ = self.s.call("POST", f"/v1/crm/outbox/{row['id']}/retry")
            self.assertEqual(s, 200)
            self.assertEqual(self.wait_deliveries(public["submissionId"])[0]["status"], "DELIVERED")
        finally:
            del os.environ["TEST_HS_TOKEN_MISSING"]

    def test_residency_blocks_us_hosted_crm(self):
        d = self.hubspot_dest(region="us")
        self.assertIn("outside Canada", d["warning"])
        calls_before = len(self.hs.calls)
        _, public, _ = self.submit(email="blocked@sandhucpa.example")
        rows = self.s.store.list_outbox(submission_id=public["submissionId"])
        self.assertEqual(rows[0]["status"], "BLOCKED")
        self.assertIn("outside Canada", rows[0]["reason"])
        time.sleep(0.2)
        self.assertEqual(len(self.hs.calls), calls_before)
        _, audit, _ = self.s.call("GET", "/v1/audit")
        self.assertIn("crm.blocked", [a["action"] for a in audit["items"]])

    def test_cross_border_flag_alone_is_not_enough(self):
        self.hubspot_dest(region="us", allowCrossBorder=True)
        _, public, _ = self.submit(email="xborder@sandhucpa.example")
        row = self.s.store.list_outbox(submission_id=public["submissionId"])[0]
        self.assertEqual(row["status"], "BLOCKED")
        self.assertIn("crossBorderDisclosure", row["reason"])

    def test_signed_webhook_for_zapier_or_make(self):
        hook = MockWebhook(secret="whsec_test")
        try:
            self.s.call("POST", "/v1/crm/destinations", {"name": "Zapier", "kind": "WEBHOOK", "region": "ca",
                                                         "config": {"url": hook.url, "secretEnv": "TEST_HOOK_SECRET"}})
            _, public, _ = self.submit("cyber_submit_enterprise_fr", headers={"Accept-Language": "fr-CA"})
            row = self.wait_deliveries(public["submissionId"])[0]
            self.assertEqual(row["status"], "DELIVERED", row["last_error"])
            got = hook.received[-1]
            self.assertTrue(got["signatureValid"])
            self.assertEqual(got["headers"]["X-Evalcore-Delivery"], row["id"])
            self.assertEqual(got["body"]["event"], "assessment.completed")
            self.assertEqual(got["body"]["result"]["risk"], "MEDIUM")
            self.assertEqual(public["riskLabel"], "Risque modéré")
        finally:
            hook.close()

    def test_secrets_are_refused_in_destination_config(self):
        s, out, _ = self.s.call("POST", "/v1/crm/destinations", {
            "name": "x", "kind": "HUBSPOT", "region": "ca", "config": {"token": "pat-na1-123"}})
        self.assertEqual(s, 400)
        s, _, _ = self.s.call("POST", "/v1/crm/destinations", {
            "name": "x", "kind": "WEBHOOK", "region": "ca", "config": {"url": "http://evil.example/hook"}})
        self.assertEqual(s, 400)  # https only outside localhost

    def test_form_key_and_cors(self):
        self.assertEqual(self.pub("GET", "", form="pk_wrong")[0], 401)
        s, _, _ = self.pub("GET", "", headers={"Origin": "https://attacker.example"})
        self.assertEqual(s, 403)
        s, _, h = self.s.call("OPTIONS", f"/public/v1/assessments/{TID}/submit", key=None,
                              headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
        self.assertEqual(s, 204)
        self.assertEqual(h["Access-Control-Allow-Origin"], ORIGIN)
        self.assertEqual(self.s.call("GET", "/v1/submissions", key=None)[0], 401)  # admin still needs API key

    def test_invalid_submissions(self):
        body = payload("cyber_submit_smb")
        body["lead"]["corporate_email"] = "someone@gmail.com"
        del body["answers"]["mfa_coverage"]
        body["consent"]["privacyNoticeAcknowledged"] = False
        s, out, _ = self.pub("POST", "/submit", body)
        self.assertEqual(s, 422)
        joined = " ".join(out["details"])
        for frag in ("mfa_coverage: required", "work email", "privacyNoticeAcknowledged"):
            self.assertIn(frag, joined)

    def test_honeypot(self):
        before = len(self.s.store.list_submissions(limit=1000))
        body = payload("cyber_submit_smb")
        body["website"] = "http://spam.example"
        s, out, _ = self.pub("POST", "/submit", body)
        self.assertEqual((s, out["status"]), (202, "received"))
        self.assertEqual(len(self.s.store.list_submissions(limit=1000)), before)

    def test_dry_run_previews_both_payloads_and_crm_properties(self):
        s, out, _ = self.s.call("POST", f"/v1/templates/{TID}/dry-run", {"answers": payload("cyber_submit_smb")["answers"]})
        self.assertEqual((s, out["status"]), (200, "SUCCEEDED"))
        self.assertEqual(out["public"]["risk"], "HIGH")
        self.assertEqual(out["agency"]["salesHooks"][0]["id"], "no_firewall")
        self.assertEqual(out["crmPreview"]["hubspot"]["contact"]["cyber_risk_level"], "HIGH")
        self.assertEqual(len(self.s.store.list_submissions(email="test.lead@example.ca")), 0)

    def test_subject_access_and_erasure_by_email(self):
        email = "erase.me@sandhucpa.example"
        self.submit(email=email)
        _, access, _ = self.s.call("GET", f"/v1/subjects/{email}")
        self.assertEqual(len(access["submissions"]), 1)
        _, out, _ = self.s.call("DELETE", f"/v1/subjects/{email}")
        self.assertEqual(out["recordsDeleted"], 1)
        self.assertIn("CRM", out["note"])
        self.assertEqual(self.s.call("GET", f"/v1/subjects/{email}")[1]["count"], 0)

    def test_points_template_not_accepted_on_evaluations_endpoint(self):
        s, out, _ = self.s.call("POST", "/v1/evaluations", {"templateId": TID, "payload": {}})
        self.assertEqual(s, 422)
        self.assertIn("/public/v1/assessments", out["error"])

    def test_residency_status(self):
        _, out, _ = self.s.call("GET", "/v1/admin/residency")
        self.assertEqual((out["residency"], out["schemaVersion"]), ("CA", 2))


class RateLimit(unittest.TestCase):
    def test_submit_rate_limited_per_ip(self):
        s = Server(rate_limits={"submit": (2, 600), "read": (100, 600)})
        try:
            _, out, _ = s.call("POST", "/v1/forms", {"templateId": TID, "allowedOrigins": []})
            codes = [s.call("POST", f"/public/v1/assessments/{TID}/submit", payload("cyber_submit_smb"), key=None,
                            headers={"X-Form-Key": out["key"]})[0] for _ in range(3)]
            self.assertEqual(codes, [201, 201, 429])
        finally:
            s.close()


class Migrations(unittest.TestCase):
    def test_existing_v1_database_is_upgraded(self):
        import sqlite3
        from evalcore.store import SCHEMA_SQL
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            c = sqlite3.connect(db)
            c.executescript(SCHEMA_SQL)  # a database created by v0.1, before migrations existed
            c.close()
            st = Store(db)
            self.assertEqual(st.schema_version(), 2)
            self.assertEqual(st.list_submissions(), [])


if __name__ == "__main__":
    unittest.main()
