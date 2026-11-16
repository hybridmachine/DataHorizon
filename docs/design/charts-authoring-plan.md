# Charts Desktop Authoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person can insert a chart from a selection, select, move, resize, reorder and delete it on the sheet, and change everything specification version 1 holds in a docked side panel, with the mouse and with the keyboard.

**Architecture:** The existing overlays in `desktop/chartlayer.cpp` become interactive (selection, focus, drag, keys) and `ChartLayer` gains a selection. A new right-hand dock holds `ChartPanel`, which edits one chart. Pure chart-specification logic (stored form to input form, type-change companions, pixel position to anchor and offset) lives in `desktop/chartspec.cpp` so it is tested without a service. Every change is one `chart_create`, `chart_update` or `chart_delete` sent through `MainWindow::submit`, the path every other desktop edit uses; the catalog is read again after each, as today. No service change.

**Tech Stack:** C++17, Qt 6.8 Widgets, Network, Svg and Test; the existing Python service unchanged.

**Spec:** [charts-authoring-export-design.md](charts-authoring-export-design.md), sections 4, 5, the Authoring part of 7, and 8. Read also [charts-design.md](charts-design.md) section 8 (operations) and [charts-import-view-design.md](charts-import-view-design.md) sections 6 and 11 (the view-only layer as built).

## Global Constraints

- Wireframes are signed off by the owner before any desktop code (Task 1 is a gate; `AGENTS.md`).
- Every authoring action is exactly one managed operation sent with `MainWindow::submit`: `chart_create`, `chart_update` or `chart_delete`. Nothing is applied to the picture without being stored. One Undo reverses one action.
- No service, REST, CLI or SDK change. If one proves necessary, stop and report: it must go through REST with an OpenAPI entry, not a desktop-only path.
- Specification version 1 is unchanged: types `column`, `bar`, `line`, `area`, `pie`, `scatter`; grouping `clustered` or `stacked` for `column`, `bar`, `area` only; legend `right`, `bottom`, `none`; text at most 255 characters; name 1 to 64 characters.
- Size bounds: width 64 to 4096, height 48 to 4096. Offset 0 to 4096 on each axis.
- A refusal shows the service's own message, changes nothing, and puts the field or the chart back. The panel never drops data to make a change fit.
- Every action is reachable from the keyboard.
- One chart is selected at a time. No cut, copy, paste, multi-selection, copy as image, type-picker dialog or preview.
- Charts stay hidden in a projected list view; authoring commands are disabled there, with no workbook, in a read-only workbook and while the relational workbench is shown.
- After any change the catalog is read again and drawn from (`submit` already refreshes; do not patch overlays by hand).
- New source files carry `// SPDX-License-Identifier: GPL-3.0-only`.
- Object names are how tests find things; every new action, widget and dock gets the `objectName` given in its task.
- Status text follows the existing style: short phrases joined with " • ". Errors go to `MainWindow::issue`.
- Test commands: `cmake --build build --target chart_authoring_tests desktop_tests` then `ctest --test-dir build -R '^(chart_authoring|desktop)$' --output-on-failure`. Service suite (must stay green, unchanged): `PYTHONPATH=service python3 -m unittest discover -s tests`.

## Decisions this plan makes (design section 8, "left to the builder")

Each is the planner's call, open to the owner's correction at plan review.

