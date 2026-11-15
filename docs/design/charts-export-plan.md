# Charts Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** XLSX and ODS exports write every native chart whose data comes from cell ranges as a live chart, and report every chart they leave out by name and reason.

**Architecture:** A shared module, `export_charts.py`, mirrors `import_charts.py` in the other direction: it reads the stored charts in the export's snapshot, decides which can be written, and turns each into a format-neutral *export draft*. One writer per format (`xlsx_charts.py`, `ods_charts.py`, beside the existing readers) turns a draft into that format's chart part and packages the parts. The three export routes (inline whole workbook, inline selection, streamed whole workbook) call the same shared step, so the leave-out rules and report entries exist once.

**Tech Stack:** Python 3.11 standard library (service, `unittest`); `openpyxl` and `odfpy` in tests only, as the existing interchange tests use them.

**Spec:** [charts-authoring-export-design.md](charts-authoring-export-design.md), section 6 and the Export part of section 7. Read it first. This is plan 1 of 2; desktop authoring (sections 4 and 5) has its own plan.

## Global Constraints

- Work on a branch `claude/charts-export` cut from `main`. Every source file starts with `# SPDX-License-Identifier: GPL-3.0-only`.
- Service: Python standard library only. No new third-party packages.
- Specification version 1 and the native format are unchanged. No engine limit is added or changed.
- An export never fails, and never loses a cell, because of a chart. A chart that cannot be written is reported and the export continues.
- The whole chart is left out, never part of it. Every chart left out has a report entry naming the chart, its sheet and the reason.
- An exported chart never points at different cells than the native chart did.
- The export runs no calculation and no chart resolve or render job for charts. Chart parts carry no cached values in this plan.
- Report entries use `ConversionReport.add("chart.skipped", "omitted", <reason>, sheet=<sheet name>, cell=<chart name, first 32 characters>)`, the same category and form the import uses. Reasons, verbatim: `table_source`, `query_source`, `broken_source`, `source`, `unsupported`, `unwritable`.
- Units: 96 DPI. One pixel is 9,525 EMU in XLSX and 1/96 inch in ODS.
- Chart parts count against each route's existing expanded-size limit (16 MiB inline, `LARGE_EXPANDED` streamed).
- List, relational and CSV exports are unchanged and carry no charts.
- Service tests: `PYTHONPATH=service python3 -m unittest tests.<module> -v` from the repository root.

## Review Focus

Each line is pinned by a test in the task named.

1. **Sheet names that need quoting in references** (`Bob's Q1`, `Ventas 2026`, `数据`). Expected: the exported reference parses back to the same sheet and cells in both formats. Task 1.
2. **Text with XML-special or control characters** in a chart name, title, axis title or literal series name (`A & B <1>`, a tab, U+0001). Expected: the package is well-formed and opens; characters XML cannot hold are dropped from the written text, the chart is still written. Tasks 2 and 3.
3. **A chart on a sheet that has no cells**, sourced from another sheet. Expected: the empty sheet is written with its chart. Task 4.
4. **A selection export at the cell limit in a workbook holding many large charts.** Expected: the charts do not count against the 10,000-cell read's size check, so the export that worked before charts still works. Task 4.
5. **64 charts of 64 series each on the streamed route, and cancellation while charts are written.** Expected: within `LARGE_EXPANDED` the export completes; a cancel removes the partial file as it does during cells. Task 5.

## File Structure

| File | Responsibility |
| --- | --- |
| `service/datahorizon/export_charts.py` (new) | Collect stored charts from a snapshot; decide written or left out; build export drafts; format references; report |
| `service/datahorizon/xlsx_charts.py` (modify) | Add the DrawingML writer beside the reader: one chart part per draft, and the drawing, relationship and content-type parts |
| `service/datahorizon/ods_charts.py` (modify) | Add the OpenDocument writer beside the reader: one chart object per draft, the frames and the manifest entries |
| `service/datahorizon/engine.py` (modify) | Snapshots carry chart entries instead of chart name strings |
| `service/datahorizon/interchange.py`, `ods.py` (modify) | Inline exports write charts; `package_parts` take extra parts; report lines |
| `service/datahorizon/xlsx_stream.py`, `ods_stream.py`, `service.py` (modify) | Streamed exports write charts |
| `service/datahorizon/xlsx_features.py`, `ods_features.py` and the import paths that call them (modify) | Import report clean-up |
| `tests/test_export_charts.py`, `test_xlsx_chart_export.py`, `test_ods_chart_export.py`, `test_chart_export_paths.py`, `test_chart_round_trip.py` (new) | One test module per task group |

