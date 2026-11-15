# Charts on Open: Import and View-Only Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Charts in an XLSX or ODS file become native charts on import, and the desktop draws every native chart over its sheet, view-only.

**Architecture:** Each format gets one chart reader that turns a chart part into a format-neutral *draft*. A shared module converts a draft into an *imported chart record* (a `chart_create` request keyed by worksheet position), applying the design's keep, convert and skip rules and writing the report. Records travel with the reviewed import (stage file, inline request, or the one-sheet `edit`) and are created in the same engine operation as the cells. The desktop adds a chart layer that lists a sheet's charts through the existing catalog read, renders them through existing chart jobs, and shows the SVG in mouse-transparent overlay widgets.

**Tech Stack:** Python 3.11 standard library (service, `unittest`); Qt 6.8 Widgets, Network, Svg, Test with C++ (desktop); CMake and CTest.

**Spec:** [charts-import-view-design.md](charts-import-view-design.md). Read it first; section numbers below refer to it.

## Global Constraints

- Work on a branch `claude/charts-import-view` cut from `main`. Every source file starts with `# SPDX-License-Identifier: GPL-3.0-only` (or the `//` form).
- Service: Python standard library only. No new third-party packages.
- Specification version 1 and the native format are unchanged. An imported chart is exactly what `chart_create` writes.
- A malformed, oversized or unsupported chart never fails an import and never changes which cells are imported.
- Nothing is dropped silently: every skipped chart and every dropped property has a report entry naming the chart and its sheet.
- A source never points at different cells than in the file. If it cannot be bound exactly, the chart is skipped.
- Uploaded packages are untrusted: chart, drawing and relationship parts are read only through the existing budgeted readers (`xlsx_stream._read_whole` / `ByteBudget`, `interchange.read_xml`), and relationship targets are resolved inside the package only.
- Limits, verbatim: 64 charts per workbook, 64 series per chart, 10,000 cells per source (1,000 for a pie), 100,000 source cells per chart, title 255 characters, name 1 to 64 characters, size 64×48 to 4096×4096 pixels, offset 0 to 4096.
- The desktop reaches charts only through REST. It does not draw charts itself and adds no REST endpoint.
- Whole-workbook import still clears undo. Only the one-sheet `edit` is undoable.
- Service tests: `PYTHONPATH=service python3 -m unittest tests.<module> -v` from the repository root. Desktop tests: `cmake --build build --target desktop_tests && ctest --test-dir build -R '^desktop$' --output-on-failure`.

## Review Focus

Each line is pinned by a test in the task named.

1. **Files not written by Excel** (LibreOffice, openpyxl): one-cell and absolute anchors, no title, no categories, no series name. Expected: the chart imports with defaults. Task 2.
2. **Sheet names needing quotes in references** (`'Bob''s Q1'!$A$2:$A$8`, non-ASCII, ODS `$'Bob''s Q1'.$A$2:.$A$8`). Expected: bound to the right sheet. Task 1.
3. **Whole-column and oversized references** (`Sheet1!$B:$B`, 20,000-cell ranges). Expected: chart skipped with the limit named; import succeeds. Task 1.
4. **Workbook or sheet changes while renders are in flight.** Expected: late results are discarded, their jobs released, no overlay from the old sheet appears. Task 8.
5. **One-sheet import at an offset that pushes a chart off the grid.** Expected: that chart is skipped and reported; the cells import as they would without it. Task 5.

## File Structure

| File | Responsibility |
| --- | --- |
| Create `service/datahorizon/import_charts.py` | Draft-to-record conversion rules, reference parsing, record validation, shifting, and creating records in an engine operation |
| Create `service/datahorizon/xlsx_charts.py` | Read a worksheet's drawing and chart parts into drafts |
| Create `service/datahorizon/ods_charts.py` | Read a `draw:frame` chart object into a draft |
| Modify `service/datahorizon/import_stage.py` | Hold records in the stage, digest and summary |
| Modify `service/datahorizon/xlsx_stream.py`, `ods_stream.py` | Streaming readers hand records to the sink |
| Modify `service/datahorizon/interchange.py`, `ods.py` | Bounded previews return records |
| Modify `service/datahorizon/engine.py` | `workbook_import` and `edit` create records |
| Modify `service/datahorizon/service.py`, `charts_api.py`, `client.py`, `cli.py` | OpenAPI, SDK, CLI |
| Create `desktop/chartlayer.h`, `desktop/chartlayer.cpp` | Overlay widgets, layout, render queue |
| Modify `desktop/mainwindow.cpp`, `mainwindow.h`, `CMakeLists.txt` | Wire the layer; pass charts on the one-sheet import; review count; Qt SVG |
| Create `tests/chart_fixtures.py`, `tests/test_import_charts.py`, `tests/test_xlsx_charts.py`, `tests/test_ods_charts.py`, `tests/test_import_chart_paths.py` | Service tests |
| Modify `tests/test_desktop.cpp` | Desktop tests |

## Shared data shapes

**Reference** (`Ref`): `(sheet_name: str, row1: int, col1: int, row2: int, col2: int)`, 1-based, or the sentinel `import_charts.UNUSABLE` for a reference that is present but cannot be used (multi-area, defined name, external workbook, literal data, structured reference). `None` means absent.

**Draft** (reader output, a `dict`):

```python
{"sheet": 0,                      # 0-based position among the imported worksheets
 "name": "Chart 1", "order": 0,   # frame name; drawing order on the sheet
 "plots": [{"kind": "bar",        # bar, line, area, pie, doughnut, scatter, or any other name as found
            "direction": "col",   # "col" or "bar" for kind bar; else None
            "grouping": "clustered",  # clustered, standard, stacked, percentStacked or None
            "three_d": False, "secondary": False}],
 "title": {"text": "Sales by Division", "from_cell": False},   # text None when there is no title
 "axis_titles": {"category": {"text": None, "from_cell": False}, "value": {"text": None, "from_cell": False}},
 "value_min": None, "value_max": None,
 "legend": None,                  # right, bottom, top, left, or None when absent
 "series": [{"name": {"text": "Sales"} | {"ref": Ref} | None, "values": Ref, "categories": Ref | None, "x": Ref | None}],
 "styled": ["colours", "data labels"],   # dropped styling found, in the fixed vocabulary of Task 1
 "anchor": {"row": 1, "col": 3}, "offset": {"x": 66, "y": 16},
 "size": {"width": 480, "height": 300},
 "approximate_size": False,       # extent computed without the file's real cell sizes
 "absolute_anchor": False}        # placed by absolute position, snapped to the containing cell
```

