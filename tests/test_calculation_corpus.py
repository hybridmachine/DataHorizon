# SPDX-License-Identifier: GPL-3.0-only
"""The formula evaluator still returns what the characterization corpus recorded (Task 1 of calculation stage 2)."""
from pathlib import Path
import json
import unittest

import calculation_corpus
from calculation_corpus import CASES

FIXTURE = Path(__file__).parent / "fixtures" / "calculation-corpus.json"


class CorpusTests(unittest.TestCase):
    def test_every_case_matches_its_recording(self):
        recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        for case in CASES:
            with self.subTest(case=case["name"]):
                self.assertEqual(calculation_corpus.results(case), recorded[case["name"]])

    def test_the_recording_holds_exactly_the_corpus_cases(self):
        recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        self.assertEqual(list(recorded), [case["name"] for case in CASES])
