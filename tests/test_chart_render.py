# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the charts stage 1 renderer experiment (not product code)."""

import json
import math
import pathlib
import unittest
import xml.etree.ElementTree as ET

import chart_render_benchmark as bench
from datahorizon import chart_font
from datahorizon import chart_render as cr

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


class RenderPoints(unittest.TestCase):
    def chart(self, *lengths):
        spec = one([1.0] * lengths[0], cats=[""] * max(lengths))
        spec["series"] = [{"name": f"s{i}", "values": [float(n % 7) for n in range(length)], "x": None}
                          for i, length in enumerate(lengths)]
        return spec

    def test_twenty_thousand_values_render(self):
        self.assertEqual(cr.LIMIT_RENDER_POINTS, 20_000)
        self.assertIn("<svg", cr.render(self.chart(10_000, 10_000), 800, 500)["svg"])

    def test_more_values_across_series_are_refused_before_drawing(self):
        with self.assertRaises(cr.ChartRenderError) as caught:
            cr.render(self.chart(10_000, 10_000, 1), 800, 500)
        self.assertEqual((caught.exception.code, caught.exception.status), ("LIMIT_RENDER_POINTS", 400))


class PieAndLegend(unittest.TestCase):
    def test_pie_refuses_more_than_the_pie_limit(self):
        ok = one([1.0] * cr.LIMIT_PIE_POINTS, type_="pie", grouping=None)
        self.assertIn("<svg", cr.render(ok, 800, 500)["svg"])
        with self.assertRaises(cr.ChartRenderError) as caught:
            cr.render(one([1.0] * (cr.LIMIT_PIE_POINTS + 1), type_="pie", grouping=None), 800, 500)
        self.assertEqual(caught.exception.code, "LIMIT_PIE_POINTS")

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


class FontInSvg(unittest.TestCase):
    def test_ratios_and_the_bundled_font_window(self):
        self.assertEqual(bench.font_check(536, 459, 540.0),
                         {"laid_out_px": 540.0, "bundled_px": 536, "unknown_family_px": 459,
                          "bundled_ratio": 536 / 540.0, "unknown_family_ratio": 459 / 540.0, "ok": True})
        self.assertTrue(bench.font_check(567, 459, 540.0)["ok"])  # 1.05
        self.assertFalse(bench.font_check(568, 459, 540.0)["ok"])
        self.assertFalse(bench.font_check(459, 459, 540.0)["ok"])

    def test_probe_svg_names_one_family(self):
        root = ET.fromstring(bench.font_svg("DejaVu Sans"))
        text = [el for el in root.iter() if el.tag.endswith("text")]
        self.assertEqual([(el.get("font-family"), el.text) for el in text], [("DejaVu Sans", bench.FONT_TEXT)])


class PieLimit(unittest.TestCase):
    def test_sizes_parse_sorted_and_positive(self):
        self.assertEqual(bench.pie_sizes("2000,500,1000"), [500, 1000, 2000])
        for bad in ("", "0,500", "a", "500,-1"):
            with self.assertRaises(ValueError):
                bench.pie_sizes(bad)

    def test_largest_size_within_half_the_qt_threshold(self):
        self.assertEqual(bench.choose_pie_limit({500: 20.0, 1000: 75.0, 2000: 75.1}),
                         {"limit": 1000, "fallback": False})

    def test_a_slower_smaller_size_does_not_hide_a_faster_larger_one(self):
        self.assertEqual(bench.choose_pie_limit({500: 80.0, 1000: 70.0}), {"limit": 1000, "fallback": False})

    def test_none_within_budget_falls_back_to_250(self):
        self.assertEqual(bench.choose_pie_limit({500: 76.0, 1000: 150.0}), {"limit": 250, "fallback": True})


class Capacity(unittest.TestCase):
    def run_of(self, wall=(1.0, 2.0), size=(1000, 2000), error=None):
        return {"wall_s": list(wall), "result_bytes": list(size), "error": error}

    def test_within_budget_and_size_passes(self):
        self.assertEqual(bench.capacity_failures({"line_8": self.run_of(), "column_1": self.run_of()}), [])

    def test_time_size_and_errors_are_named(self):
        runs = {"line_8": self.run_of(wall=(1.0, 30.5)), "column_1": self.run_of(size=(4 * 1024 * 1024 + 1,)),
                "pie": self.run_of(wall=(), size=(), error="READ_LIMIT")}
        self.assertEqual(bench.capacity_failures(runs), ["column_1.result_bytes", "line_8.wall_s", "pie.error"])