**Record** (JSON; what the stage, previews, `workbook_import.charts` and `edit.charts` carry):

```python
{"sheet": 0, "name": "Chart 1", "anchor": {...}, "offset": {...}, "size": {...},
 "spec": {...}}   # chart_create input form, except each range source is {"kind": "range", "sheet": <position int>, "ref": "B2:B8"}
```

**Report entries.** Category `chart.skipped` (severity as the existing `workbook.feature` omissions use) with `detail` one of `type`, `combination`, `secondary_axis`, `pie_series`, `source`, `limit`, `workbook_limit`, `unreadable`, `grouped`, `off_grid`. Chart sheets keep their existing `workbook.feature` `chart_sheets` entry. Category `chart.converted` (the existing severity for a transformed feature) with `detail` one of `styling: <property>` (one entry per dropped property, `<property>` from `STYLED`), `grouping` (a stacked grouping on a type that takes none), `series_name` (a series name that could not be bound, replaced by `Series N`), `percent_stacked`, `three_d`, `doughnut`, `legend_position`, `title_from_cell`, `title_cut`, `categories_differ`, `size_approximate`, `anchor_absolute`, `name`. Every entry passes `sheet=<sheet name>` and a sample naming the chart. Read `conversion_report.py` for the severity names and the sample form.

---

### Task 1: Conversion rules and record handling

**Files:**
- Create: `service/datahorizon/import_charts.py`
- Test: `tests/test_import_charts.py`

**Interfaces:**
- Consumes: `charts.normalise_spec`, `charts._create` path via `charts.mutate` internals, `charts.LIMIT_CHARTS`, `conversion_report.ConversionReport.add`, `engine.EngineError`, `engine.require`.
- Produces:
  - `UNUSABLE` (module sentinel)
  - `STYLED = ("colours", "data labels", "gap width", "markers", "smoothing", "gridlines", "number formats", "fonts")`
  - `parse_reference(text: str, dialect: str) -> Ref | UNUSABLE` with `dialect` `"xlsx"` or `"ods"`
  - `convert(draft: dict, sheet_names: list[str], report: ConversionReport) -> dict | None` (a record, or `None` when skipped)
  - `check_records(records: object, sheet_count: int) -> list[dict]` (raises `EngineError("INVALID_ARGUMENT", ...)`)
  - `for_sheet(records: list[dict], position: int, row: int, col: int, sheet_name: str, report: ConversionReport) -> list[dict]` (records of one worksheet, rebased to position 0 and shifted by `row-1`, `col-1`)
  - `summary(records: list[dict], sheet_names: list[str]) -> list[dict]` giving `[{"name", "sheet", "type"}]`
  - `create(book, records: list[dict], sheet_ids: list[str]) -> dict` giving `{"created": int, "left_out": [str]}`

- [ ] **Step 1: Write the failing tests** in `tests/test_import_charts.py`. Build drafts with a local `draft(**overrides)` helper based on the shape above (one clustered column series, `values` `("Data", 2, 2, 8, 2)`, `categories` `("Data", 2, 1, 8, 1)`, sheet names `["Data", "Other"]`). Tests and assertions:

