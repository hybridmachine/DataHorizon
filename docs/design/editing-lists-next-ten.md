# Editing and list usability: next ten increments

Authorized November 7, 2026 after PR #44. Execute sequentially: implement, verify
locally, commit/push, publish/review/fix/merge one PR before starting the next.
Hosted build credits are exhausted. Existing managed/direct, atomicity, storage
and release contracts remain in force.

1. Formula-aware same-sheet Cut/Paste.
2. Selection across frozen panes.
3. Freeze first column.
4. Freeze multiple rows/columns at the selected cell.
5. Data-aware Ctrl+Arrow/Ctrl+Shift+Arrow navigation.
6. Expanded multi-key sort/filter editor.
7. Copy filtered results in display order.
8. Export filtered results from a consistent snapshot.
9. Saved named sort/filter views after reopen.
10. Bounded, state-checked Find navigation beyond 500 matches.

## Formula-aware Cut

Same-sheet Cut moves exact source/kind/style records, including empty Text/NULL
and absent cells, with destination overwrite and overlap handled from a source
snapshot. All workbook scalar references into the original cut rectangle follow
the move, including absolute markers, names/UUID-qualified references and targets
that were absent. Moved formulas retain references to cells outside that rectangle;
Copy/Fill relative translation is separate. Fully contained referenced ranges move
as a whole; disjoint ranges remain unchanged. Partially intersecting ranges and mixed-sheet
ranges involving the cut sheet or unsupported syntax reject the entire move before writing. References to
overwritten destination cells still refer to those destination coordinates.

Source/destination footprints remain within grid bounds; zero-distance moves are
no-ops. Cut state must match the reviewed state. Cut stays within one sheet and
full paste only. The source snapshot is capped at 4 MiB; reference scans at 10,000
formulas/8 MiB; the existing five-second operation, 10,000 changed-coordinate and
8-MiB before/after budgets include moved/dependent/hook-generated changes. Hooks
see final coalesced inputs and may reject all changes; Undo/Redo includes moved
cells, rewritten references and generated changes. No structure-event bypass or
schema change is introduced. This is a declared scalar compatibility stage, not
complete Excel move semantics.

## Cross-pane selection

Frozen panes and the body mirror one source-coordinate current cell and one
canonical rectangle. Plain/Shift arrows, Shift-click and left-button drag across
the visible top/body boundary use the same anchor; address ranges may include
the frozen row. Drag updates occur over visible cells (edge auto-scroll is not
introduced). Proxy selections show the rectangle's intersection without their
own cell caches. Body scrolling is retained when selecting the frozen header.
Hidden sections retain existing visibility/range-operation guards; list views
suspend freezing. Draft cancellation preserves the earlier rectangle and input.
Navigation produces no managed edits; range commands still use ordinary guards,
one atomic engine operation and Undo. Existing cell editors, headers, read-only
behavior, native geometry, resize synchronization and local preferences remain.

## Freeze first column

View → Freeze first column is independent of Freeze top row. Their intersection
has an editable corner pane. All panes forward source values, flags and edits
through the same REST-backed SheetModel cache and canonical selection. Scrolling,
source row/column dimensions, headers and keyboard focus are synchronized without
covering body cells. Sizing dialogs/fit/reset and header dragging update hidden
source sections and their visible panes, including saved custom dimensions.

The preference is local per workbook path/sheet, including read-only opens.
Projected lists suspend both edges; hiding column A suspends its edge until shown,
as hiding row 1 suspends the top edge. Draft capture/resolution and Save cover every pane. Automatic wrapping includes
frozen cells within the existing 64-row × 16-column loaded-preview measurement
budget, with one shared row height. Plain/Shift arrows and Enter cross panes;
Tab-specific traversal is part of the later keyboard-navigation increment.
There are no data/history/hook changes from freezing or navigation. The following increment extends
these panes to multiple rows/columns. Native automation and visual QA
cover layout, distant scrolling, selection, corner editing, persistence and guards;
physical input/screen-reader/cross-platform release acceptance remains open.

## Freeze selected panes

View → Freeze panes above and left of active cell freezes the source prefixes
before the active row/column (C3 freezes rows 1–2 and columns A–B). Prefixes are
limited to 16 rows and 8 columns and must leave at least a small scrolling body;
excessive counts, hidden prefix sections or insufficient room reject without
changing preferences. Unfreeze panes removes both edges while retaining custom
sizes and stored inputs. Top-row/first-column commands remain one-edge shortcuts.

Top, left and corner models resize their bounded source prefixes and retain the
canonical source selection, actual section sizes and one shared cache. Saved
counts use local preferences; old Boolean preferences migrate to one-row/column
settings, and malformed counts fall back safely. Projected/hidden prefixes suspend
edges. Saved multi-section prefixes that do not fit are suspended with an explicit
message; enlarge and reapply, or unfreeze. Draft resolution and workbook/sheet/state,
visibility, size and geometry checks protect the reviewed freeze request. Freezing
and navigation do not emit managed edits, hooks or history entries. The wrapping
measurement cap includes prefix cells and scrolling cells together.

## Completed PRs

1. [Formula-aware Cut #45](https://github.com/hybridmachine/DataHorizon/pull/45): locally verified, self-reviewed and merged.

2. [Cross-pane selection #46](https://github.com/hybridmachine/DataHorizon/pull/46): locally verified, self-reviewed and merged.

3. [Freeze first column #47](https://github.com/hybridmachine/DataHorizon/pull/47): locally verified, self-reviewed and merged.
