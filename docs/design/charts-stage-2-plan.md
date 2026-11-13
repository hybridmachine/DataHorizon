# Charts stage 2: engine and interfaces (range sources) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A script with no desktop can create, change, resolve, render, undo and delete all six chart types over cell ranges through the engine, REST, the managed CLI and the SDK, with charts stored in the native workbook at format v8.

**Architecture:** A chart is a row in the existing `dh_objects` catalog under a new `chart` namespace. Chart writes are managed operation actions that record a `('chart', sheet_id, chart_id)` entry in the operation's change set, so they share the existing undo and redo path. Resolve and render run as read-only snapshot jobs modelled on `CalculationJobs`; the stage 1 renderer moves into the service and lays text out against a metrics table for one bundled font.

**Tech Stack:** Python standard library only in the service; SQLite; the existing `PreparationJobs` job runner; `unittest`; the stage 1 Qt SVG probe for font and pie measurements.

**Spec:** [charts-design.md](charts-design.md) (Draft 0.3), sections 4 to 8 and 12. Evidence for the renderer: [chart-render-experiment.md](chart-render-experiment.md).

**Scope (Owner, November 12, 2026):** range sources only. `table` and `query` sources get their own plan after this one lands. Desktop work and workbook-Python chart operations are stage 3.

## Global Constraints

- The service imports nothing outside the Python standard library. No new pip dependency, including for tests that run in the `service` suite.
- Every new source file starts with `# SPDX-License-Identifier: GPL-3.0-only`.
- Chart font (Owner, November 12, 2026): DejaVu Sans, release 2.37, file `DejaVuSans.ttf`. The SVG names the family `DejaVu Sans`.
- Limits, each refused with a structured error that names the limit, never truncated: 10,000 points per series (`LIMIT_POINTS`), 64 series (`LIMIT_SERIES`), 100,000 source cells per chart (`LIMIT_CELLS`), 64 charts per workbook (`LIMIT_CHARTS`), 256 KiB per chart payload (`LIMIT`), and the pie point limit chosen in Task 9 (`LIMIT_PIE_POINTS`, provisional value 1,000).
- Chart size is 64×48 to 4,096×4,096 pixels. Rendered SVG stays inside the stage 1 element list: `svg`, `g`, `rect`, `line`, `path`, `circle`, `text`.
- Chart operations raise no Python events and are not cell changes: no change handler or binding runs for them.
- Resolve and render never write to the workbook and never return partial series.
- Format v8 is entered only by the first chart write, inside that operation's transaction. No operation lowers the version, including undo. A `chart` row below v8 is `INVALID_FORMAT` on open.
- Errors use `EngineError(code, message, status)` from `engine.py`. New codes: `CHART_NOT_FOUND` (404), `INVALID_SPEC` (400), `UNSUPPORTED_SPEC_VERSION` (409), `SOURCE_BROKEN` (409), `LIMIT_POINTS`, `LIMIT_SERIES`, `LIMIT_CELLS`, `LIMIT_CHARTS`, `LIMIT_PIE_POINTS`, `NEGATIVE_IN_PIE`, `VALUE_RANGE` (all 400), `CALCULATION_FAILED` (409).
- Run the chart tests with `cd tests && PYTHONPATH=../service python3 -m unittest <module> -v`. The whole suite is `ctest --test-dir build -R service`.

## Review Focus

Each line has a test in the task named.

1. **A direct SQLite edit leaves an unreadable chart specification.** The workbook still opens; the chart is listed with an `issue`, resolve refuses with `INVALID_SPEC`, and nothing is rewritten. (Task 4)
2. **A chart's source lies on rows that are then deleted, or on a sheet that is then deleted.** The source is marked broken, the receipt lists the chart, resolve refuses with `SOURCE_BROKEN`, and the source is never silently repointed. (Task 5)
3. **A chart over formula cells nobody has scrolled to, one of which fails or times out.** Values are calculated inside the job; a formula error plots as a counted gap; a calculation that cannot finish fails the whole job with no partial series. (Task 6)
4. **The workbook changes while a resolve job is running.** The job's result is reported as `stale` and carries no series. (Task 7)
5. **A cell change handler is bound and a chart is created, changed, undone and deleted.** The handler never runs and the operation commits. (Task 4)

## File Structure

| File | Responsibility |
| --- | --- |
| `service/datahorizon/chart_render.py` | The stage 1 renderer, moved from `tests/chart_render_experiment.py`. Resolved chart in, SVG and marks out. |
| `service/datahorizon/chart_font.py` | Loads the metrics table; `default_metrics()`. |
| `service/datahorizon/fonts/DejaVuSans.ttf`, `fonts/DejaVuSans-metrics.json`, `fonts/LICENSE-DejaVu.txt` | The bundled font, its advance table and its license. |
| `tools/font_metrics.py` | Standard-library TrueType reader that writes the metrics table. |
| `service/datahorizon/charts.py` | Payload and specification validation, storage, operation actions, list/read, structural maintenance. |
| `service/datahorizon/chart_resolve.py` | Reads sources through the engine into a resolved chart; `suggest`. |
| `service/datahorizon/chart_jobs.py` | `ChartJobs`: resolve and render as cancellable snapshot jobs. |
| `service/datahorizon/charts_api.py` | OpenAPI schemas and action examples. |
| `tests/test_chart_render.py` | Moved from `tests/test_chart_render_experiment.py`. |
| `tests/test_chart_font.py`, `test_charts.py`, `test_chart_structure.py`, `test_chart_resolve.py`, `test_chart_jobs.py`, `test_chart_clients.py`, `test_format_v8.py` | One file per task below. |

---

### Task 1: Move the renderer into the service

**Files:**
- Move: `tests/chart_render_experiment.py` → `service/datahorizon/chart_render.py`
- Move: `tests/test_chart_render_experiment.py` → `tests/test_chart_render.py`
- Modify: `tests/chart_render_benchmark.py` (import), `docs/design/chart-render-experiment.md` (one note)