```python
def test_parse_reference_xlsx(self):
    self.assertEqual(parse_reference("'Column Chart'!$B$2:$B$8", "xlsx"), ("Column Chart", 2, 2, 8, 2))
    self.assertEqual(parse_reference("Data!$A$1", "xlsx"), ("Data", 1, 1, 1, 1))
    self.assertEqual(parse_reference("'Bob''s Q1'!$A$2:$A$8", "xlsx"), ("Bob's Q1", 2, 1, 8, 1))
    self.assertEqual(parse_reference("Données!$A$2:$A$8", "xlsx"), ("Données", 2, 1, 8, 1))
    self.assertEqual(parse_reference("Data!$B:$B", "xlsx"), ("Data", 1, 2, 1048576, 2))
    for bad in ("(Data!$A$1:$A$3,Data!$C$1:$C$3)", "MyName", "[1]Data!$A$1", "Table1[Sales]", "{1,2,3}", ""):
        self.assertIs(parse_reference(bad, "xlsx"), UNUSABLE)

def test_parse_reference_ods(self):
    self.assertEqual(parse_reference("$'Column Chart'.$B$2:.$B$8", "ods"), ("Column Chart", 2, 2, 8, 2))
    self.assertEqual(parse_reference("Data.A1", "ods"), ("Data", 1, 1, 1, 1))
    self.assertEqual(parse_reference("$'Bob''s Q1'.$A$2:$'Bob''s Q1'.$A$8", "ods"), ("Bob's Q1", 2, 1, 8, 1))
    self.assertIs(parse_reference("Data.A1:Data.A3 Data.C1:Data.C3", "ods"), UNUSABLE)
    self.assertIs(parse_reference("$Data.$A$1:$Other.$A$3", "ods"), UNUSABLE)
```

  Conversion tests, each asserting the record's `spec` fields and the report's `(category, detail)` pairs:
  - `test_clustered_column`: type `column`, grouping `clustered`, `categories` `{"source": {"kind": "range", "sheet": 0, "ref": "A2:A8"}}`, series values ref `B2:B8`, legend `none` when the draft has no legend, no report entries.
  - `test_types`: `(bar, col)`→`column`; `(bar, bar)`→`bar`; `line`, `area`, `pie`, `scatter` map to themselves; grouping is `None` for line, pie and scatter; scatter has `categories` `None` and each series `x` set from the draft's `x`.
  - `test_grouping`: `standard`→`clustered` silently; `stacked`→`stacked`; `percentStacked`→`stacked` with `chart.converted/percent_stacked`.
  - `test_three_d_and_doughnut`: `three_d` gives the 2D type with `chart.converted/three_d`; a one-series doughnut gives `pie` with `chart.converted/doughnut`.
  - `test_legend`: `right`, `bottom` kept; `top` and `left` give `right` with `chart.converted/legend_position`.
  - `test_titles`: a 300-character title is cut to 255 with `title_cut`; `from_cell` gives the text with `title_from_cell`; axis titles and `value_min`/`value_max` are carried; `value_min > value_max` drops both silently to `None`.
  - `test_series_name`: `{"text": "Sales"}` gives `{"text": "Sales"}`; `{"ref": ("Data", 1, 2, 1, 2)}` gives a one-cell source `B1`; a multi-cell or `UNUSABLE` name ref, or `None`, gives `{"text": "Series N"}` (1-based) with no skip.
  - `test_categories_differ`: second series with other categories keeps the first, with `categories_differ`.
  - `test_styled`: `styled=["colours", "data labels"]` gives exactly one `chart.converted/styling` entry whose sample lists both.
  - `test_name`: a name over 64 characters or with unprintable characters is cut or replaced by `Chart` with `chart.converted/name`; an empty name becomes `Chart`.
  - `test_placement_clamped`: size 10×10 becomes 64×48, 9000×9000 becomes 4096×4096, offset 5000 becomes 4096; `approximate_size` gives `size_approximate`; `absolute_anchor` gives `anchor_absolute`.
  - `test_skips`: each returns `None` with one `chart.skipped` entry of the detail given: kind `radar` (`type`); two plots (`combination`); `secondary` true (`secondary_axis`); pie with two series (`pie_series`); values `UNUSABLE`, values on sheet `"Missing"`, values a 3×3 block, values `None`, scatter without `x` (`source`); 65 series, values of 10,001 cells, pie values of 1,001 cells, eleven series of 10,000 cells each (`limit`); `("Data", 1, 2, 1048576, 2)` whole column (`limit`).
  - `test_sheet_name_match_ignores_case_as_import_does`: `("data", ...)` binds to `Data` (use `interchange.fold_name`).
  - `test_for_sheet`: of records on positions 0 and 1, `for_sheet(records, 1, 3, 2, "Other", report)` returns only position 1's, with `sheet` 0, every ref and the anchor moved down 2 rows and right 1 column; a record with any source on another position is dropped with `chart.skipped/source`; a record whose shifted anchor or any shifted ref passes row 1,048,576 or column 16,384 is dropped with `chart.skipped/off_grid`.
  - `test_check_records`: accepts `convert` output unchanged; refuses a non-list, more than 64 records, an unknown key, a `sheet` outside `range(sheet_count)`, a source `sheet` that is not an in-range `int`, each with `INVALID_ARGUMENT`.
  - `test_create`: on a `Workbook` with two sheets and data, `create(book, records, [id0, id1])` inside an operation returns `{"created": 2, "left_out": []}` and `book.charts`/catalog read shows both charts with stored `sheet_id`s and ascending `z` in record order; duplicate names on one sheet become `Chart 1`, `Chart 1 (2)`; with 63 charts already stored, two records give `{"created": 1, "left_out": ["<second name>"]}`.

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest tests.test_import_charts -v`. Expected: import error, `datahorizon.import_charts` not found.

- [ ] **Step 3: Implement the module.** Decisions the tests do not fix:
  - `convert` checks skips in this order: `type`, `combination`, `secondary_axis`, `pie_series`, `source`, `limit`; the first that applies is the one reported.
  - `create` builds each chart by the same code path as `chart_create` (refactor `charts._create` into a helper taking a resolved `sheet_id` and request if needed, so name defaulting, `z`, `_checked` and `store` are shared). It replaces each position with `sheet_ids[position]` before `normalise_spec`. It must be called inside an open engine operation.
  - A record that `normalise_spec` refuses at create time is a bug in `convert`, not a user error: let the `EngineError` propagate.

- [ ] **Step 4: Run** the same command. Expected: all pass. Also run `tests.test_charts` and `tests.test_chart_clients` to confirm the `_create` refactor changed nothing.

- [ ] **Step 5: Commit** `Add chart import conversion rules and record handling`.

---

### Task 2: XLSX chart reader

**Files:**
- Create: `service/datahorizon/xlsx_charts.py`, `tests/chart_fixtures.py`
- Test: `tests/test_xlsx_charts.py`

**Interfaces:**
- Consumes: `import_charts.parse_reference`, `import_charts.UNUSABLE`, `import_charts.STYLED`; `xlsx_stream._read_whole(archive, name, budget)`, `xlsx_features.relationships_path(part)`.
- Produces:
  - `xlsx_charts.read_sheet_drafts(archive, sheet_part: str, position: int, sheet_name: str, budget, report: ConversionReport, widths=None) -> list[dict]` (drafts in drawing order; unreadable and grouped charts are reported here and omitted). `widths` is an optional `(column_width_px(col) , row_height_px(row))` pair of callables; without it the defaults are 64 and 20 pixels.
  - `chart_fixtures.xlsx_with_charts(sheets: dict, charts: list[dict], **builder_options) -> bytes`, where each chart dict is `{"sheet": name, "xml": chart_part_xml, "anchor": drawing_anchor_xml | None, "name": "Chart 1"}`, built on `xlsx_builder.workbook_bytes(extra_parts=..., sheet_after=...)`.
  - `chart_fixtures.chart_xml(kind="bar", direction="col", grouping="clustered", series=[{"name_ref", "name_text", "cat", "val", "x"}], title=None, legend=None, extras="") -> str`
  - `chart_fixtures.sample_like() -> bytes`: two worksheets `Column Chart` and `Stacked Column`, each A1:B8 (or A1:E8) of data, a clustered and a stacked column chart with a title, data labels, theme colours, `gapWidth`, and a `twoCellAnchor` from C1 (+632114, +154565 EMU) to J16, mirroring the sample.

- [ ] **Step 1: Write the failing tests.**
  - `test_sample_like`: two drafts, one per sheet; first has `plots == [{"kind": "bar", "direction": "col", "grouping": "clustered", "three_d": False, "secondary": False}]`, title text `Sales by Division`, one series with `name == {"ref": ("Column Chart", 1, 2, 1, 2)}`, values `("Column Chart", 2, 2, 8, 2)`, categories `("Column Chart", 2, 1, 8, 1)`, `anchor == {"row": 1, "col": 3}`, `offset == {"x": 66, "y": 16}` (EMU/9525, rounded), `"colours"` and `"data labels"` and `"gap width"` in `styled`; second has grouping `stacked`.
  - `test_size_from_two_cell_anchor`: with default widths, C1+0 to J16+0 gives `{"width": 448, "height": 300}`; with `widths` returning 100 and 30 it gives `{"width": 700, "height": 450}`; `approximate_size` is `False` in both.
  - `test_one_cell_and_absolute_anchor` (Review Focus 1): `oneCellAnchor` with `ext` 4572000×2743200 EMU gives size 480×288; `absoluteAnchor` at (1219200, 381000) EMU gives the cell containing that point with default sizes (`{"row": 3, "col": 3}`, offset `{"x": 0, "y": 0}`) with `absolute_anchor` true.
  - `test_minimal_chart` (Review Focus 1): no `c:title`, no `c:cat`, no `c:tx`, no `c:legend` gives title text `None`, categories `None`, name `None`, legend `None`.
  - `test_kinds`: `barChart` with `barDir=bar`; `lineChart`, `areaChart`, `pieChart`, `doughnutChart`, `scatterChart` (with `c:xVal`/`c:yVal`); `bar3DChart` sets `three_d`; `radarChart` gives kind `radar`; two chart elements in one `plotArea` give two plots; a second value axis crossing at max, or series bound to a second axis pair, sets `secondary`.
  - `test_title_from_cell_and_axis_titles`: `c:tx/c:strRef` title gives the cached text with `from_cell` true; `c:valAx/c:scaling/c:min` and `max` are read as floats.
  - `test_legend_positions`: `legendPos` `r`, `b`, `t`, `l`, `tr` give `right`, `bottom`, `top`, `left`, `right`.
  - `test_literal_and_multi_area_sources`: `c:numLit` gives values `UNUSABLE`; a `c:f` of `(A,B)` gives `UNUSABLE`.
  - `test_unreadable_chart_is_reported_not_raised`: a chart part of `<c:chartSpace` (truncated), a drawing relationship to a missing part, and a relationship target of `../../../outside.xml` each give no draft, one `chart.skipped/unreadable` entry, and no exception.
  - `test_chart_part_over_budget`: with a `ByteBudget` smaller than the chart part, the same outcome as unreadable. The budget's own exhaustion for later parts must behave as before, so charge only what was read.
  - `test_group_shape_and_extended_chart`: a chart inside `xdr:grpSp` gives `chart.skipped/grouped`; a `cx:chart` graphic gives a draft of kind `extended` (skipped by `convert` as `type`).
  - `test_mc_alternate_content`: a `graphicFrame` wrapped in `mc:AlternateContent` is read from `mc:Choice` when its chart is `c:`, else from `mc:Fallback`.

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest tests.test_xlsx_charts -v`. Expected: import error.

