# Next five editing and CSV increments

November 5, 2026. Continue everyday features after PRs #14–#18. Implement,
verify locally, open/review/fix and merge each item before starting the next.
Hosted CI remains unavailable under the user's local-verification authorization.

1. [Sheet rename, PR #19](https://github.com/hybridmachine/DataHorizon/pull/19), with formula reference updates.
2. [Sheet duplication, PR #20](https://github.com/hybridmachine/DataHorizon/pull/20), with reference preservation and copied self-references.
3. [Sheet deletion, PR #21](https://github.com/hybridmachine/DataHorizon/pull/21), with broken-reference transformation and warnings.
4. [CSV encoding and delimiters, PR #22](https://github.com/hybridmachine/DataHorizon/pull/22).
5. [CSV column typing, PR #23](https://github.com/hybridmachine/DataHorizon/pull/23), with explicit conversion review.

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

[Deletion local verification](evidence/delete-verification.txt): full CTest passes
215 Python tests (15 optional-binding skips) and 85 Qt checks. A further review
regression confirms deadline expiry preserves the catalog, cells and receipt;
all 18 focused sheet tests pass. The initial contract now reflects these scopes.

## CSV encoding and delimiters

File import now asks for an explicit encoding and delimiter, then sends bytes
to the shared REST preview operation. UTF-8 accepts a BOM by default; UTF-16
requires one and supports either byte order. Windows-1252 and Latin-1 are also
available. Decoding is strict: there is no guessing or replacement of invalid
bytes. The GUI offers comma, semicolon, tab and pipe; the API accepts any one
character except quote, NUL and line endings. CSV quoting remains the Python
parser's strict double-quote convention, including embedded delimiters/newlines.

The API accepts exactly one of Unicode `csv` or base64 `csv_base64`; only bytes
accept an `encoding`. Raw source and decoded UTF-8 are each limited to 3 MiB;
the complete request and response each retain their 4-MiB limit, including
base64/JSON overhead. Records/cells remain limited to 10,000 and each cell to
64 KiB of UTF-8. The dialog samples only the first eight rows/columns and 120
characters per field; all parsed cells are imported in one managed Text edit.
The preview never mutates. File/options/review dialogs retain the original
workbook, sheet, destination and state; changed context cancels the import.

[CSV controls local verification](evidence/csv-dialect-verification.txt): all 221
Python tests (15 optional-binding skips) and 86 Qt checks pass. Both UTF-16 byte
orders and missing-BOM rejection are covered. Review enforces literal PlainText
rendering so formula-like or HTML-like text cannot alter the review display.

## CSV column typing

Text remains the default; no inference activates formulas or code. Column types
lets the user select a 1-based CSV column and choose Number, Boolean, ISO date,
M/D/YYYY date or D/M/YYYY date. The first-record header option imports that
record as Text rather than skipping it. Controls use one column selector/type
chooser rather than allocating a widget for every possible source column.

Numbers and TRUE/FALSE booleans use the native strict input and precision rules,
without trimming or locale separators. Dates require the selected spelling and
valid calendar values; slash dates with two possible orders are counted in the
review. Dates normalize to YYYY-MM-DD stored as native Text, with no new date
storage type or serial arithmetic promise. Empty converted fields become blank;
Text fields preserve empty Text. Ragged records are not padded, and blank records
retain their destination row offsets. Formula-looking Text stays literal.

The preview returns original rows and, for successful explicit types, typed
`inputs` that clients can flatten into one managed edit. It never mutates. Any
failed conversion disables Apply and omits that typed payload; there is no
partial import or implicit Text fallback. The user can change types and request
a new review. Reports include total errors, first 100 error locations, total
ambiguous slash dates and first 20 date examples; the desktop displays bounded
samples. Text/typed payloads share the existing complete 4-MiB response limit.
The desktop also checks the full destination extent before review/submission.

The Qt review uses a compact two-row/four-column summary (40 characters per
field), with expandable, scrollable details for encoding, selected types,
conversion reports and the eight-row/eight-column sample. It renders literal
text and defaults to Cancel. The custom column action and disabled Import state
use a Qt dialog consistently across native platform plugins.

Types are local to the current review. All dialogs retain its original workbook,
sheet, coordinate and state; a context change cancels the import. One managed
edit preserves destination styles, Undo/Redo and applicable required hooks.
Hook rejection rolls back the complete import and generated edits, with the
normal durable receipt. Large jobs, locale numbers, native date/time types,
skipped columns and spreadsheet file formats remain later work.

[Final local verification](evidence/editing-csv-verification.txt) passes all 227
Python tests without skips in the optional-binding environment and 89 Qt checks.
Cocoa native acceptance passes at [scale 1](evidence/editing-csv-native-1.json)
and [scale 2](evidence/editing-csv-native-2.json). The native CSV typing and
error-correction flows pass at scale 2, with the [review screenshot](evidence/csv-types-review.png)
visually inspected. Installed-app edit/calculation smoke passes. Hosted runs
still start no jobs; no new hosted platform qualification is claimed.

Review fixed Cocoa custom-button behavior using Qt's
[documented dialog option](https://doc.qt.io/qt-6.8/qmessagebox.html#Option-enum),
shortened the main report into a compact summary with scrollable details, and
bounded the automated file-picker fixture. No unresolved findings remain in
these declared tiers. The next everyday dependencies are explicit progress and
cancellation/large-job semantics, followed by independent XLSX/ODS fidelity and
recovery increments. M1 and cross-platform release qualification remain open.
