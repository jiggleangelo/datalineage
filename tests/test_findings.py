import json
import os
import stat
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from datalineage.clients import FakeHubClient, FakePaperClient, RealHubClient
from datalineage.exports import json_export
from datalineage.extract import parse_front_matter, upstream
from datalineage.licences import detect, normalise, strictest
from datalineage.pdf import make_pdf
from datalineage.resolver import Resolver
from datalineage.template_draft import build_draft
from datalineage.vault import FileVault


class _Response:
    status = 200

    def __init__(self, data):
        self.data = data
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.data[:size]


class ReviewerFindingTests(unittest.TestCase):
    def test_list_form_licence_is_not_unlicensed(self):
        docs = {"org/data": {"card_text": "---\nlicense:\n - mit\n---\n", "card_data": {"license": ["mit"]}, "tags": ["modality:text"]}}
        report = Resolver(FakeHubClient(docs), aliases={}).resolve(["org/data"])
        self.assertEqual(report.nodes["hf:org/data"].declared.id, "MIT")
        self.assertNotIn("UNLICENSED", {flag.code for flag in report.flags})

    def test_inline_sources_and_comments(self):
        data = parse_front_matter("---\nlicense: mit # comment\nsource_datasets: [a/b, c/d] # inputs\n---\n")
        self.assertEqual(data["license"], "mit")
        self.assertEqual(data["source_datasets"], ["a/b", "c/d"])
        self.assertEqual(len(upstream("", data, {})), 2)

    def test_incidental_and_negated_licences_are_not_detected(self):
        self.assertEqual(detect("We exclude CC-BY-NC content and discuss other approaches."), [])
        self.assertEqual(detect("The licence is not CC-BY-NC-4.0."), [])
        self.assertEqual(detect("We do not restrict use to non-commercial projects."), [])
        self.assertEqual(detect("A paper quotes the phrase research purposes only."), [])

    def test_unknown_does_not_mask_noncommercial(self):
        value = strictest([normalise("invented-license"), normalise("cc-by-nc-4.0")])
        self.assertEqual(value.category, "noncommercial")

    def test_arxiv_lookups_are_capped(self):
        papers = FakePaperClient({})
        text = " ".join("arXiv: 2401.%04d" % number for number in range(1000, 1005))
        limits = []
        upstream(text, {}, {}, papers, limits)
        self.assertEqual(len(papers.calls), 3)
        self.assertIn("ARXIV_LIMIT", limits)

    def test_file_vault_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "vault"
            directory.mkdir(mode=0o777)
            os.chmod(directory, 0o777)
            path = directory / "contacts.jsonl"
            vault = FileVault(path)
            vault.put("signup", {"email": "person@example.test"})
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(hasattr(vault, "get"))

    def test_json_export_matches_public_schema_shape(self):
        report = Resolver(FakeHubClient({"org/data": {"card_text": "", "card_data": {}, "tags": []}}), aliases={}).resolve(["org/data"])
        value = json.loads(json_export(report, build_draft(report)))
        self.assertNotIn("roles", value)
        self.assertTrue(all("traversed" not in edge for edge in value["edges"]))

    def test_hub_reads_are_bounded_and_decode_replacement(self):
        metadata = _Response(b'{"tags": []}')
        card = _Response(b"---\nlicense: mit\n---\ninvalid:\xff")
        with tempfile.TemporaryDirectory() as temporary, patch("urllib.request.urlopen", side_effect=[metadata, card]):
            client = RealHubClient(temporary, minimum_interval=0)
            result = client.get_dataset("org/data")
        self.assertIn("\ufffd", result["card_text"])
        self.assertEqual(metadata.read_sizes, [1_000_001])
        self.assertEqual(card.read_sizes, [1_000_001])

    def test_hub_status_and_negative_cache(self):
        error = urllib.error.HTTPError("url", 404, "missing", {}, None)
        with tempfile.TemporaryDirectory() as temporary, patch("urllib.request.urlopen", side_effect=[error, error]) as opened:
            client = RealHubClient(temporary, minimum_interval=0)
            first = client.get_dataset("org/missing")
            second = client.get_dataset("org/missing")
        self.assertIn("HUB_NOT_FOUND", first["_limits_hit"])
        self.assertEqual(first, second)
        self.assertEqual(opened.call_count, 2)

    def test_malformed_litigation_and_fictional_match(self):
        docs = {"fictional/data": {"card_text": "", "card_data": {"license": "mit"}, "tags": ["modality:text"]}}
        litigation = ["bad", {"kind": "litigated", "match": ["fictional/data"], "reviewed_by_counsel": True, "summary": "Fictional proceeding"}]
        report = Resolver(FakeHubClient(docs), aliases={}, litigation=litigation).resolve(["fictional/data"])
        self.assertIn("LITIGATED", {flag.code for flag in report.flags})

    def test_pdf_strips_controls_and_wraps_hostile_text(self):
        pdf = make_pdf(["title\r\nET q 0 0 100 100 re f" + "x" * 300])
        self.assertNotIn(b"title\r", pdf)
        self.assertGreater(pdf.count(b") Tj T*"), 2)

    def test_common_crawl_uses_graph_and_bad_template_falls_back(self):
        report = Resolver(FakeHubClient({}), aliases={"Common Crawl": {"id": "ext:common-crawl", "title": "Common Crawl", "licence": "MIT"}}).resolve(["ext:common-crawl"])
        with tempfile.TemporaryDirectory() as temporary:
            bad = Path(temporary) / "bad.json"
            bad.write_text("not json", encoding="utf-8")
            draft = build_draft(report, template_path=bad)
        section = next(item for item in draft["sections"] if item["id"] == "2.3")
        self.assertEqual(section["fields"]["crawler"]["status"], "inferred")


if __name__ == "__main__":
    unittest.main()