- [ ] **Step 3: Implement.** Path: worksheet part → its relationships → the `drawing` relationship → drawing part → each anchor's `graphicFrame` → `r:id` → chart part. Both Transitional and Strict namespaces must work (see `interchange.TRANSITIONAL` / `STRICT`). Series are ordered by `c:order`. `styled` detection: `c:spPr` with a fill on a series or the chart → `colours`; `c:dLbls` with any `show*` true → `data labels`; `c:gapWidth` or `c:overlap` → `gap width`; `c:marker` other than `none` → `markers`; `c:smooth val=1` → `smoothing`; `c:majorGridlines`/`minorGridlines` → `gridlines`; `c:numFmt` not linked to source → `number formats`; `c:txPr` → `fonts`.

- [ ] **Step 4: Run** the tests. Expected: all pass.

- [ ] **Step 5: Commit** `Read XLSX chart parts into drafts`.

---

### Task 3: ODS chart reader

**Files:**
- Create: `service/datahorizon/ods_charts.py`
- Modify: `tests/chart_fixtures.py`
- Test: `tests/test_ods_charts.py`

**Interfaces:**
- Consumes: Task 1's `parse_reference(text, "ods")`; `ods_features` namespace constants (`DRAW`, and the chart, svg, table, xlink namespaces, added there if missing).
- Produces:
  - `ods_charts.read_frame_draft(archive, frame: ET.Element, position: int, sheet_name: str, order: int, read_part, report: ConversionReport, cell: tuple[int, int] | None, widths=None) -> dict | None`. `read_part(archive, name)` returns the parsed root of a package part under the caller's budget. `cell` is the `(row, col)` of the cell holding the frame, or `None` for a frame in `table:shapes`. Returns `None` for a frame that is not a chart object (no report) or cannot be read (reported).
  - `chart_fixtures.ods_with_charts(sheets, charts, **builder_options) -> bytes` and `chart_fixtures.ods_chart_xml(...)` with the same arguments as the XLSX helpers, built on `ods_builder.workbook_bytes(content=..., manifest=..., extra_parts=...)`.

