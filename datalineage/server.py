from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import uuid
from urllib.parse import urlparse
from .clients import FakeHubClient, FakePaperClient, RealHubClient, RealPaperClient
from .exports import cyclonedx, json_export, pdf_export
from .logsafe import Metrics, get_logger
from .pdf import make_pdf
from .resolver import Resolver
from .template_draft import build_draft
from .vault import FileVault

ROOT = Path(__file__).resolve().parents[1]
ID = re.compile(r"^[A-Za-z0-9][\w.-]{0,95}(/[\w.-]{1,96})?$")
EMAIL = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,185}\.[^\s@]{2,63}$")
STATIC = {"/": "index.html", "/static/app.js": "app.js", "/static/style.css": "style.css"}
_POST_PATHS = {"/api/check", "/api/manifest", "/api/report", "/api/signup", "/api/pilot"}


class App:
    def __init__(self, resolver, vault, report_ttl=3600, session_secret=None):
        self.resolver, self.vault = resolver, vault
        self.reports, self.metrics = OrderedDict(), Metrics()
        self.report_ttl = max(60, int(report_ttl))
        configured = session_secret if session_secret is not None else os.getenv("DL_SESSION_SECRET")
        self.session_secret = configured or secrets.token_bytes(32)
        if isinstance(self.session_secret, str):
            self.session_secret = self.session_secret.encode("utf-8")
        self._lock = threading.RLock()
        self._rates = OrderedDict()

    def sign_sid(self, sid):
        signature = hmac.new(self.session_secret, sid.encode("ascii"), hashlib.sha256).hexdigest()
        return sid + "." + signature

    def verify_sid(self, cookie):
        match = re.fullmatch(r"([0-9a-f]{32})\.([0-9a-f]{64})", cookie or "")
        if not match:
            return None
        expected = hmac.new(self.session_secret, match.group(1).encode("ascii"), hashlib.sha256).hexdigest()
        return match.group(1) if hmac.compare_digest(expected, match.group(2)) else None

    def _prune(self):
        cutoff = time.monotonic() - self.report_ttl
        while self.reports and next(iter(self.reports.values()))[3] < cutoff:
            self.reports.popitem(last=False)
        while len(self.reports) > 100:
            self.reports.popitem(last=False)

    def keep(self, report, manifest, sid):
        draft = build_draft(report, manifest)
        with self._lock:
            self._prune()
            self.reports[report.report_id] = (report, draft, sid, time.monotonic())
            self._prune()
        return draft

    def get_report(self, report_id, sid):
        with self._lock:
            self._prune()
            value = self.reports.get(report_id)
            if not value or not sid or not hmac.compare_digest(value[2], sid):
                return None
            self.reports.move_to_end(report_id)
            return value[0], value[1]

    def allow(self, action, identity, limit=8, window=60.0):
        now = time.monotonic()
        key = (action, identity)
        with self._lock:
            events = self._rates.setdefault(key, deque())
            while events and events[0] <= now - window:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            self._rates.move_to_end(key)
            while len(self._rates) > 10000:
                self._rates.popitem(last=False)
            return True


def parse_roots(value):
    if not isinstance(value, dict):
        raise ValueError
    if "datasets" in value:
        if not isinstance(value["datasets"], list) or any(not isinstance(item, dict) for item in value["datasets"]):
            raise ValueError
        roots = [item.get("id") for item in value["datasets"]]
    elif "text" in value:
        if not isinstance(value["text"], str):
            raise ValueError
        roots = [item.strip() for item in value["text"].splitlines() if item.strip()]
    elif "id" in value:
        roots = [value["id"]]
    else:
        raise ValueError
    if not roots or len(roots) > 50 or any(not isinstance(item, str) or not ID.fullmatch(item.removeprefix("hf:")) for item in roots):
        raise ValueError
    return roots


def _validate_manifest(value):
    if not isinstance(value, dict):
        raise ValueError
    model = value.get("model")
    if model is not None:
        if not isinstance(model, dict):
            raise ValueError
        for key in ("provider", "name", "base_model"):
            field = model.get(key)
            if field is not None and (not isinstance(field, str) or len(field) > 500):
                raise ValueError
    datasets = value.get("datasets")
    if datasets is not None:
        if not isinstance(datasets, list) or len(datasets) > 50:
            raise ValueError
        for item in datasets:
            if not isinstance(item, dict):
                raise ValueError
            role = item.get("role")
            if role is not None and (not isinstance(role, str) or len(role) > 100):
                raise ValueError


