# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the charts stage 1 renderer experiment (not product code)."""

import json
import math
import pathlib
import unittest
import xml.etree.ElementTree as ET

import chart_render_experiment as cr

FIX = pathlib.Path(__file__).parent / "fixtures" / "charts"
ALLOWED = {"svg", "g", "rect", "line", "path", "circle", "text"}


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def tags(svg):
    return {el.tag.split("}")[-1] for el in ET.fromstring(svg).iter()}


def one(values, type_="column", grouping="clustered", cats=None):
    cats = cats or [str(i) for i in range(len(values))]
    return {"type": type_, "grouping": grouping, "title": None, "categories": cats,
            "series": [{"name": "s", "values": values, "x": None}],
            "axes": {"category": {"title": None}, "value": {"title": None, "min": None, "max": None}},
            "legend": {"position": "none"}}


class Ticks(unittest.TestCase):
    def test_nice_steps(self):
        self.assertEqual(cr.nice_ticks(0, 97), [0, 20, 40, 60, 80, 100])
        self.assertEqual(cr.nice_ticks(-3, 7), [-4, -2, 0, 2, 4, 6, 8])
        self.assertEqual(cr.nice_ticks(0, 0.5), [0, 0.1, 0.2, 0.3, 0.4, 0.5])

    def test_degenerate_ranges(self):
        self.assertEqual(cr.nice_ticks(5, 5), [0, 1, 2, 3, 4, 5])
        self.assertEqual(cr.nice_ticks(-5, -5), [-5, -4, -3, -2, -1, 0])
        self.assertEqual(cr.nice_ticks(0, 0), [0, 1])

    def test_format(self):
        self.assertEqual(cr.format_tick(20.0), "20")
        self.assertEqual(cr.format_tick(0.30000000000000004), "0.3")
        self.assertEqual(cr.format_tick(1500000.0), "1500000")


class Text(unittest.TestCase):
    def test_default_classes(self):
        self.assertAlmostEqual(cr.estimate_text_width("il", 10), 6.0)
        self.assertAlmostEqual(cr.estimate_text_width("MW", 10), 18.0)
        self.assertAlmostEqual(cr.estimate_text_width("09", 10), 12.0)
        self.assertAlmostEqual(cr.estimate_text_width("AB", 10), 14.4)
        self.assertAlmostEqual(cr.estimate_text_width("ab", 10), 11.2)
        self.assertAlmostEqual(cr.estimate_text_width("日", 10), 10.0)

    def test_table_overrides_classes(self):
        self.assertAlmostEqual(cr.estimate_text_width("ab", 10, {"a": 0.5, "b": 0.25}), 7.5)

    def test_truncate(self):
        self.assertEqual(cr.truncate_to_width("abc", 10, 1000), "abc")
        out = cr.truncate_to_width("a" * 80, 12, 60)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(cr.estimate_text_width(out, 12), 60)


class Cartesian(unittest.TestCase):
    def test_every_fixture_is_tiny_subset_and_in_bounds(self):
        for name in ["column_clustered", "column_stacked", "bar_clustered", "bar_stacked",
                     "line", "area_stacked", "scatter"]:
            out = cr.render(load(name), 800, 500)
            self.assertLessEqual(tags(out["svg"]), ALLOWED, name)
            self.assertEqual(len(out["marks"]), 35, name)          # 36 points, one gap
            for m in out["marks"]:
                self.assertTrue(0 <= m["x"] and m["x"] + m["w"] <= 800, name)
                self.assertTrue(0 <= m["y"] and m["y"] + m["h"] <= 500, name)

    def test_column_height_is_proportional(self):
        m = cr.render(one([10.0, 20.0]), 800, 500)["marks"]
        self.assertAlmostEqual(m[1]["h"], 2 * m[0]["h"], places=3)

    def test_stacked_negatives_go_below_zero(self):
        spec = one([5.0], grouping="stacked")
        spec["series"].append({"name": "t", "values": [-5.0], "x": None})
        up, down = cr.render(spec, 800, 500)["marks"]
        self.assertAlmostEqual(up["y"] + up["h"], down["y"], places=3)

    def test_degenerate_series_render(self):
        for values in ([None, None], [0.0, 0.0], [7.0]):
            ET.fromstring(cr.render(one(values), 800, 500)["svg"])

    def test_non_finite_is_a_gap(self):
        out = cr.render(one([1.0, math.nan, math.inf, 2.0]), 800, 500)
        self.assertEqual([m["point"] for m in out["marks"]], [0, 3])

    def test_text_is_escaped(self):
        spec = one([1.0], cats=['<a & "b">'])
        spec["title"] = "R&D <1> \u65e5\u672c"
        ET.fromstring(cr.render(spec, 800, 500)["svg"])

    def test_many_long_labels_stay_inside(self):
        spec = one([1.0] * 500, cats=["x" * 80 for _ in range(500)])
        root = ET.fromstring(cr.render(spec, 800, 500)["svg"])
        labels = [el for el in root.iter() if el.tag.endswith("text")]
        self.assertLess(len(labels), 120)
        self.assertTrue(all(len(el.text or "") < 80 for el in labels))

    def test_limits_and_size(self):
        for spec, w, h, code in [
            (one([1.0] * 10_001), 800, 500, "LIMIT_POINTS"),
            ({**one([1.0]), "series": [{"name": "s", "values": [1.0], "x": None}] * 65}, 800, 500, "LIMIT_SERIES"),
            (one([1.0]), 0, 500, "INVALID_SIZE"),
            (one([1.0], type_="radar"), 800, 500, "UNSUPPORTED_TYPE")]:
            with self.assertRaises(cr.ChartRenderError) as caught:
                cr.render(spec, w, h)
            self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