- [ ] **Step 1: Write the failing tests**, mirroring Task 2 so the two readers are held to the same drafts:
  - `test_sample_like_matches_xlsx`: `ods_with_charts` built from the same logical input as `sample_like` yields drafts equal to the XLSX drafts in `plots`, `title`, `series`, `legend` and `anchor`.
  - `test_class_and_style_mapping`: `chart:class` `chart:bar` with the plot-area style's `chart:vertical="true"` is direction `bar`, without it `col`; `chart:stacked="true"` → `stacked`; `chart:percentage="true"` → `percentStacked`; `chart:three-dimensional="true"` → `three_d`; `chart:line`, `chart:area`, `chart:circle` (pie), `chart:ring` (doughnut), `chart:scatter`; `chart:radar` gives kind `radar`.
  - `test_series_sources`: `chart:values-cell-range-address`, `chart:label-cell-address`, the `chart:categories` `table:cell-range-address` under the x axis (applies to every series), and for scatter the series' `chart:domain` as `x`.
  - `test_series_with_own_chart_class_is_combination`: a series whose `chart:class` differs from the chart's gives two plots.
  - `test_secondary_axis`: a series `chart:attached-axis` naming a second y axis sets `secondary`.
  - `test_placement`: a frame in `table:shapes` with `svg:x="2.54cm" svg:y="1.27cm" svg:width="12.7cm" svg:height="7.62cm"` gives the cell containing that point with default sizes and `absolute_anchor` true, size 480×288; a frame inside a cell at row 1, column 3 with `svg:x="0.5cm"` gives anchor `{"row": 1, "col": 3}`, offset x 19. Lengths in `cm`, `mm`, `in`, `pt` and `px` convert at 96 DPI.
  - `test_legend_and_titles`: `chart:legend-position` `end`, `bottom`, `top`, `start`; `chart:title/text:p`; axis `chart:title`; `chart:minimum`/`chart:maximum` from the y axis style.
  - `test_unreadable_object`: an object directory with no `content.xml`, truncated XML, and an `xlink:href` of `../other` each give `None` with one `chart.skipped/unreadable`.
  - `test_non_chart_object`: a `draw:object` whose content is a formula object gives `None` with no chart entry.

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest tests.test_ods_charts -v`. Expected: import error.

- [ ] **Step 3: Implement.** The object's sub-document is `<href without "./">/content.xml`. Styling detection for `styled` uses the automatic styles the series and chart name: `draw:fill-color` → `colours`; `chart:data-label-number` other than `none` → `data labels`; `chart:gap-width`/`chart:overlap` → `gap width`; `chart:symbol-type` other than `none` → `markers`; `chart:interpolation` other than `none` → `smoothing`; a `chart:grid` element → `gridlines`.

- [ ] **Step 4: Run** the tests. Expected: all pass.

- [ ] **Step 5: Commit** `Read ODS chart objects into drafts`.

---

### Task 4: Whole-workbook import carries charts (staged and inline)

**Files:**
- Modify: `service/datahorizon/import_stage.py`, `xlsx_stream.py` (`Sink`, `read_workbook`), `ods_stream.py` (`_read_sheets`), `interchange.py` (`_xlsx_preview`, `prepare_workbook`, `workbook_report`), `ods.py` (`_ods_preview`), `engine.py` (`plan_import`, `import_workbook`, `import_arguments`), `service.py` (OpenAPI for the review summary, `workbook_import` request, receipt), `client.py` (`import_prepared`), `cli.py` (`import_spreadsheet`)
- Test: `tests/test_import_chart_paths.py`; update existing tests that implement a fake sink or assert the stage version, digest or report scope lines

**Interfaces:**
- Consumes: Tasks 1 to 3.
- Produces:
  - `Sink.charts(records: list[dict]) -> None`, called once per import, after the last sheet, with every converted record.
  - `ImportStage.charts(records)`; stage schema gains `CREATE TABLE charts(position INTEGER PRIMARY KEY, payload TEXT NOT NULL)`; `import_stage.VERSION = 2`; `StageReader.chart_records() -> list[dict]`; `StageReader.digest()` also hashes `encoded(self.chart_records())` after the cells.
  - Review summary and bounded workbook preview gain `"charts": [{"name", "sheet", "type"}]` (from `import_charts.summary`); the bounded workbook preview also gains `"workbook_charts": [record, ...]`.
  - `workbook_import` accepts optional `charts` (records) with inline `sheets`; refused with `INVALID_ARGUMENT` together with `stage_id`.
  - `plan_import(...)` result gains `"charts": list[dict]`.
  - Receipt: `imported` gains `"charts": int`; top-level `"charts_left_out": [name, ...]` present only when non-empty.

- [ ] **Step 1: Write the failing tests** (use a real `Workbook` and the service's job path as `tests/test_xlsx_stream.py` and the existing staged-import tests do):
  - `test_staged_xlsx_sample`: preparing `sample_like()` gives a summary with `charts == [{"name": "Chart 1", "sheet": "Column Chart", "type": "column"}, {"name": "Chart 1", "sheet": "Stacked Column", "type": "column"}]`; applying with `replace_empty` gives `imported["charts"] == 2`; the catalog read lists two charts on the right sheets; a resolve job for the first returns values `[400586, 353095, 804585, 410434, 99699, 25539, 25156]` and categories starting `Sports`; the workbook's `format_version` is 8.
  - `test_staged_ods_sample`: the same through `ods_stream`.
  - `test_inline_workbook_import`: `xlsx_preview` and `ods_preview` at workbook scope return `workbook_charts`; `client.import_prepared(preview)` creates them.
  - `test_add_sheets_binds_to_new_sheets`: into a workbook that already has `Sheet1` with data and one chart, `add_sheets` creates the imported charts on the added sheets and leaves the existing chart untouched.
  - `test_workbook_limit_under_add_sheets`: with 63 charts present, importing the two-chart sample creates one and the receipt has `charts_left_out == ["Chart 1"]`.
  - `test_digest_covers_charts`: two stages with equal cells and different records have different digests; applying with a digest computed before a record was altered is `STALE_PLAN`.
  - `test_values_mode`: `mode="values"` imports the charts and they resolve to the same values.
  - `test_chart_on_skipped_chart_sheet_and_unsupported`: a workbook with a chart sheet and a radar chart imports its cells, creates no chart, and reports `chart.skipped` for the radar; the existing `workbook.feature` `charts` and `chart_sheets` entries are still present.
  - `test_malformed_chart_does_not_change_cells`: the digest of cells-only content (compare `stored_cells` and a `SELECT` over cells after apply) is equal with and without a truncated chart part.
  - `test_import_clears_undo_as_before`: after a staged import with charts, undo is unavailable.
  - `test_more_than_64_charts_in_one_file`: a workbook with 66 supported charts prepares with 64 entries in `charts` and two `chart.skipped/workbook_limit` entries, in file order.
  - `test_summary_without_charts`: a workbook with no charts has `charts == []` and its report lines are unchanged from before this task.
  - `test_openapi`: the document validates (run `tests.test_job_openapi` and the OpenAPI tests) and the `workbook_import` schema lists `charts`.

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest tests.test_import_chart_paths -v`. Expected: failures on missing `charts` keys.

