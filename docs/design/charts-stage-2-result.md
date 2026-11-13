# Charts stage 2: result (range sources)

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
[Defects found by the capacity run](#defects-found-by-the-capacity-run).

Table and query sources, the desktop, workbook-Python chart operations,
interchange, and any measurement on Windows or Linux are not done.

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

The format is in [Charts format v8](native-format-v1.md#charts-format-v8). The
as-built design, with its departures from Draft 0.3, is in
[charts-design.md](charts-design.md) (Draft 0.4).

Behaviour worth knowing:

- Chart writes record their own change-set entries, so they undo without
  clearing cell history. Structural edits and sheet rename or delete still
  clear history, as before.
- Row and column inserts and deletes move chart sources and anchors. A source
  on deleted rows, or on a deleted sheet, is marked broken and reported in the
  receipt's `broken_charts`, never repointed. A same-sheet Cut moves sources
  wholly inside the cut rectangle, but not the chart's anchor. Deleting a sheet
  deletes the charts placed on it. Duplicating a sheet does not copy its
  charts.
- Chart operations raise no Python events and are not cell changes. A bound
  change handler does not run for them.
- A direct SQLite edit that leaves a specification unreadable does not block
  opening. The chart is listed with an `issue` and kept unchanged.

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
as JSON.

| Chart | Job | Points | Wall time (median, worst) | Result |
| --- | --- | --- | --- | --- |
| Line, 8 series, 90,008 source cells, 4 of the series formulas | resolve | 80,000, no gaps | 2.06 s, 2.07 s | 0.67 MiB |
| Column, 1 series of formulas | resolve | 10,000 | 0.51 s, 0.56 s | 0.19 MiB |
| Column, 1 series of formulas | render | 10,000 | 0.62 s, 0.66 s | 2.07 MiB |
| Bar, same | render | 10,000 | 0.62 s, 0.82 s | 1.92 MiB |
| Line, same | render | 10,000 | 0.55 s, 0.59 s | 1.15 MiB |
| Area, same | render | 10,000 | 0.64 s, 0.65 s | 1.40 MiB |
| Scatter, x values and 1 series of formulas | render | 10,000 | 0.63 s, 0.66 s | 1.54 MiB |

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
  `CALCULATION_FAILED`. Other formula errors stay counted gaps.

Tests that failed first: a job over 3,000 dependent formulas returns 3,000
points and no gaps; a budget error fails the resolve; slice counts and
boundaries. Measured afterwards: 80,000 points, no gaps, in 2.1 s.

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

## Known and left as they are

Found in review and judged minor. None changes a documented result.

**Renderer and font tooling**
- The renderer's own pie check counts values only, not categories. Validation
  in `charts.py` checks both, so a stored chart cannot reach it.
- Series name text and axis titles have no length cap or control-character
  rule. The chart title is capped at 255 characters, and the payload at 256 KiB.
- `tools/font_metrics.py` does not bound a format 12 cmap group at U+10FFFF. Its
  CLI hard-codes the family name `DejaVu Sans`. A font test reads text without
  an explicit UTF-8 encoding, which matters on Windows. The shared-last-advance
  test does not use a distinct trailing advance.

**Validation, storage and undo**
- The change set records the in-memory payload, while undo compares a JSON
  round trip. A value that is not JSON-identical would cause a false
  `HISTORY_CONFLICT`.
- Opening does not require chart ids to be unique across sheets, and lookups
  are by id alone. A direct SQLite edit could create duplicates.
- A chart counts as one cell toward an operation's 10,000-cell budget.
- Undo labels are the engine default, such as "Chart_Update".
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
- The lazy chart job accessor is not created under the book lock, the same as
  calculation jobs.
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

## What is open

- **Table and query sources,** the second step of stage 2, with their own plan.
- **Windows and Linux.** Font loading, text layout and Qt timings were measured
  on one Mac only.
- **The desktop** (stage 3): wireframes, Insert Chart, floating charts and the
  edit panel. The desktop must load `fonts/DejaVuSans.ttf` and link Qt SVG.
- **Workbook-Python chart operations** (stage 3).
- **Export reports** that list native charts one by one (stage 4 or earlier),
  and XLSX and ODS chart interchange (stage 4).
- **Formula sources at the edge of the job budget.** The largest or slowest
  source that still calculates within 30 seconds is not measured.