def text_extent(el, size_px):
    width = cr.estimate_text_width(el.text or "", size_px, chart_font.default_metrics())
    x = float(el.get("x"))
    anchor = el.get("text-anchor")
    if anchor == "middle":
        return x - width / 2, x + width / 2
    return (x - width, x) if anchor == "end" else (x, x + width)


def outside_canvas(svg, width, height):
    """Shapes and unrotated text that reach outside the canvas."""
    bad = []
    for el in ET.fromstring(svg).iter():
        tag = el.tag.split("}")[-1]
        get = lambda name: float(el.get(name))
        if tag == "rect":
            box = (get("x"), get("y"), get("x") + get("width"), get("y") + get("height"))
        elif tag == "line":
            box = (min(get("x1"), get("x2")), min(get("y1"), get("y2")), max(get("x1"), get("x2")), max(get("y1"), get("y2")))
        elif tag == "circle":
            box = (get("cx"), get("cy"), get("cx"), get("cy"))
        elif tag == "text" and el.get("transform") is None:
            lo, hi = text_extent(el, get("font-size"))
            box = (lo, get("y") - get("font-size"), hi, get("y"))
        else:
            continue
        if box[0] < -0.01 or box[1] < -0.01 or box[2] > width + 0.01 or box[3] > height + 0.01:
            bad.append((tag, box))
    return bad


def overlapping_labels(svg):
    """Pairs of unrotated labels on one text row whose estimated extents overlap."""
    rows = {}
    for el in ET.fromstring(svg).iter():
        if el.tag.endswith("text") and el.get("transform") is None:
            rows.setdefault(el.get("y"), []).append(text_extent(el, float(el.get("font-size"))))
    return [(a, b) for row in rows.values() for a, b in zip(sorted(row), sorted(row)[1:]) if a[1] > b[0] + 0.01]


def every_type():
    for name in ["column_clustered", "column_stacked", "bar_clustered", "bar_stacked",
                 "line", "area_stacked", "scatter", "pie"]:
        yield name, load(name)


