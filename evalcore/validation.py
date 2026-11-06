"""Template validation: structure (JSON Schema) -> semantics -> compliance."""
import json
import re
from functools import lru_cache
from pathlib import Path

from . import compliance
from .jsonschema_lite import schema_errors

SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "evaluation-template.schema.json"


@lru_cache(maxsize=1)
def template_schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def semantic_errors(tpl):
    errs = []
    dims = tpl["evaluationMatrix"]["dimensions"]
    codes = [d["code"] for d in dims]
    if len(set(codes)) != len(codes):
        errs.append("evaluationMatrix: duplicate dimension codes")
    total = sum(d["weight"] for d in dims)
    if abs(total - 1.0) > 1e-6:
        errs.append(f"evaluationMatrix: weights sum to {total:g}, expected 1.0")

    chan_ids = [c["channelId"] for c in tpl["dataIngestion"]["channels"]]
    if len(set(chan_ids)) != len(chan_ids):
        errs.append("dataIngestion: duplicate channelId")
    for c in tpl["dataIngestion"]["channels"]:
        if c["type"] == "DYNAMIC_FORM" and not c.get("fields"):
            errs.append(f"{c['channelId']}: DYNAMIC_FORM needs 'fields'")
        if c["type"] == "WEBHOOK_PAYLOAD" and "validationSchema" not in c:
            errs.append(f"{c['channelId']}: WEBHOOK_PAYLOAD needs 'validationSchema'")
        for f in c.get("fields", []):
            b = f.get("bindToDimension")
            if b and b not in codes:
                errs.append(f"{c['channelId']}.{f['fieldCode']}: binds to unknown dimension {b}")
            if "min" in f and "max" in f and f["min"] > f["max"]:
                errs.append(f"{c['channelId']}.{f['fieldCode']}: min > max")

    th = tpl.get("outputConfiguration", {}).get("visualization", {}).get("thresholds")
    if th and th["critical"] >= th["excellent"]:
        errs.append("thresholds: critical must be below excellent")

    eng = tpl["calculationEngine"]
    entry = eng.get("entrypoint", "calculate_scores")
    try:
        compile(eng["script"], "<template-script>", "exec")
    except SyntaxError as e:
        errs.append(f"calculationEngine.script: SyntaxError line {e.lineno}: {e.msg}")
    else:
        if not re.search(rf"^def {re.escape(entry)}\(", eng["script"], re.M):
            errs.append(f"calculationEngine.script: entrypoint '{entry}' not defined")
    for code in re.findall(r"matrix_config\[['\"](\w+)['\"]\]", eng["script"]):
        if code not in codes:
            errs.append(f"calculationEngine.script: references unknown dimension {code}")
    return errs


def validate_template(tpl, trace):
    for stage_errors in (
        lambda: schema_errors(tpl, template_schema()),
        lambda: semantic_errors(tpl),
        lambda: compliance.template_errors(tpl),
    ):
        errs = stage_errors()
        for e in errs:
            trace.log("template", "ERROR", e)
        if errs:
            return False
    trace.log("template", "OK", f"{tpl['evaluationTemplateId']}@{tpl['version']} is valid")
    return True
