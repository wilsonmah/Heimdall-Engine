import unittest

from evalcore import Trace, assessment, compliance, conditions, validate_template
from evalcore.residency import Policy, ResidencyError

from helpers import cyber, payload


def smb():
    return payload("cyber_submit_smb")


def ent():
    return payload("cyber_submit_enterprise_fr")


def errors(tpl):
    t = Trace()
    validate_template(tpl, t)
    return t.errors()


class Conditions(unittest.TestCase):
    ctx = {"answers": {"size": 8, "fw": False, "locs": ["cloud"]}, "score": {"overall": 40}, "risk": {"overall": "HIGH"}}

    def test_ops(self):
        m = lambda c: conditions.matches(c, self.ctx)
        self.assertTrue(m({"field": "size", "op": "lt", "value": 10}))
        self.assertTrue(m({"field": "answers.fw", "op": "eq", "value": False}))
        self.assertTrue(m({"field": "locs", "op": "contains", "value": "cloud"}))
        self.assertTrue(m({"field": "risk.overall", "op": "in", "value": ["HIGH"]}))
        self.assertTrue(m({"all": [{"field": "size", "op": "gte", "value": 1}, {"not": {"field": "fw", "op": "truthy"}}]}))

    def test_missing_field_never_matches_comparisons(self):
        m = lambda c: conditions.matches(c, self.ctx)
        self.assertFalse(m({"field": "skipped_q", "op": "eq", "value": False}))
        self.assertFalse(m({"field": "skipped_q", "op": "ne", "value": True}))
        self.assertTrue(m({"field": "skipped_q", "op": "missing"}))


class SkipLogic(unittest.TestCase):
    def test_small_company_skips_governance(self):
        shown = [q["id"] for q in assessment.visible_questions(cyber(), {"company_size": 8})]
        self.assertNotIn("incident_response_plan", shown)
        self.assertNotIn("compliance_frameworks", shown)

    def test_mid_company_gets_some_governance(self):
        shown = [q["id"] for q in assessment.visible_questions(cyber(), {"company_size": 15})]
        self.assertIn("incident_response_plan", shown)
        self.assertNotIn("compliance_frameworks", shown)  # 25+ only

    def test_no_backups_skips_backup_details(self):
        shown = [q["id"] for q in assessment.visible_questions(cyber(), {"backup_frequency": "none"})]
        for q in ("backup_locations", "restore_tested", "backup_in_canada"):
            self.assertNotIn(q, shown)

    def test_next_question_and_progress(self):
        tpl = cyber()
        n = assessment.next_question(tpl, {}, "fr-CA")
        self.assertEqual(n["question"]["id"], "company_size")
        self.assertEqual(n["question"]["text"], "Combien d'employés compte votre entreprise?")
        n = assessment.next_question(tpl, {"company_size": 8, "industry": "other", "it_budget": "under_5k",
                                           "has_firewall": False}, "en-CA")
        self.assertEqual(n["question"]["id"], "mfa_coverage")  # firewall_managed skipped
        self.assertEqual(n["progress"]["answered"], 4)

    def test_under_three_minutes(self):
        """Longest path (25+ staff, backups on, firewall on) fits the 3-minute promise."""
        tpl = cyber()
        a = ent()["answers"]
        n = assessment.next_question(tpl, {}, "en-CA")
        total = len(assessment.visible_questions(tpl, a))
        self.assertLessEqual(total * tpl["assessment"]["secondsPerQuestion"], 180)
        self.assertEqual(total, 17)
        self.assertFalse(n["done"])

    def test_answers_to_hidden_questions_are_ignored(self):
        a = smb()["answers"]
        a["firewall_managed"] = "managed_provider"  # firewall is false -> question hidden
        r = assessment.score_submission(cyber(), a)
        self.assertNotIn("firewall_managed", r["answers"])
        self.assertIn("firewall_managed", r["skipped"])

    def test_answer_validation(self):
        tpl = cyber()
        errs = assessment.answers_errors(tpl, {"company_size": "eight", "mfa_coverage": "some", "bogus": 1}, False)
        self.assertEqual(len(errs), 3)
        errs = assessment.answers_errors(tpl, {"company_size": 8}, True)
        self.assertTrue(any("has_firewall: required" in e for e in errs))


