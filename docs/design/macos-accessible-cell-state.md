# Accessible cell-state increment

Task 8 gives grid accessible text/description an explicit source coordinate,
loading state, pending calculation state, blank value, computed result or error.
Pending and failed values say that the stored input is retained. Descriptions
bound values to 256 characters and errors to 512; they do not trigger full-source
reads or synchronous calculation. Projected rows name the original source row.

View → Read current cell (Command+Shift+I on macOS) presents the same bounded
text in an ordinary named, plain-text modal. It provides a keyboard-accessible
way to inspect the calculated result separately from the editable source field;
Escape/OK return without edits. The grid description also follows the current
cell. The modal is a snapshot, so a pending result requires reopening after
calculation completes. It does not announce later results over active typing.

Automated contracts verify loading → pending → computed descriptions, projected
coordinates, failure/input retention and a native-widget read-only dialog.
These checks do not certify spoken output, VoiceOver table navigation or an
accessible editing workflow. Qt's default full-sheet table accessibility also
needs an explicit bounded-enumeration audit: its conceptual row/column count is
much larger than a small materialized table. No screen-reader performance claim
is made from accessible names and extents alone.

The default Qt full-sheet table exposed a conceptual extent above 32-bit child
index capacity. Native automation acquired the installed app and opened Supplies
through the real OS dialog, then stalled after address navigation. The grid and
frozen panes now expose a bounded named keyboard pane and its viewport/editor,
rather than enumerating every conceptual table cell. Table-style screen-reader
row/column navigation is not provided by this tier; use the address/input
controls and result-inspection command. Grid keyboard editing and the stored
sheet extent are unchanged. A regression verifies the full conceptual extent
while the accessible pane exposes exactly one viewport child.

Actual VoiceOver use is pending. No VoiceOver settings were changed and no spoken
walkthrough was recorded. Task 8 and the screen-reader portion of M7 remain open. Validate
address navigation, source and result reading, blank/pending/error distinctions,
editing/cancellation, frozen/projected rows and dialog recovery in a usable Mac
session before closing that gate.