- [ ] **Step 3: Implement.**
  - Streaming XLSX: in `read_workbook`'s sheet loop, after `_read_sheet`, call `read_sheet_drafts(archive, part, position, name, budget, sheet_report, widths)` and `convert` each draft against `names`; collect records; after the loop call `sink.charts(records)`. `widths` comes from the sheet's `SheetFeatures` column widths when it holds them.
  - Streaming ODS: `_read_sheets` already streams `draw:*` elements for features; hand each `draw:frame` that holds a `draw:object` to `read_frame_draft`, with `read_part` charging the stream's `ByteBudget`.
  - Bounded readers: `_xlsx_preview` and `_ods_preview` return `"_charts"` (records of that sheet, position = index in `sheet_names`) when `sheet_names` is given; `prepare_workbook` pops and concatenates them into `workbook_charts` and `charts`. Their limits (3 MiB package, 100,000 nodes per part) apply to chart parts unchanged.
  - Engine: in `import_workbook`, after sheets and cells are loaded and before returning, call `import_charts.create(self, plan["charts"], identities)`. In `plan_import`, staged charts come from `stage.chart_records()` read inside the same snapshot as the digest check; inline charts pass `import_charts.check_records(request.get("charts", []), len(sheets))`.
  - Both readers stop converting after 64 records and report each further supported chart as `chart.skipped/workbook_limit`. The destination's own count is checked only at apply (`left_out`).
  - A stage with `VERSION` 1 on disk is refused as stale (`STALE_PLAN`, "prepare the workbook again"); stages are throwaway.
  - CLI prints `charts` beside sheets and cells in its import summary, and any `charts_left_out`.

- [ ] **Step 4: Run** `PYTHONPATH=service python3 -m unittest discover -s tests -v`. Expected: all pass (about five minutes).

- [ ] **Step 5: Commit** `Import charts with whole-workbook XLSX and ODS imports`.

---

### Task 5: One-sheet import carries charts through `edit`

**Files:**
- Modify: `service/datahorizon/interchange.py` (`_xlsx_preview`), `ods.py` (`_ods_preview`), `engine.py` (`edit` branch near line 1294), `service.py` and `charts_api.py` (OpenAPI: `edit.charts`, preview `charts`, receipt), `client.py` (`import_prepared`), `cli.py`
- Test: `tests/test_import_chart_paths.py`

**Interfaces:**
- Consumes: `import_charts.for_sheet`, `check_records`, `create`.
- Produces:
  - A selection-scope preview gains `"charts": [record, ...]`: the selected worksheet's records, rebased to position 0 and shifted by the request's `row`, `col`.
  - `edit` accepts optional `charts` (records, every `sheet` 0). They are created on the edit's sheet after its cells are applied, in the same operation.
  - Receipt: `changed_charts` counts them; `charts_left_out` as in Task 4.

- [ ] **Step 1: Write the failing tests:**
  - `test_selection_preview_and_edit`: `xlsx_preview` of `sample_like()` with `sheet_index=2`, `row=1`, `col=1` returns one record; `client.import_prepared(preview, "Sheet1")` creates one chart on `Sheet1` that resolves to the stacked sheet's values.
  - `test_selection_offset_shifts_chart`: with `row=5`, `col=3` the chart's sources are `C6:C12` (categories) and so on, its anchor is `{"row": 5, "col": 5}`, and it resolves to the same values.
  - `test_selection_chart_on_other_sheet_is_skipped`: a chart on sheet 1 whose values are on sheet 2 gives no record and `chart.skipped/source`.
  - `test_selection_off_grid` (Review Focus 5): a chart anchored at column 16,380 imported at `col=10` gives no record and `chart.skipped/off_grid`, and the preview's `cells`, `can_import` and `stored_cells` equal those of the same file without the chart.
  - `test_edit_with_charts_is_one_undo_step`: undo removes the cells and the chart; redo restores both with the same `chart_id`.
  - `test_failing_change_handler_rolls_back_charts`: with a bound cell-change handler that raises (as `tests/test_binding_execution.py` sets one up), the edit is rejected and the catalog read is empty.
  - `test_edit_charts_validation`: `charts` with a record whose `sheet` is 1, or that is not a list, is `INVALID_ARGUMENT`; an `edit` without `charts` behaves exactly as before (run `tests.test_engine`).
  - `test_ods_selection`: the first test through `ods_preview`.

- [ ] **Step 2: Run** the module. Expected: failures on the missing `charts` key.

- [ ] **Step 3: Implement.** In the `edit` branch call `import_charts.create(self, check_records(request["charts"], 1), [sheet_id])` after `apply_edit`, so the chart changes join the operation's change set and history. The edit's label, events and structural classification are unchanged.

- [ ] **Step 4: Run** `PYTHONPATH=service python3 -m unittest tests.test_import_chart_paths tests.test_engine tests.test_charts tests.test_chart_structure -v`. Expected: all pass.

- [ ] **Step 5: Commit** `Carry charts on one-sheet spreadsheet imports`.

---

### Task 6: Qt SVG as a desktop dependency

**Files:**
- Modify: `CMakeLists.txt`, `LICENSES.md`, `docs/design/toolchain-packaging-baseline.md`, packaging scripts under `packaging/` and `cmake/` that list Qt modules or deploy plugins

