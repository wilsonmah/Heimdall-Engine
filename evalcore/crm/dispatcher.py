"""Asynchronous CRM delivery (transactional outbox).

Submit path (in the request): plan() decides, per enabled destination,
PENDING or BLOCKED (residency); the submission and its outbox rows are written
in one transaction; wake() nudges the worker. The HTTP response never waits
for a CRM.

Worker (background thread): claims due PENDING rows, calls the adapter,
then marks DELIVERED, or schedules a retry with exponential backoff, or marks
FAILED (permanent error / attempts exhausted). FAILED rows can be retried from
the admin API. On start, rows stuck in SENDING after a crash are re-queued.

Delivery is at-least-once: the outbox id travels as X-Evalcore-Delivery (and
HubSpot calls are upserts with resumable state) so receivers can dedupe.
"""
import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from . import PermanentError, TransientError
from .hubspot import HubSpotAdapter
from .webhook import WebhookAdapter

log = logging.getLogger("evalcore.crm")


def _dest_view(row):
    return {"id": row["id"], "name": row["name"], "kind": row["kind"], "config": json.loads(row["config"]),
            "region": row["region"], "allowCrossBorder": bool(row["allow_cross_border"])}


class Dispatcher:
    def __init__(self, store, policy, adapters=None, poll_interval=1.0, max_attempts=6,
                 base_delay=2.0, max_delay=900.0):
        self.store, self.policy = store, policy
        self.adapters = adapters or {a.kind: a for a in (HubSpotAdapter(), WebhookAdapter())}
        self.poll_interval, self.max_attempts = poll_interval, max_attempts
        self.base_delay, self.max_delay = base_delay, max_delay
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ planning
    def plan(self, template_id, tpl):
        out = []
        for d in self.store.destinations_for(template_id):
            if d["kind"] not in self.adapters:
                out.append((d["id"], "BLOCKED", f"no adapter for kind {d['kind']}"))
                continue
            ok, reason = self.policy.destination_allowed(d["region"], bool(d["allow_cross_border"]), tpl)
            out.append((d["id"], "PENDING" if ok else "BLOCKED", reason))
        return out

    # ------------------------------------------------------------- worker
    def start(self):
        if self._thread:
            return
        n = self.store.reset_stale_sending()
        if n:
            log.warning("re-queued %d outbox rows left in SENDING", n)
        self._thread = threading.Thread(target=self._loop, name="crm-dispatcher", daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def wake(self):
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                busy = self.run_once()
            except Exception:  # keep the worker alive whatever happens
                log.exception("dispatcher iteration failed")
                busy = 0
            if not busy:
                self._wake.wait(self.poll_interval)
                self._wake.clear()

    def run_once(self, limit=20):
        rows = self.store.claim_due_outbox(limit)
        for row in rows:
            self._deliver(row)
        return len(rows)

    def _deliver(self, row):
        attempts = row["attempts"] + 1
        state = json.loads(row["state"] or "{}")
        state["deliveryId"] = row["id"]
        sub = self.store.get_submission(row["submission_id"])
        dest_row = self.store.get_destination(row["destination_id"])
        if not sub or not dest_row:
            return self.store.finish_outbox(row["id"], "FAILED", attempts, state,
                                            error="submission or destination no longer exists")
        tpl = json.loads(self.store.get_template(sub["template_id"], sub["version_no"])["body"])
        ok, reason = self.policy.destination_allowed(dest_row["region"], bool(dest_row["allow_cross_border"]), tpl)
        if not ok:  # policy or destination changed since the lead was queued
            return self.store.finish_outbox(row["id"], "BLOCKED", attempts, state, reason=reason)
        if not dest_row["enabled"]:
            return self.store.finish_outbox(row["id"], "BLOCKED", attempts, state, reason="destination disabled")
        try:
            result = self.adapters[dest_row["kind"]].send(_dest_view(dest_row), json.loads(sub["agency_payload"]),
                                                         tpl, state)
        except TransientError as e:
            if attempts >= self.max_attempts:
                return self.store.finish_outbox(row["id"], "FAILED", attempts, state, error=str(e))
            delay = e.retry_after or min(self.max_delay, self.base_delay * 2 ** (attempts - 1))
            nxt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
            return self.store.finish_outbox(row["id"], "PENDING", attempts, state, next_attempt_at=nxt, error=str(e))
        except PermanentError as e:
            return self.store.finish_outbox(row["id"], "FAILED", attempts, state, error=str(e))
        except Exception as e:  # adapter bug: do not retry forever
            log.exception("adapter crashed")
            return self.store.finish_outbox(row["id"], "FAILED", attempts, state, error=f"adapter error: {e!r}")
        self.store.finish_outbox(row["id"], "DELIVERED", attempts, state, result=result)