class Scoring(unittest.TestCase):
    def test_smb_high_risk(self):
        r = assessment.score_submission(cyber(), smb()["answers"])
        dims = {d["code"]: d for d in r["dimensions"]}
        self.assertEqual((dims["DIM_NETWORK"]["earned"], dims["DIM_NETWORK"]["possible"]), (17, 80))
        self.assertEqual(dims["DIM_AWARENESS"]["score"], 40.0)
        self.assertEqual(dims["DIM_BACKUP"]["score"], 10.0)
        self.assertIsNone(dims["DIM_GOVERNANCE"]["score"])  # not applicable to 8 staff
        self.assertAlmostEqual(r["overall"], (21.2 * .35 + 40 * .20 + 10 * .30) / .85, delta=0.06)
        self.assertEqual(r["risk"], "HIGH")
        self.assertEqual(set(r["tags"]), {"smb_lead", "critical_vulnerability", "backup_gap", "hot_lead"})
        self.assertEqual([h["id"] for h in r["hooks"]],
                         ["no_firewall", "mfa_gap", "untested_restore", "backups_outside_canada", "awareness_gap"])

    def test_deductions_are_itemized_and_add_up(self):
        r = assessment.score_submission(cyber(), smb()["answers"])
        for d in r["dimensions"]:
            self.assertEqual(sum(x["pointsDeducted"] for x in d["deductions"]), d["possible"] - d["earned"])
        net = next(d for d in r["dimensions"] if d["code"] == "DIM_NETWORK")
        fw = next(x for x in net["deductions"] if x["questionId"] == "has_firewall")
        self.assertEqual((fw["pointsDeducted"], fw["impactOnDimension"]), (30, 37.5))

    def test_enterprise_medium_risk(self):
        r = assessment.score_submission(cyber(), ent()["answers"])
        dims = {d["code"]: d["score"] for d in r["dimensions"]}
        self.assertEqual(dims, {"DIM_NETWORK": 86.0, "DIM_AWARENESS": 85.0, "DIM_BACKUP": 80.0, "DIM_GOVERNANCE": 15.0})
        self.assertAlmostEqual(r["overall"], 73.35, delta=0.06)
        self.assertEqual(r["risk"], "MEDIUM")
        self.assertEqual(set(r["tags"]), {"enterprise_lead", "regulated_industry"})
        self.assertEqual([h["id"] for h in r["hooks"]],
                         ["self_managed_firewall", "no_ir_plan", "insurance_readiness", "regulated_industry"])

    def test_multi_choice_is_capped(self):
        a = ent()["answers"]
        a["backup_locations"] = ["cloud", "immutable", "offsite_physical"]  # 40 points, cap 30
        r = assessment.score_submission(cyber(), a)
        item = next(i for i in r["items"] if i["questionId"] == "backup_locations")
        self.assertEqual(item["pointsEarned"], 30)

    def test_status_override_escalates(self):
        a = ent()["answers"]
        a.update(backup_frequency="none", incident_response_plan=True, cyber_insurance="yes",
                 compliance_frameworks=["soc2"])
        r = assessment.score_submission(cyber(), a)
        self.assertEqual((r["risk"], r["riskOverride"]), ("HIGH", "no_backups"))
        self.assertGreater(r["overall"], 50)  # score alone would not be HIGH


class Payloads(unittest.TestCase):
    def build(self, sub, lang):
        tpl = cyber()
        lead = compliance.clean_lead(sub["lead"])
        r = assessment.score_submission(tpl, sub["answers"], lead, lang)
        pub = assessment.public_view(tpl, r, "sub_x", lang)
        consent = compliance.consent_record(tpl, "1.0.0", sub["consent"], "2026-11-06T12:00:00+00:00", lang)
        ag = assessment.agency_view(tpl, {"versionNo": 1, "semver": "1.0.0"}, r, "sub_x", "2026-11-06T12:00:00+00:00",
                                    lead, consent, sub["source"], lang)
        return pub, ag

    def test_public_payload_is_lightweight_and_leaks_nothing(self):
        pub, _ = self.build(smb(), "en-CA")
        self.assertEqual((pub["score"], pub["risk"], pub["riskLabel"]), (22, "HIGH", "High risk"))
        self.assertEqual(len(pub["dimensions"]), 3)
        text = str(pub)
        for secret in ("hooks", "tags", "deduct", "cross-selling", "hot_lead", "Priya"):
            self.assertNotIn(secret, text)
        self.assertEqual(pub["chart"]["type"], "RADAR_CHART")

    def test_public_payload_in_french(self):
        pub, _ = self.build(ent(), "fr-CA")
        self.assertEqual(pub["riskLabel"], "Risque modéré")
        self.assertEqual(pub["dimensions"][0]["name"], "Sécurité du réseau")
        self.assertTrue(pub["summary"].startswith("Vous avez une bonne base"))

    def test_agency_payload(self):
        _, ag = self.build(smb(), "en-CA")
        self.assertEqual(ag["lead"]["corporate_email"], "priya@sandhucpa.example")
        self.assertEqual(ag["lead"]["email_domain"], "sandhucpa.example")
        self.assertEqual(ag["salesHooks"][0]["text"],
                         "Lead lacks basic perimeter defence; prioritize cross-selling managed firewall services.")
        self.assertTrue(ag["consent"]["marketing"]["granted"])
        self.assertIn("unsubscribe", ag["consent"]["marketing"]["text"])
        self.assertIn("firewall_managed", ag["skippedQuestions"])
        self.assertEqual(ag["source"]["utm_campaign"], "q4-cpa-firms")

    def test_agency_payload_stays_in_agency_locale(self):
        _, ag = self.build(ent(), "fr-CA")  # respondent answered in French
        self.assertEqual(ag["respondentLocale"], "fr-CA")
        self.assertEqual(ag["result"]["dimensions"][0]["name"], "Network Security")
        self.assertFalse(ag["consent"]["marketing"]["granted"])
        self.assertIn("Oui, Northbeam", ag["consent"]["marketing"]["text"])  # wording shown to the person


