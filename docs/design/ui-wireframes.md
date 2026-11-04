# Desktop UI wireframes

Status: Draft 0.1, October 31, 2026. Static interaction proposals for review;
these screens do not describe an implemented application or an approved release
scope. Qt Widgets with C++ remains the selected GUI stack.

Implementation has since begun by explicit instruction. These remain historical
wireframes; [initial contracts](initial-contracts.md) resolve selected proposals
and [implementation status](implementation-status.md) records actual behavior.

Page 8 implementation annotation: the Sheet menu now inserts/deletes the
selected contiguous whole-row or whole-column span. Confirmation states that
the full span changes, references across sheets adjust and Undo history clears.
Cancel retains the original workbook. New #REF! expressions are reported with
formula locations in Issues; stale dialog context, required handlers, projected
lists, unsupported formula syntax and oversized edits reject explicitly. The
[initial structural contract](initial-contracts.md) defines the bounded tier;
the historical PDF has not been regenerated.

The [wireframe PDF](../../output/pdf/datahorizon-ui-wireframes.pdf) contains 18
landscape pages. It follows the [everyday spreadsheet user stories](../../output/pdf/datahorizon-everyday-spreadsheet-user-stories.pdf),
including their Supplies sample session. The PDF has selectable text, vector
linework, an outline, and links from its reading map.

The [implementation plan](implementation-plan.md) maps these screens to delivery
increments, dependencies, and completion criteria. A
[PDF edition](../../output/pdf/datahorizon-implementation-plan.pdf) is available
alongside this wireframe set. Visual direction has been reviewed; the interaction
and storage proposals below still require explicit contracts before coding.

## Screen map

| PDF page | Surface | Story coverage |
| --- | --- | --- |
| 1 | Design basis and reading map | All stories |
| 2 | Main workbook window | 01, 04-06, 09, 12-14 |
| 3 | Create, open, and read-only access | 01, 02, 04 |
| 4 | Selection, cell editing, and address navigation | 05, 06 |
| 5 | Copy, paste special, and fill | 07, 08, 18 |
| 6 | Formula source, calculation freshness, and errors | 09, 10 |
| 7 | Cell formatting, freeze, and view options | 13, 14 |
| 8 | Sheet, row, and column structure | 11, 12 |
| 9 | Sort scope, headers, and key precedence | 15 |
| 10 | Filter criteria, record targets, and clear filters | 16 |
| 11 | Find/replace dock and undo behavior | 17, 18 |
| 12 | Import preview, column types, and conversions | 03 |
| 13 | Storage states, close, and save failure | 02, 04, 10 |
| 14 | Rule rejection and after-commit failure | 20 |
| 15 | Export scope, snapshot, and conversion review | 19 |
| 16 | Supplies walkthrough and expected values | Sample session |
| 17 | Qt Widgets mapping and platform behavior | Handoff |
| 18 | Story coverage and unresolved decisions | Review |

## Proposed interaction direction

Use a conventional desktop main window: menus, compact toolbars, an address box,
a formula bar, row/column headers, a central grid, sheet tabs, and a status bar.
Keep SQL, Python authoring, and other advanced tools available through menus and
optional docks without requiring them for ordinary spreadsheet work.

The formula bar exposes stored input or formula source while the grid displays
the calculated or formatted value. Editing, selection, and pending calculation
must be visibly distinct. Errors retain the underlying formula and identify an
affected cell or operation. Search and issue details use resizable panels;
conversion and scope review use task dialogs.

The PDF proposes automatic storage for each submitted managed operation. An
unsubmitted cell editor remains a cancellable draft. Each submitted edit, paste,
or fill runs applicable handlers and either commits or rolls back as a unit.
Save finishes pending edits and waits for a known storage outcome. An untitled
workbook chooses a native location on first Save. This persistence model is a
proposal for review, not a replacement for the open persistence decision in the
stories. Temporary workbook storage, first-save mechanics, crash recovery,
cancellation, and operation history still need detailed design.

