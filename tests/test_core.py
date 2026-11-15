import io
import json
import logging
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from datalineage.clients import FakeHubClient, FakePaperClient
from datalineage.extract import parse_front_matter, upstream
from datalineage.exports import cyclonedx, json_export, pdf_export
from datalineage.licences import detect, more_permissive, normalise, strictest
from datalineage.logsafe import RedactingFilter
from datalineage.resolver import Resolver
from datalineage.template_draft import build_draft
from datalineage.vault import MemoryVault

ROOT = Path(__file__).resolve().parents[1]

class LicenceTests(unittest.TestCase):
    def test_normalise_and_precedence(self):
        self.assertEqual(normalise("cc by-nc 4.0").id, "CC-BY-NC-4.0")
        self.assertEqual(strictest([normalise("MIT"), normalise("cc-by-sa-4.0")]).category, "sharealike")
        self.assertFalse(more_permissive("unknown", "permissive"))
    def test_text_detection(self):
        cats = {x.category for x in detect("Research purposes only; not for commercial use")}
        self.assertIn("restricted", cats); self.assertIn("noncommercial", cats)

class ExtractTests(unittest.TestCase):
    def test_front_matter(self):
        value = parse_front_matter("---\nlicense: 'mit'\nsource_datasets:\n - a/b\njunk\n---\nbody")
        self.assertEqual(value["license"], "mit"); self.assertEqual(value["source_datasets"], ["a/b"])
    def test_sources_and_whole_words(self):
        aliases = {"Pile": "ext:pile"}
        self.assertFalse(upstream("compiled", {}, aliases))
        self.assertIn(("ext:pile", "alias_mention", "low"), upstream("The Pile corpus", {}, aliases))

class ResolverTests(unittest.TestCase):
    def test_tree_flags_and_fetch_once(self):
        hub = FakeHubClient(ROOT / "fixtures/hub_cards.json")
        report = Resolver(hub, FakePaperClient({})).resolve(["testorg/mix-a"])
        self.assertIn("hf:testorg/source-b", report.nodes)
        self.assertEqual(hub.calls.count("testorg/source-b"), 1)
        self.assertIn("NC_UPSTREAM", {f.code for f in report.flags})
        self.assertGreater(report.coverage["pct"], 0)
    def test_cycle_depth_unresolved_nontext(self):
        docs = {"a/x":{"card_text":"","card_data":{"source_datasets":["b/y"]},"tags":[]},"b/y":{"card_text":"","card_data":{"source_datasets":["a/x"]},"tags":[]}}
        report = Resolver(FakeHubClient(docs), aliases={}).resolve(["a/x"])
        self.assertIn("CYCLE", report.limits_hit)
        limited = Resolver(FakeHubClient(docs), aliases={}, max_depth=0).resolve(["a/x"])
        self.assertIn("DEPTH_LIMIT", limited.limits_hit)
        missing = Resolver(FakeHubClient({"a/x":{"card_text":"","card_data":{"source_datasets":["z/no"]},"tags":[]}}), aliases={}).resolve(["a/x"])
        self.assertIn("UNRESOLVED_UPSTREAM", {f.code for f in missing.flags})
        image = Resolver(FakeHubClient(ROOT / "fixtures/hub_cards.json"), aliases={}).resolve(["testorg/image"])
        self.assertEqual(len(image.nodes), 1); self.assertIn("NON_TEXT_NOT_ANALYSED", {f.code for f in image.flags})
    def test_banned_words(self):
        report = Resolver(FakeHubClient(ROOT / "fixtures/hub_cards.json")).resolve(["testorg/mix-a"])
        for flag in report.flags:
            self.assertFalse({"infringes","illegal","violation"} & set(flag.message.lower().split()))

class ExportTemplateTests(unittest.TestCase):
    def setUp(self):
        self.report = Resolver(FakeHubClient(ROOT / "fixtures/hub_cards.json")).resolve(["testorg/mix-a"])
        self.draft = build_draft(self.report, {"model":{"name":"model-x","provider":"Acme"}})
    def test_template(self):
        self.assertTrue(self.draft["banner"].startswith("DRAFT"))
        section = next(x for x in self.draft["sections"] if x["id"] == "3")
        self.assertTrue(all(x["status"] == "unknown" for x in section["fields"].values()))
        for section in self.draft["sections"]:
            for field in section["fields"].values(): self.assertIn("basis", field)
    def test_exports(self):
        value = json.loads(json_export(self.report, self.draft)); self.assertEqual(value["schema"], "datalineage.report/0.1")
        bom = json.loads(cyclonedx(self.report)); self.assertEqual(bom["specVersion"], "1.6")
        self.assertTrue(all(x["type"] == "data" for x in bom["components"]))
        pdf = pdf_export(self.report, self.draft); self.assertTrue(pdf.startswith(b"%PDF-1.4")); self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        start = int(pdf.split(b"startxref\n")[1].splitlines()[0]); self.assertEqual(pdf[start:start+4], b"xref")

class PrivacyTests(unittest.TestCase):
    def test_vault_and_redaction(self):
        vault = MemoryVault(); vault.put("signup", {"email":"person@example.com"})
        self.assertEqual(vault.count("signup"), 1); self.assertFalse(hasattr(vault, "get")); self.assertFalse(hasattr(vault, "read"))
        stream = io.StringIO(); handler = logging.StreamHandler(stream); handler.addFilter(RedactingFilter())
        logger = logging.getLogger("privacy-test"); logger.handlers=[handler]; logger.setLevel(logging.INFO)
        logger.info("bad person@example.com"); self.assertNotIn("@", stream.getvalue())

if __name__ == "__main__": unittest.main()
