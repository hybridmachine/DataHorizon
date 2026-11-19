# Implementation status

Execution started October 31, 2026 after the explicit instruction to execute the
plan. The design-only hold is superseded for this work. This is a working local
development preview, not completion of the full M0–M8 plan or a release.

## Calculation stage 1 (measurement) — November 18, 2026

The owner approved the [stored results and scaled recalculation design](calculation-design.md)
(increment 10 of the Excel fidelity programme) on November 18, 2026, with a
content fingerprint as the freshness rule. Its stage 1 is built on branch
`claude/calculation-stage-1` following the [plan](calculation-stage-1-plan.md):
isolated test code in `tests/` (a fixture, three prototypes, 25 tests and a
benchmark). No service, desktop or native format code changed, and no chart,
formula or workbook behaviour is different.

Measured on the reference Mac on a 1,002,003-cell workbook with 102,003
formulas ([result](calculation-experiment.md),
[evidence](evidence/calculation-experiment.json)):
- Digests of every 64-row block (SHA-256) add 1.3 to 1.8 s to opening; a
  one-cell edit re-hashes its block in 0.73 to 0.83 ms on a 10-column sheet.
- The 100,000-row chained running total recalculates in 3.05 s. The affected
  set for an edit at its top is found in 0.057 s.
- A streamed `SUM` over 100,000 cells takes 0.196 s, against 1.1 to 1.8 s cell
  by cell.
- Two thresholds failed as first prototyped: dependency data needed 131.7 MiB
  (limit 64 MiB) and the derived tables grew the file by 57.5% (limit 25%).
  A compact integer-keyed structure and integer sheet numbers, measured as a
  probe and not as a tested implementation, give 45.7 MiB and 11.0%.
- Finding dependents by SQL query took 41.6 s as prototyped; the cost is its
  range join, and a better query was not measured.
- The passes are specific to the fixture: its ranges cover input columns only.
  A review probe with ranges over a formula column took 4.6 s to find
  dependents in memory. No measured option covers a million formulas.