Closing can discard an unsubmitted draft; it cannot discard already committed
data or assume that an in-flight request rolled back. Unknown commit outcomes
must be resolved before retry. A committed edit with pending calculations is
different from an uncommitted draft. A failed after-commit action is reported
separately from transaction rollback.

The screens preserve the existing managed/direct boundary: normal GUI edits use
the shared engine through REST. Reopening independently edited SQLite data
refreshes stored inputs and invalidates derived state without replaying skipped
change handlers. Ordinary configured open events remain separate.

## Qt Widgets fit

The visual language uses rectangular controls, restrained selection emphasis,
standard headers, ordinary scrollbars, dock titles, and task dialogs. The light
palette is a neutral schematic; it does not prescribe identical rendering on
Windows, macOS, and Linux. Follow system fonts, palette, focus indicators,
spacing metrics, menu placement, and platform button order.

| Surface | Candidate building blocks |
| --- | --- |
| Main window and actions | `QMainWindow`, `QMenuBar`, `QToolBar`, `QAction` |
| Grid | `QTableView`, `QHeaderView`, model/view interfaces, delegates |
| Address and formula input | `QLineEdit`, ordinary widgets and layouts |
| Sheets and panels | `QTabBar`, `QDockWidget`, `QSplitter` |
| Task dialogs | `QDialog`, `QFormLayout`, `QDialogButtonBox` |
| Fields and status | `QComboBox`, `QSpinBox`, `QStatusBar`, `QProgressBar` |

These are feasible building blocks, not a claim that Qt provides complete
spreadsheet behavior. Range editing, formula coordination, fill handles,
reference adjustment, frozen panes, and bounded service-backed viewport reads
need custom design. The detailed grid implementation remains open. Qt's frozen
column example illustrates coordinated table views rather than a built-in
spreadsheet freeze command.

All commands need keyboard access. Use platform standard shortcuts where
available. Convey errors, focus, selection, and pending work with text or shapes
as well as color. At smaller sizes, overflow toolbar actions and hide optional
docks while retaining formula entry and storage/calculation feedback. Dedicated
narrow-window and accessibility wireframes are follow-up design work.

## Review and next pass

Walk the PDF's page 16 sequence: initial total 18.00; changing Pen quantity from
4 to 6 yields Pen amount 7.50 and total 20.50. Quantity > 2 shows Pen and Folder,
and clearing the filter restores Notebook. Reopen retains editable formulas;
export reports conversions and preserves the native workbook.

Resolve these before application implementation:

- Review automatic per-operation storage versus another explicit persistence
  model, including untitled workbook recovery and first-save behavior.
- Define reference adjustments for sort, cut, insertion/deletion, and duplicated
  sheets; define paste/fill targets in hidden or filtered ranges.
- Specify undo scope, history lifetime, rule execution during reversal, and
  handling of external side effects.
- Declare first formula and interchange compatibility tiers, locale/type rules,
  and actual unsupported-feature fallbacks.
- Define transaction limits, cancellation boundaries, large imports, loading
  states, and measurable grid/calculation budgets.
- Refine keyboard focus, accessibility, narrow layouts, frozen/hidden indicators,
  and full conversion-report states.

This iteration does not settle freeform sheet/table/view mapping, SQL and Python
authoring, remote service setup, or business workflow UI. No application code,
scaffolding, or executable UI prototype was created.

## Sources

Product and access requirements come from `AGENTS.md` and the other documents in
this directory. Stable story IDs refer to the existing user-story PDF.

Qt feasibility references (official documentation):

- [Application main window](https://doc.qt.io/qt-6/mainwindow.html)
- [QTableView](https://doc.qt.io/qt-6/qtableview.html)
- [Frozen column example](https://doc.qt.io/qt-6/qtwidgets-itemviews-frozencolumn-example.html)
- [QDialogButtonBox](https://doc.qt.io/qt-6/qdialogbuttonbox.html)
- [QStyle](https://doc.qt.io/qt-6/qstyle.html)
- [QKeySequence](https://doc.qt.io/qt-6/qkeysequence.html)
