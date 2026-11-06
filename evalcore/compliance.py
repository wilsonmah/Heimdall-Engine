"""Canadian privacy rules enforced at template-save time and at evaluation time.

This is engineering guard-rails, not legal advice. The rules encode common
obligations for Canadian SMBs evaluating people:

* PIPEDA (federal private sector): identified purpose, consent, limited
  retention, individual access.
* Quebec Law 25: a person must be informed when a decision about them is
  based exclusively on automated processing and may have it reviewed by a
  human; French must be available.
* Ontario Working for Workers Act (job postings must disclose AI use in
  screening) - surfaced as the disclosure notice returned with each result.

Rules apply only when a template can evaluate HUMAN_CANDIDATE targets;
vendor and AI-agent evaluations are not personal information.
"""
from .i18n import has_lang

HUMAN = "HUMAN_CANDIDATE"
MAX_RETENTION_DAYS = 2555  # ~7 years; longer needs a documented justification


def evaluates_people(tpl):
    return HUMAN in tpl.get("targetSchema", {}).get("allowedTypes", [])


def template_errors(tpl):
    """Hard errors that block saving the template."""
    if not evaluates_people(tpl):
        return []
    c = tpl.get("compliance")
    if not c:
        return ["compliance: required when allowedTypes includes HUMAN_CANDIDATE"]
    errs = []
    if not c.get("purpose"):
        errs.append("compliance.purpose: state why personal information is collected (PIPEDA 4.2)")
    if not c.get("retentionDays"):
        errs.append("compliance.retentionDays: required for personal information (PIPEDA 4.5)")
    elif c["retentionDays"] > MAX_RETENTION_DAYS:
        errs.append(f"compliance.retentionDays: over {MAX_RETENTION_DAYS} days")
    if c.get("consentRequired") is not True:
        errs.append("compliance.consentRequired: must be true for HUMAN_CANDIDATE")
    ad = c.get("automatedDecision") or {}
    if not ad.get("disclosureNotice"):
        errs.append("compliance.automatedDecision.disclosureNotice: tell people an automated system scores them")
    if "CA-QC" in c.get("jurisdictions", []):
        if not ad.get("humanReviewRequired"):
            errs.append("compliance.automatedDecision.humanReviewRequired: must be true for CA-QC (Law 25 s.12.1)")
        for field in ("purpose",):
            if not has_lang(c.get(field), "fr"):
                errs.append(f"compliance.{field}: French text required for CA-QC")
        if not has_lang(ad.get("disclosureNotice"), "fr"):
            errs.append("compliance.automatedDecision.disclosureNotice: French text required for CA-QC")
    return errs


def payload_errors(tpl, payload):
    if payload.get("targetType") != HUMAN:
        return []
    c = tpl.get("compliance") or {}
    if not c.get("consentRequired"):
        return []
    consent = payload.get("consent") or {}
    errs = []
    if consent.get("obtained") is not True:
        errs.append("consent.obtained must be true to evaluate a person")
    if not consent.get("obtainedAt"):
        errs.append("consent.obtainedAt is required (ISO 8601 timestamp)")
    return errs


def needs_human_review(tpl, payload):
    return (payload.get("targetType") == HUMAN and
            bool((tpl.get("compliance") or {}).get("automatedDecision", {}).get("humanReviewRequired")))


def retention_days(tpl, payload):
    if payload.get("targetType") != HUMAN:
        return None
    return (tpl.get("compliance") or {}).get("retentionDays")


def disclosure(tpl, payload):
    if payload.get("targetType") != HUMAN:
        return None
    return (tpl.get("compliance") or {}).get("automatedDecision", {}).get("disclosureNotice")