- [ ] **Step 1:** Change `find_package(Qt6 6.8 REQUIRED COMPONENTS Widgets Network Test)` to include `Svg`; link `Qt6::Svg` into `datahorizon_desktop`; make `chart_svg_probe` unconditional and drop the "experiment only" comment.
- [ ] **Step 2:** Add Qt SVG to every place the packaging enumerates Qt modules, and record it in `LICENSES.md` and the toolchain baseline as a linked module (it is listed today as deployed plugins only).
- [ ] **Step 3: Run** `cmake -S . -B build && cmake --build build && ctest --test-dir build -R 'chart_svg_probe_smoke|package' --output-on-failure`. Expected: configure finds Qt SVG, build succeeds, the probe passes.
- [ ] **Step 4: Commit** `Link Qt SVG into the desktop`.

---

### Task 7: Chart overlays in the desktop

**Files:**
- Create: `desktop/chartlayer.h`, `desktop/chartlayer.cpp`
- Modify: `CMakeLists.txt` (sources), `desktop/mainwindow.h`, `desktop/mainwindow.cpp` (construct after `m_frozenTop` near line 852; bind in `acceptInfo`, `changeSheet`, and wherever `m_model->bind` or the projected state changes)
- Test: `tests/test_desktop.cpp`

**Interfaces:**
- Consumes: `RestClient::request`; `POST {base}/charts` with `{expected_state, sheet}` (the chart catalog; each chart has `chart_id`, `name`, `anchor`, `offset`, `size`, `z`, `issue`, `over_limit` and a `spec` whose sources carry `broken`); `KeyboardGridView`, `FrozenPanes`, `SheetModel::projected()`.
- Produces:
  - `class ChartOverlay : public QWidget` with object name `chartOverlay`, dynamic property `chartId`, accessible name = chart name; `void setSvg(const QByteArray &)`, `void setPlaceholder(const QString &reason)`, `bool hasImage() const`, `QString placeholder() const`. It sets `Qt::WA_TransparentForMouseEvents` and `Qt::NoFocus`.
  - `class ChartLayer : public QObject` with `ChartLayer(RestClient *client, QTableView *body, FrozenPanes *frozen, QWidget *gridArea, QObject *parent)`; `void bind(const QString &workbookBase, const QString &sheet, const QString &state, bool projected)`; `void relayout()`; `QList<ChartOverlay *> overlays() const`; signal `void listed(int count)`.
  - Task 8 adds rendering to these classes.

- [ ] **Step 1: Write the failing desktop tests** (follow the existing tests that spawn the service on a fixture workbook; create charts through the service's `chart_create` operation before the window refreshes):
  - `chartOverlayAppearsAtItsAnchor`: a chart with anchor C2, offset (10, 5), size 320×200 gives one `chartOverlay` whose top-left, mapped to the grid viewport, equals the top-left of cell C2 plus (10, 5), and whose size is 320×200.
  - `chartOverlayFollowsScrollAndResize`: after scrolling the grid down three rows the overlay has moved up by those rows' heights; after widening column A by 40 pixels it has moved right by 40.
  - `chartOverlaysStackByZ`: of two overlapping charts, the higher `z` is above (`QWidget::stackUnder` order).
  - `chartOverlayDoesNotInterceptClicks`: `QTest::mouseClick` on the viewport at a point under the overlay makes that cell current.
  - `chartsOnOtherSheetsAreNotShown`: switching sheets replaces the overlays with that sheet's.
  - `chartAnchoredInFrozenRowStaysVisible`: with the top row frozen and a chart anchored at C1, the overlay is visible and does not move when the body scrolls vertically; a chart anchored at C5 moves and is clipped at the frozen row's bottom edge (its visible region's top is not above it).
  - `chartsHideInProjectedView`: with a filtered list view active no overlay is visible; clearing it shows them again.
  - `chartOverlayNeverCoversHeaders`: scrolled so the chart's top is above the viewport, the overlay's visible region does not intersect the horizontal header.
  - `brokenChartShowsPlaceholder`: a chart whose source sheet was deleted shows `placeholder()` containing the chart's name and a reason mentioning the source; an overlay never has neither an image nor a placeholder once listed (a chart awaiting its first image shows a placeholder with the name and "Loading").

- [ ] **Step 2: Run** the desktop tests. Expected: compile failure, `chartlayer.h` missing.

- [ ] **Step 3: Implement.**
  - Overlays are children of `gridArea` (the widget that holds the body and the frozen panes), raised above the views, so a chart can sit over any pane. Each overlay's geometry is the chart rectangle in `gridArea` coordinates; its mask (`setMask`) is the intersection with the pane region the chart belongs to: the frozen corner, top or left pane's viewport when the anchor row and/or column is frozen, otherwise the body viewport.
  - The anchor's pixel position comes from the view that shows the anchor cell (`columnViewportPosition`, `rowViewportPosition`, mapped to `gridArea`). When the anchor row or column is outside the model's loaded window or hidden, walk to the nearest following visible one; read `sheetmodel.h` and `frozenpanes.h` for how rows map.
  - `relayout()` runs on: both scroll bars' `valueChanged`, both headers' `sectionResized` and `geometriesChanged`, the model's `modelReset`/`layoutChanged`/rows-columns inserted or removed, `gridArea` resize (event filter), and after each `bind`.
  - `bind` with the same workbook, sheet and state does nothing. A catalog response whose `state` or sheet no longer matches the latest `bind` is dropped (compare `RestClient::generation()` and the bound state, as `mainwindow.cpp` does for names at line 326).
  - The grid has no zoom today; if one exists when this is built, scale the chart rectangle by it.
  - Placeholder reasons, in priority order: `issue` (`"This chart cannot be read (INVALID_SPEC)"` form), `over_limit`, any source `broken` (`"A source of this chart no longer exists"`), then render errors (Task 8). The placeholder paints a 1-pixel frame, the name and the reason on the palette's base colour.

- [ ] **Step 4: Run** the desktop tests. Expected: all pass, existing tests included.

- [ ] **Step 5: Commit** `Show chart overlays over the grid`.

---

### Task 8: Rendering, staleness and the render queue

**Files:**
- Modify: `desktop/chartlayer.h`, `desktop/chartlayer.cpp`
- Test: `tests/test_desktop.cpp`

