# Charts Stage 1 (Renderer Experiment) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether a standard-library SVG renderer in the service, displayed through Qt SVG, is good enough to be DataHorizon's only chart renderer, and record the result.

**Architecture:** An isolated experiment in `tests/`, in the manner of the M1 experiments. A Python module turns a resolved chart (plain values, no workbook access) into SVG plus mark rectangles. A small Qt program loads those SVGs, rasterises them, measures real text widths and times loading and painting. A benchmark script runs both, applies the thresholds below and writes evidence. Nothing is installed, stored in workbooks or exposed through REST.

**Tech Stack:** Python 3.11+ standard library (`unittest`, `xml.etree`, `json`, `math`, `time`); C++ with Qt 6.8+ Core, Gui and Svg; CMake/CTest.

**Spec:** [charts-design.md](charts-design.md), sections 2, 5, 6 and 12 (stage 1).

## Global Constraints

- Standard library only in Python. No pip dependency, including test-only ones.
- The experiment lives in `tests/` and `docs/design/`. No file under `service/` or `desktop/` changes. No REST route, no native format change.
- SVG output stays inside a static SVG Tiny 1.2 subset: elements `svg`, `g`, `rect`, `line`, `path`, `circle`, `text` only; presentation attributes only; no `style` element, CSS, `clipPath`, gradients, filters, `use`, scripts or external references.
- Limits from the design: 10,000 points per series, 64 series per chart. Over a limit is a structured error, never truncation.
- First chart types: `column`, `bar`, `line`, `area`, `pie`, `scatter`; `grouping` is `clustered` or `stacked` for column, bar and area.
- Errors and text in a value series, and empty cells, arrive as `null` and plot as gaps.
- Number formatting is invariant: `.` decimal, no locale.
- Qt SVG is a new linked module. The CMake change must be optional (`QUIET`) so a build without Qt SVG still configures and the existing targets are untouched.
- Commit messages follow the repository's style: one imperative sentence, sentence case, no prefix.
- Work on a branch, not `main`.

**Thresholds (Call, open to the owner).** The route is confirmed when all hold on the reference Mac:

| Measure | Threshold |
| --- | --- |
| Python render, each type, 1 series × 10,000 points, 800×500, median of 20 | ≤ 250 ms |
| SVG size at 10,000 points | ≤ 4 MiB |
| Qt load + paint of that SVG, median of 20 | ≤ 150 ms |
| Render + load + paint at 12 points | ≤ 50 ms |
| Every fixture SVG | `QSvgRenderer::isValid()` is true |
| Text widths, each label, default and substituted font | measured ≤ estimate × 1.05 |
| Text widths, median of estimate ÷ measured | ≤ 1.35 |

If only the text rows fail, rerun them with the bundled metrics table (Task 4). If they then pass, the result is `service_svg_with_metrics_table`. Otherwise the result is `fallback_qcustomplot`.

## Review Focus

1. A series that is all gaps, all zero or a single value: the chart still renders with a sensible axis, with no division by zero.
2. Text containing `<`, `&`, `"` or non-ASCII characters: it is escaped and the SVG still parses.
3. Negative values in a stacked chart or a pie: stacked negatives stack downward from zero; a pie refuses with a structured error.
4. Many or long category labels (500 categories, 80-character labels): labels are thinned and truncated, never overlapped or drawn outside the canvas.
5. Non-finite numbers (`NaN`, `inf`) and a zero or negative canvas size: treated as a gap and as a structured error respectively.

Each has a test in the task that owns the code.

## File Structure

| File | Responsibility |
| --- | --- |
| `tests/chart_render_experiment.py` | Scales, ticks, text estimate, layout and SVG emission. The whole renderer |
| `tests/test_chart_render_experiment.py` | `unittest` cases; discovered by the existing `service` CTest |
| `tests/fixtures/charts/*.json` | Resolved-chart fixtures, one per type and grouping |
| `tests/chart_svg_probe.cpp` | Qt program: validity, PNG, timings, real text widths, metrics table |
| `tests/chart_render_benchmark.py` | Runs renderer and probe, applies thresholds, writes evidence |
| `CMakeLists.txt` | Optional `chart_svg_probe` target |
| `docs/design/chart-render-experiment.md` | The written result |
| `docs/design/evidence/chart-render-experiment.json`, `evidence/chart-render/*.png` | Evidence |

