import builtins
import io
import json
import logging
import unittest
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import patch

from datalineage import server
from datalineage.clients import FakeHubClient, FakePaperClient
from datalineage.logsafe import RedactingFilter, get_logger
from datalineage.template_draft import UNKNOWN
from datalineage.vault import MemoryVault


class FakeConnection:
    """Exercise BaseHTTPRequestHandler's real HTTP parsing without sockets."""

    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = bytearray()

    def settimeout(self, timeout):
        pass

    def makefile(self, mode, buffering=-1):
        if mode != 'rb':
            raise AssertionError('Unexpected stream mode')
        return self.input

    def sendall(self, data):
        self.output.extend(data)


def no_request_import(original):
    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'urllib.request' or (name == 'urllib' and 'request' in (fromlist or ())):
            raise AssertionError('Fake demo must not import urllib.request')
        return original(name, globals, locals, fromlist, level)
    return guarded


class DemoEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch('builtins.__import__', side_effect=no_request_import(builtins.__import__))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.socket_guard = patch('socket.socket', side_effect=AssertionError('No sockets in demo tests'))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.vault = MemoryVault()
        with patch.object(server, 'FileVault', return_value=self.vault):
            self.app = server.create_app({})
        self.handler = server.handler(self.app)
        self.cookie = None

    def request(self, path, body=None, method='POST', headers=None, authenticated=True):
        payload = json.dumps(body).encode('utf-8') if body is not None else b''
        supplied = {
            'Host': '127.0.0.1:8765',
            'Origin': 'http://127.0.0.1:8765',
            'Content-Type': 'application/json',
            'Content-Length': str(len(payload)),
            'Connection': 'close',
        }
        if authenticated and self.cookie:
            supplied['Cookie'] = self.cookie
        supplied.update(headers or {})
        head = method + ' ' + path + ' HTTP/1.1\r\n'
        head += ''.join(key + ': ' + value + '\r\n' for key, value in supplied.items())
        connection = FakeConnection(head.encode('ascii') + b'\r\n' + payload)
        self.handler(connection, ('127.0.0.1', 12345), SimpleNamespace(server_port=8765))
        raw_head, data = bytes(connection.output).split(b'\r\n\r\n', 1)
        status_line, raw_headers = raw_head.split(b'\r\n', 1)
        response_headers = BytesParser().parsebytes(raw_headers + b'\r\n\r\n')
        self.assertEqual(int(response_headers['Content-Length']), len(data))
        if response_headers.get('Set-Cookie'):
            self.cookie = response_headers['Set-Cookie'].split(';', 1)[0]
        return int(status_line.split()[1]), response_headers, data

    def ok(self, path, body):
        status, _, data = self.request(path, body)
        self.assertEqual(status, 200, data)
        return json.loads(data)

    def add_chain(self):
        for depth in range(6):
            self.app.resolver.hub.data['fictional/node' + str(depth)] = {
                'card_text': 'Licence: research purposes only. arXiv: 2401.00001' if depth == 0 else '',
                'card_data': {
                    'license': 'mit' if depth != 2 else 'cc-by-nc-4.0',
                    'source_datasets': ['fictional/node' + str(depth + 1)] if depth < 5 else [],
                },
                'tags': ['modality:text'],
            }

    def assert_pdf(self, data):
        self.assertTrue(data.startswith(b'%PDF-1.4'))
        self.assertTrue(data.rstrip().endswith(b'%%EOF'))
        offset = int(data.split(b'startxref\n')[1].splitlines()[0])
        self.assertEqual(data[offset:offset + 4], b'xref')
        self.assertIn(b'UNKNOWN \x97 provider must supply', data)

    def test_full_core_flow_with_fixture_clients(self):
        self.assertIsInstance(self.app.resolver.hub, FakeHubClient)
        self.assertIsInstance(self.app.resolver.papers, FakePaperClient)
        for path, content_type in (('/', 'text/html'), ('/static/app.js', 'text/javascript'), ('/static/style.css', 'text/css')):
            status, headers, data = self.request(path, method='GET')
            self.assertEqual(status, 200)
            self.assertTrue(headers['Content-Type'].startswith(content_type))
            self.assertTrue(data)
            self.assertIn("default-src 'self'", headers['Content-Security-Policy'])

        demo = self.ok('/api/check', {'id': 'testorg/mix-a'})
        self.assertEqual(demo['nodes']['hf:testorg/mix-a']['declared']['id'], 'Apache-2.0')
        self.assertIn('NC_UPSTREAM', {flag['code'] for flag in demo['flags']})
        self.assertGreater(demo['coverage']['pct'], 0)

        self.add_chain()
        self.app.resolver.hub.calls.clear()
        free = self.ok('/api/check', {'id': 'fictional/node0', 'depth': 99})
        self.assertIn('DEPTH_LIMIT', free['limits_hit'])
        self.assertEqual(self.app.resolver.hub.calls, ['fictional/node0', 'fictional/node1', 'fictional/node2'])
        self.assertFalse(free['nodes']['hf:fictional/node3']['resolved'])
        self.assertTrue(all(node['depth'] <= 2 for node in free['nodes'].values() if node['resolved']))
        self.assertTrue(any(edge['evidence'] == 'paper_abstract' for edge in free['edges']))
        self.assertIn('2401.00001', self.app.resolver.papers.calls)
        self.assertIn('DECLARED_DETECTED_MISMATCH', {flag['code'] for flag in free['flags']})
        self.assertTrue(free['nodes']['hf:fictional/node0']['detected'])

        self.app.resolver.hub.calls.clear()
        report = self.ok('/api/manifest', {
            'model': {'name': 'fictional-model', 'provider': 'Fictional Lab'},
            'datasets': [{'id': 'fictional/node0', 'role': 'fine-tune'}, {'id': 'testorg/clean'}],
        })
        self.assertEqual(len(report['roots']), 2)
        self.assertIn('fictional/node4', self.app.resolver.hub.calls)
        self.assertNotIn('fictional/node5', self.app.resolver.hub.calls)
        self.assertIn('DEPTH_LIMIT', report['limits_hit'])
        self.assertEqual(report['roles']['hf:fictional/node0'], 'fine-tune')
        self.assertEqual(len(self.app.resolver.hub.calls), len(set(self.app.resolver.hub.calls)))
        report_id = report['report_id']
        exported = self.ok('/api/report', {'report_id': report_id, 'format': 'json'})
        self.assertEqual(exported['schema'], 'datalineage.report/0.1')
        self.assertEqual(exported['coverage'], report['coverage'])
        self.assertEqual(exported['roots'], report['roots'])
        draft = self.ok('/api/report', {'report_id': report_id, 'format': 'template-json'})
        self.assertEqual(draft, exported['template_draft'])
        self.assertTrue(draft['banner'].startswith('DRAFT'))
        general = next(section for section in draft['sections'] if section['id'] == '1')['fields']
        self.assertEqual(general['model_name']['value'], 'fictional-model')
        self.assertEqual(general['provider']['status'], 'provided')
        self.assertEqual(general['base_model']['value'], UNKNOWN)
        processing = next(section for section in draft['sections'] if section['id'] == '3')['fields']
        self.assertTrue(all(field['value'] == UNKNOWN for field in processing.values()))
        bom = self.ok('/api/report', {'report_id': report_id, 'format': 'cyclonedx'})
        self.assertEqual(bom['specVersion'], '1.6')
        self.assertTrue(all(component['type'] == 'data' for component in bom['components']))
        self.assertEqual({component['bom-ref'] for component in bom['components']}, set(report['nodes']))
        for kind in ('pdf', 'template-pdf'):
            status, headers, data = self.request('/api/report', {'report_id': report_id, 'format': kind})
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Type'], 'application/pdf')
            self.assert_pdf(data)
        status, _, _ = self.request('/api/report', {'report_id': report_id}, authenticated=False)
        self.assertEqual(status, 404)
        other_cookie = 'dl_sid=' + self.app.sign_sid('1' * 32)
        status, _, _ = self.request('/api/report', {'report_id': report_id}, headers={'Cookie': other_cookie})
        self.assertEqual(status, 404)

        text_report = self.ok('/api/manifest', {'text': 'testorg/clean\ntestorg/mix-a\n'})
        self.assertEqual(text_report['roots'], ['hf:testorg/clean', 'hf:testorg/mix-a'])
        stream = io.StringIO()
        capture = logging.StreamHandler(stream)
        capture.addFilter(RedactingFilter())
        logger = get_logger()
        logger.addHandler(capture)
        try:
            self.assertEqual(self.ok('/api/signup', {'email': 'person@example.test', 'provider_answer': 'request'}), {'ok': True})
            self.assertEqual(self.ok('/api/pilot', {'contact': 'Private Contact', 'notes': 'Private Note'}), {'ok': True})
        finally:
            logger.removeHandler(capture)
        self.assertEqual(self.vault.count('signup'), 1)
        self.assertEqual(self.vault.count('pilot'), 1)
        self.assertFalse(hasattr(self.vault, 'get'))
        self.assertFalse(hasattr(self.vault, 'read'))
        self.assertEqual(self.app.metrics.snapshot(), {'checker_uses': 1, 'signups': 1, 'pilot_requests': 1})
        for private in ('person@example.test', 'Private Contact', 'Private Note'):
            self.assertNotIn(private, stream.getvalue())
            self.assertNotIn(private, json.dumps(self.app.metrics.snapshot()))

    def test_free_default_and_configured_limits(self):
        self.add_chain()
        report = self.ok('/api/check', {'id': 'fictional/node0'})
        self.assertNotIn('fictional/node3', self.app.resolver.hub.calls)
        self.assertIn('DEPTH_LIMIT', report['limits_hit'])
        self.app.resolver.max_depth = 1
        self.app.resolver.hub.calls.clear()
        self.ok('/api/manifest', {'text': 'fictional/node0'})
        self.assertEqual(self.app.resolver.hub.calls, ['fictional/node0', 'fictional/node1'])
        self.app.resolver.max_nodes = 1
        self.app.resolver.hub.calls.clear()
        limited = self.ok('/api/manifest', {'text': 'fictional/node0'})
        self.assertEqual(self.app.resolver.hub.calls, ['fictional/node0'])
        self.assertIn('NODE_LIMIT', limited['limits_hit'])

    def test_invalid_inputs_and_request_guards(self):
        for body in ({'text': 'testorg/clean\ntestorg/mix-a'}, {'id': '../bad'}, {'id': 'testorg/clean', 'depth': '4'}, {'id': 'testorg/clean', 'depth': -1}, {'id': 'testorg/clean', 'depth': True}):
            status, _, _ = self.request('/api/check', body)
            self.assertEqual(status, 400)
        for body in ({'datasets': [{'id': 'testorg/clean'}] * 51}, {'datasets': [{'id': 'testorg/clean'}, None]}):
            status, _, _ = self.request('/api/manifest', body)
            self.assertEqual(status, 400)
        for headers in ({'Host': 'evil.example'}, {'Origin': 'http://evil.example'}, {'Origin': ''}):
            status, _, _ = self.request('/api/check', {'id': 'testorg/clean'}, headers=headers)
            self.assertEqual(status, 403)


