import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

from datalineage.clients import FakeHubClient, RealHubClient
from datalineage.extract import upstream
from datalineage.pdf import make_pdf
from datalineage.resolver import Resolver
from datalineage.template_draft import UNKNOWN, build_draft


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


class IndependentReviewerRegressions(unittest.TestCase):
    def test_text_task_categories_expand_lineage(self):
        categories = [
            "summarization",
            "fill-mask",
            "token-classification",
            "sentence-similarity",
            "feature-extraction",
            "zero-shot-classification",
        ]
        for category in categories:
            with self.subTest(category=category):
                docs = {
                    "org/root": {
                        "card_text": "",
                        "card_data": {"source_datasets": ["org/source"]},
                        "tags": ["task_categories:" + category],
                    },
                    "org/source": {"card_text": "", "card_data": {"license": "mit"}, "tags": ["language:en"]},
                }
                report = Resolver(FakeHubClient(docs), aliases={}).resolve(["org/root"])
                self.assertEqual(report.nodes["hf:org/root"].modality, "text")
                self.assertIn("hf:org/source", report.nodes)

    def test_transient_hub_failure_is_not_cached(self):
        limited = urllib.error.HTTPError("url", 429, "limited", {}, None)
        metadata = _Response(b'{"tags":["language:en"],"cardData":{"license":"mit"}}')
        card = _Response(b"---\nlicense: mit\n---\n")
        with tempfile.TemporaryDirectory() as temporary, patch(
            "urllib.request.urlopen", side_effect=[limited, limited, metadata, card]
        ) as opened:
            client = RealHubClient(temporary, minimum_interval=0)
            first = client.get_dataset("org/data")
            second = client.get_dataset("org/data")
        self.assertTrue(first["partial"])
        self.assertIn("HUB_RATE_LIMITED", first["_limits_hit"])
        self.assertFalse(second["partial"])
        self.assertEqual(opened.call_count, 4)

    def test_missing_draft_values_are_always_unknown(self):
        docs = {"org/data": {"card_text": "", "card_data": {}, "tags": []}}
        report = Resolver(FakeHubClient(docs), aliases={}).resolve(["org/data"])
        report.nodes["hf:org/data"].url = None
        draft = build_draft(report, {"model": {"name": "model"}})
        general = next(section for section in draft["sections"] if section["id"] == "1")
        self.assertEqual(general["fields"]["provider"]["status"], "unknown")
        self.assertEqual(general["fields"]["base_model"]["status"], "unknown")
        datasets = next(section for section in draft["sections"] if section["id"] == "2.1")["fields"]["datasets"]["value"]
        self.assertEqual(datasets[0]["link"]["status"], "unknown")
        self.assertEqual(datasets[0]["licence"]["status"], "unknown")

    def test_pdf_preserves_mandatory_em_dash_label(self):
        pdf = make_pdf([UNKNOWN, "DRAFT — template"])
        self.assertIn(b"UNKNOWN \x97 provider must supply", pdf)
        self.assertIn(b"/Encoding /WinAnsiEncoding", pdf)

    def test_ownerless_and_extended_sources_are_resolved(self):
        docs = {
            "root": {"card_text": "", "card_data": {"source_datasets": ["extended|squad", "bad value"]}, "tags": []},
            "squad": {"card_text": "", "card_data": {"license": "mit"}, "tags": []},
        }
        report = Resolver(FakeHubClient(docs), aliases={}).resolve(["root"])
        self.assertIn("hf:squad", report.nodes)
        self.assertIn("INVALID_SOURCE_DATASETS", report.limits_hit)
        self.assertTrue(any(node.startswith("unresolved:source-") for node in report.nodes))

    def test_limits_create_unresolved_placeholders(self):
        docs = {
            "a/root": {"card_text": "", "card_data": {"source_datasets": ["b/one", "c/two"]}, "tags": []},
            "b/one": {"card_text": "", "card_data": {"license": "mit"}, "tags": []},
            "c/two": {"card_text": "", "card_data": {"license": "mit"}, "tags": []},
        }
        report = Resolver(FakeHubClient(docs), aliases={}, max_nodes=1).resolve(["a/root"])
        self.assertIn("NODE_LIMIT", report.limits_hit)
        self.assertEqual(set(report.nodes), {"hf:a/root", "hf:b/one", "hf:c/two"})
        self.assertEqual(report.coverage["unresolved"], 2)

        class SlowHub(FakeHubClient):
            def get_dataset(self, dataset_id):
                time.sleep(0.06)
                return super().get_dataset(dataset_id)

        timed = Resolver(SlowHub(docs), aliases={}, time_budget=0.05).resolve(["a/root", "b/one"])
        self.assertIn("TIME_LIMIT", timed.limits_hit)
        self.assertIn("hf:b/one", timed.nodes)
        self.assertFalse(timed.nodes["hf:b/one"].resolved)


if __name__ == "__main__":
    unittest.main()
