"""Tiny JSON-over-HTTPS client shared by the adapters (stdlib urllib)."""
import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from . import PermanentError, TransientError

USER_AGENT = "evalcore-crm/0.1"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def check_url(url):
    """HTTPS only, except loopback for development and tests."""
    u = urlparse(url)
    if u.scheme == "https" and u.hostname:
        return
    if u.scheme == "http" and u.hostname in LOCAL_HOSTS:
        return
    raise PermanentError(f"destination URL must be https: {url}")


def request(method, url, body=None, headers=None, timeout=10):
    check_url(url)
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read()[:300].decode(errors="replace")
        msg = f"{method} {urlparse(url).path} -> HTTP {e.code}: {detail}"
        if e.code == 429 or e.code >= 500:
            ra = e.headers.get("Retry-After")
            raise TransientError(msg, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
        raise PermanentError(msg)
    except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
        raise TransientError(f"{method} {urlparse(url).netloc}: {e}")
