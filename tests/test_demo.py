import http.client
import json
from pathlib import Path
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import demo


ROOT = Path(__file__).resolve().parents[1]


class HostedDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = demo.DemoState()
        cls.server = demo.create_server(0, cls.state)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result

    def submit(self, kind, value):
        body = urlencode({"kind": kind, "input": value}).encode("utf-8")
        status, headers, data = self.request(
            "POST", "/analyze", body,
            {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))},
        )
        self.assertEqual(status, 303, data)
        self.assertRegex(headers["Location"], r"^/reports/[0-9a-f]{32}$")
        return headers["Location"]

    def test_health_and_complete_fake_core_flow(self):
        status, headers, body = self.request("GET", "/healthz")
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(headers["Content-Security-Policy"], demo.CSP)

        status, headers, home = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], demo.CSP)
        self.assertIn(demo.BANNER.encode(), home)
        self.assertIn(b"testorg/mix-a", home)

        location = self.submit("dataset", "testorg/mix-a")
        status, headers, report_page = self.request("GET", location)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], demo.CSP)
        self.assertIn(demo.BANNER.encode(), report_page)
        self.assertIn(b"Apache-2.0", report_page)
        self.assertIn(b"CC-BY-NC-4.0", report_page)
        self.assertIn(b"NC_UPSTREAM", report_page)
        self.assertIn(b"hf:testorg/mix-a", report_page)
        self.assertIn(b"hf:testorg/source-b", report_page)
        self.assertIn(b"licence coverage", report_page)

        status, headers, exported = self.request("GET", location + "/report.json")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(headers["Content-Security-Policy"], demo.CSP)
        value = json.loads(exported)
        self.assertEqual(value["schema"], "datalineage.report/0.1")
        self.assertIn("hf:testorg/source-b", value["nodes"])

        status, _, bom_body = self.request("GET", location + "/bom.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(bom_body)["specVersion"], "1.6")

        status, headers, pdf = self.request("GET", location + "/report.pdf")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))

    def test_manifest_flow_escapes_supplied_html(self):
        manifest = json.dumps({
            "model": {"name": "<script>alert(1)</script>", "provider": "A & B"},
            "datasets": [{"id": "testorg/clean", "role": "fine-tune"}],
        })
        location = self.submit("manifest", manifest)
        status, _, page = self.request("GET", location)
        self.assertEqual(status, 200)
        self.assertNotIn(b"<script>alert(1)</script>", page)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn(b"A &amp; B", page)
        self.assertIn(demo.BANNER.encode(), page)

    def test_body_limit_errors_and_unknown_pages(self):
        status, _, page = self.request(
            "POST", "/analyze", b"x",
            {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "65537"},
        )
        self.assertEqual(status, 413)
        self.assertIn(demo.BANNER.encode(), page)

        status, _, page = self.request("GET", "/not-found")
        self.assertEqual(status, 404)
        self.assertIn(demo.BANNER.encode(), page)

    def test_deeply_nested_manifest_returns_400(self):
        nested = "[" * 2000 + "0" + "]" * 2000
        body = urlencode({"kind": "manifest", "input": nested}).encode("utf-8")
        status, headers, page = self.request(
            "POST", "/analyze", body,
            {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))},
        )
        self.assertEqual(status, 400)
        self.assertEqual(headers["Content-Security-Policy"], demo.CSP)
        self.assertIn(demo.BANNER.encode(), page)

    def test_demo_is_bound_for_hosted_access(self):
        host, port = self.server.server_address
        self.assertEqual(host, "0.0.0.0")
        self.assertGreater(port, 0)
        self.assertIsInstance(self.server, demo.LimitedThreadingHTTPServer)

    def test_handler_timeout_and_connection_cap(self):
        self.assertEqual(demo.make_handler(demo.DemoState()).timeout, 10)
        limited = object.__new__(demo.LimitedThreadingHTTPServer)
        limited._connection_slots = threading.BoundedSemaphore(1)
        first = Mock()
        second = Mock()
        with patch.object(ThreadingHTTPServer, "process_request") as parent, patch.object(
            limited, "shutdown_request"
        ) as shutdown:
            limited.process_request(first, ("192.0.2.1", 1))
            limited.process_request(second, ("192.0.2.2", 2))
        parent.assert_called_once_with(first, ("192.0.2.1", 1))
        shutdown.assert_called_once_with(second)

    def test_analyze_rate_limit_is_per_client_ip(self):
        state = demo.DemoState(analyze_limit=1)
        server = demo.create_server(0, state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        body = urlencode({"kind": "dataset", "input": "testorg/clean"}).encode("utf-8")

        def request(forwarded):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("POST", "/analyze", body, {
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(body)),
                "X-Forwarded-For": forwarded,
            })
            response = connection.getresponse()
            response.read()
            status = response.status
            connection.close()
            return status

        try:
            self.assertEqual(request("192.0.2.10"), 303)
            self.assertEqual(request("192.0.2.10"), 429)
            self.assertEqual(request("192.0.2.11"), 303)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_ingress_proxy_enforces_body_time_rate_and_connection_limits(self):
        config = (ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn("client_max_body_size 64k", config)
        self.assertIn("client_header_timeout 10s", config)
        self.assertIn("client_body_timeout 10s", config)
        self.assertIn("limit_conn demo_connections 8", config)
        self.assertIn("limit_req zone=demo_analyze", config)
        self.assertIn("proxy_request_buffering on", config)
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr", config)


if __name__ == "__main__":
    unittest.main()
