"""Maps the agency payload onto CRM properties, driven by the template.

    "crmMapping": {
      "hubspot": {
        "contact": {"email": "lead.corporate_email", "cyber_risk_score": "score.overall",
                    "cyber_risk_tags": {"path": "tags", "format": "multi"}},
        "company": {"name": "lead.company_name", "domain": "lead.email_domain"},
        "note": true
      }
    }

A value is a field path (see conditions.py namespaces) or {"path", "format"}.
Formats: text (default), number, bool ("true"/"false"), multi (HubSpot
multi-checkbox, ';'-joined), lines (newline-joined), date (YYYY-MM-DD).
Empty values are left out so they never blank an existing CRM field.
"""
from .. import conditions

FORMATS = ("text", "number", "bool", "multi", "lines", "date")
MAPPABLE = ("lead", "score", "risk", "answers", "tags", "hooks", "consent", "submission", "source")


def context(agency):
    r = agency["result"]
    return {
        "lead": agency["lead"],
        "score": {"overall": r["score"], **{d["code"]: d["score"] for d in r["dimensions"]}},
        "risk": {"overall": r["risk"], **{d["code"]: d["risk"] for d in r["dimensions"]}},
        "answers": {a["questionId"]: a["answer"] for a in agency["answers"]},
        "tags": agency["tags"],
        "hooks": [h["text"] for h in agency["salesHooks"]],
        "consent": agency["consent"],
        "submission": {"id": agency["submissionId"], "occurredAt": agency["occurredAt"],
                       "templateId": agency["assessment"]["id"], "semver": agency["assessment"].get("semver"),
                       "locale": agency["respondentLocale"]},
        "source": agency.get("source") or {},
    }


def _format(v, fmt):
    if v is None or v == [] or v == "":
        return None
    if fmt == "bool":
        return "true" if v else "false"
    if fmt == "number":
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if fmt == "multi":
        return ";".join(map(str, v)) if isinstance(v, list) else str(v)
    if fmt == "lines":
        return "\n".join(map(str, v)) if isinstance(v, list) else str(v)
    if fmt == "date":
        return str(v)[:10]
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return ";".join(map(str, v))
    return v


def build_properties(spec, ctx):
    props = {}
    for prop, src in (spec or {}).items():
        path, fmt = (src, "text") if isinstance(src, str) else (src.get("path"), src.get("format", "text"))
        val = _format(conditions.resolve(ctx, path), fmt)
        if val is not None:
            props[prop] = val
    return props


def mapping_errors(tpl):
    errs = []
    qids = {q["id"] for q in (tpl.get("assessment") or {}).get("questions", [])}
    dims = {d["code"] for d in tpl["evaluationMatrix"]["dimensions"]} | {"overall"}
    for system, objects in tpl["crmMapping"].items():
        if system not in ("hubspot", "webhook"):
            errs.append(f"crmMapping.{system}: unsupported (hubspot, webhook)")
            continue
        for obj, spec in objects.items():
            if not isinstance(spec, dict):
                continue
            for prop, src in spec.items():
                where = f"crmMapping.{system}.{obj}.{prop}"
                path = src if isinstance(src, str) else (src or {}).get("path")
                if not isinstance(path, str):
                    errs.append(f"{where}: needs a field path")
                    continue
                if not isinstance(src, str) and src.get("format", "text") not in FORMATS:
                    errs.append(f"{where}: format must be one of {FORMATS}")
                head = path.split(".")[0]
                if head not in MAPPABLE:
                    errs.append(f"{where}: path must start with one of {MAPPABLE}")
                ns, name = conditions.split_path(path)
                if ns == "answers" and name not in qids:
                    errs.append(f"{where}: unknown question '{name}'")
                if ns in ("score", "risk") and name not in dims:
                    errs.append(f"{where}: unknown dimension '{name}'")
    hs = tpl["crmMapping"].get("hubspot", {})
    if hs and "email" not in (hs.get("contact") or {}):
        errs.append("crmMapping.hubspot.contact.email: required (contacts are upserted by email)")
    if hs.get("company") and "domain" not in hs["company"]:
        errs.append("crmMapping.hubspot.company.domain: required (companies are matched by domain)")
    return errs