1. **Whole `spec`, always.** The panel sends a full `spec` in input form built from the catalog's chart, never `spec_patch`. Sources are sent by id (`sheet` = `sheet_id`, `table` = `table_id`, `column` = `column_id`, `query` = `query_id`), which the engine accepts and which survives renames.
2. **Broken sources.** A broken source cannot be sent back. While a chart has a broken source, the panel's other specification fields are disabled with the reason at the top, and edits to source fields are held in the panel (marked "Not applied yet") until no broken source remains; then one `chart_update` carries them. Name, placement and order are never blocked. This is the same rule the design gives Add Series.
3. **Shortcuts.** Insert Chart `Alt+F1`; Select Next Chart `Ctrl+Alt+C`; Select Previous Chart `Ctrl+Alt+Shift+C`. On a selected chart: arrows move by 8 px, `Alt`+arrows by 1 px, `Shift`+arrows resize by 8 px, `Shift+Alt`+arrows by 1 px, `Delete` or `Backspace` deletes, `Enter` or `Return` opens the panel and focuses it, `Escape` returns focus to the grid and deselects. Key auto-repeat is ignored while an operation is in flight.
4. **Handles.** Eight (corners and edge midpoints), 8 px squares drawn inside the overlay's rectangle with a 2 px frame in the palette's highlight colour, so the existing mask and header clipping are untouched.
5. **Drag across panes.** A drag does not scroll the grid. On release the anchor is the cell under the chart's top-left corner in whichever pane (frozen corner, frozen row, frozen column, body) is under it; when that point is over no cell (a header, outside the grid, past the last row or column) the drag is cancelled and the chart goes back.
6. **No debounce.** A choice applies on change; text applies on Enter or focus-out, and only when it differs from the chart's stored value. While an operation is in flight the panel and the overlays' drag are disabled (`MainWindow::setBusy`).
7. **Stale state.** Nothing new: `submit` already refreshes on `STALE_STATE`, the layer lists again, and the panel reloads from the new catalog; the refused change is not retried.
8. **Tests in a new executable.** `tests/test_desktop.cpp` already takes 444 s of its 600 s limit on CI. Authoring tests go in `tests/test_chart_authoring.cpp`, CTest name `chart_authoring`.
9. **Picking a range.** During a pick the user may switch sheets; the panel keeps its chart by id. On Enter or Escape the window returns to the chart's sheet and selects the chart again.

## Review Focus

The five conditions the design implies but states no test for, most likely first. Each has its test in the task named.

