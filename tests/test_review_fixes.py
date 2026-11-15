"""Regression coverage for the external usable-app review.

No live network, browser, or Hugging Face account is required by these tests.
"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import app
from datalineage.clients import FakeHubClient, RealHubClient
from datalineage.resolver import Resolver
from tests.test_app import AppTests


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.body[:size]


def document(ident):
    return {'id': ident, 'card_text': '', 'card_data': {'license': 'mit'}, 'tags': [], 'partial': False, '_limits_hit': []}


class ReviewHTTPTests(AppTests):
    def test_same_origin_policy_and_external_link_privacy(self):
        status, headers, body = self.req('GET', '/')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Referrer-Policy'], 'same-origin')
        self.assertIn(b'rel="noreferrer" href="https://manykiln.com/privacy"', body)
        self.workspace()
        self.assertFalse(self.server.RequestHandlerClass.origin_ok(type('H', (), {'headers': {'Origin': 'null'}})()))

    def test_export_contains_every_report_and_is_private(self):
        w = self.workspace(True)
        status, headers, body = self.req('GET', w + '/export')
        self.assertEqual(status, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        value = json.loads(body)
        self.assertEqual(len(value['reports']), 2)
        self.assertNotIn(w.split('/')[-1], body.decode())
        self.assertEqual(self.req('GET', '/s/' + w.split('/')[-1] + '/export')[0], 404)
        self.assertEqual(self.req('POST', w + '/export', {})[0], 404)

    def test_private_card_yaml_and_name_are_not_retained(self):
        w = self.workspace()
        card = '---\nlicense: mit\ndescription: SECRET_DESCRIPTION\nconfigs: SECRET_CONFIG\nfeatures: SECRET_FEATURE\nsource_datasets: [org/upstream]\n---\nSECRET_BODY'
        status, headers, _ = self.req('POST', w + '/card', {'card': card, 'name': 'SECRET_NAME'})
        self.assertEqual(status, 303)
        exported = self.req('GET', w + '/export')[2]
        disk = b''.join(p.read_bytes() for p in Path(self.tmp.name).rglob('*') if p.is_file())
        for secret in ('SECRET_DESCRIPTION', 'SECRET_CONFIG', 'SECRET_FEATURE', 'SECRET_BODY', 'SECRET_NAME'):
            self.assertNotIn(secret.encode(), disk)
            self.assertNotIn(secret.encode(), exported)
        saved = json.loads(exported)['reports'][0]
        self.assertEqual(set(saved['input']), {'card_sha256'})
        self.assertEqual(saved['report']['nodes'][saved['report']['roots'][0]]['declared']['id'], 'MIT')

    def test_run_budget_is_per_workspace_and_never_blocks_delete(self):
        first, second = self.workspace(), self.workspace()
        d = self.server.state.store.resolve(first.split('/')[-1])
        for _ in range(10):
            self.assertTrue(self.server.state.allow(('run', d.name)))
        self.assertEqual(self.req('POST', first + '/card', {'card': 'card'})[0], 429)
        self.assertEqual(self.req('POST', second + '/card', {'card': 'card'})[0], 303)
        self.assertEqual(self.req('POST', first + '/share', {})[0], 200)
        self.assertEqual(self.req('POST', first + '/delete', {'confirm': 'yes'})[0], 200)

    def test_view_refreshes_last_used(self):
        w = self.workspace()
        store = self.server.state.store
        d = store.resolve(w.split('/')[-1])
        meta = store.meta(d)
        meta['last_used'] = '2020-01-01T00:00:00+00:00'
        store.atomic(d / 'meta.json', meta)
        self.assertEqual(self.req('GET', w)[0], 200)
        self.assertNotEqual(store.meta(d)['last_used'], meta['last_used'])
        store.sweep()
        self.assertIsNotNone(store.resolve(w.split('/')[-1]))

    def test_card_bom_and_offline_upstream_placeholder(self):
        w = self.workspace()
        status, headers, _ = self.req('POST', w + '/card', {'card': '\ufeff---\nlicense: mit\nsource_datasets: [org/source]\n---\n'})
        self.assertEqual(status, 303)
        value = json.loads(self.req('GET', headers['Location'] + '/report.json')[2])
        self.assertEqual(value['nodes'][value['roots'][0]]['declared']['id'], 'MIT')
        self.assertFalse(value['nodes']['hf:org/source']['resolved'])
        flag = next(f for f in value['flags'] if f['code'] == 'UNRESOLVED_UPSTREAM')
        self.assertEqual(flag['path'], [value['roots'][0], 'hf:org/source'])

    def test_card_continue_uses_extracted_graph(self):
        w = self.workspace()
        self.server.state.hub = FakeHubClient({'org/source': {'card_text': '', 'card_data': {}, 'tags': [], 'partial': True, '_limits_hit': ['TIME_LIMIT']}})
        status, headers, _ = self.req('POST', w + '/card', {'card': '---\nlicense: mit\nsource_datasets: [org/source]\n---\nPRIVATE_BODY'})
        self.assertEqual(status, 303)
        location = headers['Location']
        self.assertIn(b'Continue tracing', self.req('GET', location)[2])
        self.server.state.hub = FakeHubClient({'org/source': document('org/source')})
        status, headers, _ = self.req('POST', location.replace('/r/', '/rerun/'), {})
        self.assertEqual(status, 303)
        report = json.loads(self.req('GET', headers['Location'] + '/report.json')[2])
        self.assertTrue(report['nodes']['hf:org/source']['resolved'])
        self.assertNotIn('TIME_LIMIT', report['limits_hit'])

    def test_manifest_roles_survive_save_and_rerun(self):
        self.server.state.hub = FakeHubClient({'org/data': document('org/data')})
        w = self.workspace()
        manifest = {'datasets': [{'id': 'org/data', 'role': 'fine-tune'}]}
        status, headers, _ = self.req('POST', w + '/manifest', {'text': json.dumps(manifest)})
        self.assertEqual(status, 303)
        self.assertEqual(self.req('POST', headers['Location'].replace('/r/', '/rerun/'), {})[0], 303)
        saved = json.loads(self.req('GET', w + '/export')[2])['reports']
        self.assertTrue(all(x['report']['roles'] == {'hf:org/data': 'fine-tune'} for x in saved))

    def test_budget_limit_offers_continue(self):
        report = Resolver(FakeHubClient({'org/data': document('org/data')})).resolve(['org/data'])
        report.limits_hit = ['HUB_REQUEST_BUDGET']
        self.assertIn(b'Continue tracing', app.report_page(report, {}, '/w/token/r/' + report.report_id))
        self.assertNotIn(b'<form', app.report_page(report, {}, '/s/token/r/' + report.report_id, True))


class ReviewStoreTests(unittest.TestCase):
    def test_deleted_workspace_cannot_be_resurrected_by_pending_save(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = app.Store(temporary)
            token = store.create()
            d = store.resolve(token)
            started, resume = threading.Event(), threading.Event()
            errors = []
            def pending():
                started.set()
                resume.wait(2)
                try:
                    store.save(d, {'report': {'report_id': 'a' * 32}})
                except FileNotFoundError:
                    errors.append('deleted')
            thread = threading.Thread(target=pending)
            thread.start()
            self.assertTrue(started.wait(2))
            store.delete(d)
            resume.set()
            thread.join(2)
            self.assertEqual(errors, ['deleted'])
            self.assertFalse(d.exists())
            self.assertIsNone(store.resolve(token))

    def test_public_cache_does_not_consume_workspace_capacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = app.Store(temporary, 1)
            cache = Path(temporary) / 'hubcache'
            cache.mkdir()
            (cache / 'large').write_bytes(b'x' * (2 * 1024 * 1024))
            token = store.create()
            store.save(store.resolve(token), {'report': {'report_id': 'a' * 32}})
            self.assertLess(store.usage(), 1024)

    def test_empty_workspace_flood_reclaims_only_empty_reservations(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = app.Store(temporary)
            store.max_workspaces = 2
            used = store.create()
            store.save(store.resolve(used), {'report': {'report_id': 'a' * 32}})
            empty = store.create()
            fresh = store.create()
            self.assertIsNotNone(store.resolve(used))
            self.assertIsNone(store.resolve(empty))
            self.assertIsNotNone(store.resolve(fresh))

    def test_runtime_maintenance_sweeps_without_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = app.State({'DATA_DIR': temporary})
            token = state.store.create()
            d = state.store.resolve(token)
            meta = state.store.meta(d)
            meta['last_used'] = '2020-01-01T00:00:00+00:00'
            state.store.atomic(d / 'meta.json', meta)
            state.last_sweep = 0
            state.maintenance()
            self.assertIsNone(state.store.resolve(token))


class ReviewHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = RealHubClient(self.tmp.name, minimum_interval=0)
        self.addCleanup(setattr, RealHubClient, '_global_stop', 0.0)
        self.addCleanup(setattr, RealHubClient, '_global_last', 0.0)

    def test_cache_cap_evicts_oldest_mtime_and_prunes_expired(self):
        self.client.cache_cap = 500   # old+new+large = 550 bytes: over the cap, so the oldest must go
        self.client._put('org/old', document('org/old'))
        os.utime(self.client._path('org/old'), (time.time() - 20, time.time() - 20))
        self.client._put('org/new', document('org/new'))
        value = document('org/large')
        value['card_text'] = 'x' * 200
        self.client._put('org/large', value)
        self.assertFalse(self.client._path('org/old').exists())
        self.assertLessEqual(sum(p.stat().st_size for p in self.client.cache.iterdir()), 500)
        self.client.ttl = 1
        for p in self.client.cache.iterdir():
            os.utime(p, (time.time() - 10, time.time() - 10))
        self.client._put('org/fresh', document('org/fresh'))
        self.assertEqual(len(list(self.client.cache.glob('*.json'))), 1)

    def test_metadata_without_readme_is_complete_and_cached(self):
        missing = urllib.error.HTTPError('https://huggingface.co/x', 404, 'missing', {}, None)
        with patch('urllib.request.urlopen', side_effect=[Response(b'{"cardData":{"license":"mit"}}'), missing]) as opened:
            first = self.client.get_dataset('org/data')
            second = self.client.get_dataset('org/data')
        self.assertFalse(first['partial'])
        self.assertEqual(first, second)
        self.assertEqual(opened.call_count, 2)
        self.assertNotIn('HUB_NOT_FOUND', first['_limits_hit'])

    def test_forty_fetch_budget_is_shared_across_datasets(self):
        session = self.client.session(max_fetches=40)
        with patch('urllib.request.urlopen', side_effect=lambda *a, **k: Response(b'{}')) as opened:
            for i in range(21):
                result = session.get_dataset('org/data' + str(i))
        self.assertEqual(opened.call_count, 40)
        self.assertIn('HUB_REQUEST_BUDGET', result['_limits_hit'])

    def test_expired_shared_deadline_stops_all_network_calls(self):
        session = self.client.session(deadline=time.monotonic() - 1)
        with patch('urllib.request.urlopen') as opened:
            result = session.get_dataset('org/data')
        opened.assert_not_called()
        self.assertIn('TIME_LIMIT', result['_limits_hit'])

    def test_429_stops_other_sessions_for_sixty_seconds(self):
        self.client.cooldown = 60
        error = urllib.error.HTTPError('https://huggingface.co/x', 429, 'slow down', {}, None)
        started = time.monotonic()
        with patch('urllib.request.urlopen', side_effect=error) as opened:
            first = self.client.session().get_dataset('org/first')
            second = self.client.session().get_dataset('org/second')
        self.assertEqual(opened.call_count, 1)
        self.assertGreaterEqual(RealHubClient._global_stop, started + 60)
        self.assertIn('HUB_RATE_LIMITED', first['_limits_hit'])
        self.assertIn('HUB_RATE_LIMITED', second['_limits_hit'])

    def test_partial_endpoint_progress_is_reused_by_continue(self):
        with patch('urllib.request.urlopen', return_value=Response(b'{"cardData":{"license":"mit"}}')) as opened:
            partial = self.client.session(max_fetches=1).get_dataset('org/data')
        self.assertEqual(opened.call_count, 1)
        self.assertTrue(partial['partial'])
        with patch('urllib.request.urlopen', return_value=Response(b'---\nlicense: mit\n---')) as opened:
            complete = self.client.session(max_fetches=1).get_dataset('org/data')
        self.assertEqual(opened.call_count, 1)
        self.assertIn('/raw/main/README.md', opened.call_args.args[0].full_url)
        self.assertFalse(complete['partial'])
        with patch('urllib.request.urlopen') as opened:
            self.client.session(max_fetches=0).get_dataset('org/data')
        opened.assert_not_called()

    def test_contended_throttle_reports_resumable_time_limit(self):
        self.client.minimum_interval = 1
        RealHubClient._global_last = time.monotonic() + 30
        with patch('urllib.request.urlopen') as opened:
            result = self.client.session(deadline=time.monotonic() + .1).get_dataset('org/data')
        opened.assert_not_called()
        self.assertIn('TIME_LIMIT', result['_limits_hit'])


if __name__ == '__main__':
    unittest.main()
