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
import re

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


# ------------------------------------------------------------ B2B leads
# Business contacts are still personal information when used for marketing
# (the PIPEDA business-contact exemption covers communication about someone's
# job, not prospecting). Emailing a lead also needs CASL consent, which must be
# separate from accepting the privacy notice and never pre-checked.
LEAD = "LEAD"
LEAD_REQUIRED = ("first_name", "last_name", "corporate_email", "company_name")
PROVINCES = ("AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT")
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.ca", "hotmail.com", "hotmail.ca", "outlook.com",
    "live.com", "live.ca", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "shaw.ca", "rogers.com", "sympatico.ca", "telus.net", "videotron.ca", "bell.net",
}
_EMAIL = re.compile(r"^[^@\s]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")
_PHONE = re.compile(r"^\+?[0-9 ().-]{7,20}$")


def lead_template_errors(tpl):
    if LEAD not in tpl.get("targetSchema", {}).get("allowedTypes", []):
        return []
    c = tpl.get("compliance")
    if not c:
        return ["compliance: required when allowedTypes includes LEAD"]
    locales = (tpl.get("assessment") or {}).get("locales", ["en-CA"])
    errs = []
    for key, why in (("privacyNotice", "explain what is collected and why (PIPEDA 4.2/4.3)"),
                     ("sharingDisclosure", "say that answers go to the provider's sales team / CRM")):
        if not c.get(key):
            errs.append(f"compliance.{key}: required - {why}")
        else:
            errs += [f"compliance.{key}: missing {loc} text" for loc in locales if not has_lang(c[key], loc)]
    if not c.get("retentionDays"):
        errs.append("compliance.retentionDays: required for lead data (PIPEDA 4.5)")
    elif c["retentionDays"] > MAX_RETENTION_DAYS:
        errs.append(f"compliance.retentionDays: over {MAX_RETENTION_DAYS} days")
    mc = c.get("marketingConsent")
    if mc is not None:
        if not mc.get("text"):
            errs.append("compliance.marketingConsent.text: the exact CASL consent wording shown to the person")
        else:
            errs += [f"compliance.marketingConsent.text: missing {loc} text"
                     for loc in locales if not has_lang(mc["text"], loc)]
        if not mc.get("senderName"):
            errs.append("compliance.marketingConsent.senderName: CASL requires identifying who is asking")
    return errs


def email_domain(email):
    return email.rsplit("@", 1)[-1].lower() if isinstance(email, str) and "@" in email else None


def lead_errors(tpl, lead, consent):
    errs = []
    if not isinstance(lead, dict):
        return ["lead must be an object"]
    for k in LEAD_REQUIRED:
        if not isinstance(lead.get(k), str) or not lead[k].strip():
            errs.append(f"lead.{k}: required")
    for k, v in lead.items():
        if not isinstance(v, str) or len(v) > 200:
            errs.append(f"lead.{k}: must be a string of at most 200 characters")
    email = lead.get("corporate_email")
    if isinstance(email, str) and email:
        if not _EMAIL.match(email):
            errs.append("lead.corporate_email: not a valid email address")
        elif (tpl.get("assessment") or {}).get("requireCorporateEmail") and email_domain(email) in FREE_EMAIL_DOMAINS:
            errs.append("lead.corporate_email: please use a work email address")
    if lead.get("phone") and not _PHONE.match(lead["phone"]):
        errs.append("lead.phone: not a valid phone number")
    if lead.get("province") and lead["province"] not in PROVINCES:
        errs.append(f"lead.province: must be one of {list(PROVINCES)}")
    consent = consent if isinstance(consent, dict) else {}
    if consent.get("privacyNoticeAcknowledged") is not True:
        errs.append("consent.privacyNoticeAcknowledged: must be true")
    if "marketingOptIn" in consent and not isinstance(consent["marketingOptIn"], bool):
        errs.append("consent.marketingOptIn: must be true or false")
    return errs


def clean_lead(lead):
    allowed = LEAD_REQUIRED + ("phone", "province", "job_title")
    out = {k: lead[k].strip() for k in allowed if isinstance(lead.get(k), str) and lead[k].strip()}
    out["corporate_email"] = out["corporate_email"].lower()
    out["email_domain"] = email_domain(out["corporate_email"])
    return out


def consent_record(tpl, tpl_semver, consent, at, lang):
    """What was shown and agreed to, kept with the lead (CASL record-keeping)."""
    from .i18n import text
    c = tpl.get("compliance") or {}
    mc = c.get("marketingConsent")
    granted = bool(mc) and consent.get("marketingOptIn") is True
    return {
        "privacyNotice": {"acknowledged": True, "templateVersion": tpl_semver, "locale": lang, "at": at},
        "marketing": {
            "granted": granted,
            "type": "express" if granted else None,
            "text": text(mc["text"], lang) if mc else None,
            "senderName": mc.get("senderName") if mc else None,
            "at": at if granted else None,
            "method": "web-form-unchecked-checkbox" if granted else None,
        },
    }


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
