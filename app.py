#!/usr/bin/env python3
"""Persistent, script-free DataLineage application."""
from collections import OrderedDict, deque
from datetime import datetime, timezone, timedelta
from email.parser import BytesParser
from email.policy import default
from html import escape
from http.server import BaseHTTPRequestHandler
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

from demo import LimitedThreadingHTTPServer
from datalineage import __version__
from datalineage.clients import FakeHubClient, RealHubClient
from datalineage.exports import cyclonedx, json_export, pdf_export
from datalineage.extract import parse_front_matter, upstream
from datalineage.flags import build_flags
from datalineage.licences import declared, detect, strictest
from datalineage.resolver import Edge, Flag, Node, Report, Resolver, report_from_dict
from datalineage.server import _template_pdf, _validate_manifest, parse_roots
from datalineage.template_draft import build_draft

ROOT = Path(__file__).resolve().parent
TOKEN = re.compile(r'^[A-Za-z0-9_-]{32}$')
RID = re.compile(r'^[0-9a-f]{32}$')
MAX_BODY = 1_100_000
MAX_FILE = 1_000_000
NOTICE = 'Operated by AI agents. Early version: keep your own copy of anything important. Do not enter personal data about anyone, access tokens or passwords, or confidential dataset contents (cards and IDs only). Nothing is charged.'
REPORT_NOTICE = 'Rule-based extraction from public dataset cards; no AI model is used. Not legal advice.'
CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
FULL = 'Workspace full — delete reports or export and start a new one.'
RETRY_LIMITS = {'TIME_LIMIT', 'HUB_RATE_LIMITED', 'HUB_REQUEST_BUDGET'}


def h(value):
    return escape(str(value), quote=True)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def page(title, body):
    return ('''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>%s — DataLineage</title><style>body{font:16px system-ui;color:#17243a;background:#eef3f8;margin:0}main,footer{max-width:1050px;margin:auto;padding:1rem}.notice{padding:1rem;background:#fff0b8}section,form{background:white;padding:1rem;margin:1rem 0}input,textarea,button{font:inherit;padding:.6rem;max-width:100%%;box-sizing:border-box}textarea{width:100%%}button,.button{display:inline-block;background:#173b70;color:white;padding:.7rem;margin:.2rem}.danger{background:#8b2020}.metric{font-size:3rem;font-weight:bold}table{width:100%%;border-collapse:collapse}td,th{text-align:left;padding:.5rem;border-bottom:1px solid #ccc}code,pre{white-space:pre-wrap;overflow-wrap:anywhere}.banner{border:2px solid #b98400}</style></head><body><div class="notice">%s <a rel="noreferrer" href="https://manykiln.com/privacy">Privacy</a></div><main>%s</main><footer>%s<p>Litigation/removal flag set is not populated yet.</p></footer></body></html>''' % (h(title), h(NOTICE), body, h(REPORT_NOTICE))).encode()


class NoForeignRedirect(HTTPRedirectHandler):
    def __init__(self, host='huggingface.co'):
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urlsplit(newurl)
        if p.scheme != 'https' or p.hostname != self.host or p.port not in (None, 443) or p.username or p.password:
            raise OSError('redirect refused')
        # Redirects are fetched explicitly by RealHubClient so each consumes a
        # request slot and shares the original deadline.
        return None


