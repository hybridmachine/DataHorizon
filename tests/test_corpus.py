# SPDX-License-Identifier: GPL-3.0-only
"""The interchange corpus: listed files exist with their hashes, nothing is rejected, the scorecard is current."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
CORPUS = TESTS / "fixtures" / "corpus"
GENERATED = CORPUS / "generate_feature_workbooks.py"
FIELDS = {"file", "sha256", "producer", "producer_version", "saved_on", "exercises", "source", "format", "hand_built", "unsupported_features"}
sys.path.insert(0, str(CORPUS))
import corpus_scorecard  # noqa: E402
import generate_feature_workbooks  # noqa: E402


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provenance = json.loads((CORPUS / "provenance.json").read_text(encoding="utf-8"))
        cls.scorecard = corpus_scorecard.build()

    def test_every_listed_file_exists_with_its_hash_and_complete_provenance(self):
        files = [entry["file"] for entry in self.provenance]
        self.assertEqual(len(files), len(set(files)), "a file is listed twice")
        for entry in self.provenance:
            with self.subTest(file=entry["file"]):
                self.assertEqual(set(entry), FIELDS)
                path = ROOT / entry["file"]
                self.assertTrue(path.is_file())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
                self.assertTrue(entry["exercises"] and entry["producer"] and entry["source"])
                self.assertIsInstance(entry["hand_built"], bool)
                if entry["hand_built"]:
                    self.assertNotIn(entry["producer"], ("Microsoft Excel", "LibreOffice Calc"))

    def test_every_file_in_the_corpus_directory_is_listed(self):
        listed = {entry["file"] for entry in self.provenance}
        ignored = {"README.md", "provenance.json", GENERATED.name}
        present = {f"tests/fixtures/corpus/{p.name}" for p in CORPUS.iterdir() if p.is_file() and p.name not in ignored and p.suffix != ".pyc"}
        self.assertEqual(present - listed, set(), "files in the corpus directory without provenance")
        self.assertEqual({f for f in listed if f.startswith("tests/fixtures/corpus/")} - present, set())

    def test_the_corpus_stays_small(self):
        total = sum((ROOT / entry["file"]).stat().st_size for entry in self.provenance)
        self.assertLess(total, 2 * 1024 * 1024)

    def test_no_file_is_rejected_when_formulas_are_kept(self):
        for name, modes in self.scorecard["files"].items():
            for mode in ("mixed", "formulas"):
                with self.subTest(file=name, mode=mode):
                    self.assertNotEqual(modes[mode]["status"], "rejected", modes[mode].get("error"))

    def test_the_checked_in_scorecard_equals_a_fresh_run(self):
        checked_in = corpus_scorecard.DEFAULT_OUT.read_text(encoding="utf-8")
        self.assertEqual(checked_in, corpus_scorecard.render(self.scorecard),
                         "regenerate with: PYTHONPATH=service python3 tests/corpus_scorecard.py")

    def test_the_scorecard_records_the_open_gaps(self):
        producers = {entry["producer"] for entry in self.provenance}
        expected = [gap for producer, gap in corpus_scorecard.REQUIRED_PRODUCERS if producer not in producers]
        self.assertEqual(self.scorecard["summary"]["gaps"], expected)

    def test_every_unsupported_feature_is_reported_in_every_mode(self):
        # No listed feature of any file, in any mode or status, is dropped without its report entry.
        fresh = corpus_scorecard.known_unreported(self.scorecard["files"], self.provenance)
        self.assertEqual(fresh, {mode: [] for mode in corpus_scorecard.MODES})
        self.assertEqual(self.scorecard["summary"]["known_unreported"], fresh)
        checked_in = json.loads(corpus_scorecard.DEFAULT_OUT.read_text(encoding="utf-8"))["summary"]
        self.assertEqual(checked_in["known_unreported"], fresh)
        self.assertEqual(checked_in["status_meaning"], corpus_scorecard.STATUS_MEANING)

    def test_every_listed_feature_id_has_a_required_report(self):
        ids = {feature for entry in self.provenance for feature in entry["unsupported_features"]}
        self.assertEqual(ids - set(corpus_scorecard.FEATURE_REPORTS), set())
        self.assertEqual(set(corpus_scorecard.FEATURE_REPORTS_VALUES_MODE) - set(corpus_scorecard.FEATURE_REPORTS), set())

    def test_a_missing_required_entry_is_listed_whatever_the_status(self):
        files = {"a.xlsx": {mode: {"status": "degraded", "entries": [{"category": "style.font", "severity": "omitted", "detail": "x", "count": 1}]}
                            for mode in corpus_scorecard.MODES}}
        provenance = [{"file": "a.xlsx", "unsupported_features": ["merged_cells", "array_formulas"]}]
        result = corpus_scorecard.known_unreported(files, provenance)
        self.assertEqual(result["formulas"], [{"file": "a.xlsx", "features": ["array_formulas", "merged_cells"]}])
        # Values mode replaces every formula by its cache by request, so only the sheet feature is missing.
        self.assertEqual(result["values"], [{"file": "a.xlsx", "features": ["merged_cells"]}])

    def test_readme_states_gaps_and_status_meaning(self):
        readme = (CORPUS / "README.md").read_text(encoding="utf-8")
        for gap in self.scorecard["summary"]["gaps"]:
            self.assertIn(gap.replace("no ", "No ", 1), readme.replace("**", ""), gap)
        for text in ("How to read the scorecard", "not a fidelity claim", "known_unreported", "`degraded`", "`rejected`", "`clean`"):
            self.assertIn(text, readme)

    def test_the_feature_workbooks_are_what_the_generator_produces(self):
        for name, build in generate_feature_workbooks.FILES.items():
            with self.subTest(file=name):
                self.assertEqual((CORPUS / name).read_bytes(), build())
        generated = {f"tests/fixtures/corpus/{name}" for name in generate_feature_workbooks.FILES}
        self.assertEqual(generated, {e["file"] for e in self.provenance if e["file"].startswith("tests/fixtures/corpus/")})


if __name__ == "__main__":
    unittest.main()