**Interfaces:**
- Produces: `datahorizon.chart_render` with the stage 1 names unchanged: `render(resolved: dict, width: int, height: int, metrics: dict[str, float] | None = None) -> {"svg": str, "marks": list[dict]}`, `ChartRenderError(code)`, `LIMIT_POINTS`, `LIMIT_SERIES`, `LABEL_PX`, `estimate_text_width`, `nice_ticks`, `format_tick`.
- Produces: `ChartRenderError` becomes a subclass of `engine.EngineError` with status 400, so the service serialises it without a wrapper. `error.code` keeps working.

- [ ] **Step 1: Move both files with `git mv`** and change the imports to `from datahorizon import chart_render as cr` in the test and `from datahorizon import chart_render as cr` in the benchmark.

- [ ] **Step 2: Run the moved tests**

Run: `cd tests && PYTHONPATH=../service python3 -m unittest test_chart_render -v`
Expected: 35 tests, OK.

- [ ] **Step 3: Write the failing test** in `tests/test_chart_render.py`

```python
class ServiceErrors(unittest.TestCase):
    def test_render_errors_are_engine_errors(self):
        from datahorizon.engine import EngineError
        with self.assertRaises(EngineError) as caught:
            cr.render(one([1.0], type_="radar"), 800, 500)
        self.assertEqual((caught.exception.code, caught.exception.status), ("UNSUPPORTED_TYPE", 400))
        self.assertEqual(caught.exception.body["code"], "UNSUPPORTED_TYPE")
```

- [ ] **Step 4: Run it.** Expected: FAIL, `ChartRenderError` is not an `EngineError`.

- [ ] **Step 5: Make `ChartRenderError(EngineError)`** with `__init__(self, code, message="")` calling `super().__init__(code, message or code, 400)`. Update the module docstring: it is service code now.

- [ ] **Step 6: Run the module again.** Expected: 36 tests, OK.

- [ ] **Step 7: Add one sentence** under the summary of `chart-render-experiment.md`: the renderer and its tests moved to `service/datahorizon/chart_render.py` and `tests/test_chart_render.py` in stage 2; paths in this document are as they were when it ran.

- [ ] **Step 8: Commit** — `Move the chart renderer into the service`.

---

### Task 2: Bundled font and metrics table

**Files:**
- Create: `tools/font_metrics.py`, `service/datahorizon/chart_font.py`, `service/datahorizon/fonts/` (three files), `tests/test_chart_font.py`
- Modify: `service/datahorizon/chart_render.py`, `tests/chart_svg_probe.cpp`, `tests/chart_render_benchmark.py`, `LICENSES.md`

