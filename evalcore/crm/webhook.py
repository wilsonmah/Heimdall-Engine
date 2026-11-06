"""Generic signed webhook - for Zapier, Make, n8n, Salesforce Flow, Zoho Flow
or the agency's own middleware.

Body: the full agency payload (JSON). Headers:
    X-Evalcore-Event:      assessment.completed
    X-Evalcore-Delivery:   outbox id - stable across retries, use it to dedupe
    X-Evalcore-Timestamp:  unix seconds
    X-Evalcore-Signature:  v1=<hex HMAC-SHA256(secret, "{timestamp}.{body}")>

Receivers should reject timestamps older than 5 minutes and compare the
signature in constant time. Destination config:
    {"url": "https://hooks.zapier.com/...", "secretEnv": "ZAPIER_WEBHOOK_SECRET"}
Omit secretEnv for receivers that cannot verify signatures.
"""
import hashlib
import hmac
import json
import os
import time

from . import PermanentError
from .http import request


def sign(secret, timestamp, body):
    return "v1=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


class WebhookAdapter:
    kind = "WEBHOOK"

    def send(self, dest, agency, tpl, state):
        cfg = dest["config"]
        if not cfg.get("url"):
            raise PermanentError("webhook destination has no url")
        body = json.dumps(agency, ensure_ascii=False).encode()
        ts = str(int(time.time()))
        headers = {"X-Evalcore-Event": agency["event"], "X-Evalcore-Delivery": state["deliveryId"],
                   "X-Evalcore-Timestamp": ts}
        if cfg.get("secretEnv"):
            secret = os.environ.get(cfg["secretEnv"], "")
            if not secret:
                raise PermanentError(f"webhook secret env var {cfg['secretEnv']!r} is not set")
            headers["X-Evalcore-Signature"] = sign(secret, ts, body)
        status, _ = request("POST", cfg["url"], body, headers)
        return {"httpStatus": status}
