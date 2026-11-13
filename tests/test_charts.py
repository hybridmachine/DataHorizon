# SPDX-License-Identifier: GPL-3.0-only
"""Tests for chart specification and stored payload validation."""

import copy
import json
import pathlib
import tempfile
import unittest
import uuid

from datahorizon import charts
from datahorizon.engine import EngineError, Workbook
from datahorizon.workbook_code import activation, fingerprint


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
    """The error code of an ``assertRaises`` context or of an ``EngineError``."""
    return getattr(caught, "exception", caught).body["code"]


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


MARKER_MODULE = "def mark(ctx, changes):\n    ctx.set('Sheet1!Z1', 'ran')\n"


class Operations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.book = Workbook(pathlib.Path(self.tmp.name) / "ops.dhwb", create=True)
        self.sheet = self.book.sheet("Sheet1")

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def apply(self, action, **args):
        return self.book.mutate(dict(action=action, operation_id=str(uuid.uuid4()), expected_state=self.book.state(), **args))

    def refused(self, action, **args):
        with self.assertRaises(EngineError) as caught:
            self.apply(action, **args)
        return caught.exception

    def create(self, **args):
        return self.apply("chart_create", sheet="Sheet1", spec=spec(), **args)["chart_id"]

    def listed(self, **filters):
        return charts.read(self.book, dict(expected_state=self.book.state(), **filters))["charts"]

    def chart(self, chart_id):
        return self.listed(chart_id=chart_id)[0]

    def stored(self, chart_id):
        return charts.find(self.book, chart_id)[1]

    def direct(self, chart_id, payload):
        self.book.db.execute("UPDATE dh_objects SET payload=? WHERE namespace='chart' AND id=?", (json.dumps(payload), chart_id))

    def test_create_update_delete_round_trip(self):
        receipt = self.apply("chart_create", sheet="Sheet1", spec=spec())
        chart_id = receipt["chart_id"]
        self.assertEqual(str(uuid.UUID(chart_id)), chart_id)
        self.assertEqual((receipt["changed_charts"], receipt["changed_cells"]), (1, 0))
        listed = self.listed()
        self.assertEqual([(item["id"], item["sheet_id"], item["name"], item["issue"]) for item in listed],
                         [(chart_id, self.sheet, "Chart 1", None)])
        self.assertEqual(listed[0]["spec"]["series"][0]["values"]["source"]["ref"], "B2:B13")
        receipt = self.apply("chart_update", chart_id=chart_id, name="Revenue", size={"width": 600, "height": 400})
        self.assertEqual((receipt["chart_id"], receipt["changed_charts"]), (chart_id, 1))
        self.assertEqual((self.chart(chart_id)["name"], self.chart(chart_id)["size"]), ("Revenue", {"width": 600, "height": 400}))
        receipt = self.apply("chart_update", chart_id=chart_id, spec=spec("line"))
        self.assertEqual((self.chart(chart_id)["spec"]["type"], self.chart(chart_id)["name"]), ("line", "Revenue"))
        receipt = self.apply("chart_delete", chart_id=chart_id)
        self.assertEqual((receipt["chart_id"], receipt["changed_charts"]), (chart_id, 1))
        self.assertEqual(self.listed(), [])

    def test_defaults_name_z_and_size(self):
        first, second = self.create(), self.create()
        made = [self.chart(first), self.chart(second)]
        self.assertEqual([(item["name"], item["z"]) for item in made], [("Chart 1", 0), ("Chart 2", 1)])
        self.assertEqual({(item["size"]["width"], item["size"]["height"]) for item in made}, {(480, 300)})
        self.assertEqual((made[0]["anchor"], made[0]["offset"]), ({"row": 1, "col": 1}, {"x": 0, "y": 0}))
        self.apply("chart_delete", chart_id=first)
        self.assertEqual(self.chart(self.create())["name"], "Chart 1")
        placed = self.create(name="Mine", anchor={"row": 5, "col": 3}, offset={"x": 4, "y": 2}, size={"width": 64, "height": 48})
        self.assertEqual({key: self.chart(placed)[key] for key in ("name", "anchor", "offset", "size", "z")},
                         {"name": "Mine", "anchor": {"row": 5, "col": 3}, "offset": {"x": 4, "y": 2},
                          "size": {"width": 64, "height": 48}, "z": 3})
        self.assertEqual(code(self.refused("chart_create", sheet="Sheet1", spec=spec(), size={"width": 10, "height": 10})), "INVALID_ARGUMENT")
        self.assertEqual(code(self.refused("chart_create", sheet="Sheet1", spec=spec(), colour="red")), "INVALID_ARGUMENT")
        self.assertEqual(code(self.refused("chart_create", sheet="Sheet1", spec=spec("radar"))), "INVALID_SPEC")

    def test_spec_patch_is_a_merge_patch(self):
        chart_id = self.create()
        self.apply("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}, "legend": {"position": "none"}})
        stored = self.stored(chart_id)["spec"]
        self.assertEqual((stored["title"], stored["legend"]), ({"text": "T"}, {"position": "none"}))
        self.assertEqual(stored["series"], charts.normalise_spec(self.book, spec())["series"])
        self.apply("chart_update", chart_id=chart_id, spec_patch={"categories": None, "type": "line", "grouping": None})
        stored = self.stored(chart_id)["spec"]
        self.assertEqual((stored["categories"], stored["type"], stored["grouping"], stored["title"]), (None, "line", None, {"text": "T"}))
        for args in ({}, {"spec": spec(), "spec_patch": {}}, {"z": 1, "order": "front"}, {"order": "middle"}, {"colour": "red"}):
            with self.subTest(args=args):
                self.assertEqual(code(self.refused("chart_update", chart_id=chart_id, **args)), "INVALID_ARGUMENT")

    def test_spec_patch_is_refused_for_an_issue_or_a_broken_source(self):
        chart_id = self.create()
        payload = self.stored(chart_id)
        payload["spec"]["categories"]["source"].update(sheet_id=None, row1=None, col1=None, row2=None, col2=None)
        self.direct(chart_id, payload)
        error = self.refused("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})
        self.assertEqual((error.body["code"], error.status), ("SOURCE_BROKEN", 409))
        payload["spec"]["type"] = "radar"
        self.direct(chart_id, payload)
        self.assertEqual(code(self.refused("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})), "INVALID_SPEC")
        self.apply("chart_update", chart_id=chart_id, name="Moved", anchor={"row": 9, "col": 9})
        self.assertEqual((self.chart(chart_id)["name"], self.chart(chart_id)["issue"]), ("Moved", "INVALID_SPEC"))
        self.apply("chart_update", chart_id=chart_id, spec=spec())
        self.assertIsNone(self.chart(chart_id)["issue"])

    def test_order_front_and_back(self):
        first, second, third = self.create(), self.create(), self.create()
        self.apply("chart_update", chart_id=first, order="front")
        self.assertEqual(self.chart(first)["z"], 3)
        self.apply("chart_update", chart_id=third, order="back")
        self.assertEqual(self.chart(third)["z"], 0)
        self.apply("chart_update", chart_id=second, z=-5)
        self.assertEqual(self.chart(second)["z"], -5)
        self.apply("chart_update", chart_id=first, order="back")
        self.assertEqual(self.chart(first)["z"], -6)

    def test_undo_and_redo_each_chart_operation(self):
        chart_id = self.create()
        created = self.stored(chart_id)
        self.apply("undo")
        self.assertEqual(self.listed(), [])
        self.apply("redo")
        self.assertEqual(self.stored(chart_id), created)
        self.apply("chart_update", chart_id=chart_id, name="Renamed", spec_patch={"title": {"text": "T"}})
        self.assertEqual(self.stored(chart_id)["name"], "Renamed")
        receipt = self.apply("undo")
        self.assertEqual((receipt["changed_charts"], receipt["changed_cells"]), (1, 0))
        self.assertEqual(self.stored(chart_id), created)
        self.apply("redo")
        self.assertEqual(self.stored(chart_id)["spec"]["title"], {"text": "T"})
        updated = self.stored(chart_id)
        self.apply("chart_delete", chart_id=chart_id)
        self.assertEqual(self.listed(), [])
        self.apply("undo")
        self.assertEqual(self.stored(chart_id), updated)
        self.assertEqual(self.book.info()["undo"], "Edit chart")

    def test_undo_labels_name_each_chart_operation(self):
        chart_id = self.create()
        self.assertEqual(self.book.info()["undo"], "Create chart")
        self.apply("chart_update", chart_id=chart_id, name="Renamed")
        self.assertEqual(self.book.info()["undo"], "Edit chart")
        self.apply("chart_delete", chart_id=chart_id)
        self.assertEqual(self.book.info()["undo"], "Delete chart")
        self.apply("undo")
        self.assertEqual(self.book.info()["redo"], "Delete chart")
        self.apply("chart_update", chart_id=chart_id, name="Again", label="Rename sales chart")
        self.assertEqual(self.book.info()["undo"], "Rename sales chart")

    def test_undo_conflict_when_the_chart_changed_underneath(self):
        chart_id = self.create()
        self.apply("chart_update", chart_id=chart_id, name="Renamed")
        self.direct(chart_id, dict(self.stored(chart_id), name="Elsewhere"))
        error = self.refused("undo")
        self.assertEqual((error.body["code"], error.status), ("HISTORY_CONFLICT", 409))
        self.assertEqual(self.stored(chart_id)["name"], "Elsewhere")

    def test_limit_charts(self):
        for _ in range(charts.LIMIT_CHARTS):
            self.create()
        self.assertEqual(code(self.refused("chart_create", sheet="Sheet1", spec=spec())), "LIMIT_CHARTS")
        self.assertEqual(len(self.listed()), charts.LIMIT_CHARTS)

    def test_unknown_chart(self):
        missing = str(uuid.uuid4())
        for action, args in (("chart_update", {"name": "X"}), ("chart_delete", {})):
            with self.subTest(action=action):
                error = self.refused(action, chart_id=missing, **args)
                self.assertEqual((error.body["code"], error.status), ("CHART_NOT_FOUND", 404))
        for action, args in (("chart_update", {"name": "X"}), ("chart_delete", {})):
            for bad in ({}, {"chart_id": 7}, {"chart_id": None}):
                with self.subTest(action=action, bad=bad):
                    error = self.refused(action, **bad, **args)
                    self.assertEqual((error.body["code"], error.status), ("INVALID_ARGUMENT", 400))
        with self.assertRaises(EngineError) as caught:
            self.listed(chart_id=missing)
        self.assertEqual((code(caught), caught.exception.status), ("CHART_NOT_FOUND", 404))

    def test_chart_operations_leave_cells_and_cell_counts_alone(self):
        receipt = self.apply("edit", sheet="Sheet1", cells=[{"row": 2, "col": 2, "source": "5"}])
        self.assertEqual((receipt["changed_cells"], receipt["changed_charts"]), (1, 0))
        chart_id = self.create()
        for action, args in (("chart_update", {"chart_id": chart_id, "name": "N"}), ("undo", {}), ("chart_delete", {"chart_id": chart_id})):
            with self.subTest(action=action):
                receipt = self.apply(action, **args)
                self.assertEqual((receipt["changed_cells"], receipt["changed_charts"]), (0, 1))
        self.assertEqual(self.book.read("Sheet1", {"r1": 1, "c1": 1, "r2": 20, "c2": 20})["cells"][0]["source"], "5")
        self.assertEqual(len(self.book.read("Sheet1", {"r1": 1, "c1": 1, "r2": 20, "c2": 20})["cells"]), 1)

    def test_bound_cell_change_handler_does_not_run_for_chart_operations(self):
        self.apply("module_save", name="marker", source=MARKER_MODULE, macros=[], fingerprint=fingerprint(self.book.db))
        self.apply("binding_save", event="cell_change", scope={"kind": "sheet", "sheet": "Sheet1"}, module="marker",
                   function="mark", required=True, fingerprint=fingerprint(self.book.db))
        self.book.approved_code = frozenset([activation(self.book)["activation_key"]])
        marker = (self.sheet, 1, 26)
        chart_id = self.create()
        for action, args in (("chart_update", {"chart_id": chart_id, "spec_patch": {"title": {"text": "T"}}}), ("undo", {}),
                             ("chart_delete", {"chart_id": chart_id})):
            with self.subTest(action=action):
                self.assertEqual(self.apply(action, **args)["outcome"], "committed")
                self.assertIsNone(self.book.raw(*marker))
        self.apply("edit", sheet="Sheet1", cells=[{"row": 2, "col": 2, "source": "5"}])
        self.assertEqual(self.book.raw(*marker)["source"], "ran")


if __name__ == "__main__":
    unittest.main()