The resolved-chart input every task shares:

```json
{"type": "column", "grouping": "clustered", "title": "Units by month",
 "categories": ["Jan", "Feb"],
 "series": [{"name": "Units", "values": [12.0, null], "x": null}],
 "axes": {"category": {"title": null}, "value": {"title": null, "min": null, "max": null}},
 "legend": {"position": "right"}}
```

`categories` is `null` for `scatter`, whose series carry numeric `x`. `grouping` is `null` for line, pie and scatter. `legend.position` is `right`, `bottom` or `none`.

---

### Task 1: Scales, ticks and text estimate

**Files:**
- Create: `tests/chart_render_experiment.py`
- Test: `tests/test_chart_render_experiment.py`

**Interfaces:**
- Produces:
  - `LIMIT_POINTS = 10_000`, `LIMIT_SERIES = 64`
  - `class ChartRenderError(ValueError)` with attribute `code: str`
  - `nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]`
  - `format_tick(value: float) -> str`
  - `estimate_text_width(text: str, size_px: float, table: dict[str, float] | None = None) -> float`
  - `truncate_to_width(text: str, size_px: float, max_px: float, table: dict[str, float] | None = None) -> str`

- [ ] **Step 1: Write the failing tests**

```python
import unittest
import chart_render_experiment as cr

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
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest discover -s tests -p test_chart_render_experiment.py -v`
Expected: FAIL, `ModuleNotFoundError: chart_render_experiment`

- [ ] **Step 3: Implement the four functions and the error class**

`nice_ticks`: step is the smallest of 1, 2, 5 × 10^k that is ≥ (hi − lo) ÷ target; first tick is `floor(lo / step) * step`, last is `ceil(hi / step) * step`. When `lo == hi`, widen to include zero first; `(0, 0)` returns `[0, 1]`. Round each tick to the step's decimal places so `0.1 * 3` is `0.3`.

`estimate_text_width` default classes, in em: `iIjl.,:;|!'` and space 0.30; `mwMW@%` 0.90; digits 0.60; other uppercase ASCII 0.72; other ASCII 0.56; non-ASCII 1.00. A `table` entry for a character replaces its class.

- [ ] **Step 4: Run to verify pass**

Run: same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/chart_render_experiment.py tests/test_chart_render_experiment.py
git commit -m "Add tick and text-width helpers for the chart renderer experiment"
```

### Task 2: Cartesian renderer with marks

**Files:**
- Modify: `tests/chart_render_experiment.py`
- Create: `tests/fixtures/charts/column_clustered.json`, `column_stacked.json`, `bar_clustered.json`, `bar_stacked.json`, `line.json`, `area_stacked.json`, `scatter.json`
- Test: `tests/test_chart_render_experiment.py`

**Interfaces:**
- Consumes: everything Task 1 produces.
- Produces: `render(resolved: dict, width: int, height: int, metrics: dict[str, float] | None = None) -> dict`, returning `{"svg": str, "marks": list[dict]}`. Each mark is `{"series": int, "point": int, "x": float, "y": float, "w": float, "h": float}` in SVG pixels. Columns and bars: the drawn rectangle. Line, area and scatter: an 8×8 box centred on the point. Gaps produce no mark.
- Error codes on `ChartRenderError.code`: `LIMIT_POINTS`, `LIMIT_SERIES`, `INVALID_SIZE`, `UNSUPPORTED_TYPE`, `INVALID_SPEC`.

Fixtures: each has 12 categories (`Jan`…`Dec`) and 3 series named `North`, `South`, `West`, with one `null` in `South` at index 4; `scatter.json` has 3 series of 12 `(x, values)` pairs.

- [ ] **Step 1: Write the failing tests**

```python
import json, math, pathlib, xml.etree.ElementTree as ET
FIX = pathlib.Path(__file__).parent / "fixtures" / "charts"
ALLOWED = {"svg", "g", "rect", "line", "path", "circle", "text"}
def load(name): return json.loads((FIX / f"{name}.json").read_text())
def tags(svg): return {el.tag.split("}")[-1] for el in ET.fromstring(svg).iter()}
def one(values, type_="column", grouping="clustered", cats=None):
    cats = cats or [str(i) for i in range(len(values))]
    return {"type": type_, "grouping": grouping, "title": None, "categories": cats,
            "series": [{"name": "s", "values": values, "x": None}],
            "axes": {"category": {"title": None}, "value": {"title": None, "min": None, "max": None}},
            "legend": {"position": "none"}}

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
        spec["title"] = "R&D <1> 日本"
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest discover -s tests -p test_chart_render_experiment.py -v`
Expected: FAIL, `AttributeError: ... has no attribute 'render'`

- [ ] **Step 3: Implement `render` for column, bar, line, area and scatter**

Layout is computed from text estimates only: plot area = canvas minus title (14 px text), value-axis labels (widest tick label), category labels (12 px text), legend and an 8 px margin. Column, bar and area value axes always include zero. Category labels are thinned to every n-th so neighbours do not overlap by estimate, and each is truncated to its slot with `truncate_to_width`. A line or area with gaps breaks into separate path segments. Text uses `font-family="sans-serif"`. Series colours cycle through `#3b6fb6 #d9822b #4f9d69 #b8475f #7b61a8 #8a8f98 #c2a633 #2f9aa0`; this is a placeholder palette for the experiment, not a product decision. Coordinates are written with at most 2 decimal places.

