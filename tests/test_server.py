import http.client
import io
import json
import logging
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from datalineage.clients import FakeHubClient
from datalineage.logsafe import RedactingFilter, get_logger
from datalineage.resolver import Resolver
from datalineage.server import App, handler
from datalineage.vault import MemoryVault

ROOT = Path(__file__).resolve().parents[1]


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = App(Resolver(FakeHubClient(ROOT / "fixtures/hub_cards.json")), MemoryVault())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(cls.app))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        payload = json.dumps(body).encode() if body is not None else None
        supplied = {"Content-Type": "application/json", "X-DataLineage-Request": "1"} if payload else {}
        supplied.update(headers or {})
        conn.request(method, path, payload, supplied)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def test_check_and_headers(self):
        response, data = self.request("POST", "/api/check", {"id": "testorg/mix-a"})
        self.assertEqual(response.status, 200)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertIn("coverage", json.loads(data))

    def test_bad_id(self):
        response, _ = self.request("POST", "/api/check", {"id": "../bad"})
        self.assertEqual(response.status, 400)

    def test_signup_privacy(self):
        stream = io.StringIO()
        capture = logging.StreamHandler(stream)
        capture.addFilter(RedactingFilter())
        logger = get_logger()
        logger.addHandler(capture)
        try:
            response, _ = self.request("POST", "/api/signup", {"email": "worker@example.org", "provider_answer": "request"})
            pilot, _ = self.request("POST", "/api/pilot", {"contact": "+1 555 0100 Jane Person", "notes": "private note"})
        finally:
            logger.removeHandler(capture)
        self.assertEqual(response.status, 200)
        self.assertEqual(pilot.status, 200)
        self.assertEqual(self.app.vault.count("signup"), 1)
        self.assertNotIn("worker@example.org", stream.getvalue())
        self.assertNotIn("Jane Person", stream.getvalue())
        self.assertNotIn("private note", stream.getvalue())

    def test_oversized(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("POST", "/api/check", b"x", {"Content-Length": "65537"})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 413)

    def test_post_rejects_missing_and_foreign_origin(self):
        response, _ = self.request("POST", "/api/check", {"id": "testorg/mix-a"}, {"X-DataLineage-Request": ""})
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/api/check", {"id": "testorg/mix-a"}, {"Origin": "http://evil.example", "X-DataLineage-Request": "1"})
        self.assertEqual(response.status, 403)

    def test_bad_host_rejected(self):
        response, _ = self.request("POST", "/api/check", {"id": "testorg/mix-a"}, {"Host": "evil.example"})
        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()