1. The selected chart disappears underneath the user (Undo of its creation, another client's delete, a sheet switch) while the panel is open or a field has focus: the selection clears, the panel reads "No chart selected", and the pending focus-out sends nothing. (Task 6)
2. A drag released where no cell is under the chart's top-left corner (a header, outside the window, beyond the last row): no operation is sent and the chart returns to where it was. (Task 5)
3. A focus-out or Enter on a text field whose value did not change, and a second change while the first is still in flight: no operation, and no second operation, is sent. (Task 6)
4. A chart with a broken source, an unsupported specification version or an invalid specification: it can be selected, moved, resized, reordered, renamed and deleted; the panel shows the reason and does not send a specification it cannot build. (Tasks 5 and 7)
5. Insert when the anchor column to the right of the selection does not exist or is hidden, and when the selection is over 10,000 cells or intersects hidden rows: the documented fallback or the refusal, never a chart at an unshown cell. (Task 4)

## File Structure

| File | Responsibility |
| --- | --- |
| `docs/design/charts-authoring-wireframes.md` (new) | The signed-off wireframes: every screen and state of sections 4 and 5 |
| `desktop/chartspec.h`, `desktop/chartspec.cpp` (new) | Pure functions on chart JSON: input form, type change, placement arithmetic, source text. No widgets, no network |
| `desktop/chartlayer.h`, `desktop/chartlayer.cpp` (modify) | Interactive overlays, the selection, drag and keys; emits requests, sends nothing itself |
| `desktop/chartpanel.h`, `desktop/chartpanel.cpp` (new) | The side panel's widget: shows one chart, emits one change request per edit |
| `desktop/mainwindow.h`, `desktop/mainwindow.cpp` (modify) | Insert menu and actions, the dock, turning requests into `submit` calls, refusals, the pick mode |
| `tests/test_chart_authoring.cpp` (new) | All authoring tests |
| `tests/test_desktop.cpp` (modify) | Only `chartOverlayDoesNotInterceptClicks`, which the design reverses |
| `CMakeLists.txt` (modify) | New sources and the `chart_authoring` test |

---

### Task 1: Wireframes and owner sign-off

**Files:**
- Create: `docs/design/charts-authoring-wireframes.md`

**Interfaces:**
- Produces: the layouts, labels and states every later task builds; later tasks copy label text from this document verbatim.

- [ ] **Step 1: Write the wireframes** in the form of `relational-workbench-wireframes.md` (ASCII boxes in `text` fences, then Qt mapping, keyboard and states prose). Start from the mockup the owner chose (`.superpowers/brainstorm/16514-1794765490/content/chart-authoring.html`, option A). Required screens, one fenced sketch each:
  1. Main window with the Insert menu open (Chart; Select Next Chart; Select Previous Chart, with the shortcuts of Decision 3).
  2. A sheet with a selected chart (frame, eight handles) and the panel docked right.
  3. The panel in full, groups in this order with these labels: Chart (Name; Bring to Front; Send to Back), Type (six type buttons; Grouping), Title, Data (Categories; Series list with Name, Values, X; Add Series; Remove; Move Up; Move Down), Axes (Category title; Value title; Minimum; Maximum, placeholder "Automatic"), Legend (Right, Bottom, None).
  4. A source field in each of its three kinds (Range with a pick button; Table column; Query column).
  5. The panel during a range pick (the field highlighted; status text "Pick a range for <field> • Enter applies • Escape cancels").
  6. The context menu on a chart (Edit Chart; Bring to Front; Send to Back; Delete).
  7. States: "No chart selected"; a refused change (message beside the field); a broken source (reason at the top, source fields "Not applied yet"); unsupported or invalid specification (reason at the top, only the Chart group enabled); a chart still drawing; busy.
  Then the keyboard table (Decision 3), the focus order through the panel, accessible names ("Chart: <name>") and descriptions ("<Type> chart, titled <title>, <n> series"), and a list of the nine decisions above for the owner to confirm.
- [ ] **Step 2: Commit** the document on the branch.
- [ ] **Step 3: STOP for the owner's sign-off.** Show the document, ask for approval or corrections, apply corrections, and record "Signed off by the owner on <date>" at its top. No later task starts before this line exists. A changed label or decision is carried into this plan before continuing.

### Task 2: Chart specification helpers and the test executable

**Files:**
- Create: `desktop/chartspec.h`, `desktop/chartspec.cpp`, `tests/test_chart_authoring.cpp`
- Modify: `CMakeLists.txt` (add `target_sources(datahorizon_desktop PRIVATE desktop/chartspec.cpp desktop/chartspec.h)`; add `chart_authoring_tests` by copying the `python_workspace_tests` block, CTest name `chart_authoring`, output `chart-authoring-tests.txt`, the same environment and `TIMEOUT 600` as `desktop`)

**Interfaces:**
- Produces (namespace `ChartSpec`, all pure):
  - `QJsonObject inputSpec(const QJsonObject &chart, QString *reason = nullptr)`: the input-form `spec` of a catalog chart (Decision 1); `{}` with `reason` set when `chart["issue"]` is not null or any source is broken.
  - `bool hasBrokenSource(const QJsonObject &chart)`; `QString stateReason(const QJsonObject &chart)`: the same text `ChartLayer::reasonFor` gives (move that function's body here and have the layer call it).
  - `QJsonObject withType(const QJsonObject &spec, const QString &type, QString *refusal = nullptr)`: the companion rules of design section 5: grouping becomes `clustered` when the new type takes one and the old had none, and null when the new type takes none; to `scatter`, `categories` becomes every series' `x` and `categories` becomes null; from `scatter`, the first series' `x` becomes `categories` and every `x` becomes null. To `scatter` with no categories returns `{}` and `refusal` = "Give an X source before changing this chart to a scatter chart." It never removes a series (a two-series chart to `pie` is returned as asked, for the engine to refuse).
  - `bool takesGrouping(const QString &type)`.
  - `QJsonObject sourceToInput(const QJsonObject &storedSource)` and `QString sourceText(const QJsonObject &storedSource, const QHash<QString,QString> &sheetNames)`: display text `Sheet1!A2:A13`, `Table.Column`, `Query.Column`.
  - `QJsonObject rangeSource(const QString &sheetId, int r1, int c1, int r2, int c2)`: `{kind:"range", sheet, ref}` with `ref` in A1 form (`A2:A13`, or `B1` for one cell).
  - `bool parseRange(const QString &text, const QHash<QString,QString> &sheetIdsByName, const QString &currentSheetId, QJsonObject *source)`: accepts `A2:A13`, `Sheet1!A2:A13`, `'Q1 Sales'!$A$2:$A$13`.
  - `QSize clampSize(QSize)`: to 64..4096 by 48..4096.
  - `QRect resized(QRect start, int handle, QPoint delta)`: handle 0..7 clockwise from top-left; the result never goes below the minimum and keeps the opposite edge fixed.

- [ ] **Step 1: Write the failing tests** (`class ChartAuthoringTests : public QObject`, `QTEST_MAIN`), one function per rule: `inputSpecRebuildsSourcesById` (range, table, query), `inputSpecRefusesBrokenAndUnreadable`, `typeChangeCompanions_data` / `typeChangeCompanions` (every pair of the six types: grouping and categories/x as above), `scatterNeedsAnXSource`, `pieKeepsEverySeries`, `rangeTextRoundTrips_data` (`A1`, `A2:A13`, quoted names, `$` forms, a bad reference is false), `resizeHoldsTheOppositeEdge_data` (all eight handles, a delta past the minimum).
- [ ] **Step 2: Run** `cmake --build build --target chart_authoring_tests && ctest --test-dir build -R '^chart_authoring$' --output-on-failure`. Expected: build fails, `chartspec.h` not found.
- [ ] **Step 3: Implement** the functions.
- [ ] **Step 4: Run** the same command. Expected: PASS. Run `ctest --test-dir build -R '^desktop$'` once to confirm `reasonFor`'s move changed nothing.
- [ ] **Step 5: Commit.**

### Task 3: Interactive overlays and the selection

**Files:**
- Modify: `desktop/chartlayer.h`, `desktop/chartlayer.cpp`, `tests/test_desktop.cpp` (`chartOverlayDoesNotInterceptClicks`), `tests/test_chart_authoring.cpp`

**Interfaces:**
- Consumes: `ChartSpec::stateReason`.
- Produces on `ChartLayer`:
  - `QString selectedChart() const;` `void select(const QString &chartId);` (empty clears; an id not on the sheet clears) `void selectNext(int direction);` (z order, wraps; +1 or -1)
  - `QJsonObject chart(const QString &chartId) const;` `ChartOverlay *overlay(const QString &chartId) const;`
  - `void setInteractive(bool);` (false while busy, read-only or projected: overlays select but do not drag or take keys)
  - signals `void selectionChanged(const QString &chartId);` `void editRequested(const QString &chartId);` (double-click, Enter)
- Produces on `ChartOverlay`: `void setSelected(bool);` `bool selected() const;` focus policy `Qt::StrongFocus`; `WA_TransparentForMouseEvents` removed.

Behaviour: a press on an overlay selects it and gives it focus; a press on any grid pane, or Escape on the overlay, clears the selection and returns focus to the grid (`FrozenPanes::focusCurrent`); the wheel over an overlay is forwarded to the body view's viewport; the selection survives a relist when the chart still exists and clears (emitting `selectionChanged("")`) when it does not or when the layer binds another sheet; the accessible name is "Chart: <name>" and the description "<Type> chart, titled <title>, <n> series" (title part omitted when empty). The selected overlay paints the frame and handles of Decision 4 over the image or placeholder.

- [ ] **Step 1: Write the failing tests** (a `CHART_WINDOW`-style fixture copied into the new file with `fillChartData`, `addChart`, `overlayNamed`): `clickSelectsAChart`, `clickOnTheGridDeselects`, `escapeDeselectsAndFocusesTheGrid`, `onlyOneChartIsSelected`, `selectionSurvivesARelist`, `selectionClearsWhenTheChartIsGone` (undo its creation), `selectionClearsOnSheetSwitch`, `wheelOverAChartScrollsTheGrid`, `selectNextWrapsInZOrder`, `overlayIsAccessible`, `brokenChartCanBeSelected`. Change `chartOverlayDoesNotInterceptClicks` in `tests/test_desktop.cpp` to `chartOverlayTakesClicksOverItsArea` asserting `childAt` is the overlay and that a click beside it still selects the cell.
- [ ] **Step 2: Run** both test targets. Expected: the new tests FAIL (no `select`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** both. Expected: PASS, including every existing chart test in `desktop`.
- [ ] **Step 5: Commit.**

### Task 4: Insert → Chart

**Files:**
- Modify: `desktop/mainwindow.h`, `desktop/mainwindow.cpp`, `tests/test_chart_authoring.cpp`

**Interfaces:**
- Changes: `QString MainWindow::submit(QJsonObject operation)` now returns the `operation_id` it sent, or an empty string when it sent nothing (existing callers ignore it).
- Produces: `void MainWindow::insertChart();` actions `insertChartAction`, `selectNextChartAction`, `selectPreviousChartAction` in a new menu `insertMenu` created between Edit and Format and added to `m_sheetMenus` and `m_editingMenus`; private `void updateChartActions();` (enablement); private `QString m_pendingChartSelect;` (operation id whose receipt's `chart_id` is selected after the refresh).

Flow: `selectedRange()` (its refusals already raise an issue) → `POST base()+"/charts/suggest"` with `{expected_state, sheet: m_model->sheet(), r1, c1, r2, c2}` → on error `issue(message)` and nothing is inserted → `submit({action:"chart_create", spec, anchor})`. Anchor: row `r1`, column `c2+1`; when column `c2+1` is past the model's last column or hidden, row `r1`, column `c1`. On the committed receipt (`operationReceipt` with the stored id) remember `receipt["chart_id"]`; when the layer next emits `listed`, `select` it and call the private `void MainWindow::showChartPanel();` (a no-op stub in this task; Task 6 makes it show the dock and focus stays on the chart). Status on success: "Chart inserted • Undo removes it".

- [ ] **Step 1: Write the failing tests:** `insertMenuSitsBetweenEditAndFormat`, `insertChartCreatesASelectedChart` (anchor row and column, engine default name "Chart 1" and size 480x300, selected), `oneUndoRemovesTheInsertedChart`, `insertWithoutNumericDataShowsTheReason` (issue text contains "no numeric data"; chart count unchanged), `insertAnchorsOnTheSelectionWhenNoColumnIsToTheRight` (Review Focus 5: hide the column to the right), `insertIsRefusedOverHiddenRows`, `insertIsDisabled_data` (no workbook, read-only, projected view, relational workbench), `insertAtTheChartLimitShowsTheReason` (64 charts created through `submit`).
- [ ] **Step 2: Run.** Expected: FAIL, no `insertChartAction`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.**

### Task 5: Move, resize, order, delete, context menu and keys

**Files:**
- Modify: `desktop/chartlayer.h`, `desktop/chartlayer.cpp`, `desktop/mainwindow.cpp`, `tests/test_chart_authoring.cpp`

**Interfaces:**
- Consumes: `ChartSpec::resized`, `ChartSpec::clampSize`, `MainWindow::submit`.
- Produces on `ChartLayer`:
  - `bool placement(const QRect &rectInGridArea, QJsonObject *anchor, QJsonObject *offset) const;` the inverse of `relayout()`: the cell under the rectangle's top-left corner in the pane under it, as 1-based `{row, col}`, and `{x, y}` inside that cell; false when no cell is there (Decision 5).
  - signals `void moveRequested(const QString &chartId, const QJsonObject &anchor, const QJsonObject &offset);` `void resizeRequested(const QString &chartId, const QJsonObject &anchor, const QJsonObject &offset, const QJsonObject &size);` `void deleteRequested(const QString &chartId);` `void orderRequested(const QString &chartId, const QString &order);` (`"front"` or `"back"`) `void contextMenuRequested(const QString &chartId, const QPoint &globalPosition);`
- `MainWindow` connects each to one `submit`: `{action:"chart_update", chart_id, anchor, offset}`, the same with `size`, `{action:"chart_delete", chart_id}`, `{action:"chart_update", chart_id, order}`. The context menu holds `editChartAction`, `chartToFrontAction`, `chartToBackAction`, `deleteChartAction` (also reachable with the menu key or `Shift+F10` on a selected chart).

Behaviour: during a drag the overlay follows the pointer (geometry only, no request) and Escape or a release at an invalid place restores it; a release that moved nothing sends nothing. The overlay accepts `QEvent::ShortcutOverride` for Delete, Backspace, Escape, Enter, Return and the arrow keys so the window's Clear Contents shortcut does not fire, then handles them in `keyPressEvent` per Decision 3. A keyboard move past offset 4096 or into no cell is refused locally with the status "The chart cannot move further". A refused update (the service's message through `issue`) leaves the catalog unchanged, so the relist puts the overlay back.

- [ ] **Step 1: Write the failing tests:** `dragMovesAChartWithOneUpdate` (the stored anchor and offset equal `placement` of the release rectangle; one undo entry), `dragIntoAFrozenRowAnchorsThere`, `escapeCancelsADrag`, `releaseOverAHeaderCancelsTheDrag` (Review Focus 2), `clickWithoutMovingSendsNothing`, `handleDragResizes_data` (eight handles), `resizeStopsAtTheMinimum`, `arrowKeysMoveAndShiftArrowsResize`, `deleteKeyDeletesTheChartNotTheCells` (the cell under the grid cursor keeps its value), `backspaceDeletes`, `contextMenuOffersTheFourCommands`, `bringToFrontAndSendToBack` (z order in the catalog), `brokenChartCanBeMovedResizedAndDeleted` (Review Focus 4), `nothingMovesWhileBusyOrReadOnly`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** both test targets. Expected: PASS.
- [ ] **Step 5: Commit.**

### Task 6: The side panel: dock, Chart, Type, Title, Axes and Legend

**Files:**
- Create: `desktop/chartpanel.h`, `desktop/chartpanel.cpp`
- Modify: `CMakeLists.txt` (sources), `desktop/mainwindow.h`, `desktop/mainwindow.cpp`, `tests/test_chart_authoring.cpp`

**Interfaces:**
- Consumes: `ChartSpec::inputSpec`, `withType`, `takesGrouping`, `stateReason`; `ChartLayer::selectionChanged`, `editRequested`, `chart`.
- Produces `class ChartPanel : public QWidget` (objectName `chartPanel`):
  - `void setChart(const QJsonObject &chart);` (`{}` shows "No chart selected") `QString chartId() const;`
  - `void setLocked(bool);` (busy or read-only)
  - `void refused(const QString &field, const QString &message);` puts the named field back to the chart's value and shows the message beside it until the next `setChart` or edit of that field.
  - signals `void changeRequested(const QString &chartId, const QJsonObject &changes, const QString &field);` (`changes` holds exactly the `chart_update` fields: `name`, `order`, or `spec`) `void pickRequested(const QString &field);` (Task 8)
  - Field object names: `chartName`, `chartToFront`, `chartToBack`, `chartType_<type>`, `chartGrouping`, `chartTitle`, `categoryAxisTitle`, `valueAxisTitle`, `valueAxisMin`, `valueAxisMax`, `legendPosition`, `chartPanelReason`, and `<field>Message` for each refusal label.
- `MainWindow`: dock `chartDock` (title "Chart", right area, hidden at start, `toggleViewAction` in View), opened by `editRequested` and by insert; `changeRequested` → `submit({action:"chart_update", chart_id, ...changes})`, remembering operation id → field; a rejected `operationReceipt` for that id calls `refused(field, message)`; every `listed` calls `setChart(layer->chart(layer->selectedChart()))`; `setBusy` calls `setLocked`.

Behaviour: choices (type, grouping, legend, order buttons) emit on change; text fields emit on Enter and on focus-out only when the text differs from the chart's value; minimum and maximum accept a number or empty (null). A type change builds the spec with `withType`; a local refusal goes to `refused` without a request. While a request from this panel is in flight no second one is emitted. When `inputSpec` gives a reason, `chartPanelReason` shows it and only the Chart group is enabled (Decision 2 covers broken sources in Task 7).

- [ ] **Step 1: Write the failing tests:** `dockOpensOnInsertEditAndDoubleClickNotOnAClick`, `panelSaysNoChartSelected`, `renameAppliesOnEnterAndOnFocusOut`, `unchangedTextSendsNothing` (Review Focus 3), `eachTypeButtonChangesTheType_data`, `typeChangeCarriesItsCompanions` (column to scatter and back, through the service), `twoSeriesToPieIsRefusedAndTheTypeGoesBack` (the service's message beside the buttons; chart unchanged), `titleAxisTitlesMinMaxAndLegendApply`, `emptyMinimumMeansAutomatic`, `minimumAboveMaximumIsRefused`, `frontAndBackButtonsReorder`, `everyPanelChangeIsOneUndo`, `panelClearsWhenItsChartIsUndoneAway` (Review Focus 1: focus in `chartTitle` with edited text, undo the creation from the Edit menu, no `chart_update` is sent), `panelIsLockedWhileBusyAndWhenReadOnly`, `unsupportedVersionShowsTheReasonAndOnlyTheChartGroup`.
- [ ] **Step 2: Run.** Expected: FAIL, `chartpanel.h` not found.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.**

### Task 7: The Data group: sources and series

**Files:**
- Modify: `desktop/chartpanel.h`, `desktop/chartpanel.cpp`, `desktop/mainwindow.cpp`, `tests/test_chart_authoring.cpp`

**Interfaces:**
- Consumes: `ChartSpec::sourceText`, `sourceToInput`, `parseRange`, `hasBrokenSource`.
- Produces on `ChartPanel`: `void setCatalog(const QJsonArray &sheets, const QJsonArray &tables, const QJsonArray &queries);` (sheets as `{id, name}`; tables and queries as `POST base()+"/tables"` with `{expected_state}` returns them). `MainWindow` calls it after every refresh while the dock is visible.
- A source field (`class ChartSourceField : public QWidget`, in `chartpanel.cpp`) with a kind chooser (`Range`, `Table column`, `Query column`), a line edit and pick button for Range, and two combo boxes (table or query; column) otherwise. Object names: `categoriesSource`, `series<N>Name`, `series<N>Values`, `series<N>X`, `addSeries`, `removeSeries<N>`, `seriesUp<N>`, `seriesDown<N>`. A series name field offers Text or Cell only; a cell must be a single cell.

Behaviour: every completed source edit, remove and reorder is one `changeRequested` with a whole `spec`. Add Series adds a row to the panel only and emits nothing until its values (and x, for scatter) are filled. Removing the last series is refused locally ("A chart needs at least one series"). With a broken source (Decision 2) source edits are held and marked "Not applied yet" and one request is sent when none is broken. A range typed with an unknown sheet or a bad reference is refused locally beside the field.

- [ ] **Step 1: Write the failing tests:** `sourceFieldsShowTheStoredSources` (range, table, query text), `typedRangeChangesTheValues`, `tableColumnAndQueryColumnCanBeChosen` (through the service, then the catalog holds a `table` and a `query` source), `seriesNameTakesTextOrOneCell`, `addSeriesIsPanelOnlyUntilItsValuesAreGiven` (no operation before; one after), `removeAndReorderSeries`, `lastSeriesCannotBeRemoved`, `badReferenceIsRefusedBesideTheField`, `sourceOverTheCellLimitShowsTheServiceReason`, `brokenSourceIsRepairedWithOneUpdate` (delete the source sheet, pick a new range for each broken field, exactly one `chart_update`), `scatterSeriesShowAnXField`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.**

### Task 8: Picking a range on the grid

**Files:**
- Modify: `desktop/mainwindow.h`, `desktop/mainwindow.cpp`, `desktop/chartpanel.cpp`, `tests/test_chart_authoring.cpp`

**Interfaces:**
- Consumes: `ChartPanel::pickRequested(field)`, `ChartSpec::rangeSource`.
- Produces: `void MainWindow::beginChartPick(const QString &field);` `void MainWindow::endChartPick(bool apply);` `bool MainWindow::chartPickActive() const;` and `void ChartPanel::setPicked(const QString &field, const QJsonObject &rangeSource);` (fills the field; applies as a typed range would when `endChartPick(true)` is called).

Behaviour: while a pick is active the grid's selection on any sheet fills the field live, a click on the grid does not deselect the chart, editing commands are disabled the way the external Python pick disables them (`pickBlockedActions`), and the status reads "Pick a range for <field label> • Enter applies • Escape cancels". Enter applies, Escape cancels and restores the field; both return to the chart's sheet, select the chart and focus the field (Decision 9). Starting an edit, closing the workbook or a projected view cancels the pick. A series name pick accepts one cell only and says so in the status when more are selected.

- [ ] **Step 1: Write the failing tests:** `pickFillsTheFieldFromTheGridSelection`, `pickKeepsTheChartSelected`, `enterAppliesThePickAsOneUpdate`, `escapeCancelsThePickAndRestoresTheField`, `pickOnAnotherSheetNamesThatSheetAndReturns`, `namePickNeedsOneCell`, `pickIsCancelledByClosingTheWorkbook`, `editingCommandsAreDisabledDuringAPick`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** both test targets and the service suite. Expected: PASS.
- [ ] **Step 5: Commit.**

### Task 9: Documents and the acceptance run

**Files:**
- Modify: `docs/design/charts-authoring-export-design.md` (status line: authoring built; an "as built" paragraph in sections 4 and 5 for every departure, the nine decisions included), `docs/design/charts-design.md` (section 9 and the stages table), `docs/design/charts-import-view-design.md` (the "view-only" statements), `docs/design/implementation-status.md` (a new dated section: scope, verification, not verified), `docs/design/README.md` (links to this plan and the wireframes), `docs/design/macos-native-interaction.md` if it lists menus or shortcuts
- Create: `docs/design/evidence/charts-authoring-verification.txt`

- [ ] **Step 1: Run everything:** both desktop test targets and the service suite; record commands, counts and the commit in the evidence file.
- [ ] **Step 2: Acceptance run in the built application** (`build/DataHorizon.app`), once with the mouse and once with the keyboard alone, recording each step's result: insert a chart from a selection; change its type, title, a series source to a range, to a table column and to a query column, an axis title and bounds, the legend; move, resize, reorder and delete it; undo and redo each step; trigger each refusal of design sections 4 and 5 and confirm the reason is shown and nothing changed. If the run is driven through automation rather than by hand, say so.
- [ ] **Step 3: Export check:** export the workbook holding a chart made in the application to XLSX and ODS, read both back with the import preview, and record that the chart came back equal. Opening in Excel and LibreOffice: run where installed, otherwise record "not verified".
- [ ] **Step 4: Record as open** anything not run: VoiceOver, physical-input acceptance, Windows and Linux.
- [ ] **Step 5: Update the documents and commit.**

## Self-review record

- Design section 4: Insert (Task 4), Select (3), Move and resize (5), Delete and order (5), Keyboard (3, 5, Decision 3), States (3, 5, 6), After any change (existing refresh; 3 and 6 pin the selection). Section 5: dock and groups (6, 7), Sources and picking (7, 8), Applying and refusals (6, 7), type companions (2, 6), Add Series (7), broken `spec_patch` (Decisions 1 and 2), Service changes (none; Global Constraints), Wireframes (1). Section 7 Authoring: sign-off (1), keyboard and mouse run (9), refusals (4 to 8, 9), export of an authored chart (9), desktop tests and open items (2 to 9).
- Not covered on purpose (design non-goals): workbook-Python chart operations, copy as image, cut/copy/paste, multi-selection, zoom (the desktop has none).
