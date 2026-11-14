# Charts design

Draft 0.5 | November 13, 2026 | Status: approved by the owner on November 11, 2026; rendering route confirmed with two changes on November 12, 2026; table and saved-query sources decided on November 13, 2026. Stage 1 (the experiment) and stage 2 are built: range sources on branch `claude/charts-stage-2`, and table and saved-query sources on branch `claude/charts-table-query-sources`, which is not merged. The desktop and workbook Python are not built. Sections marked **Built** record what the code does; [section 15](#15-departures-from-draft-03-as-built) lists where it departs from Draft 0.3, and [section 16](#16-departures-from-draft-04-as-built) where table and saved-query sources depart from Draft 0.4.

This document proposes how DataHorizon stores, renders and exposes charts. It is
a design to plan from. It is not an implementation plan; each stage gets a
task-level plan when it starts, and the desktop stage gets wireframes first.

Each decision is marked **Owner** (stated by the owner) or **Call** (chosen by
the author and open to reversal).

## 1. Intent

A person who has data in a sheet, table or saved query should be able to select
it, insert a chart, and have that chart stay with the workbook and follow the
data as it changes. The same chart should be readable and changeable by a
script, the CLI or an AI agent through REST, without a desktop running.

The owner asked on November 11, 2026 whether Plotly, from the Python world,
should do the charting. The answer adopted here is no for the core feature:

- A chart is a native workbook object described by a declarative specification,
  not a figure produced by a plotting library. **Owner** (accepted as proposed)
- Plotly is not the core engine. **Owner** (accepted as proposed)

Plotly was set aside for three reasons. It renders through plotly.js, so the Qt
Widgets desktop would have to embed QtWebEngine, and its static export needs a
Chrome runtime. An arbitrary Plotly figure has no faithful mapping to an Excel
or OpenDocument chart. And the service ships with no third-party Python
packages ([LICENSES.md](../../LICENSES.md)), which Plotly would end.

## 2. Goals, non-goals and anti-goals

**Goals**

- A chart is created from a selection in the desktop and from REST, the CLI and
  the SDK, with the same result.
- A chart is stored in the native workbook, survives save and reopen, and is
  visible to direct SQLite readers as documented metadata.
- A chart draws from a sheet range, a structured table or a saved query.
- A chart follows its data: managed edits and recalculation mark it stale and it
  redraws; row and column inserts, deletes and moves keep its ranges pointing at
  the same cells.
- A chart renders without a desktop, so a script or agent can fetch an image.
- Chart create, change, move and delete are managed operations with history and
  undo.
- The specification maps onto the common Excel and OpenDocument chart model, so
  later interchange is a mapping and not a redesign.

**Non-goals for the first stages**

- XLSX and ODS chart import and export. The Excel fidelity programme lists
  charts as a non-goal ([section 2](excel-fidelity-plan.md)); this design keeps
  them out until stage 4. Until then charts are reported as omitted on export,
  as they already are on import.
- Pivot charts, sparklines, 3D charts, maps, stock, radar, surface, treemap,
  sunburst and waterfall charts.
- Zoom, pan, brushing and other exploratory interaction.
- Charts authored as Plotly or Matplotlib figures in workbook Python. Package
  installation is a non-goal of the Python workspace, so user code cannot import
  them today.
- Excel-parity formatting dialogs. **Call**: basic authoring only, as in the
  Excel fidelity programme.
- Locale-specific axis and label formatting. Display stays invariant.

**Anti-goals: these would make the feature a failure even if it works**

- A chart whose data, layout or marks differ between the desktop and the image
  REST returns. Glyph shapes may differ where a viewer substitutes a font; see
  section 6.
- A chart that silently shows old data after a managed edit.
- An export that drops a chart without reporting it.
- A chart that reads an unbounded range into memory.
- A new runtime dependency that the installed service or desktop cannot ship on
  all three platforms under GPL-3.0-only.

## 3. Decisions

| Decision | Choice | Mark |
| --- | --- | --- |
| Core model | Native chart object with a declarative, versioned specification | **Owner** |
| Plotly | Not the core engine | **Owner** |
| Who resolves data | The service: it reads the sources, applies bounds and returns resolved series | **Call** |
| Who draws | The service draws SVG; the desktop displays that SVG. See section 6 | **Owner** (November 11, 2026), subject to the stage 1 experiment |
| Storage | A chart kind in the existing `dh_objects` catalog, at native format v8 | **Call**; **Built** |
| Source kinds | `range` first; `table` and `query` in a later step of stage 2 | **Owner** (accepted as proposed); **Built** |
| Table source | The whole table in stored row order, with no saved view; filtering and ordering through a saved query | **Owner** (November 13, 2026); **Built** |
| Uncalculated sources | Resolve and render are cancellable jobs that calculate their sources inside their own read-only snapshot | **Call**; **Built** |
| Staleness | Coarse: any managed revision change marks every chart stale | **Call** |
| Placement | Floating over a sheet, anchored to a cell, with a size in pixels. Chart sheets later | **Call** |
| Hooks | No chart events in the first stages; chart operations do not raise cell change events | **Owner** (accepted as proposed) |
| First chart types | Column, bar, line, area, pie, scatter; clustered and stacked where they apply | **Owner** (accepted as proposed) |
| Timing | Stage 1 starts now, in parallel with the Excel fidelity programme | **Owner** (accepted as proposed) |

## 4. The chart object and its specification

A chart has an identity, an owning sheet, a placement and a specification. The
identity is stable and is not the display name or the z-order, in line with the
rule that identity, display order, stored inputs and derived values stay
distinct.

The specification is JSON with a `spec_version`. It holds only inputs. Resolved
values, tick positions and rendered output are derived and are never stored in
it. An illustration, not a fixed contract:

```json
{
  "spec_version": 1,
  "type": "column",
  "grouping": "clustered",
  "title": {"text": "Units by month"},
  "categories": {"source": {"kind": "range", "sheet": "sheet-id", "ref": "A2:A13"}},
  "series": [
    {"name": {"source": {"kind": "range", "sheet": "sheet-id", "ref": "B1"}},
     "values": {"source": {"kind": "range", "sheet": "sheet-id", "ref": "B2:B13"}}}
  ],
  "axes": {"category": {"title": null}, "value": {"title": null, "min": null, "max": null}},
  "legend": {"position": "right"}
}
```

A source is one of three kinds:

- `range`: a rectangle on a sheet, by sheet identity.
- `table`: a column of a structured table, by table and column identity, read
  whole in stored row order. **Owner** (November 13, 2026). Draft 0.4 read it
  through a saved view that supplied filter and order; the code has no saved
  view over a table, and what the project calls saved views are saved queries.
  A chart that needs a filter or an order reads a saved query instead.
- `query`: a column of a saved query's result, by saved-definition identity and
  column name. Resolving it runs the query as a snapshot job with its saved
  parameter values; it does not reuse an earlier result.

**Call**: `range` sources are built first. `table` and `query` sources follow
as a second step of stage 2, because they depend on the saved-view and snapshot
job paths and a table without a view is refused once it passes the point limit.
**Owner** (November 13, 2026): table and saved-query sources come next, before
the desktop.

The specification deliberately follows the shape Excel and OpenDocument share
(a chart type, a grouping, category and value axes, series that each name their
category, value and name sources). Options with no equivalent there are kept out
of version 1.

An invalid or unknown specification is rejected with a structured error. A
specification whose `spec_version` is newer than the service understands is
kept, shown as a placeholder and never rewritten.

**Built (range sources).** The illustration above is the input form: REST, the
CLI and the SDK accept a source as `{"kind": "range", "sheet", "ref"}`, where
`sheet` is a name or identity and `ref` is A1 text without a sheet. The stored
form replaces `sheet` and `ref` with `sheet_id`, `row1`, `col1`, `row2` and
`col2`, so structural edits are arithmetic; a read returns both, adding `ref`
and `broken` to each source. Optional keys are defaulted on write, so a stored
specification has every key. Version 1 is specified key by key in the
[native format](native-format-v1.md#charts-format-v8). An unreadable or newer
stored specification is listed with an `issue` (`INVALID_SPEC` or
`UNSUPPORTED_SPEC_VERSION`) and resolve and render refuse it with that code.
A stored specification over a size limit is not unreadable (see section 5).

**Built (table and saved-query sources).** Both are additions to specification
version 1; the version does not change. The three kinds have these forms:

| Kind | Input form | Stored form | Added on read |
| --- | --- | --- | --- |
| `range` | `{"kind": "range", "sheet", "ref"}` | `{"kind": "range", "sheet_id", "row1", "col1", "row2", "col2"}` | `ref`, `broken` |
| `table` | `{"kind": "table", "table", "column"}` | `{"kind": "table", "table_id", "column_id"}` | `table`, `column` (current names), `broken` |
| `query` | `{"kind": "query", "query", "column"}` | `{"kind": "query", "query_id", "column"}` | `query` (current name), `broken` |

On input a table is found by identity or by name ignoring case, as elsewhere in
the relational interfaces, and its column by identity, else by name ignoring
case; an unknown table is `TABLE_NOT_FOUND` (404) and an unknown column
`COLUMN_NOT_FOUND`. A saved query is found by identity or name, an exact
identity always winning over a query that is merely named like it
(`QUERY_NOT_FOUND`, 404), and `column` must equal exactly one name in the
query's saved result shape, else `COLUMN_NOT_FOUND`. A chart cannot supply
parameter values, so a saved query can be charted only when every parameter it
declares has a saved default; otherwise the source is `INVALID_SPEC`, for
example "Saved query Param has no default for parameter m; a chart cannot
supply parameter values". Tables, columns and saved
queries are then held by identity, so renaming any of them needs no chart
maintenance. A query result column is held by its name, because result columns
have no identity.

A series name is one cell, so a name source must be a `range`; a `table` or
`query` name source is `INVALID_SPEC`, and `{"text": ...}` gives a fixed name.
`values`, `categories` and `x` accept all three kinds, and one chart may mix
them. Sources are lined up by position only: categories from a table come in
stored row order, and values from a saved query come in the query's order.
Mixing kinds can therefore label points wrongly, and nothing checks it; take a
chart's categories and values from the same table or the same saved query. The
same has always been true of two ranges of different lengths or orders.

A `spec_patch` rebuilds table and query sources in input form by
identity, so a rename between read and patch cannot change their target. A
merge patch keeps keys it is not told to remove, so changing a source's kind
with `spec_patch` sets the old kind's keys to `null` in the patch, for example
`{"kind": "table", "table": "Sales", "column": "Units", "sheet": null, "ref":
null}` over a range source; or replace the specification with `spec`. A
build from before this block lists a chart that uses the new kinds with issue
`INVALID_SPEC`; nothing has been released, so no such reader is in use.

## 5. Resolving data

The service resolves a chart: it reads each source through the shared engine and
returns series as typed values. Text in a value series, errors, empty cells and
hidden rows need defined behaviour. **Call**: errors and text plot as gaps, empty
cells plot as gaps, hidden rows are plotted; each is counted in the resolved
result so a client can say so. A scatter point needs both coordinates:
a usable value whose x cell is text, an error or empty is a counted gap too.

**Bounds.** A resolve reads at most a fixed number of points per series and
series per chart. **Call**: 10,000 points per series and 64 series as the first
limits, to be replaced by measured values. **Owner** (November 12, 2026): a
pie has its own, lower point limit, because Qt cannot paint 10,000 slices in
time; stage 2 measures and fixes the value. Over the limit the service refuses
with a structured error that names the limit; it does not silently truncate or
downsample in the first stage.

**Built: the limits.** Each is refused with a structured error carrying its
own code, never truncated. Measurements are in the
[stage 2 result](charts-stage-2-result.md).

| Limit | Value | Code |
| --- | --- | --- |
| Charts per workbook | 64 | `LIMIT_CHARTS` |
| Series per chart | 64 | `LIMIT_SERIES` |
| Points per series (cells in a values, categories or x source) | 10,000 | `LIMIT_POINTS` |
| Points in a pie | 1,000 (measured: 41 ms in Qt against a 75 ms budget) | `LIMIT_PIE_POINTS` |
| Source cells per chart, all sources together | 100,000 | `LIMIT_CELLS` |
| Values drawn by one render, all series together | 10,000 (a render result must stay under 4 MiB) | `LIMIT_RENDER_POINTS` |
| Text and values a resolve holds, all sources together | 4 MiB, charged as each value is read and before it is kept; for a saved query only the columns the chart uses count | `LIMIT_BYTES` |
| Chart size | 64×48 to 4,096×4,096 pixels | `INVALID_ARGUMENT` |
| Stored payload | 256 KiB | `LIMIT` |
| Job time and result | 30 seconds; 4 MiB | `TIMEOUT`, `CALCULATION_FAILED`; `READ_LIMIT` |
| Calculation input, a whole chart job | 8 MiB of cell source text read by the job's one calculator | `#LIMIT!` in the cell, so `CALCULATION_FAILED` for a value or x cell |
| A saved-query run for a chart | 20 seconds and the job's own deadline; 10,001 rows; 16 MiB of result held in memory | `CALCULATION_FAILED`; `LIMIT_POINTS`; `QUERY_LIMIT` |

A chart can therefore hold more data than one render draws: an eight-series
line chart over 10,000 rows resolves, but rendering it is refused with
`LIMIT_RENDER_POINTS`.

The size limits (`LIMIT_POINTS`, `LIMIT_PIE_POINTS`, `LIMIT_CELLS`) are
enforced when a specification is given (`chart_create`, or `chart_update` with
`spec` or `spec_patch`) and again by resolve before it reads anything. They are
not part of stored-form readability: a stored specification is readable when
its shape and grid bounds are valid. A row or column insert can therefore grow
a stored chart past a size limit. Such a chart has no `issue`, keeps following
structural edits, and accepts `spec` and `spec_patch`, which re-validate the
limits; resolve and render refuse it with the limit's code and a message that
gives the cells its source now holds. `charts.read` reports
`"over_limit"` (the limit code, or `null`) for each chart, and a row or column
edit's receipt lists `charts_over_limit` (sorted chart ids) for each chart it
pushed from within its limits to over one, alongside `broken_charts`.

**Calculated values.** Formula results are calculated today by bounded
background jobs over visible grid tiles ([viewport calculation](viewport-calculation.md)),
and a client may not fall back to synchronous evaluation. A chart can sit over
formula cells nobody has scrolled to. **Call**: resolve and render are
cancellable jobs. A resolve whose sources hold uncalculated formulas starts a
bounded calculation job for those source rectangles and waits on it; if the
calculation is refused, cancelled or fails, the resolve reports that and returns
no partial series. A chart over stored values only completes at once through
the same job interface.

**Built differently.** The chart job does not start a second calculation job.
It opens its own read-only snapshot and calculates there, with a calculator
wrapper modelled on the calculation jobs', so cancellation and the job's
30-second deadline bound the work. The ordinary per-read budget of 10,000 evaluation steps does
not apply inside a chart job; each source is read in slices of at most 1,000
cells, one bounded read per slice, each with a read's two-second deadline. One
calculator serves the whole job: every slice reuses its cache, so a formula
chain that crosses a slice boundary (a running total, say) is not recalculated
from an empty cache. A read that times out, or any `#LIMIT!` engine error in a
value or x cell, fails the whole job with `CALCULATION_FAILED` and no series;
the message names the cell. Ordinary formula errors (`#DIV/0!`, `#REF!`,
`#VALUE!`, a cycle) plot as counted gaps.

Because one calculator serves the whole job, the engine's cap of 8 MiB on the
cell source text a calculation reads applies to the chart job as a whole, not
to each read: its counter is not reset between slices. The counter grows by the
source text of each cell the calculator evaluates or reads (formula cells in
the sources and every cell their formulas reach), once per cell. Past 8 MiB
the remaining formula cells come back as `#LIMIT!`, and the resolve fails with
`CALCULATION_FAILED`. The stage 2 documents did not state this. Table and
saved-query sources are not calculated and do not count toward it.

**Following the data.** Chart sources are JSON rectangles addressed by sheet
identity, so a sheet rename needs no maintenance. Row and column inserts and
deletes, and the same-sheet Cut that formulas support today, are applied to
chart rectangles with the coordinate transforms the formula reference code
uses; the formula text scanner and rewriter in `formula_refs.py` are not
reused, because they work on formula source and sheet names. Chart sources
inherit the limits of that code: full reference transformations remain open in
the project. Where a formula would become `unmaintained` or broken, the chart
source is marked broken and the chart shows that; it is not silently repointed.
Deleting a sheet deletes the charts it owns in the same operation, and breaks
the sources of charts on other sheets that read from it.

**Built: table and saved-query sources.** Range sources keep their slice
reads. A table or saved-query source is turned into the same cell shape the
range code classifies, so gaps are counted the same way for every kind.

- *Reading a table.* Each table source is one read: under the workbook lock, a
  read transaction on the chart job's snapshot, checked against the state the
  snapshot was opened at (`STALE_STATE` otherwise), runs
  `SELECT "<column>" FROM "<table>" ORDER BY "_dh_row_id" LIMIT 10,001` (or
  1,001 for a pie) under a two-second deadline and the job's cancellation. Rows
  come in stored row order, by the hidden `_dh_row_id`; there is no filter. A
  timed-out read fails the job with `CALCULATION_FAILED`, as for ranges.
- *Running a saved query.* Each saved query a chart reads runs once per chart
  job, however many sources use it, so its series line up; a query source never
  reuses an earlier result. The run (`QueryJobs.run_once`) repeats the checks
  an ordinary saved run makes: the stored SQL is prepared again, its result
  shape and dependencies must be unchanged (`QUERY_DEPENDENCY_CHANGED`), and
  saved parameter defaults are bound. It requires the live workbook to be at
  the state the chart job started at (`STALE_STATE`, so the job ends `stale`),
  copies a private snapshot, and executes there under the query engine's
  authoriser and value rules. It is bounded by its own 20 seconds, the
  snapshot copy's 5 seconds and, for both, the chart job's absolute deadline,
  so the job stays inside its 30 seconds; the job's cancellation stops it. It
  takes no query job slot: a chart cannot be blocked by, or block, a person's
  own query, and the run is not visible on the query job routes. Rows are held
  in memory, at most 10,001 and at most 16 MiB; past 16 MiB the run fails with
  the query engine's `QUERY_LIMIT`, although an ordinary query job would spool
  the same result. The query engine's own codes (`QUERY_FAILED`, `QUERY_VALUE`,
  `QUERY_DEPENDENCY_CHANGED`, `SQL_DENIED`, `INVALID_PARAMETERS`) pass through
  with their status and details, and a `TIMEOUT` becomes `CALCULATION_FAILED`;
  each message is prefixed with `Saved query <name>: ` so a failed job names
  the query. `STALE_STATE` and `CANCELLED` are about the job, not the query,
  and are not prefixed. A query saved again later with a parameter that has no
  default fails here with `INVALID_PARAMETERS`; a chart already stored over it
  is not marked broken. The source's column must be exactly
  one column of the result, else `SOURCE_BROKEN` naming the query and the
  column.
- *Values.* From a table: integer and number columns give points; text,
  boolean, date, time and datetime values are `text` gaps; SQL `NULL` is an
  `empty` gap. From a query: integers and floats give points, and an integer
  beyond 2^53 (which the query engine tags) plots as the nearest float; text is
  a `text` gap and `NULL` an `empty` gap; a BLOB fails the job with
  `QUERY_VALUE`. Query results carry no date or boolean type, so those arrive
  as text or numbers, as SQLite returns them.
- *Category text.* Text as stored; numbers through the renderer's tick format;
  booleans as `TRUE` or `FALSE`; dates, times and datetimes as ISO text; a large
  tagged integer as its digits; `NULL` as empty text.
- *Limits known only when read.* A table's row count and a query's result size
  are known only by reading them, so they are checked at resolve. A table or
  result with more rows than a series admits is refused with `LIMIT_POINTS`
  (`LIMIT_PIE_POINTS` for a pie's values and categories), with a message that
  names the table or query, for example "Table Sales holds more than 10,000
  rows; the limit for a series is 10,000". Nothing is truncated. `LIMIT_CELLS`
  is a running total over every source of every kind as resolve reads them. A
  table or query source adds nothing to the size checks made when a
  specification is given, and chart listings report no `over_limit` for these
  kinds.
- *Breaking.* Dropping a table breaks every source on it; a schema change that
  removes a column breaks the sources on that column; deleting a saved query
  breaks every source on it. A broken table source has both identities `null`;
  a broken query source has `query_id` `null` and keeps its column name. Each
  receipt (`table_drop`, `table_schema`, `query_delete`) lists the charts in
  `broken_charts`; each breaks the sources before its after-structure hook
  runs, so a handler sees them already broken. Renaming or retyping a column, renaming a table or a saved
  query, and saving a saved query again break nothing in storage. A retyped
  column simply resolves as gaps. A saved query saved again without the
  chart's column is reported `broken` on read and refused at resolve with
  `SOURCE_BROKEN`; it plots no other column. A source is never repointed: a new
  table or query of the same name does not repair a broken source. These
  actions are structural and already clear undo history, so the chart changes
  ride with them. Resolve refuses a broken source, or one whose table, column
  or saved query is gone from the snapshot, with `SOURCE_BROKEN` before
  anything is read or run.

**History.** Successful structural edits and sheet rename or delete clear
history today. Charts inherit that: a chart change made before such an edit
cannot be undone after it.

**Freshness.** Each resolve result carries the managed revision it was resolved
at. That revision is returned to the client and held in service memory; it is
not written to the workbook, so a resolve is never a workbook write. **Call**:
staleness is coarse in the first stages. Any change of the managed revision
makes every chart's last result stale, and clients resolve again for the charts
they show. Exact tracking through formulas and queries is a later refinement.
**Built**: staleness is decided when the job is read. A job records the
workbook state it started at; once the workbook's state differs, a completed
job, or one that failed because the workbook changed while it read, is
reported `stale` and its result is withheld. Because resolved data is derived state, nothing is
replayed after a direct SQLite edit: on reopen or refresh the chart is stale and
resolves again from stored data, under the existing external-edit contract. A
chart over values the engine has not calculated yet says so, in the same way the
grid does.

## 6. Rendering

**Owner**: the service renders a resolved chart to SVG with standard-library
code, and every client shows that SVG. The desktop displays it with Qt SVG and
rasterises it for PNG copy and export. Qt SVG is listed among the deployed
macOS libraries in [LICENSES.md](../../LICENSES.md), but the build links only
Widgets and Network and no desktop source uses it, so this adds a linked Qt
module on all three platforms, with matching license materials for each. The render response also carries a list of mark rectangles (which
series and point each drawn shape is), so the desktop can offer tooltips and
click-to-select without drawing anything itself.

This is a change from the conversation that led to this document, which
suggested a native Qt plotting widget in the desktop and Matplotlib in the
service. Reading the project changed that:

- The service has no third-party Python dependencies. Matplotlib would be the
  first, on three platforms.
- Two renderers would have to agree on one specification. One renderer cannot
  disagree with itself, which removes the first anti-goal by construction.
- Qt Charts is deprecated since Qt 6.10 and Qt advises against it in new code.
  Its replacement, Qt Graphs, is built on Qt Quick; its widget route goes
  through `QQuickWidget`.
- QCustomPlot remains a sound native option (GPL, a pure `QWidget`, last
  release 2.1.1), but it would be a vendored dependency that only the desktop
  could use.

What the single-renderer route costs:

- No zoom or pan. Excel charts do not have them either, and they are a non-goal
  here.
- The service has no font metrics, so label widths, legend sizes and tick-label
  collisions must be estimated, and each viewer draws text in its own font.
  This is the main quality risk. The requirement is the same layout and marks
  everywhere; identical glyphs are not required. If estimates prove too loose,
  the remedy is a bundled font with a metrics table in the service, which the
  experiment must price.
- A resize needs a new render from the service. The desktop scales the old SVG
  until the new one arrives.
- The service returns SVG only. PNG comes from a client that has Qt.
- Qt SVG implements SVG Tiny 1.2, so the renderer must stay inside that subset.

**The experiment that decides it.** In the project's usual way, stage 1 is a
bounded experiment before any contract is fixed. It renders the six first chart
types from a fixed set of specifications and records: label and legend layout
quality against the reference Mac's fonts and at least one substituted font,
the cost of a bundled metrics table, render time at 10,000 points per
series, resize latency through the desktop, and Qt SVG fidelity. If text layout
or latency is not acceptable, the fallback is that the service still resolves
data and computes scales and ticks, and the desktop paints with QCustomPlot,
with the service SVG kept for headless use. The specification, storage and REST
resources in this document do not change in either outcome.

**Outcome, November 11, 2026.** The [experiment](chart-render-experiment.md) ran
on the reference Mac and did not meet every threshold: a 10,000-slice pie is too
slow in Qt, and text overflows its estimate under a wide substitute font. Its
result document proposes a lower point limit for pies and a bundled font in
place of the fallback.

**Ruling, November 12, 2026 (Owner).** The owner accepted both proposals. The
service SVG route is confirmed and the QCustomPlot fallback is not taken.

1. **Pie point limit.** A pie is refused above a point limit lower than the
   10,000 that applies to the other types, with the same structured error.
   Stage 2 measures the value; the experiment only shows that a limit in the
   hundreds passes with a wide margin.
2. **One bundled font.** The product ships one freely licensed font. The
   service lays out text against a metrics table for it, measured at label
   size, and the SVG names that family. The desktop loads the same font, so
   its text matches the layout. A REST consumer that views the SVG without the
   font may see text a little wider or narrower than laid out; a PNG
   rasterised by the desktop does not have that problem.

**Built.** The font is DejaVu Sans 2.37, shipped as
`service/datahorizon/fonts/DejaVuSans.ttf` with a metrics table generated from
it; the pie limit is 1,000. The SVG names exactly one family,
`font-family="DejaVu Sans"`: Qt SVG reads a list such as
`"DejaVu Sans, sans-serif"` as one unknown family and draws in the system font
even when the bundled font is loaded. A viewer without the font therefore uses
its own default face rather than a sans-serif.

Settled by the stage 2 plan: which font (its license must be
compatible with GPL-3.0-only distribution and it must be recorded in
`LICENSES.md`, with Qt SVG), the pie limit's value, how the font is packaged on
all three platforms, and text outside the metrics table. The experiment
measured only Latin text on one Mac; Windows and Linux are unmeasured.

## 7. Storage

**Call**, **Built**: charts are a new kind in the existing `dh_objects` catalog
(`namespace`, `id`, `sheet_id`, `payload`), following formats v3 to v7, which
each added kinds to the existing catalogs and upgrade a workbook lazily on
first use. The format becomes v8 when a workbook first holds a chart. The
payload holds display name, anchor cell, offset, size, z-order, the
specification JSON and its `spec_version`. Resolved data and rendered output
are not stored. The kind is documented in the native format document so direct
SQLite readers can list charts and read their specifications.

As built, the payload has exactly `name`, `anchor` (`row`, `col`), `offset`
(`x`, `y`), `size` (`width`, `height`), `z`, `spec_version` and `spec`, and a
range source is stored as `{"kind": "range", "sheet_id", "row1", "col1", "row2",
"col2"}`, all four coordinates `null` when broken. The row's `sheet_id` is the
sheet the chart is placed on. Table and saved-query sources add two stored
forms, `{"kind": "table", "table_id", "column_id"}` (both `null` when broken)
and `{"kind": "query", "query_id", "column"}` (`query_id` `null` when broken);
the format stays v8. See [Charts format v8](native-format-v1.md#charts-format-v8).
Duplicating a sheet does not copy its charts, as it does not copy declared
lists.

A direct edit to a chart row is treated like any other direct edit: the
service validates on open, reports a specification it cannot read, and does not
repair it silently. Older services refuse a workbook at a newer format version,
as today.

## 8. Managed operations and interfaces

Engine operations, each available through REST with OpenAPI entries, the managed
CLI and the SDK:

- list charts on a sheet or in the workbook
- create a chart from a specification and placement
- suggest a specification from a range (the desktop's Insert Chart uses this, so
  a script gets the same defaults)
- read, replace and patch a chart's specification
- move, resize, reorder, rename and delete a chart
- resolve a chart, as a cancellable job (typed series, counts of gaps, the
  revision resolved at)
- render a chart at a given size, as a cancellable job (SVG and mark
  rectangles)

Create, change, move and delete take part in history and undo as single
operations.

**Built.** Writes are three operation actions on the existing operations route:

- `chart_create`: `spec` (input form), optional `sheet`, `name`, `anchor`,
  `offset`, `size`; the name defaults to the lowest unused "Chart N" and the
  new chart goes on top.
- `chart_update`: `chart_id` and at least one of `name`, `anchor`, `offset`,
  `size`, `z` or `order` (`front` or `back`), and `spec` (a replacement) or
  `spec_patch` (an RFC 7386 merge patch on the input form). A patch is refused
  with `SOURCE_BROKEN` while a source is broken; send `spec` instead. To change
  a source's kind with a patch, set the old kind's keys to `null` (section 4).
  A table or query source is not admitted as a series name, and the OpenAPI
  `ChartSpec` points the name source at `ChartRangeSource` alone.
- `chart_delete`: `chart_id`.

Each records its own change-set entry, so it undoes and redoes without clearing
cell history. Their undo labels are `Create chart`, `Edit chart` and `Delete
chart`; an explicit `label` in the request wins. Receipts carry `chart_id` and
`changed_charts`; a structural edit that breaks a chart source lists it in
`broken_charts`, and one that pushes a chart past a size limit lists it in
`charts_over_limit` (section 5). A failed chart job's error carries
`http_status`, which the SDK raises with.

Reads and jobs:

| Route | Purpose |
| --- | --- |
| `POST /v1/workbooks/{workbook}/charts` | State-checked catalog: every chart, or one sheet's, or one chart |
| `POST /v1/workbooks/{workbook}/charts/suggest` | Suggest an input-form specification from a rectangle of at most 10,000 cells, or from a table |
| `POST /v1/workbooks/{workbook}/charts/jobs` | Start a resolve or render job (`kind`, and `width` and `height` for render) |
| `GET /v1/workbooks/{workbook}/charts/jobs/{job}` | Poll: `running`, `completed`, `failed`, `cancelled` or `stale` |
| `POST .../charts/jobs/{job}/cancel`, `.../release` | Cancel a job; release a finished one |

The SDK's `WorkbookClient` has `charts`, `create_chart`, `update_chart`,
`delete_chart`, `suggest_chart`, `chart_job`, `chart_job_status`,
`resolve_chart` and `render_chart`; the last two start a job, wait for it,
release it and raise on a failed or stale job. The managed CLI has
`charts list|create|update|delete|suggest|resolve|render`, reading create and
update arguments as JSON from standard input; `render --svg-out FILE` writes
the SVG to a file through a temporary sibling. It refuses a destination that
is the workbook or a symbolic or hard link to it, and replaces another
existing file only with `--overwrite`. Capabilities advertise `charts`, `max_charts`,
`chart_spec_version`, `chart_jobs`, `chart_job_seconds`,
`max_chart_render_points` and `max_chart_source_bytes`.

**Built: suggest from a table.** The suggest route also takes
`{expected_state, table, type?}`; giving a table with a sheet or rectangle is
`INVALID_ARGUMENT`. It reads the table's schema and no rows. Integer and number
columns are data columns, in schema order. The first column that is neither a
data column nor boolean holds the categories; with none, `categories` is
`null`. Each data column becomes a series named `{"text": <column name>}`, at
most 64 (`LIMIT_SERIES`). For `scatter` the first data column is `x` for every
series and at least two are needed; for `pie` only the first is used. A table
with no data column is `INVALID_SPEC`. The suggestion names the table and its
columns by identity and passes the same validation as create. The SDK has
`suggest_chart(table=..., type=...)` and the CLI `charts suggest --table NAME
[--type T]`. There is no suggestion from a saved query: its saved shape has
names but no types, so choosing series would mean running it.

Not built: rename, move, resize and reorder are fields of `chart_update`
rather than separate operations, and there is no render of an unsaved
specification; a preview of one is left to the desktop stage. Errors are structured: an unknown source, a broken reference, a
limit, an unsupported specification version.

**Call**: chart operations raise no Python events in the first stages and are
not cell changes, so existing change handlers and bindings do not run for them.
Chart events can be added later under the Python runtime's event contract if a
use appears.

## 9. Desktop

The desktop needs wireframes before it is built, under the project's
wireframes-before-implementation rule. They should cover:

- Insert Chart from a selection, with a type picker and a live preview.
- A chart floating over the grid: select, move, resize, delete, copy as image.
- A side panel or dialog to edit the title, series sources, axes and legend,
  with range picking in the manner of the Python workspace.
- Stale, calculating, broken-reference, over-limit and unsupported-version
  states.
- Keyboard access and an accessible description of the chart.
- Charts in the workbook object browser beside Sheets, Tables and Queries.

## 10. Python

**Call**: workbook Python gets chart operations on its transaction-aware
context in stage 3 (list, create, change, delete), calling the engine directly
and taking part in the macro's transaction. This is where a user builds charts
in code.

Plotly and Matplotlib figures in workbook code depend on package installation,
which is not offered today. If it is added, a figure can be attached to the
workbook as an image or HTML artifact. That would be a separate object kind
from a chart: it would not follow data, would not be editable in the desktop and
would not export as a chart.

## 11. Interchange

Until stage 4, importing an XLSX or ODS file with charts reports them as today
(`workbook.feature` `charts`, `chart_sheets`), and exporting a workbook that
holds native charts reports each one as omitted. Nothing is dropped silently.

**Built.** XLSX and ODS exports (whole workbook, selection and streamed) list
each native chart as omitted, by chart and sheet name, as they list workbook
names and event bindings: "Charts are native-only and omitted from this
export: Sales on Sheet1, …". CSV export reports nothing about charts. A
whole-workbook import with `replace_empty` counts charts as content and is
refused with `DESTINATION_NOT_EMPTY`.

Stage 4 maps the specification to and from DrawingML charts and OpenDocument
chart objects: export first, because it only has to write what DataHorizon
holds; import second, with unsupported chart types and options degraded and
reported under the Excel fidelity programme's rules. CSV and other tabular
interchange never carry charts.

## 12. Stages

| Stage | Delivers | Done when |
| --- | --- | --- |
| 1. Renderer experiment | Section 6 measurements and a written result | The evidence confirms the service SVG route or triggers the fallback. Run November 11, 2026: thresholds not all met ([result](chart-render-experiment.md)). Owner ruling November 12, 2026: service SVG route confirmed with a pie point limit and a bundled font (section 6) |
| 2. Engine and interfaces | Storage, specification validation, resolve, render, reference maintenance, staleness, REST/CLI/SDK, history | A script creates, changes and renders all six chart types with no desktop; structural edits and undo are verified. **Done**: range sources on November 12, 2026, table and saved-query sources on November 13, 2026 ([result](charts-stage-2-result.md), [plan](charts-table-query-sources-plan.md)) |
| 3. Desktop and Python | Wireframes, Insert Chart, floating charts, edit panel, object browser entry, context operations | The wireframe session is completed in the installed application |
| 4. Interchange | XLSX and ODS chart export, then import, with reports | A round trip keeps every supported chart and reports the rest |
| 5. More chart types | Combination charts, secondary axis, histogram, trendlines, chart sheets | Per-type criteria in that stage's plan |

Each stage gets its own task-level plan when it starts.

## 13. Owner decisions

Settled on November 11, 2026, with the rendering route confirmed on November 12, 2026 and two decisions on table and saved-query sources added on November 13, 2026. None remain open.

1. **Rendering route.** The single service SVG renderer of section 6. The
   stage 1 experiment still runs and can trigger the QCustomPlot fallback
   described there. After the experiment the owner confirmed this route on
   November 12, 2026, with a lower point limit for pies and one bundled font
   in place of the fallback.
2. **Timing.** Stage 1 starts now, in parallel with the Excel fidelity
   programme. Later stages are scheduled when stage 1 reports.
3. **First chart types.** Column, bar, line, area, pie and scatter.
4. **Chart events.** None in the first stages.
5. **Source kinds.** Ranges first; table and saved-query sources as a second
   step of stage 2.
6. **Table and saved-query sources before the desktop** (November 13, 2026).
   They are the next block after range sources, ahead of stage 3.
7. **A table source is the whole table** (November 13, 2026): one column in
   stored row order, with no saved view, refused when the table holds more
   rows than the point limit. Filtering and ordering are done with a saved
   query. The saved view of Draft 0.4 is dropped: the code has no saved view
   over a table, and what the project calls saved views are saved queries.

## 14. References

- Qt Charts deprecation: <https://doc.qt.io/qt-6/qtcharts-index.html>
- Qt Graphs widgets: <https://doc.qt.io/qt-6.9/qtgraphswidgets-index.html>
- QCustomPlot: <https://www.qcustomplot.com/>

## 15. Departures from Draft 0.3, as built

Each is recorded above where it applies. All are **Built** on branch
`claude/charts-stage-2`.

- **Undo entries for charts.** No earlier `dh_objects` kind took part in undo;
  names and bindings clear history. Chart writes record their own change-set
  entries, so chart edits undo without clearing cell history.
- **Calculation inside the job.** Section 5 said a resolve starts a calculation
  job and waits on it. The chart job calculates inside its own read-only
  snapshot instead, with one calculator for the whole job and without the
  per-read step budget, reading each source in slices of 1,000 cells. Any
  `#LIMIT!` in a value or x cell fails the resolve.
- **Size limits outside stored readability.** The point and cell limits are
  enforced on input and at resolve, not as a condition of a readable stored
  specification, so a structural edit cannot strand a chart (`over_limit`,
  `charts_over_limit`).
- **Integer stored sources.** Section 4's illustration stores `ref` text.
  Stored sources hold `row1`, `col1`, `row2`, `col2`; `ref` is accepted on input
  and returned on read.
- **A render point limit.** Not in Draft 0.3. One render draws at most 10,000
  values across all series, because a render result over 4 MiB fails.
- **One font family name.** Draft 0.3 had the SVG name the bundled family. It
  names that family alone, with no fallback list, because Qt SVG cannot read a
  list.
- **Sheet duplication does not copy charts.**

## 16. Departures from Draft 0.4, as built

Table and saved-query sources, **Built** on branch
`claude/charts-table-query-sources`. Each is recorded above where it applies.

- **No saved view.** A table source is the whole table in stored row order
  (owner decision 7). Draft 0.4 read a table through a saved view.
- **A saved-query run outside the job store.** Draft 0.4 said a query source
  runs "as a snapshot job". It runs once per chart job through
  `QueryJobs.run_once`, which takes no query job slot, holds at most 10,001 rows
  and 16 MiB in memory, and is bounded by the chart job's deadline. It still
  makes the private snapshot copy every query run makes.
- **A query result column held by name.** If the saved query is saved again
  without that column, the chart is refused at resolve and reported `broken` on
  read; its stored source is not rewritten when the query is saved.
- **Size limits at resolve only for these kinds.** A table's or result's size
  is known only by reading it, so `LIMIT_POINTS` and `LIMIT_PIE_POINTS` are
  checked when it is read, `LIMIT_CELLS` becomes a running total at resolve,
  and listings report no `over_limit` for these kinds.
- **A series name is a range or text.** A name is one cell; a table or query
  column is many rows.
- **Suggest from a table, not from a saved query.** Section 8 named suggest from
  a range; a table is added, and a saved query is left out until the desktop
  needs it.
- **A saved query needs a default for every parameter to be charted.** Draft
  0.4 said a query source runs with its saved parameter values; a chart has no
  way to supply others, so a query with a parameter that has no saved default
  is refused on input with `INVALID_SPEC`, and every error a query run raises
  at resolve names the saved query.
