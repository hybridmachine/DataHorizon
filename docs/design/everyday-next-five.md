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
