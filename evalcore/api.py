"""Headless REST API (stdlib http.server).

Zero-deploy: every request resolves the template's *active version* from the
store, so a saved change takes effect on the next call with no restart.

Auth: when an API key is configured, every /v1 call needs  X-API-Key: <key>.
Actor: X-Actor names who made a change (recorded in the audit log). In
production this comes from the SSO identity (Microsoft Entra ID / Google).
Language: Accept-Language or ?lang=en|fr selects localized template text.

See docs/API.md for the endpoint reference.
"""
import hmac
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import compliance, i18n
from .pipeline import band_for, evaluate
from .store import Store, ValidationFailed, now
from .trace import Trace

MAX_BODY = 1_000_000


class ApiError(Exception):
    def __init__(self, status, msg, details=None):
        super().__init__(msg)
        self.status, self.msg, self.details = status, msg, details


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = "evalcore/0.1"

    @property
    def store(self) -> Store:
        return self.server.store

    @property
    def actor(self):
        return self.headers.get("X-Actor") or self.headers.get("X-Author")

    def _dispatch(self, method):
        url = urlparse(self.path)
        self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        self.lang = i18n.pick_lang(self.headers.get("Accept-Language"), self.query.get("lang"))
        try:
            key = self.server.api_key
            if url.path.startswith("/v1/") and key and not hmac.compare_digest(
                    self.headers.get("X-API-Key", ""), key):
                raise ApiError(401, "missing or invalid X-API-Key")
            matched = False
            for m, rx, fn in ROUTES:
                mt = rx.match(url.path)
                if not mt:
                    continue
                matched = True
                if m == method:
                    status, body = fn(self, *(unquote(g) for g in mt.groups()))
                    return self._send(status, body)
            raise ApiError(405 if matched else 404, "method not allowed" if matched else "not found")
        except ApiError as e:
            self._send(e.status, {"error": e.msg, **({"details": e.details} if e.details else {})})
        except ValidationFailed as e:
            self._send(422, {"error": "template validation failed", "details": e.errors})
        except Exception as e:  # never leak internals to callers
            self.log_error("unhandled: %r", e)
            self._send(500, {"error": "internal error"})

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(413, "body too large")
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError as e:
            raise ApiError(400, f"invalid JSON: {e}")
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Language", getattr(self, "lang", "en"))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        if not self.server.quiet:
            super().log_message(fmt, *args)

    def do_GET(self): self._dispatch("GET")
    def do_PUT(self): self._dispatch("PUT")
    def do_POST(self): self._dispatch("POST")
    def do_PATCH(self): self._dispatch("PATCH")
    def do_DELETE(self): self._dispatch("DELETE")


# --------------------------------------------------------------- helpers
def int_param(v, name):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} must be an integer")


def require_template(h, tid, version=None):
    row = h.store.get_template(tid, version)
    if not row:
        raise ApiError(404, f"template {tid}" + (f" version {version}" if version else "") + " not found")
    return row


def version_meta(row, lang=None):
    out = {"templateId": row["template_id"], "versionNo": row["version_no"], "semver": row["semver"],
           "author": row["author"], "note": row["note"], "createdAt": row["created_at"]}
    if lang:
        out["name"] = i18n.text(json.loads(row["body"])["name"], lang)
    return out


def eval_view(r, lang, full=False):
    result = json.loads(r["result"])
    review = json.loads(r["review"]) if r["review"] else None
    if r["status"] == "SUCCEEDED":
        final = {"score": result["compositeScore"], "band": result["band"], "decidedBy": "system"}
    elif r["status"] == "REVIEWED":
        final = {"score": review["finalScore"], "band": review["finalBand"], "decidedBy": "human"}
    else:
        final = None
    out = {"id": r["id"], "status": r["status"], "templateId": r["template_id"], "versionNo": r["version_no"],
           "semver": r["semver"], "entityId": r["entity_id"], "targetType": r["target_type"],
           "failedStage": r["failed_stage"], "result": result, "review": review, "final": final,
           "createdAt": r["created_at"], "expiresAt": r["expires_at"]}
    if full:
        out["payload"] = json.loads(r["payload"])
        out["trace"] = json.loads(r["trace"])
    return out