- [ ] **Step 4: Run to verify pass**

Run: same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/chart_render_experiment.py tests/test_chart_render_experiment.py tests/fixtures/charts
git commit -m "Render column, bar, line, area and scatter charts to SVG in the experiment"
```

### Task 3: Pie, legend and title

**Files:**
- Modify: `tests/chart_render_experiment.py`
- Create: `tests/fixtures/charts/pie.json` (one series `Share`, 6 categories, all positive)
- Test: `tests/test_chart_render_experiment.py`

**Interfaces:**
- Consumes: `render` from Task 2.
- Produces: `render` accepts `type == "pie"`. A pie mark is the slice's bounding box plus `"a0"` and `"a1"`, the slice's start and end angles in degrees clockwise from 12 o'clock. Additional error code `NEGATIVE_IN_PIE`. A pie uses the first series only.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest discover -s tests -p test_chart_render_experiment.py -v`
Expected: FAIL in `PieAndLegend` (`UNSUPPORTED_TYPE` for pie; legend assertions).

- [ ] **Step 3: Implement pie slices and the legend**

Slices are `path` arcs. The legend lists series (categories for a pie) with a 10 px swatch; a right-hand legend takes at most 30% of the canvas width and truncates names to fit.

- [ ] **Step 4: Run to verify pass**

