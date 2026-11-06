import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from evalcore.api import create_server, seed_templates
from evalcore.store import Store

from helpers import ROOT, payload, universal

KEY = "test-key"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.tmp.name) / "test.db")
        seed_templates(cls.store, ROOT / "templates")
        cls.srv = create_server(cls.store, port=0, api_key=KEY, quiet=True)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

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
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def evaluate(self, name="ai_agent", tid="tmpl_universal_v1", **kw):
        return self.call("POST", "/v1/evaluations", {"templateId": tid, "payload": payload(name)}, **kw)

    # ------------------------------------------------------------------
    def test_auth_required(self):
        self.assertEqual(self.call("GET", "/v1/templates", key=None)[0], 401)
        self.assertEqual(self.call("GET", "/v1/templates", key="wrong")[0], 401)
        self.assertEqual(self.call("GET", "/health", key=None)[0], 200)

    def test_localized_listing(self):
        _, en = self.call("GET", "/v1/templates")
        _, fr = self.call("GET", "/v1/templates", headers={"Accept-Language": "fr-CA"})
        names_en = {i["templateId"]: i["name"] for i in en["items"]}
        names_fr = {i["templateId"]: i["name"] for i in fr["items"]}
        self.assertEqual(names_en["tmpl_vendor_scorecard"], "Supplier Scorecard")
        self.assertEqual(names_fr["tmpl_vendor_scorecard"], "Fiche d'évaluation des fournisseurs")

    def test_zero_deploy_weight_change_and_rollback(self):
        tid = "tmpl_vendor_scorecard"
        s, before = self.evaluate("vendor_q3", tid)
        self.assertEqual(s, 201)
        v_before = before["versionNo"]

        # quality alone 0.60 -> 0.70 breaks the sum and is refused
        s, err = self.call("PATCH", f"/v1/templates/{tid}/weights", {"DIM_QUALITY": 0.70})
        self.assertEqual(s, 422)
        self.assertIn("weights sum to 1.1", err["details"][0])

        s, saved = self.call("PATCH", f"/v1/templates/{tid}/weights",
                             {"DIM_QUALITY": 0.70, "DIM_PRICE": 0.05}, headers={"X-Actor": "ops@example.ca"})
        self.assertEqual(s, 200)
        self.assertEqual(saved["versionNo"], v_before + 1)

        _, after = self.evaluate("vendor_q3", tid)
        self.assertEqual(after["versionNo"], v_before + 1)
        bd = after["result"]["breakdown"]
        expected = bd["DIM_QUALITY"] * 0.70 + bd["DIM_DELIVERY"] * 0.25 + bd["DIM_PRICE"] * 0.05
        self.assertAlmostEqual(after["result"]["compositeScore"], expected, delta=0.011)
        self.assertNotEqual(after["result"]["compositeScore"], before["result"]["compositeScore"])

        s, _ = self.call("POST", f"/v1/templates/{tid}/rollback", {"version": v_before})
        self.assertEqual(s, 200)
        _, rolled = self.evaluate("vendor_q3", tid)
        self.assertEqual(rolled["result"]["compositeScore"], before["result"]["compositeScore"])

        _, audit = self.call("GET", "/v1/audit")
        actions = [a["action"] for a in audit["items"]]
        self.assertIn("template.rollback", actions)
        self.assertTrue(any(a["actor"] == "ops@example.ca" for a in audit["items"]))

    def test_dry_run_draft_does_not_change_active(self):
        draft = universal()
        draft["calculationEngine"]["script"] = draft["calculationEngine"]["script"].replace(
            "efficiency_score = 100", "efficiency_score = 1/0")
        s, out = self.call("POST", "/v1/templates/tmpl_universal_v1/dry-run",
                           {"payload": payload("ai_agent"), "draft": draft})
        self.assertEqual(s, 200)
        self.assertEqual((out["source"], out["failedStage"]), ("draft", "sandbox"))
        _, live = self.evaluate("ai_agent")
        self.assertEqual(live["status"], "SUCCEEDED")

    def test_candidate_goes_to_human_review(self):
        s, ev = self.evaluate("hr_candidate", headers={"Accept-Language": "fr"})
        self.assertEqual(s, 201)
        self.assertEqual(ev["status"], "PENDING_REVIEW")
        self.assertIsNone(ev["final"])
        self.assertTrue(ev["disclosureNotice"].startswith("Un système automatisé"))
        self.assertIsNotNone(ev["expiresAt"])

        s, _ = self.call("POST", f"/v1/evaluations/{ev['id']}/review",
                         {"reviewer": "hr@example.ca", "decision": "OVERRIDDEN", "finalScore": 75})
        self.assertEqual(s, 400)  # override needs a reason

        s, done = self.call("POST", f"/v1/evaluations/{ev['id']}/review",
                            {"reviewer": "hr@example.ca", "decision": "OVERRIDDEN", "finalScore": 75,
                             "reason": "Strong portfolio not captured by the timed task"})
        self.assertEqual(s, 200)
        self.assertEqual(done["final"], {"score": 75, "band": "NORMAL", "decidedBy": "human"})

        s, _ = self.call("POST", f"/v1/evaluations/{ev['id']}/review",
                         {"reviewer": "hr@example.ca", "decision": "CONFIRMED"})
        self.assertEqual(s, 409)

    def test_machine_results_are_final_immediately(self):
        _, ev = self.evaluate("ai_agent")
        self.assertEqual(ev["final"]["decidedBy"], "system")
        self.assertNotIn("disclosureNotice", ev)
        self.assertIsNone(ev["expiresAt"])

    def test_subject_access_and_erasure(self):
        p = payload("hr_candidate")
        p["entity_id"] = "cand-erase-me"
        self.call("POST", "/v1/evaluations", {"templateId": "tmpl_universal_v1", "payload": p})
        self.call("POST", "/v1/evaluations", {"templateId": "tmpl_universal_v1", "payload": p})

        _, access = self.call("GET", "/v1/subjects/cand-erase-me")
        self.assertEqual(access["count"], 2)
        self.assertIn("payload", access["evaluations"][0])

        _, erased = self.call("DELETE", "/v1/subjects/cand-erase-me")
        self.assertEqual(erased["evaluationsDeleted"], 2)
        self.assertEqual(self.call("GET", "/v1/subjects/cand-erase-me")[1]["count"], 0)

        _, audit = self.call("GET", "/v1/audit")
        erase = next(a for a in audit["items"] if a["action"] == "subject.erase")
        self.assertTrue(erase["subject"].startswith("sha256:"))
        self.assertNotIn("cand-erase-me", json.dumps(audit))

    def test_retention_purge(self):
        p = payload("hr_candidate")
        p["entity_id"] = "cand-expired"
        _, ev = self.call("POST", "/v1/evaluations", {"templateId": "tmpl_universal_v1", "payload": p})
        with self.store.conn() as c:
            c.execute("UPDATE evaluations SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (ev["id"],))
        _, out = self.call("POST", "/v1/admin/purge-expired")
        self.assertGreaterEqual(out["evaluationsDeleted"], 1)
        self.assertEqual(self.call("GET", f"/v1/evaluations/{ev['id']}")[0], 404)

    def test_errors(self):
        self.assertEqual(self.call("GET", "/v1/templates/tmpl_nope")[0], 404)
        self.assertEqual(self.call("DELETE", "/v1/templates/tmpl_universal_v1")[0], 405)
        s, out = self.call("POST", "/v1/evaluations",
                           {"templateId": "tmpl_universal_v1",
                            "payload": {"targetType": "ROBOT", "entity_id": "x", "channels": {}}})
        self.assertEqual((s, out["failedStage"]), (422, "payload"))
        s, out = self.call("PUT", "/v1/templates/tmpl_universal_v1", {"evaluationTemplateId": "tmpl_other"})
        self.assertEqual(s, 422)


if __name__ == "__main__":
    unittest.main()
