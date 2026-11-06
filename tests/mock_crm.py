"""Local stand-ins for HubSpot and a webhook receiver (Zapier/Make)."""
import hashlib
import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Base(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        return raw, (json.loads(raw) if raw else None)

    def _json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MockHubSpot:
    """Implements the HubSpot v3/v4 calls the adapter makes, with failure injection."""

    def __init__(self, token="test-hubspot-token"):
        self.token = token
        self.calls, self.contacts, self.companies, self.notes, self.associations = [], {}, {}, [], []
        self.fail = {}  # path regex -> number of 503s to return first
        self.delay = 0.0  # seconds added to every response (simulates a slow CRM)
        self.lock = threading.Lock()
        mock = self

        class H(_Base):
            def _handle(self, method):
                _, body = self._body()
                time.sleep(mock.delay)
                with mock.lock:
                    mock.calls.append((method, self.path, body))
                    for rx, n in list(mock.fail.items()):
                        if n and re.search(rx, self.path):
                            mock.fail[rx] = n - 1
                            return self._json(503, {"message": "temporarily unavailable"})
                    if self.headers.get("Authorization") != f"Bearer {mock.token}":
                        return self._json(401, {"message": "invalid token"})
                    return mock.route(self, method, self.path, body)

            def do_POST(self): self._handle("POST")
            def do_PATCH(self): self._handle("PATCH")
            def do_PUT(self): self._handle("PUT")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def route(self, h, method, path, body):
        if path == "/crm/v3/objects/contacts/batch/upsert":
            inp = body["inputs"][0]
            cid = self.contacts.get(inp["id"], {}).get("id") or f"c{len(self.contacts) + 1}"
            self.contacts[inp["id"]] = {"id": cid, "properties": inp["properties"]}
            return h._json(200, {"status": "COMPLETE", "results": [{"id": cid, "properties": inp["properties"]}]})
        if path == "/crm/v3/objects/companies/search":
            dom = body["filterGroups"][0]["filters"][0]["value"]
            hit = [{"id": c["id"]} for c in self.companies.values() if c["properties"].get("domain") == dom]
            return h._json(200, {"total": len(hit), "results": hit})
        if path == "/crm/v3/objects/companies" and method == "POST":
            coid = f"co{len(self.companies) + 1}"
            self.companies[coid] = {"id": coid, "properties": body["properties"]}
            return h._json(201, {"id": coid})
        m = re.match(r"^/crm/v3/objects/companies/(\w+)$", path)
        if m and method == "PATCH":
            self.companies[m.group(1)]["properties"].update(body["properties"])
            return h._json(200, {"id": m.group(1)})
        m = re.match(r"^/crm/v4/objects/contacts/(\w+)/associations/default/companies/(\w+)$", path)
        if m and method == "PUT":
            self.associations.append(m.groups())
            return h._json(200, {"status": "COMPLETE"})
        if path == "/crm/v3/objects/notes":
            self.notes.append(body)
            return h._json(201, {"id": f"n{len(self.notes)}"})
        return h._json(404, {"message": f"no route {method} {path}"})

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class MockWebhook:
    def __init__(self, secret=None, status=200):
        self.secret, self.status, self.received = secret, status, []
        mock = self

        class H(_Base):
            def do_POST(self):
                raw, body = self._body()
                ts, sig = self.headers.get("X-Evalcore-Timestamp"), self.headers.get("X-Evalcore-Signature")
                valid = None
                if mock.secret:
                    want = "v1=" + hmac.new(mock.secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
                    valid = hmac.compare_digest(want, sig or "")
                mock.received.append({"headers": dict(self.headers), "body": body, "signatureValid": valid})
                self._json(mock.status, {"ok": mock.status < 300})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/hook"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
