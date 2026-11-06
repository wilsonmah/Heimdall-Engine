"""The evaluation pipeline: template -> payload -> sandbox -> output.

    result, failed_stage = evaluate(template, payload, trace)

Payload shape:
    {
      "targetType": "AI_AGENT" | "HUMAN_CANDIDATE" | "VENDOR_ENTITY",
      "<primaryKey>": "...",                 # e.g. entity_id
      "consent": {"obtained": true, "obtainedAt": "..."},   # people only
      "channels": {"<channelId>": {field: value, ...}, ...}
    }
"""
from . import compliance
from .jsonschema_lite import schema_errors
from .sandbox import execute
from .validation import validate_template


def validate_payload(tpl, payload, trace):
    ok = True
    allowed = tpl["targetSchema"]["allowedTypes"]
    if payload.get("targetType") not in allowed:
        trace.log("payload", "ERROR", f"targetType {payload.get('targetType')!r} not in {allowed}")
        return False, {}
    pk = tpl["targetSchema"]["identifierMapping"]["primaryKey"]
    if not payload.get(pk):
        trace.log("payload", "ERROR", f"missing primary key '{pk}'")
        ok = False
    for e in compliance.payload_errors(tpl, payload):
        trace.log("consent", "ERROR", e)
        ok = False

    merged = {}
    chans = {c["channelId"]: c for c in tpl["dataIngestion"]["channels"]}
    for cid, data in (payload.get("channels") or {}).items():
        c = chans.get(cid)
        if not c:
            trace.log("payload", "ERROR", f"unknown channel {cid}")
            ok = False
            continue
        if not c["enabled"]:
            trace.log("payload", "WARN", f"channel {cid} disabled - ignored")
            continue
        if not isinstance(data, dict):
            trace.log("payload", "ERROR", f"{cid}: channel data must be an object")
            ok = False
            continue
        errs = schema_errors(data, c["validationSchema"], cid) if "validationSchema" in c else []
        for f in c.get("fields", []):
            v = data.get(f["fieldCode"])
            if v is None or f["type"] != "NUMBER_SLIDER":
                continue
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                errs.append(f"{cid}.{f['fieldCode']}: must be a number")
            elif not (f.get("min", v) <= v <= f.get("max", v)):
                errs.append(f"{cid}.{f['fieldCode']}: {v} outside [{f.get('min')}, {f.get('max')}]")
        for e in errs:
            trace.log("payload", "ERROR", e)
        if errs:
            ok = False
            continue
        clash = set(merged) & set(data)
        if clash:
            trace.log("payload", "WARN", f"{cid} overwrites keys from another channel: {sorted(clash)}")
        merged.update(data)
        trace.log("payload", "OK", f"{cid}: {len(data)} field(s)")
    return ok, merged


def band_for(tpl, score):
    th = tpl.get("outputConfiguration", {}).get("visualization", {}).get("thresholds", {})
    if score < th.get("critical", float("-inf")):
        return "CRITICAL"
    if score >= th.get("excellent", float("inf")):
        return "EXCELLENT"
    return "NORMAL"


def check_output(tpl, result, trace):
    codes = {d["code"] for d in tpl["evaluationMatrix"]["dimensions"]}
    errs = []
    if not isinstance(result, dict):
        errs.append("result must be an object")
    else:
        cs = result.get("compositeScore")
        if not isinstance(cs, (int, float)) or isinstance(cs, bool):
            errs.append("compositeScore missing or not numeric")
        elif not 0 <= cs <= 100:
            errs.append(f"compositeScore {cs} outside 0-100")
        bd = result.get("breakdown")
        if not isinstance(bd, dict):
            errs.append("breakdown missing or not an object")
        else:
            if codes - set(bd):
                errs.append(f"breakdown missing dimensions {sorted(codes - set(bd))}")
            if set(bd) - codes:
                errs.append(f"breakdown has unknown dimensions {sorted(set(bd) - codes)}")
            for k, v in bd.items():
                if isinstance(v, (int, float)) and not 0 <= v <= 100:
                    trace.log("output", "WARN", f"{k}={v} outside 0-100")
    for e in errs:
        trace.log("output", "ERROR", e)
    if errs:
        return None
    band = band_for(tpl, result["compositeScore"])
    trace.log("output", "OK", f"compositeScore={result['compositeScore']} band={band}")
    return {"compositeScore": result["compositeScore"], "breakdown": result["breakdown"], "band": band}


def evaluate(tpl, payload, trace):
    """Returns (result_or_None, failed_stage_or_None)."""
    if not validate_template(tpl, trace):
        return None, "template"
    ok, data = validate_payload(tpl, payload, trace)
    if not ok:
        return None, "payload"
    raw = execute(tpl, data, trace)
    if raw is None:
        return None, "sandbox"
    final = check_output(tpl, raw, trace)
    if final is None:
        return None, "output"
    return final, None
