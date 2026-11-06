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
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import assessment, compliance, i18n
from .crm import PermanentError, mapping
from .crm.http import check_url
from .pipeline import band_for, evaluate
from .residency import CANADIAN_REGIONS, Policy
from .store import Store, ValidationFailed, now
from .trace import Trace
from .validation import validate_template

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

    @property
    def client_ip(self):
        # behind a proxy, set EVAL_TRUST_PROXY=1 and the proxy's X-Forwarded-For is used
        if self.server.trust_proxy and self.headers.get("X-Forwarded-For"):
            return self.headers["X-Forwarded-For"].split(",")[0].strip()
        return self.client_address[0]

    def _dispatch(self, method):
        url = urlparse(self.path)
        self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        self.lang = i18n.pick_lang(self.headers.get("Accept-Language"), self.query.get("lang"))
        self.cors_origin = None
        try:
            key = self.server.api_key
            if url.path.startswith("/v1/") and key and not hmac.compare_digest(
                    self.headers.get("X-API-Key", ""), key):
                raise ApiError(401, "missing or invalid X-API-Key")
            if url.path.startswith("/public/"):
                self._public_preflight(url.path, method)
                if method == "OPTIONS":
                    return self._send(204, None)
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

    def _public_preflight(self, path, method):
        """CORS for website embeds: the browser Origin must be allowed by a form key
        for this assessment. Server-to-server calls (no Origin) skip the check."""
        origin = self.headers.get("Origin")
        if not origin:
            return
        m = re.match(r"^/public/v1/assessments/([\w-]+)", path)
        allowed = set()
        if m:
            for fk in self.store.list_form_keys():
                if fk["enabled"] and fk["template_id"] == m.group(1):
                    allowed |= set(json.loads(fk["allowed_origins"]))
        if origin not in allowed:
            raise ApiError(403, "origin not allowed for this assessment")
        self.cors_origin = origin

    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode() if obj is not None else b""
        self.send_response(status)
        if obj is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Language", getattr(self, "lang", i18n.DEFAULT))
        if getattr(self, "cors_origin", None):
            self.send_header("Access-Control-Allow-Origin", self.cors_origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Form-Key, Accept-Language")
            self.send_header("Access-Control-Max-Age", "600")
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
    def do_OPTIONS(self): self._dispatch("OPTIONS")


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
    if req.get("draft"):
        tpl, source = req["draft"], "draft"
    else:
        tpl, source = json.loads(require_template(h, tid)["body"]), "active"
    if "assessment" in tpl:
        return 200, {"source": source, **assessment_dry_run(tpl, req, h.lang)}
    if not isinstance(req.get("payload"), dict):
        raise ApiError(400, 'object "payload" is required')
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
    if "assessment" in tpl:
        raise ApiError(422, f"{tid} is a question-based assessment; submit it via "
                            f"POST /public/v1/assessments/{tid}/submit")
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
    """Access request: everything held about one person (PIPEDA Principle 4.9).
    `entity_id` is an evaluation entity id or a lead's email address."""
    rows = h.store.list_evaluations(entity_id=entity_id, limit=10_000)
    subs = h.store.list_submissions(email=entity_id, limit=10_000) if "@" in entity_id else []
    return 200, {"entityId": entity_id, "count": len(rows) + len(subs),
                 "evaluations": [eval_view(r, h.lang, full=True) for r in rows],
                 "submissions": [json.loads(s["agency_payload"]) for s in subs]}


@route("DELETE", r"/v1/subjects/([^/]+)")
def subject_erase(h, entity_id):
    n = h.store.erase_subject(entity_id, h.actor)
    return 200, {"evaluationsDeleted": n, "recordsDeleted": n,
                 "note": "records already delivered to a CRM must also be erased in that CRM"}


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


def create_server(store, host="127.0.0.1", port=8700, api_key=None, quiet=False, dispatcher=None,
                  policy=None, trust_proxy=False, rate_limits=None):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.store, srv.api_key, srv.quiet = store, api_key, quiet
    srv.policy = policy or Policy()
    srv.dispatcher = dispatcher
    srv.trust_proxy = trust_proxy
    srv.limiter = RateLimiter(rate_limits or {"submit": (10, 600), "read": (300, 600)})
    return srv


# ======================================================================
# Lead-gen diagnostics
# ======================================================================
class RateLimiter:
    """Per-IP sliding window, in memory. Replace with Redis when running >1 instance."""

    def __init__(self, limits):
        self.limits, self.hits, self.lock = limits, {}, threading.Lock()

    def check(self, ip, bucket):
        limit, window = self.limits[bucket]
        t = time.monotonic()
        with self.lock:
            q = self.hits.setdefault((ip, bucket), deque())
            while q and q[0] <= t - window:
                q.popleft()
            if len(q) >= limit:
                raise ApiError(429, "too many requests, try again later")
            q.append(t)


SOURCE_KEYS = ("pageUrl", "referrer", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")


def _clean_source(src):
    if not isinstance(src, dict):
        return {}
    return {k: str(src[k])[:500] for k in SOURCE_KEYS if src.get(k)}


def _form_template(h, tid):
    """Public routes: a publishable form key must be valid for this assessment."""
    key = h.headers.get("X-Form-Key") or h.query.get("key")
    fk = h.store.get_form_key(key) if key else None
    if not fk or fk["template_id"] != tid:
        raise ApiError(401, "missing or invalid form key")
    row = require_template(h, tid)
    tpl = json.loads(row["body"])
    if "assessment" not in tpl:
        raise ApiError(404, "assessment not found")
    return row, tpl


def assessment_dry_run(tpl, req, lang):
    """Developer preview of both payloads; stores nothing, sends nothing."""
    trace = Trace()
    if not validate_template(tpl, trace):
        return {"status": "FAILED", "failedStage": "template", "errors": trace.errors()}
    answers = req.get("answers") or {}
    errs = assessment.answers_errors(tpl, answers, require_complete=True)
    if errs:
        return {"status": "FAILED", "failedStage": "answers", "errors": errs}
    lead = compliance.clean_lead(req.get("lead") or {
        "first_name": "Test", "last_name": "Lead", "corporate_email": "test.lead@example.ca",
        "company_name": "Example Inc."})
    result = assessment.score_submission(tpl, answers, lead, lang)
    consent = compliance.consent_record(tpl, tpl["version"], {"marketingOptIn": False}, now(), lang)
    agency = assessment.agency_view(tpl, {"versionNo": None, "semver": tpl["version"]}, result, "sub_dryrun",
                                    now(), lead, consent, {}, lang)
    crm = {}
    if (tpl.get("crmMapping") or {}).get("hubspot"):
        ctx = mapping.context(agency)
        crm["hubspot"] = {o: mapping.build_properties(s, ctx)
                          for o, s in tpl["crmMapping"]["hubspot"].items() if isinstance(s, dict)}
    return {"status": "SUCCEEDED", "public": assessment.public_view(tpl, result, "sub_dryrun", lang),
            "agency": agency, "crmPreview": crm}


@route("GET", r"/public/v1/assessments/([\w-]+)")
def public_intro(h, tid):
    h.server.limiter.check(h.client_ip, "read")
    _, tpl = _form_template(h, tid)
    c, a, lang = tpl.get("compliance") or {}, tpl["assessment"], h.lang
    mc = c.get("marketingConsent")
    return 200, {
        "id": tid, "name": i18n.text(tpl["name"], lang), "locale": lang,
        "intro": i18n.text(a.get("intro"), lang) if a.get("intro") else None,
        "estimatedMinutes": a.get("estimatedMinutes"),
        "privacyNotice": i18n.text(c.get("privacyNotice"), lang),
        "sharingDisclosure": i18n.text(c.get("sharingDisclosure"), lang),
        "crossBorderDisclosure": i18n.text(c["crossBorderDisclosure"], lang) if c.get("crossBorderDisclosure") else None,
        "marketingConsent": {"text": i18n.text(mc["text"], lang), "senderName": mc["senderName"],
                             "defaultChecked": False} if mc else None,
        "contactFields": {"required": list(compliance.LEAD_REQUIRED), "optional": ["phone", "province", "job_title"],
                          "requireCorporateEmail": bool(a.get("requireCorporateEmail"))},
        "next": assessment.next_question(tpl, {}, lang),
    }


@route("POST", r"/public/v1/assessments/([\w-]+)/next")
def public_next(h, tid):
    """Skip logic: send the answers so far, get the next applicable question."""
    h.server.limiter.check(h.client_ip, "read")
    _, tpl = _form_template(h, tid)
    answers = h.body().get("answers") or {}
    errs = assessment.answers_errors(tpl, answers, require_complete=False)
    if errs:
        raise ApiError(422, "invalid answers", errs)
    return 200, assessment.next_question(tpl, answers, h.lang)


@route("POST", r"/public/v1/assessments/([\w-]+)/submit")
def public_submit(h, tid):
    h.server.limiter.check(h.client_ip, "submit")
    row, tpl = _form_template(h, tid)
    req = h.body()
    if req.get("website"):  # honeypot: humans never see this field
        h.log_message("honeypot hit from %s", h.client_ip)
        return 202, {"status": "received"}
    answers, lead, consent = req.get("answers") or {}, req.get("lead"), req.get("consent")
    errs = assessment.answers_errors(tpl, answers, require_complete=True) + compliance.lead_errors(tpl, lead, consent)
    if errs:
        raise ApiError(422, "submission is incomplete or invalid", errs)
    lang, ts = h.lang, now()
    lead = compliance.clean_lead(lead)
    result = assessment.score_submission(tpl, answers, lead, lang)
    sid = "sub_" + uuid.uuid4().hex[:16]
    public = assessment.public_view(tpl, result, sid, lang)
    agency = assessment.agency_view(
        tpl, {"versionNo": row["version_no"], "semver": row["semver"]}, result, sid, ts, lead,
        compliance.consent_record(tpl, row["semver"], consent, ts, lang), _clean_source(req.get("source")), lang)
    dispatcher = h.server.dispatcher
    plan = dispatcher.plan(tid, tpl) if dispatcher else []
    h.store.create_submission(sid, row, lang, lead, result["overall"], result["risk"], result["tags"], public,
                              agency, (tpl.get("compliance") or {}).get("retentionDays"), plan)
    if dispatcher:
        dispatcher.wake()  # CRM delivery happens on the worker thread, never in this request
    return 201, public


# ------------------------------------------------------------- admin: forms
@route("POST", r"/v1/forms")
def create_form(h):
    req = h.body()
    tid, origins = req.get("templateId"), req.get("allowedOrigins", [])
    tpl = json.loads(require_template(h, tid or "")["body"])
    if "assessment" not in tpl:
        raise ApiError(422, "forms are only for question-based assessments")
    if not isinstance(origins, list) or not all(isinstance(o, str) and re.match(r"^https?://[^/]+$", o)
                                                for o in origins):
        raise ApiError(400, '"allowedOrigins" must be a list like ["https://www.example.ca"]')
    return 201, {"key": h.store.create_form_key(tid, origins, h.actor), "templateId": tid, "allowedOrigins": origins}


@route("GET", r"/v1/forms")
def list_forms(h):
    return 200, {"items": [{"key": f["key"], "templateId": f["template_id"],
                            "allowedOrigins": json.loads(f["allowed_origins"]), "enabled": bool(f["enabled"]),
                            "createdAt": f["created_at"]} for f in h.store.list_form_keys()]}


# ---------------------------------------------------------- admin: CRM
SECRET_KEYS = {"token", "secret", "apikey", "api_key", "password", "accesstoken", "access_token"}


def dest_view(d):
    return {"id": d["id"], "name": d["name"], "kind": d["kind"], "config": json.loads(d["config"]),
            "region": d["region"], "allowCrossBorder": bool(d["allow_cross_border"]),
            "templateIds": json.loads(d["template_ids"]) if d["template_ids"] else None,
            "enabled": bool(d["enabled"]), "createdAt": d["created_at"]}


@route("POST", r"/v1/crm/destinations")
def create_destination(h):
    req = h.body()
    kind, cfg, region = req.get("kind"), req.get("config") or {}, req.get("region")
    if kind not in ("HUBSPOT", "WEBHOOK"):
        raise ApiError(400, '"kind" must be HUBSPOT or WEBHOOK')
    if not req.get("name") or not isinstance(region, str) or not region:
        raise ApiError(400, '"name" and "region" (e.g. "ca", "us", "eu") are required')
    if not isinstance(cfg, dict) or SECRET_KEYS & {k.lower() for k in cfg}:
        raise ApiError(400, "never send secrets: reference an environment variable with tokenEnv / secretEnv")
    try:
        if kind == "WEBHOOK":
            if not cfg.get("url"):
                raise ApiError(400, "WEBHOOK needs config.url")
            check_url(cfg["url"])
        else:
            if not cfg.get("tokenEnv"):
                raise ApiError(400, "HUBSPOT needs config.tokenEnv")
            check_url(cfg.get("baseUrl", "https://api.hubapi.com"))
    except PermanentError as e:
        raise ApiError(400, str(e))
    tids = req.get("templateIds")
    if tids is not None and not (isinstance(tids, list) and all(isinstance(t, str) for t in tids)):
        raise ApiError(400, '"templateIds" must be a list of template ids')
    d = h.store.create_destination(req["name"], kind, cfg, region, req.get("allowCrossBorder", False), tids, h.actor)
    out = dest_view(d)
    if h.server.policy.enforced and region.lower() not in CANADIAN_REGIONS:
        out["warning"] = ("destination is outside Canada: leads will be BLOCKED unless allowCrossBorder is true "
                          "and the template has compliance.crossBorderDisclosure")
    return 201, out


@route("GET", r"/v1/crm/destinations")
def list_destinations(h):
    return 200, {"items": [dest_view(d) for d in h.store.list_destinations()]}


@route("POST", r"/v1/crm/destinations/(dst_[0-9a-f]+)/(enable|disable)")
def toggle_destination(h, did, action):
    if not h.store.set_destination_enabled(did, action == "enable", h.actor):
        raise ApiError(404, "destination not found")
    return 200, dest_view(h.store.get_destination(did))


def outbox_view(o):
    return {"id": o["id"], "submissionId": o["submission_id"], "destinationId": o["destination_id"],
            "status": o["status"], "attempts": o["attempts"], "nextAttemptAt": o["next_attempt_at"],
            "lastError": o["last_error"], "reason": o["reason"],
            "result": json.loads(o["result"]) if o["result"] else None,
            "createdAt": o["created_at"], "deliveredAt": o["delivered_at"]}


@route("GET", r"/v1/crm/outbox")
def list_outbox(h):
    limit = min(int_param(h.query.get("limit"), "limit") or 100, 1000)
    rows = h.store.list_outbox(h.query.get("status"), h.query.get("submissionId"), limit)
    return 200, {"items": [outbox_view(o) for o in rows]}


@route("POST", r"/v1/crm/outbox/(ob_[0-9a-f]+)/retry")
def retry_outbox(h, oid):
    if not h.store.retry_outbox(oid, h.actor):
        raise ApiError(409, "only FAILED deliveries can be retried")
    if h.server.dispatcher:
        h.server.dispatcher.wake()
    return 200, outbox_view(h.store.get_outbox(oid))


# ---------------------------------------------------- admin: submissions
@route("GET", r"/v1/submissions")
def list_submissions(h):
    limit = min(int_param(h.query.get("limit"), "limit") or 50, 500)
    rows = h.store.list_submissions(h.query.get("templateId"), h.query.get("email"), limit)
    return 200, {"items": [{"id": s["id"], "templateId": s["template_id"], "semver": s["semver"],
                            "email": s["lead_email"], "company": s["company_name"], "score": s["score"],
                            "risk": s["risk"], "tags": json.loads(s["tags"]), "locale": s["locale"],
                            "createdAt": s["created_at"]} for s in rows]}


@route("GET", r"/v1/submissions/(sub_[0-9a-f]+)")
def read_submission(h, sid):
    s = h.store.get_submission(sid)
    if not s:
        raise ApiError(404, "submission not found")
    return 200, {"agency": json.loads(s["agency_payload"]), "public": json.loads(s["public_payload"]),
                 "expiresAt": s["expires_at"],
                 "deliveries": [outbox_view(o) for o in h.store.list_outbox(submission_id=sid)]}


@route("GET", r"/v1/admin/residency")
def residency_status(h):
    return 200, {**h.server.policy.describe(), "schemaVersion": h.store.schema_version()}
