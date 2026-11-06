"""Dependency-free subset of JSON Schema (draft 2020-12).

Supported keywords: type, enum, anyOf, required, properties,
additionalProperties (false only), items, minItems, minimum, maximum,
minLength, pattern.

Swap for the `jsonschema` package once the project takes dependencies;
the call signature `schema_errors(instance, schema) -> list[str]` is the
only contract the rest of the code relies on.
"""
import re

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}


def _type_ok(inst, t):
    if isinstance(t, list):
        return any(_type_ok(inst, x) for x in t)
    if t in ("integer", "number") and isinstance(inst, bool):
        return False
    if t == "integer" and isinstance(inst, float):
        return inst.is_integer()
    return isinstance(inst, _TYPES[t])


def schema_errors(inst, schema, path="$"):
    errs = []
    t = schema.get("type")
    if t and not _type_ok(inst, t):
        return [f"{path}: expected {t}, got {type(inst).__name__}"]
    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{path}: {inst!r} not in {schema['enum']}")
    if "anyOf" in schema:
        branches = [schema_errors(inst, s, path) for s in schema["anyOf"]]
        if all(branches):
            errs.append(f"{path}: matches none of the allowed forms ({branches[0][0]})")
    if isinstance(inst, dict):
        for k in schema.get("required", []):
            if k not in inst:
                errs.append(f"{path}: missing required '{k}'")
        props = schema.get("properties", {})
        for k, v in inst.items():
            if k in props:
                errs += schema_errors(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected property '{k}'")
    if isinstance(inst, list):
        if len(inst) < schema.get("minItems", 0):
            errs.append(f"{path}: needs at least {schema['minItems']} items")
        if "items" in schema:
            for i, v in enumerate(inst):
                errs += schema_errors(v, schema["items"], f"{path}[{i}]")
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            errs.append(f"{path}: {inst} < minimum {schema['minimum']}")
        if "maximum" in schema and inst > schema["maximum"]:
            errs.append(f"{path}: {inst} > maximum {schema['maximum']}")
    if isinstance(inst, str):
        if len(inst) < schema.get("minLength", 0):
            errs.append(f"{path}: shorter than {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            errs.append(f"{path}: {inst!r} does not match /{schema['pattern']}/")
    return errs
