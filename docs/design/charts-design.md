# Charts design

Draft 0.2 | November 11, 2026 | Status: approved by the owner on November 11, 2026. Nothing here is built.

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
| Storage | A chart kind in the existing `dh_objects` catalog, at native format v8 | **Call** |
| Source kinds | `range` first; `table` and `query` in a later step of stage 2 | **Owner** (accepted as proposed) |
| Uncalculated sources | Resolve and render are cancellable jobs that calculate their sources | **Call** |
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
  through a saved view when one is named so that the view supplies filter and
  order.
- `query`: a column of a saved query's result, by saved-definition identity and
  column name. Resolving it runs the query as a snapshot job with its saved
  parameter values; it does not reuse an earlier result.

**Call**: `range` sources are built first. `table` and `query` sources follow
as a second step of stage 2, because they depend on the saved-view and snapshot
job paths and a table without a view is refused once it passes the point limit.

The specification deliberately follows the shape Excel and OpenDocument share
(a chart type, a grouping, category and value axes, series that each name their
category, value and name sources). Options with no equivalent there are kept out
of version 1.

An invalid or unknown specification is rejected with a structured error. A
specification whose `spec_version` is newer than the service understands is
kept, shown as a placeholder and never rewritten.

## 5. Resolving data

The service resolves a chart: it reads each source through the shared engine and
returns series as typed values. Text in a value series, errors, empty cells and
hidden rows need defined behaviour. **Call**: errors and text plot as gaps, empty
cells plot as gaps, hidden rows are plotted; each is counted in the resolved
result so a client can say so.

**Bounds.** A resolve reads at most a fixed number of points per series and
series per chart. **Call**: 10,000 points per series and 64 series as the first
limits, to be replaced by measured values. Over the limit the service refuses
with a structured error that names the limit; it does not silently truncate or
downsample in the first stage.

**Calculated values.** Formula results are calculated today by bounded
background jobs over visible grid tiles ([viewport calculation](viewport-calculation.md)),
and a client may not fall back to synchronous evaluation. A chart can sit over
formula cells nobody has scrolled to. **Call**: resolve and render are
cancellable jobs. A resolve whose sources hold uncalculated formulas starts a
bounded calculation job for those source rectangles and waits on it; if the
calculation is refused, cancelled or fails, the resolve reports that and returns
no partial series. A chart over stored values only completes at once through
the same job interface.

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

**History.** Successful structural edits and sheet rename or delete clear
history today. Charts inherit that: a chart change made before such an edit
cannot be undone after it.

**Freshness.** Each resolve result carries the managed revision it was resolved
at. That revision is returned to the client and held in service memory; it is
not written to the workbook, so a resolve is never a workbook write. **Call**:
staleness is coarse in the first stages. Any change of the managed revision
makes every chart's last result stale, and clients resolve again for the charts
they show. Exact tracking through formulas and queries is a later refinement. Because resolved data is derived state, nothing is
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

## 7. Storage

**Call**: charts are a new kind in the existing `dh_objects` catalog
(`namespace`, `id`, `sheet_id`, `payload`), following formats v3 to v7, which
each added kinds to the existing catalogs and upgrade a workbook lazily on
first use. The format becomes v8 when a workbook first holds a chart. The
payload holds display name, anchor cell, offset, size, z-order, the
specification JSON and its `spec_version`. Resolved data and rendered output
are not stored. The kind is documented in the native format document so direct
SQLite readers can list charts and read their specifications.

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
operations. Errors are structured: an unknown source, a broken reference, a
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

Stage 4 maps the specification to and from DrawingML charts and OpenDocument
chart objects: export first, because it only has to write what DataHorizon
holds; import second, with unsupported chart types and options degraded and
reported under the Excel fidelity programme's rules. CSV and other tabular
interchange never carry charts.

## 12. Stages

| Stage | Delivers | Done when |
| --- | --- | --- |
| 1. Renderer experiment | Section 6 measurements and a written result | The evidence confirms the service SVG route or triggers the fallback |
| 2. Engine and interfaces | Storage, specification validation, resolve, render, reference maintenance, staleness, REST/CLI/SDK, history | A script creates, changes and renders all six chart types with no desktop; structural edits and undo are verified |
| 3. Desktop and Python | Wireframes, Insert Chart, floating charts, edit panel, object browser entry, context operations | The wireframe session is completed in the installed application |
| 4. Interchange | XLSX and ODS chart export, then import, with reports | A round trip keeps every supported chart and reports the rest |
| 5. More chart types | Combination charts, secondary axis, histogram, trendlines, chart sheets | Per-type criteria in that stage's plan |

Each stage gets its own task-level plan when it starts.

## 13. Owner decisions

All settled on November 11, 2026. None remain open.

1. **Rendering route.** The single service SVG renderer of section 6. The
   stage 1 experiment still runs and can trigger the QCustomPlot fallback
   described there.
2. **Timing.** Stage 1 starts now, in parallel with the Excel fidelity
   programme. Later stages are scheduled when stage 1 reports.
3. **First chart types.** Column, bar, line, area, pie and scatter.
4. **Chart events.** None in the first stages.
5. **Source kinds.** Ranges first; table and saved-query sources as a second
   step of stage 2.

## 14. References

- Qt Charts deprecation: <https://doc.qt.io/qt-6/qtcharts-index.html>
- Qt Graphs widgets: <https://doc.qt.io/qt-6.9/qtgraphswidgets-index.html>
- QCustomPlot: <https://www.qcustomplot.com/>