def _depth(body, cap):
    requested = body.get("depth", cap)
    if requested is None:
        requested = cap
    if type(requested) is not int or requested < 0:
        raise ValueError
    return min(requested, cap)


def _template_pdf(draft):
    lines = ["Commission training-content summary draft", draft.get("banner") or "Verified template structure"]
    lines += ["Template version: " + str(draft.get("template_version")), "Source: " + str(draft.get("source_url"))]
    for section in draft["sections"]:
        lines.append(section["id"] + " " + section["title"])
        for name, field in section["fields"].items():
            value = field["value"]
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            lines.append(f"{name}: {value} [{field['status']}; {field['basis']}]")
    return make_pdf(lines)


def handler(app):
    log = get_logger()

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def log_message(self, *args):
            pass

        def _host_ok(self):
            host = self.headers.get("Host", "")
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}", f"[::1]:{self.server.server_port}"}
            return host.lower() in allowed

        def _origin_ok(self):
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlparse(origin)
                return parsed.scheme == "http" and parsed.netloc.lower() == self.headers.get("Host", "").lower() and not parsed.path.strip("/")
            return self.headers.get("X-DataLineage-Request", "") == "1"

        def _sid(self):
            match = re.search(r"(?:^|;\s*)dl_sid=([^;]+)", self.headers.get("Cookie", ""))
            return app.verify_sid(match.group(1)) if match else None

        def _headers(self, status, content_type, length=0, cookie=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            if cookie:
                self.send_header("Set-Cookie", f"dl_sid={cookie}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()

        def _send(self, status, value, content_type="application/json; charset=utf-8", cookie=None):
            body = json.dumps(value).encode("utf-8") if not isinstance(value, bytes) else value
            self._headers(status, content_type, len(body), cookie)
            self.wfile.write(body)

        def _error(self, status):
            request_id = uuid.uuid4().hex
            route = urlparse(self.path).path
            log.error("request failed request_id=%s route=%s", request_id, route)
            self._send(status, {"error": "Request could not be processed", "request_id": request_id})

        def _body(self):
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise TypeError
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError
            if length > 65536:
                raise OverflowError
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            value = json.loads(raw or b"{}")
            if not isinstance(value, dict):
                raise ValueError
            return value

        def do_GET(self):
            if not self._host_ok():
                self._error(403)
                return
            path = urlparse(self.path).path
            if path in STATIC:
                try:
                    body = (ROOT / "static" / STATIC[path]).read_bytes()
                except OSError:
                    self._error(404)
                    return
                kind = "text/html" if path == "/" else "text/javascript" if path.endswith(".js") else "text/css"
                self._send(200, body, kind + "; charset=utf-8")
                return
            if path == "/api/metrics":
                token = os.getenv("DL_ADMIN_TOKEN")
                supplied = self.headers.get("Authorization", "")
                allowed = False
                try:
                    allowed = bool(os.getenv("DL_ADMIN") == "1" and token and hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + token).encode("utf-8")))
                except (TypeError, UnicodeError):
                    allowed = False
                if not allowed:
                    self._error(403)
                    return
                self._send(200, app.metrics.snapshot())
                return
            self._error(404)

        def do_POST(self):
            started = time.monotonic()
            path = urlparse(self.path).path
            try:
                if not self._host_ok() or path not in _POST_PATHS:
                    self._error(403 if path in _POST_PATHS else 404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._error(400)
                    return
                if length > 65536:
                    self._error(413)
                    return
                if not self._origin_ok():
                    self._error(403)
                    return
                try:
                    body = self._body()
                except OverflowError:
                    self._error(413)
                    return
                except (TypeError, ValueError, json.JSONDecodeError):
                    self._error(400)
                    return
                if not app.allow(path, self.client_address[0]):
                    self._error(429)
                    return
                if path in {"/api/check", "/api/manifest"}:
                    roots = parse_roots(body)
                    free = path == "/api/check"
                    if free and len(roots) != 1:
                        raise ValueError
                    manifest = body.get("manifest", body)
                    _validate_manifest(body)
                    _validate_manifest(manifest)
                    report = app.resolver.resolve(roots, _depth(body, 2 if free else 4))
                    if isinstance(body.get("datasets"), list):
                        report.roles = {"hf:" + item["id"].removeprefix("hf:"): item["role"] for item in body["datasets"] if isinstance(item.get("role"), str)}
                    sid = self._sid() or uuid.uuid4().hex
                    app.keep(report, manifest, sid)
                    app.metrics.checker(sid)
                    self._send(200, report.asdict(), cookie=app.sign_sid(sid))
                    return
                if path == "/api/report":
                    report_id = body.get("report_id")
                    if not isinstance(report_id, str) or not re.fullmatch(r"[0-9a-f]{32}", report_id):
                        raise ValueError
                    saved = app.get_report(report_id, self._sid())
                    if not saved:
                        self._error(404)
                        return
                    report, draft = saved
                    kind = body.get("format", "json")
                    if kind == "json":
                        payload, content_type = json_export(report, draft), "application/json; charset=utf-8"
                    elif kind == "cyclonedx":
                        payload, content_type = cyclonedx(report), "application/vnd.cyclonedx+json"
                    elif kind == "pdf":
                        payload, content_type = pdf_export(report, draft), "application/pdf"
                    elif kind == "template-json":
                        payload, content_type = json.dumps(draft, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8"
                    elif kind == "template-pdf":
                        payload, content_type = _template_pdf(draft), "application/pdf"
                    else:
                        raise ValueError
                    self._send(200, payload, content_type)
                    return
                if path == "/api/signup":
                    email = body.get("email")
                    if not isinstance(email, str) or len(email) > 254 or not EMAIL.fullmatch(email):
                        raise ValueError
                    answer = body.get("provider_answer")
                    if answer is not None and (not isinstance(answer, str) or len(answer) > 500):
                        raise ValueError
                    app.vault.put("signup", {"email": email, "provider_answer": answer})
                    app.metrics.inc("signups")
                    self._send(200, {"ok": True})
                    return
                contact = body.get("contact")
                if contact is not None and (not isinstance(contact, str) or len(contact) > 500):
                    raise ValueError
                notes = body.get("notes")
                if notes is not None and (not isinstance(notes, str) or len(notes) > 2000):
                    raise ValueError
                app.vault.put("pilot", {"contact": contact, "notes": notes})
                app.metrics.inc("pilot_requests")
                self._send(200, {"ok": True})
            except (ValueError, TypeError, OverflowError):
                self._error(400)
            except Exception:
                self._error(500)
            finally:
                log.info("route=%s duration_ms=%d", path, int((time.monotonic() - started) * 1000))

    return Handler


def _configuration(environ):
    mode = environ.get("DL_MODE", "fake")
    if mode not in {"fake", "real"}:
        raise ValueError("DL_MODE must be fake or real")
    numbers = {}
    for name, default, minimum in (("DL_PORT", 8765, 1), ("DL_MAX_DEPTH", 4, 0), ("DL_MAX_NODES", 200, 1)):
        try:
            value = int(environ.get(name, str(default)))
        except (TypeError, ValueError):
            raise ValueError(name + " must be an integer") from None
        if value < minimum or (name == "DL_PORT" and value > 65535):
            raise ValueError(name + " is out of range")
        numbers[name] = value
    return mode, numbers


def create_app(environ=None):
    environ = os.environ if environ is None else environ
    mode, numbers = _configuration(environ)
    if mode == "real":
        cache = environ.get("DL_CACHE_DIR", "./.cache")
        hub, papers = RealHubClient(cache), RealPaperClient(cache)
    else:
        hub = FakeHubClient(ROOT / "fixtures/hub_cards.json")
        papers = FakePaperClient(ROOT / "fixtures/papers.json")
    resolver = Resolver(hub, papers, max_depth=numbers["DL_MAX_DEPTH"], max_nodes=numbers["DL_MAX_NODES"])
    vault = FileVault(environ.get("DL_VAULT_PATH", "./vault/contacts.jsonl"))
    return App(resolver, vault, session_secret=environ.get("DL_SESSION_SECRET") or secrets.token_bytes(32))


def create_server(environ=None):
    environ = os.environ if environ is None else environ
    _, numbers = _configuration(environ)
    return ThreadingHTTPServer(("127.0.0.1", numbers["DL_PORT"]), handler(create_app(environ)))


def main():
    try:
        server = create_server()
    except (OSError, ValueError):
        raise SystemExit("DataLineage could not start. Check configuration, storage permissions and port availability.") from None
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