# ------------------------------------------------------------- endpoints
@route("GET", r"/health")
def health(h):
    return 200, {"status": "ok"}


@route("GET", r"/v1/templates")
def list_templates(h):
    return 200, {"items": [version_meta(r, h.lang) for r in h.store.list_templates()]}


@route("GET", r"/v1/templates/([\w-]+)")
def read_template(h, tid):
    row = require_template(h, tid, int_param(h.query.get("version"), "version"))
    return 200, {**version_meta(row, h.lang), "template": json.loads(row["body"])}


@route("GET", r"/v1/templates/([\w-]+)/versions")
def list_versions(h, tid):
    require_template(h, tid)
    return 200, {"activeVersionNo": h.store.active_version_no(tid),
                 "items": [version_meta(r) for r in h.store.list_versions(tid)]}


@route("PUT", r"/v1/templates/([\w-]+)")
def put_template(h, tid):
    row = h.store.save_version(tid, h.body(), h.actor, h.query.get("note"))
    return 200, {**version_meta(row), "activated": True}


@route("PATCH", r"/v1/templates/([\w-]+)/weights")
def patch_weights(h, tid):
    """Business-side change: adjust weights only, everything else untouched."""
    changes = h.body()
    if not changes or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in changes.values()):
        raise ApiError(400, 'expected {"DIM_CODE": number, ...}')
    tpl = json.loads(require_template(h, tid)["body"])
    dims = {d["code"]: d for d in tpl["evaluationMatrix"]["dimensions"]}
    unknown = sorted(set(changes) - set(dims))
    if unknown:
        raise ApiError(422, "unknown dimension codes", unknown)
    diff = []
    for code, w in changes.items():
        diff.append(f"{code} {dims[code]['weight']}->{w}")
        dims[code]["weight"] = w
    row = h.store.save_version(tid, tpl, h.actor, "weights: " + ", ".join(diff))
    return 200, {**version_meta(row), "activated": True}


@route("POST", r"/v1/templates/([\w-]+)/rollback")
def rollback(h, tid):
    n = int_param(h.body().get("version"), "version")
    if n is None:
        raise ApiError(400, '"version" is required')
    require_template(h, tid)
    row = h.store.activate(tid, n, h.actor)
    if not row:
        raise ApiError(404, f"template {tid} version {n} not found")
    return 200, {**version_meta(row), "activated": True}


@route("POST", r"/v1/templates/([\w-]+)/dry-run")
def dry_run(h, tid):
    """Algorithm sandbox: test a payload against the active version or an
    unsaved draft. Nothing is stored; production traffic is unaffected."""
    req = h.body()
    if not isinstance(req.get("payload"), dict):
        raise ApiError(400, 'object "payload" is required')
    if req.get("draft"):
        tpl, source = req["draft"], "draft"
    else:
        tpl, source = json.loads(require_template(h, tid)["body"]), "active"
    trace = Trace()
    result, failed = evaluate(tpl, req["payload"], trace)
    return 200, {"source": source, "status": "FAILED" if failed else "SUCCEEDED",
                 "failedStage": failed, "result": result, "trace": trace.events}


@route("POST", r"/v1/evaluations")
def create_evaluation(h):
    req = h.body()
    tid, payload = req.get("templateId"), req.get("payload")
    if not tid or not isinstance(payload, dict):
        raise ApiError(400, '"templateId" and object "payload" are required')
    row = require_template(h, tid, int_param(req.get("version"), "version"))
    tpl = json.loads(row["body"])
    trace = Trace()
    result, failed = evaluate(tpl, payload, trace)
    if failed:
        status = "FAILED"
    elif compliance.needs_human_review(tpl, payload):
        status = "PENDING_REVIEW"
    else:
        status = "SUCCEEDED"
    pk = tpl["targetSchema"]["identifierMapping"]["primaryKey"]
    stored = h.store.insert_evaluation(row, payload, payload.get(pk), status, failed, result, trace.events,
                                       compliance.retention_days(tpl, payload))
    out = eval_view(stored, h.lang)
    notice = compliance.disclosure(tpl, payload)
    if notice:
        out["disclosureNotice"] = i18n.text(notice, h.lang)
    if failed:
        out["errors"] = trace.errors()
        return 422, out
    return 201, out


