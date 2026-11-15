#!/usr/bin/env python3
"""Hosted, fake-data-only DataLineage user-test demo."""

from collections import OrderedDict, deque
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from datalineage.clients import FakeHubClient, FakePaperClient
from datalineage.exports import cyclonedx, json_export, pdf_export
from datalineage.resolver import Resolver
from datalineage.server import _validate_manifest, parse_roots
from datalineage.template_draft import build_draft

ROOT = Path(__file__).resolve().parent
MAX_BODY = 64 * 1024
MAX_CONNECTIONS = 32
BANNER = "Demo operated by AI agents. Made-up data. Do not enter real personal data. Nothing is charged."
REPORT_PATH = re.compile(r"^/reports/([0-9a-f]{32})(?:/(report\.json|bom\.json|report\.pdf|template\.json))?$")
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"


def _html(value):
    return escape(str(value), quote=True)


def _page(title, content):
    return ("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>%s — DataLineage demo</title>
<style>
:root{font:16px system-ui;color:#17243a;background:#eef3f8}body{margin:0}.banner{background:#fff0b8;color:#422f00;padding:1rem;text-align:center;font-weight:700;border-bottom:2px solid #d6a900}main{max-width:900px;margin:auto;padding:2rem 1rem}section,form{background:white;padding:1.25rem;margin:1rem 0;border-radius:.75rem;box-shadow:0 2px 8px #16324b18}textarea,select,button{box-sizing:border-box;font:inherit;padding:.75rem;border:1px solid #91a0b4;border-radius:.4rem}textarea{width:100%%}button,.button{display:inline-block;background:#173b70;color:white;text-decoration:none;padding:.7rem 1rem;border:0;border-radius:.4rem;cursor:pointer}.links{display:flex;gap:.6rem;flex-wrap:wrap}.metric{font-size:1.5rem;color:#08705a}table{width:100%%;border-collapse:collapse}th,td{text-align:left;vertical-align:top;padding:.55rem;border-bottom:1px solid #d9e0e8}code{overflow-wrap:anywhere}.high{color:#982828}.medium{color:#805700}.muted{color:#526174}footer{max-width:900px;margin:auto;padding:1rem;color:#526174}
</style>
</head>
<body>
<div class="banner" role="note">%s</div>
<main>%s</main>
<footer>Automated provenance draft for user testing only. It is not legal advice or a compliance certification.</footer>
</body>
</html>""" % (_html(title), _html(BANNER), content)).encode("utf-8")


class LimitedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading server with a hard ceiling on live request threads."""

    daemon_threads = True
    block_on_close = False
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, server_address, request_handler, max_connections=MAX_CONNECTIONS):
        self.max_connections = max(1, int(max_connections))
        self._connection_slots = threading.BoundedSemaphore(self.max_connections)
        super().__init__(server_address, request_handler)

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class DemoState:
    """All demo state is process-local and disappears on restart."""

    def __init__(self, analyze_limit=8, analyze_window=60.0):
        hub = FakeHubClient(ROOT / "fixtures" / "hub_cards.json")
        papers = FakePaperClient(ROOT / "fixtures" / "papers.json")
        self.resolver = Resolver(hub, papers, max_depth=4, max_nodes=200)
        self.reports = OrderedDict()
        self.rates = OrderedDict()
        self.analyze_limit = max(1, int(analyze_limit))
        self.analyze_window = max(1.0, float(analyze_window))
        self.lock = threading.RLock()

    def allow_analyze(self, identity):
        now = time.monotonic()
        with self.lock:
            events = self.rates.setdefault(str(identity), deque())
            while events and events[0] <= now - self.analyze_window:
                events.popleft()
            if len(events) >= self.analyze_limit:
                return False
            events.append(now)
            self.rates.move_to_end(str(identity))
            while len(self.rates) > 10000:
                self.rates.popitem(last=False)
            return True

    def analyze(self, kind, raw):
        raw = raw.strip()
        if kind == "dataset":
            manifest = {"id": raw}
            depth = 2
        elif kind == "manifest":
            try:
                manifest = json.loads(raw)
            except json.JSONDecodeError:
                manifest = {"text": raw}
            depth = 4
        else:
            raise ValueError("invalid flow")
        _validate_manifest(manifest)
        roots = parse_roots(manifest)
        if kind == "dataset" and len(roots) != 1:
            raise ValueError("single check requires one dataset")
        report = self.resolver.resolve(roots, depth)
        draft = build_draft(report, manifest)
        with self.lock:
            self.reports[report.report_id] = (report, draft)
            self.reports.move_to_end(report.report_id)
            while len(self.reports) > 100:
                self.reports.popitem(last=False)
        return report

    def get(self, report_id):
        with self.lock:
            saved = self.reports.get(report_id)
            if saved:
                self.reports.move_to_end(report_id)
            return saved


def _home():
    sample_manifest = json.dumps({
        "model": {"name": "Fictional Assistant 1B", "provider": "Example Lab"},
        "datasets": [
            {"id": "testorg/mix-a", "role": "fine-tune"},
            {"id": "testorg/clean", "role": "evaluation"},
        ],
    }, indent=2)
    content = """
<h1>Trace a training dataset in under three minutes</h1>
<p>This isolated demo uses pre-loaded fictional dataset cards and fake clients. It never contacts Hugging Face, arXiv, payment services, or any other network service.</p>
<form method="post" action="/analyze">
<label for="kind"><strong>Flow</strong></label><br>
<select id="kind" name="kind">
<option value="dataset">Quick single-dataset check</option>
<option value="manifest">Full manifest report</option>
</select>
<p><label for="input"><strong>Dataset ID or manifest</strong></label></p>
<textarea id="input" name="input" rows="10" required>testorg/mix-a</textarea>
<p class="muted">The quick sample is ready: select Analyze. Other fixture IDs include <code>testorg/clean</code> and <code>testorg/image</code>. For a manifest, select the full report flow and paste JSON such as:</p>
<details><summary>Made-up manifest example</summary><pre>%s</pre></details>
<p><button type="submit">Analyze made-up data</button></p>
</form>
<section>
<h2>What you will see</h2>
<p>Declared and detected licences, review flags, recursive upstream lineage, coverage, a Commission-template draft, and downloadable JSON, CycloneDX, and PDF exports.</p>
</section>
""" % _html(sample_manifest)
    return _page("Start", content)


def _field_value(draft, name):
    for section in draft.get("sections", []):
        field = section.get("fields", {}).get(name)
        if isinstance(field, dict):
            return field.get("value")
    return None


def _report_page(report, draft):
    node_rows = []
    for node in report.nodes.values():
        declared = node.declared.id or node.declared.raw or "Unknown"
        detected = ", ".join(item.id or item.raw or "Unknown" for item in node.detected) or "None detected"
        node_rows.append("<tr><td><code>%s</code></td><td>%s</td><td>%s</td></tr>" % (
            _html(node.title), _html(declared), _html(detected)))
    flags = "".join(
        '<li class="%s"><strong>%s</strong>: %s<br><small>%s</small></li>' % (
            _html(flag.severity), _html(flag.code), _html(flag.message), _html(" → ".join(flag.path)))
        for flag in report.flags
    ) or "<li>No review flags in the made-up fixture.</li>"
    edges = "".join(
        "<li><code>%s → %s</code> — %s, %s%s</li>" % (
            _html(edge.parent), _html(edge.child), _html(edge.evidence), _html(edge.confidence),
            "" if edge.traversed else ", not expanded")
        for edge in report.edges
    ) or "<li>No upstream references found.</li>"
    limits = ", ".join(report.limits_hit) if report.limits_hit else "None"
    model_name = _field_value(draft, "model_name")
    provider = _field_value(draft, "provider")
    content = """
<p><a href="/">← Run another made-up example</a></p>
<h1>Provenance report</h1>
<section>
<p class="metric">%s%% licence coverage</p>
<table><thead><tr><th>Dataset</th><th>Declared licence</th><th>Detected in card</th></tr></thead><tbody>%s</tbody></table>
<p><strong>Limits reached:</strong> %s</p>
</section>
<section><h2>Flags for review</h2><ul>%s</ul></section>
<section><h2>Upstream tree</h2><ul>%s</ul></section>
<section>
<h2>Commission-template draft</h2>
<p><strong>Model:</strong> %s<br><strong>Provider:</strong> %s</p>
<p>%s</p>
</section>
<section>
<h2>Exports</h2>
<div class="links">
<a class="button" href="/reports/%s/report.json">Report JSON</a>
<a class="button" href="/reports/%s/bom.json">CycloneDX 1.6</a>
<a class="button" href="/reports/%s/report.pdf">PDF</a>
<a class="button" href="/reports/%s/template.json">Template JSON</a>
</div>
</section>
""" % (
        _html(report.coverage["pct"]), "".join(node_rows), _html(limits), flags, edges,
        _html(model_name), _html(provider), _html(draft.get("banner") or "Template structure verified."),
        report.report_id, report.report_id, report.report_id, report.report_id,
    )
    return _page("Report", content)


def _error_page(status):
    messages = {
        400: "The request was invalid. Use a listed fictional dataset ID or a valid manifest of at most 64 KB.",
        404: "That demo page or in-memory report was not found.",
        413: "The request body exceeds the 64 KB demo limit.",
        415: "Submit the form using application/x-www-form-urlencoded data.",
        429: "Too many analyses were requested from this address. Wait a minute and try again.",
    }
    return _page(str(status), "<h1>Request could not be processed</h1><p>%s</p><p><a href=\"/\">Return to the demo</a></p>" % _html(messages.get(status, "Please try again.")))


def make_handler(state):
    class DemoHandler(BaseHTTPRequestHandler):
        server_version = "DataLineageDemo/0.1"
        sys_version = ""
        timeout = 10

        def log_message(self, format, *args):
            pass

        def _client_ip(self):
            peer = self.client_address[0]
            try:
                is_loopback = ipaddress.ip_address(peer).is_loopback
            except ValueError:
                is_loopback = False
            if is_loopback:
                forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
                if forwarded:
                    try:
                        return str(ipaddress.ip_address(forwarded))
                    except ValueError:
                        pass
            return peer

        def _headers(self, status, content_type, length, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            if extra:
                for name, value in extra.items():
                    self.send_header(name, value)
            self.end_headers()

        def _send(self, status, body, content_type, extra=None):
            self._headers(status, content_type, len(body), extra)
            self.wfile.write(body)

        def _send_html(self, status, body):
            self._send(status, body, "text/html; charset=utf-8")

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
                return
            if path == "/":
                self._send_html(200, _home())
                return
            match = REPORT_PATH.fullmatch(path)
            if not match:
                self._send_html(404, _error_page(404))
                return
            saved = state.get(match.group(1))
            if not saved:
                self._send_html(404, _error_page(404))
                return
            report, draft = saved
            export_name = match.group(2)
            if export_name is None:
                self._send_html(200, _report_page(report, draft))
            elif export_name == "report.json":
                self._send(200, json_export(report, draft), "application/json; charset=utf-8", {
                    "Content-Disposition": "attachment; filename=datalineage-report.json"})
            elif export_name == "bom.json":
                self._send(200, cyclonedx(report), "application/vnd.cyclonedx+json", {
                    "Content-Disposition": "attachment; filename=datalineage-bom.json"})
            elif export_name == "report.pdf":
                self._send(200, pdf_export(report, draft), "application/pdf", {
                    "Content-Disposition": "attachment; filename=datalineage-report.pdf"})
            else:
                body = json.dumps(draft, ensure_ascii=False, indent=2).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8", {
                    "Content-Disposition": "attachment; filename=commission-template-draft.json"})

        def do_POST(self):
            if urlsplit(self.path).path != "/analyze":
                self._send_html(404, _error_page(404))
                return
            if self.headers.get("Transfer-Encoding"):
                self._send_html(400, _error_page(400))
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send_html(400, _error_page(400))
                return
            if length < 0:
                self._send_html(400, _error_page(400))
                return
            if length > MAX_BODY:
                self._send_html(413, _error_page(413))
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/x-www-form-urlencoded":
                self._send_html(415, _error_page(415))
                return
            if not state.allow_analyze(self._client_ip()):
                self._send_html(429, _error_page(429))
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                self._send_html(400, _error_page(400))
                return
            try:
                form = parse_qs(raw.decode("utf-8"), strict_parsing=True, max_num_fields=4)
                kind = form["kind"][0]
                value = form["input"][0]
                if len(form["kind"]) != 1 or len(form["input"]) != 1:
                    raise ValueError
                report = state.analyze(kind, value)
            except (KeyError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, RecursionError):
                self._send_html(400, _error_page(400))
                return
            self._send(303, b"", "text/plain; charset=utf-8", {"Location": "/reports/" + report.report_id})

    return DemoHandler


def create_server(port=None, state=None):
    if port is None:
        try:
            port = int(os.environ.get("PORT", "8080"))
        except ValueError:
            raise ValueError("PORT must be an integer") from None
    if not 0 <= port <= 65535:
        raise ValueError("PORT is out of range")
    return LimitedThreadingHTTPServer(("0.0.0.0", port), make_handler(state or DemoState()))


def main():
    try:
        server = create_server()
    except (OSError, ValueError):
        raise SystemExit("DataLineage demo could not start. Check PORT and filesystem access.") from None
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
