# Charts: desktop authoring and export

Status: **Draft 0.2** (November 15, 2026), approved by the owner on November
15, 2026. **Export is built** on branch `claude/charts-export` (not merged; see
[the status document](implementation-status.md#charts-export--november-15-2026)
and the [export plan](charts-export-plan.md)); the as-built rules are in
sections 6 and 7 and the [compatibility contract](compatibility-v0.md#charts).
**Authoring (sections 4 and 5) is not built.**
Marks: **Owner** is a decision the owner made; **Call** is the author's
proposal, open to correction; **Builder** is left to the implementation plans.

Read with the [charts design](charts-design.md) (Draft 0.6), the
[import and view-only design](charts-import-view-design.md), the
[compatibility contract](compatibility-v0.md#charts) and the
[native format](native-format-v1.md#charts-format-v8).

## 1. Intent

A person can make a chart in the application, change it afterwards, and get it
out to Excel and LibreOffice. Today none of the three is possible from the
desktop:

1. The desktop draws native charts but they ignore the mouse and keyboard.
   Creating or changing one takes a script, the CLI or REST.
2. XLSX and ODS export leave every chart out and say so. A chart imported from
   a file is lost on the way back out.

The engine side of authoring exists: `chart_create`, `chart_update`,
`chart_delete` and the suggest route are built, undoable and tested (charts
design, section 8). This block is the desktop half of charts stage 3 and the
export half of stage 4.

## 2. Goals, non-goals and anti-goals

**Goals**

- Select data, choose Insert → Chart, and a suggested chart appears on the
  sheet, selected, with a docked side panel open. **Owner**
- Everything about a chart that specification version 1 holds can be changed in
  that side panel. **Owner** (the side panel); **Call** (its contents, section 5)
- A chart can be selected, moved, resized, reordered and deleted on the sheet,
  with the mouse and with the keyboard.
- Every authoring action is one undoable managed operation, the same one a
  script would send.
- XLSX and ODS exports write every chart whose data comes from cell ranges as a
  live chart that follows its cells in the other application.
- A chart fed by a table column or a saved query is left out of the export and
  named in the report. **Owner**
- Import of an exported file gives back the same charts (section 7).

**Non-goals**

- Workbook-Python chart operations (charts design, section 10). **Call**: the
  owner asked for creating, editing and exporting; these stay open in stage 3.
- A type-picker dialog with a live preview, and the service route that would
  render an unsaved specification. **Owner** chose insert-at-once instead.
- Copy as image; a Charts entry in a workbook object browser (the desktop has
  no object browser). **Call**
- Cut, copy and paste of charts; multi-chart selection.
- New chart types or options. Specification version 1 is unchanged. Colours,
  data labels, gap width and the rest of the styling that import drops are not
  added, so they are not exported either.
- Chart sheets, combination charts, secondary axes.
- Exporting table- or query-sourced charts with frozen values. **Owner**
- CSV and other tabular formats, which hold no charts.
- Windows and Linux verification.

**Anti-goals**

- An export that fails, or loses cells, because of a chart.
- An exported chart that points at different cells than the native chart did.
- A chart left out of an export, or changed by it, without a report line.
- A desktop edit that bypasses the managed operations, or that is applied to
  the picture without being stored.
- An authoring action that cannot be done from the keyboard.

## 3. Approaches

**Authoring**

| Approach | Trade-off | |
| --- | --- | --- |
| Extend the existing overlays in `desktop/chartlayer.cpp`; add a right-hand dock to the main window | Keeps the placement, frozen-pane and render-job code that was reviewed and merged in PR 140. The overlays change from mouse-transparent to interactive | **Chosen (Owner)** |
| Replace the overlays with a drawing scene over the grid | Suits future shapes and images. Rewrites merged placement code for objects that do not exist | Not taken |
| Edit in a modal dialog | Ruled out by the owner's choice of a side panel | Not taken |

**Insert flow.** Two were shown as mockups. **Owner** chose insert-at-once
(Excel's behaviour) over a dialog with a live preview. Insert-at-once needs no
new service route.

**Export**

| Approach | Trade-off | |
| --- | --- | --- |
| Mirror the import: one shared step turns a native chart into an export draft and decides what is left out; one writer per format | The leave-out rules and report entries exist once. All export routes call the same step | **Chosen (Owner)** |
| Write each format straight from the native chart | Fewer files; the rules exist twice and can drift | Not taken |
| Write through a spreadsheet library | The service writes XLSX and ODS itself with the standard library | Not taken |

**Table- and query-sourced charts.** **Owner** chose leave out and report over
writing frozen values. Frozen values would look right in Excel but stop
following data, and the importer refuses literal chart data, so they would not
come back.

**Order.** **Owner**: one design, two implementation plans, two pull requests.
Export first (service only, smaller, gives a round trip to test against), then
authoring, whose acceptance run ends by exporting a chart made in the
application.

## 4. Authoring: on the sheet

**Insert.** A new Insert menu holds Chart. **Call**: a new menu, between Edit
and Format, because the owner asked for Excel's shape and no existing menu
fits. With one rectangular selection on a sheet:

1. The desktop asks the suggest route for a specification of the selection.
2. It sends `chart_create` with that specification. **Call**: the anchor is the
   selection's top row, one column right of the selection (or the selection's
   own top-left when no column is to the right); size and name are the engine
   defaults.
3. The new chart is selected and the side panel opens on it.

One Undo removes the chart. When suggest or create refuses (no numeric data, a
selection over 10,000 cells, the chart limit, a read-only workbook), the
service's message is shown and nothing is inserted; the desktop does not insert
an empty chart. **Call**. Insert → Chart is disabled with no workbook open, in
a read-only workbook and in a projected list view (charts are hidden there).
The grid's selection is always one rectangle.

**Select.** A click on a chart selects it and shows a frame with handles. A
click on the grid or Escape deselects. A chart takes mouse clicks over its
area, as in Excel; the wheel still scrolls the grid. One chart is selected at a
time.

**Move and resize.** Dragging the chart moves it; dragging a handle resizes it
within the engine's bounds (64x48 to 4096x4096). The overlay follows the
pointer during the drag and one `chart_update` is sent on release, with the
anchor cell under the chart's top-left corner and the offset inside that cell.
Escape during a drag cancels it. A refused update puts the chart back and shows
the reason.

**Delete and order.** Delete or Backspace removes the selected chart. A context
menu on a chart offers Edit Chart (opens the panel), Bring to Front, Send to
Back and Delete.

**Keyboard.** **Call**: commands Select Next Chart and Select Previous Chart
(in the Insert or Edit menu, shortcuts **Builder**) move focus to a chart on
the sheet. On a selected chart the arrow keys move it, Shift with arrows
resizes it, Delete removes it, Enter moves focus to the panel and Escape
returns to the grid. Each overlay has an accessible name ("Chart: Sales") and a
description built from the type, title and series count.

**States.** A chart with a broken source, over a size limit, with an
unsupported specification version, still drawing or stale shows what the
view-only layer shows today, and can still be selected, moved, deleted and
opened in the panel. The panel repeats the reason at its top.

**After any change** the desktop reads the chart catalog again and draws from
it, as the view-only layer does after any workbook change. Undo and Redo use
the existing Edit commands and the operations' own labels; when the selected
chart no longer exists, the selection clears.

## 5. Authoring: the side panel

A dock on the right of the main window, in the manner of the Issues dock at the
bottom. **Call**: it opens when a chart is inserted, on Edit Chart and on a
double-click; a plain click selects without opening it. It has a View menu
toggle, stays open while the selection changes, and reads "No chart selected"
with nothing selected.

| Group | Fields |
| --- | --- |
| Chart | Name; Bring to Front, Send to Back |
| Type | Column, bar, line, area, pie, scatter; grouping (clustered, stacked) where the type takes one |
| Title | Title text |
| Data | Categories source; per series: name (text or a cell), values source, and x source for scatter; add, remove and reorder series |
| Axes | Category axis title; value axis title, minimum and maximum (empty means automatic) |
| Legend | Right, bottom or none |

**Sources.** Each source field has a kind: Range, Table column or Query column.
**Call**: all three are offered because the engine holds all three and tables
and saved queries are what set the product apart. A range is typed as a
reference or picked on the grid. **Call**: picking is a new mode, because the
existing in-grid picker works only inside a formula draft and the reference
picker dialog excludes the current sheet. A pick button on the field starts it;
while it is active, selecting cells on any sheet fills the field and the chart
stays selected (the one exception to "a click on the grid deselects"); Enter
applies the pick and Escape cancels it. A table
column and a query column are chosen from lists of the workbook's tables and
saved queries. A series name takes text or a single cell only, as the engine
requires.

**Applying.** Every change is applied at once as one `chart_update`: choices on
change, text on Enter or when the field loses focus. There is no Apply button.
A change the engine refuses (a pie with two series, a source over a limit, an
unknown reference) leaves the chart as it was, puts the field back and shows
the service's message beside it. **Call**: the panel never drops data to make
a change fit; changing a two-series chart to a pie is refused with the reason,
not silently cut to one series.

**Call**: a change of type is one update that also carries the companion
changes the engine's rules require, none of which loses a source:

- Grouping becomes `clustered` when the new type takes a grouping and the old
  one had none, and none when the new type takes none.
- To scatter, the chart's categories source becomes the x source of every
  series; from scatter, the first series' x source becomes the categories. A
  chart with no categories cannot become a scatter until an x source is given,
  and the refusal says so.

Add Series adds a row to the panel only; the series is created in the chart
when its values source (and x source, for scatter) is filled in, because the
engine has no series without values.

The engine refuses `spec_patch` while a source is broken. **Builder**: send a
whole `spec` in that case, or always.

**Service changes.** None are expected. If one is needed (for example the
lists the source fields show), it goes through REST with an OpenAPI entry and
is reachable from the CLI and SDK, not a desktop-only path.

**Wireframes.** The project requires wireframes before desktop work. The
mockup the owner chose from (option A, kept outside version control under
`.superpowers/brainstorm/`) is the starting point; the authoring plan's first
task records the wireframes for sections 4 and 5 (every state included) in
`docs/design/` for the owner's sign-off before any desktop code.

## 6. Export

**What is written.** A native chart is exported when every source it has is a
range source that the file format can express as one reference on an exported
sheet. It is written as a DrawingML chart in XLSX and a chart object in ODS,
anchored at its cell with its offset and size, in z order, with:

| Native | In the file |
| --- | --- |
| `column`, `bar`, `line`, `area`, `pie`, `scatter` | The matching chart type |
| `clustered`, `stacked` | The matching grouping |
| Title, axis titles | Literal text |
| Value axis `min`, `max` | Fixed minimum and maximum |
| Legend `right`, `bottom`, `none` | Legend position, or no legend |
| Series name by cell or by text | XLSX: a cell reference or literal text. ODS: a cell reference only (see the change below) |
| Values, categories, scatter x | Cell references by sheet name |
| Name, anchor, offset, size, z | Frame name, anchor, extent and drawing order |

This is the inverse of the "what an imported chart keeps" table in the
compatibility contract. Nothing native is approximated, because specification
version 1 holds nothing the two formats cannot express.

**Change made during the build (November 15, 2026).** ODS cannot carry a series
name given as text: it would need an embedded copy of the data in the chart
object, which the export does not write. The chart is still written, the export
reports `chart.converted` with detail `series_name` for it, and on import the
series is named "Series N". A series named by a cell reference round-trips in
both formats; XLSX keeps both kinds. The "Done when" tests use cell-reference
names for ODS and pin this difference in a test of its own.

**What is left out.** The whole chart is left out, never part of it, and the
report names it with one of these reasons. **Call** for the list beyond the
owner's table and query ruling:

| Reason | Meaning |
| --- | --- |
| `table_source` | A source is a table column |
| `query_source` | A source is a saved-query column |
| `broken_source` | A source is broken |
| `source` | A range source the format cannot express as one reference, or one on a sheet that is not in the export; in a selection export, an anchor or a source outside the selection |
| `unsupported` | The stored chart's record or specification cannot be read: a version this service cannot read, or a malformed stored record; the export does not fail |
| `unwritable` | Writing the chart failed for an unexpected reason; the export continues |

The existing line "Charts are native-only and omitted from this export" goes
away; each export report instead says how many charts were written and lists
each chart left out with its sheet and reason. **Builder**: the entry names and
the report form, matching the import's `chart.*` vocabulary and each export
route's existing report shape.

**Routes.** The whole-workbook exports, inline and streamed, write charts.
**Call**: a selection export writes a chart only when its anchor and every
source lie inside the selection; any other chart on the sheet is reported as
left out (`source`). A selection export keeps cells at their original
coordinates on a sheet named `Sheet1`, so the chart keeps its anchor and its
references name `Sheet1`. List and relational exports and CSV
never carry charts, as today.

**Cached values.** **Call**: charts follow the rule the exports already have
for formulas ("Formula caches are omitted; target recalculation is
requested"). The export runs no calculation and no chart resolve job for
charts. Where the export writes a cell's value, the builder may write it as the
chart's cached value too; where it writes only a formula, the chart has no
cache and shows its data once the other application recalculates on open.
That meets the acceptance in section 7.

**Bounds.** Writing charts is bounded by the limits the engine already
enforces (64 charts, 64 series). Chart writing checks cancellation and the
export deadline like the rest of the export. A chart that cannot be written
for an unexpected reason is reported and the export continues.

**Import report clean-up.** The status document records an inconsistency
awaiting an owner decision: an import whose charts all import still lists
`workbook.feature` `charts` and `sheet.feature` `drawings` as omitted.
**Call**: settle it in the export plan, because it makes a clean round trip
report noise. `charts` is no longer reported as an omitted workbook feature
(`chart.converted` and `chart.skipped` already account for every chart), and
`drawings` is reported only for a sheet whose drawing holds something other
than charts.

**Change made during the build (November 15, 2026).** Two cases the paragraph
above left out are part of the rule as built: `sheet.feature` `drawings` is also
reported for a sheet whose drawing was not read (so nothing was imported from
it), and the ODS `embedded_objects` count does not include an object that was
read as a chart. `chart_sheets` is unchanged.

## 7. Done when

**Export**

- For each of the six types, clustered and stacked where they apply, a native
  workbook exported to XLSX and to ODS and imported again holds charts equal to
  the originals in type, grouping, titles, axes, legend, sources, series names,
  name, anchor, offset, size and order. Any difference the formats force is
  listed in the compatibility contract, not hidden.
- **Call**: three differences come from the importer's own rules and are
  documented, not fixed, in this block: text over 255 characters is cut on
  import, duplicate chart names on a sheet are made unique, and an empty
  series name comes back as "Series N". The round-trip tests use charts inside
  those rules, and one test pins each difference. No engine limit is added.
- The repository's look-alike fixtures (`tests/fixtures/charts-two-sheets.xlsx`)
  imported and exported keep both charts. The owner's own sample
  `Column Chart_Complete.xlsx` is not committed; it is run by hand when it is
  on the reference Mac, and recorded as such.
- A workbook with a table-sourced chart, a query-sourced chart and a
  broken-source chart exports its cells and its range charts, and reports the
  three by name and reason.
- The exported files are opened in Excel and in LibreOffice and each chart
  shows its data. Where an application is not available on the reference Mac,
  the status document says "not verified" for it; openpyxl and odfpy reading
  the files is the automated check.
- The compatibility contract, the charts design, the status document and
  OpenAPI are updated; the stale "not merged" statements about the chart
  branches are corrected.

**Authoring**

- The wireframes are signed off before desktop code.
- In the installed application, with the keyboard alone and with the mouse:
  insert a chart from a selection, change its type, title, a series source (a
  range, a table column and a query column), axis and legend, move it, resize
  it, reorder it, delete it, and undo and redo each step.
- Every refusal named in sections 4 and 5 shows the service's reason and
  changes nothing.
- A chart made in the application is exported and shows its data in the other
  application (same availability rule as above).
- Desktop tests cover selection, drag, resize, keyboard, the panel's fields
  and refusals; VoiceOver and physical-input acceptance are recorded as open if
  not run.

## 8. Left to the builder

- File layout on the service side (`export_charts.py` beside
  `import_charts.py` is the expected shape) and how the streamed writers take
  chart parts.
- XLSX anchor form, units and rounding; ODS frame and chart sub-document
  layout; sheet-name quoting in references.
- Shortcuts, handle count and look, drag behaviour across frozen panes, how
  the panel debounces edits and handles a stale workbook state.
- Whether the panel sends `spec_patch` or a whole `spec`.
- Test fixtures and the round-trip harness.