class Store:
    def __init__(self, root, cap_mb=200):
        self.root = Path(root)
        self.w, self.s = self.root / 'w', self.root / 's'
        self.cap = int(cap_mb) * 1024 * 1024
        self.max_workspaces = 3000
        self.guard = threading.RLock()
        for p in (self.root, self.w, self.s):
            p.mkdir(parents=True, exist_ok=True, mode=0o700)

    def lock(self, wh):
        # One storage transaction lock also protects global capacity checks.
        return self.guard

    def atomic(self, path, value):
        # Never recreate a parent: deletion must remain final for in-flight work.
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def usage(self, path=None):
        with self.guard:
            bases = [Path(path)] if path is not None else [self.w, self.s]
            return sum(p.stat().st_size for base in bases for p in base.rglob('*') if p.is_file())

    def create(self):
        with self.guard:
            self.sweep()
            dirs = [d for d in self.w.iterdir() if d.is_dir()]
            if len(dirs) >= self.max_workspaces:
                # Empty workspaces contain no user reports. Reclaim the oldest
                # reservation instead of letting empty reservations block users.
                empty = [d for d in dirs if not any((d / 'r').glob('*.json'))]
                if empty:
                    self.delete(min(empty, key=lambda d: (d / 'meta.json').stat().st_mtime))
                else:
                    raise OverflowError(FULL)
            if self.usage() + 512 > self.cap:
                raise OverflowError(FULL)
            token = secrets.token_urlsafe(24)
            d = self.w / digest(token)
            d.mkdir(mode=0o700)
            (d / 'r').mkdir(mode=0o700)
            self.atomic(d / 'meta.json', {'created': now(), 'last_used': now(), 'share_hash': None})
            return token

    def resolve(self, token):
        if not TOKEN.fullmatch(token):
            return None
        d = self.w / digest(token)
        with self.guard:
            return d if (d / 'meta.json').is_file() else None

    def shared(self, token):
        if not TOKEN.fullmatch(token):
            return None
        with self.guard:
            try:
                wh = (self.s / digest(token)).read_text().strip()
                d = self.w / wh
                meta = self.meta(d)
                return d if re.fullmatch(r'[0-9a-f]{64}', wh) and meta and meta.get('share_hash') == digest(token) else None
            except OSError:
                return None

    def meta(self, d, touch=False):
        with self.guard:
            try:
                value = json.loads((d / 'meta.json').read_text())
            except (OSError, ValueError):
                return None
            if touch:
                value['last_used'] = now()
                self.atomic(d / 'meta.json', value)
            return value

    def reports(self, d):
        with self.guard:
            out = []
            for p in (d / 'r').glob('*.json'):
                try:
                    out.append(json.loads(p.read_text()))
                except (OSError, ValueError):
                    pass
            return sorted(out, key=lambda x: x['report']['created_utc'], reverse=True)

    def save(self, d, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        with self.guard:
            if self.meta(d) is None:
                raise FileNotFoundError('Workspace was deleted')
            if len(list((d / 'r').glob('*.json'))) >= 50 or self.usage(d) + len(raw) > 5 * 1024 * 1024 or self.usage() + len(raw) > self.cap:
                raise OverflowError(FULL)
            self.atomic(d / 'r' / (value['report']['report_id'] + '.json'), value)
            self.meta(d, True)

    def sweep(self):
        with self.guard:
            cutoff = datetime.now(timezone.utc) - timedelta(days=90)
            for d in list(self.w.iterdir()):
                if not d.is_dir():
                    continue
                meta = self.meta(d)
                try:
                    expired = datetime.fromisoformat(meta['last_used']) < cutoff
                except (TypeError, KeyError, ValueError):
                    expired = True
                if expired:
                    self.delete(d)

    def delete(self, d):
        with self.guard:
            sh = (self.meta(d) or {}).get('share_hash')
            if sh and re.fullmatch(r'[0-9a-f]{64}', sh):
                (self.s / sh).unlink(missing_ok=True)
            shutil.rmtree(d, ignore_errors=True)


class State:
    def __init__(self, environ):
        self.env = environ
        self.host = environ.get('APP_HOST', '127.0.0.1')
        self.port = int(environ.get('PORT', '8080'))
        self.base = environ.get('PUBLIC_BASE_URL', 'http://127.0.0.1:' + str(self.port)).rstrip('/')
        p = urlsplit(self.base)
        if p.scheme not in {'http', 'https'} or not p.netloc or p.path or p.query or p.fragment or p.username or p.password:
            raise ValueError('invalid PUBLIC_BASE_URL')
        self.netloc = p.netloc.lower()
        self.store = Store(environ.get('DATA_DIR', './datalineage-data'), environ.get('APP_DATA_CAP_MB', '200'))
        self.proxy = environ.get('HTTPS_PROXY')
        self.rates = OrderedDict()
        self.rate_lock = threading.Lock()
        self.last_sweep = 0
        self.hub = None
        if self.proxy:
            opener = build_opener(ProxyHandler({'https': self.proxy}), NoForeignRedirect())
            self.hub = RealHubClient(self.store.root / 'hubcache', request_budget=20, minimum_interval=1, opener=opener, allowed_host='huggingface.co', cooldown=60)
        self.maintenance()

    def maintenance(self):
        with self.store.guard:
            if time.monotonic() - self.last_sweep >= 60 or not self.last_sweep:
                self.store.sweep()
                self.last_sweep = time.monotonic()

    def allow(self, identity):
        t = time.monotonic()
        with self.rate_lock:
            q = self.rates.setdefault(identity, deque())
            while q and q[0] <= t - 60:
                q.popleft()
            if len(q) >= 10:
                return False
            q.append(t)
            self.rates.move_to_end(identity)
            while len(self.rates) > 10000:
                self.rates.popitem(last=False)
            return True

    def resolver(self, hub=None):
        selected = self.hub if hub is None else hub
        if isinstance(selected, RealHubClient):
            selected = selected.session(deadline=time.monotonic() + 20, max_fetches=40)
        return Resolver(selected, papers=None, max_depth=4, max_nodes=200, time_budget=20)


def finish_report(report):
    known = sum(bool(n.resolved and (effective := strictest([n.declared] + n.detected)) and effective.category not in {'unknown', 'unlicensed'}) for n in report.nodes.values())
    pct = round(100 * known / len(report.nodes), 1) if report.nodes else 0.0
    report.coverage = {'nodes': len(report.nodes), 'licence_known': known, 'pct': pct, 'unresolved': sum(not n.resolved for n in report.nodes.values())}
    report.disclaimer = 'Automated provenance draft. Not legal advice and not a compliance certification. Based only on public dataset cards; upstream sources may be incomplete or wrong. Coverage: ' + str(pct) + '%. Review with qualified counsel before relying on it.'
    report.flags = build_flags(report, [])
    return report


def resume_card(state, saved_report):
    old = report_from_dict(saved_report)
    root = old.roots[0]
    node = old.nodes[root]
    edges = [edge for edge in old.edges if edge.parent == root]
    refs = list(dict.fromkeys(edge.child for edge in edges))
    child = state.resolver().resolve(refs, 3)
    for n in child.nodes.values():
        n.depth += 1
    report = Report(uuid.uuid4().hex, now(), [root], {root: node, **child.nodes}, edges + child.edges, [], {}, child.limits_hit, __version__, '')
    finish_report(report)
    report.flags.extend(f for f in old.flags if f.code == 'PAPER_REFERENCED_NOT_READ' and f.node == root)
    return report


def card_report(state, text, name):
    # The original bytes are represented by a digest only. Neither arbitrary
    # YAML fields nor the submitted display name are persisted.
    sha = hashlib.sha256(text.encode()).hexdigest()
    text = text.removeprefix('\ufeff')
    data = parse_front_matter(text)
    root = 'local:card-' + sha[:16]
    aliases = Resolver(None, papers=None).aliases
    hits = upstream(text, data, aliases, None, [])
    node = Node(root, 'Pasted dataset card', None, 'text', declared(data), detect(text), True, 0)
    edges = [Edge(root, target, evidence, confidence, True) for target, evidence, confidence in hits]
    seed = Report(uuid.uuid4().hex, now(), [root], {root: node}, edges, [], {}, [], __version__, '')
    for aid in dict.fromkeys(re.findall(r'(?:arxiv\.org/(?:abs|pdf)/|arXiv:\s*)(\d{4}\.\d{4,5})', text, re.I)):
        seed.flags.append(Flag('PAPER_REFERENCED_NOT_READ', 'info', root, 'Paper referenced, not read: https://arxiv.org/abs/' + aid, [root]))
    return resume_card(state, seed.asdict()), {'card_sha256': sha}


def report_page(report, draft, prefix, readonly=False):
    rows = ''.join('<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (h(n.id), h(n.declared.id or n.declared.raw or 'UNKNOWN'), h(', '.join(x.id or x.raw or 'UNKNOWN' for x in n.detected) or 'None'), h(n.resolved), h(n.modality)) for n in report.nodes.values())
    flags = ''.join('<li><strong>%s</strong>: %s<br><code>%s</code></li>' % (h(f.code), h(f.message), h(' → '.join(f.path))) for f in report.flags) or '<li>None</li>'
    edges = ''.join('<li><code>%s → %s</code> — %s, %s%s</li>' % (h(e.parent), h(e.child), h(e.evidence), h(e.confidence), '' if e.traversed else ', not expanded') for e in report.edges) or '<li>None found</li>'
    files = ' '.join('<a class="button" href="%s/%s">%s</a>' % (h(prefix), filename, label) for filename, label in [('report.json', 'Report JSON'), ('bom.json', 'CycloneDX 1.6'), ('report.pdf', 'Report PDF'), ('template.json', 'Template JSON'), ('template.pdf', 'Template PDF')])
    actions = ''
    if not readonly:
        actions = '<form method="post" action="%s/delete"><button class="danger">Delete report</button></form>' % h(prefix)
        if RETRY_LIMITS.intersection(report.limits_hit):
            actions += '<form method="post" action="%s"><button>Continue tracing</button></form>' % h(prefix.replace('/r/', '/rerun/'))
    return page('Report', '<p><a href="%s">← Workspace</a></p><h1>Report</h1><p class="metric">%s%%</p><p>%s</p><p><strong>Limits hit:</strong> %s</p><section><h2>Nodes</h2><table><tr><th>Node</th><th>Declared</th><th>Detected</th><th>Resolved</th><th>Modality</th></tr>%s</table></section><section><h2>Review flags and paths</h2><ul>%s</ul></section><section><h2>Upstream edges</h2><ul>%s</ul></section><section class="banner"><h2>DRAFT — Commission training-content summary</h2><pre>%s</pre></section><section>%s</section>%s' % (h(prefix.rsplit('/r/', 1)[0]), h(report.coverage['pct']), h(REPORT_NOTICE), h(', '.join(report.limits_hit) or 'None'), rows, flags, edges, h(json.dumps(draft, ensure_ascii=False, indent=2)), files, actions))


def workspace_page(state, d, base, readonly=False, share=None):
    items = []
    for saved in state.store.reports(d):
        r = saved['report']
        items.append('<li><a href="%s/r/%s">%s</a> — %s — %s%% coverage — %s flags%s</li>' % (h(base), r['report_id'], h(r['created_utc']), h(', '.join(r['roots'])), h(r['coverage']['pct']), len(r['flags']), ' — made-up data' if saved.get('sample') else ''))
    forms = ''
    if not readonly:
        forms = '''<p><strong>This link is the only key. It cannot be recovered.</strong> Bookmark it now.</p><p>Empty workspaces may be reclaimed when capacity is reached.</p><section><h2>Quick check</h2><form method="post" action="%s/check"><input name="id" required placeholder="owner/name"><button>Check one dataset</button></form></section><section><h2>Manifest</h2><form method="post" enctype="multipart/form-data" action="%s/manifest"><textarea name="text" rows="8" placeholder="One ID per line or JSON manifest"></textarea><input type="file" name="file" accept=".txt,.json"><button>Build report</button></form></section><section><h2>Dataset card</h2><form method="post" enctype="multipart/form-data" action="%s/card"><input name="name" placeholder="Optional name (not retained)"><textarea name="card" rows="10" placeholder="README.md with YAML front-matter"></textarea><input type="file" name="file" accept=".md,text/markdown"><button>Analyse card</button></form></section>''' % (h(base), h(base), h(base))
        forms += '<section><a class="button" href="%s/export">Export everything</a><form method="post" action="%s/share"><button>Create read-only share link</button></form><form method="post" action="%s/share/revoke"><button>Revoke share link</button></form><form method="post" action="%s/delete"><label><input type="checkbox" name="confirm" value="yes" required> Confirm deletion</label><button class="danger">Delete this workspace now</button></form></section>' % (h(base), h(base), h(base), h(base))
    disabled = '' if state.hub else '<p><strong>Live Hugging Face lookup unavailable; paste a card instead</strong></p>'
    return page('Workspace', '<h1>%s workspace</h1>' % ('Shared' if readonly else 'Private') + disabled + forms + '<section><h2>Saved reports</h2><ul>' + (''.join(items) or '<li>No reports yet.</li>') + '</ul></section>')


def parse_form(handler):
    if handler.headers.get('Transfer-Encoding'):
        raise ValueError
    length = int(handler.headers.get('Content-Length', '0'))
    if length < 0 or length > MAX_BODY:
        raise OverflowError
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise ValueError
    ctype = handler.headers.get('Content-Type', '')
    if ctype.startswith('application/x-www-form-urlencoded'):
        out = {k: v[-1] for k, v in parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=20).items()}
    elif ctype.startswith('multipart/form-data'):
        msg = BytesParser(policy=default).parsebytes(('Content-Type: ' + ctype + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + raw)
        out = {}
        for part in msg.iter_parts():
            name = part.get_param('name', header='content-disposition')
            data = part.get_payload(decode=True) or b''
            if len(data) > MAX_FILE:
                raise OverflowError
            if name:
                out[name] = data.decode('utf-8')
    else:
        raise ValueError
    if any(len(value.encode()) > MAX_FILE for value in out.values()):
        raise OverflowError
    return out


def set_roles(report, manifest):
    report.roles = {'hf:' + item['id'].removeprefix('hf:'): item['role'] for item in manifest.get('datasets', []) if isinstance(item.get('role'), str)}


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        timeout = 25
        server_version = 'DataLineage'
        sys_version = ''

        def log_message(self, *args):
            pass

        def headers_out(self, status, kind, length, extra=None):
            self.send_response(status)
            for key, value in {'Content-Type': kind, 'Content-Length': str(length), 'Referrer-Policy': 'same-origin', 'Cache-Control': 'no-store', 'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff', **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()

        def send(self, status, body, kind='text/html; charset=utf-8', extra=None):
            self.headers_out(status, kind, len(body), extra)
            self.wfile.write(body)

        def err(self, status, msg='Request could not be processed'):
            self.send(status, page(str(status), '<h1>%s</h1><p>Reference: %s</p>' % (h(msg), uuid.uuid4().hex)))

        def host_ok(self):
            return self.headers.get('Host', '').lower() == state.netloc

        def origin_ok(self):
            return self.headers.get('Origin', '') == state.base

        def redirect(self, to):
            self.send(303, b'', 'text/plain', {'Location': to})

        def client_key(self):
            peer = self.client_address[0]
            # Only an explicitly declared local TLS ingress may assert identity.
            if state.env.get('APP_HTTPS_TERMINATED', '').lower() in {'1', 'true', 'yes'}:
                try:
                    if ipaddress.ip_address(peer).is_loopback:
                        forwarded = self.headers.get('X-Forwarded-For', '').strip()
                        return str(ipaddress.ip_address(forwarded))
                except ValueError:
                    pass
            return peer

        def do_GET(self):
            if not self.host_ok():
                return self.err(403)
            state.maintenance()
            path = urlsplit(self.path).path
            if path == '/healthz':
                return self.send(200, b'ok', 'text/plain; charset=utf-8')
            if path == '/':
                return self.send(200, page('Home', '<h1>DataLineage</h1><p>Paste public Hugging Face dataset IDs or a dataset card to create a licence and upstream report, CycloneDX ML-BOM, and draft EU training-content summary.</p><p>No legal opinion; text datasets only; public cards only for live lookups.</p><form method="post" action="/w"><button>Start a workspace</button></form><form method="post" action="/w/sample"><button>Try with sample data</button></form>'))
            m = re.fullmatch(r'/(w|s)/([^/]+)(?:/(export|r/([0-9a-f]{32})(?:/(report\.json|bom\.json|report\.pdf|template\.json|template\.pdf))?))?', path)
            if not m:
                return self.err(404)
            private = m[1] == 'w'
            token, action, rid, filename = m[2], m[3], m[4], m[5]
            with state.store.guard:
                d = state.store.resolve(token) if private else state.store.shared(token)
                if not d or (action == 'export' and not private):
                    return self.err(404)
                state.store.meta(d, True)
                if action == 'export':
                    payload = {'schema': 'datalineage.workspace/0.1', 'exported_utc': now(), 'reports': state.store.reports(d)}
                    return self.send(200, json.dumps(payload, ensure_ascii=False, indent=2).encode(), 'application/json', {'Content-Disposition': 'attachment; filename="workspace.json"'})
                base = '/' + m[1] + '/' + token
                if not rid:
                    body = workspace_page(state, d, base, not private)
                    return self.send(200, body)
                try:
                    saved = json.loads((d / 'r' / (rid + '.json')).read_text())
                    report, draft = report_from_dict(saved['report']), saved['draft']
                except (OSError, ValueError, KeyError):
                    return self.err(404)
            if not filename:
                return self.send(200, report_page(report, draft, base + '/r/' + rid, not private))
            if filename == 'report.json':
                body, kind = json_export(report, draft), 'application/json'
            elif filename == 'bom.json':
                body, kind = cyclonedx(report), 'application/vnd.cyclonedx+json'
            elif filename == 'report.pdf':
                body, kind = pdf_export(report, draft), 'application/pdf'
            elif filename == 'template.json':
                body, kind = json.dumps(draft, ensure_ascii=False, indent=2).encode(), 'application/json'
            else:
                body, kind = _template_pdf(draft), 'application/pdf'
            return self.send(200, body, kind, {'Content-Disposition': 'attachment; filename="' + filename + '"'})

        def do_POST(self):
            # Absolute connection deadline also bounds slow form uploads. Hub
            # sessions separately bound all upstream work to twenty seconds.
            import socket
            def expire():
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(25, expire)
            timer.daemon = True
            timer.start()
            try:
                self.post()
            finally:
                timer.cancel()

        def post(self):
            if not self.host_ok() or not self.origin_ok():
                return self.err(403)
            state.maintenance()
            path = urlsplit(self.path).path
            try:
                form = parse_form(self)
            except OverflowError:
                return self.err(413)
            except (ValueError, UnicodeError, OSError):
                return self.err(400)
            try:
                if path in {'/w', '/w/sample'}:
                    if not state.allow(('create', self.client_key())):
                        return self.err(429, 'Too many workspace creations; wait a minute.')
                    token = state.store.create()
                    d = state.store.resolve(token)
                    if path.endswith('sample'):
                        hub = FakeHubClient(ROOT / 'fixtures/hub_cards.json')
                        for ident in ('testorg/mix-a', 'testorg/clean'):
                            report = Resolver(hub, papers=None).resolve([ident], 2)
                            draft = build_draft(report, {'id': ident})
                            state.store.save(d, {'input_kind': 'sample', 'input': {'id': ident}, 'roots': [ident], 'model': {}, 'report': report.asdict(), 'draft': draft, 'sample': True})
                    return self.redirect('/w/' + token)
                m = re.fullmatch(r'/w/([^/]+)/(check|manifest|card|share|share/revoke|delete|rerun/([0-9a-f]{32})|r/([0-9a-f]{32})/delete)', path)
                if not m:
                    return self.err(404)
                token, action = m[1], m[2]
                d = state.store.resolve(token)
                if not d:
                    return self.err(404)
                base = '/w/' + token
                # Mutations and metadata checks share the deletion lock. Long
                # network work happens outside it; save rechecks existence.
                with state.store.guard:
                    meta = state.store.meta(d)
                    if meta is None:
                        return self.err(404)
                    if action == 'delete':
                        if form.get('confirm') != 'yes':
                            return self.err(400)
                        state.store.delete(d)
                        return self.send(200, page('Deleted', '<h1>Deleted</h1><p>The workspace files were removed.</p>'))
                    if action in {'share', 'share/revoke'}:
                        old = meta.get('share_hash')
                        if old:
                            (state.store.s / old).unlink(missing_ok=True)
                        if action == 'share/revoke':
                            meta['share_hash'] = None
                            state.store.atomic(d / 'meta.json', meta)
                            return self.redirect(base)
                        st = secrets.token_urlsafe(24)
                        sh = digest(st)
                        fd = os.open(state.store.s / sh, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, 'w') as stream:
                            stream.write(d.name)
                        meta['share_hash'] = sh
                        state.store.atomic(d / 'meta.json', meta)
                        return self.send(200, page('Share link', '<h1>Read-only share link</h1><p><a href="%s/s/%s">%s/s/%s</a></p><p>Copy it now; the raw token is not stored.</p>' % (h(state.base), st, h(state.base), st)))
                    if action.startswith('r/'):
                        (d / 'r' / (m[4] + '.json')).unlink(missing_ok=True)
                        return self.redirect(base)
                if not state.allow(('run', d.name)):
                    return self.err(429, 'Too many report runs; wait a minute.')
                sample = False
                if action.startswith('rerun/'):
                    with state.store.guard:
                        saved = json.loads((d / 'r' / (m[3] + '.json')).read_text())
                    inp, kind = saved['input'], saved['input_kind']
                    if kind == 'card':
                        report = resume_card(state, saved['report'])
                    else:
                        if not state.hub and kind != 'sample':
                            return self.err(503, 'Live Hugging Face lookup unavailable; paste a card instead')
                        sample = kind == 'sample'
                        resolver = state.resolver(FakeHubClient(ROOT / 'fixtures/hub_cards.json')) if sample else state.resolver()
                        report = resolver.resolve(parse_roots(inp), 2 if kind in {'check', 'sample'} else 4)
                        set_roles(report, inp)
                elif action == 'card':
                    text = form.get('file') or form.get('card') or ''
                    if not text:
                        return self.err(400)
                    report, inp = card_report(state, text, form.get('name', ''))
                    kind = 'card'
                else:
                    if not state.hub:
                        return self.err(503, 'Live Hugging Face lookup unavailable; paste a card instead')
                    raw = (form.get('file') or form.get('text') or form.get('id') or '').removeprefix('\ufeff')
                    kind = action
                    if action == 'check':
                        inp = {'id': raw.strip()}
                    else:
                        try:
                            inp = json.loads(raw)
                        except json.JSONDecodeError:
                            inp = {'text': raw}
                        _validate_manifest(inp)
                    roots = parse_roots(inp)
                    if kind == 'check' and len(roots) != 1:
                        raise ValueError
                    report = state.resolver().resolve(roots, 2 if kind == 'check' else 4)
                    set_roles(report, inp)
                draft = build_draft(report, inp if kind != 'card' else {})
                state.store.save(d, {'input_kind': kind, 'input': inp, 'roots': report.roots, 'model': inp.get('model', {}), 'report': report.asdict(), 'draft': draft, 'sample': sample})
                return self.redirect(base + '/r/' + report.report_id)
            except FileNotFoundError:
                return self.err(404)
            except OverflowError:
                return self.err(507, FULL)
            except (ValueError, TypeError, KeyError, RecursionError):
                return self.err(400)
            except Exception:
                return self.err(500)
    return Handler


def create_server(environ=None):
    state = State(os.environ if environ is None else environ)
    server = LimitedThreadingHTTPServer((state.host, state.port), make_handler(state))
    server.state = state
    return server


def main():
    try:
        server = create_server()
    except (OSError, ValueError, PermissionError):
        raise SystemExit('DataLineage could not start. Check configuration, storage permissions and port availability.') from None
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