Not verified: a cold file cache, sheets wider than 10 columns, long text cells
in the digest, Windows and Linux, and anything inside the service. Stages 2 to
5 are not planned or built; calculation in the product is unchanged
(`evaluated_on_read`, with today's limits).

## Large structural edits — November 17, 2026

Chart export and desktop authoring reached `main` in PRs #141 and #143.
Excel fidelity increment 9 adds confirmed background execution for inserting and
deleting whole rows and columns and duplicating and deleting sheets. Qt, REST,
the managed CLI and the SDK use the same operation ID and transaction. The
ordinary path keeps its existing limits. The large path stages coordinates on
disk, streams formula rewrites, maintains native objects and checks cancellation
before committing. [Plan and verification](excel-fidelity-increment-9-plan.md)

## Charts desktop authoring — November 16, 2026

The [authoring design](charts-authoring-export-design.md#4-authoring-on-the-sheet)
(sections 4 and 5), [plan](charts-authoring-plan.md) and signed-off
[wireframes](charts-authoring-wireframes.md) are executed on branch
`claude/charts-authoring`, built November 15-16, 2026 from `main` at 550cbe4
and merged through PR #143. The [verification evidence](evidence/charts-authoring-verification.txt)
records the commands and results. No service, REST, CLI or SDK code changed.
The rulings made during the build and every departure from the design are in
the "As built" paragraphs of design sections 4 and 5.

Built, and covered by the `chart_authoring` tests:
- Wireframes: recorded and signed off by the owner on November 15, 2026, with
  the nine plan decisions and the sixteen owner questions accepted as written,
  before any desktop code. Three panel texts and one field marker added during
  the build are listed in the wireframes' text table as not part of the
  sign-off.
- Chart specification helpers (`desktop/chartspec.*`): the stored form to the
  input form with sources by id, the type change with its companions (grouping;
  categories to and from scatter X sources), placement and resize arithmetic
  within 64x48 to 4096x4096, and the text of a range source.
- On the sheet (`desktop/chartlayer.*`): a chart takes clicks over its area and
  focus by click; one chart is selected at a time, with a 2 px frame and eight
  handles; a press on the grid or a header, or Escape, deselects. Drag to move
  and handle drag to resize, one `chart_update` on release; arrow keys move
  (8 px, 1 px with Alt) and Shift+arrows resize; Delete or Backspace deletes;
  Enter opens the panel; a context menu (Edit Chart, Bring to Front, Send to
  Back, Delete) from the right button, the Menu key or Shift+F10. A refused or
  unsent move or resize puts the chart back. Clicks pass through charts during
  the formula reference pick, the format painter and a Python pick. Accessible
  name "Chart: <name>" and a description from the type, title and series
  count, or the chart's state.
- Insert menu between Edit and Format: Chart (`Alt+F1`), Select Next Chart
  (`Ctrl+Alt+C`), Select Previous Chart (`Ctrl+Alt+Shift+C`). Insert asks the
  suggest route for the selection and sends one `chart_create` anchored one
  column right of the selection (its top-left cell when that column does not
  exist or is hidden); the chart is selected and focused and the panel opens.
  One Undo removes it. Disabled with no workbook, in a read-only workbook, a
  projected list view, the relational workbench, while busy and while a
  suggestion is awaited.
- The side panel (`desktop/chartpanel.*`, `desktop/chartsourcefield.*`), a
  dock titled "Chart" with a View menu toggle: Chart (Name, Bring to Front,
  Send to Back), Type (six types, Grouping), Title, Data (Categories; series
  with Name, Values and X for scatter; Add Series, Remove, Move Up, Move Down),
  Axes (titles, Minimum, Maximum) and Legend. Each source is a range (typed or
  picked on the grid), a table column or a query column. Every change is one
  `chart_update` with a whole `spec`, shown as "Edit chart" in Undo. Refusals
  show the service's message beside the field and put back only that change.
  A broken source holds source edits ("Not applied yet") until one update
  repairs it; an unreadable specification leaves only the Chart group enabled.
- Range picking on the grid: the field fills as the selection changes, on any
  sheet; Enter applies it as one update and Escape puts the field back; the
  chart stays selected; the window returns to the chart's sheet.

- Final fix wave (the whole-branch review, November 16, 2026, commit
  436d8fe): the panel is locked whenever the chart layer has not listed the
  state the window is bound to, whatever caused the bind, so another client's
  change can no longer be reverted by held text; Select Next and Previous
  scroll a chart into view before focusing it, and a chart that cannot be
  shown is passed over with a status; a focused chart scrolled or moved out of
  view, or deleted, gives the keyboard to the grid; a held Delete's repeats no
  longer clear cells once its chart is gone; stored axis titles and series
  names longer than 255 characters are kept whole and only typed text is sent;
  every panel button and list is a Tab stop in the wireframes' order and the
  lists ignore the wheel; whole-number bounds are sent as JSON integers.

Measured on the reference Mac (macOS, Darwin 25.6.0; Qt 6.12.0, as the build
directory's `Qt6_DIR` and QtTest report; system Python 3.14.6), on commit
436d8fe:
- `chart_authoring`: passed, 132 s; 222 passed, 0 failed, 0 skipped.
- `desktop`: passed, 274 s; 282 passed, 0 failed (the known unrelated flake
  `backgroundCalculationReportsSnapshotWithoutEdits` did not occur).
- `python_workspace`: passed, 81 s; 72 passed, 0 failed.
- Service suite: 1,716 tests, OK, 28 skipped, 327 s, with a fresh `TMPDIR`
  (no service code changed on the branch). A first run with the system
  temporary directory had one error, `test_sigint_also_stops_in_order`,
  because 8,203 leftover `datahorizon-<pid>-*` directories from earlier runs
  were there and one matched the new service's pid (see the evidence).
- Build: no compiler warnings.
- Export check: a throwaway SDK script (not committed) sent the operations the
  desktop sends for Insert → Chart over A1:C13 (`suggest_chart`, then
  `chart_create` anchored at D1), a whole-`spec` type change to bar, a
  whole-`spec` title change, a move (anchor F3, offset 12,7) and a resize
  (520x340); all five committed. The workbook was exported to XLSX and ODS
  (inline whole-workbook route, "Charts: 1 written." each) and read back with
  the import previews: in both formats the chart came back equal in name,
  type, grouping, title, legend, categories, series names and values sources,
  anchor, offset and size, with no chart or drawing entry in the import
  report. The two files are in `output/charts-authoring-samples/` (not
  tracked). This exercised the operations the desktop sends, not the desktop
  UI; the UI path is covered by the `chart_authoring` tests, which drive mouse
  and key events through Qt on the offscreen platform.

Known limitations:
- No object browser entry for charts, no type picker with a preview, no copy
  as image, cut, copy, paste or multi-selection, and no workbook Python chart
  operations.
- A broken source's old reference cannot be shown (the engine clears it); the
  field is blank with "This source no longer exists". A dropped table or query
  appears as a blank entry in its list.
- Saved-query column names are read on demand, one `/queries/read` per query
  chosen; the `/tables` catalog carries only a column count.
- Any text field outside the panel taking focus cancels a range pick,
  including the Find dialog's and the Python editor's fields; cancellations
  other than Enter and Escape do not return to the chart's sheet; starting a
  pick does not fill the field from the existing selection.
- The panel is locked and empty until a workbook's first chart listing, and
  stays locked whenever the chart layer has not listed the workbook, sheet and
  state the window is bound to, whatever caused the bind (this window's
  commit, another client's change, Refresh, an operation job, a stale read);
  if that listing fails, it stays locked until a later refresh lists them.
  Text held meanwhile is applied after the listing only when the chart's
  specification is the one it was typed against.
- Axis bounds: a whole number typed is sent as a JSON integer; other bounds,
  and bounds carried unchanged in a whole `spec`, pass through a double, so an
  integer bound beyond 2^53 can lose precision.
- While a change is awaited, any key or mouse press elsewhere in the
  application cancels the return of focus to the panel field.

Deferred minor notes from the build (kept for later work):
- `chartspec.cpp` wraps `tr()` in a helper that hides its strings from
  `lupdate` (there are no `.ts` files today).
- `ChartSpec::resized` does not clamp the axis not being dragged;
  `parseRange` rejects spaces around `:` and `!` and matches sheet names
  case-sensitively; `withType` can carry a stale grouping string;
  `sourceText` shows a raw id for an unknown sheet.
- The wireframe sketches' menu layout and object names were not checked
  against the built window (the object names are pinned by the tests).
- `rangeTextRoundTrips` lacks cases for a quoted sheet name containing `!`, a
  quoted unknown sheet and an unterminated quote.
- Remove's clearing of the series message is untested.
- A suggestion dropped because the window became busy is reported in Issues
  only, not in the status bar. Some negative assertions in the proxy tests use
  fixed waits of 100 to 300 ms.
- The application event filter and the remembered focus linger
  when the chart is gone for good (harmless).
- Pressing another Pick while busy ends the old pick and then shows the issue.
  `~MainWindow` disconnects `qApp` from the window broadly.
- The test helper `choose()` can leave a combo popup open offscreen.

Not verified:
- The acceptance run by hand in the built application with a physical mouse
  and keyboard (design section 7, plan Task 9 Step 2): not run. The automated
  tests drive Qt mouse and key events on the offscreen platform only.
- Opening the exported files in Excel and in LibreOffice: neither is
  installed on the reference Mac.
- VoiceOver; physical-input acceptance; Windows and Linux.
- macOS Option as Alt for the 1-pixel arrow steps on real hardware; `Alt+F1`
  may need the fn key on a Mac keyboard.
- Without a test of their own: the external Python pick's pass-through, the
  drag cancel on window deactivation, and the Shift+F10 shortcut override
  while the chart cannot be changed.
- macOS with the system's default keyboard setting ("Keyboard navigation"
  off): every panel button and list is given `Qt::StrongFocus` so Tab reaches
  it, and the Tab order is tested offscreen, but not checked on Mac hardware.
  The wheel over panel lists (which changed values on Windows and Linux) is
  tested offscreen on macOS, where Qt's combo box takes no wheel anyway.
- `Ctrl+Alt+C` (Select Next Chart) is AltGr+C on some Windows keyboard
  layouts (Polish "ć", for one), where typing that letter in the grid may
  select a chart instead; the shortcut was signed off and needs the owner's
  decision before a Windows release.

## Charts export — November 15, 2026

The [export design](charts-authoring-export-design.md#6-export) and
[plan](charts-export-plan.md) are executed on branch `claude/charts-export`,
merged through PR #141 after the import and view-only work in PR #140.
The [verification evidence](evidence/charts-export-verification.txt) records
the commands and results. [Desktop authoring](#charts-desktop-authoring--november-16-2026)
followed in PR #143.

Built, and covered by service tests:
- XLSX and ODS exports write native charts: the inline whole-workbook and
  selection routes and the streamed whole-workbook export job. A shared step
  (`export_charts.py`) decides which charts can be written and builds one draft
  per chart; `xlsx_charts.py` and `ods_charts.py` write the DrawingML chart and
  the ODS chart object and package them. The six chart types and both groupings
  are written with titles, axis titles, value axis minimum and maximum, legend
  position, series names by cell reference, sources by sheet name, anchor,
  offset, size and drawing order. XLSX also writes literal series names.
- A chart is left out whole, with `chart.skipped` and one of seven reasons:
  `table_source`, `query_source`, `broken_source`, `source`, `unsupported` (a
  stored specification this service cannot read: an unsupported version or an
  invalid specification; a stored chart record that is itself malformed is
  refused when the workbook is opened or refreshed, before any export, as
  before) and `unwritable` (including every chart, when packaging the built
  charts fails; the export continues without them), and `size_limit` (inline
  exports: no room within the size limits). The report says
  `Charts: N written.` or `Charts: N written, M left out; each is listed below
  with its reason.`, and nothing when there are no charts. A selection export
  writes a chart only when its anchor and every source lie inside the
  selection.
- No cached values and no ODS local data table are written. In XLSX a chart
  shows its data once Excel fills the caches on load; in ODS the chart reads the
  sheet's cells directly. No markers or gridlines are written. ODS
  cannot carry a series name given as text: the chart is written, the export
  reports `chart.converted` / `series_name`, and the series comes back "Series
  N". XLSX stacked column and bar charts carry full overlap, and the importer no
  longer reports that case as a dropped `gap width` style.
- Changes after the whole-branch review: collecting charts reads each catalog
  (tables, saved queries, sheets) at most once per export, and a selection
  export collects only its sheet's charts, so table-sourced charts no longer
  time out an export (60 tables and 64 charts of 64 table-sourced series: a
  20-cell selection export went from failing after 2 s to about 0.05 s); a
  stored payload that is JSON but not an object is reported `unsupported`
  (reached only when charts are collected from a database the engine has not
  validated; through the service such a record is refused when the workbook is
  opened or refreshed); ODS
  chart objects say their data comes from the sheet (`xlink:href=".."`, the
  plot area's `table:cell-range-address`); each ODS frame carries
  `draw:z-index` and the importer orders a sheet's charts by it, so stacking
  order survives when anchors disagree with it; XLSX references always quote
  the sheet name; XLSX charts have square corners (`c:roundedCorners` 0); a
  packaging failure leaves every chart out instead of failing the export. An
  ODS scatter chart is not given LibreOffice's points-only marker settings,
  because the importer would report them as a dropped `markers` style.
- Changes in pull request review: chart parts still count against the inline
  exports' 16 MiB expanded and 4 MiB response limits, but an export whose cells
  fit no longer fails because of its charts: the leading charts that fit are
  written and each other one is reported `size_limit` (owner decision; 25 cells
  of 64 KiB of ampersands and 64 charts of 64 series now export to ODS with
  the charts that fit). A bounded package may hold 384 parts (was 128), so
  an XLSX export with one chart on each of 25 sheets (130 parts) reads back
  through the bounded import. An ODS title of several lines is written as one
  paragraph with `text:line-break` and read back. The CLI report names every
  chart left out.
- Import report clean-up: no `workbook.feature` `charts`; `sheet.feature`
  `drawings` only for a sheet whose drawing was not read, holds no chart frame,
  or holds something besides chart frames (an XLSX drawing root element that is
  not an anchor included); ODS `embedded_objects` does not count an object read as
  a chart; `chart_sheets` is unchanged.
- The rules are in the [compatibility contract](compatibility-v0.md#chart-export).
  The OpenAPI document (`openapi` in `service.py`) did not describe charts as
  omitted from export, so it is unchanged.

Measured on the reference Mac (system Python 3.14.6, no openpyxl or odfpy):
- `tests/test_chart_round_trip.py`: 8 tests, OK. Ten charts of every type and
  grouping on two sheets, exported and imported again through XLSX and ODS, the
  inline and the streamed routes, come back equal in type, grouping, title,
  axes, legend, sources, series names, name, anchor, offset, size and order,
  with no chart or drawing entry in the import report. The committed fixtures
  `charts-two-sheets.xlsx` and `charts-one-sheet.xlsx` import, export and import
  again with the same charts and cells. Three differences from the importer's
  own rules are pinned, one test each: text over 255 characters is cut, duplicate
  chart names on a sheet are made unique, and an empty series name comes back
  "Series N". The eighth: two charts whose anchor order is the reverse of their
  z order come back in z order in both formats and routes.
- The whole service suite, after the review fixes (run on commit 6c05e56):
  1,702 tests, OK, 28 skipped, 363 s. The chart modules: 482 tests, OK, 2
  skipped.
- The desktop suite (this branch changes no desktop code): passed, 255 s.
- Two sample files, `output/charts-export-samples/charts.xlsx` and `charts.ods`
  (not tracked by git), regenerated after the review fixes, hold seven charts
  each (the six types and a stacked column chart); every XML part of each
  parses and both import again with the seven charts.

Not verified:
- Opening the exports in Excel (not installed).
- Opening them in LibreOffice (not installed). The ODS chart data binding
  (`xlink:href=".."`, the plot area's `table:cell-range-address`) and
  `draw:z-index` were written from the format's documentation and LibreOffice's
  known output, and are unverified until a file is opened there.
- The owner's sample file `Column Chart_Complete.xlsx` (not on the machine).
  The two sample files are there for the owner to open.
- openpyxl and odfpy reading the files: neither is installed, so the tests that
  use them skipped. Windows and Linux. Desktop
authoring and workbook-Python chart operations are not built. (Desktop
authoring was built later, November 16, 2026:
[Charts desktop authoring](#charts-desktop-authoring--november-16-2026); its
wireframes were signed off November 15.)

## Charts import and view-only display — November 15, 2026

The [design](charts-import-view-design.md) and [plan](charts-import-view-plan.md)
are executed on branch `claude/charts-import-view`, merged to `main` through
pull request 140. They
pull chart import and a view-only desktop display ahead of charts stage 3
authoring and stage 4 export. The [verification evidence](evidence/charts-import-view-verification.txt)
records the commands and results.

Built, and covered by service and desktop tests:
- XLSX and ODS import reads chart parts into drafts and converts them to native
  charts (`import_charts.py`, `xlsx_charts.py`, `ods_charts.py`) through the
  staged whole-workbook path (stage format version 2 with a `charts` table in
  the digest), the inline whole-workbook path and the one-sheet path, where
  `edit` takes `charts`. Receipts report `imported.charts`, `changed_charts`
  and `charts_left_out`. The rules and the `chart.converted` and `chart.skipped`
  vocabulary are in the [compatibility contract](compatibility-v0.md#charts).
- Chart, drawing and relationship parts are read against their own 16 MiB
  allowance per import, each distinct part read and parsed once and only its
  extracted body kept, apart from the package budget; past it charts are
  reported `unreadable`, as is a part whose compressed data is corrupt. An XLSX
  worksheet's charts come only from the drawing its `<drawing r:id>` names. At
  most 256 chart frames are read per import, across all its sheets; once 64
  charts are held no further chart part is read; a draft keeps at most 65
  series. Both streaming readers check cancellation and the deadline for every
  frame. Tests bound this work by count (parts parsed, bodies extracted, drafts
  built) for repeated frames, shared drawings and many drawing relationships.
- Series names and axis titles are cut to 255 characters; a converted chart is
  at most 64 KiB as JSON (else `limit`) and one import's charts at most 1 MiB
  (else `workbook_limit`), and client-supplied records past either cap are
  refused, so a chart can no longer fail an import or push a preview past its
  4 MiB limit.
- Pixel sizes use default cell sizes (64x20 px); a sheet that declares custom
  sizes reports `size_approximate` (on ODS, any declared column-width style).
- The desktop lists a sheet's charts through the existing catalog read, renders
  them through chart jobs and shows the service's SVG in mouse-transparent
  overlays over the grid (`chartlayer.h`, `chartlayer.cpp`). A chart anchored
  in a frozen row or column is pinned in that frozen direction and may extend
  over the scrolling cells. Charts are hidden in a projected list view. A chart
  that cannot be drawn shows a placeholder with the reason. A failed chart list
  read is shown in the window's issue line and read again on the next refresh.
  The import dialog carries charts on "One sheet" and "All sheets" and shows the
  chart count in both reviews; the one-sheet import's status after the edit
  counts its changed cells and charts. A render that failed is rendered again
  when the window refreshes without a change. Qt SVG is now a required linked module (Widgets,
  Network, Svg, Test); [development.md](development.md) lists it.
- The service runs one chart job per workbook, so the layer opens one render
  job at a time (`MaxConcurrentRenders` is 1); a start refused because another
  client's chart job holds the slot backs off and tries again.

Measured through the real service on the reference Mac, with the owner's
sample file `Column Chart_Complete.xlsx` (two worksheets, one clustered and one
stacked column chart): the whole-workbook import and each one-sheet import
create the charts, and their resolved series equal the sheet cells. The chart
SVGs are 2,509 and 3,376 bytes. The whole-file conversion report holds seven
`chart.*` entries (dropped colours, data labels, gap width, gridlines and fonts;
`size_approximate`; one `legend_position`) among 18 entries, and still lists `workbook.feature` `charts` and
`sheet.feature` `drawings` as omitted. The full suite result is in the evidence
file.

The run above predates the owner's decision on the `workbook.feature` `charts`
and `sheet.feature` `drawings` entries, now built: an import no longer reports
`charts`, and reports a sheet's `drawings` only when its drawing held something
besides charts or was not read.

Not verified: the application window on the sample file (overlays over the
grid, the import dialog) was not driven in this run; LibreOffice was not
installed, so no cross-format check ran; Windows and Linux. Chart authoring
(Insert Chart, select, move, resize, delete, edit panel, object browser),
workbook-Python chart operations are not built. Chart export was built later
the same day ([below](#charts-export--november-15-2026)). The stage 3 wireframe session is still owed. (Authoring was built later,
November 16, 2026: [Charts desktop authoring](#charts-desktop-authoring--november-16-2026);
its wireframes were signed off November 15.) CLI one-sheet
import does not exist, so only the SDK and the desktop carry charts on that
path.

## Charts table and saved-query sources — November 13, 2026

The [table and saved-query sources plan](charts-table-query-sources-plan.md) is
executed on branch `claude/charts-table-query-sources`, merged to `main`
through pull request 140.
With it, charts stage 2 is complete. A chart's values, categories or x values
can now come from a column of a structured table (`{"kind": "table", "table",
"column"}`) or a column of a saved query's result (`{"kind": "query", "query",
"column"}`), as well as a range, through REST, the managed CLI and the SDK.
Tables, columns and saved queries are held by identity, a query column by
name. Dropping a table, removing a column or deleting a saved query marks the
chart sources that used them broken and lists the charts in the receipt's
`broken_charts`; nothing is repointed. A table is read whole in stored row
order; filtering and ordering go through a saved query. Each saved query a
chart reads runs once per chart job on a private snapshot copy, bounded by the
chart job's deadline and a 16 MiB in-memory cap, and takes no query job slot.
A table or result longer than the point limit is refused at resolve by name.
Suggest works from a table (`suggest_chart(table=...)`, `charts suggest
--table`); there is no suggestion from a saved query.

Measured on the reference Mac only, through the service:
- Over a 10,000-row table, a one-series line chart resolves in 0.12 s and
  renders in 0.18 s (1.18 MiB result); a four-series chart resolves 40,000
  points in 0.15 s.
- A column chart over a 100-row grouping query resolves and renders in about
  0.06 s. On the same workbook padded with 200,000 cells (0.70 MiB to
  13.3 MiB), it takes about 0.17 s; the query's snapshot copy itself grew from
  2 ms to 22 ms.

Also recorded: the engine's 8 MiB calculation input cap applies to a whole
chart job, because one calculator serves the job.

Open: the desktop, workbook-Python chart operations, XLSX and ODS chart
interchange, and Windows and Linux. See the
[result](charts-stage-2-result.md#table-and-saved-query-sources) and the
as-built [design](charts-design.md) (Draft 0.5).

## Charts stage 2 engine and interfaces (range sources) — November 12, 2026

The [stage 2 plan](charts-stage-2-plan.md) is executed for range sources on
branch `claude/charts-stage-2`. That branch was stacked on
`claude/charts-stage-1`; both were merged to `main` through pull request 140. Charts are a `chart` kind in
`dh_objects` at [native format v8](native-format-v1.md#charts-format-v8). They
are created, changed and deleted by undoable operation actions, follow row and
column edits, and are resolved and rendered to SVG by cancellable 30-second
jobs. They are reachable through REST, the managed CLI (`charts`) and the SDK.
The renderer lays text out against a metrics table for the bundled DejaVu Sans
font.

Measured on the reference Mac only:
- A pie of 1,000 slices loads and paints in 41 ms in Qt, so the pie limit is
  1,000.
- An eight-series line chart over 10,000 rows, four of its eight value columns
  formulas, resolves 80,000 points in 2.0 s.
- Column, bar, line, area and scatter charts each render 10,000 formula points
  in under 1 s, with results of 1.2 to 2.1 MiB.
- One render is limited to 10,000 values so that its result stays under the
  4 MiB job limit.

The capacity run found three defects, each fixed test-first: formula sources
plotted as error gaps, renders overflowing the result limit, and Qt SVG
ignoring the bundled font. The whole-branch review found seven more, also
fixed test-first, among them a formula chain across 1,000-cell slices plotting
as error gaps and an insert that could strand a chart past a size limit.

Open: table and query sources, the desktop, workbook-Python chart operations,
XLSX and ODS chart interchange, and Windows and Linux. See the
[result](charts-stage-2-result.md) and the as-built [design](charts-design.md),
which was Draft 0.4 on November 12. The current as-built design is Draft 0.5,
with table and saved-query sources, built on November 13.

## Charts stage 1 renderer experiment — November 11, 2026

The [charts design](charts-design.md) is approved and its stage 1
[experiment](chart-render-experiment.md) has run on branch
`claude/charts-stage-1`, following the
[stage 1 plan](charts-stage-1-plan.md). It is isolated test code: a
standard-library SVG chart renderer, an optional Qt SVG probe and a benchmark.
No chart feature exists in the service or the desktop.

The plan's thresholds were not all met. Qt takes 393 ms to load and paint a pie
of 10,000 slices (limit 150 ms); every other type passes at 10,000 points. Text
widths pass with a metrics table when the viewer's font is the measured one or
narrower, and overflow by up to 18% under a wide substitute font. By the plan's
rule the result is the QCustomPlot fallback; the result document explains why
the author recommended a pie point limit and a bundled font instead. On
November 12, 2026 the owner accepted both, so stage 2 proceeds on the service
SVG route and the fallback is not taken. Open: the stage 2 plan, including the
choice of font and the pie limit's value, and any measurement on Windows or
Linux.

Evidence: [measurements](evidence/chart-render-experiment.json),
[wide-font run](evidence/chart-render-experiment-verdana.json) and
[rendered charts](evidence/chart-render/).

## Excel fidelity increment 1 — November 11, 2026

Increment 1 of the [Excel fidelity programme](excel-fidelity-plan.md) is built
on local branches `claude/excel-fidelity-1a` to `1e` (not pushed), following the
[task plan](excel-fidelity-increment-1-plan.md). XLSX (transitional and strict)
and ODS imports degrade instead of blocking: unsupported cells and formulas are
kept, converted or left out, every worksheet and workbook feature that is not
imported has an entry, and the structured report reaches REST, the SDK, the CLI
and the desktop review. Import modes are `formulas`, `mixed` and `values`. A
lenient reference scanner keeps references maintained inside formulas DataHorizon
cannot evaluate, and the native `unmaintained` kind (format v7) marks the rest.
A large path streams whole workbooks of up to 1,000,000 cells through upload,
staged preparation, atomic apply (into an empty workbook or as additional sheets),
an export job and download, within limits measured on the reference Mac.

Evidence: [verification](evidence/excel-fidelity-1-verification.txt),
[capacity](evidence/excel-fidelity-1-capacity.json),
[corpus scorecard](evidence/excel-fidelity-scorecard.json),
[specification coverage](excel-spec-coverage.md),
[task 19](evidence/excel-fidelity-1-task19.json) and
[task 21](evidence/excel-fidelity-1-task21.json) records. Open: the corpus has no
Excel-saved or LibreOffice-saved workbook (hand-built approximations only), so
the increment's corpus criterion is not met; the rulings and known limitations,
and the default-detection heuristics, are in
[section 15 of the plan](excel-fidelity-plan.md#15-increment-1-as-built-rulings-and-known-limitations).

A final whole-branch review was fixed on `claude/excel-fidelity-1f`
([record](evidence/excel-fidelity-1-final-fixes.json)): the feature scanners'
accumulators are bounded (`LIMIT` for impossible counts, saturated `at_least`
counts otherwise); a workbook imported in `formulas` mode exports by every route
(XLSX writes formulas DataHorizon cannot evaluate as stored and counts them; ODS
writes the value or an empty cell for a formula OpenFormula cannot carry); the
service removes its staging directory on SIGTERM and SIGINT and sweeps marked
directories of dead services at startup; recovery inspection treats degraded
formulas as valid inputs; every result carries `scope_lines`; and `READ_ONLY`
refusals are `rejected`. The 1,000,000-cell XLSX capacity case was re-measured
with the bounded scanner (section 5.2 of the plan).

## November 8 review remediation

The [review remediation plan](review-remediation-plan.md) is implemented and
verified locally in nine ordered increments. The frozen code is on local branch
`codex/review-remediation`; its commit and exact source fingerprint are recorded
in the [verification record](evidence/review-remediation-verification.json).

Record/list/ordinary spreadsheet exports share a protected writer and recheck
native-file aliases after final review. Durable job sidecars use independent v2
execution identities with conservative legacy recovery. Narrow record/Find scans
use indexed probes and nested deadline/cancellation guards. GUI resume binds a
fresh reviewed state, and active-job close cancels, inspects terminal status and
releases the captured handle, retaining private recovery information when cleanup
is unresolved. Grid and record paste share bounded newline/native decoding.
Action-specific OpenAPI schemas/examples and corrected ODS compatibility reports
match the current supported tier.

[Final qualification](evidence/review-final-qualification.json) passes both CTest
suites: 205 Qt checks and 451 Python tests run (19 optional skips, all covered by
separate isolated validator/capture runs). The repaired workflows pass 23 Cocoa
checks at each scale, native accessibility/Tab checks, visually inspected native
screenshots, independent consumers, relocated installed-app/SDK/service smoke and
archive inventory. Native workflow automation uses Qt widget dialogs; a native
NSAlert automation stall is recorded, and physical/native OS dialog acceptance
remains a manual gate.

Measured cancellation is 6.46 ms for a no-output Find scan and 8.42 ms after a
committed batch on the 319.35-MiB large fixture; all 100,000 sorted rows verify and
service peak RSS is about 54.09 MiB for that workload. Fresh-process/warm viewport
measurements and exact scope are linked in the plan. These do not claim true
OS-cold behavior, universal capacity, or a relaxed large-file opening deadline.
Windows/Linux, screen readers, clean-machine runtime/distribution and the other
milestone/release gates below remain open.

## Current result

The [preview qualification batch](preview-qualification-next-ten.md) is now
authorized with a separate branch/PR per task. The user limited qualification
to macOS, deferred Windows/Linux work and selected GPLv3 for every component.
Task 1's hosted macOS suites, experiments, native scales 1/2 and installed smoke
pass at the tested source recorded in [its evidence](evidence/task1-macos-qualification.json).
Physical input, VoiceOver, runtime bundling and installer acceptance remain open.

The Qt Widgets/C++ desktop runs against an authenticated localhost Python REST
service. The shared engine owns SQLite transactions, source typing, formulas,
history, operation receipts and explicitly activated Python handlers. The CLI
and thin Python client use the same REST resources. The GUI has no SQLite access.

Native create/open/read-only, recovery files, automatic storage, first Save and
consistent snapshot copies are implemented. Range operations include paste
(all/values/formats), relative formula translation, bounded Cut/Paste, fill,
numeric series and cell history. Sheet add/rename/duplicate/reorder/delete are
available with documented reference and handler restrictions. Styles preserve
source and types. Bounded whole-row/column insertion/deletion moves stored inputs
and styles and adjusts references across sheets, including absolute components
and range expansion/contraction. The desktop confirms history clearing and
reports newly broken references with locations. Explicit list projections support multikey sorting and filters
over the entire declared scope, stable source-coordinate edits, counts and clear.
Scoped find/replace and UTF-8 CSV are implemented within the declared tier.
One File → Import… command detects supported extensions; File → Export… selects
CSV/XLSX/ODS in its file dialog's Format dropdown. Existing options, conversion
reviews and managed operation safeguards apply. [Unified-dialog verification](evidence/unified-dialogs-verification.txt)
passes 102 Qt checks and 257 Python tests (15 optional-binding skips), native
Cocoa scale-2 routing/overwrite flows and installed-app smoke; see [PR #29](https://github.com/hybridmachine/DataHorizon/pull/29).

The Supplies arithmetic is verified: amounts 7, 5, 6; total 18; Pen quantity 6
produces 7.5 and total 20.5. Quantity >2 returns Pen and Folder. Reopen retains
formula source; values export contains 20.5. Formatting renders 20.50.

Whole-workbook XLSX/ODS now preserve supported cross-sheet formulas, sheet names,
order and empty sheets. Import replaces an empty destination in one managed
operation; activated handlers must explicitly opt into the transactional structural
event contract. XLSX shared formulas and explicit ODS cell-style parent chains have
bounded expansion/resolution and independent producer/consumer fixtures. Shared
formulas expand only stored members, so an extent may span the grid; the
streaming reader bounds the groups open at once (65,536 per sheet), not the
sheet total. Read-only
recovery inspection reports bounded input/style/formula-syntax issues without
running code or repairs. The [preceding workbook batch](workbook-next-five.md) records scope,
local qualification/archive tooling and remaining platform/distribution gates.

The [ten spreadsheet increments](spreadsheet-next-ten.md) add thirteen shared
scalar functions (aggregate, logical, IFERROR and decimal rounding tiers), native
percentage formatting with XLSX/ODS mapping, atomic Clear Formatting/Clear All,
independent Fill Down/Right, bounded address ranges, scoped Find Next/Previous
and stored-selection count/sum/average through REST/SDK. Errors, exact input
kinds, transactional hooks and existing size limits remain explicit. Local batch
verification is recorded in [the final evidence](evidence/spreadsheet-batch-verification.txt);
Windows/Linux, accessibility, clean-machine/runtime and distribution gates remain open.

The [editing/list usability batch](editing-lists-next-ten.md) adds bounded
formula-aware Cut, canonical cross-pane selection, first-column and selected
prefix freezing (up to 16 rows/8 columns), stored-input boundary jumps, eight-key/
eight-filter typed editing, ordered filtered values copy/export, local named view
definitions and state-checked Find pages across the full bounded scope. These
remain shared-engine/REST operations and local presentation preferences, with
explicit formula/reference, snapshot, draft, visibility and late-reply limits.
[Batch verification](evidence/editing-batch-verification.txt) records the final
local candidate; this does not qualify Windows/Linux, screen readers, physical
interaction, clean-machine runtime or distribution.

The [formula editing and coverage batch](formula-next-ten.md) adds a shared
function catalog/help, source and cell completion/hints, draft reference picking,
reference/parenthesis cues, scalar text/substring/concatenation functions,
conditional aggregates, typed exact XLOOKUP and snapshot-checked direct syntactic
tracing. Shared resource, type, hook and XLSX/ODS compatibility contracts remain
explicit. [Local verification](evidence/formula-batch-verification.txt) records
this batch; broader formula parity and platform/release gates remain open.

The [follow-on formula coverage batch](formula-coverage-next-ten.md) adds scalar
type/error inspection, lazy IFNA, EXACT, SUBSTITUTE, REPLACE, FIND/SEARCH,
TEXTJOIN, aligned COUNTIFS/SUMIFS and scalar INDEX/exact MATCH. The native editor
executes every catalog example; package reimport recalculates the same examples,
with independent XLSX/ODS consumers and transactional hook rollback fixtures.
[Final local verification](evidence/formula-coverage-batch-verification.txt)
records the frozen candidate, installed relocation and archive. These remain
explicit bounded native tiers, with the full M0–M8 completion criteria unchanged.

The [editing and formatting usability batch](editing-formatting-next-ten.md)
adds F4 reference cycling, cross-sheet draft reference picking, trace navigation,
a unified Format Cells dialog, 0–15-place precision, invariant grouping, explicit
currency labels, portable fonts/RGB text color, individual border edges and a
one-shot range-pattern format painter. Style extensions preserve input types
and formula sources across shared managed edits/history and bounded XLSX/ODS
mappings with explicit loss reports. State-checked style-only REST/SDK snapshots
avoid input evaluation for painter copies. [Final local verification](evidence/editing-formatting-batch-verification.txt)
records the frozen candidate and package checks; all existing M0–M8 and platform/
manual/release completion gates remain open.

The [date/time batch](date-time-next-ten.md) adds strict native date/time/datetime
inputs with transactional v2 upgrades, explicit entry across managed clients,
invariant Format Cells categories, ten calendar/clock functions, a shared UTC
calculation clock, civil arithmetic and typed list ordering/filters. CSV companions
and bounded XLSX/ODS ISO/serial mappings preserve the documented temporal tier.
[Local qualification](evidence/date-time-batch-verification.txt) records this batch;
named-zone/DST parsing, wider Excel coercion/format parity and platform/release gates
remain open. Explicit deterministic locale profiles are covered by the following batch.

The [M3–M5 completion increments](m3-m5-next-ten.md), delivered as PRs #95–104,
add a bounded dependency/result cache, generation-bound background calculations,
opt-in transactional structural events, native v3 portable views and declared-record
UUIDs, reviewed filtered record edits/formula copy/export, explicit locale conversion
and durable cancellable jobs. Shared REST/SDK/CLI operations and Qt controls retain
the managed/direct boundary. [Consolidated evidence](evidence/m3-m5-batch-verification.txt)
records local qualification and measured limits; the milestone ledger below remains
a development-tier assessment, not cross-platform release acceptance.

## Relational feature batch

The ten tasks in the [structured tables and query workbench plan](relational-workbench-plan.md)
are implemented for the bounded local tier. Native v4 tables support reviewed
selection conversion, typed schema/constraints, stable record identities, atomic
managed edits and shared record/cell history. Full-table sort/filter uses indexed,
authenticated keyset pages; native RESTRICT relationships have explicit orphan
inspection/repair. Required transactional hooks run through REST, GUI, CLI and
SDK. Direct SQLite edits refresh without replaying historical handlers.

The Qt workbench includes a Sheets/Tables/Queries browser, typed record editors,
relationships/index review and a read-only SQL pane. SELECT/CTEs, joins,
aggregates and typed named bindings run on private snapshots with complete
bounded disk spools, cancellation and stale-result protection. Native saved
definitions retain explicit defaults/dependencies/shape without executing on
open. Complete table/filter and frozen query exports use protected publication;
the initial CSV/XLSX/ODS tier caps output at 10,000 values including headers and
rejects oversized output without truncation. SQL writes remain deferred.

[Final integration evidence](evidence/relational-task10.json) records 232 Qt
checks and 571 Python tests (19 optional skips, covered by isolated validator and
capture runs). All 571 Python tests also pass using installed service code and
the pinned bundled CPython 3.13.18 runtime. Relocated installed GUI/CLI/SDK/service
checks cover Customers/Orders, required-rule rollback, generated edits/Undo,
independent edits, orphan repair and complete exports. CSV/openpyxl/odfpy verify
the two baseline records and total 56. Cocoa relational checks pass at scale
requests 1/2, with physical input and spoken accessibility still separate.

On an Apple M1/8-GiB host, the 343.17-MiB native fixture has 300,000 records,
exceeding the 256-MiB service target. All 37,504 filtered IDs match independent
SQLite over 147 pages; indexed REST pages take 3.85–5.17 ms. Native warm page
changes stay below 75 ms, input/heartbeat samples below 50 ms, and cancellation
below 180 ms. Managed edit samples take 1–3 ms with/without an executing query.
Service peak RSS is 48.41 MiB; GUI peaks without screenshots are 170.97/251.34 MiB
at actual Retina ratios 2/4. Capture-inclusive peak at ratio 4 is 332.39 MiB and
is not a pass for the ordinary GUI memory target. Full private snapshot disk
cost is 343.17 MiB per retained job in this workload, in addition to spool/temp
storage; the actual 64-MiB spool-limit probe rejects with no successful partial
result. These are fresh-process/warm-cache observations, not true OS-cold,
eight-workbook, worst-case temporary-disk or universal capacity qualification.

The [manual guide](relational-workbench-manual.md) and local handoff identify the
exact installed app, fixture, rule and artifact hashes. Owner GUI/VoiceOver,
Windows/Linux, clean-machine distribution and existing M1/M7/release gates remain
open; this batch does not close full M8.

## Workbook Python automation batch

The [six-increment automation plan](python-automation-plan.md) implements native
v5 source modules and explicit scalar macro declarations, service-local approval
of an exact workbook/code fingerprint, atomic macro execution, bounded diagnostics
and REST/OpenAPI/CLI/SDK support. Opening/inspection/copy never executes modules.
Required externally activated code/data handlers retain their transactional
contracts. Macro-generated sheet/table edits and hook-generated edits share
rollback and one supported Undo/Redo entry. Durable receipts preserve scalar
results and diagnostics without rerunning Python after an unknown response.

The worker uses ordinary Python module globals and a shared transaction broker;
its two-second spawn/import/execution/broker budget sits inside the five-second
operation budget. Cancellation reaches active writers without queuing behind
path locks. Attached CLI cancellation uses an existing lease rather than opening
a new handle. UTF-8 log tails/tracebacks, frames, typed arguments/results and
module catalogs have explicit bounds. Workers contain crashes/timeouts but are
not a secure sandbox; file/network effects cannot be undone by SQLite rollback.

The [guide](python-automation-guide.md) supplies a reviewed example, exact
operator activation, CLI/SDK usage and a shared-service GUI walkthrough. The
example exercises a required author-defined rule, generated edits, result
lookup, Undo/Redo and rejection with complete rollback. Final results and artifact
identity are in [the integration evidence](evidence/python-automation-task6.json).
Qt code editing, automatic workbook event bindings, custom formula functions,
package installation, durable external-effect jobs and production remote access
retain separate contracts. Physical GUI/VoiceOver and existing platform/release
gates remain open.

## Python workspace batch

The [workspace design](python-workspace-design.md) is implemented on local
branch `claude/python-workspace`, which is stacked on the unmerged relational
and automation branches. No pull request has been opened and no hosted CI has
run for it.

Native v6 adds two inert catalog kinds. Names label one cell or rectangle by
sheet identity, move with row/column changes, can become broken, and are
limited to 256. Event bindings attach a top-level workbook function to
`cell_change` (sheet, range or name scope) or `table_change` (table scope),
up to 16, and are covered by the code fingerprint; a workbook without bindings
keeps its previous fingerprint. Approved bindings run inside every managed edit
in scope, after the external handler, and reject the whole edit with
`BINDING_FAILED`/`BINDING_TIMEOUT`; required bindings that cannot run reject with
`BINDING_NOT_APPROVED`/`BINDING_UNAVAILABLE`, optional ones are skipped with a
notice. Structural rewrites do not call them. The broker context accepts
sheet-qualified addresses and names (`get`, `value`, `set`, `values`). A
service started with `--owner-channel` accepts approval lines from its parent
over standard input only. Names and bindings have REST/OpenAPI, CLI and SDK
operations.

The desktop adds Tools ▸ Python workspace: a separate window with modules,
an editor with find and bounded completion, macro declarations, Pick from
sheet, a Names panel, Review & approve, Run/Output/Events tabs, and protected
local drafts. The address box shows and accepts names and Data ▸ Name manager…
manages them. Rejected grid edits keep their draft and name the rule.

The final fix wave (November 11, 2026) reports rules broken by a name delete,
a table drop, or a structural edit or sheet delete that breaks a name in the
receipt's `broken_bindings` and in Issues, and the name-delete confirmation
lists the rules scoped to the name. New R1C1-style names are rejected. Main
window commands that could change the workbook are disabled while Pick from
sheet is armed. Selection XLSX/ODS/CSV export reports list names and rules as
native-only omissions. The `name_save` schema no longer requires `reference`
for a rename.

[Task 16 evidence](evidence/python-workspace-task16.json) records 708 Python
tests (22 optional skips), 232 desktop and 70 workspace Qt checks, package
install and smoke, and the [Supplies example](python-workspace-guide.md) on
the installed bundled CPython 3.13.18 runtime: total 18 → 20.5, Undo to 18, a
`Below zero` rollback and a rejected -1 edit. A 13-step Cocoa scenario drives
the real workspace window through the storyboard at scale requests 1 and 2 with
synthetic QTest input. The full native acceptance run did not complete in that
session because its existing job-dialog activation check failed; the
storyboard has not been performed by a person in the installed application.

Open: names in formulas and XLSX/ODS interchange; Python custom worksheet
functions; package installation; structure, workbook-open and code events for
bindings; user-controllable binding order; a source/diff view in the approval
dialog; changed-cell highlighting after a run; docking; crash-persistent drafts;
debouncing the tables-view Cancel; listing rules in the table-drop
confirmation. Physical input, VoiceOver, Windows/Linux and release gates remain open.

## Milestone ledger

| Increment | Evidence and scope | Remaining before full milestone closure |
| --- | --- | --- |
| M0 | [Initial contracts](initial-contracts.md), [compatibility](compatibility-v0.md), [native schema](native-format-v1.md), keyboard/state rules and numeric budgets recorded | Resolve deferred cases at their named milestones; no claim that historical PDFs have been regenerated |
| M1 | Qt 6.12/C++17 builds locally; bounded TEMP/native SQLite capture and service/Python execution comparisons with typed changes, transaction/worker failures and receipts; sparse/dense/long-text/large-disk, formula-chain and Qt scroll/input harnesses with local evidence and CI artifact collection | Production framework/runtime and capture-binding adoption, cold-cache qualification and native platform performance acceptance; hosted artifacts require inspection |
| M2 | Real GUI→REST→SQLite edit, shared CLI/SDK behavior, transactional handler fixture, rejection/timeout, receipts, direct access, stale writes, schema stop, API explorer, client handle lifecycle; restart/reopen and warned unknown-outcome recovery | Broader failure injection, clean-machine packaging evidence and service lifecycle hardening |
| M3 | Typed entry/date functions and scalar contracts; bounded transitive dependency reuse/invalidation; state/generation/UTC-clock background calculations with pending/error/stale/cancelled results; transactional hook reads; bounded automatic visible/frozen viewport scheduling | Broader Excel functions/coercion/reference compatibility, native usability/accessibility and platform acceptance |
| M4 | Typed/formula-aware copy, Cut/fill/history, supported sheet/row/column transforms; opt-in transactional before/after structural hooks; native v3 portable view settings; local hiding/freezing and cross-pane selection | Full Excel reference compatibility, broader presentation/view models, native platform conventions and screen-reader acceptance |
| M5 | Portable named bounded list definitions and persistent declared-record UUIDs; explicit external-edit reconciliation; reviewed record bulk edits and formula-preserving filtered copy/XLSX/ODS export; explicit locale/temporal conversion; durable staged atomic/batched jobs, frozen global sort/find paging and cooperative cancellation | Wider locale/timezone/duration policies, larger scopes and performance qualification (including large-file opening budget), full SQL/query/relational models at M8, production job scheduling and platform/manual acceptance |
| M6 | Native snapshots/recovery, bounded opening integrity checks and read-only inspection; conservative external refresh; bounded CSV/XLSX/ODS with conversion reports, local formula/style tiers and process-crash fixtures; Excel fidelity increment 1: degrade-and-report XLSX/ODS import (strict namespaces, `mixed` mode, every unimported feature reported), large staged import/export at measured limits (1,000,000 cells on the reference Mac), corpus and scorecard ([verification](evidence/excel-fidelity-1-verification.txt)) | Excel-saved and LibreOffice-saved corpus; native features of fidelity increments 2–8 and 10; broader XLSX/ODS application fidelity, comprehensive disk/permission/power-loss recovery, bundled runtime, follow-up execution semantics |
| M7 | Automated local macOS tests and visual screenshot; local install smoke; hosted Windows/macOS/Linux build/test/install smoke passes after deployment-path fix | Native platform interaction, screen readers, high DPI, clean-machine installers, signing, compatibility matrices and distribution/license decisions |
| M8 | Bounded native tables/relationships/indexes, read-only SQL and saved views; native v5 modules, exact service-local activation, atomic macros, required hooks/history and managed clients; native v6 names and cell/table event bindings, owner-channel approval and the Qt Python workspace (local branch, no PR) | Proven managed SQL writes, names in formulas/interchange, structure/open/code binding events, custom functions/packages, durable external effects, remote TLS/mTLS/password access, physical-input/VoiceOver acceptance of the workspace and remaining foundation/platform qualification |

Later-stage facilities in this preview are bounded development slices. They do
not waive the dependencies or completion criteria in the implementation plan.

## Evidence

Build: CMake 4.4.2, Apple Clang 17, Qt 6.12.0, Python 3.14.6, Apple Silicon,
macOS 26.6.2. Sources build with compiler warnings enabled. Tests exercise
actual REST and child processes; no substitute in-process GUI database path is
used. The automated suite includes:

- Atomic multi-cell validation, hook-generated edits, required load/timeout
  failures, rollback, no-op change coalescing, Undo/Redo and source retention.
- Durable operation deduplication/status, external source edits without handler
  replay, stale-write rejection, schema/trigger rejection, read-only access and
  consistent snapshot copies.
- Formula errors/recovery, lazy IF, absolute/cross-sheet references, bounded
  distant reads and long-text/calculation/export limits.
- Range offsets, overlapping moves, rejected Cut destination retaining its
  source, numeric series, sheet duplication/order, source-preserving styles,
  full list scope, projected targets and scoped Replace All.
- REST/SDK/CLI parity with the same required handler, authentication/origin
  rejection, CSV preview, headless file CLI and independent client handles.
- Qt model invariants on a bounded instance; actual Supplies edits, source-bar
  Save/Escape, Undo, formatted results, projected source targets, far-address
  navigation, required-hook rejection and a closable startup failure.

The original review regression evidence includes 127 Python tests and 68 Qt contract cases.
[Python test output](evidence/service-tests.txt),
[desktop test output](evidence/desktop-tests.txt),
[desktop process resources](evidence/desktop-resources.txt), and
[rendered Supplies window](evidence/desktop-supplies.png) are saved locally.
The [combined CTest result](evidence/verification.txt) and
[installed native-app smoke result](evidence/package-smoke.txt) are also recorded.
The screenshot uses Qt's offscreen platform and does not certify native platform
appearance. Qt warnings about offscreen size hints are expected test output.

[Benchmark JSON](evidence/local-benchmark.json) records a 390,001-cell dataset:
100k sparse cells, 10k dense cells and 280k one-KiB text cells. Its SQLite disk
size exceeds the declared 256 MiB service memory target. Warm/distant/dense/disk
viewports and edit acknowledgement use 100 authenticated REST requests each.
The report includes driver/service peak RSS and explicit missing measurements.
These results do not promise an unlimited workbook or every dense address-space
shape. The cache has both tile-count and estimated-byte limits; memory targets
apply to the measured workload, not all possible active workbook combinations.

`cmake --install build --prefix build/package` deploys Qt and service sources.
`python3 tests/package_smoke.py build/package` exercises a real installed native
application, managed edit and calculation using an ephemeral workbook. Python
remains separately installed. This is an install smoke, not clean-machine or
distribution qualification. CI describes Windows/macOS/Linux build/test/install
jobs. The first hosted green matrix is recorded in
[PR #1](https://github.com/hybridmachine/DataHorizon/pull/1), following commit
08d1d81's absolute Qt deployment prefix correction.
The installed smoke uses the bundled native platform plugin (Cocoa here);
Linux CI provides a display with Xvfb. Unit/interaction tests use offscreen Qt.

## Next dependencies

The [spreadsheet increments](spreadsheet-next-ten.md) through PR #44 and the
[editing/list usability batch](editing-lists-next-ten.md) through PR #54 are complete
within their declared development tiers, each with local verification and a
separate reviewed/merged PR. The [following M3–M5 batch](m3-m5-next-ten.md)
implements bounded dependency/background calculations, hook-aware structure, portable
views/record identity, filtered formula operations, explicit locale/temporal conversion
and durable large-operation jobs. Broader compatibility, scale and production models
remain in the ledger. Native keyboard/accessibility
and Windows/Linux qualification remain required before a cross-platform release.

The [workbook compatibility/recovery/qualification increments](workbook-next-five.md)
now add bounded whole-workbook XLSX/ODS with atomic empty-destination import and
consistent multi-sheet export, XLSX shared-formula expansion, ODS parent-cell-style
resolution and independent producer/consumer fixtures. Recovery inspection now
reports bounded input/style/formula-syntax issues; real SQLite capacity/read-only
and Qt atomic-save crash fixtures cover additional failures. The local qualification
driver adds native scale-1/2 automation, relocated install/missing-service checks and
an inventoried development archive. Windows/Linux/manual/clean-machine/signing and
runtime distribution gates remain open. Local verification is authoritative while hosted
Actions credits are exhausted; Windows/Linux and manual acceptance remain open.

The [next interchange/recovery increments](interchange-next-five.md) begin with
cancellable, bounded CSV preparation jobs, XLSX input/formula/style and ODS
interchange, plus bounded native integrity/inspection and process-crash recovery. Progress and result discard precede
an explicit atomic managed Apply; no chunked commits or larger limits are claimed. Each increment was locally
verified and reviewed before sequential merge; hosted platform qualification
remains blocked. [Final local verification](evidence/interchange-verification.txt)
passes 257 Python tests without optional-binding skips, 94 Qt checks, Cocoa
scales 1/2, native interchange/recovery flows and installed-app smoke. See the
conversion tiers and remaining gates in that ledger.


The next [five editing and CSV increments](editing-csv-next-five.md) continue
everyday features. Sheet rename now retargets parsed qualified name references
while preserving stable sheet identity, styles and literal Text. Unsupported
syntax, limits and active required handlers reject atomically; history clearing
and the remaining structure-event contract are explicit.
Sheet duplication now preserves other-sheet targets and retargets explicit
self-references, including stable UUID references, in the copy alone. It
preflights at most 10,000 stored cells/8 MiB before creating the new sheet.
Sheet deletion now atomically removes bounded stored cells and permanently
breaks surviving name/UUID references, reporting the affected formula locations.
Deleted and rewritten cells share the ordinary operation budget.
CSV import now offers strict encoding and delimiter controls with service-side
byte decoding and bounded parsed previews. Explicit Number/Boolean and date-to-ISO
Text column choices now support header preservation, conversion errors and
ambiguity reports. Reviewed typed imports use one managed edit with history and
required hooks; failed conversions disable Apply until corrected explicitly.
[Final local evidence](evidence/editing-csv-verification.txt) passes 227 Python
tests without skips in the optional-binding environment and 89 Qt checks, Cocoa
scales 1/2, native typing/correction flows and installed-app smoke. Hosted jobs
still do not start; platform qualification remains open.

The user prioritized [five everyday spreadsheet increments](everyday-next-five.md)
after PR #13. Text wrapping and all-cell borders now use native managed styles, with bounded loaded
viewport measurement. Row/column dimensions now have local per-workbook/sheet preferences, source-row
projection mapping, bounded visible-text fit and reset controls. Hide/unhide adds
local source-coordinate preferences, header gap markers, persistent recovery
controls and hidden-rectangle guards. Freeze top row adds an editable, shared-cache
row view with synchronized headers/columns, keyboard navigation and saved local
state; projected lists and hidden row 1 temporarily suspend it.
[Final local verification](evidence/everyday-verification.txt) passes 198 Python
tests in the optional-binding environment, 82 Qt checks, native Cocoa scales 1/2,
the existing grid benchmark and installed-app smoke. Native scale 1/2 keyboard, editor accessibility and modal
cancellation checks also pass locally; hosted platforms remain pending.

The [five qualification increments](qualification-next-five.md) now implement
native-platform automation, cache/RSS measurement, HTTP admission, worker
lifecycle hardening and an optional native capture-binding comparison. Native Qt
acceptance now has an executable that checks the real REST-backed window at
scale factors 1/2 with keyboard/clipboard, accessible names/extent and geometry
checks. CI runs native Cocoa/Windows/Xcb plugins and collects JSON/screenshots.
Physical input, screen-reader navigation and native dialogs remain unqualified.
The cache/RSS increment adds 100 fresh-process and warm REST measurements over
a generated 1.27-GB workbook, per-process service peaks and GUI peak RSS. See
[the measured scope](qualification-next-five.md#cache-and-rss-evidence).
OS-cold caches remain unqualified. Hosted checks are currently unavailable due
to account billing/spending limits; the user authorized local verification and
sequential merges until that capacity is restored.
HTTP admission now bounds handler concurrency, total slow-request reads and
aggregate headers, with real-socket saturation/recovery tests that preserve
durable operation outcomes. See [the contract](initial-contracts.md#jobs-limits-and-evidence).
Worker lifecycle hardening now closes process/pipe resources on startup and
protocol failures, bounds JSON broker messages, and keeps blocked pipe I/O off
the transaction thread. Repeated crash/recovery checks preserve hook rollback
and receipt semantics; [scope and limits](qualification-next-five.md#python-worker-lifecycle).
The optional [native capture comparison](native-capture-binding.md) adds APSW
preupdate images, bounded failure/rollback and direct-access evidence alongside
the TEMP journal. The preview keeps stdlib SQLite; managed SQL remains disabled.

The five bounded M1 experiment slices, including the
[toolchain/packaging baseline](toolchain-packaging-baseline.md), now have
repeatable evidence. Next are the remaining native-platform acceptance,
OS-cold cache and broader RSS qualification, plus production framework/runtime
and capture-binding adoption decisions. The bounded
[Qt grid/frozen-row display](grid-frozen-row-experiment.md),
[native storage](native-storage-experiment.md),
[SQLite capture](sqlite-capture-experiment.md) and
[service/Python execution](service-execution-experiment.md) comparisons are recorded below.
Finish the remaining M1 experiments and native platform acceptance. Extend the
bounded M4 structural tier to additional operations and hook-aware events only
with explicit contracts and destructive-operation fixtures. Then finish list/CSV typing and explicit jobs,
followed by independent XLSX/ODS fidelity stages and failure recovery. Full
desktop acceptance and licensing/runtime distribution decisions precede a
public preview. M8 remains a later set of independently designed increments.

## Foundation benchmarks and bounded structural editing

The new [formula-chain evidence](evidence/formula-benchmark.json) records 100
fresh REST reads per chain length: p95 0.664/1.169/1.800 ms at 16/64/128 cells,
with the 129-cell chain producing the declared #LIMIT! result. Editing a seed
and reading fresh results measured p95 2.813 ms. These are synchronous snapshot
calculations, not evidence for background calculation or a dependency index.

The [native Cocoa measurements](evidence/desktop-benchmark-native.json) record
100 scrolling viewports (p95 32.131 ms), 100 distant jumps (p95 30.278 ms), and a
14.674-ms maximum observed event-loop gap. Synthetic address input was dispatched
during reads. [Offscreen evidence](evidence/desktop-benchmark-offscreen.json) is
also retained for comparison with the CI harness. These runs do not qualify
screen readers, physical input/compositor latency, high DPI, cold OS caches,
memory on every workload or native Windows/Linux interaction. CI collects
formula and offscreen Qt JSON artifacts on all three supported systems.

The scroll harness reproduced a viewport that never finished loading beyond the
16-tile cache limit. Arbitrary-key eviction could repeatedly remove another
visible tile. Minimum per-tile charges now let QCache enforce both count and
byte limits with its LRU policy. The scrolling regression fails with the previous
policy and passes with the fix.

The row/column engine, REST schemas, SDK/CLI parity and four Sheet menu commands
share the [structural contract](initial-contracts.md). Verification covers
absolute/cross-sheet/UUID references, inherited and reversed ranges, NULL/Text
and styles, durable deduplication, unsupported syntax, grid overflow, deadlines,
injected write failure and confirmation-state races. The formula range tests
compare deletion shapes against independently enumerated surviving cells.
Current checks pass 146 Python tests and 71 Qt contract cases (73 Qt checks
including suite setup and cleanup), plus the installed native-app smoke.
Active required handlers and projected structural edits remain explicitly
unsupported; successful structural edits clear history. See the
[increment verification](evidence/foundation-verification.txt) for current
checks. M1 and M4 remain open for the ledger's remaining criteria.

The PR #3 review reproduced parenthesized range endpoints losing inherited sheet
scope on insertion and becoming broken scalars instead of contracting on deletion.
Structural transformation now walks the calculator's parsed ranges with optional
source offsets, retaining parentheses and spelling for surviving references and
replacing wholly broken ranges with one valid `#REF!` expression. Regressions
cover both axes, reversed and nested endpoints, sheet UUIDs, calculated totals,
warning counts and reopen; the deletion oracle covers 8,232 overlap/parenthesis
cases. Non-reference range endpoints reject with a durable rollback receipt.

Managed SQL support matrix for this tier: SELECT/DDL/DML/PRAGMA/ATTACH/transaction
control through an application SQL resource are all unavailable. No statement
is silently run outside hooks. Ordinary external SQLite cell changes are tested
and intentionally supported; reserved schema/metadata changes are rejected on
managed refresh. Proving capture for triggers/cascades/upsert/replace is still
required before the first managed SQL mutation can be exposed. The bounded
experiment below supplies initial capture evidence; engine integration remains
unqualified.

## M1 SQLite capture comparison — November 3, 2026

The [isolated experiment](sqlite-capture-experiment.md) compares explicit
known-target stdlib edits with a TEMP trigger journal. Its 18 SQL cases replay
typed old/new images against independent before/after snapshots, including
WITHOUT ROWID/composite keys, upsert/replacement, triggers, cascades and CTEs.
Sixteen contract tests verify atomic failure, deferred COMMIT failure,
cancellation, savepoints, event/byte limits, reserved-state protection and
callback-free independent access. Snapshot copies omit the connection-local
journal. [Local JSON evidence](evidence/sqlite-capture-benchmark.json) includes
100 warm in-memory samples, compile options and typed images; CI now collects
the same report on each platform. Hosted results remain uninspected.
[Full local verification](evidence/sqlite-capture-verification.txt) passes
162 Python tests and 73 Qt checks including suite setup/cleanup.

Executable negative controls show RETURNING/changes() missing cascade rows and
replacement deletes missing when recursive triggers are disabled. Keep explicit
engine operations for the preview. The journal is only a candidate for a
restricted SQL prototype; native preupdate/session bindings, arbitrary schemas,
Python hooks, history, receipts and result limits need separate qualification.
Managed SQL stays unavailable. M1 remains open.

## M1 service/Python execution comparison — November 4, 2026

The [second experiment](service-execution-experiment.md) sends identical bounded
engine operations through threaded HTTP and serial WSGI, and compares spawned
brokered workers with an inline fast-code baseline. Both transports commit
generated edits atomically and retain durable rollback receipts after handler
exceptions, process exit and timeout. Disconnect/reopen reconciles committed
receipts without rerunning hooks. Authentication rejects before mutation.
[Local timing/outcome evidence](evidence/service-execution-benchmark.json) is
repeatable in CI; [Python verification](evidence/service-execution-verification.txt)
includes three new contract tests. Keep the current threaded localhost service
and spawned broker for this development tier; inline cannot provide worker
containment. Public job cancellation and production framework/runtime choices
remain open. PR #4's capture artifacts were also inspected on all three hosted
platforms before its merge; its duplicate macOS suite timeout passed on retry.
The next PR reproduced that whole-suite macOS timeout without assertion
failures while an identical run passed. macOS now has the same 180-second
desktop suite budget as Windows; individual operation/hook deadlines are
unchanged. The Python suite passes all 165 tests locally.

## M1 native representation validation — November 4, 2026

The [storage experiment](native-storage-experiment.md) verifies typed inputs,
formula-source/derived separation, styled blanks, stable identities, snapshots,
external insertion/refresh without handler replay, durable stale-write rejection
and unsupported version/schema boundaries. Four 10,000-cell fixtures compare
native WITHOUT ROWID and isolated rowid layouts with sparse/dense addresses.
[Local JSON](evidence/native-storage-benchmark.json) records sizes, query plans
and 100 warm reads; the [Python suite](evidence/native-storage-verification.txt)
includes two new contract tests. Keep native format v1 unchanged. Advanced
storage, migration, cold-cache, RSS and production durability remain unqualified.
CI collects equivalent platform reports; PR #5's Linux/macOS worker failure and
receipt artifacts were inspected before its fully green merge.

## M1 Qt grid/frozen-row display experiment — November 4, 2026

The [grid experiment](grid-frozen-row-experiment.md) extends the existing Qt
harness with 100 frozen-header/body scroll samples, horizontal alignment,
column/row resizing, narrower windows, synthetic input and zero-edit checks.
It shares the real model/cache and reports live charges, request counts and
screenshots. The read-only probe remains outside the installed app; production
freeze interactions and persisted view state remain M4 work.

Screenshot inspection reproduced a display probe moving offscreen; a sibling
view and visibility regression correct it. A real-service input regression also
reproduced background tile replies overwriting the address being typed. The
desktop now preserves that focused draft until valid submission. The model,
visibility and input regressions accompany full-suite verification. See
[native](evidence/grid-benchmark-native.json),
[offscreen](evidence/grid-benchmark-offscreen.json) and
[verification](evidence/grid-experiment-verification.txt) evidence. M1 still
requires its toolchain baseline and remaining native/cold-cache qualification.

## M1 toolchain/packaging baseline — November 4, 2026

The [baseline](toolchain-packaging-baseline.md) records C++17/Qt Widgets, CMake
3.24+, Python 3.11+ and the current native build/test/install arrangement.
CMake emits actual compiler/generator/Qt/interpreter metadata; the report
verifies it against runtime/grid artifacts and generated test budgets. A
[local report](evidence/toolchain-benchmark.json) records the working host.
New Python 3.11 jobs execute the complete Python suite on all three hosted
systems and retain actual runtime/SQLite versions. Desktop jobs retain Qt
6.8.3/Python 3.13. PR updates and main pushes run one matrix each, with superseded
runs cancelled. The first four PR cycles were reviewed, verified and merged;
their M1 evidence remains bounded and does not close native/release acceptance.
Python bundling, clean-machine installers, signing and other component licenses
remain open. All PR #7 platform grid artifacts were inspected before merge.
The new explicit Windows interpreter path exposed C++ escape sequences when
embedded in the desktop launcher. Normalize native separators for the compiled
definition; keep the selected interpreter for CTest/SDK. A focused startup
contract now passes in six seconds. The remaining silent suite timeouts are
budgeted separately: a passing full Windows desktop suite took 171.77 seconds
after two 180-second timeouts, so its whole-suite limit is now 300 seconds.
Operation, hook and request deadlines remain unchanged; macOS/Linux suite
budgets are unchanged. Failure artifacts and the focused startup check remain.

## Review corrections — November 1, 2026

All 16 initial review findings have targeted fixes and regression coverage:
CSV previews capture and validate their original target; service crashes permit
restart/reconciliation or warned exit with a durable client record; editors
preserve long sources and drafts; Windows recovery paths normalize separators;
Cut rejects special paste modes; malformed/truncated receipts retain operation
IDs and block further writes; CLI apply accepts the JSON sheet; receipt paths
encode/decode opaque operation IDs; style-only changes preserve blank inputs
and coalesce events by input; clipboard blanks and formula errors retain their
semantics; CSV numbers round-trip; and blank comparisons remain cell-local.
Late mutation/lookup callbacks are associated with their captured operation ID,
and metadata refreshes cannot overwrite a reopened workbook's handle.

The next 11 review findings also have regression coverage: display replacement
preserves Text and rejects implicit numeric/formula conversion; the initial
command-line workbook opens once; restart preserves drafts at their original
coordinates for explicit review; empty lists have no editor target; Copy/Cut
supports 10,000 cells through a consistent clipboard resource; bounded viewport
previews and state-checked full-input reads keep long cells editable; invalid
CLI addresses produce JSON errors; sheet deletion checks the cell budget before
cascading; extreme references and range comparisons remain cell-local errors;
and the SDK parity test now verifies an actual HOOK_FAILED rollback after refresh.

Additional review cycle 1 addressed seven substantive findings. CSV export
rejects the current native workbook and its aliases; direct SQL honors explicit
read-only mode and returns structured storage errors; receipt deduplication is
rechecked under the writer lock without misclassifying an existing outcome;
new and duplicated sheets append after position gaps; list/search/export reads
refresh external state within their read transactions; CSV limits include the
serialized response and blank-record count. Regressions inject commits at the
receipt-lookup/writer-lock and initial-refresh/read-transaction boundaries.

Additional review cycle 2 published no inline issues, but its summary named
three unpublished concerns. All three were reproduced and fixed: GUI edits and
recovered drafts retain explicit Text typing; range/clipboard byte limits count
the complete JSON envelope and array separators; non-finite numeric literals
become #NUM! before scalar operations can consume them, while lazy IF retains
its selected-branch semantics. Boundary and editor regressions cover these cases.

Additional review cycle 3 also published no inline issues, but named omitted
concerns about overflow, pasted values and receipt consistency. Reproductions
confirmed three defects and targeted fixes: SUM rejects overflowing intermediate
totals before callers can hide them; full paste preserves stored input kinds
independently of Text formatting; failure receipts commit under the original
writer lock after savepoint rollback, preventing another service from claiming
the ID in between. Receipt-storage failure injection verifies an unknown outcome
and transaction cleanup. The first three-cycle limit was reached here; the user
subsequently authorized two more cycles with the same cosmetic-only stopping rule.

Additional review cycle 4 published no inline issues, but named moderate
concerns about asynchronous state, edit recovery and transport errors. Targeted
fixes discard callbacks from replaced transport sessions, retain rejected and
interrupted in-cell drafts for correction/review at their original coordinates,
preserve reviewed state/sheet preconditions through submission, and report HTTP
failures even when their JSON message is empty. Missing receipts return an
unknown outcome with the operation ID; failed SDK lookups retain the pending
operation. Delayed socket replies, HTTP failures, restart/rejection recovery and
stale reviewed-operation regressions exercise these paths.
The CI rerun encountered GitHub's macOS 14 retirement brownout before checkout;
the matrix now uses `macos-15`, following the
[runner retirement notice](https://github.com/actions/runner-images/issues/13518).
That image exposed the upstream
[slow reverse-DNS lookup](https://github.com/actions/setup-python/issues/1223)
in Python HTTPServer startup. The loopback service now binds numerically and
sets its fixed local server name without DNS; a regression denies hostname lookup
during construction. Startup and test timeouts are unchanged.

Additional review cycle 5 named a confirmation-target race despite posting no
inline issues. Desktop operations now capture workbook, sheet and state before
their dialogs and retain those preconditions through editor resolution and
submission. Sheet management, numeric fill, formatting, CSV export, list review
and recovered-draft confirmation use the captured context. A real two-service
regression removes the original sheet while its deletion dialog is open,
refreshes the desktop to another sheet, confirms the old dialog, and verifies
that no new deletion is submitted. This completes the two newly authorized
cycles; final fixes are validated without requesting another review.

The user authorized another two review/fix cycles after cycle 5. Cycle 6
returned four substantive findings. External and native clipboard payloads are
now bounded before matrix construction; the REST client rejects oversized
serialized requests locally with a known, asynchronous rejection. Direct SQL
encodes BLOB results as typed base64 JSON. Undo/Redo resolves targets from
recorded history without requiring a sheet named Sheet1. Formula tokenization
distinguishes digit-bearing function names from cell references during
evaluation and copying. Regressions cover each behavior.

Cycle 6 also includes a recovery regression for a draft whose original sheet was removed
while the service was stopped. Its input remains visible and modified after
reopening; submission cannot use the fallback sheet, and later edits require an
explicit discard. The draft stays available for copying until that choice.

Cycle 7 posted no new inline issues, but its summary raised a critical CSV
import-target race and editing/resource concerns. A two-service regression
reproduced a CSV import into another sheet after the original sheet was removed
while the file picker was open. Imports now capture workbook, sheet, state and
coordinates before draft resolution and file selection, validate them after
each modal step, and pass them explicitly through preview and submission.
An active in-cell draft is retained for Save/Discard/Cancel before import starts;
Cancel preserves its input without submitting an import. CSV file reads are
bounded during reading as well as by the initial size check. The reproduced
race now cancels with no mutation. This completes the two further authorized
cycles; final fixes are validated without requesting an eighth review.

The user then authorized two further cycles. Cycle 8 published no inline issues
but named omitted findings about reference detection, Enter navigation and
boolean validation. Confirmed fixes distinguish literal exclamation marks from
sheet references while retaining restrictions on real quoted references;
reject non-boolean open and clear/style flags without creating files or
partially editing cells; and move Enter/Shift+Enter selection only after a
confirmed commit. Save keeps its cell, rejection retains its target and draft,
and sorted-list navigation follows the originally intended next source row.
An independent regression also reproduced closing over an active in-cell draft
without a discard prompt. Open and Close now capture those drafts and preserve
them visibly when discard is declined. The regression suite covers both paths.

Cycle 9 posted no new inline issues but identified a critical retry/admission
race. A real two-service regression pauses the original transaction before
commit, makes an identical retry time out on the writer lock, and verifies that
the SDK stays pending until the original commit is reconciled. Unconfirmed
admission failures now return the operation ID with an unknown outcome and
release any acquired transaction. Desktop and SDK mutation errors need a
matching terminal operation ID before unlocking writes; a generic rejection is
insufficient. Local SDK read-only and size checks fail before transmission
without creating a pending write. Failure injection covers admission cleanup;
a Qt socket fixture checks generic-error recovery records and blocked editing.
This completes the two further authorized cycles. Final fixes are validated
without requesting another review.

The user authorized two further cycles. Cycle 10 published no inline issues but
identified validation paths that rolled back without durable failure receipts,
leaving the SDK pending indefinitely. Malformed styles, ordering, clipboard
sources/values/origins, series steps, labels, sheet identifiers and Unicode now
return explicit validation failures. Unexpected failures after admission also
record a durable rollback receipt after savepoint rollback; replay does not
execute the request again. Invalid operation IDs fail SDK preflight. Long
integer sources retain the exact numeric boundary without Python's integer-text
conversion limit. An independent Qt regression found that range actions could
commit before resolving an active cell draft. Common submission now captures
that draft first: Cancel submits nothing and leaves the input editable. The
long-input regression now exercises actual editor commit signals.

Cycle 11 posted no inline issues but raised service-launch security, formula
correctness and resource-lifecycle concerns. Marker-file regressions reproduced
owned services importing code from the working directory and ambient
PYTHONPATH before any handler activation. Desktop and SDK launch by absolute
trusted path in Python isolated mode; hostile-directory tests also verify
explicit required hooks still execute normally. Failed opens now publish no
unreturned handle and close a new engine if metadata loading fails; existing
leases remain valid. Runtime formula recursion is #LIMIT!, with unrelated cells
still calculated in the same viewport. Installed native smoke and the complete
suite verify the final behavior. This completes the two further authorized
cycles; final fixes are validated without requesting another review.

The user authorized two further cycles. Cycle 12 published no inline issues but
identified formula error literals being mistaken for cross-sheet references. A
copy/paste regression reproduced #REF! unexpectedly blocking sheet rename,
duplication and deletion. Reference detection now skips known formula error
literals while retaining restrictions for real sheet separators and unclassified
exclamation marks. The regression verifies all three operations preserve the
broken formula and its error. Additional cases cover errors inside expressions,
quoted names and actual sheet references adjacent to error literals.

Cycle 13 published no inline issues but raised proxy-mediated exposure of local
credentials and workbook contents. A capture-server regression confirmed the
SDK sent its bearer token through an ambient HTTP proxy. The SDK and managed
CLI now use a private proxy-disabled opener; Qt also explicitly disables proxies.
Redirect regressions reproduced SDK credential forwarding and Qt forwarding of
a redirected POST. Both transports now reject redirects, discard redirect bodies
as receipts and preserve pending mutation IDs. Tests cover SDK/CLI edits with
ambient proxies, HTTP redirect variants and a Qt redirect carrying a forged
rollback receipt. This completes the two further authorized cycles; final fixes
are validated without requesting another review.

The user authorized two further cycles. Cycle 14 published no inline issues but
identified unhandled HTTP failures, premature SQL output and incomplete API
operation schemas. A truncated real HTTP reply now produces a structured CLI
error. Direct SQL serializes within its transaction and prints rows only after
successful commit; a held SQLite read lock reproduces commit failure without
false success output, and serialization failure still rolls back. OpenAPI now
defines every action's arguments and required fields, typed POST bodies, bounds
and examples; the explorer selects action examples. Integration tests execute
all advertised operation examples with current IDs/state, and the specification
is checked with an OpenAPI validator.

Cycle 15 published one high-severity issue: WITH-prefixed direct SQL could commit
before serialization failed because Python did not implicitly open a transaction.
The CLI now explicitly owns the transaction through serialization and commit.
A SQLite authorizer rejects user transaction controls, savepoints, PRAGMA and
attachment commands; commands such as VACUUM that require autocommit fail in the
owned transaction. An external SQLite tool remains available for administration.
Regressions verify CTE failure rollback, successful CTE and DDL writes, rejected
transaction/administrative commands and the absence of created attachment or
VACUUM files. This completes the two further authorized cycles; final fixes are
validated without requesting another review.

The user authorized up to four further cycles, stopping early for cosmetic-only
feedback. Cycle 16 published no inline issues but raised post-commit cleanup
failures being reported as rejected mutations without operation IDs. A real HTTP
regression reproduced a committed CLI receipt followed by a contradictory error
from handle release. The CLI now preserves the completed result and successful
exit status while reporting cleanup separately with the mutation ID and outcome.
Regressions cover HTTP, malformed-JSON and truncated cleanup failures, plus lost
mutation acknowledgements that must retain an unknown outcome and nonzero exit.

Cycle 17 published no inline issues but identified SDK context cleanup masking
primary outcomes and shared registry locking stalling unrelated workbooks. SDK
context exits now preserve the primary exception and attach cleanup details;
cleanup-only failures use a separate error with the last confirmed receipt.
The service uses per-path access gates and brief registry updates, with reserved
engine/handle capacity during slow opens. Slow metadata, construction and close
regressions fail against the prior code and pass with independent reads kept
available. Parallel opens respect the engine cap and concurrent opens of one
file share one engine; releasing one handle preserves the other client.

Cycle 18 published no inline issues but identified metadata refresh after receipt
confirmation replacing a committed result with a generic rejection and clearing
its pending ID. The SDK now preserves the receipt in MetadataRefreshError and
blocks further writes until valid metadata refreshes. Retrying reconciliation
uses the already confirmed receipt; closing a handle after a known outcome is
safe. Regressions cover committed and rolled-back outcomes, HTTP and malformed
metadata failures, guarded writes, successful recovery and context cleanup.

Cycle 19 published no inline issues but identified a critical output failure
that could relabel a committed CLI write as rejected without its ID. Output is
now flushed within an explicit reporting phase; failures preserve the confirmed
receipt and ID on stderr and suppress a second interpreter-shutdown flush.
Broken-pipe regressions cover buffered/unbuffered output, simultaneous cleanup
failure and direct SQL commits. The review log also pointed to receipt lookup
bookkeeping and boolean arithmetic: queried receipts now remain available in SDK
cleanup diagnostics, and unary signs follow the documented numeric 0/1 coercion
instead of returning a boolean that SUM would ignore. Both have regressions.
This completes all four newly authorized cycles; final fixes are validated
without requesting another external review.

The user authorized up to four further cycles with the same cosmetic-only stop.
Cycle 20 published no inline issues, but identified a schema-validation bypass,
failed-create deletion, revision overflow, invalid list criteria and list focus.
The schema signature now excludes only the literal sqlite_ prefix, so user
triggers named sqliteX... cannot evade validation and bypass tracked writes.
Failed creation preserves the destination, and a changed file identity is checked
before schema initialization. Revision exhaustion rolls back with a durable
failure instead of storing a floating-point revision. List criteria reject
non-object members, and Save retains source row/column through a sorted-list
refresh. An independent SDK regression also fixes confirmed-close idempotence
and prevents requests or pending IDs after a client has closed.

Cycle 21 published no inline issues but found the same destructive cleanup
pattern in snapshot failures. Failed copies now preserve their destination.
Creation/copy share exclusive reservation and filesystem identity checks; copies
check the destination before and after backup and use existing-file mode when
opening it. Missing identities reject the operation before target initialization.
Creation also rechecks identity after initialization before returning its engine.
Regressions replace paths before backup, after backup and during a failed backup,
and verify foreign contents survive and a changed copy is not reported committed.

Cycle 22 published no inline issues but identified omitted history costs, an
unbounded duplication preflight under the writer lock, and failed-open handle
publication. History budgets now include serialized labels through Undo/Redo;
this is explicitly a payload budget rather than a Python RSS guarantee. Sheet
duplication stops counting after 10,001 cells before rejecting oversized sheets.
Open responses are assembled and checked for JSON serialization before handles
are published; malformed native metadata releases fresh engines and reservations
without consuming handles or closing an existing client engine. All three
regressions fail on the previous head and pass with these fixes, including a
100,000-cell fixture checked against a bounded SQLite instruction budget.

Cycle 23 published no inline issues but identified uninterruptible formula
preflight scans and native validation, copy, projected-read and numeric-input
risks. SQLite progress handlers now enforce formula-scan deadlines even when
queries return no rows; handlers are removed before rollback and receipt storage.
Validation checks workbook/sheet UUIDs, names, display positions and foreign-key
relationships, including external changes to an active workbook identity.
Snapshots use bounded backup steps with a busy-retry deadline and validate the
actual copied schema/catalog/relationships before acknowledging success. Failed
destinations remain available for inspection. Explicit projected reads default
to their requested starting column, like rectangular reads. Number entry uses
the invariant decimal grammar and applies integer precision checks to zero
decimal suffixes and zero exponents as well as plain integers; other floating
notation retains documented binary64 rounding. Regressions reproduce the old
failures and verify durable timeout rollback, real SQLite backup contention,
invalid external catalogs, incompatible copies and source-preserving rejection.
All four newly authorized cycles are complete. Final fixes are validated and
pushed without requesting a fifth external review.

The user authorized up to ten further review/fix cycles, stopping early when
feedback is clean or cosmetic. Cycle 24 identified sheet-position overflow in
Add Sheet and Duplicate Sheet: SQLite promoted the increment to REAL, allowing a
commit that failed native validation later. Both insertions now share a checked
Python integer increment under the writer lock and reject exhaustion with a
durable POSITION_LIMIT rollback receipt. The maximum signed position remains
valid; reordering compacts positions and permits appending again. The regression
reproduces the corruption on the previous head and verifies both operations,
receipt replay, unchanged data/catalog, valid reopening/copying and recovery.

During the next review, Windows CI exposed a whole-suite runner timeout: all
reported assertions passed, but the Python suite reached its 45-second CTest
limit. The duplicate run passed in 38.62 seconds; desktop suites took about
84 seconds against a 90-second limit. Windows now has 120/180-second Python/Qt
suite budgets, retaining per-operation, hook and request deadlines. Other
platforms retain their existing CTest limits; local reconfiguration confirms
90/45 seconds. The platform-specific settings are covered by the Windows CI job.

Cycle 25 published no inline issues but identified missing deadline checks before
commit and fractional native coordinates. The engine now budgets validation with
the parent operation, checks after hooks and immediately before COMMIT, and gives
Replace All the remaining budget for SQL/search evaluation. Standalone Find has
a two-second budget, including SQL scans that return no rows. Expiry rolls back
the data and provisional success receipt while preserving a durable timeout
receipt. Native validation checks coordinate storage types and bounds so REAL
coordinates accepted by SQLite affinity cannot become inaccessible grid cells.
Open, external refresh and copying reject these values without changing the
invalid external data. New regressions fail on the previous engine and pass for
post-hook expiry, expiry during receipt preparation, empty-match SQL scans and
fractional row/column values, alongside all existing rollback/copy tests.

Cycle 26 identified row-range queries scanning unrelated stored columns for
narrow viewports and searches. A shared iterator now uses primary-key probes
with row equality and bounded column ranges for each source row, retaining one
transaction and row-major ordering. Projected source rows remain sorted and
deduplicated. Reads have a two-second engine/SQL budget, including validation,
and timeouts release the read snapshot. A 60,000-cell wide-sheet regression
fits a bounded SQLite instruction budget for narrow reads, projected reads,
Find and Replace All, while the old scans exceed it. A concurrent WAL writer
used only in a regression commits between row probes: the current read remains
consistent and the next read refreshes its state and values. This does not add a
WAL sidecar portability claim to the native format.

Cycle 27 continued with user-authorized local review agents after the external
review provider exhausted its quota. Reviewers found two substantive issues.
Replace All previously allocated an expanded result before checking the source
limit; a 256-character replacement against a valid 64-KiB cell caused about
32 MiB of temporary allocations before rejection. It now preflights the UTF-8
result size from the match count, including the apostrophe text escape. The
regression caps traced transient allocations while verifying durable rollback,
unchanged inputs, exact UTF-8 boundaries and unchanged display replacements.
A delayed Cut response could also replace a newer Copy and restore a superseded
move source. Clipboard commands and observed clipboard changes now invalidate
older responses before payload, status or error handling. Eight Qt scenarios
cover both response orders and newer external/source-editor content, including
stale error responses. The interface reviewer found no substantive issues.

Cycle 28 local reviewers confirmed the prior fixes and found two further issues.
SQLite LIKE treated embedded NUL as a terminator, allowing a later cross-sheet
reference to bypass rename/delete/duplicate protection. A NUL-safe instr filter
now finds candidate separators before the existing parser classification, still
under its deadline. Tests cover managed/direct formula inputs, all three guarded
operations, durable failures and literal-only exclamations on either side of NUL.
An attached GUI could also submit edits after its server handle was released but
before the close acknowledgement arrived. Releasing now holds the central busy
lock across read completions; callbacks are fenced to their handle and service
URL. Cleanup errors still permit the requested exit, and an older release cannot
close a different newly opened handle. Six Qt cases cover delayed successful
release, newer-state metadata, range reads, failed/disconnected cleanup and an
in-flight Open, alongside existing draft and unknown-outcome close coverage.

Cycle 29 local reviewers found Boolean/number comparisons bypassing the declared
type rules and unmodified editor commits deleting native NULL cells. Formula
comparisons now require matching type categories, preserving the documented
blank/numeric/Boolean exception and explicit arithmetic conversion. Regressions
cover all six operators, 48 rejected mixed cases and 114 permitted cases. Both
GUI editor paths now preserve unchanged source and typing intent through durable
no-op edits; native NULL, absence, explicit types and automatic numbers with Text
formatting retain their inputs and do not run change handlers. Sixteen Qt
scenarios cover native kinds, actual values-only pasted NULL origins and hooks.
Linux CI also exposed a disconnect fixture that held a transport retry forever;
the proxy now simulates a persistent outage across retries while asserting one
logical cleanup completion. A subsequent raw socket probe verifies that behavior
on Qt versions that do not retry the initial request.

Cycle 30 found no new desktop or formula issues, but the interface reviewer
confirmed that unchanged-input no-op commits cleared Redo. Nonstructural history
now clears Redo only for actual stored-cell changes after coalescing and hooks.
The no-op still receives its durable receipt and one revision increment.
Regressions cover row/column-only input, identical source, coalesced writes and
a transactional hook restoring the original value, then verify Redo still
restores the prior result. Real input/style changes still invalidate Redo.

## November 9 macOS distribution baseline

All original components now use GPL-3.0-only. A checksum-pinned relocatable
CPython 3.13.18 runtime, isolated installed CLI, Qt/Python dependency materials
and explicit macOS 15+ ARM64 baseline are implemented and locally verified.
See [distribution evidence](macos-distribution.md). Historical external-Python
and undecided-license reports above retain their original context. Developer ID,
notarization, full distribution review, physical/VoiceOver input and actual
clean-Mac acceptance remain open; this is not a qualified release.
