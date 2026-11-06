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
        with self.conn() as c:
            c.executescript(SCHEMA_SQL)

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

    # ------------------------------------------------------------- privacy
    def erase_subject(self, entity_id, actor=None):
        with self.tx() as c:
            n = c.execute("DELETE FROM evaluations WHERE entity_id=?", (entity_id,)).rowcount
            self._audit(c, actor, "subject.erase", subject_hash(entity_id), {"evaluationsDeleted": n})
            return n

    def purge_expired(self, actor=None):
        with self.tx() as c:
            n = c.execute("DELETE FROM evaluations WHERE expires_at IS NOT NULL AND expires_at < ?",
                          (now(),)).rowcount
            self._audit(c, actor, "retention.purge", None, {"evaluationsDeleted": n})
            return n

    def list_audit(self, limit=100):
        with self.conn() as c:
            return c.execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
