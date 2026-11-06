"""One small rule language for skip logic, risk tags, sales hooks and CRM mapping.

A condition is either a leaf or a combinator:

    {"field": "company_size", "op": "lt", "value": 10}
    {"all": [cond, ...]}   {"any": [cond, ...]}   {"not": cond}

Field paths resolve against a context of namespaces:

    answers.<questionId>     (a bare name means answers.<name>)
    score.overall | score.<DIM_CODE>
    risk.overall  | risk.<DIM_CODE>
    lead.<field>
    tags

A missing field makes every comparison false, except `missing` (true) and
`ne` / `nin` (false) - so a skipped question never triggers a hook by accident.
"""
NAMESPACES = ("answers", "score", "risk", "lead", "tags", "hooks", "consent", "submission", "source")
OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "nin", "contains", "exists", "missing", "truthy", "falsy")
_MISSING = object()


def split_path(path):
    head, _, rest = path.partition(".")
    if head in NAMESPACES:
        return head, rest
    return "answers", path


def resolve(ctx, path, default=None):
    ns, rest = split_path(path)
    cur = ctx.get(ns, _MISSING)
    for part in (rest.split(".") if rest else []):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return default if cur is _MISSING else cur


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def matches(cond, ctx):
    if "all" in cond:
        return all(matches(c, ctx) for c in cond["all"])
    if "any" in cond:
        return any(matches(c, ctx) for c in cond["any"])
    if "not" in cond:
        return not matches(cond["not"], ctx)
    v = resolve(ctx, cond["field"], _MISSING)
    op, target = cond["op"], cond.get("value")
    if op == "missing":
        return v is _MISSING or v is None
    if v is _MISSING or v is None:
        return False
    if op == "exists":
        return True
    if op == "truthy":
        return bool(v)
    if op == "falsy":
        return not v
    if op == "eq":
        return v == target
    if op == "ne":
        return v != target
    if op == "in":
        return v in (target or [])
    if op == "nin":
        return v not in (target or [])
    if op == "contains":
        return isinstance(v, (list, str)) and target in v
    if op in ("gt", "gte", "lt", "lte"):
        if not (_num(v) and _num(target)):
            return False
        return {"gt": v > target, "gte": v >= target, "lt": v < target, "lte": v <= target}[op]
    raise ValueError(f"unknown op {op}")


def shape_errors(cond, where):
    """Structural check used by template validation."""
    if not isinstance(cond, dict):
        return [f"{where}: condition must be an object"]
    for k in ("all", "any"):
        if k in cond:
            if not isinstance(cond[k], list) or not cond[k]:
                return [f"{where}.{k}: must be a non-empty list"]
            return [e for i, c in enumerate(cond[k]) for e in shape_errors(c, f"{where}.{k}[{i}]")]
    if "not" in cond:
        return shape_errors(cond["not"], f"{where}.not")
    errs = []
    if not isinstance(cond.get("field"), str):
        errs.append(f"{where}: 'field' is required")
    if cond.get("op") not in OPS:
        errs.append(f"{where}: 'op' must be one of {list(OPS)}")
    return errs


def fields(cond):
    """Every field path a condition reads."""
    if "all" in cond or "any" in cond:
        return [f for c in cond.get("all", cond.get("any", [])) for f in fields(c)]
    if "not" in cond:
        return fields(cond["not"])
    return [cond["field"]] if "field" in cond else []