class LauncherTests(unittest.TestCase):
    def test_default_and_custom_loopback_bind(self):
        for environment, port in (({}, 8765), ({'DL_PORT': '9876'}, 9876)):
            with self.subTest(environment=environment), patch.object(server, 'FileVault', return_value=MemoryVault()), patch.object(server, 'ThreadingHTTPServer') as http_server:
                instance = server.create_server(environment)
                self.assertIs(instance, http_server.return_value)
                self.assertEqual(http_server.call_args.args[0], ('127.0.0.1', port))
                self.assertTrue(issubclass(http_server.call_args.args[1], server.BaseHTTPRequestHandler))

    def test_fake_defaults_and_environment_configuration(self):
        with patch.object(server, 'FileVault', return_value=MemoryVault()) as vault:
            app = server.create_app({})
            self.assertIsInstance(app.resolver.hub, FakeHubClient)
            self.assertIsInstance(app.resolver.papers, FakePaperClient)
            self.assertEqual((app.resolver.max_depth, app.resolver.max_nodes), (4, 200))
            vault.assert_called_once_with('./vault/contacts.jsonl')
        environment = {'DL_MODE': 'real', 'DL_CACHE_DIR': 'custom-cache', 'DL_VAULT_PATH': 'private/contacts.jsonl', 'DL_MAX_DEPTH': '1', 'DL_MAX_NODES': '7'}
        with patch.object(server, 'RealHubClient') as hub, patch.object(server, 'RealPaperClient') as papers, patch.object(server, 'FileVault', return_value=MemoryVault()) as vault:
            app = server.create_app(environment)
            hub.assert_called_once_with('custom-cache')
            papers.assert_called_once_with('custom-cache')
            vault.assert_called_once_with('private/contacts.jsonl')
            self.assertIs(app.resolver.hub, hub.return_value)
            self.assertIs(app.resolver.papers, papers.return_value)
            self.assertEqual((app.resolver.max_depth, app.resolver.max_nodes), (1, 7))

    def test_invalid_configuration_fails_before_resources_are_created(self):
        for environment in ({'DL_MODE': 'REAL'}, {'DL_PORT': '0'}, {'DL_PORT': '65536'}, {'DL_PORT': 'bad'}, {'DL_MAX_DEPTH': '-1'}, {'DL_MAX_NODES': '0'}):
            with self.subTest(environment=environment), patch.object(server, 'FileVault') as vault, patch.object(server, 'ThreadingHTTPServer') as http_server:
                with self.assertRaises(ValueError):
                    server.create_server(environment)
                vault.assert_not_called()
                http_server.assert_not_called()

    def test_main_serves_and_closes_on_interrupt(self):
        with patch.object(server, 'create_server') as create:
            create.return_value.serve_forever.side_effect = KeyboardInterrupt
            server.main()
            create.assert_called_once_with()
            create.return_value.serve_forever.assert_called_once_with()
            create.return_value.server_close.assert_called_once_with()

    def test_main_closes_on_unexpected_failure(self):
        with patch.object(server, 'create_server') as create:
            create.return_value.serve_forever.side_effect = RuntimeError('test failure')
            with self.assertRaises(RuntimeError):
                server.main()
            create.return_value.server_close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