Run: same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/chart_render_experiment.py tests/test_chart_render_experiment.py tests/fixtures/charts/pie.json
git commit -m "Add pie charts and legends to the chart renderer experiment"
```

### Task 4: Qt SVG probe

**Files:**
- Create: `tests/chart_svg_probe.cpp`
- Modify: `CMakeLists.txt` (inside the existing tests block, after `export_failure_probe`)

**Interfaces:**
- Consumes: SVG files and a labels file written by Task 5's script; for this task, hand-made inputs.
- Produces: executable `chart_svg_probe`:
  - `chart_svg_probe --svg-dir <dir> --out <dir> --labels <labels.json> --report <report.json> [--font <family>]`
  - `chart_svg_probe --emit-metrics <family> <table.json>`
  - `labels.json`: `[{"text": str, "size_px": number, "estimate": number}]`
  - `report.json`: `{"qt": str, "default_font": str, "substituted_font": str | null, "charts": [{"file": str, "valid": bool, "load_ms": number, "paint_ms": number, "png": str}], "labels": [{"text": str, "size_px": number, "estimate": number, "default_px": number, "substituted_px": number | null}]}`
  - `table.json`: `{"family": str, "resolved_family": str, "advances": {"<char>": em}}` for U+0020–U+007E and U+00A0–U+00FF.
  - Exit code 0 when it ran, 2 on bad arguments or an unreadable input. An invalid SVG is reported, not an exit failure.

- [ ] **Step 1: Add the optional target**

```cmake
find_package(Qt6 QUIET COMPONENTS Svg)
if(Qt6Svg_FOUND)
    add_executable(chart_svg_probe tests/chart_svg_probe.cpp)
    target_link_libraries(chart_svg_probe PRIVATE Qt6::Core Qt6::Gui Qt6::Svg)
    add_test(NAME chart_svg_probe_smoke COMMAND chart_svg_probe --self-test)
    set_tests_properties(chart_svg_probe_smoke PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen" TIMEOUT 30)
endif()
```

- [ ] **Step 2: Write the self-test first**

`--self-test` renders a built-in 100×100 SVG holding one red `rect` covering the left half to a `QImage`, and exits 0 only if pixel (25, 50) is red and pixel (75, 50) is white, and a built-in malformed document reports `valid == false`. It exits 1 otherwise.

- [ ] **Step 3: Run to verify failure**

Run: `cmake -S . -B build && cmake --build build --target chart_svg_probe && ctest --test-dir build -R chart_svg_probe_smoke --output-on-failure`
Expected: build error or FAIL until the program exists.

- [ ] **Step 4: Implement the probe**

Use `QGuiApplication`, `QSvgRenderer`, `QImage` (ARGB32 premultiplied, white fill, the SVG's own size) and `QElapsedTimer`. `load_ms` and `paint_ms` are each the median of 20 runs. Text widths use `QFontMetricsF::horizontalAdvance` with a pixel-sized `QFont` of family `sans-serif` resolved by Qt (record the resolved family as `default_font`) and of `--font` when given. `--emit-metrics` measures each character at 1000 px and divides by 1000.

- [ ] **Step 5: Run to verify pass**

Run: same command as Step 3. Expected: `chart_svg_probe_smoke` PASS. Also confirm `ctest --test-dir build -R "^desktop$|^service$" --output-on-failure` still passes.

- [ ] **Step 6: Commit**

```bash
git add CMakeLists.txt tests/chart_svg_probe.cpp
git commit -m "Add an optional Qt SVG probe for the chart renderer experiment"
```

### Task 5: Benchmark, evidence and written result

**Files:**
- Create: `tests/chart_render_benchmark.py`
- Create: `docs/design/chart-render-experiment.md`, `docs/design/evidence/chart-render-experiment.json`, `docs/design/evidence/chart-render/*.png`
- Modify: `docs/design/README.md` (document map), `docs/design/implementation-status.md` (new dated section at the top), `docs/design/charts-design.md` (section 6: link the result; section 12: stage 1 outcome), `LICENSES.md` only if the result keeps Qt SVG (note that Windows and Linux materials are still owed)
- Test: `tests/test_chart_render_experiment.py`

**Interfaces:**
- Consumes: `render`, `estimate_text_width` (Tasks 1–3); `chart_svg_probe` and its file formats (Task 4).
- Produces:
  - `evaluate(measurements: dict) -> dict` in `tests/chart_render_benchmark.py`, returning `{"route": "service_svg" | "service_svg_with_metrics_table" | "fallback_qcustomplot", "failed": list[str]}`
  - CLI: `python3 tests/chart_render_benchmark.py --probe build/chart_svg_probe --work build/chart-render --evidence docs/design/evidence [--font <family>]`
  - `measurements` keys: `render_ms_10k: {type: ms}`, `svg_bytes_10k: {type: int}`, `qt_ms_10k: {type: ms}`, `total_ms_12: {type: ms}`, `all_valid: bool`, `text: {"max_under": float, "median_over": float}`, `text_with_table: {"max_under": float, "median_over": float} | None`. `max_under` is the largest measured ÷ estimate; `median_over` is the median estimate ÷ measured; both taken across the default and substituted fonts.

- [ ] **Step 1: Write the failing tests for `evaluate`**

```python
import chart_render_benchmark as bench
TYPES = ["column", "bar", "line", "area", "pie", "scatter"]
def good():
    return {"render_ms_10k": {t: 100.0 for t in TYPES}, "svg_bytes_10k": {t: 1_000_000 for t in TYPES},
            "qt_ms_10k": {t: 50.0 for t in TYPES}, "total_ms_12": {t: 10.0 for t in TYPES},
            "all_valid": True, "text": {"max_under": 1.0, "median_over": 1.2}, "text_with_table": None}

class Evaluate(unittest.TestCase):
    def test_all_within_thresholds(self):
        self.assertEqual(bench.evaluate(good()), {"route": "service_svg", "failed": []})
    def test_text_failure_rescued_by_table(self):
        m = good(); m["text"]["max_under"] = 1.2
        m["text_with_table"] = {"max_under": 1.01, "median_over": 1.05}
        self.assertEqual(bench.evaluate(m)["route"], "service_svg_with_metrics_table")
    def test_text_failure_not_rescued(self):
        m = good(); m["text"]["median_over"] = 1.5
        m["text_with_table"] = {"max_under": 1.2, "median_over": 1.05}
        self.assertEqual(bench.evaluate(m)["route"], "fallback_qcustomplot")
    def test_speed_or_validity_failure_is_fallback(self):
        for key, value in [("render_ms_10k", 251.0), ("qt_ms_10k", 151.0),
                           ("svg_bytes_10k", 4 * 1024 * 1024 + 1), ("total_ms_12", 50.1)]:
            m = good(); m[key]["line"] = value
            out = bench.evaluate(m)
            self.assertEqual(out["route"], "fallback_qcustomplot")
            self.assertEqual(out["failed"], [f"{key}.line"])
        m = good(); m["all_valid"] = False
        self.assertEqual(bench.evaluate(m), {"route": "fallback_qcustomplot", "failed": ["all_valid"]})
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest discover -s tests -p test_chart_render_experiment.py -v`
Expected: FAIL, `ModuleNotFoundError: chart_render_benchmark`

- [ ] **Step 3: Implement `evaluate` and the benchmark CLI**

The CLI renders every fixture at 800×500 and at 400×250, plus one 10,000-point, one-series variant per type (values `sin(i / 50) * 100 + 100`), into `--work`. It times `render` with `time.perf_counter` (median of 20). The labels file holds every text the renderer emitted across the fixtures plus these stress strings at 12 px: `"WWWWWWWWWW"`, `"iiiiiiiiii"`, `"1,234,567.89"`, `"Quarterly revenue (EUR)"`, `"Ærøskøbing Größe façade"`. It runs the probe once with the default table, then, when `--font` is given, runs `--emit-metrics` for the default font and repeats the label estimates with that table to fill `text_with_table`. It copies the 800×500 PNGs to `--evidence/chart-render/` and writes `chart-render-experiment.json` with the machine, Qt and Python versions, both fonts, all measurements and the `evaluate` result. When `--probe` is missing it exits 2 with a message; it never writes a result that omits the Qt measurements.

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m unittest discover -s tests -p test_chart_render_experiment.py -v`
Expected: PASS.

- [ ] **Step 5: Run the experiment on the reference Mac**

Run without `QT_QPA_PLATFORM=offscreen`, so real system fonts are measured:

```bash
python3 tests/chart_render_benchmark.py --probe build/chart_svg_probe --work build/chart-render --evidence docs/design/evidence --font "Arial"
```

Expected: exit 0, `docs/design/evidence/chart-render-experiment.json` written with a `route`, and seven-plus PNGs under `docs/design/evidence/chart-render/`. If `Arial` resolves to the default family, rerun with another installed family and record which.

- [ ] **Step 6: Look at the PNGs**

Open every PNG. Record in the result document anything a threshold cannot see: overlapping or clipped text, a legend that crowds the plot, unreadable tick density. A defect here is a finding to report, not something to tune the thresholds around.

- [ ] **Step 7: Write `docs/design/chart-render-experiment.md`**

Follow the shape of [grid-frozen-row-experiment.md](grid-frozen-row-experiment.md): date and one-paragraph result first; design and contract; the measurement table with every threshold and the measured value; visual findings; limits (macOS only, one machine, estimated resize latency rather than a measured REST round trip, pie marks as bounding boxes plus angles); and the route with what it means for stage 2. State plainly whether the thresholds were met. Do not describe the route as confirmed for Windows or Linux.

- [ ] **Step 8: Update the design documents and run the full suites**

Add the result to the README document map and a dated section at the top of `implementation-status.md`; in `charts-design.md` link the result from section 6 and record the stage 1 outcome in section 12.

Run: `ctest --test-dir build --output-on-failure`
Expected: all tests pass, including `service` and `chart_svg_probe_smoke`.

- [ ] **Step 9: Commit**

```bash
git add tests/chart_render_benchmark.py tests/test_chart_render_experiment.py docs/design
git commit -m "Record the chart renderer experiment result and evidence"
```
