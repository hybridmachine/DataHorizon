# Next five editing and CSV increments

November 5, 2026. Continue everyday features after PRs #14–#18. Implement,
verify locally, open/review/fix and merge each item before starting the next.
Hosted CI remains unavailable under the user's local-verification authorization.

1. [Sheet rename, PR #19](https://github.com/hybridmachine/DataHorizon/pull/19), with formula reference updates.
2. [Sheet duplication, PR #20](https://github.com/hybridmachine/DataHorizon/pull/20), with reference preservation and copied self-references.
3. Sheet deletion with broken-reference transformation and warnings.
4. CSV encoding and delimiter controls.
5. CSV column typing with explicit conversion review.

## Sheet rename

Rename preserves stable sheet identity and retargets qualified name references
across the workbook using the existing formula parser's reference spans. New
names are quoted and apostrophes escaped; strings, unqualified references,
absolute markers, cell spelling and references through stable sheet UUIDs remain
intact. Name lookup follows SQLite's ASCII NOCASE rules. Native Text inputs that
look like formulas remain literal.

The ordinary tier scans at most 10,000 formulas and 8 MiB of sources/styles,
with the existing five-second operation deadline and input/history byte limits.
Unsupported formula syntax, overlong expanded sources, collisions and active
required handlers reject atomically. A structure-event hook contract remains
open. Successful rename clears history; its dialog states that consequence.
Rollback and replay retain the existing durable operation receipt contract.

Verification covers quoted/case/UUID/range/string references, reopen, Text
preservation, source/scan limits, collision, injected write failure and receipt
replay; SDK/REST and the real Qt rename dialog exercise the same operation.

[Rename local verification](evidence/rename-verification.txt): 205 Python tests
(15 optional-binding skips) and 83 Qt checks pass. Published diff reviewed; no
unresolved findings in this scope.

## Sheet duplication

Copied unqualified references resolve locally on the new sheet. Explicit name
and UUID references to the source are retargeted to the new sheet; references to
other sheets retain their targets. Original formulas/inputs/styles remain
unchanged. Text that looks like a formula remains literal. Copied styles and
input kinds survive reopen. The new sheet receives a new stable UUID and is
appended after the current last sheet.

Only the source's stored cells are scanned, at most 10,000 cells/8 MiB under the
existing deadline and history byte limits. Unrelated formulas cannot block a
copy. Unsupported formulas in copied cells, oversized expansion, duplicate
names and active required handlers reject atomically. Duplication clears Undo
history and the dialog explains self-reference handling and history clearing.

[Duplication local verification](evidence/duplicate-verification.txt): 209 Python
tests (15 optional-binding skips) and 84 Qt checks pass. Published diff reviewed
for original/copy isolation, name/UUID references, limits, rollback and replay.

## Sheet deletion

Delete replaces surviving qualified name/UUID references to the removed sheet
with permanent `#REF!` expressions. An inherited range such as `Sheet1!A1:A3`
becomes one error expression; unrelated references, literal strings and native
Text remain unchanged. Recreating the old name cannot reconnect broken links.
The receipt counts newly broken expressions and includes the first 100 affected
formula locations, shown in the desktop Issues dock. The confirmation warns
about reference errors and Undo history clearing and defaults to Cancel.

Deleted cells and rewritten surviving formulas share the 10,000 affected-cell
limit. Reference scans cover at most 10,000 surviving formulas, with an aggregate
8-MiB source/style budget, existing change-byte limits and five-second deadline.
Unsupported syntax or mixed-sheet ranges in surviving formulas, required hooks,
limits and the last-sheet guard reject atomically. Formulas being deleted do
not need transformation. Cell removals participate in the same tracked changes
as reference rewrites before the catalog entry is removed. Failure restores
cells, catalog and history; operation replay never repeats deletion.
