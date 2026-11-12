# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the charts stage 1 renderer experiment (not product code)."""

import json
import math
import pathlib
import unittest
import xml.etree.ElementTree as ET

import chart_render_benchmark as bench
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


class PieAndLegend(unittest.TestCase):
    def test_pie_angles_cover_the_circle(self):
        out = cr.render(load("pie"), 800, 500)
        self.assertLessEqual(tags(out["svg"]), ALLOWED)
        marks = out["marks"]
        self.assertEqual(len(marks), 6)
        self.assertAlmostEqual(marks[0]["a0"], 0.0)
        self.assertAlmostEqual(marks[-1]["a1"], 360.0)
        self.assertAlmostEqual(sum(m["a1"] - m["a0"] for m in marks), 360.0)

    def test_pie_refuses_negatives_and_skips_gaps(self):
        with self.assertRaises(cr.ChartRenderError) as caught:
            cr.render(one([1.0, -1.0], type_="pie", grouping=None), 800, 500)
        self.assertEqual(caught.exception.code, "NEGATIVE_IN_PIE")
        out = cr.render(one([1.0, None, 3.0], type_="pie", grouping=None), 800, 500)
        self.assertEqual([m["point"] for m in out["marks"]], [0, 2])

    def test_all_zero_pie_renders_without_slices(self):
        out = cr.render(one([0.0, 0.0], type_="pie", grouping=None), 800, 500)
        self.assertEqual(out["marks"], [])
        ET.fromstring(out["svg"])

    def test_legend_positions_change_the_plot_area(self):
        spec = load("column_clustered")
        widths = {}
        for pos in ("right", "bottom", "none"):
            spec["legend"]["position"] = pos
            marks = cr.render(spec, 800, 500)["marks"]
            widths[pos] = max(m["x"] + m["w"] for m in marks)
        self.assertLess(widths["right"], widths["none"])
        self.assertAlmostEqual(widths["bottom"], widths["none"], places=3)

    def test_long_series_names_do_not_starve_the_plot(self):
        spec = load("column_clustered")
        for s in spec["series"]:
            s["name"] = "n" * 200
        marks = cr.render(spec, 800, 500)["marks"]
        self.assertGreater(max(m["x"] + m["w"] for m in marks), 400)


TYPES = ["column", "bar", "line", "area", "pie", "scatter"]


def good():
    return {"render_ms_10k": {t: 100.0 for t in TYPES}, "svg_bytes_10k": {t: 1_000_000 for t in TYPES},
            "qt_ms_10k": {t: 50.0 for t in TYPES}, "total_ms_12": {t: 10.0 for t in TYPES},
            "all_valid": True, "text": {"max_under": 1.0, "median_over": 1.2}, "text_with_table": None}


class Evaluate(unittest.TestCase):
    def test_all_within_thresholds(self):
        self.assertEqual(bench.evaluate(good()), {"route": "service_svg", "failed": []})

    def test_text_failure_rescued_by_table(self):
        m = good()
        m["text"]["max_under"] = 1.2
        m["text_with_table"] = {"max_under": 1.01, "median_over": 1.05}
        self.assertEqual(bench.evaluate(m)["route"], "service_svg_with_metrics_table")

    def test_text_failure_not_rescued(self):
        m = good()
        m["text"]["median_over"] = 1.5
        m["text_with_table"] = {"max_under": 1.2, "median_over": 1.05}
        self.assertEqual(bench.evaluate(m)["route"], "fallback_qcustomplot")

    def test_speed_or_validity_failure_is_fallback(self):
        for key, value in [("render_ms_10k", 251.0), ("qt_ms_10k", 151.0),
                           ("svg_bytes_10k", 4 * 1024 * 1024 + 1), ("total_ms_12", 50.1)]:
            m = good()
            m[key]["line"] = value
            out = bench.evaluate(m)
            self.assertEqual(out["route"], "fallback_qcustomplot")
            self.assertEqual(out["failed"], [f"{key}.line"])
        m = good()
        m["all_valid"] = False
        self.assertEqual(bench.evaluate(m), {"route": "fallback_qcustomplot", "failed": ["all_valid"]})


if __name__ == "__main__":
    unittest.main()
