"""SQLite persistence: versioned templates, evaluations, audit log.

SQLite keeps the prototype dependency-free. The Store class is the only
place that knows SQL, so moving to PostgreSQL (hosted in a Canadian region)
means reimplementing this one class.
"""
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .trace import Trace
from .validation import validate_template

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS template_versions (
  template_id TEXT NOT NULL, version_no INTEGER NOT NULL, semver TEXT NOT NULL,
  body TEXT NOT NULL, author TEXT, note TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY (template_id, version_no));
CREATE TABLE IF NOT EXISTS template_heads (
  template_id TEXT PRIMARY KEY, active_version_no INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations (
  id TEXT PRIMARY KEY, template_id TEXT NOT NULL, version_no INTEGER NOT NULL,
  semver TEXT NOT NULL, entity_id TEXT, target_type TEXT, status TEXT NOT NULL,
  failed_stage TEXT, result TEXT, payload TEXT NOT NULL, trace TEXT NOT NULL,
  review TEXT, created_at TEXT NOT NULL, expires_at TEXT);
CREATE INDEX IF NOT EXISTS ix_eval_tpl ON evaluations(template_id, created_at);
CREATE INDEX IF NOT EXISTS ix_eval_entity ON evaluations(entity_id, created_at);
CREATE INDEX IF NOT EXISTS ix_eval_expiry ON evaluations(expires_at);
CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT,
  action TEXT NOT NULL, subject TEXT, detail TEXT);
"""

LEADGEN_SQL = """
CREATE TABLE IF NOT EXISTS submissions (
  id TEXT PRIMARY KEY, template_id TEXT NOT NULL, version_no INTEGER NOT NULL, semver TEXT NOT NULL,
  locale TEXT NOT NULL, lead_email TEXT NOT NULL, company_name TEXT, score REAL, risk TEXT,
  tags TEXT NOT NULL, public_payload TEXT NOT NULL, agency_payload TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT);
