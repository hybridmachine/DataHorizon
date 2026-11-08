# Editing and formatting usability: next ten increments

Authorized November 7, 2026 after PR #74. Each increment is locally built/tested,
committed, pushed, opened as a PR, author-reviewed on its published final head,
and merged before the next. Hosted Actions credits are exhausted.

1. F4 cycles relative/absolute references in source and cell editors.
2. Cross-sheet formula reference picking.
3. Navigation from precedent/dependent trace results.
4. Unified Format Cells dialog for supported styles.
5. Configurable decimal places and toolbar controls.
6. Thousands-separator number formats.
7. Explicit currency display formats.
8. Font family, size and text color.
9. Individual border edges and line styles.
10. Format painter for copying styles between ranges.

Preserve exact inputs/types, shared REST/engine/transactional-hook behavior,
source-state/draft/late-reply fences and bounded work. Each style stage includes
applicable native persistence and XLSX/ODS mapping/reporting; formatting does not
retype values. This remains a local development preview, with cross-platform,
manual accessibility/input, runtime and distribution gates open.

## F4 reference cycling

In a writable automatic/formula draft, F4 cycles A1 → $A$1 → A$1 → $A1 → A1.
The caret targets one cell endpoint; a selection changes wholly selected cell
endpoints independently, including ranges/qualified references. A partial-cell
selection rejects the whole toggle. Sheet spelling, case and quoted Text remain
intact. References outside the supported grid do not toggle. Analysis retains up
to 1,024 references within the 64-KiB draft bound (visual cues stay capped at 64).
A toggle is one native draft Undo step; Enter submits the ordinary managed edit.
Literal Text/read-only editors do not toggle. Body/frozen delegates share this
behavior; no REST mutation, hooks or history occur merely from pressing F4.

## Completed PRs

1. [F4 reference cycling #75](https://github.com/hybridmachine/DataHorizon/pull/75): locally verified, author-reviewed and merged.

2. [Cross-sheet reference picking #76](https://github.com/hybridmachine/DataHorizon/pull/76): locally verified, author-reviewed and merged.

## Cross-sheet reference picking

Other sheet… beside the source bar (also Edit) transfers an unsubmitted in-cell
draft to the source editor and opens a separate read-only, virtualized preview.
Choose another sheet and click/drag a rectangle, or enter A1/range and press Enter
to select it. Insert reference quotes/escapes the sheet name and retains the
original source cell, input kind and draft. The preview supports 2–64 sheets and
at most 10,000 selected cells; no whole-workbook cache or writes are introduced.

A fresh single-cell REST read verifies the captured workbook state before one
native draft insertion. Changed service, target, state, cursor/selection or later
typing cancels the reply. Expired preview disables Insert; cancellation retains
the draft. Enter still submits one ordinary managed edit. Preview model callbacks
are lifetime-guarded, including replies arriving after the dialog is destroyed.

Unfinished/invalid address entry disables Insert until a valid Enter selection or a grid click replaces it; an older range is never used implicitly.
