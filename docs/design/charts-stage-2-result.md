# Charts stage 2: result (range, table and saved-query sources)

November 12, 2026. The [stage 2 plan](charts-stage-2-plan.md) is executed for
range sources on branch `claude/charts-stage-2`. That branch is stacked on
`claude/charts-stage-1`, an open pull request. Nothing here is merged or
released. A script with no desktop can now create, change, resolve, render,
undo and delete all six chart types over cell ranges through the engine, REST,
the managed CLI and the SDK, with charts stored in the native workbook at
format v8.

The capacity run in the last task found three defects, each fixed test-first
before this document was written. A chart over thousands of formulas came back
mostly as error gaps. A large render overflowed the job result limit. And Qt
SVG ignored the bundled font. See
[Defects found by the capacity run](#defects-found-by-the-capacity-run). The
whole-branch review then found seven more, also fixed test-first; the most
serious was a running total over more than 1,000 rows plotting as error gaps.
See [Defects found by the whole-branch review](#defects-found-by-the-whole-branch-review).

Table and query sources, the desktop, workbook-Python chart operations,
interchange, and any measurement on Windows or Linux are not done.

**November 13, 2026.** Table and saved-query sources, the second step of stage
2, are now built on branch `claude/charts-table-query-sources`, which is not
merged. See [Table and saved-query sources](#table-and-saved-query-sources).
With them stage 2 is complete. The desktop, workbook-Python chart operations,
interchange and any measurement on Windows or Linux are still not done.

## What was built

| Part | Where | What it does |
| --- | --- | --- |
| Renderer | `service/datahorizon/chart_render.py` | The stage 1 renderer, now service code: resolved chart in, SVG Tiny subset and mark rectangles out. Its errors are engine errors |
| Bundled font | `service/datahorizon/fonts/`, `chart_font.py`, `tools/font_metrics.py` | DejaVu Sans 2.37 (`DejaVuSans.ttf`, SHA-256 `7da195a7…8954`), its license, and an advance table generated from the file by a standard-library TrueType reader. Text is laid out against the table, and the SVG names `DejaVu Sans` |
| Validation and storage | `charts.py` | Specification version 1 and the stored payload, `chart` rows in `dh_objects` at format v8, the three operation actions, undo and redo, and the following of structural edits |
| Resolve and suggest | `chart_resolve.py` | Reads each source in bounded slices through the engine, classifies gaps, and suggests a specification from a range |
| Jobs | `chart_jobs.py` | Resolve and render as cancellable 30-second jobs on a private read-only snapshot. A result is withheld as `stale` once the workbook moves on |
| REST | `charts_api.py`, `service.py` | Catalog read, suggest, the job routes, and OpenAPI schemas |
| SDK and CLI | `client.py`, `cli.py` | `charts`, `create_chart`, `update_chart`, `delete_chart`, `suggest_chart`, `chart_job`, `chart_job_status`, `resolve_chart` and `render_chart`; the CLI's `charts list\|create\|update\|delete\|suggest\|resolve\|render` |
| Example | `examples/charts_script.py` | Suggests, creates, renders, patches, undoes and redoes all six types, then deletes one chart and undoes the delete |

The format is in [Charts format v8](native-format-v1.md#charts-format-v8). On
November 12 the as-built design for range sources, with its departures from
Draft 0.3, was [charts-design.md](charts-design.md) Draft 0.4. The current
as-built design is Draft 0.5, which adds table and saved-query sources and
their departures from Draft 0.4.

Behaviour worth knowing:

- Chart writes record their own change-set entries, so they undo without
  clearing cell history. Structural edits and sheet rename or delete still
  clear history, as before.
- Row and column inserts and deletes move chart sources and anchors. A source
  on deleted rows, or on a deleted sheet, is marked broken and reported in the
  receipt's `broken_charts`, never repointed. An insert that pushes a chart
  past a size limit is reported in the receipt's `charts_over_limit`; the chart
  stays readable and maintained, and `charts.read` reports `over_limit`. A
  same-sheet Cut moves sources
  wholly inside the cut rectangle, but not the chart's anchor. Deleting a sheet
  deletes the charts placed on it. Duplicating a sheet does not copy its
  charts.
- Chart operations raise no Python events and are not cell changes. A bound
  change handler does not run for them.
- A direct SQLite edit that leaves a specification unreadable does not block
  opening. The chart is listed with an `issue`, and its specification is kept
  unchanged; structural edits still move its anchor.
- Undo labels are `Create chart`, `Edit chart` and `Delete chart`; an explicit
  `label` in the request wins.
- XLSX and ODS export list each omitted chart, by chart and sheet name, in the
  export report. CSV export reports nothing about charts.

## Limits

| Limit | Value | Basis |
| --- | --- | --- |
| Charts per workbook | 64 | Plan's choice |
| Series per chart | 64 | Stage 1 |
| Points per series | 10,000 | Stage 1 measurement |
| Points in a pie | 1,000 | Measured below |
| Source cells per chart | 100,000 | Measured below: resolve passes |
| Values drawn by one render | 10,000 | Measured below: added because of the result limit |
| Chart size | 64×48 to 4,096×4,096 px | Plan's choice |
| Job time and result | 30 s; 4 MiB | Existing job limits |
| Stored payload | 256 KiB | Existing catalog limit |

The points-per-series, pie and source-cell limits are checked when a
specification is given and again at resolve, each with its own code. They are
not part of the stored form: a row or column insert can push a stored chart
past one, and resolve and render then refuse it by name until it fits again.

## Measurements

All on the reference Mac only: Apple M1 (arm64), 8 GB, macOS 26.6.2, Python
3.14.6, Qt 6.12.0 on the native `cocoa` platform. Taken while the test suites
were not running. Ordinary system background work was present (load average
about 2.5 to 3.5). Nothing was measured on Windows or Linux.

### Text with the bundled font

Benchmark run with DejaVu Sans loaded in the probe: 36 labels, measured width
over laid-out width at most **0.9995** (limit 1.05), median laid-out over
measured **1.0012** (limit 1.35). So no safety multiplier was added.
Text drawn through Qt SVG (added after defect C below): one 42-character line
at 24 px is 536 px wide in DejaVu Sans against 539.8 px laid out, a ratio of
**0.993**. The same line with an unknown family draws at 0.850, so the check
can tell the fonts apart. The benchmark fails outside 0.95–1.05. Evidence:
[chart-render-experiment.json](evidence/charts-stage-2/chart-render-experiment.json),
[rendered fixtures](evidence/charts-stage-2/chart-render/).

The same run's renderer figures at 10,000 points, 800×500, are close to stage
1: Python render 24–34 ms, Qt load plus paint 29–116 ms. The pie is now drawn
at its 1,000-point limit, with render 4 ms and Qt 41 ms.

### Pie point limit

One series at 800×500, Qt load plus paint, medians of 20. The rule: the largest
size within 75 ms, half the 150 ms Qt threshold.

| Slices | Load | Paint | Total | SVG |
| --- | --- | --- | --- | --- |
| 500 | 1.7 ms | 20.7 ms | 22.4 ms | 71 KiB |
| 1,000 | 3.4 ms | 38.2 ms | **41.5 ms** | 141 KiB |
| 2,000 | 7.0 ms | 74.3 ms | 81.3 ms | 282 KiB |

`LIMIT_PIE_POINTS` stays at **1,000**, the provisional value. Three runs agreed
within 3 ms. Evidence: [pie-limit.json](evidence/charts-stage-2/pie-limit.json).

### Capacity through the service

`LocalService`, a workbook of 10,000 data rows × 11 columns: one category
column and ten value columns, five of them formulas (`=B2*2+1` on the column to
the left). Filling it took 12 edits and about 2 s. Each job ran three times,
timed end to end through the SDK (start, poll, release); size is the job result
as JSON. The figures are from a rerun after the whole-branch review fixes
(load average about 1.8 to 3.3); result sizes, points and gaps were identical
to the first run and times within 0.25 s.

| Chart | Job | Points | Wall time (median, worst) | Result |
| --- | --- | --- | --- | --- |
| Line, 8 series, 90,008 source cells, 4 of the series formulas | resolve | 80,000, no gaps | 2.01 s, 2.04 s | 0.67 MiB |
| Column, 1 series of formulas | resolve | 10,000 | 0.49 s, 0.57 s | 0.19 MiB |
| Column, 1 series of formulas | render | 10,000 | 0.57 s, 0.64 s | 2.07 MiB |
| Bar, same | render | 10,000 | 0.57 s, 0.60 s | 1.92 MiB |
| Line, same | render | 10,000 | 0.57 s, 0.58 s | 1.15 MiB |
| Area, same | render | 10,000 | 0.60 s, 0.61 s | 1.40 MiB |
| Scatter, x values and 1 series of formulas | render | 10,000 | 0.55 s, 0.67 s | 1.54 MiB |

Every job finished well inside its 30-second budget and under 4 MiB, so
`LIMIT_CELLS` stays at 100,000. The eight-series chart resolves but cannot be
rendered: its 80,000 values exceed the render limit. Evidence:
[capacity.json](evidence/charts-stage-2/capacity.json).

The 80,000 formula cells in the resolve were calculated inside the job. Larger
or slower formula sources were not measured, and nor were cross-sheet
references. The point at which a chart's sources stop calculating within 30
seconds is not established.

### Example

`PYTHONPATH=service python3 examples/charts_script.py` exits 0 and writes six
SVG files. They and their PNGs rasterised by the probe with the bundled font
are in [evidence/charts-stage-2/example/](evidence/charts-stage-2/example/). The
PNGs were inspected: the text is DejaVu Sans, legible, and inside the canvas.

## Defects found by the capacity run

**A. Formula sources plotted as error gaps.** The chart job's calculator kept
the budget of an ordinary grid read: 10,000 evaluation steps and 2 seconds per
read. Each read of a source covered the whole source. A source of more than
about 1,400 simple formulas therefore came back with the remaining cells as
`#LIMIT!` "Calculation budget exceeded", and resolve counted them as error
gaps. In the first run the 10,000-formula column resolved with 1,428 points and
8,572 error gaps, and the job reported success. This broke the rule that a
calculation which cannot finish fails the job with no partial series.

What changed:
- The chart job's calculator no longer applies the step budget. Calculation is
  bounded by the job's 30-second deadline and cancellation.
- Each source is read in slices of at most 1,000 cells, each under a read's own
  two-second deadline.
- A value or x cell that still carries the budget error fails the resolve with
  `CALCULATION_FAILED`. Other formula errors stay counted gaps. (The
  whole-branch review widened this to any `#LIMIT!` cell; see defect D.)

Tests that failed first: a job over 3,000 dependent formulas returns 3,000
points and no gaps; a budget error fails the resolve; slice counts and
boundaries. Measured afterwards: 80,000 points, no gaps, in 2.1 s (2.0 s after
the whole-branch review fixes).

**B. Render results over the 4 MiB job limit.** A render result carries the
resolved data, the SVG and one mark of about 84 bytes per drawn value. The
eight-series line chart failed with `READ_LIMIT`. The plan's remedy, lowering
`LIMIT_CELLS` to a power of ten, would have refused even a one-series
10,000-point chart with categories. The cell limit was kept for resolve, and
render gained its own limit on values drawn.

The ruling set it at 20,000, unless measurement showed some type did not fit.
Through the service at 800×500, two series of 10,000 fit for every type, but
column was at 3.99 MiB. In-process, a column or bar chart of fractional values
at 4,096×4,096 needs 4.16 MiB. So the limit is **10,000**, with a test that a
render at the limit fits the worst measured case. The job route refuses a
larger render with `LIMIT_RENDER_POINTS` before drawing. Capabilities advertise
`max_chart_render_points`. A resolve can still exceed 4 MiB, for example with
very long category labels, and fails with `READ_LIMIT`; a test pins that.

**C. The bundled font was not used through Qt SVG.** The SVG named
`font-family="DejaVu Sans, sans-serif"`. Qt SVG reads that as a single family
name, does not find it, and draws in the system font even with DejaVu Sans
loaded. The stage 2 text measurement used Qt's font metrics directly, not
through the SVG, so it did not see this. The problem showed only in the
example PNGs and in a Qt warning.

What changed:
- The SVG now names `DejaVu Sans` alone.
- The probe gained `--ink-width`. The benchmark draws one line through
  `QSvgRenderer` in the bundled family and in an unknown one, and records both
  widths against the laid-out width.
- All evidence images were regenerated.

The cost: a viewer without the font uses its default face, which may be serif,
not a sans-serif.

## Defects found by the whole-branch review

**D. A formula chain across slices plotted as error gaps.** Slicing (decision
10) gave each 1,000-cell read a new, empty calculator. The first cell of each
later slice recalculated its whole chain, hit the engine's "Dependency chain
exceeds 128 cells", and every later cell inherited the error. A running total
over A1:A2500 came back from a `completed` job with 1,000 points and 1,500
error gaps.

What changed:
- The chart job builds one calculator for the whole job and reuses it for
  every read. The snapshot is read-only and each read is state-checked, so its
  cache stays valid. Its input-bytes counter is not reset between reads.
- Any `#LIMIT!` engine error in a value or x cell now fails the resolve with
  `CALCULATION_FAILED` (409), with a message naming the cell, for example
  "A300 could not be calculated: Dependency chain exceeds 128 cells". It is
  detected by the cell's `#LIMIT!` display, not by message text. Other formula
  errors (`#DIV/0!`, `#REF!`, `#VALUE!`, `#CYCLE!`, …) stay counted gaps.

Tests that failed first: a real job over the 2,500-row chain on a reopened
workbook returns 2,500 points and no gaps; a cell whose own chain is deeper
than the engine allows fails the resolve. A `#DIV/0!` cell is still a gap.

**E. An insert could push a chart past a size limit and strand it.** The size
limits were part of stored-form readability. A pie over A1:A1000 and B1:B1000,
after inserting two rows at row 10, read as `INVALID_SPEC`: it could not be
patched, and structural edits stopped maintaining it, so its sources drifted
from the data.

What changed:
- `validate_stored` checks shape and grid bounds only. `LIMIT_POINTS`,
  `LIMIT_PIE_POINTS` and `LIMIT_CELLS` are enforced by `normalise_spec` on
  input, as before, and by resolve before any read, with a message that gives
  the cell count the source now holds.
- Such a chart has no `issue`, is still maintained by structural edits, and
  accepts `spec` and `spec_patch`, which re-validate the limits.
- `charts.read` reports `"over_limit"`, the limit code or `null`, for each
  chart. A row or column receipt lists `charts_over_limit` (sorted ids) when an
  edit pushes a chart from within its limits to over them.

**F. XLSX and ODS export dropped charts with only a generic note.** The export
report now lists each chart as omitted ("Charts are native-only and omitted
from this export: Sales on Sheet1, …"), as it does for names and bindings, for
whole-workbook, selection and streamed exports. CSV export reports nothing new.

**G. Smaller defects.**
- `Workbook.chart_jobs()` is created under the book lock. Two concurrent first
  requests could each create one, and jobs on the lost instance answered
  `JOB_NOT_FOUND` and were never closed.
- Undo labels are `Create chart`, `Edit chart` and `Delete chart` instead of
  the engine default (`Chart_Update`).
- The SDK reported every failed chart job as 409. A chart job's error now
  carries `http_status`, and the SDK raises with it, so `LIMIT_RENDER_POINTS`
  reaches callers as 400.
- A whole-workbook import with `replace_empty` into a workbook whose only
  content was a chart deleted the chart unreported. Charts now count as
  content, and the import is refused with `DESTINATION_NOT_EMPTY`.
- Chart tests and the benchmark read JSON with an explicit UTF-8 encoding.

## Defects found by the pull request review

The owner's review of the pull request made five findings. Each was
reproduced by a test that failed first.

- **`charts render --svg-out` could overwrite the workbook.** Naming the open
  workbook, or a symbolic or hard link to it, replaced the SQLite file with
  SVG and reported success. The destination is now checked before the render
  starts: the workbook and its aliases are refused, another existing file is
  replaced only with `--overwrite`, and the SVG is written through a temporary
  sibling file. The workbook's SQLite sidecar files are not checked by name.
- **The same chart identity could sit on two sheets.** The catalog's primary
  key permits it after a direct SQLite edit, while update, delete, resolve and
  render look a chart up by identity alone, so a delete could remove the copy
  and leave the original. Opening and refreshing now reject a repeated chart
  identity as `INVALID_FORMAT`.
- **A timed-out chart job kept its slot.** The SDK cancelled and released at
  once, but a job that is still stopping cannot be released, so four timeouts
  could fill the four job slots for five minutes. The SDK now waits, for up to
  about two seconds, until the cancelled job has stopped before releasing it.
- **A suggestion could be refused by create.** Suggesting a pie over 1,001
  rows succeeded and the returned specification then failed with
  `LIMIT_PIE_POINTS`. Suggest now runs the same validation as create and
  reports the limit itself.
- **Scatter counts ignored the x coordinate.** A point was counted when its
  value was a number even if its x cell was text, an error or empty, so resolve
  reported more points than render drew. A scatter point is now counted only
  when both coordinates are usable; otherwise it is a gap, classified by the
  value cell first and then by the x cell. An x source shorter than the value
  source leaves the remaining values as `empty` gaps.

## Decisions the controller made

Made while the plan was executed. Each is open to reversal.

1. **Branch in place rather than a worktree,** because the checkout's `build/`
   directory holds the Qt probe and CTest build. Cost: the main checkout stays
   on this branch.
2. **One owner for the pie limit.** `LIMIT_PIE_POINTS` is defined in
   `chart_render.py` beside `LIMIT_POINTS`, `render` refuses a larger pie, and
   `charts.py` imports it. Cost if wrong: one constant moves.
3. **One read per source** (reversed by decision 10). A source was at most the
   size one read admits, so slicing seemed unnecessary. Cost then judged: none.
4. **A patch to a chart with a broken source is refused** with `SOURCE_BROKEN`;
   send `spec` instead. The stored specification cannot be turned back into
   input form without coordinates. Cost if wrong: a user resends the whole
   specification to repair one source.
5. **A missing or non-text `chart_id` is `INVALID_ARGUMENT`;** an unknown one
   stays `CHART_NOT_FOUND`. This matches how other malformed operation
   arguments are treated. Cost if wrong: one error code changes.
6. **A source on a deleted sheet** is stored with `sheet_id` and all four
   coordinates `null`, as broken workbook names are. Cost if wrong: none
   visible, since it reads as broken either way.
7. **A same-sheet Cut does not move a chart's anchor;** the design names
   sources only. Cost if wrong: a chart stays put when the cells under it are
   cut.
8. **Starting a job checks that the chart exists** and refuses an unknown one
   at once. The job then reads the chart from its own snapshot. Cost if wrong:
   none.
9. **The chart job's calculator drops the per-read step budget** and is bounded
   by the job's deadline and cancellation. A budget error fails the resolve.
   Cost if wrong: a pathological formula can run until the 30-second job
   deadline instead of two seconds.
10. **Sources are read in slices of at most 1,000 cells,** reversing decision 3,
    because each read has a fixed two-second deadline. Cost if wrong: up to ten
    times as many read transactions per source; the state check across reads
    still guards consistency.
11. **A render limit instead of a lower cell limit.** `LIMIT_CELLS` stays at
    100,000, and a render draws a bounded number of values. The ruling set 20,000
    subject to measurement; measurement set 10,000 (defect B). Cost if wrong:
    some renders that would fit are refused.
12. **The SVG names one font family** with no fallback list. Cost if wrong: a
    browser without the font uses its default face, often a serif.
13. **One calculator per chart job, and any `#LIMIT!` fails the resolve**
    (defect D). Slicing gave each slice an empty calculator cache. A `#LIMIT!`
    means the engine could not compute the value, which is "cannot finish", not
    an ordinary formula error. Cost if wrong: a chart over a cell that
    permanently shows `#LIMIT!` in the grid fails outright instead of drawing
    with a gap.
14. **Size limits are not stored-form readability** (defect E).
    `validate_stored` checks shape and grid bounds; `normalise_spec` enforces
    the limits on input; resolve enforces them with their own codes; the
    structural-edit receipt lists such charts under `charts_over_limit`. Cost if
    wrong: a stored chart can exceed a limit and exist until it is resolved,
    where it is refused by name.
15. **Export lists each omitted chart, in this branch** (defect F). The design
    names an unreported dropped chart as an anti-goal, and stage 2 is what
    makes charts creatable; no plan task owned it. Cost if wrong: a few lines
    outside the plan's stated scope.
16. **Smaller review findings fixed now, graded by effect** (defect G): the
    chart jobs accessor under the book lock, the undo labels, the SDK's job
    error status, charts counted as content by a `replace_empty` import, the
    UTF-8 reads, and false or imprecise sentences in
    [implementation-status.md](implementation-status.md) and
    [native-format-v1.md](native-format-v1.md).

## Known and left as they are

Found in review and judged minor. None changes a documented result.

**Renderer and font tooling**
- The renderer's own pie check counts values only, not categories. Validation
  in `charts.py` checks both, so a stored chart cannot reach it.
- Series name text and axis titles have no length cap or control-character
  rule. The chart title is capped at 255 characters, and the payload at 256 KiB.
- `tools/font_metrics.py` does not bound a format 12 cmap group at U+10FFFF. Its
  CLI hard-codes the family name `DejaVu Sans`. The shared-last-advance
  test does not use a distinct trailing advance.

**Validation, storage and undo**
- The change set records the in-memory payload, while undo compares a JSON
  round trip. A value that is not JSON-identical would cause a false
  `HISTORY_CONFLICT`.
- A chart counts as one cell toward an operation's 10,000-cell budget.
- `spec_patch` does not re-apply defaults. Changing a line chart to a column
  chart by patch needs `grouping` in the patch.
- `describe` runs one query per source. `_stored_source` has an unused `book`
  parameter. The delete path reads `chart_id` from the request twice.
- Some refusals in stored mode, odd `spec_version` types and a multi-cell name
  source have no test. Nor does refresh, as opposed to reopen, with a chart
  that has an issue.

**Following structural edits**
- Moving sources with a Cut relies on moves being same-sheet.
- There is no test for a chart on one sheet whose source is on the edited
  sheet; the code decides by the source's sheet, which is correct. A few test
  idioms are untidy.

**Resolve and suggest**
- `chart_resolve` calls a private `charts` helper and queries `sheets` directly.
  `_holds_number` repeats a calculation check.
- Progress counts cells requested, not cells returned.
- A scatter suggestion drops a leading text column.
- "A non-finite number is a text gap" has no test.

**Jobs and REST**
- A job that fails because the chart was deleted, or its source broken, between
  start and snapshot is reported `failed` at the old state rather than `stale`.
  The failed-then-stale branch has no test.
- The lazy calculation jobs accessor is still not created under the book lock.
- The OpenAPI schemas for `chart_update` and `chart_delete` omit the accepted
  `sheet` envelope field. `ChartSuggestRequest` allows unknown keys. The
  release schema allows 64-character job ids while chart jobs allow 100.
- `max_chart_render_points` is written as a literal in `service.py` rather than
  read from the renderer's constant.

**SDK and CLI**
- `charts create` with a `sheet` key or an unknown key in its standard input
  raises an unstructured Python error, as `names save` does.
- `charts list` prints `{state, charts}`, while the SDK returns the list.
- A failed job without an error body gets a synthesised `CHART_JOB_<STATUS>`
  error.
- No CLI test covers update with a bad id or suggest without its arguments.

**Deferred from the whole-branch review**
- The `chart_create` and `chart_update` OpenAPI `spec` is a bare object rather
  than a `$ref` to `ChartSpec`.
- Capabilities do not advertise the per-series, pie or cell limits.
- Repeated `order: "front"` grows `z` without bound until the ±1,000,000
  envelope limit.
- The "two-second deadline" wording.
- No committed evidence for the 3.99 MiB figure in defect B.
- `capacity.json` does not record the eight-series render refusal.
- A chart whose source starts in the middle of a dependency chain longer than
  128 cells fails with `CALCULATION_FAILED`. The grid shows `#LIMIT!` for the
  same cell: a pre-existing engine limit.

## Table and saved-query sources

November 13, 2026. The [table and saved-query sources plan](charts-table-query-sources-plan.md)
is executed on branch `claude/charts-table-query-sources`, cut from `main`
after the range-source work. Nothing in it is merged or released. A chart
series, category list or x list can now come from a column of a structured
table or a column of a saved query's result, with the resolve, render, undo
and interface behaviour charts already had over ranges. The as-built design is
[charts-design.md](charts-design.md) Draft 0.5 (sections 4, 5, 8 and 16), and
the stored forms are in [Charts format v8](native-format-v1.md#charts-format-v8).

### What was built

| Part | Where | What it does |
| --- | --- | --- |
| Source kinds | `charts.py` | Input and stored forms of `table` and `query` sources in specification version 1; `source_kind`, `is_broken`; current names and `broken` on read; `break_table` and `break_query` |
| Breaking on drop | `relational.py`, `table_schema.py`, `query_views.py` | `table_drop`, a `table_schema` that removes a column, and `query_delete` break the chart sources that used them and list the charts in `broken_charts` |
| Reading sources | `chart_sources.py` (new) | Reads a table column in stored row order from the job's snapshot, turns a query result column into cells, and applies the value, display and row-limit rules |
| A saved-query run | `query_jobs.py`, `jobs.py` | `QueryJobs.run_once`: one synchronous run on a private snapshot, outside the job store, with an absolute `deadline` and a 16 MiB in-memory cap (`run_once_bytes`). The preparation job's checkpoint exposes `deadline` |
| Resolve | `chart_resolve.py`, `chart_jobs.py` | Dispatch by kind, one run per saved query per job, a running `LIMIT_CELLS` total, and suggest from a table. The chart job passes a query runner bounded by its own deadline |
| Interfaces | `charts_api.py`, `client.py`, `cli.py` | `ChartSource` as three input forms in OpenAPI; suggest from a table through REST, `suggest_chart(table=...)` and `charts suggest --table` |
| Example and benchmark | `examples/charts_script.py`, `tests/chart_render_benchmark.py` | The example also charts a table and a saved query; the benchmark's `--sources` run measures them through the service |

Behaviour worth knowing:

- Tables, columns and saved queries are held by identity, so renaming any of
  them needs no chart maintenance. A query's result column is held by its
  name, because result columns have no identity.
- A source is marked broken when its table is dropped, its column is removed
  by a schema change, or its saved query is deleted. It is never repointed: a
  new table or query of the same name does not repair it. Renaming or retyping
  a column breaks nothing; a retyped column resolves as gaps.
- A saved query saved again without the chart's column is not rewritten in
  storage. The chart reads as `broken` and is refused at resolve with
  `SOURCE_BROKEN`, naming the column; it never plots another column or an old
  result.
- Each saved query a chart reads runs once per chart job, on a private
  snapshot copy, bounded by the chart job's 30-second deadline and its
  cancellation, and by a 16 MiB in-memory cap on the result. It takes no query
  job slot, so a chart and a person's own query job do not block each other.
- Table values: integer and number columns are points; text, boolean and
  temporal values are `text` gaps; `NULL` is an `empty` gap. Query values:
  numbers are points, an integer beyond 2^53 plots as the nearest float, text
  and `NULL` are gaps, and a BLOB fails the job with `QUERY_VALUE`. Category
  text shows numbers in the tick format, booleans as `TRUE` and `FALSE`, and
  dates as ISO text.
- A table or result with more rows than a series admits is refused at resolve
  with `LIMIT_POINTS` (`LIMIT_PIE_POINTS` for a pie), with a message naming the
  table or query. Nothing is truncated. Chart listings report no `over_limit`
  for these kinds, because knowing the size means reading the data.
- Suggest works from a table's schema without reading rows. There is no
  suggestion from a saved query.
- A series name stays a range or fixed text.

### A limit the earlier stage 2 documents omit

The engine caps the cell source text one calculation may read at 8 MiB. The
chart job uses one calculator for the whole job (decision 13 above), and that
counter is not reset between reads, so the cap applies to a whole chart job,
not to each slice. A chart whose formula sources reach more than 8 MiB of cell
source text fails with `CALCULATION_FAILED`. The capacity run's 80,000 short
formulas are well below it. Table and saved-query sources do not use the
calculator and do not count toward it.

### Measurements

On the reference Mac only: arm64, macOS 26.6.2, Python 3.14.6, as recorded in
the evidence file. This is one Mac; nothing was measured on Windows or Linux.
Observed during the run but not recorded in the evidence file: the hardware
(Apple M1, 8 GB), Qt 6.12.0 installed but not used by these measurements, no
test suite running, a load average of about 1.4 to 1.9, and 19 GiB of free disk
space before and after the run.

`PYTHONPATH=service python3 tests/chart_render_benchmark.py --sources --work build/unused --evidence docs/design/evidence/charts-stage-2`
creates the workbook in a temporary directory that is removed when the run
ends (`--work` is not used). Through `LocalService`: a table `Sales` of
10,000 rows with one text column (`Region`, 100 distinct values) and four
number columns, filled by 10 inserts of 1,000 rows in 0.46 s, and a saved
query `Totals`, `SELECT Region, SUM(A) AS Total FROM Sales GROUP BY Region
ORDER BY Region`, returning 100 rows. Each job ran three times, timed end to
end through the SDK (start, poll, release); size is the job result as JSON;
charts are 800×500.

| Chart | Job | Points | Wall time (median, worst) | Result |
| --- | --- | --- | --- | --- |
| Line, 1 table series, table categories | resolve | 10,000 | 0.12 s, 0.14 s | 218 KiB |
| Line, same | render | 10,000 | 0.18 s, 0.19 s | 1.18 MiB |
| Line, 4 table series | resolve | 40,000 | 0.15 s, 0.19 s | 459 KiB |
| Column, query categories and values | resolve | 100 | 0.06 s, 0.06 s | 2.8 KiB |
| Column, same | render | 100 | 0.06 s, 0.06 s | 23.7 KiB |

The four-series chart was not rendered: its 40,000 values exceed the render
limit of 10,000.

**The private snapshot copy.** The same workbook was then padded with 200,000
number cells on `Sheet1` (20 edits of 10,000 cells, 4.0 s), which took the
workbook file from 729,088 bytes (0.70 MiB) to 13,926,400 bytes (13.3 MiB).

| Chart | Job | Unpadded (median, worst) | Padded (median, worst) |
| --- | --- | --- | --- |
| Column over the saved query | resolve | 0.06 s, 0.06 s | 0.17 s, 0.17 s |
| Column over the saved query | render | 0.06 s, 0.06 s | 0.17 s, 0.17 s |
| Line, 1 table series (runs no query) | resolve | 0.12 s, 0.14 s | 0.13 s, 0.18 s |

An ordinary query job over the same saved query, run once at each size,
reports its own snapshot copy: 737,280 bytes in 1.9 ms unpadded and
13,926,400 bytes in 22.4 ms padded, with execution 4.1 and 4.8 ms. So the copy
grows with the workbook, about 1.6 ms per MiB here, and a table chart, which
copies nothing, barely changes. The query chart's end-to-end time grew by
0.11 s, more than the copy alone. The SDK polls a job every 50 ms, so times
under about 0.2 s move in steps of roughly that size and cannot be split
finely; a probe run outside the benchmark, timing `run_once` in-process on the
same two workbooks, took 4 ms and 17 ms (median of five); it was observed
during the run but is not recorded in the evidence file. In an earlier run of the same benchmark a few minutes
before, the one-series table resolve took 0.06 s rather than 0.12 s; the other
figures agreed within about 0.05 s.

Every job finished far inside its 30-second budget and under 4 MiB, so no
limit changed. Larger workbooks were not measured. At the rate above the copy
of a 1 GiB workbook would take about 1.7 s on this machine, and the copy's own
5-second limit would be reached near 3 GiB; both are extrapolations, not
measurements. Evidence:
[sources.json](evidence/charts-stage-2/sources.json).

The example, `PYTHONPATH=service python3 examples/charts_script.py`, exits 0;
its table chart (24 points) and saved-query chart (5 points) are each created,
rendered to `table.svg` and `query.svg`, and deleted.

### Defects found by the whole-branch review

Each code defect was fixed test-first.

- **A chart over a saved query with a parameter that has no saved default could
  be created but never resolved.** The query engine needs an explicit value
  for every declared parameter, and a chart cannot supply one, so every resolve
  failed with `INVALID_PARAMETERS`: "Each parameter needs a unique name,
  declared type and explicit value", naming neither the query nor the
  parameter. Such a source is now refused on input with `INVALID_SPEC`, naming
  the query and the parameter. At resolve, every error a saved-query run raises
  keeps its code, status and details and is prefixed with `Saved query <name>: `,
  which covers a query saved again later without a default. `STALE_STATE` and
  `CANCELLED` are not prefixed; the row-limit refusal already named the query.
- **Stale draft references.** Three lines, in the documents index, this
  document's range-source section and the implementation status, still named
  Draft 0.4 as the as-built design. They now say Draft 0.4 was the as-built
  draft on November 12 and Draft 0.5 is the current one.
- **Hook ordering.** A `table_schema` that removed a column broke chart sources
  after its after-structure hook, while `table_drop` and `query_delete` break
  them before theirs, so an after handler on a schema change saw the sources
  not yet broken. All three now break before the after-structure hook.
- **OpenAPI.** The `Receipt` schema did not list `broken_charts`; it does now.
  The `ChartSpec` series `name` source referred to the three-kind
  `ChartSource`; the range form is now its own schema, `ChartRangeSource`,
  which `ChartSource` references and the name source references alone. The
  `spec_patch` description now says how to change a source's kind.

### Decisions the controller made

Made while this block's plan was executed. Each is open to reversal.

1. **Branch in place rather than a worktree,** because the checkout's `build/`
   directory holds the CTest build and the Qt probe. Cost: the main checkout
   stays on this branch.
2. **A table read uses the workbook's own read cancellation event,** the one
   an ordinary read passes to its SQL deadline, which is what the chart job's
   snapshot is opened with. The plan left the event unspecified. Cost if
   wrong: none.
3. **A saved-query run for a chart was given a fixed 20 seconds** (reversed by
   decision 5). The plan asked for "the time left in the job, at most 20", but
   the job's checkpoint exposed no time left. The cost then judged: a job could
   overrun its 30 seconds by up to the query's remaining time before failing
   with `TIMEOUT`. Review showed that overrun could reach about 25 seconds for
   a run started late in the job, which breaks the rule that a chart job stays
   inside its 30-second budget.
4. **A one-off saved-query run holds at most 16 MiB in memory,** and its row
   bound is clamped to the query jobs' own row limit. The plan bounded it by
   rows alone; an automated security check found that a row can carry up to
   256 columns of 64 KiB text, so 10,001 rows could be gigabytes. Past 16 MiB
   the run fails with `QUERY_LIMIT`. Cost if wrong: a chart over a query
   returning more than 16 MiB is refused, although a query job would spool the
   same result to disk.
5. **The chart job's deadline bounds its saved-query runs,** reversing
   decision 3. The preparation job's checkpoint now exposes its absolute
   `deadline`, `QueryJobs.run_once` takes an optional absolute `deadline` that
   bounds both the snapshot copy and execution, and the chart job passes it,
   with 20 seconds kept as the run's own ceiling. Cost if wrong: one additive
   attribute on the preparation-job checkpoint and one optional parameter on
   `run_once`.
6. **A saved query needs a default for every parameter to be charted.** A chart
   over a saved query with a parameter that has no saved default was accepted
   but could never resolve. It is refused on input with `INVALID_SPEC` naming
   the query and the parameter, and every error a query run raises at resolve
   names the saved query, with its code unchanged; a chart has no way to supply
   parameter values. Cost if wrong: a query with a parameter must be given a
   default before it can be charted.
7. **Mixed source kinds get a documentation note, not a refusal.** Table
   categories come in stored row order and query values in the query's order,
   so mixing them can misalign labels. The design allows mixed kinds, ranges
   already behave this way, and there is no join key to check. Cost if wrong: a
   person can build a chart whose labels do not match its bars and is told only
   by the documents.
8. **Fixed now, re-graded by effect or cheapness:** the three document lines
   naming Draft 0.4 as the as-built design; `break_table` called before the
   after-structure hook in `table_schema`, as in `table_drop` and
   `query_delete`; `broken_charts` in the OpenAPI `Receipt`; a range-only
   series name source in the OpenAPI `ChartSpec`; how to change a source's kind
   with `spec_patch`, in the design and OpenAPI; and the measurement figures
   not in the evidence file labelled as such. Cost if wrong: none beyond the
   change itself.

Two questions the controller settled by checking the code rather than by a
ruling: a stored table or query source used as a series name is
`INVALID_SPEC`, as is a table source with only one of its two identities; and
temporal table values are stored as canonical ISO text, so time and datetime
categories display as ISO text, as dates do.

### Known and left as they are

Found in review and judged minor. None changes a documented result.

**Specification and following**
- Reading a chart's description looks up the table or saved query once per
  source rather than once per chart.
- Tests: no query-source counterpart to the test that a workbook without a
  relational catalog describes cleanly; no test of one chart over two tables
  where one is dropped; the refused-drop test for a table source sits in the
  query source test file.

**Reading tables and running queries**
- A `TIMEOUT` from the refresh before a table read's transaction escapes as
  `TIMEOUT`, not `CALCULATION_FAILED`, and no test drives a table read's
  timeout through to `CALCULATION_FAILED`.
- `LIMIT_CELLS` is checked after each source is read, so the running total can
  overshoot by at most one read of 10,001 rows before the refusal. That
  refusal reads "more than 100000 cells", without the thousands separator the
  other limit messages use.
- The refusal of a result over 10,001 rows is recognised by the query engine's
  exact message text; a test pins it, and a drift would show `QUERY_LIMIT`
  instead of `LIMIT_POINTS`.
- A chart with a query source resolved without a query runner is refused with
  `INVALID_ARGUMENT` before its broken-source checks. Only internal callers can
  do this.
- The pre-check that a run has not already passed its deadline uses `<` where
  the run's own checkpoint uses `<=`.

**The one-off query run and shutdown**
- Stopping is not checked again after `run_once` takes the workbook lock, so a
  run blocked there past `close()`'s two-second wait would surface a raw SQLite
  error. An ordinary query job start has the same exposure.
- `close()` can take up to two seconds longer for each run in flight.
- A cancelled run still waiting for the workbook lock reports `CANCELLED` with
  status 400, and one cancelled during execution with 409; this is the
  existing behaviour of the lock helper. No test pins the status when `close()`
  lands during execution.
- Tests: removal of the run's temporary directory is asserted only for success
  and `QUERY_LIMIT`; there is no test of `REPAIR_SCOPE`; one message assertion
  only checks that the text contains "2".

**Jobs and interfaces**
- A render job's progress drops from the cell count to the point count at its
  "Rendering" step. This predates the block.
- The SDK's `suggest_chart(sheet=..., table=...)` ignores the sheet, where REST
  and the CLI refuse the combination.
- The OpenAPI `ChartSpec` series `name` referred to the three-kind
  `ChartSource`, though only a range is accepted there. Fixed after the
  whole-branch review: it now refers to `ChartRangeSource`.
- Tests: `LIMIT_SERIES` for a table suggestion is tested at 65 data columns,
  not at the boundary of 64; one refusal test repeats a call.
- Two `ResourceWarning`s under `python3 -X dev` predate this block, and an
  unclosed-database `ResourceWarning` from `chart_resolve` when the chart test
  modules run together is untraced.

**Found by the whole-branch review and deferred**
- Suggest from a table reads no rows, so it can propose a chart that cannot
  resolve or render. That is reported at resolve, and the `LIMIT_CELLS` and
  `LIMIT_RENDER_POINTS` messages name no source.
- `run_once` waits for the live workbook lock without a deadline or a
  cancellation check.
- A lowered `QueryJobs.max_rows` would surface `QUERY_LIMIT` instead of
  `LIMIT_POINTS`.
- When categories, values and x differ in length, the renderer pads them, and
  the padding is not counted as gaps.
- Query result columns match case-sensitively, while table columns match
  ignoring case.
- One chart job runs at a time per workbook, so a slow saved query delays
  other charts.

**Commit trailers**
- The co-author trailers on this branch's commits do not all name the same
  model. They are left as they are.

### Still open after this block

- The desktop (stage 3), and workbook-Python chart operations.
- A suggestion from a saved query, and any filtered or ordered table source
  other than through a saved query.
- A measured ceiling for the private snapshot copy on a large workbook, and
  for formula sources at the edge of the job budget.
- Windows and Linux.

## What is open

- **Table and query sources,** the second step of stage 2, with their own plan.
  Built on November 13, 2026; see [Table and saved-query sources](#table-and-saved-query-sources).
- **Windows and Linux.** Font loading, text layout and Qt timings were measured
  on one Mac only.
- **The desktop** (stage 3): wireframes, Insert Chart, floating charts and the
  edit panel. The desktop must load `fonts/DejaVuSans.ttf` and link Qt SVG.
- **Workbook-Python chart operations** (stage 3).
- **XLSX and ODS chart interchange** (stage 4). Export already reports each
  omitted chart.
- **Formula sources at the edge of the job budget.** The largest or slowest
  source that still calculates within 30 seconds is not measured.
