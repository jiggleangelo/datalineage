import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from datalineage.clients import RealHubClient


class _Response:
    status = 200

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.data[:size]


class CacheSecurityRegressionTests(unittest.TestCase):
    def test_cache_directory_is_private_and_wrong_owner_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = RealHubClient(temporary, minimum_interval=0)
            self.assertEqual(stat.S_IMODE(client.cache.stat().st_mode), 0o700)

        if not hasattr(os, "geteuid"):
            return
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "hub"
            cache.mkdir()
            with patch("datalineage.clients.os.geteuid", return_value=os.geteuid() + 1):
                with self.assertRaises(PermissionError):
                    RealHubClient(temporary, minimum_interval=0)

    def test_cached_hub_document_must_match_requested_id(self):
        metadata = _Response(b'{"tags":["language:en"],"cardData":{"license":"mit"}}')
        card = _Response(b"---\nlicense: mit\n---\n")
        with tempfile.TemporaryDirectory() as temporary:
            client = RealHubClient(temporary, minimum_interval=0)
            planted = {
                "id": "attacker/data",
                "card_text": "hostile",
                "card_data": {},
                "tags": [],
                "partial": False,
                "_limits_hit": [],
            }
            path = client._path("org/data")
            path.write_text(json.dumps(planted), encoding="utf-8")
            path.chmod(0o600)
            with patch("urllib.request.urlopen", side_effect=[metadata, card]) as opened:
                result = client.get_dataset("org/data")
            self.assertEqual(result["id"], "org/data")
            self.assertNotEqual(result["card_text"], "hostile")
            self.assertEqual(opened.call_count, 2)

    def test_malformed_negative_cache_entry_is_not_trusted(self):
        metadata = _Response(b'{"tags":[],"cardData":{}}')
        card = _Response(b"")
        with tempfile.TemporaryDirectory() as temporary:
            client = RealHubClient(temporary, minimum_interval=0)
            path = client._path("org/missing")
            path.write_text(
                json.dumps({"id": "org/missing", "_negative_404": True, "_cached_at": time.time()}),
                encoding="utf-8",
            )
            path.chmod(0o600)
            with patch("urllib.request.urlopen", side_effect=[metadata, card]) as opened:
                result = client.get_dataset("org/missing")
            self.assertEqual(result["id"], "org/missing")
            self.assertNotIn("_negative_404", result)
            self.assertEqual(opened.call_count, 2)


if __name__ == "__main__":
    unittest.main()