class ReviewFixes(unittest.TestCase):
    def test_forced_axis_range_keeps_shapes_inside_the_plot(self):
        for type_, grouping, expected in [("column", "clustered", [2]), ("bar", "clustered", [2]),
                                          ("area", "clustered", [0, 1, 2]), ("line", None, []), ("scatter", None, [])]:
            spec = one([1.0, 2.0, 300.0], type_=type_, grouping=grouping)
            spec["axes"]["value"].update(min=10, max=20)
            if type_ == "scatter":
                spec["categories"], spec["series"][0]["x"] = None, [1.0, 2.0, 3.0]
            out = cr.render(spec, 800, 500)
            self.assertEqual(outside_canvas(out["svg"], 800, 500), [], type_)
            self.assertEqual([m["point"] for m in out["marks"]], expected, type_)
            for m in out["marks"]:
                self.assertLess(m["h"], 500, type_)
                self.assertLess(m["w"], 800, type_)

    def test_overflowing_values_are_refused_with_a_code(self):
        specs = [one([1e308, -1e308], type_=t, grouping=g)
                 for t, g in [("column", "clustered"), ("bar", "clustered"), ("line", None), ("area", "clustered")]]
        stacked = one([1e308], grouping="stacked")
        stacked["series"].append({"name": "t", "values": [1e308], "x": None})
        scatter = one([1.0, 2.0], type_="scatter", grouping=None)
        scatter["categories"], scatter["series"][0]["x"] = None, [1e308, -1e308]
        specs += [stacked, scatter, one([1e308, 1e308, 5.0], type_="pie", grouping=None)]
        for spec in specs:
            with self.assertRaises(cr.ChartRenderError) as caught:
                cr.render(spec, 800, 500)
            self.assertEqual(caught.exception.code, "VALUE_RANGE", spec["type"])

    def test_small_canvases_stay_inside_or_are_refused(self):
        for name, spec in every_type():
            spec["axes"]["value"]["title"] = "Units sold"
            spec["axes"]["category"]["title"] = "Month"
            for legend in ("right", "bottom"):
                spec["legend"]["position"] = legend
                for width, height in [(64, 48), (120, 90), (200, 150), (300, 200)]:
                    out = cr.render(spec, width, height)
                    self.assertEqual(outside_canvas(out["svg"], width, height), [], (name, legend, width, height))
        for width, height in [(63, 48), (64, 47), (20, 20), (1, 1)]:
            with self.assertRaises(cr.ChartRenderError) as caught:
                cr.render(load("column_clustered"), width, height)
            self.assertEqual(caught.exception.code, "INVALID_SIZE")

    def test_value_tick_labels_never_overlap(self):
        for name, spec in every_type():
            for item in spec["series"]:
                item["values"] = [None if v is None else v * 1e6 for v in item["values"]]
                if item["x"] is not None:
                    item["x"] = [x * 1e6 for x in item["x"]]
            for width, height in [(200, 150), (300, 200), (800, 500)]:
                svg = cr.render(spec, width, height)["svg"]
                self.assertEqual(overlapping_labels(svg), [], (name, width, height))
                self.assertEqual(outside_canvas(svg, width, height), [], (name, width, height))

    def test_many_long_labels_never_overlap_or_leave_the_canvas(self):
        for type_, grouping in [("column", "clustered"), ("bar", "stacked"), ("line", None), ("area", "stacked")]:
            spec = one([1.0] * 500, type_=type_, grouping=grouping, cats=["x" * 80 for _ in range(500)])
            for width, height in [(800, 500), (300, 200)]:
                svg = cr.render(spec, width, height)["svg"]
                self.assertEqual(overlapping_labels(svg), [], (type_, width))
                self.assertEqual(outside_canvas(svg, width, height), [], (type_, width))

    def test_text_failures_are_listed_beside_other_failures(self):
        m = good()
        m["qt_ms_10k"]["pie"] = 400.0
        m["text"]["max_under"] = 1.2
        m["text_with_table"] = {"max_under": 1.2, "median_over": 1.0}
        self.assertEqual(bench.evaluate(m), {"route": "fallback_qcustomplot",
                                             "failed": ["qt_ms_10k.pie", "text", "text_with_table"]})


def fills(svg, colour):
    """Vertex lists of the filled paths drawn in ``colour``."""
    out = []
    for el in ET.fromstring(svg).iter():
        if el.tag.endswith("path") and el.get("fill") == colour:
            body = el.get("d").lstrip("M").rstrip("Z ").split(" L")
            out.append([tuple(float(n) for n in pair.split()) for pair in body])
    return out


def circles(svg):
    return [(float(el.get("cx")), float(el.get("cy"))) for el in ET.fromstring(svg).iter()
            if el.tag.endswith("circle")]


