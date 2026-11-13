# SPDX-License-Identifier: GPL-3.0-only
"""Tests for chart specification and stored payload validation."""

import copy
import pathlib
import tempfile
import unittest

from datahorizon import charts
from datahorizon.engine import EngineError, Workbook


def series(ref, sheet="Sheet1"):
    return {"name": {"text": "s"}, "values": {"source": {"kind": "range", "sheet": sheet, "ref": ref}}, "x": None}


def two_series():
    return [series("B2:B13"), series("C2:C13")]


def axes(min=None, max=None):
    return {"category": {"title": None}, "value": {"title": None, "min": min, "max": max}}


def spec(type_="column", **overrides):
    base = {"spec_version": 1, "type": type_,
            "categories": {"source": {"kind": "range", "sheet": "Sheet1", "ref": "A2:A13"}},
            "series": [series("B2:B13")]}
    base.update(overrides)
    return base


def payload(stored_spec):
    return {"name": "Chart", "anchor": {"row": 1, "col": 1}, "offset": {"x": 0, "y": 0},
            "size": {"width": 480, "height": 320}, "z": 0,
            "spec_version": stored_spec["spec_version"], "spec": stored_spec}


def code(caught):
    return caught.exception.body["code"]


class Spec(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = Workbook(pathlib.Path(self.tmp.name) / "x.dhwb", create=True)
        self.sheet = self.book.sheet("Sheet1")

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def test_normalise_turns_refs_into_stored_rectangles(self):
        out = charts.normalise_spec(self.book, spec())
        self.assertEqual(out["series"][0]["values"]["source"],
                         {"kind": "range", "sheet_id": self.sheet, "row1": 2, "col1": 2, "row2": 13, "col2": 2})
        self.assertEqual(out["grouping"], "clustered")

    def test_normalise_fills_defaults_and_is_complete(self):
        out = charts.normalise_spec(self.book, spec("pie", categories=None))
        self.assertEqual((out["grouping"], out["title"], out["categories"], out["legend"], out["series"][0]["x"]),
                         (None, {"text": None}, None, {"position": "right"}, None))
        self.assertEqual(out["axes"], axes())
        self.assertIsNone(charts.validate_stored(payload(out)))

    def test_normalise_accepts_scatter_and_sorts_corners(self):
        item = series("B13:B2")
        item["x"] = {"source": {"kind": "range", "sheet": "Sheet1", "ref": "A2"}}
        out = charts.normalise_spec(self.book, spec("scatter", categories=None, series=[item]))
        self.assertEqual((out["series"][0]["values"]["source"]["row1"], out["series"][0]["values"]["source"]["row2"]), (2, 13))
        self.assertEqual(out["series"][0]["x"]["source"]["row2"], 2)

    def test_rejections(self):
        cases = [
            (dict(type="radar"), "INVALID_SPEC"), (dict(spec_version=2), "UNSUPPORTED_SPEC_VERSION"),
            (dict(colour="red"), "INVALID_SPEC"), (dict(series=[]), "INVALID_SPEC"),
            (dict(type="pie", series=two_series()), "INVALID_SPEC"),
            (dict(type="scatter"), "INVALID_SPEC"),                        # no x
            (dict(type="line", grouping="stacked"), "INVALID_SPEC"),
            (dict(series=[series("B2:C13")]), "INVALID_SPEC"),             # not one row or column
            (dict(series=[series("B2:B10002")]), "LIMIT_POINTS"),
            (dict(series=[series("B2:B13")] * 65), "LIMIT_SERIES"),
            (dict(series=[series(f"{c}2:{c}10001") for c in "BCDEFGHIJKL"]), "LIMIT_CELLS"),
            (dict(type="pie", series=[series("B2:B1002")]), "LIMIT_PIE_POINTS"),
            (dict(axes=axes(min=5, max=1)), "INVALID_SPEC"),
            (dict(series=[series("B2:B13", sheet="Nowhere")]), "SHEET_NOT_FOUND"),
        ]
        for overrides, expected in cases:
            with self.assertRaises(EngineError) as caught:
                charts.normalise_spec(self.book, spec(**overrides))
            self.assertEqual(code(caught), expected, overrides)

    def test_validate_stored_separates_bad_envelopes_from_unreadable_specs(self):
        good = payload(charts.normalise_spec(self.book, spec()))
        self.assertIsNone(charts.validate_stored(good))
        newer = dict(good, spec_version=2, spec=dict(good["spec"], spec_version=2, shiny=True))
        self.assertEqual(charts.validate_stored(newer), "UNSUPPORTED_SPEC_VERSION")
        self.assertEqual(charts.validate_stored(dict(good, spec=dict(good["spec"], type="radar"))), "INVALID_SPEC")
        for broken in (dict(good, size={"width": 10, "height": 10}), {k: v for k, v in good.items() if k != "z"},
                       dict(good, extra=1), dict(good, spec_version=3), dict(good, name="")):
            with self.assertRaises(EngineError) as caught:
                charts.validate_stored(broken)
            self.assertEqual(code(caught), "INVALID_FORMAT")

    def test_validate_stored_treats_a_broken_source_as_valid_and_does_not_mutate(self):
        stored = charts.normalise_spec(self.book, spec())
        stored["categories"]["source"].update(sheet_id=None, row1=None, col1=None, row2=None, col2=None)
        good = payload(stored)
        before = copy.deepcopy(good)
        self.assertIsNone(charts.validate_stored(good))
        self.assertEqual(good, before)
        stored["series"][0]["values"]["source"]["row1"] = None
        self.assertEqual(charts.validate_stored(payload(stored)), "INVALID_SPEC")

    def test_describe_adds_refs_and_broken_flags(self):
        stored = charts.normalise_spec(self.book, spec())
        stored["categories"]["source"].update(row1=None, col1=None, row2=None, col2=None)
        given = payload(stored)
        before = copy.deepcopy(given)
        out = charts.describe(self.book.db, self.sheet, "c1", given)
        self.assertEqual(given, before)
        self.assertEqual((out["id"], out["sheet_id"], out["issue"]), ("c1", self.sheet, None))
        self.assertEqual(out["spec"]["series"][0]["values"]["source"]["ref"], "B2:B13")
        self.assertEqual((out["spec"]["categories"]["source"]["ref"], out["spec"]["categories"]["source"]["broken"]), (None, True))
        self.assertFalse(out["spec"]["series"][0]["values"]["source"]["broken"])

    def test_describe_returns_an_unreadable_spec_as_stored(self):
        newer = payload({"spec_version": 2, "shiny": True})
        out = charts.describe(self.book.db, self.sheet, "c1", newer)
        self.assertEqual(out["issue"], "UNSUPPORTED_SPEC_VERSION")
        self.assertEqual(out["spec"], {"spec_version": 2, "shiny": True})


if __name__ == "__main__":
    unittest.main()
