import unittest

from evalcore import Trace, evaluate, validate_template
from evalcore.i18n import pick_lang, text

from helpers import payload, universal, vendor


def run(tpl, p):
    t = Trace()
    result, failed = evaluate(tpl, p, t)
    return result, failed, t.errors()


class TemplateValidation(unittest.TestCase):
    def test_shipped_templates_are_valid(self):
        for tpl in (universal(), vendor()):
            self.assertTrue(validate_template(tpl, Trace()), tpl["evaluationTemplateId"])

    def test_weights_must_sum_to_one(self):
        tpl = universal()
        tpl["evaluationMatrix"]["dimensions"][1]["weight"] = 0.7
        t = Trace()
        self.assertFalse(validate_template(tpl, t))
        self.assertIn("weights sum to 1.1", t.errors()[0])

    def test_script_referencing_unknown_dimension(self):
        tpl = universal()
        tpl["calculationEngine"]["script"] = tpl["calculationEngine"]["script"].replace(
            "'DIM_QUALITY_ACCURACY']['weight']", "'DIM_QUALITY']['weight']")
        t = Trace()
        self.assertFalse(validate_template(tpl, t))
        self.assertTrue(any("unknown dimension DIM_QUALITY" in e for e in t.errors()))

    def test_syntax_error_reported_with_line(self):
        tpl = universal()
        tpl["calculationEngine"]["script"] = "def calculate_scores(d, m):\n    return {\n"
        t = Trace()
        self.assertFalse(validate_template(tpl, t))
        self.assertIn("SyntaxError", t.errors()[0])


class Compliance(unittest.TestCase):
    def errors_for(self, tpl):
        t = Trace()
        validate_template(tpl, t)
        return t.errors()

    def test_people_templates_need_compliance_block(self):
        tpl = universal()
        del tpl["compliance"]
        self.assertIn("compliance: required", self.errors_for(tpl)[0])

    def test_vendor_only_template_needs_no_compliance(self):
        self.assertEqual(self.errors_for(vendor()), [])

    def test_quebec_requires_human_review_and_french(self):
        tpl = universal()
        tpl["compliance"]["automatedDecision"]["humanReviewRequired"] = False
        tpl["compliance"]["purpose"] = "English only"
        errs = self.errors_for(tpl)
        self.assertTrue(any("humanReviewRequired" in e and "CA-QC" in e for e in errs))
        self.assertTrue(any("purpose: French text required" in e for e in errs))

    def test_without_quebec_english_only_is_fine(self):
        tpl = universal()
        tpl["compliance"]["jurisdictions"] = ["CA-ON"]
        tpl["compliance"]["purpose"] = "English only"
        tpl["compliance"]["automatedDecision"]["humanReviewRequired"] = False
        self.assertEqual(self.errors_for(tpl), [])

    def test_retention_cap(self):
        tpl = universal()
        tpl["compliance"]["retentionDays"] = 4000
        self.assertTrue(any("retentionDays" in e for e in self.errors_for(tpl)))

    def test_candidate_without_consent_is_rejected(self):
        p = payload("hr_candidate")
        del p["consent"]
        _, failed, errs = run(universal(), p)
        self.assertEqual(failed, "payload")
        self.assertTrue(any("consent.obtained" in e for e in errs))

    def test_ai_agent_needs_no_consent(self):
        _, failed, _ = run(universal(), payload("ai_agent"))
        self.assertIsNone(failed)


class Scoring(unittest.TestCase):
    def test_ai_agent(self):
        result, failed, _ = run(universal(), payload("ai_agent"))
        self.assertIsNone(failed)
        self.assertEqual(result["compositeScore"], 89.8)
        self.assertEqual(result["band"], "NORMAL")

    def test_candidate(self):
        result, _, _ = run(universal(), payload("hr_candidate"))
        self.assertEqual(result["compositeScore"], 52.0)
        self.assertEqual(result["band"], "CRITICAL")

    def test_vendor(self):
        result, failed, _ = run(vendor(), payload("vendor_q3"))
        self.assertIsNone(failed)
        self.assertEqual(result["compositeScore"], 86.22)

    def test_bad_payload_rejected_before_script(self):
        _, failed, errs = run(universal(), payload("bad_slider"))
        self.assertEqual(failed, "payload")
        self.assertEqual(len(errs), 2)

    def test_quality_is_clamped(self):
        p = payload("ai_agent")
        p["channels"]["ch_automated_telemetry"]["error_rate_percentage"] = 100
        result, _, _ = run(universal(), p)
        self.assertEqual(result["breakdown"]["DIM_QUALITY_ACCURACY"], 0)


class Sandbox(unittest.TestCase):
    def with_script(self, script, timeout=2000):
        tpl = universal()
        tpl["calculationEngine"]["script"] = script
        tpl["calculationEngine"]["timeoutMs"] = timeout
        return run(tpl, payload("ai_agent"))

    def test_timeout(self):
        _, failed, errs = self.with_script("def calculate_scores(d, m):\n    while True:\n        pass\n", 500)
        self.assertEqual(failed, "sandbox")
        self.assertIn("timeout", errs[0])

    def test_imports_blocked(self):
        _, failed, errs = self.with_script("import os\ndef calculate_scores(d, m):\n    return {}\n")
        self.assertEqual(failed, "sandbox")
        self.assertIn("import of 'os' is not allowed", errs[0])

    def test_runtime_error_has_line(self):
        _, _, errs = self.with_script("def calculate_scores(d, m):\n    x = 1\n    return 1 / 0\n")
        self.assertIn("ZeroDivisionError", errs[0])
        self.assertIn("script line 3", errs[0])

    def test_output_contract(self):
        _, failed, errs = self.with_script("def calculate_scores(d, m):\n    return {'compositeScore': 150}\n")
        self.assertEqual(failed, "output")
        self.assertTrue(any("outside 0-100" in e for e in errs))


class I18n(unittest.TestCase):
    def test_pick_lang(self):
        self.assertEqual(pick_lang("fr-CA,fr;q=0.9,en;q=0.8"), "fr-CA")
        self.assertEqual(pick_lang("fr"), "fr-CA")
        self.assertEqual(pick_lang("de-DE"), "en-CA")
        self.assertEqual(pick_lang("fr-CA", override="en"), "en-CA")

    def test_text(self):
        self.assertEqual(text({"en": "Quality", "fr": "Qualité"}, "fr-CA"), "Qualité")
        self.assertEqual(text({"en-CA": "Quality", "fr-CA": "Qualité"}, "fr"), "Qualité")
        self.assertEqual(text({"en": "Quality"}, "fr-CA"), "Quality")
        self.assertEqual(text("Plain", "fr-CA"), "Plain")


if __name__ == "__main__":
    unittest.main()