class PullRequestReview(unittest.TestCase):
    def test_screenshots_are_copied_only_for_fixtures_qt_rendered(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            png, shots = pathlib.Path(folder, "png"), pathlib.Path(folder, "shots")
            png.mkdir()
            (png / "pie.png").write_bytes(b"x")
            charts = {"pie.svg": {"valid": True}, "line.svg": {"valid": False}}
            copied = bench.copy_screenshots(charts, ["line", "pie"], png, shots)
            self.assertEqual(copied, ["pie"])
            self.assertEqual(sorted(p.name for p in shots.iterdir()), ["pie.png"])

    def test_explicit_axis_limits_are_the_plotted_range(self):
        spec = one([2.1, 5.0, 7.95, 7.0], type_="line", grouping=None, cats=list("abcd"))
        spec["axes"]["value"].update(min=2.4, max=7.9)
        out = cr.render(spec, 800, 500)
        self.assertEqual([m["point"] for m in out["marks"]], [1, 3])
        labels = [el.text for el in ET.fromstring(out["svg"]).iter() if el.tag.endswith("text")]
        for tick in ("2", "8"):
            self.assertNotIn(tick, labels)
        for tick in ("4", "6"):
            self.assertIn(tick, labels)
        column = one([2.1, 5.0], grouping="clustered")
        column["axes"]["value"].update(min=2.4, max=7.9)
        self.assertEqual([m["point"] for m in cr.render(column, 800, 500)["marks"]], [1])

    def test_one_explicit_axis_limit_leaves_the_other_end_automatic(self):
        spec = one([2.1, 5.0, 7.5], type_="line", grouping=None)
        spec["axes"]["value"]["min"] = 2.4
        self.assertEqual([m["point"] for m in cr.render(spec, 800, 500)["marks"]], [1, 2])
        above = one([1.0, 2.0], type_="line", grouping=None)
        above["axes"]["value"]["min"] = 10
        out = cr.render(above, 800, 500)
        self.assertEqual(out["marks"], [])
        self.assertEqual(outside_canvas(out["svg"], 800, 500), [])

    def test_stacked_area_does_not_join_positive_and_negative_spans(self):
        spec = one([5.0, 5.0, 5.0], type_="area", grouping="stacked")
        spec["series"].append({"name": "t", "values": [2.0, -2.0, 2.0], "x": None})
        out = cr.render(spec, 800, 500)
        base = fills(out["svg"], cr.PALETTE[0])
        self.assertEqual(len(base), 1)
        top_of_base = min(y for _, y in base[0])
        zero = max(y for _, y in base[0])
        upper = fills(out["svg"], cr.PALETTE[1])
        self.assertTrue(upper)
        for polygon in upper:
            ys = [y for _, y in polygon]
            self.assertTrue(max(ys) <= top_of_base + 0.01 or min(ys) >= zero - 0.01, polygon)
        self.assertEqual(sorted(m["point"] for m in out["marks"] if m["series"] == 1), [0, 1, 2])

    def test_scatter_axes_come_from_complete_pairs(self):
        spec = one([1.0, 2.0, None], type_="scatter", grouping=None)
        spec["categories"], spec["series"][0]["x"] = None, [1.0, 2.0, 1_000_000.0]
        dots = circles(cr.render(spec, 800, 500)["svg"])
        self.assertEqual(len(dots), 2)
        self.assertGreater(abs(dots[0][0] - dots[1][0]), 100)
        flipped = one([1.0, 2.0, 1_000_000.0], type_="scatter", grouping=None)
        flipped["categories"], flipped["series"][0]["x"] = None, [1.0, 2.0, None]
        dots = circles(cr.render(flipped, 800, 500)["svg"])
        self.assertEqual(len(dots), 2)
        self.assertGreater(abs(dots[0][1] - dots[1][1]), 100)

    def test_float_extremes_are_refused_with_a_code_or_drawn(self):
        for value in (1.79e308, -1.79e308, 5e-324, -5e-324, 1e-320):
            for type_, grouping in [("column", "clustered"), ("bar", "clustered"), ("line", None),
                                    ("area", "stacked"), ("scatter", None)]:
                spec = one([value], type_=type_, grouping=grouping)
                if type_ == "scatter":
                    spec["categories"], spec["series"][0]["x"] = None, [value]
                try:
                    out = cr.render(spec, 800, 500)
                except cr.ChartRenderError as error:
                    self.assertEqual(error.code, "VALUE_RANGE", (value, type_))
                else:
                    self.assertEqual(outside_canvas(out["svg"], 800, 500), [], (value, type_))
            for call in (lambda: cr.nice_ticks(0, value), lambda: cr.nice_ticks(value, value)):
                try:
                    ticks = call()
                except cr.ChartRenderError as error:
                    self.assertEqual(error.code, "VALUE_RANGE")
                else:
                    self.assertTrue(all(math.isfinite(t) for t in ticks))


class ServiceErrors(unittest.TestCase):
    def test_render_errors_are_engine_errors(self):
        from datahorizon.engine import EngineError
        with self.assertRaises(EngineError) as caught:
            cr.render(one([1.0], type_="radar"), 800, 500)
        self.assertEqual((caught.exception.code, caught.exception.status), ("UNSUPPORTED_TYPE", 400))
        self.assertEqual(caught.exception.body["code"], "UNSUPPORTED_TYPE")


if __name__ == "__main__":
    unittest.main()