**Interfaces:**
- Consumes: `POST {base}/charts/jobs` with `{job_id, expected_state, chart_id, kind: "render", width, height}`; `GET {base}/charts/jobs/{job}`; `POST .../cancel`; `POST .../release`. Job statuses and the result's SVG field are as `client.py:_chart_result` and `tests/test_chart_jobs.py` show.
- Produces: `ChartLayer` constants `MaxConcurrentRenders = 2`, `RelayoutRenderDelayMs = 150`, `PollIntervalMs = 50`; signal `void rendered(const QString &chartId)`.

- [ ] **Step 1: Write the failing tests:**
  - `chartRendersTheServiceSvg`: after `rendered`, `hasImage()` is true and the overlay's grabbed pixmap is not uniformly the base colour.
  - `chartRerendersAfterAnEdit`: editing a source cell through the grid produces a second `rendered` for the chart, and between the edit and that signal the overlay still has its image (never blank).
  - `onlyVisibleChartsRender`: with one chart in view and one anchored at row 500, only the first renders; scrolling to row 500 renders the second.
  - `renderQueueIsBounded`: with six visible charts, a counting proxy (as `JobLifecycleServer` counts jobs) never sees more than two running chart jobs, and every job it saw started is released.
  - `sheetSwitchDiscardsLateRenders` (Review Focus 4): with a proxy delaying job results, switching sheets before they arrive leaves no overlay from the first sheet, cancels and releases those jobs, and no `rendered` is emitted for them. The same when another workbook is opened.
  - `renderFailureShowsPlaceholder`: a chart over `LIMIT_RENDER_POINTS` shows a placeholder containing that code; a job answered `stale` is re-queued once against the current state, not shown as an error.

- [ ] **Step 2: Run** the desktop tests. Expected: the new tests fail (no image).

- [ ] **Step 3: Implement.** A chart needs a render when it intersects its pane's visible region and has no image for the current `(state, width, height)`. Needed renders queue in `z` order; at most `MaxConcurrentRenders` run; a relayout schedules the queue check after `RelayoutRenderDelayMs` (restart the timer on each relayout). Every started job is released when terminal, and cancelled then released when its chart leaves view, the sheet changes or the layer is destroyed. Results are accepted only if the job's chart, state and sheet match the current binding. `ChartOverlay` paints with a `QSvgRenderer` scaled to the widget rectangle, which is what keeps the old image, scaled, while a new size renders.

- [ ] **Step 4: Run** the desktop tests. Expected: all pass.

- [ ] **Step 5: Commit** `Render charts in the desktop and follow workbook changes`.

---

### Task 9: Desktop import carries charts

**Files:**
- Modify: `desktop/mainwindow.cpp` (`importSpreadsheetBytes` near line 3379 for the one-sheet path; the review dialog near line 3569; the apply receipt handling near line 3660)
- Test: `tests/test_desktop.cpp`

**Interfaces:**
- Consumes: preview `charts` (Task 5), summary `charts` and receipt `imported.charts`, `charts_left_out` (Task 4).

- [ ] **Step 1: Write the failing tests.** The C++ tests need a file on disk, so commit a fixture `tests/fixtures/charts-two-sheets.xlsx` written by a new `tests/fixtures/generate_chart_workbook.py` that calls `chart_fixtures.sample_like()` with a fixed `timestamp`, and add a service test asserting the committed file equals the generator's output. Desktop tests:
  - `oneSheetImportShowsItsChart`: importing the fixture with scope "One sheet at the selected cell", sheet 1, at A1 results in one `chartOverlay` on the current sheet.
  - `allSheetsImportShowsCharts`: with scope "All sheets" the review dialog's summary label (`importReviewSummary`) contains `2 charts`, and after Apply each of the two sheets shows one overlay.
  - `importReviewCountsChartsInTheSingular`: a one-chart workbook says `1 chart`; a workbook without charts does not mention charts.
  - `chartsLeftOutAreReported`: a receipt with `charts_left_out` puts a message naming them in the status issue line.

- [ ] **Step 2: Run** the desktop tests. Expected: the new tests fail.

- [ ] **Step 3: Implement.** The one-sheet path adds the preview's `charts` array to the `edit` operation it already submits, only when non-empty. The review count uses the same singular and plural helper as sheets and cells.

- [ ] **Step 4: Run** the desktop tests and `PYTHONPATH=service python3 -m unittest tests.test_import_chart_paths -v`. Expected: all pass.

- [ ] **Step 5: Commit** `Import charts from the desktop import dialog`.

---

### Task 10: Documents, sample-file check and evidence

**Files:**
- Modify: `docs/design/charts-design.md`, `compatibility-v0.md`, `interfaces-and-interoperability.md`, `implementation-status.md`, `README.md` (design index), `charts-import-view-design.md` (status line)
- Create: `docs/design/evidence/charts-import-view-verification.txt`

- [ ] **Step 1:** `charts-design.md`: bump the draft; in sections 2, 11 and 12 record that chart import and a view-only desktop slice were pulled ahead of stage 3 authoring and stage 4 export, linking this plan and the design; stage 3's wireframe session is still owed.
- [ ] **Step 2:** `compatibility-v0.md`: move `charts` from the omitted `workbook.feature` list to a new "Charts" section holding the keep, convert and skip tables of design section 4 and the `chart.skipped` / `chart.converted` details; state that export still omits charts. `interfaces-and-interoperability.md`: the three import paths and the `edit.charts` field.
- [ ] **Step 3:** Run the full suites: `ctest --test-dir build --output-on-failure`. Expected: all pass.
- [ ] **Step 4:** In the built application, import `~/Downloads/Column Chart_Complete.xlsx` with each scope. Expected: "One sheet" shows the clustered chart over the current sheet; "All sheets" shows one chart on each sheet; the conversion report lists dropped styling for both. Then save LibreOffice's XLSX and ODS exports of one small chart document, import both, and compare the resolved series. Record commands, results and screenshots' paths in the evidence file. The sample and the LibreOffice files are not committed.
- [ ] **Step 5:** Add the dated entry to `implementation-status.md` (built, measured, open: authoring, export, Windows and Linux). Set the design's status to "as built".
- [ ] **Step 6: Commit** `Document chart import and the view-only chart layer`.
