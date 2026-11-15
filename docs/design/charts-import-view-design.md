# Charts on open: import and view-only display

Status: **As built** (November 15, 2026), on branch `claude/charts-import-view`, not merged. Drafted November 14, 2026 (Draft 0.1). Where the build differs from the text below, [compatibility-v0.md](compatibility-v0.md#charts), [implementation-status.md](implementation-status.md#charts-import-and-view-only-display--november-15-2026) and the [evidence](evidence/charts-import-view-verification.txt) are the record; the sections below are the design as agreed.
Marks: **Owner** is a decision the owner made; **Call** is the author's
proposal, open to correction; **Builder** is left to the implementation plan.

Read with the [charts design](charts-design.md) (Draft 0.6), the
[native format](native-format-v1.md#charts-format-v8) and the
[compatibility contract](compatibility-v0.md).

## 1. Intent

When a file that holds charts is opened in the desktop, the charts are shown.
Today they are not, for two separate reasons:

1. XLSX and ODS import does not read charts. It reports them as omitted
   (`workbook.feature` `charts`).
2. The desktop has no chart code. The service stores, resolves and renders
   charts (stage 2), but nothing in `desktop/` lists or draws them, so a native
   workbook that holds charts shows none either.

The trigger is `Column Chart_Complete.xlsx`: two worksheets, each with one
column chart (one clustered, one stacked) anchored over cells. After import
neither chart is visible.

## 2. Goals, non-goals and anti-goals

**Goals**

- A chart stored in a native workbook is drawn over its sheet in the desktop.
- Importing an XLSX or ODS file creates a native chart for every chart the
  specification can express, through every import path (section 5).
- An imported chart is an ordinary native chart: it follows its data, survives
  save and reopen, and is reachable through REST, the CLI and the SDK.
- Everything not carried over is named in the conversion report.
- The sample file shows both charts after import with either dialog scope.

**Non-goals**

- Authoring in the desktop: Insert Chart, the edit panel, select, move, resize,
  delete, the object browser entry. They stay in charts stage 3.
- Chart export to XLSX or ODS. Export keeps reporting each chart as omitted.
- New chart types or options. Specification version 1 is unchanged; nothing is
  added to carry Excel colours, data labels or gap width.
- Chart sheets, pivot charts, combination charts, secondary axes.
- Workbook-Python chart operations.
- CSV and other tabular formats, which hold no charts.

**Anti-goals**

- An import that fails, or loses cells, because of a chart.
- A chart imported with a source that silently points at different cells than
  it did in the file.
- A chart dropped or changed without a report line.
- A chart in the desktop that differs from the image REST returns, or that
  keeps showing old data after a managed edit without any sign it is stale.

## 3. Approaches

**Import**

| Approach | Verdict |
| --- | --- |
| A. Read charts with the import: the readers convert each chart to a native specification, it travels with the reviewed import and is created in the same operation as the cells | **Chosen** (**Owner**, November 14, 2026) |
| B. A second pass after apply re-reads the package and creates charts | Rejected: the stage is deleted on apply, and a failure leaves cells without charts and no single receipt |
| C. Keep the raw chart XML and convert when displayed | Rejected: the charts design stores only a declarative specification, and direct SQLite readers would see opaque XML |

**Desktop display**

| Approach | Verdict |
| --- | --- |
| A. One overlay widget per chart, parented to the grid body's viewport, positioned from the anchor cell | **Chosen** (**Owner**, November 14, 2026): stage 3 select, move and resize build on the same widgets |
| B. The grid paints each chart's SVG after its cells | Rejected: least code now, but likely replaced by widgets in stage 3 |
| C. A side panel listing chart images | Rejected: charts are placed over cells; a panel is not what opening the file in Excel shows |

**Scope of the desktop work**: view-only first (**Owner**, November 14, 2026).

## 4. What an imported chart keeps

**Call**, all of this section. The mapping is the same for XLSX (DrawingML
`c:chartSpace`, reached from a worksheet's drawing part) and ODS (a
`draw:frame` holding a `draw:object` whose sub-document is a `chart:chart`).

| In the file | Native result |
| --- | --- |
| Bar chart, vertical; bar chart, horizontal; line; area; pie; scatter | `column`; `bar`; `line`; `area`; `pie`; `scatter` |
| Clustered or standard grouping | `clustered` (`null` where the type takes none) |
| Stacked grouping | `stacked` |
| Chart title text (literal rich text) | `title.text`, cut to 255 characters with a report line |
| Category and value axis titles (literal) | `axes.category.title`, `axes.value.title` |
| Value axis fixed minimum and maximum | `axes.value.min`, `axes.value.max` |
| Legend at right; at bottom; absent | `right`; `bottom`; `none` |
| Legend at top or left | `right`, reported |
| Series name by cell reference; by literal | `name.source`; `name.text` |
| Series values, categories, scatter x by one contiguous single-row or single-column reference | Range sources |
| Categories of the first series | The chart's `categories` |
| Frame name ("Chart 1") | Chart name, made unique on its sheet |
| Top-left anchor cell and offset | `anchor`, `offset` (EMU or length converted to pixels at 96 DPI) |
| Frame extent | `size` in pixels, clamped to 64×48..4096×4096 |
| Drawing order on the sheet | `z` |

**Converted with a report line (the chart is still imported):**

- Colours, fills, fonts, data labels, gap width and overlap, markers, smoothing,
  gridlines, number formats, chart and plot-area styling: dropped. One line per
  chart lists what was dropped.
- Percent-stacked grouping becomes `stacked`.
- 3D variants of a supported type become the 2D type. Doughnut becomes `pie`
  when it has one series.
- A title or axis title taken from a cell reference becomes that cell's cached
  text at import time (a fixed text, no longer following the cell).
- Series whose categories differ from the first series keep the first series'
  categories.
- A two-cell anchor whose extent cannot be computed from known column widths
  and row heights uses default cell sizes; an absolute anchor is placed at the
  nearest cell.

**Skipped with a report line (no chart is created):**

- Any other chart type: radar, stock, surface, bubble, treemap, sunburst,
  waterfall, histogram, funnel, map, and Excel's extended (`cx:`) charts.
- A plot area with more than one chart type, or a secondary axis.
- A pie or doughnut with more than one series.
- A source that is not one contiguous single-row or single-column range on an
  imported worksheet: a multi-area or multi-level reference, a defined name, an
  external workbook, a literal array with no reference, a sheet that is not
  imported, a table structured reference.
- A chart that breaks a native limit: more than 64 series, a source over 10,000
  cells (1,000 for a pie), over 100,000 source cells in all.
- Charts past the workbook limit of 64, in file order, counting charts the
  destination already holds.
- Chart sheets, and charts inside a group shape.
- A chart part that cannot be parsed. A malformed chart never fails the import.

Report lines use a new `chart.*` category family (**Builder**: exact category
names and wording, following the existing conversion report conventions). The
existing `workbook.feature` `charts` count stays, so nothing that reads it
breaks, and is joined by the per-chart lines. (As built the count stayed; the
owner dropped it on November 15, 2026, a later change recorded in
[charts-authoring-export-design.md](charts-authoring-export-design.md#6-export)
section 6 and in the bullet "Omitted entries" under the as-built notes.) Each
line names the chart and its sheet.

Cached values in the file (`c:numCache`, `c:strCache`) are never used as data;
the chart reads the imported cells. In values mode the cells hold values and
the chart reads them the same way.

## 5. Import paths

Every managed import that can meet a chart carries it. There are three apply
shapes today and all three are in scope:

| Path | Today | With charts |
| --- | --- | --- |
| Whole workbook, staged (desktop "All sheets", CLI, SDK `import_staged`; streaming readers `xlsx_stream`, `ods_stream`) | Cells in a stage file, reviewed, applied atomically | Charts are stored in the stage, listed in the review summary, covered by the stage digest and created by the apply |
| Whole workbook, inline `sheets` (bounded readers, at most 10,000 cells) | Sheets and cells in the request | The preview returns charts beside the sheets; the `workbook_import` request carries them back |
| One sheet at a cell (desktop default "One sheet at the selected cell"; bounded readers `xlsx_preview`, `ods_preview`, then `import_edits`) | Edits to the current sheet at an offset | Charts of that worksheet whose sources all lie on it are created on the destination sheet, with sources, anchor and offset shifted by the paste offset. A chart with a source on another sheet is skipped and reported |

**Call**: the one-sheet path is included because it is the dialog's default;
leaving it out would leave the reported problem in place for most imports.

**How the one-sheet path carries charts. Call.** This path is not an import
operation today: the client takes the preview's cells and submits a plain
`edit`. The preview gains a `charts` list, and `edit` gains an optional
`charts` field in the same form as the inline `workbook_import` request. The
service creates those charts in the same operation as the cells. It follows
`edit`'s existing contract, not the import's: cell change events fire for the
cells as they do today, a failing handler rolls back cells and charts
together, and the edit is one undo step that also removes the charts. The
receipt reports them with the existing `changed_charts`. The SDK and CLI
one-sheet imports pass the field through. Sending separate `chart_create`
requests after the edit was rejected because it is not atomic.

Rules common to all three:

- Charts are created in the same operation as the cells: one receipt, all or
  nothing. The `workbook_import` receipt's `imported` gains `charts`.
- Undo is unchanged. A whole-workbook import is a structural operation that
  clears undo and redo today, and stays so. Only the one-sheet edit is
  undoable.
- Sources are held in the reviewed import by worksheet position and A1
  rectangle, and bound to the new sheet identities at apply. Neither target
  (`replace_empty`, `add_sheets`) renames sheets, so no name mapping is needed.
- Everything that can refuse a chart is decided when the import is prepared, so
  the review is accurate and the apply cannot fail on a chart. The one check
  that needs the destination, the 64-chart workbook limit under `add_sheets`
  and the one-sheet path, is applied at apply: excess charts are left out and
  listed in the receipt (**Builder**: receipt field name).
- A workbook that receives a chart is raised to native format v8, as
  `chart_create` does.
- The review summary gains a `charts` list (name, sheet, type) and the count is
  shown in the desktop review dialog next to sheets and cells. **Builder**:
  layout within the existing dialog; no new dialog.
- Creating the charts raises no chart events, as for chart operations today.
  A whole-workbook import raises no cell change events, as today.
- The bounded and streaming readers share one chart-reading module per format,
  so the two paths cannot disagree. Chart parts are read under the same byte
  budgets and XML safety rules as other parts.

`replace_empty` still refuses a destination that already holds charts.

## 6. Desktop display

**What the user sees.** Each chart on the current sheet appears as a floating
rectangle over the cells, its top-left at the anchor cell plus offset, at its
stored pixel size, stacked by `z`. It moves with the cells when the sheet
scrolls and when rows or columns are resized, hidden or inserted.

```
      A        B        C        D        E        F        G
 1  Division  Sales   ┌────────────────────────────────────────┐
 2  Sports   400,586  │           Sales by Division            │
 3  Arts     353,095  │   █                                    │
 4  Electr.  804,585  │   █    █                               │
 5  Office   410,434  │   █    █    █    █                     │
 6  Celebr.   99,699  │   █    █    █    █    ▄    ▁    ▁      │
 7  Food      25,539  └────────────────────────────────────────┘
```

**Rules**

- The image is the service's SVG from a chart render job, shown with Qt SVG.
  The desktop does not draw charts itself. Qt SVG becomes a required desktop
  dependency (it is optional and probe-only today); `LICENSES.md` and packaging
  are updated.
- View-only: the overlay passes mouse and keyboard through to the grid, so
  cells under a chart stay selectable and editable. **Call**. It carries the
  chart's name as its accessible name.
- Charts are listed when a sheet is shown and when the workbook state changes
  (after any managed operation, refresh, import, undo or redo). Coarse
  staleness, as designed: a state change re-renders the charts that are in
  view. The previous image stays, scaled if the size changed, until the new
  one arrives.
- Only charts intersecting the visible area are rendered. Render jobs are
  queued so a sheet of many charts cannot flood the service; jobs for charts
  scrolled away or for a sheet left are cancelled and released. **Builder**:
  queue depth and debounce.
- A chart that cannot be drawn shows a placeholder of the same size with the
  chart's name and the reason: its `issue` (`INVALID_SPEC`,
  `UNSUPPORTED_SPEC_VERSION`), `over_limit`, a broken source, or the render
  job's error code and message. A failure is never an empty space.
- Frozen panes. **Call**: a chart is placed where its anchor cell is shown, as
  Excel does. A chart anchored in a frozen row or column stays with that
  frozen area and does not scroll in that direction; a chart anchored in the
  scrolling body scrolls and passes under the frozen rows and columns. A chart
  is therefore never hidden only because its anchor is frozen (the frozen rows
  and columns are hidden in the body view, so anchoring to the body alone would
  lose it). **Builder**: which widget parents the overlays and how the clip is
  made.
- Projected views. **Call**: while the sheet is shown through a filtered or
  sorted list view, which remaps rows, charts are hidden; they return when the
  plain sheet is shown. The one-sheet import is already refused in a projected
  view.
- Charts never cover the row and column headers, the formula bar or other
  panes.
- Wireframe. The charts design requires wireframes before desktop chart work.
  For this view-only slice the sketch and rules of this section are the
  wireframe, and approving this document is the sign-off. The full stage 3
  wireframe session is still owed before authoring is built.
- Zoom, if the grid has it, scales charts with the cells. **Builder**.
- Printing, portable view export and screenshots of the grid are unchanged in
  this slice.

Nothing is added to REST for display: the desktop uses the existing chart
catalog read and chart jobs.

## 7. Constraints

- All clients go through REST; the desktop gets charts only from the service.
- Managed/direct boundary is unchanged: an imported chart is a `chart` row in
  `dh_objects` with a version 1 specification, exactly as `chart_create`
  writes it. No native format change.
- Uploaded packages are untrusted data. Chart parts are parsed with the
  existing bounded, entity-safe readers; relationship targets are resolved
  inside the package only.
- Python standard library only in the service; no new third-party packages.
- OpenAPI is updated for the review summary, the bounded previews, the inline
  `workbook_import` request, the `edit` request and the receipts.
- GPL-3.0-only; Qt SVG is used under the same Qt licence as the other modules.

## 8. Verification

- Service tests build XLSX and ODS fixtures in code (`tests/xlsx_builder.py`
  and its ODS counterpart): each supported type and grouping, each converted
  and each skipped case of section 4, all three import paths, both targets,
  the 64-chart limit, a malformed chart part, undo and redo of a one-sheet
  import with charts, and a failing change handler rolling back its charts.
- A fixture equivalent to the sample file (two worksheets, a clustered and a
  stacked column chart, data labels and theme colours) imports with two charts
  whose resolved series equal the sheet values. The sample itself is not
  committed.
- A manual cross-format check, recorded under `docs/design/evidence/`: one
  document saved by LibreOffice as XLSX and as ODS imports to charts that
  resolve to the same series. It is not an automated test and no
  LibreOffice-produced file is committed.
- Desktop tests (`tests/test_desktop.cpp`): overlays appear at the anchor,
  follow scroll and column resize, re-render after an edit, show a placeholder
  for a broken chart, do not intercept a click on a covered cell, stay visible
  when anchored in a frozen row, and hide in a projected view.
- Manual: the sample file in the installed application, with each dialog scope.

## 9. Documents to update

[charts-design.md](charts-design.md) (stage table: import ahead of export, and
a view-only slice ahead of stage 3; section 11),
[compatibility-v0.md](compatibility-v0.md) (charts move from omitted to
converted, with the tables of section 4),
[interfaces-and-interoperability.md](interfaces-and-interoperability.md),
[implementation-status.md](implementation-status.md), `LICENSES.md`.

## 10. Left to the builder

Module layout and names; the stage file's chart table and digest encoding; the
`chart.*` report category names; receipt and summary field names beyond those
given; how pixel extents are computed from column widths and row heights in
each format; render queue and debounce values; widget class structure and how
positions track the bounded grid model and frozen panes; test fixture design.

## 11. As built: departures from this design

Recorded November 15, 2026. Each is also in the
[compatibility contract](compatibility-v0.md#charts).

- **Report vocabulary.** `chart.skipped` details are `type`, `combination`,
  `secondary_axis`, `pie_series`, `source`, `limit`, `off_grid`,
  `workbook_limit`, `unreadable` and `grouped`. `chart.converted` details are
  `styling: <property>` (one entry per dropped property), `grouping`,
  `series_name`, `percent_stacked`, `three_d`, `doughnut`, `legend_position`,
  `title_from_cell`, `title_cut`, `series_name_cut`, `categories_differ`,
  `size_approximate`, `anchor_absolute` and `name`. Section 4 said one line per chart lists what was
  dropped; the build writes one entry per property, each counting charts.
- **Chart byte budget and reading bounds.** Chart, drawing and relationship
  parts are read against their own 16 MiB allowance per import, not the package
  budget, each distinct part read and parsed once and only its extracted body
  kept; past it, or when a part's compressed data is corrupt, charts are
  reported `unreadable`. An XLSX worksheet follows only the drawing its
  `<drawing r:id>` names. At most 256 chart frames are read per import, across
  all its sheets; once 64 charts are held no further chart part is read, so the
  rest are reported `workbook_limit` unchecked. A draft keeps at most 65 series.
  The streaming readers check cancellation and the deadline for every frame.
- **Text and record caps.** Series names and axis titles are cut to 255
  characters like the title (`series_name_cut`, `title_cut`). A converted chart
  over 64 KiB as JSON is skipped as `limit`, and one import's charts are held to
  1 MiB in all (`workbook_limit`); `check_records` refuses client records past
  either cap, so a chart can no longer fail an import's apply (the 256 KiB
  payload limit) or a bounded preview (4 MiB).
- **Sizes.** The pixel size is computed from default cell sizes (64x20 px); a
  sheet that declares custom sizes reports `size_approximate` (on ODS, any
  declared column-width style). A chart keeps roughly its source pixel size, so
  on the desktop grid, whose default cells are larger, it covers fewer cells
  than in the source.
- **Omitted entries (a later change).** As first built, the `workbook.feature`
  `charts` count stayed and `sheet.feature` `drawings` was reported too, so a
  file whose charts all imported still listed both as omitted. The owner
  settled this on November 15, 2026 (see
  [charts-authoring-export-design.md](charts-authoring-export-design.md#6-export)
  section 6), after this record was written. The rule as now built: there is no
  `workbook.feature` `charts` entry; `sheet.feature` `drawings` is reported for
  a sheet only when its drawing was not read or held something other than chart
  frames (a picture, shape, connector, group or other object); on ODS,
  `embedded_objects` does not count an object read as a chart; `chart_sheets`
  is unchanged. Every chart still has its `chart.converted` or `chart.skipped`
  entry.
- **Frozen panes.** A chart anchored in a frozen row or column is pinned in the
  frozen direction and may extend over the scrolling cells.
- **Chart list and render failures.** A failed chart list read is shown in the
  window's issue line and read again on the next refresh; a chart whose render
  failed is rendered again when the window refreshes without a change.
- **Render concurrency.** The service runs one chart job per workbook, so the
  layer opens one render job at a time (`ChartLayer::MaxConcurrentRenders` is
  1); a start refused because another client's chart job holds the slot
  (`JOB_BUSY`, `JOB_LIMIT`) backs off and tries again.
- **One-sheet import counts.** The one-sheet import's review names its charts
  and the status after the edit counts the changed cells and charts.
- **CLI.** The CLI has no one-sheet import, so only the SDK and the desktop
  carry charts on that path; the CLI's whole-workbook import carries them.
- **Not built or not verified.** Export, authoring, workbook-Python chart
  operations, and any verification on Windows and Linux. The manual desktop
  check of section 8 and the LibreOffice cross-format check were not run; see
  the [evidence](evidence/charts-import-view-verification.txt).