CREATE INDEX IF NOT EXISTS ix_sub_tpl ON submissions(template_id, created_at);
CREATE INDEX IF NOT EXISTS ix_sub_email ON submissions(lead_email);
CREATE INDEX IF NOT EXISTS ix_sub_expiry ON submissions(expires_at);
CREATE TABLE IF NOT EXISTS crm_destinations (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, config TEXT NOT NULL,
  region TEXT NOT NULL, allow_cross_border INTEGER NOT NULL DEFAULT 0, template_ids TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS crm_outbox (
  id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, destination_id TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
  last_error TEXT, reason TEXT, state TEXT NOT NULL DEFAULT '{}', result TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, delivered_at TEXT,
  UNIQUE (submission_id, destination_id));
CREATE INDEX IF NOT EXISTS ix_outbox_due ON crm_outbox(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS form_keys (
  key TEXT PRIMARY KEY, template_id TEXT NOT NULL, allowed_origins TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
"""

# Append only. Each entry runs once per database, in order.
MIGRATIONS = [
    (1, "core: templates, evaluations, audit", SCHEMA_SQL),
    (2, "lead-gen: submissions, CRM destinations, outbox, form keys", LEADGEN_SQL),
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _semver(s):
    return tuple(int(x) for x in s.split("."))


def subject_hash(entity_id):
    """Audit entries about erased people keep only a hash, never the raw id."""
    return "sha256:" + hashlib.sha256(entity_id.encode()).hexdigest()[:16]


class ValidationFailed(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.migrate()

    def migrate(self):
        with self.conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                      "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            done = {r[0] for r in c.execute("SELECT version FROM schema_migrations")}
            for version, name, sql in MIGRATIONS:
                if version in done:
                    continue
                c.executescript(sql)
                c.execute("INSERT INTO schema_migrations VALUES (?,?,?)", (version, name, now()))

    def schema_version(self):
        with self.conn() as c:
            return c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def tx(self):
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    @staticmethod
    def _audit(c, actor, action, subject, detail=None):
        c.execute("INSERT INTO audit_log (at, actor, action, subject, detail) VALUES (?,?,?,?,?)",
                  (now(), actor, action, subject, json.dumps(detail) if detail is not None else None))

    # ------------------------------------------------------------ templates
    def get_template(self, tid, version_no=None, c=None):
        if c is None:
            with self.conn() as c:
                return self.get_template(tid, version_no, c)
        if version_no is None:
            row = c.execute("SELECT active_version_no FROM template_heads WHERE template_id=?", (tid,)).fetchone()
            if not row:
                return None
            version_no = row[0]
        return c.execute("SELECT * FROM template_versions WHERE template_id=? AND version_no=?",
                         (tid, version_no)).fetchone()

    def active_version_no(self, tid):
        with self.conn() as c:
            row = c.execute("SELECT active_version_no FROM template_heads WHERE template_id=?", (tid,)).fetchone()
            return row[0] if row else None

    def list_templates(self):
        with self.conn() as c:
            return c.execute("SELECT v.* FROM template_heads h JOIN template_versions v "
                             "ON v.template_id=h.template_id AND v.version_no=h.active_version_no "
                             "ORDER BY v.template_id").fetchall()

    def list_versions(self, tid):
        with self.conn() as c:
            return c.execute("SELECT * FROM template_versions WHERE template_id=? ORDER BY version_no DESC",
                             (tid,)).fetchall()

    def save_version(self, tid, body, author=None, note=None):
        """Validate, store as a new immutable version and activate it.
        The server owns version bumps: a client semver is kept only if it moves forward."""
        if body.get("evaluationTemplateId") != tid:
            raise ValidationFailed([f"evaluationTemplateId must be '{tid}'"])
        with self.tx() as c:
            head = self.get_template(tid, c=c)
            if head:
                prev, given = head["semver"], str(body.get("version", ""))
                if not re.fullmatch(r"\d+\.\d+\.\d+", given) or _semver(given) <= _semver(prev):
                    a, b, p = _semver(prev)
                    body["version"] = f"{a}.{b}.{p + 1}"
            trace = Trace()
            if not validate_template(body, trace):
                raise ValidationFailed(trace.errors())
            n = c.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM template_versions WHERE template_id=?",
                          (tid,)).fetchone()[0]
            ts = now()
            c.execute("INSERT INTO template_versions VALUES (?,?,?,?,?,?,?)",
                      (tid, n, body["version"], json.dumps(body, ensure_ascii=False), author, note, ts))
            c.execute("INSERT INTO template_heads VALUES (?,?,?) ON CONFLICT(template_id) DO UPDATE "
                      "SET active_version_no=excluded.active_version_no, updated_at=excluded.updated_at",
                      (tid, n, ts))
            self._audit(c, author, "template.save", tid, {"versionNo": n, "semver": body["version"], "note": note})
            return self.get_template(tid, n, c)

    def activate(self, tid, version_no, actor=None):
        with self.tx() as c:
            row = self.get_template(tid, version_no, c)
            if not row:
                return None
            prev = self.get_template(tid, c=c)["version_no"]
            c.execute("UPDATE template_heads SET active_version_no=?, updated_at=? WHERE template_id=?",
                      (version_no, now(), tid))
            self._audit(c, actor, "template.rollback", tid, {"from": prev, "to": version_no})
            return row

    # ---------------------------------------------------------- evaluations
    def insert_evaluation(self, tpl_row, payload, entity_id, status, failed_stage, result, trace,
                          retention_days=None):
        eid = "ev_" + uuid.uuid4().hex[:16]
        created = datetime.now(timezone.utc)
        expires = (created + timedelta(days=retention_days)).isoformat(timespec="seconds") if retention_days else None
        with self.conn() as c:
            c.execute("INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (eid, tpl_row["template_id"], tpl_row["version_no"], tpl_row["semver"], entity_id,
                       payload.get("targetType"), status, failed_stage, json.dumps(result),
                       json.dumps(payload, ensure_ascii=False), json.dumps(trace), None,
                       created.isoformat(timespec="seconds"), expires))
        return self.get_evaluation(eid)

    def get_evaluation(self, eid):
        with self.conn() as c:
            return c.execute("SELECT * FROM evaluations WHERE id=?", (eid,)).fetchone()

    def list_evaluations(self, template_id=None, entity_id=None, status=None, limit=50):
        sql, args = "SELECT * FROM evaluations WHERE 1=1", []
        for col, val in (("template_id", template_id), ("entity_id", entity_id), ("status", status)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        with self.conn() as c:
            return c.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()

    def record_review(self, eid, review, new_status):
        with self.tx() as c:
            c.execute("UPDATE evaluations SET review=?, status=? WHERE id=?",
                      (json.dumps(review, ensure_ascii=False), new_status, eid))
            self._audit(c, review["reviewer"], "evaluation.review", eid,
                        {"decision": review["decision"], "finalScore": review["finalScore"]})

    # ---------------------------------------------------------- submissions
    def create_submission(self, sid, tpl_row, locale, lead, score, risk, tags, public, agency,
                          retention_days, outbox_plan):
        """Stores the submission and its outbox rows in ONE transaction
        (transactional outbox): a lead is never saved without its CRM jobs."""
        created = datetime.now(timezone.utc)
        ts = created.isoformat(timespec="seconds")
        expires = (created + timedelta(days=retention_days)).isoformat(timespec="seconds") if retention_days else None
        with self.tx() as c:
            c.execute("INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, tpl_row["template_id"], tpl_row["version_no"], tpl_row["semver"], locale,
                       lead["corporate_email"], lead.get("company_name"), score, risk, json.dumps(tags),
                       json.dumps(public, ensure_ascii=False), json.dumps(agency, ensure_ascii=False), ts, expires))
            for dest_id, status, reason in outbox_plan:
                c.execute("INSERT INTO crm_outbox (id, submission_id, destination_id, status, next_attempt_at, "
                          "reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                          ("ob_" + uuid.uuid4().hex[:16], sid, dest_id, status,
                           ts if status == "PENDING" else None, reason, ts, ts))
                if status == "BLOCKED":
                    self._audit(c, "system", "crm.blocked", sid, {"destination": dest_id, "reason": reason})
        return self.get_submission(sid)

    def get_submission(self, sid):
        with self.conn() as c:
            return c.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()

    def list_submissions(self, template_id=None, email=None, limit=50):
        sql, args = "SELECT * FROM submissions WHERE 1=1", []
        if template_id:
            sql += " AND template_id=?"
            args.append(template_id)
        if email:
            sql += " AND lead_email=?"
            args.append(email.lower())
        with self.conn() as c:
            return c.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()

    # ------------------------------------------------------- CRM destinations
    def create_destination(self, name, kind, config, region, allow_cross_border, template_ids, actor=None):
        did = "dst_" + uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute("INSERT INTO crm_destinations VALUES (?,?,?,?,?,?,?,?,?)",
                      (did, name, kind, json.dumps(config), region.lower(), int(bool(allow_cross_border)),
                       json.dumps(template_ids) if template_ids else None, 1, now()))
            self._audit(c, actor, "crm.destination.create", did,
                        {"name": name, "kind": kind, "region": region, "allowCrossBorder": bool(allow_cross_border)})
        return self.get_destination(did)

    def set_destination_enabled(self, did, enabled, actor=None):
        with self.tx() as c:
            n = c.execute("UPDATE crm_destinations SET enabled=? WHERE id=?", (int(enabled), did)).rowcount
            if n:
                self._audit(c, actor, "crm.destination.enable" if enabled else "crm.destination.disable", did)
            return n

    def get_destination(self, did):
        with self.conn() as c:
            return c.execute("SELECT * FROM crm_destinations WHERE id=?", (did,)).fetchone()

    def list_destinations(self):
        with self.conn() as c:
            return c.execute("SELECT * FROM crm_destinations ORDER BY created_at").fetchall()

    def destinations_for(self, template_id):
        return [d for d in self.list_destinations()
                if d["enabled"] and (not d["template_ids"] or template_id in json.loads(d["template_ids"]))]

    # ------------------------------------------------------------- outbox
    def claim_due_outbox(self, limit=20):
        ts = now()
        with self.tx() as c:
            return c.execute(
                "UPDATE crm_outbox SET status='SENDING', updated_at=? WHERE id IN ("
                " SELECT id FROM crm_outbox WHERE status='PENDING' AND next_attempt_at<=?"
                " ORDER BY next_attempt_at LIMIT ?) RETURNING *", (ts, ts, limit)).fetchall()

    def finish_outbox(self, oid, status, attempts, state, next_attempt_at=None, error=None, result=None,
                      reason=None):
        ts = now()
        with self.conn() as c:
            c.execute("UPDATE crm_outbox SET status=?, attempts=?, state=?, next_attempt_at=?, last_error=?, "
                      "result=?, reason=COALESCE(?, reason), updated_at=?, delivered_at=? WHERE id=?",
                      (status, attempts, json.dumps(state), next_attempt_at, error,
                       json.dumps(result) if result is not None else None, reason, ts,
                       ts if status == "DELIVERED" else None, oid))

    def reset_stale_sending(self):
        """A crash mid-send leaves rows SENDING; put them back in the queue."""
        with self.conn() as c:
            return c.execute("UPDATE crm_outbox SET status='PENDING', next_attempt_at=? WHERE status='SENDING'",
                             (now(),)).rowcount

    def list_outbox(self, status=None, submission_id=None, limit=100):
        sql, args = "SELECT * FROM crm_outbox WHERE 1=1", []
        for col, val in (("status", status), ("submission_id", submission_id)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        with self.conn() as c:
            return c.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()

    def get_outbox(self, oid):
        with self.conn() as c:
            return c.execute("SELECT * FROM crm_outbox WHERE id=?", (oid,)).fetchone()

    def retry_outbox(self, oid, actor=None):
        with self.tx() as c:
            n = c.execute("UPDATE crm_outbox SET status='PENDING', next_attempt_at=?, attempts=0, updated_at=? "
                          "WHERE id=? AND status='FAILED'", (now(), now(), oid)).rowcount
            if n:
                self._audit(c, actor, "crm.retry", oid)
            return n

    # ------------------------------------------------------------ form keys
    def create_form_key(self, template_id, allowed_origins, actor=None):
        key = "pk_" + uuid.uuid4().hex
        with self.tx() as c:
            c.execute("INSERT INTO form_keys VALUES (?,?,?,?,?)",
                      (key, template_id, json.dumps(allowed_origins), 1, now()))
            self._audit(c, actor, "form.key.create", template_id, {"allowedOrigins": allowed_origins})
        return key

    def get_form_key(self, key):
        with self.conn() as c:
            return c.execute("SELECT * FROM form_keys WHERE key=? AND enabled=1", (key,)).fetchone()

    def list_form_keys(self):
        with self.conn() as c:
            return c.execute("SELECT * FROM form_keys ORDER BY created_at").fetchall()

    # ------------------------------------------------------------- privacy
    def erase_subject(self, entity_id, actor=None):
        """Deletes evaluations by entity id and lead submissions by email, with
        their outbox rows. Data already delivered to a CRM must be erased there too."""
        with self.tx() as c:
            n = c.execute("DELETE FROM evaluations WHERE entity_id=?", (entity_id,)).rowcount
            subs = [r[0] for r in c.execute("SELECT id FROM submissions WHERE lead_email=?", (entity_id.lower(),))]
            for sid in subs:
                c.execute("DELETE FROM crm_outbox WHERE submission_id=?", (sid,))
                c.execute("DELETE FROM submissions WHERE id=?", (sid,))
            self._audit(c, actor, "subject.erase", subject_hash(entity_id.lower() if subs else entity_id),
                        {"evaluationsDeleted": n, "submissionsDeleted": len(subs)})
            return n + len(subs)

    def purge_expired(self, actor=None):
        ts = now()
        with self.tx() as c:
            n = c.execute("DELETE FROM evaluations WHERE expires_at IS NOT NULL AND expires_at < ?", (ts,)).rowcount
            c.execute("DELETE FROM crm_outbox WHERE submission_id IN "
                      "(SELECT id FROM submissions WHERE expires_at IS NOT NULL AND expires_at < ?)", (ts,))
            m = c.execute("DELETE FROM submissions WHERE expires_at IS NOT NULL AND expires_at < ?", (ts,)).rowcount
            self._audit(c, actor, "retention.purge", None, {"evaluationsDeleted": n, "submissionsDeleted": m})
            return n + m

    def list_audit(self, limit=100):
        with self.conn() as c:
            return c.execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