class TemplateRules(unittest.TestCase):
    def test_cyber_template_valid(self):
        self.assertEqual(errors(cyber()), [])

    def test_showif_must_reference_earlier_question(self):
        tpl = cyber()
        tpl["assessment"]["questions"][0]["showIf"] = {"field": "has_firewall", "op": "eq", "value": True}
        self.assertTrue(any("must reference an earlier question" in e for e in errors(tpl)))

    def test_rules_reference_real_things(self):
        tpl = cyber()
        tpl["rules"]["tags"][0]["when"] = {"field": "score.DIM_NOPE", "op": "lt", "value": 1}
        tpl["rules"]["salesHooks"][0]["when"] = {"field": "no_such_question", "op": "eq", "value": 1}
        errs = errors(tpl)
        self.assertTrue(any("unknown dimension 'DIM_NOPE'" in e for e in errs))
        self.assertTrue(any("unknown question 'no_such_question'" in e for e in errs))

    def test_french_required_for_every_public_string(self):
        tpl = cyber()
        del tpl["assessment"]["questions"][3]["options"][0]["label"]["fr-CA"]
        self.assertTrue(any("missing fr-CA text" in e for e in errors(tpl)))

    def test_lead_compliance_required(self):
        tpl = cyber()
        del tpl["compliance"]["sharingDisclosure"]
        del tpl["compliance"]["marketingConsent"]["senderName"]
        errs = errors(tpl)
        self.assertTrue(any("sharingDisclosure" in e for e in errs) or any("senderName" in e for e in errs))

    def test_crm_mapping_checked(self):
        tpl = cyber()
        tpl["crmMapping"]["hubspot"]["contact"]["bad"] = "answers.not_a_question"
        del tpl["crmMapping"]["hubspot"]["contact"]["email"]
        errs = errors(tpl)
        self.assertTrue(any("unknown question 'not_a_question'" in e for e in errs))
        self.assertTrue(any("contact.email: required" in e for e in errs))


class Leads(unittest.TestCase):
    def test_valid_lead(self):
        s = smb()
        self.assertEqual(compliance.lead_errors(cyber(), s["lead"], s["consent"]), [])

    def test_free_email_rejected(self):
        s = smb()
        s["lead"]["corporate_email"] = "priya.sandhu@gmail.com"
        self.assertIn("work email", compliance.lead_errors(cyber(), s["lead"], s["consent"])[0])

    def test_privacy_notice_must_be_acknowledged(self):
        s = smb()
        errs = compliance.lead_errors(cyber(), s["lead"], {"marketingOptIn": True})
        self.assertTrue(any("privacyNoticeAcknowledged" in e for e in errs))

    def test_marketing_consent_never_assumed(self):
        rec = compliance.consent_record(cyber(), "1.0.0", {"privacyNoticeAcknowledged": True}, "t", "en-CA")
        self.assertFalse(rec["marketing"]["granted"])
        self.assertIsNone(rec["marketing"]["at"])


class Residency(unittest.TestCase):
    def test_storage_region(self):
        Policy(region="ca-central-1", env="production").check_storage()
        Policy(region="local").check_storage()
        with self.assertRaises(ResidencyError):
            Policy(region="us-east-1").check_storage()
        with self.assertRaises(ResidencyError):
            Policy(region="local", env="production").check_storage()
        Policy(residency="ANY", region="us-east-1").check_storage()

    def test_destinations(self):
        p, tpl = Policy(), cyber()
        self.assertTrue(p.destination_allowed("ca-central-1", False, tpl)[0])
        self.assertFalse(p.destination_allowed("us", False, tpl)[0])
        self.assertFalse(p.destination_allowed("us", True, tpl)[0])  # no disclosure in template
        tpl["compliance"]["crossBorderDisclosure"] = {"en-CA": "Stored in the US.", "fr-CA": "Conservé aux É.-U."}
        self.assertTrue(p.destination_allowed("us", True, tpl)[0])


if __name__ == "__main__":
    unittest.main()
