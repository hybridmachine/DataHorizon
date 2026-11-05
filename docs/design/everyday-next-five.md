# Next five everyday spreadsheet increments

November 4, 2026. The user prioritized everyday spreadsheet features after
PRs #9–#13. Implement, locally verify, review and merge each increment before
beginning the next. Hosted Actions remain billing-blocked; local verification
and sequential merges are explicitly authorized. M1 qualification stays open.

1. Text wrapping with native stored styles and bounded visible-row measurement;
   retain the expanded native keyboard/accessibility/cancellation checks.
2. Cell borders with atomic styles, Undo/Redo and copy/paste preservation.
3. Row/column sizing with bounded fit measurement and saved desktop view state.
4. Hide/unhide rows and columns with visible recovery controls.
5. Freeze the top row with synchronized geometry, selection and saved state.

## Text wrapping

Format → Wrap text toggles the selected rectangle using the shared managed edit
operation. The native boolean style participates in existing atomic rollback,
history, paste, fill and reopen behavior. Display measuring inspects only loaded
visible cells, at most 64 rows × 16 columns, using bounded preview text and a
260-pixel automatic row-height cap. No full-sheet scan or complete-source fetch
is performed for sizing. Full source remains available in the formula bar.

The native-plugin acceptance executable also checks range/Tab navigation,
platform edit-key entry and Escape cancellation in the cell editor and Add Sheet
Qt modal. In-cell editors expose their source address to accessibility.
[Cocoa scale 1](evidence/native-interaction-1.json) and
[scale 2](evidence/native-interaction-2.json) have matching screenshots at a
560-pixel width. Physical input, screen-reader navigation and native system file
panels remain unqualified; hosted Windows/Xcb checks remain pending.

## Cell borders

Format → All cell borders / Clear cell borders use shared native styles and
existing rollback, history and clipboard contracts. Engine fixtures check
invalid-style atomicity, formula preservation, formats-only paste, Undo/Redo
and reopen. A real REST-backed Qt fixture renders the delegate and verifies
that the edge appears and clears without changing input.

## Row and column sizing

Sheet → Row height / Column width uses logical pixels; dragging a header edge
also saves its size. Fit columns to visible text measures only loaded previews,
at most 16 selected columns × 64 visible rows, with a 1,000-pixel fit cap.
Explicit sizes range from 18–1,000 pixels for rows and 30–2,000 for columns.
Each axis supports at most 1,024 explicit sizes. Reset sizes restores defaults
and automatic wrapping. Size changes do not alter cell inputs or history.

Preferences are local to this computer, keyed by native workbook identity,
resolved file path and stable sheet identity. They are available for read-only
opens and do not travel with native snapshots or exports. Coordinates retain
their sizes across cell edits and projected-list reordering; structural edits
leave sizes attached to coordinate numbers rather than transforming them with
cell contents. Invalid preference entries are ignored. Native portable workbook
view metadata remains a later format decision. Tests and probes use isolated
preference files.

## Hide and unhide

Sheet → Hide selected rows/columns saves at most 1,024 source-coordinate entries
per axis in the same local preferences as sizes. Header ellipses mark adjacent
hidden sections. A persistent status button shows counts and unhides both axes;
Sheet also has separate Show all rows/columns commands. Hiding never deletes
inputs or changes managed history. Source rows retain their hidden state through
list sorting. At least one displayed row must remain visible; if applying a new
projection would hide all its rows, the first source row is explicitly shown.

Address navigation to hidden cells is rejected. GUI rectangle operations that
intersect hidden sections are blocked; paste checks the complete source/destination
extent, including TSV expansion, before submission. Structural edits and CSV import require unhidden
rows/columns. Clear the local hiding preferences to perform those operations.
Managed REST/CLI/SDK coordinates remain independent of local visibility.
A recovered draft explicitly unhides its original row/column for review.

## Freeze top row

View → Freeze top row displays an editable one-row model above the body. Both
views use the same bounded SheetModel cache and REST operations; the frozen view
has no cell cache. The body hides its duplicate row 1 and column headers; the frozen view
provides synchronized headers. A layout reserves real space for both views, so no
body cells are obscured. Column widths, hidden columns and horizontal scrolling
stay synchronized. Frozen header clicks select source columns; frozen horizontal
cell selections map to the source rectangle. Plain Up/Down moves across the pane
boundary. Editing, Enter advancement, Escape and draft capture use the existing
managed operation/recovery path. Read-only workbooks remain read-only.

Freeze and explicit frozen-row height persist in local sheet preferences.
Freezing is suspended in projected lists or while source row 1 is hidden, and
resumes when that condition clears. Only source row 1 is supported; multiple
frozen edges and range dragging/Shift-selection across the pane boundary are
outside this tier. Native workbook view metadata remains unchanged.

[Cocoa scale 1](evidence/everyday-native-1.json) and
[scale 2](evidence/everyday-native-2.json) include screenshots showing wrap,
borders, width, hidden-column recovery and frozen-row alignment together. Model
invariants and REST-backed Qt checks cover editing/cancellation, keyboard and
header selection, distant scroll geometry, shared-cache bounds, list suspension,
reopen/read-only behavior and saved frozen-row height. Hosted Windows/Linux
execution remains pending the billing block; these are local macOS results.

[Final local verification](evidence/everyday-verification.txt) passes 198 Python
tests with the optional binding environment and 82 Qt checks, plus native
acceptance and installed-app smoke. The [existing grid benchmark](evidence/everyday-desktop-benchmark.json)
still meets its cache/input assertions; its read-only probe remains separate
from the production freeze feature.