@route("GET", r"/v1/evaluations")
def list_evaluations(h):
    limit = min(int_param(h.query.get("limit"), "limit") or 50, 500)
    rows = h.store.list_evaluations(h.query.get("templateId"), h.query.get("entityId"),
                                    h.query.get("status"), limit)
    return 200, {"items": [eval_view(r, h.lang) for r in rows]}


@route("GET", r"/v1/evaluations/(ev_[0-9a-f]+)")
def read_evaluation(h, eid):
    r = h.store.get_evaluation(eid)
    if not r:
        raise ApiError(404, "evaluation not found")
    return 200, eval_view(r, h.lang, full=h.query.get("trace") == "1")


@route("POST", r"/v1/evaluations/(ev_[0-9a-f]+)/review")
def review_evaluation(h, eid):
    """Human review of an automated result (Quebec Law 25 s.12.1)."""
    r = h.store.get_evaluation(eid)
    if not r:
        raise ApiError(404, "evaluation not found")
    if r["status"] != "PENDING_REVIEW":
        raise ApiError(409, f"evaluation is {r['status']}, not PENDING_REVIEW")
    req = h.body()
    reviewer, decision = req.get("reviewer") or h.actor, req.get("decision")
    if not reviewer:
        raise ApiError(400, '"reviewer" is required')
    result = json.loads(r["result"])
    if decision == "CONFIRMED":
        final = result["compositeScore"]
    elif decision == "OVERRIDDEN":
        final = req.get("finalScore")
        if not isinstance(final, (int, float)) or isinstance(final, bool) or not 0 <= final <= 100:
            raise ApiError(400, '"finalScore" 0-100 is required to override')
        if not req.get("reason"):
            raise ApiError(400, '"reason" is required to override')
    else:
        raise ApiError(400, '"decision" must be CONFIRMED or OVERRIDDEN')
    tpl = json.loads(h.store.get_template(r["template_id"], r["version_no"])["body"])
    review = {"reviewer": reviewer, "decision": decision, "finalScore": final,
              "finalBand": band_for(tpl, final), "reason": req.get("reason"), "reviewedAt": now()}
    h.store.record_review(eid, review, "REVIEWED")
    return 200, eval_view(h.store.get_evaluation(eid), h.lang)


@route("GET", r"/v1/subjects/([^/]+)")
def subject_access(h, entity_id):
    """Access request: everything held about one person (PIPEDA Principle 4.9)."""
    rows = h.store.list_evaluations(entity_id=entity_id, limit=10_000)
    return 200, {"entityId": entity_id, "count": len(rows),
                 "evaluations": [eval_view(r, h.lang, full=True) for r in rows]}


@route("DELETE", r"/v1/subjects/([^/]+)")
def subject_erase(h, entity_id):
    n = h.store.erase_subject(entity_id, h.actor)
    return 200, {"evaluationsDeleted": n}


@route("POST", r"/v1/admin/purge-expired")
def purge_expired(h):
    return 200, {"evaluationsDeleted": h.store.purge_expired(h.actor)}


@route("GET", r"/v1/audit")
def audit(h):
    limit = min(int_param(h.query.get("limit"), "limit") or 100, 1000)
    return 200, {"items": [{"seq": r["seq"], "at": r["at"], "actor": r["actor"], "action": r["action"],
                            "subject": r["subject"], "detail": json.loads(r["detail"]) if r["detail"] else None}
                           for r in h.store.list_audit(limit)]}


# ------------------------------------------------------------------ server
def seed_templates(store, directory):
    for f in sorted(Path(directory).glob("*.json")):
        if f.name.startswith("_"):
            continue
        body = json.loads(f.read_text(encoding="utf-8"))
        tid = body.get("evaluationTemplateId")
        if tid and not store.get_template(tid):
            try:
                store.save_version(tid, body, "seed", f"imported from {f.name}")
                print(f"seeded {tid}", file=sys.stderr)
            except ValidationFailed as e:
                print(f"seed {f.name} rejected: {e.errors}", file=sys.stderr)


def create_server(store, host="127.0.0.1", port=8700, api_key=None, quiet=False):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.store, srv.api_key, srv.quiet = store, api_key, quiet
    return srv