**Interfaces:**
- Produces: `tools/font_metrics.py`: `advances(ttf: bytes) -> dict[str, float]` (character → advance in ems, for every code point in the font's Unicode `cmap` from U+0020 up, excluding U+007F–U+009F) and a CLI `python3 tools/font_metrics.py <font.ttf> <out.json>` writing `{"family": "DejaVu Sans", "source": "DejaVuSans.ttf", "sha256": "<hex>", "units_per_em": int, "advances": {...}}` with sorted keys.
- Produces: `chart_font.FAMILY = "DejaVu Sans"`, `chart_font.default_metrics() -> dict[str, float]` (cached; reads `fonts/DejaVuSans-metrics.json` beside the module), `chart_font.font_path() -> pathlib.Path`.
- Changes: `chart_render.render(..., metrics=None)` uses `default_metrics()` when `metrics` is `None`, and writes `font-family="DejaVu Sans, sans-serif"`. A character missing from the table keeps the stage 1 class estimate (1.00 em outside ASCII).

**Obtaining the font.** `DejaVuSans.ttf` comes from the DejaVu Fonts 2.37 release archive (`dejavu-fonts-ttf-2.37`), with its `LICENSE` file copied as `LICENSE-DejaVu.txt`. Downloading it needs the owner's go-ahead at execution time. Record the file's SHA-256 in the metrics JSON and in `LICENSES.md`.

- [ ] **Step 1: Write the failing tests** in `tests/test_chart_font.py`

```python
class Reader(unittest.TestCase):
    def test_reads_advances_from_a_minimal_truetype_file(self):
        # build_font(units_per_em, {char: advance_units}) assembles head, hhea, maxp, hmtx and a format 4 cmap
        data = build_font(1000, {"A": 600, "i": 250, " ": 300})
        self.assertEqual(font_metrics.advances(data), {" ": 0.3, "A": 0.6, "i": 0.25})

    def test_reads_format_12_cmap_and_shared_last_advance(self):
        data = build_font(2048, {"A": 1024, "\U0001F600": 2048}, cmap_format=12, long_metrics=1)
        self.assertEqual(font_metrics.advances(data), {"A": 0.5, "\U0001F600": 0.5})

    def test_rejects_a_file_that_is_not_truetype(self):
        with self.assertRaises(ValueError):
            font_metrics.advances(b"not a font")


class Bundled(unittest.TestCase):
    def test_table_matches_the_bundled_font(self):
        table = json.loads((FONTS / "DejaVuSans-metrics.json").read_text())
        data = chart_font.font_path().read_bytes()
        self.assertEqual(table["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(table["advances"], font_metrics.advances(data))
        self.assertEqual(table["family"], chart_font.FAMILY)

    def test_digits_share_one_advance(self):
        m = chart_font.default_metrics()
        self.assertEqual(len({m[d] for d in "0123456789"}), 1)

    def test_render_names_the_family_and_uses_the_table(self):
        out = cr.render(load("column_clustered"), 800, 500)
        self.assertIn('font-family="DejaVu Sans, sans-serif"', out["svg"])
        self.assertEqual(cr.render(load("column_clustered"), 800, 500, chart_font.default_metrics()), out)
```

`build_font` is a test helper in the same file (`struct.pack`, big-endian tables with a correct table directory).

- [ ] **Step 2: Run.** Expected: import errors for `font_metrics` and `chart_font`.

- [ ] **Step 3: Implement `tools/font_metrics.py`.** Read the table directory; `head.unitsPerEm`; `hhea.numberOfHMetrics`; `hmtx`; the `cmap` subtable chosen in this order: platform 3 encoding 10 (format 12), platform 0 (format 12 or 4), platform 3 encoding 1 (format 4). Glyphs past `numberOfHMetrics` take the last advance. Round advances to 4 places. Raise `ValueError` on a bad signature, a missing table or an out-of-range offset.

- [ ] **Step 4: Add the font files, generate the table, implement `chart_font.py`,** change the renderer's default metrics and family.

- [ ] **Step 5: Run `test_chart_font` and `test_chart_render`.** Expected: both OK. Stage 1 tests that pinned `font-family="sans-serif"` or estimated without a table are updated to pass `metrics={}` where they test the class estimate.

- [ ] **Step 6: Teach the probe the font.** Add `--font-file <ttf>` to `tests/chart_svg_probe.cpp`: call `QFontDatabase::addApplicationFont`, fail with exit 2 if it returns −1, and measure labels with the loaded family instead of `sans-serif`. `chart_render_benchmark.py` passes `--font-file` with `chart_font.font_path()` and computes `text` from the bundled table.

- [ ] **Step 7: Run the benchmark** on the reference Mac.

Run: `python3 tests/chart_render_benchmark.py --probe build/chart_svg_probe --work build/chart-render --evidence docs/design/evidence/charts-stage-2`
Expected: `text.max_under` ≤ 1.05 and `text.median_over` ≤ 1.35 with the bundled font loaded. If `max_under` exceeds 1.05, do not tune the threshold: record it and add the measured worst ratio as a width multiplier `chart_font.SAFETY` applied in `estimate_text_width`, with a test, and say so in the Task 9 result.

- [ ] **Step 8: Update `LICENSES.md`** with a DejaVu Sans paragraph (Bitstream Vera and Arev licenses, public-domain DejaVu changes, version, SHA-256, shipped under `service/datahorizon/fonts/`).

- [ ] **Step 9: Commit** — `Bundle DejaVu Sans and lay chart text out against its metrics`.

---

### Task 3: Chart payload and specification validation

**Files:**
- Create: `service/datahorizon/charts.py`, `tests/test_charts.py`

**Interfaces:**
- Produces: `charts.SPEC_VERSION = 1`, `charts.TYPES = ("column","bar","line","area","pie","scatter")`.
- Produces: `charts.normalise_spec(book, spec: dict) -> dict`. Input sources are `{"kind": "range", "sheet": <name or id>, "ref": "A2:A13"}`. Output sources are `{"kind": "range", "sheet_id": str | None, "row1": int | None, "col1": ..., "row2": ..., "col2": ...}` (the `names.py` field names; all `None` when broken). Raises `INVALID_SPEC`, `UNSUPPORTED_SPEC_VERSION`, `LIMIT_SERIES`, `LIMIT_POINTS`, `LIMIT_CELLS`, `LIMIT_PIE_POINTS`, `SHEET_NOT_FOUND`.
- Produces: `charts.validate_stored(payload: dict) -> str | None`. Raises `INVALID_FORMAT` for a bad envelope; returns `None` for a readable chart, or an issue code (`"UNSUPPORTED_SPEC_VERSION"` or `"INVALID_SPEC"`) for an envelope whose specification this service cannot read.
- Produces: `charts.describe(db, sheet_id, chart_id, payload) -> dict`: the payload plus `id`, `sheet_id`, `issue`, and on every source `ref` (A1 text or `None`) and `broken`.

**The specification, version 1.** Exactly these keys; unknown keys are `INVALID_SPEC`.

| Key | Rule |
| --- | --- |
| `spec_version` | `1`. Greater than 1 is `UNSUPPORTED_SPEC_VERSION` on write. |
| `type` | One of `TYPES`. |
| `grouping` | `"clustered"` or `"stacked"` for column, bar and area; `null` otherwise. Default `"clustered"` / `null`. |
| `title` | `{"text": str \| null}`, at most 255 characters. |
| `categories` | `null` or `{"source": S}`. Must be `null` for scatter. |
| `series` | 1 to 64 items (exactly 1 for pie): `{"name": {"text": str} \| {"source": S}, "values": {"source": S}, "x": {"source": S} \| null}`. `x` is required for scatter and `null` otherwise. |
| `axes` | `{"category": {"title": str \| null}, "value": {"title": str \| null, "min": number \| null, "max": number \| null}}`. `min` ≤ `max` when both are set; finite. |
| `legend` | `{"position": "right" \| "bottom" \| "none"}`. |

A values, categories or x source is one row or one column of 1 to 10,000 cells (1 to `LIMIT_PIE_POINTS` for a pie's values and categories). A name source is one cell. The cells of all sources together are at most 100,000.

**The stored payload.** Exactly `{"name", "anchor", "offset", "size", "z", "spec_version", "spec"}`: `name` 1 to 64 printable characters (`metadata.name`); `anchor` `{"row": 1..MAX_ROWS, "col": 1..MAX_COLS}`; `offset` `{"x": 0..4096, "y": 0..4096}`; `size` `{"width": 64..4096, "height": 48..4096}`; `z` an integer −1,000,000 to 1,000,000; `spec_version` equal to `spec["spec_version"]`.

- [ ] **Step 1: Write the failing tests** in `tests/test_charts.py`, class `Spec`. Use a temporary `Workbook(path, create=True)` and a helper `spec(type_="column", **overrides)` that returns a valid version 1 specification over `Sheet1!A2:A13` (categories) and `B1` / `B2:B13` (one series).

```python
def test_normalise_turns_refs_into_stored_rectangles(self):
    out = charts.normalise_spec(self.book, spec())
    self.assertEqual(out["series"][0]["values"]["source"],
                     {"kind": "range", "sheet_id": self.sheet, "row1": 2, "col1": 2, "row2": 13, "col2": 2})
    self.assertEqual(out["grouping"], "clustered")

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
    for overrides, code in cases:
        with self.assertRaises(EngineError) as caught:
            charts.normalise_spec(self.book, spec(**overrides))
        self.assertEqual(caught.exception.code, code, overrides)

def test_validate_stored_separates_bad_envelopes_from_unreadable_specs(self):
    good = payload(charts.normalise_spec(self.book, spec()))
    self.assertIsNone(charts.validate_stored(good))
    newer = dict(good, spec_version=2, spec=dict(good["spec"], spec_version=2, shiny=True))
    self.assertEqual(charts.validate_stored(newer), "UNSUPPORTED_SPEC_VERSION")
    self.assertEqual(charts.validate_stored(dict(good, spec=dict(good["spec"], type="radar"))), "INVALID_SPEC")
    for broken in (dict(good, size={"width": 10, "height": 10}), {k: v for k, v in good.items() if k != "z"},
                   dict(good, extra=1), dict(good, spec_version=3)):
        with self.assertRaises(EngineError) as caught:
            charts.validate_stored(broken)
        self.assertEqual(caught.exception.code, "INVALID_FORMAT")

def test_describe_adds_refs_and_broken_flags(self):
    stored = charts.normalise_spec(self.book, spec())
    stored["categories"]["source"].update(row1=None, col1=None, row2=None, col2=None)
    out = charts.describe(self.book.db, self.sheet, "c1", payload(stored))
    self.assertEqual(out["spec"]["series"][0]["values"]["source"]["ref"], "B2:B13")
    self.assertEqual((out["spec"]["categories"]["source"]["ref"], out["spec"]["categories"]["source"]["broken"]), (None, True))
```

- [ ] **Step 2: Run.** Expected: FAIL, no module `datahorizon.charts`.

- [ ] **Step 3: Implement the three functions.** Parse `ref` with `formula.split_ref` as `names.parse_reference` does (no sheet qualifier inside `ref`); resolve `sheet` with `book.sheet`. `validate_stored` checks stored-form sources (integers in grid range or all `None`, `sheet_id` a string or `None`) and applies the same shape rules as `normalise_spec` without touching the database. A broken source is valid stored content.

- [ ] **Step 4: Run.** Expected: PASS.

- [ ] **Step 5: Commit** — `Validate chart specifications and stored chart payloads`.

---

### Task 4: Storage, format v8, operations and undo

**Files:**
- Modify: `service/datahorizon/charts.py`, `service/datahorizon/engine.py` (`validate` near :503 and :511-524, `mutate` near :1250 and :1394, :1444-1458), `service/datahorizon/service.py` (:45, :60-67, routes, the `install` call near :286), `tests/test_format_v6.py`, `tests/test_format_v7.py`, `tests/test_service.py` (the action set at :440)
- Create: `service/datahorizon/charts_api.py`, `tests/test_format_v8.py`; extend `tests/test_charts.py`

**Interfaces:**
- Produces: operation actions through `Workbook.mutate`:
  - `chart_create`: `{sheet, name?, anchor?, offset?, size?, spec}` → receipt gains `"chart_id"` (a `uuid4` string). Defaults: `name` `"Chart N"` (lowest unused N on the sheet), `anchor` `{"row":1,"col":1}`, `offset` `{"x":0,"y":0}`, `size` `{"width":480,"height":300}`, `z` one above the sheet's highest.
  - `chart_update`: `{chart_id, name?, anchor?, offset?, size?, z?, order?: "front"|"back", spec?, spec_patch?}`. `spec` replaces; `spec_patch` is an RFC 7386 merge patch applied to the input form of the stored specification, then normalised. `spec` with `spec_patch`, or `z` with `order`, is `INVALID_ARGUMENT`. A chart with an `issue` accepts `spec` (replace) and placement changes, and refuses `spec_patch` with its issue code.
  - `chart_delete`: `{chart_id}`.
- Produces: `charts.mutate(book, request) -> dict`, `charts.store(book, sheet_id, chart_id, payload | None)` (the only writer; records `book.changes[('chart', sheet_id, chart_id)] = [before, after]`), `charts.objects(book) -> list[(sheet_id, chart_id, payload)]`, `charts.find(book, chart_id) -> (sheet_id, payload)` raising `CHART_NOT_FOUND`, `charts.read(book, args) -> {"state", "charts": [describe(...)]}` with optional `sheet` and `chart_id` filters and a required `expected_state`.
- Produces: REST `POST /v1/workbooks/{workbook}/charts` → `charts.read`. Receipts gain `"changed_charts": int`.
- Consumes: `relational.ensure_version(book, 8)`, `book.ensure_metadata()`, the 256-object / 16 MiB `dh_objects` budget check used by `records.store`.

- [ ] **Step 1: Write the failing tests.** `tests/test_format_v8.py` (model on `tests/test_format_v7.py`) and class `Operations` in `tests/test_charts.py`:

```python
# test_format_v8.py
def test_first_chart_write_bumps_to_v8_and_reopens(self): ...        # version 8 after chart_create; reopen lists the chart
def test_undo_of_the_first_chart_keeps_v8(self): ...
def test_v7_file_with_a_chart_row_is_invalid_format(self): ...       # direct INSERT at v7 → INVALID_FORMAT on open
def test_v9_is_rejected_on_open(self): ...                           # FORMAT_VERSION
def test_capabilities_advertise_v8(self):                            # (8, [1,2,3,4,5,6,7,8]) and "charts": True
def test_direct_edit_to_an_unreadable_spec_opens_and_reports(self):  # Review Focus 1
    # UPDATE dh_objects SET payload=json_set(payload,'$.spec.type','radar') through sqlite3
    book = Workbook(path)
    listed = charts.read(book, {"expected_state": book.state()})["charts"]
    self.assertEqual(listed[0]["issue"], "INVALID_SPEC")
    self.assertEqual(json.loads(raw_payload(path))["spec"]["type"], "radar")      # not rewritten
def test_direct_edit_to_a_bad_envelope_is_invalid_format(self): ...

# test_charts.py, class Operations
def test_create_update_delete_round_trip(self): ...                  # read() after each; chart_id stable; changed_charts == 1
def test_defaults_name_z_and_size(self): ...                         # "Chart 1", "Chart 2"; z 0, 1; 480x300
def test_spec_patch_is_a_merge_patch(self):                          # {"title": {"text": "T"}, "legend": {"position": "none"}}
def test_order_front_and_back(self): ...
def test_undo_and_redo_each_chart_operation(self):
    # create → undo (gone) → redo (same id and payload) → update → undo (old payload) → delete → undo (restored)
def test_undo_conflict_when_the_chart_changed_underneath(self): ...  # HISTORY_CONFLICT
def test_limit_charts(self): ...                                     # the 65th chart_create → LIMIT_CHARTS
def test_unknown_chart(self): ...                                    # CHART_NOT_FOUND, 404
def test_chart_operations_leave_cells_and_cell_counts_alone(self):   # changed_cells == 0
def test_bound_cell_change_handler_does_not_run_for_chart_operations(self):   # Review Focus 5
    # save and approve a module with a cell_change binding that writes a marker cell (pattern: tests/test_binding_execution.py);
    # chart_create, chart_update, undo, chart_delete all commit and the marker cell stays empty
```

- [ ] **Step 2: Run.** Expected: FAIL (unknown action / `FORMAT_VERSION`).

- [ ] **Step 3: Implement.**
  - `engine.py` `validate`: accept version 8; in the `dh_objects` loop accept namespace `chart` only when the version is ≥ 8, call `charts.validate_stored`, count charts ≤ 64.
  - `engine.py` `mutate`: dispatch `chart_create` / `chart_update` / `chart_delete` to `charts.mutate`; in undo/redo handle `key[0] == 'chart'` (compare the current stored payload with `current`, else `HISTORY_CONFLICT`; write `wanted` through `charts.store`); count `changed_cells` over keys that are neither `'table'` nor `'chart'`; add `changed_charts`; pass only cell keys to `dependencies.invalidate`. Chart actions are not in `structural`, so they keep history.
  - `charts.store`: `ensure_metadata()`, `ensure_version(book, 8)`, upsert or delete, the shared budget check, then record the change.
  - `service.py`: `format_version` 8 and the supported list; `CAPABILITIES['operations'].extend(['chart_create','chart_update','chart_delete'])`; `CAPABILITIES.update(charts=True, max_charts=64, chart_spec_version=1)`; the `charts` read route in `ROUTES` and `_dispatch_book`.
  - Update the two existing `test_v8_is_rejected_on_open` tests to version 9, the capabilities assertion in `tests/test_format_v7.py:203`, and the action set in `tests/test_service.py:440`.

  - `charts_api.py`: `install(schemas, actions)` in the manner of `names_api.py`, with the three action entries and runnable examples and a `ChartsReadRequest` schema. `tests/test_service.py:427` requires every advertised operation to have one, so they land in this task; Task 7 adds the job and suggest schemas.

- [ ] **Step 4: Run** `test_charts test_format_v6 test_format_v7 test_format_v8 test_service`. Expected: PASS.

- [ ] **Step 5: Commit** — `Store charts at native format v8 with undoable chart operations`.

---

### Task 5: Following the data

**Files:**
- Modify: `service/datahorizon/charts.py`, `service/datahorizon/engine.py` (`change_structure` near :1768, `delete_sheet` near :1595, `move_cells` near :1555, `duplicate_sheet` near :1667)
- Create: `tests/test_chart_structure.py`

**Interfaces:**
- Produces: `charts.adjust_structure(book, sheet_id, axis, start, count, delete) -> list[str]` (ids of charts with a newly broken source), same argument meanings as `names.adjust_structure`; `charts.break_sheet(book, sheet_id) -> list[str]`; `charts.adjust_move(book, sheet_id, rectangle, dest_row, dest_col) -> None`.
- Consumes: `formula.adjust_interval(first, last, start, count, delete, maximum)`.
- Receipts of row/column edits and sheet deletion gain `"broken_charts": [chart_id, ...]` when not empty.

Rules: a source on the edited sheet moves, grows or shrinks as a name's rectangle does, and becomes broken (all coordinates `None`, `sheet_id` kept) when wholly deleted or pushed off the grid. An anchor on the edited sheet shifts with inserts and deletes; an anchor inside a deleted span moves to `start`, clamped to the grid. A source wholly inside a moved rectangle moves with it; a partly overlapping source stays. Deleting a sheet removes its charts (foreign-key cascade) and breaks sources on other sheets that read from it (`sheet_id` `None`). Duplicating a sheet does not copy its charts.

- [ ] **Step 1: Write the failing tests**

```python
def test_rows_inserted_above_shift_sources_and_anchor(self): ...      # B2:B13 → B4:B15 after 2 rows at 1
def test_rows_inserted_inside_grow_the_source(self): ...              # B2:B13 → B2:B14
def test_rows_deleted_inside_shrink_the_source(self): ...
def test_deleting_every_source_row_breaks_the_source(self):           # Review Focus 2
    receipt = self.apply("rows_delete", start=2, count=12)
    self.assertEqual(receipt["broken_charts"], [self.chart])
    source = self.read_chart()["spec"]["series"][0]["values"]["source"]
    self.assertEqual((source["broken"], source["ref"]), (True, None))
def test_columns_follow_the_same_rules(self): ...
def test_edit_on_another_sheet_leaves_the_chart_alone(self): ...
def test_deleting_the_data_sheet_breaks_sources_on_other_sheets(self): ...   # chart on Sheet2 over Sheet1
def test_deleting_the_owning_sheet_deletes_its_charts(self): ...
def test_cut_moves_a_wholly_contained_source_and_undo_restores_it(self): ...
def test_cut_leaves_a_partly_overlapping_source(self): ...
def test_duplicate_sheet_does_not_copy_charts(self): ...
def test_broken_source_survives_reopen(self): ...
```

- [ ] **Step 2: Run.** Expected: FAIL.

- [ ] **Step 3: Implement** the three functions (every write through `charts.store`) and call them from `change_structure` after `names.adjust_structure`, from `delete_sheet` before the sheet row is removed, and from `move_cells` after the cells move. All three return at once below format v8.

- [ ] **Step 4: Run** `test_chart_structure test_structure test_names_structure`. Expected: PASS.

- [ ] **Step 5: Commit** — `Keep chart sources and anchors in step with structural edits`.

---

### Task 6: Resolve and suggest

**Files:**
- Create: `service/datahorizon/chart_resolve.py`, `tests/test_chart_resolve.py`

**Interfaces:**
- Produces: `chart_resolve.resolve(snapshot, payload, state, checkpoint=None) -> dict`:
  `{"resolved": <the renderer's input>, "counts": {"points": int, "gaps": {"empty": int, "text": int, "error": int}}, "state": state}`. `resolved` is exactly what `chart_render.render` takes: `{"type", "grouping", "title", "categories": [str] | None, "series": [{"name": str, "values": [float | None], "x": [float | None] | None}], "axes", "legend"}`.
- Produces: `chart_resolve.suggest(book, args) -> {"spec": <input-form spec>}` for `{expected_state, sheet, r1, c1, r2, c2, type?}`.
- Consumes: `Workbook.read(sheet_id, {"r1","c1","r2","c2","calculation":"fresh"}, limit=MAX_CELLS)`; each call reads at most 10,000 cells, so a longer source is read in consecutive slices.

Rules for `resolve`:
- An `issue` on the chart raises that code. Any broken source raises `SOURCE_BROKEN`.
- Every `read` must return the `state` the job started at; otherwise raise `STALE_STATE` (409).
- A value or x cell: a finite number (not a Boolean) is a point. A missing cell is an `empty` gap; text, Boolean or a date/time is a `text` gap; `calculation == "error"` is an `error` gap. Hidden rows are plotted.
- A category is the cell's `display` text (empty string for a missing cell). A name source gives its cell's `display`.
- An `EngineError` with code `TIMEOUT` from a read raises `CALCULATION_FAILED` (409). A cancelled checkpoint propagates. There is no partial result.
- Call `checkpoint(cells_read, "Reading sources")` after each slice when a checkpoint is given.
- Pie: more live points than `LIMIT_PIE_POINTS` cannot occur (validated), a negative value raises `NEGATIVE_IN_PIE`.

Rules for `suggest`: the rectangle holds at most 10,000 cells. If the first row is all text (or empty) and at least one later row holds a number, it is a header row; likewise the first column for categories. Each remaining column is a series (at most 64; more is `LIMIT_SERIES`). `type` defaults to `"column"`. For `"scatter"` the first data column is `x` for every series. For `"pie"` only the first data column is used. The result is in input form (`sheet` name and `ref`) and passes `normalise_spec`.

- [ ] **Step 1: Write the failing tests**

```python
def test_resolves_stored_values_into_the_renderer_shape(self):
    out = chart_resolve.resolve(self.book, self.payload(), self.book.state())
    self.assertEqual(out["resolved"]["categories"][:2], ["Jan", "Feb"])
    self.assertEqual(out["resolved"]["series"][0], {"name": "Units", "values": [float(v) for v in range(1, 13)], "x": None})
    chart_render.render(out["resolved"], 480, 300)                    # accepted by the renderer

def test_gaps_are_counted_by_cause(self):                             # one empty, one text, one =1/0
    self.assertEqual(out["counts"], {"points": 9, "gaps": {"empty": 1, "text": 1, "error": 1}})

def test_formula_sources_are_calculated_without_a_viewport(self):     # Review Focus 3: =A2*2 in rows never read before
def test_calculation_that_cannot_finish_fails_with_no_series(self):   # Review Focus 3
    # patch snapshot.read to raise EngineError("TIMEOUT", ...) on the second slice → CALCULATION_FAILED
def test_sources_longer_than_one_read_are_read_in_slices(self): ...   # 10,000 points over 2 columns → 3 reads counted
def test_state_change_between_reads_is_stale(self): ...               # STALE_STATE
def test_broken_source_and_issue_are_refused(self): ...               # SOURCE_BROKEN; INVALID_SPEC
def test_scatter_reads_x(self): ...
def test_pie_negative_is_refused(self): ...                           # NEGATIVE_IN_PIE
def test_every_type_resolves_and_renders(self): ...                   # six types, SVG parses, marks non-empty

def test_suggest_detects_headers_and_categories(self):
    out = chart_resolve.suggest(self.book, dict(expected_state=s, sheet="Sheet1", r1=1, c1=1, r2=13, c2=3))
    self.assertEqual(out["spec"]["categories"], {"source": {"kind": "range", "sheet": "Sheet1", "ref": "A2:A13"}})
    self.assertEqual([x["name"] for x in out["spec"]["series"]],
                     [{"source": {"kind": "range", "sheet": "Sheet1", "ref": "B1"}}, {"source": {"kind": "range", "sheet": "Sheet1", "ref": "C1"}}])
    charts.normalise_spec(self.book, out["spec"])
def test_suggest_without_headers_names_series_by_position(self): ...  # {"text": "Series 1"}
def test_suggest_scatter_and_pie(self): ...
def test_suggest_refusals(self): ...                                  # STALE_STATE; LIMIT; LIMIT_SERIES; no numbers → INVALID_SPEC
```

- [ ] **Step 2: Run.** Expected: FAIL, no module.

- [ ] **Step 3: Implement both functions.**

- [ ] **Step 4: Run.** Expected: PASS.

- [ ] **Step 5: Commit** — `Resolve chart sources through the engine and suggest a specification from a range`.

---

### Task 7: Chart jobs, REST and OpenAPI

**Files:**
- Create: `service/datahorizon/chart_jobs.py`, `tests/test_chart_jobs.py`
- Modify: `service/datahorizon/engine.py` (accessor beside `calculation_jobs` at :469; close at :462), `service/datahorizon/service.py` (`ROUTES`, `_dispatch_book`, `openapi()` request and response schema maps), `service/datahorizon/charts_api.py`, `tests/test_job_openapi.py`

**Interfaces:**
- Produces: `ChartJobs(book)` with `start(request) -> dict` and `access(job_id, action=None) -> dict`, and `Workbook.chart_jobs()`. `start` takes `{job_id, expected_state, chart_id, kind: "resolve" | "render", width?, height?}`; `width` and `height` are required for `render` (64..4096 by 48..4096) and refused for `resolve`. It runs on `PreparationJobs(seconds=30)` with a read-only snapshot `Workbook(path, readonly=True, cancel_event=checkpoint.cancel_event)` whose calculator is wrapped exactly as `CalculationJobs.start` wraps it.
- Job view: the `PreparationJobs` view plus `state`, `chart_id`, `kind`. A completed job whose `state` no longer equals `book.state()` is returned with `status: "stale"` and no `result`. `result` for `resolve` is the `chart_resolve.resolve` output; for `render` it adds `"svg"`, `"marks"`, `"width"`, `"height"`.
- REST: `POST /v1/workbooks/{workbook}/charts/suggest`; `POST …/charts/jobs`; `GET …/charts/jobs/{job}`; `POST …/charts/jobs/{job}/cancel`; `POST …/charts/jobs/{job}/release` (the calculation job routes' shapes).
- OpenAPI: `charts_api.install(schemas, actions)` (created in Task 4) gains `ChartSpec`, `ChartSource`, `Chart`, `ChartSuggestRequest`, `ChartJobRequest` and `ChartJob`.

- [ ] **Step 1: Write the failing tests** (in-process `Service()` and `server.dispatch`, as `tests/test_service.py:694`)

```python
def test_resolve_job_completes_with_series_and_state(self): ...
def test_render_job_returns_svg_marks_and_size(self): ...             # svg parses; only the seven elements
def test_render_needs_a_size_and_resolve_refuses_one(self): ...       # INVALID_ARGUMENT
def test_job_is_stale_after_any_managed_edit(self):                   # Review Focus 4
    job = self.start("resolve"); self.wait(job)
    self.edit_cell("Z99", "1")
    view = self.get(job)
    self.assertEqual(view["status"], "stale"); self.assertNotIn("result", view)
def test_job_started_against_an_old_state_is_refused(self): ...       # STALE_STATE, 409
def test_cancel_and_release(self): ...
def test_same_job_id_with_other_inputs_conflicts(self): ...           # JOB_CONFLICT
def test_failed_job_carries_the_structured_error(self): ...           # broken source → status failed, error.code SOURCE_BROKEN
def test_render_over_the_response_limit_is_refused(self): ...         # 48 column series × 2,000 points at 4096×4096 → READ_LIMIT, no svg
def test_suggest_route(self): ...
def test_openapi_lists_chart_routes_and_schemas(self): ...            # every chart path has request and response $refs that exist
```

- [ ] **Step 2: Run.** Expected: FAIL (`NOT_FOUND`).

- [ ] **Step 3: Implement `ChartJobs`, the accessor, the routes and the OpenAPI entries.** Add chart cases to `tests/test_job_openapi.py` beside the calculation job cases.

- [ ] **Step 4: Run** `test_chart_jobs test_job_openapi test_service`. Expected: PASS.

- [ ] **Step 5: Commit** — `Resolve and render charts as cancellable jobs over REST`.

---

### Task 8: SDK and managed CLI

**Files:**
- Modify: `service/datahorizon/client.py` (beside `names` at :416 and `calculate` at :386), `service/datahorizon/cli.py` (beside the `names` group at :226 and `calculate` at :424)
- Create: `tests/test_chart_clients.py` (model on `tests/test_names_clients.py`)

**Interfaces:**
- Produces on `WorkbookClient`:
  - `charts(self, sheet=None, chart_id=None) -> list[dict]`
  - `create_chart(self, spec, *, sheet="Sheet1", name=None, anchor=None, offset=None, size=None, operation_id=None) -> dict` (the receipt, with `chart_id`)
  - `update_chart(self, chart_id, *, operation_id=None, **changes) -> dict`
  - `delete_chart(self, chart_id, *, operation_id=None) -> dict`
  - `suggest_chart(self, sheet="Sheet1", *, r1, c1, r2, c2, type="column") -> dict`
  - `chart_job(self, chart_id, kind, *, width=None, height=None, job_id=None) -> dict` and `chart_job_status(self, job_id, action=None) -> dict`
  - `resolve_chart(self, chart_id, *, timeout=30) -> dict` and `render_chart(self, chart_id, width, height, *, timeout=30) -> dict`: start a job, poll every 50 ms, release it, return `result`; raise `APIError` with the job's error body on `failed`, and with code `STALE_STATE` on `stale`.
- Produces CLI: `datahorizon charts <file> list [--sheet S]`, `create [--sheet S]` (specification and placement as JSON on stdin), `update <chart_id>` (changes as JSON on stdin), `delete <chart_id>`, `suggest --sheet S --r1 --c1 --r2 --c2 [--type T]`, `resolve <chart_id>`, `render <chart_id> --width W --height H [--svg-out PATH]`. Output is JSON through `emit_result`; with `--svg-out` the SVG is written to the file and replaced in the JSON by `"svg_path"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_sdk_rest_and_cli_agree(self): ...                            # create through SDK; list through SDK, raw REST and CLI give the same chart
def test_sdk_round_trip_for_all_six_types(self):
    # suggest → create → render → update(spec_patch title) → render → undo → delete, per type; every SVG parses
def test_render_cli_writes_svg_file(self): ...
def test_cli_error_is_structured_json_on_stderr_with_exit_1(self): ... # resolve of an unknown chart → CHART_NOT_FOUND
def test_resolve_chart_raises_on_stale(self): ...
```

- [ ] **Step 2: Run.** Expected: FAIL (`AttributeError`, unknown command).

- [ ] **Step 3: Implement the SDK methods and the CLI group.**

- [ ] **Step 4: Run** `test_chart_clients test_names_clients`. Expected: PASS.

- [ ] **Step 5: Commit** — `Add chart operations to the SDK and the managed CLI`.

---

### Task 9: Measurements, documents and the whole suite

**Files:**
- Modify: `tests/chart_render_benchmark.py`, `service/datahorizon/charts.py` / `chart_render.py` (the final `LIMIT_PIE_POINTS`), `docs/design/native-format-v1.md`, `docs/design/charts-design.md`, `docs/design/implementation-status.md`, `docs/design/README.md`, `docs/design/development.md`, `docs/design/interfaces-and-interoperability.md`
- Create: `docs/design/charts-stage-2-result.md`, `docs/design/evidence/charts-stage-2/` (benchmark JSON, capacity JSON, six SVG files and their PNGs), `examples/charts_script.py`

- [ ] **Step 1: Measure the pie limit.** Add `--pie-sizes 500,1000,2000` to the benchmark: render a one-series pie of each size at 800×500 and record Qt load plus paint (median of 20). `LIMIT_PIE_POINTS` is the largest size whose median is at most 75 ms (half the 150 ms threshold) on the reference Mac; if none qualifies, use 250 and say so. Set the constant, update the Task 3 limit test to the chosen value, rerun `test_charts`.

- [ ] **Step 2: Measure capacity through the service.** Extend the benchmark with `--service`: with `LocalService`, a workbook of 10,000 rows × 11 columns (one category column, ten value columns, half of them formulas), time `resolve_chart` and `render_chart` at 800×500 for an eight-series line chart (80,000 value cells plus 10,000 categories and eight names, under `LIMIT_CELLS`) and for a one-series column chart of 10,000 points. Record wall time and peak result size. Expected: each job completes inside its 30-second budget and under the 4 MiB result limit. If either fails, lower `LIMIT_CELLS` to the largest passing power of ten, update its test, and record that.

- [ ] **Step 3: Write `examples/charts_script.py`**: with `LocalService`, create a workbook, fill twelve rows, and for each of the six types suggest, create, render to `build/charts-example/<type>.svg`, patch the title, undo and redo, then delete one chart and undo. Run it; copy the six SVG files into the evidence directory and rasterise them with the probe.

Run: `PYTHONPATH=service python3 examples/charts_script.py`
Expected: exit 0 and six SVG files.

- [ ] **Step 4: Run every suite.**

Run: `cmake --build build && ctest --test-dir build`
Expected: all tests pass. Report any failure by name, including ones this plan did not cause.

- [ ] **Step 5: Write the documents.**
  - `native-format-v1.md`: retitle to "v1 through v8"; add a top section "Charts format v8" covering the entry trigger and its transaction, that no operation lowers the version, that v1–v7 readers reject v8, rejection of a `chart` row below v8, the payload and specification version 1 tables from Task 3, the stored source form, what direct readers can rely on, and what a direct edit does (not validated until open; an unreadable specification is reported and kept).
  - `charts-design.md`: Draft 0.4; record as built: stored source form and field names; chart limits (charts per workbook, cells per chart, pie points, size); that the resolve job calculates inside its own read-only snapshot rather than starting a second calculation job; that sheet duplication does not copy charts; the three operation actions and the job routes; stage 2 row marked done for range sources with table and query sources outstanding.
  - `charts-stage-2-result.md`: what was built, the measurements from Steps 1 and 2 and Task 2 Step 7, limits, and what is open (table and query sources, Windows and Linux font measurement, the desktop).
  - `implementation-status.md`, `README.md` (document map rows for this plan and the result), `development.md` (CLI and SDK examples), `interfaces-and-interoperability.md` (the chart resources).

- [ ] **Step 6: Commit** — `Record charts stage 2 measurements, format v8 and the as-built design`.

---

## Decisions made in this plan

Each is the author's call and open to reversal before execution.

- **Undo.** No existing `dh_objects` kind takes part in undo; names and bindings clear history. Charts follow the design instead and record their own change-set entries, so chart edits undo without clearing cell history.
- **Calculation inside the job.** The design says a resolve "starts a bounded calculation job and waits on it". The plan calculates inside the chart job's own read-only snapshot, with the same calculator wrapper, deadline checks and cancellation, because nesting two jobs adds a second lifetime to manage and no extra bound.
- **Stored sources are integers.** The design's illustration stores `"ref": "A2:A13"`. Stored sources use `row1`/`col1`/`row2`/`col2` so structural edits are arithmetic; `ref` text is accepted on input and returned on read.
- **Unreadable specifications do not block opening.** A bad envelope is `INVALID_FORMAT`, as for other kinds; a bad or newer specification is listed with an `issue`.
- **Limits.** 64 charts per workbook, 100,000 source cells per chart, chart size up to 4,096 pixels, 30-second chart jobs. Task 9 measures the cell limit and the pie limit.
- **No inline-specification render.** Rendering needs a stored chart. A preview of an unsaved specification is left to the desktop stage, which is its only user.
- **Sheet duplication does not copy charts,** matching how it treats declared lists today.
- **The font ships inside the service directory,** which every platform's install rule already copies; the desktop stage loads it from there.