**The export draft** (a plain dict; every task uses exactly this shape):

```python
{"name": str, "sheet": str,                      # exported sheet name the chart sits on
 "anchor": {"row": int, "col": int},             # 1-based
 "offset": {"x": int, "y": int}, "size": {"width": int, "height": int},   # pixels
 "type": "column"|"bar"|"line"|"area"|"pie"|"scatter",
 "grouping": "clustered"|"stacked"|None,
 "title": str|None, "category_title": str|None, "value_title": str|None,
 "min": float|None, "max": float|None, "legend": "right"|"bottom"|"none",
 "categories": Ref|None,
 "series": [{"name": ("text", str)|("ref", Ref)|None, "values": Ref, "x": Ref|None}]}
```

`Ref` is the tuple `(sheet_name, row1, col1, row2, col2)` that `import_charts.parse_reference` returns.

---

### Task 1: Shared export step

**Files:**
- Create: `service/datahorizon/export_charts.py`
- Test: `tests/test_export_charts.py`

**Interfaces:**
- Consumes: `charts.objects(book)`, `charts.describe(db, sheet_id, chart_id, payload)`, `charts.is_broken`, `charts.source_kind`, `import_charts.parse_reference(text, dialect)`, `ConversionReport.add`.
- Produces:
  - `collect(db) -> list[dict]`: one entry per stored chart, ordered by sheet position, then `z`, then id: `{"sheet_id": str, "sheet": str, "chart": <charts.describe output>}`. Empty when the workbook format is below 8.
  - `drafts(entries, sheet_names, report, *, selection=None) -> list[dict]`: the export drafts of the charts that can be written, in entry order; every other chart gets one `chart.skipped` entry. `sheet_names` maps sheet id to exported sheet name (`dict[str, str]`). `selection`, for a selection export, is `{"sheet_id": str, "range": [r1, c1, r2, c2], "name": "Sheet1"}`.
  - `format_reference(ref, dialect) -> str`: the inverse of `import_charts.parse_reference`, taking the same `dialect` values; always absolute (`$A$2`).
  - `build(drafts, build_one, report) -> list[tuple[dict, object]]`: calls `build_one(draft)` for each draft; an exception other than `EngineError` reports that chart `unwritable` and drops it.
  - `lines(written: int, left_out: int) -> list[str]`: `[]` when both are zero; otherwise one line, exactly `"Charts: {written} written."` or `"Charts: {written} written, {left_out} left out; each is listed below with its reason."`

Rules for `drafts`, first match wins:

