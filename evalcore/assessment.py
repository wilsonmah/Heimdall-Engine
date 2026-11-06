"""Question-driven diagnostics: skip logic, points scoring, risk, tags, sales hooks.

Used by templates that have an `assessment` section (scoring mode POINTS).
Script-scored templates keep using pipeline.evaluate().

    next_question(tpl, answers, lang)      -> what to ask next
    score_submission(tpl, answers, lead)   -> full result (internal)
    public_view(...) / agency_view(...)    -> the two payloads

Scores run 0-100 where higher is better (lower risk). Risk levels come from
rules.riskLevels thresholds, then rules.statusOverrides may escalate the
overall level for specific answer combinations.
"""
import re

from . import conditions, i18n

QUESTION_TYPES = ("BOOLEAN", "SINGLE_CHOICE", "MULTI_CHOICE", "NUMBER", "TEXT")
RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
RISK_LABELS = {
    "LOW": {"en-CA": "Low risk", "fr-CA": "Risque faible"},
    "MEDIUM": {"en-CA": "Moderate risk", "fr-CA": "Risque modéré"},
    "HIGH": {"en-CA": "High risk", "fr-CA": "Risque élevé"},
}
_QID = re.compile(r"^[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------- questions
def questions(tpl):
    return tpl["assessment"]["questions"]


def _option(q, value):
    for o in q.get("options", []):
        if o["value"] == value:
            return o
    return None


def max_points(q):
    if not q.get("dimension"):
        return 0
    pts = [o.get("points", 0) for o in q.get("options", [])]
    if not pts:
        return 0
    if q["type"] == "MULTI_CHOICE":
        return q.get("maxPoints", sum(p for p in pts if p > 0))
    return max(pts)


def points_for(q, value):
    if not q.get("dimension") or value is None:
        return 0
    if q["type"] == "MULTI_CHOICE":
        total = sum(_option(q, v).get("points", 0) for v in value)
        return max(0, min(total, max_points(q)))
    return _option(q, value).get("points", 0)


def visible_questions(tpl, answers):
    """Questions that apply given the answers so far. A question's showIf may only
    read earlier questions (validated), so one pass in order is enough. Answers
    to hidden questions are ignored."""
    known, shown = {}, []
    for q in questions(tpl):
        cond = q.get("showIf")
        if cond and not conditions.matches(cond, {"answers": known}):
            continue
        shown.append(q)
        if q["id"] in answers:
            known[q["id"]] = answers[q["id"]]
    return shown


def answer_error(q, v):
    t = q["type"]
    if t == "BOOLEAN":
        return None if isinstance(v, bool) else "must be true or false"
    if t == "SINGLE_CHOICE":
        return None if _option(q, v) else f"must be one of {[o['value'] for o in q['options']]}"
    if t == "MULTI_CHOICE":
        if not isinstance(v, list) or len(set(map(str, v))) != len(v):
            return "must be a list of distinct option values"
        bad = [x for x in v if not _option(q, x)]
        return f"unknown options {bad}" if bad else None
    if t == "NUMBER":
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return "must be a number"
        if "min" in q and v < q["min"] or "max" in q and v > q["max"]:
            return f"must be between {q.get('min')} and {q.get('max')}"
        return None
    if t == "TEXT":
        return None if isinstance(v, str) and len(v) <= q.get("maxLength", 500) else "must be a short string"
    return f"unsupported type {t}"


def answers_errors(tpl, answers, require_complete):
    if not isinstance(answers, dict):
        return ["answers must be an object"]
    errs = []
    for q in visible_questions(tpl, answers):
        if q["id"] in answers:
            e = answer_error(q, answers[q["id"]])
            if e:
                errs.append(f"answers.{q['id']}: {e}")
        elif require_complete and q.get("required", True):
            errs.append(f"answers.{q['id']}: required")
    known = {q["id"] for q in questions(tpl)}
    errs += [f"answers.{k}: unknown question" for k in answers if k not in known]
    return errs


def localize_question(q, lang):
    out = {"id": q["id"], "type": q["type"], "text": i18n.text(q["text"], lang),
           "required": q.get("required", True)}
    if q.get("help"):
        out["help"] = i18n.text(q["help"], lang)
    if q.get("options"):
        out["options"] = [{"value": o["value"], "label": i18n.text(o["label"], lang)} for o in q["options"]]
    for k in ("min", "max", "unit"):
        if k in q:
            out[k] = i18n.text(q[k], lang) if k == "unit" else q[k]
    return out


def next_question(tpl, answers, lang):
    """Serves the first visible unanswered question. Progress is computed on the
    questions that apply to this respondent, so skipped branches never count."""
    shown = visible_questions(tpl, answers)
    pending = [q for q in shown if q["id"] not in answers]
    answered = len(shown) - len(pending)
    secs = tpl["assessment"].get("secondsPerQuestion", 12)
    return {
        "done": not pending,
        "question": localize_question(pending[0], lang) if pending else None,
        "progress": {"answered": answered, "total": len(shown),
                     "percent": round(100 * answered / len(shown)) if shown else 100,
                     "estimatedSecondsLeft": len(pending) * secs},
    }


# ------------------------------------------------------------------ scoring
def risk_for(tpl, score):
    lv = tpl.get("rules", {}).get("riskLevels", {"high": 50, "medium": 75})
    if score is None:
        return None
    if score < lv["high"]:
        return "HIGH"
    if score < lv["medium"]:
        return "MEDIUM"
    return "LOW"


def score_submission(tpl, answers, lead=None, lang=i18n.DEFAULT):
    """Returns the full internal result: dimensions with itemized deductions,
    overall score/risk, tags and sales hooks."""
    shown = visible_questions(tpl, answers)
    shown_ids = {q["id"] for q in shown}
    clean = {k: v for k, v in answers.items() if k in shown_ids}
    dims = {d["code"]: {"code": d["code"], "def": d, "earned": 0, "possible": 0, "deductions": []}
            for d in tpl["evaluationMatrix"]["dimensions"]}
    items = []
    for q in shown:
        v = clean.get(q["id"])
        possible = max_points(q)
        earned = points_for(q, v) if v is not None else 0
        item = {"questionId": q["id"], "dimension": q.get("dimension"), "answer": v,
                "pointsEarned": earned, "pointsPossible": possible}
        items.append(item)
        if not q.get("dimension"):
            continue
        d = dims[q["dimension"]]
        d["earned"] += earned
        d["possible"] += possible
        if earned < possible:
            d["deductions"].append({"questionId": q["id"], "answer": v, "pointsDeducted": possible - earned,
                                    "reason": "unanswered" if v is None else "answer"})
    scores, risks = {}, {}
    for code, d in dims.items():
        d["score"] = round(100 * d["earned"] / d["possible"], 1) if d["possible"] else None
        d["risk"] = risk_for(tpl, d["score"])
        for ded in d["deductions"]:
            ded["impactOnDimension"] = round(100 * ded["pointsDeducted"] / d["possible"], 1)
        scores[code], risks[code] = d["score"], d["risk"]

    # weights renormalized over dimensions that had applicable questions
    scored = [d for d in dims.values() if d["score"] is not None]
    wsum = sum(d["def"]["weight"] for d in scored)
    overall = round(sum(d["score"] * d["def"]["weight"] for d in scored) / wsum, 1) if wsum else 0.0
    overall_risk = risk_for(tpl, overall)
    override = None
    ctx = {"answers": clean, "score": {**scores, "overall": overall},
           "risk": {**risks, "overall": overall_risk}, "lead": lead or {}}
    for rule in tpl.get("rules", {}).get("statusOverrides", []):
        if conditions.matches(rule["when"], ctx) and RISK_ORDER[rule["risk"]] > RISK_ORDER[overall_risk]:
            overall_risk, override = rule["risk"], rule.get("id")
            ctx["risk"]["overall"] = overall_risk

    rules = tpl.get("rules", {})
    tags = [t["tag"] for t in rules.get("tags", []) if conditions.matches(t["when"], ctx)]
    ctx["tags"] = tags
    hooks = sorted((h for h in rules.get("salesHooks", []) if conditions.matches(h["when"], ctx)),
                   key=lambda h: h.get("priority", 99))
    return {"answers": clean, "items": items, "skipped": [q["id"] for q in questions(tpl) if q["id"] not in shown_ids],
            "dimensions": list(dims.values()), "overall": overall, "risk": overall_risk,
            "riskOverride": override, "tags": tags, "hooks": hooks, "ctx": ctx}


# ----------------------------------------------------------------- payloads
def _summary(tpl, result, lang):
    s = tpl.get("rules", {}).get("summaries", {}).get(result["risk"])
    return i18n.text(s, lang) if s else None


def public_view(tpl, result, submission_id, lang):
    """Client-facing: lightweight, renders instantly, reveals nothing about
    deductions, tags or sales intent."""
    dims = []
    for d in result["dimensions"]:
        if d["score"] is None:
            continue
        fb = (d["def"].get("feedback") or {}).get(d["risk"])
        dims.append({"code": d["code"], "name": i18n.text(d["def"]["name"], lang),
                     "score": round(d["score"]), "risk": d["risk"],
                     "riskLabel": i18n.text(RISK_LABELS[d["risk"]], lang),
                     "feedback": i18n.text(fb, lang) if fb else None})
    view = tpl.get("outputConfiguration", {}).get("visualization", {}).get("defaultView", "RADAR_CHART")
    return {
        "submissionId": submission_id,
        "assessment": {"id": tpl["evaluationTemplateId"], "name": i18n.text(tpl["name"], lang)},
        "locale": lang,
        "score": round(result["overall"]),
        "risk": result["risk"],
        "riskLabel": i18n.text(RISK_LABELS[result["risk"]], lang),
        "summary": _summary(tpl, result, lang),
        "dimensions": dims,
        "chart": {"type": view, "labels": [d["name"] for d in dims], "values": [d["score"] for d in dims]},
    }


def agency_view(tpl, tpl_meta, result, submission_id, occurred_at, lead, consent, source, respondent_lang):
    """Agency-facing: everything a sales team needs, in the agency's locale."""
    lang = tpl.get("rules", {}).get("agencyLocale", i18n.DEFAULT)
    qs = {q["id"]: q for q in questions(tpl)}
    dims = []
    for d in result["dimensions"]:
        dims.append({
            "code": d["code"], "name": i18n.text(d["def"]["name"], lang), "weight": d["def"]["weight"],
            "score": d["score"], "risk": d["risk"],
            "pointsEarned": d["earned"], "pointsPossible": d["possible"],
            "pointsDeducted": d["possible"] - d["earned"],
            "deductions": [{**x, "question": i18n.text(qs[x["questionId"]]["text"], lang),
                            "answerLabel": _answer_label(qs[x["questionId"]], x["answer"], lang)}
                           for x in d["deductions"]],
        })
    answers = [{**it, "question": i18n.text(qs[it["questionId"]]["text"], lang),
                "answerLabel": _answer_label(qs[it["questionId"]], it["answer"], lang)}
               for it in result["items"]]
    return {
        "event": "assessment.completed",
        "schemaVersion": "1",
        "submissionId": submission_id,
        "occurredAt": occurred_at,
        "assessment": {"id": tpl["evaluationTemplateId"], "name": i18n.text(tpl["name"], lang), **tpl_meta},
        "respondentLocale": respondent_lang,
        "lead": lead,
        "consent": consent,
        "result": {"score": result["overall"], "risk": result["risk"], "riskOverride": result["riskOverride"],
                   "dimensions": dims},
        "answers": answers,
        "skippedQuestions": result["skipped"],
        "tags": result["tags"],
        "salesHooks": [{"id": h["id"], "priority": h.get("priority", 99), "service": h.get("service"),
                        "text": i18n.text(h["text"], lang)} for h in result["hooks"]],
        "source": source or {},
    }


def _answer_label(q, v, lang):
    if v is None:
        return None
    if q["type"] == "MULTI_CHOICE":
        return [i18n.text(_option(q, x)["label"], lang) for x in v]
    o = _option(q, v)
    return i18n.text(o["label"], lang) if o else v


# --------------------------------------------------------------- validation
def template_errors(tpl):
    a = tpl["assessment"]
    errs = []
    dim_codes = {d["code"] for d in tpl["evaluationMatrix"]["dimensions"]}
    seen, used_dims = {}, set()
    locales = a.get("locales", ["en-CA"])

    def need_locales(value, where):
        for loc in locales:
            if not i18n.has_lang(value, loc) and not (isinstance(value, str) and loc == i18n.DEFAULT):
                errs.append(f"{where}: missing {loc} text")

    for i, q in enumerate(a.get("questions", [])):
        where = f"assessment.questions[{i}]"
        qid = q.get("id", "")
        if not _QID.match(qid):
            errs.append(f"{where}.id: must match {_QID.pattern}")
        if qid in seen:
            errs.append(f"{where}.id: duplicate '{qid}'")
        if q.get("type") not in QUESTION_TYPES:
            errs.append(f"{where}.type: must be one of {QUESTION_TYPES}")
            continue
        need_locales(q.get("text"), f"{where}.text")
        if q["type"] in ("SINGLE_CHOICE", "MULTI_CHOICE", "BOOLEAN"):
            opts = q.get("options") or []
            if not opts:
                errs.append(f"{where}: {q['type']} needs options")
            vals = [o.get("value") for o in opts]
            if len(set(map(repr, vals))) != len(vals):
                errs.append(f"{where}.options: duplicate values")
            if q["type"] == "BOOLEAN" and sorted(map(repr, vals)) != ["False", "True"]:
                errs.append(f"{where}.options: BOOLEAN needs exactly true and false")
            for j, o in enumerate(opts):
                need_locales(o.get("label"), f"{where}.options[{j}].label")
        dim = q.get("dimension")
        if dim:
            if dim not in dim_codes:
                errs.append(f"{where}.dimension: unknown {dim}")
            elif q["type"] in ("NUMBER", "TEXT"):
                errs.append(f"{where}: {q['type']} questions cannot be scored; drop 'dimension'")
            elif max_points(q) <= 0:
                errs.append(f"{where}: scored question has no positive points")
            else:
                used_dims.add(dim)
        if "showIf" in q:
            errs += conditions.shape_errors(q["showIf"], f"{where}.showIf")
            for f in conditions.fields(q["showIf"]):
                ns, name = conditions.split_path(f)
                if ns != "answers" or name not in seen:
                    errs.append(f"{where}.showIf: '{f}' must reference an earlier question")
        seen[qid] = q
    for code in dim_codes - used_dims:
        errs.append(f"evaluationMatrix: dimension {code} has no scored questions")

    rules = tpl.get("rules", {})
    lv = rules.get("riskLevels")
    if lv and not lv.get("high", 0) < lv.get("medium", 0):
        errs.append("rules.riskLevels: high must be below medium")

    def check_cond(cond, where):
        errs.extend(conditions.shape_errors(cond, where))
        for f in conditions.fields(cond) if isinstance(cond, dict) else []:
            ns, name = conditions.split_path(f)
            if ns == "answers" and name not in seen:
                errs.append(f"{where}: unknown question '{name}'")
            elif ns in ("score", "risk") and name not in dim_codes | {"overall"}:
                errs.append(f"{where}: unknown dimension '{name}'")
            elif ns not in ("answers", "score", "risk", "lead", "tags"):
                errs.append(f"{where}: field '{f}' is not available to rules")

    for key in ("tags", "salesHooks", "statusOverrides"):
        ids = []
        for i, r in enumerate(rules.get(key, [])):
            check_cond(r.get("when"), f"rules.{key}[{i}].when")
            ids.append(r.get("tag") or r.get("id"))
            if key == "salesHooks" and not r.get("text"):
                errs.append(f"rules.salesHooks[{i}]: 'text' is required")
            if key == "statusOverrides" and r.get("risk") not in RISK_ORDER:
                errs.append(f"rules.statusOverrides[{i}].risk: must be LOW, MEDIUM or HIGH")
        if None in ids:
            errs.append(f"rules.{key}: every entry needs {'tag' if key == 'tags' else 'id'}")
        elif len(set(ids)) != len(ids):
            errs.append(f"rules.{key}: duplicate ids")
    for level, s in rules.get("summaries", {}).items():
        need_locales(s, f"rules.summaries.{level}")
    for d in tpl["evaluationMatrix"]["dimensions"]:
        need_locales(d["name"], f"evaluationMatrix.{d['code']}.name")
    return errs