| Condition | Reason |
| --- | --- |
| `chart["issue"]` is not `None` | `unsupported` |
| any source is broken | `broken_source` |
| any source is a table column | `table_source` |
| any source is a saved-query column | `query_source` |
| a range source's sheet id is not in `sheet_names`; or a values, categories or x source spans more than one row and more than one column; or a name source is more than one cell | `source` |
| with `selection`: the chart is on another sheet (not reported; it is not part of the selection's sheet), or its anchor or any source lies outside `range` or on another sheet | `source` |

With `selection`, every `Ref` and the draft's `sheet` use `selection["name"]`; coordinates are unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_export_charts.py`. Build entries by hand (dicts in the shape `collect` returns; copy one described chart from `tests/test_charts.py` setup or build it with `charts.describe` on a temporary `Workbook`).

```python
def test_range_chart_becomes_a_draft(self):
    report = ConversionReport()
    result = export_charts.drafts([entry(column_chart())], {"s1": "Sales"}, report)
    self.assertEqual(result[0]["categories"], ("Sales", 2, 1, 13, 1))
    self.assertEqual(result[0]["series"][0]["values"], ("Sales", 2, 2, 13, 2))
    self.assertEqual(result[0]["series"][0]["name"], ("ref", ("Sales", 1, 2, 1, 2)))
    self.assertEqual((result[0]["type"], result[0]["grouping"], result[0]["legend"]), ("column", "clustered", "right"))
    self.assertEqual(report.entries(), [])

def test_each_reason(self):
    for chart, reason in ((unsupported_chart(), "unsupported"), (broken_chart(), "broken_source"),
                          (table_chart(), "table_source"), (query_chart(), "query_source"),
                          (chart_on_sheet("gone"), "source"), (two_dimensional_values(), "source")):
        report = ConversionReport()
        self.assertEqual(export_charts.drafts([entry(chart)], {"s1": "Sales"}, report), [])
        only = report.entries()[0]
        self.assertEqual((only["category"], only["severity"], only["detail"], only["sheet"]),
                         ("chart.skipped", "omitted", reason, "Sales"))

def test_broken_wins_over_table(self): ...            # a chart with both reports broken_source once
def test_selection_keeps_chart_inside(self): ...      # refs and sheet become "Sheet1", coordinates unchanged
def test_selection_reports_chart_reaching_outside(self): ...   # anchor outside, or a source outside: "source"
def test_selection_ignores_other_sheets(self): ...    # no draft, no report entry

def test_reference_round_trips(self):
    for name in ("Sales", "Bob's Q1", "Ventas 2026", "数据"):
        for dialect in DIALECTS:    # the values parse_reference accepts
            ref = (name, 2, 1, 13, 1)
            self.assertEqual(import_charts.parse_reference(export_charts.format_reference(ref, dialect), dialect), ref)

def test_build_reports_a_failing_chart_and_keeps_the_rest(self): ...   # build_one raises ValueError for one: "unwritable"
def test_build_lets_engine_errors_through(self): ...   # EngineError("CANCELLED", ...) propagates
def test_lines(self): ...                               # the three exact outputs above
```

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest tests.test_export_charts -v`. Expected: FAIL, `No module named 'datahorizon.export_charts'`.
- [ ] **Step 3: Implement** the five functions in `service/datahorizon/export_charts.py` with the signatures above. `collect` adds nothing to the described chart beyond the entry's `sheet`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git add service/datahorizon/export_charts.py tests/test_export_charts.py && git commit -m "Add the shared chart export step"`.

### Task 2: XLSX chart writer

**Files:**
- Modify: `service/datahorizon/xlsx_charts.py`
- Test: `tests/test_xlsx_chart_export.py`

**Interfaces:**
- Consumes: the export draft; `export_charts.format_reference`.
- Produces:
  - `chart_part(draft) -> ET.Element`: a `c:chartSpace`.
  - `package(built, names) -> tuple[list[tuple[str, ET.Element]], dict[int, str], list[tuple[str, str]]]`: `built` is `export_charts.build` output, `names` the exported sheet names in order. Returns the extra parts (`xl/charts/chartK.xml`, `xl/drawings/drawingN.xml`, `xl/drawings/_rels/drawingN.xml.rels`, `xl/worksheets/_rels/sheetN.xml.rels`), a map from 1-based sheet index to the relationship id its `<drawing r:id>` must carry, and content-type overrides as `(part name, content type)`.

Pinned by the design: a one-cell anchor (`xdr:oneCellAnchor`) with `from` at the anchor cell, offsets and extent in EMU; frame name is the chart name; frames in draft order (z order); title absent means no title element and `c:autoTitleDeleted` set; legend `none` means no `c:legend`; `min` and `max` go on the value axis scaling; a text series name is a literal, a ref series name a reference; no `c:numCache` or `c:strCache`.

- [ ] **Step 1: Write the failing tests.** Read the written parts back with the existing reader in this module and `import_charts.convert`, and with `openpyxl`.

```python
def test_each_type_reads_back(self):
    for draft in drafts_for_every_fixture():      # tests/fixtures/charts/*.json turned into drafts on sheet "Data"
        archive = packaged([draft], ["Data"])      # helper: a minimal XLSX with one sheet plus package() output
        record = read_back(archive)                # existing chart reader + import_charts.convert
        self.assertEqual(report_of(record), [])    # no chart.converted, no chart.skipped
        self.assertEqual(as_draft(record), draft)  # type, grouping, titles, min/max, legend, refs, name, anchor, offset, size

def test_openpyxl_opens_the_package(self):
    book = openpyxl.load_workbook(io.BytesIO(packaged([column_draft()], ["Data"])))
    self.assertEqual(len(book["Data"]._charts), 1)

def test_two_charts_keep_their_order(self): ...        # names read back in draft order
def test_charts_on_two_sheets(self): ...                # each sheet index maps to its own drawing
def test_no_cached_values(self): ...                    # no numCache/strCache element in any chart part
def test_special_characters(self):
    draft = column_draft(name="A & B <1>", title="x\ty\x01z")
    archive = packaged([draft], ["Data"])               # must parse
    self.assertEqual(read_back(archive)["spec"]["title"]["text"], "x\tyz")
```

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest tests.test_xlsx_chart_export -v`. Expected: FAIL, `chart_part` not defined.
- [ ] **Step 3: Implement** `chart_part` and `package` in `xlsx_charts.py`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "Write XLSX chart parts from export drafts"` (after `git add tests/test_xlsx_chart_export.py`).

### Task 3: ODS chart writer

**Files:**
- Modify: `service/datahorizon/ods_charts.py`
- Test: `tests/test_ods_chart_export.py`

**Interfaces:**
- Consumes: the export draft; `export_charts.format_reference`.
- Produces:
  - `chart_document(draft) -> ET.Element`: the `office:document-content` of one chart object.
  - `package(built) -> tuple[list[tuple[str, ET.Element]], list[tuple[str, str]], dict[str, list[tuple[int, int, ET.Element]]]]`: the object parts (`Object N/content.xml`), manifest entries as `(full path, media type)`, and for each exported sheet name its frames as `(anchor row, anchor col, draw:frame element)` in draft order.

Pinned by the design: the frame is anchored to its cell, so that import reads back the same anchor and offset and reports no `anchor_absolute`; size and offset in inches at 96 DPI; no local data table.

- [ ] **Step 1: Write the failing tests**, the same six as Task 2 with the ODS reader, `odfpy` (`odf.opendocument.load`) in place of `openpyxl`, and one more:

```python
def test_frame_reads_back_at_its_cell(self):
    record, report = read_back(packaged([column_draft(anchor={"row": 5, "col": 3}, offset={"x": 7, "y": 9})], ["Data"]))
    self.assertEqual((record["anchor"], record["offset"]), ({"row": 5, "col": 3}, {"x": 7, "y": 9}))
    self.assertNotIn("anchor_absolute", [e["detail"] for e in report.entries()])
```

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest tests.test_ods_chart_export -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `chart_document` and `package` in `ods_charts.py`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `git commit -am "Write ODS chart objects from export drafts"`.

### Task 4: Inline exports write charts

**Files:**
- Modify: `service/datahorizon/engine.py` (`native_catalog`, `read`, `read_workbook`), `service/datahorizon/interchange.py` (`xlsx_export`, `package_parts`, `native_only_report`), `service/datahorizon/ods.py` (`ods_export`, `package_parts`)
- Test: `tests/test_chart_export_paths.py`

**Interfaces:**
- Consumes: Tasks 1 to 3.
- Produces:
  - `native_catalog(charts=True)` returns `chart_entries` (the `export_charts.collect` list) and `chart_sheets` (`dict` of sheet id to name). The inline snapshots no longer carry `native_charts`; the streamed snapshot still does until Task 5.
  - `interchange.package_parts(names, extra=(), drawings=None, overrides=())` and `ods.package_parts(extra=(), manifest=())`: the existing parts plus the chart parts.
  - Both exports add `export_charts.lines(...)` to their fixed report lines where the "Charts are native-only" line was, and the `chart.skipped` entries to `report_entries`.

The chart entries must not count against the size check inside `Workbook.read` (Review Focus 4): attach them after the cells are sized, or leave them out of that sum.

- [ ] **Step 1: Write the failing tests** through `LocalService`, following the setup in `tests/test_import_chart_paths.py` and `tests/test_chart_clients.py`.

```python
def test_whole_workbook_export_writes_range_charts(self):   # both formats
    # workbook: Data sheet with A1:C13, one clustered column chart and one line chart on it
    result = client.export_spreadsheet(format=fmt, scope="workbook")
    self.assertIn("Charts: 2 written.", result["report"])
    self.assertEqual([c["name"] for c in imported_charts(result)], ["Chart 1", "Chart 2"])
    self.assertFalse(any("native-only" in line and "Charts" in line for line in result["report"]))

def test_left_out_charts_are_named(self):
    # adds a table-sourced, a query-sourced and a broken-source chart
    self.assertIn("Charts: 2 written, 3 left out; each is listed below with its reason.", result["report"])
    self.assertEqual(sorted((e["detail"], e["cell"]) for e in result["report_entries"] if e["category"] == "chart.skipped"),
                     [("broken_source", "Broken"), ("query_source", "By query"), ("table_source", "By table")])
    self.assertEqual(cells_of(result), cells_without_charts)      # the cells are the same as an export with no charts

def test_values_mode_still_writes_charts(self): ...
def test_chart_on_an_empty_sheet(self): ...                  # sheet "Summary" has no cells, its chart reads "Data"
def test_selection_inside(self): ...                          # A1:H20 holds anchor and sources: chart written, refs name Sheet1
def test_selection_cutting_a_source(self): ...                # A1:B13 excludes C: one chart.skipped "source"
def test_selection_at_the_cell_limit_with_large_charts(self): ...   # 10,000 cells + 64 charts of 64 series still exports
def test_workbook_without_charts_reports_nothing_about_charts(self): ...
def test_a_chart_writer_failure_does_not_fail_the_export(self):
    with patch("datahorizon.xlsx_charts.chart_part", side_effect=ValueError("x")):
        result = client.export_spreadsheet(format="xlsx", scope="workbook")
    self.assertEqual([e["detail"] for e in result["report_entries"] if e["category"] == "chart.skipped"], ["unwritable"] * 2)
```

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest tests.test_chart_export_paths -v`. Expected: FAIL (the report still says charts are native-only).
- [ ] **Step 3: Implement.** In each export: `entries → export_charts.drafts → export_charts.build(…, chart_part | chart_document, conversions) → package`; add the worksheet's `<drawing>` element (XLSX) or the frames (ODS); include the chart parts in the existing expanded-size check.
- [ ] **Step 4: Run** the new module and the existing `tests.test_interchange tests.test_workbook_interchange tests.test_list_export tests.test_relational_export`. Expected: PASS; update any existing assertion on the old "Charts are native-only" line for the inline routes.
- [ ] **Step 5: Commit** `git commit -am "Write charts in the inline XLSX and ODS exports"`.

### Task 5: Streamed exports write charts

**Files:**
- Modify: `service/datahorizon/xlsx_stream.py` (`write_workbook`, `_write_sheet`, `export_lines`), `service/datahorizon/ods_stream.py` (`write_workbook`, `_write_table`, `export_lines`), `service/datahorizon/service.py` (`_write_export`), `service/datahorizon/interchange.py` (`native_only_report`: remove the charts line), `service/datahorizon/engine.py` (remove `native_charts`)
- Test: `tests/test_chart_export_paths.py` (add), `tests/test_large_export.py` (update)

**Interfaces:**
- Consumes: `stream.native["chart_entries"]`, `stream.native["chart_sheets"]`; Tasks 1 to 3.
- Produces: `xlsx_stream.write_workbook(path, sheets, report, checkpoint, *, charts=())` and `ods_stream.write_workbook(path, sheets, report, checkpoint, *, scratch=None, charts=())`, where `charts` is the `export_charts.drafts` list; both return `"charts"` (the count written) in their summary. `export_lines` of both take that count and the left-out count and add `export_charts.lines`.

Both writers call `checkpoint` once per chart, charge every chart part to `expanded`, and on any failure remove the partial file as they do today.

- [ ] **Step 1: Write the failing tests.**

```python
def test_streamed_export_writes_charts(self):     # both formats; same workbook as Task 4's first test
    job = export_job(client, fmt)                  # the export-job helper test_large_export.py uses
    self.assertIn("Charts: 2 written.", job["report"])
    self.assertEqual([c["name"] for c in imported_charts_of_file(job)], ["Chart 1", "Chart 2"])

def test_streamed_and_inline_exports_hold_the_same_charts(self): ...   # chart parts equal, part for part
def test_sixty_four_charts_of_sixty_four_series(self): ...             # completes; 64 written
def test_cancel_while_writing_charts_removes_the_file(self): ...       # checkpoint raises CANCELLED on the first chart
def test_left_out_charts_are_named(self): ...                          # as Task 4, through the job
```

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest tests.test_chart_export_paths -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests.test_chart_export_paths tests.test_large_export tests.test_interchange_clients`. Expected: PASS. `grep -rn "native_charts\|Charts are native-only" service/` prints nothing.
- [ ] **Step 5: Commit** `git commit -am "Write charts in the streamed XLSX and ODS exports"`.

### Task 6: Import report clean-up

**Files:**
- Modify: `service/datahorizon/xlsx_features.py`, `service/datahorizon/ods_features.py`, and the import paths that add their entries (`xlsx_stream.read_workbook`, `ods_stream.read_workbook`, `interchange._xlsx_preview`, `ods.py` preview)
- Test: `tests/test_import_chart_paths.py` (add)

**Interfaces:** none new for later tasks.

Rule, from the design: an import no longer reports `workbook.feature` `charts` (every chart already has a `chart.converted` or `chart.skipped` entry, or imports cleanly). `sheet.feature` `drawings` is reported for a sheet unless that sheet's drawing was read and held only chart frames. `chart_sheets` is unchanged.

- [ ] **Step 1: Write the failing tests**, for the staged, inline and one-sheet paths and both formats:

```python
def test_imported_charts_are_not_listed_as_omitted(self):
    entries = import_report(xlsx_with_charts(...))     # existing fixture builder
    self.assertNotIn(("workbook.feature", "charts"), categories_and_details(entries))
    self.assertNotIn(("sheet.feature", "drawings"), categories_and_details(entries))

def test_a_drawing_with_a_picture_is_still_reported(self): ...     # chart + picture in one drawing: "drawings" stays
def test_an_unreadable_drawing_is_still_reported(self): ...        # "drawings" stays, with chart.skipped "unreadable"
def test_chart_sheets_are_still_reported(self): ...                # tests/fixtures/corpus/chart-sheet.xlsx
```

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest tests.test_import_chart_paths -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests.test_import_chart_paths tests.test_import_charts tests.test_xlsx_charts tests.test_ods_charts` and the corpus scorecard test. Expected: PASS; regenerate `docs/design/evidence/excel-fidelity-scorecard.json` if its counts change, and say so in the commit.
- [ ] **Step 5: Commit** `git commit -am "Stop reporting imported charts as omitted features"`.

### Task 7: Round trip, documents and evidence

**Files:**
- Create: `tests/test_chart_round_trip.py`, `docs/design/evidence/charts-export-verification.txt`
- Modify: `docs/design/compatibility-v0.md` (Charts), `docs/design/charts-design.md` (sections 11 and 12), `docs/design/implementation-status.md`, `docs/design/charts-authoring-export-design.md` (status), the OpenAPI document in `service.py` if any export result schema lists report lines or entries by name

- [ ] **Step 1: Write the round-trip tests.**

```python
def test_every_supported_chart_round_trips(self):     # both formats, inline and streamed
    # one native workbook holding a chart from each of tests/fixtures/charts/*.json on its data
    for fmt, route in product(("xlsx", "ods"), (inline, streamed)):
        again = import_whole(route(book, fmt))
        self.assertEqual(comparable(charts_of(again)), comparable(charts_of(book)))
        # comparable: type, grouping, title, axes, legend, sources as (sheet name, ref), series names,
        # name, anchor, offset, size, and order
        self.assertEqual(chart_entries(import_report), [])

def test_fixture_file_round_trips(self):              # tests/fixtures/charts-two-sheets.xlsx: import, export, import
    self.assertEqual(summary(third), summary(first))

def test_documented_differences(self):
    # one each: a 300-character title comes back cut to 255; two charts named "Sales" on one sheet come back
    # "Sales" and a unique name; a series with no name comes back "Series 1"
```

- [ ] **Step 2: Run** `PYTHONPATH=service python3 -m unittest tests.test_chart_round_trip -v`. Expected: PASS if Tasks 1 to 6 are right. A failure here is a defect in an earlier task: fix it there, test-first.
- [ ] **Step 3: Run the whole service suite and the desktop suite** (`cmake --build build --target desktop_tests && ctest --test-dir build -R '^desktop$' --output-on-failure`). Record commands and results in the evidence file.
- [ ] **Step 4: Manual check.** Export a workbook with one chart of each type to XLSX and ODS. Open each in Excel and in LibreOffice where installed on the reference Mac and confirm every chart shows its data after the application recalculates. Run the owner's `Column Chart_Complete.xlsx` through import and export if the file is on the machine. Record what was opened in what; write "not verified" for an application that is not installed and for the owner's file if absent.
- [ ] **Step 5: Update the documents.** Compatibility contract: replace "Export still omits charts" with the export table from the design's section 6, the six reasons, the selection rule, "no cached values", and the three documented round-trip differences. Charts design: section 11 and the stage 4 row. Status document: a new dated section at the top, what was measured and what was not verified; correct the three "not merged" statements for the chart branches merged through PR 140. Design: status line to "export built".
- [ ] **Step 6: Commit** `git commit -am "Round-trip charts through export and document chart export"`.

## After the last task

Request a whole-branch review before opening a pull request, as the earlier chart branches did.
