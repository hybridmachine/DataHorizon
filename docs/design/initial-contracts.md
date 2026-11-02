# Initial implementation contracts

Accepted for the local development preview on October 31, 2026 in response to
the instruction to execute the implementation plan. These contracts take
precedence over earlier proposals. They do not certify a production release.

## Persistence and outcomes

Submitted operations store automatically in one SQLite transaction, including
transactional Python edits, history, and the operation receipt. Escape cancels
an editor draft. Committed work is reversed with Undo. Save submits a draft and
waits for acknowledgement; Save a copy uses SQLite backup, never a live-file
copy. Untitled workbooks live in the user's application-data recovery directory
until explicitly removed. First Save creates a permanent snapshot and continues
there; the recovery source remains available. Closing waits for pending writes.
An unknown outcome permits a warned exit only after preserving the workbook
path and operation ID in a client recovery record. Restart/reopen reconciles
receipts before any retry; a missing receipt remains unknown.
An unsubmitted formula-bar draft survives an owned-service restart at its original
sheet and coordinates. Reopening requires review against the current stored
input before applying it. A rejected draft remains available for correction.
This includes in-cell edits: rejection restores them in the formula bar, and
service interruption captures an active editor before reopening resets the grid.
An unresolved formula-bar draft prevents opening a second in-cell editor.
Managed GUI mutations capture and resolve an active in-cell draft before
transmission. Cancel preserves the draft and submits no range action.
Open and Close capture an active in-cell draft before deciding whether to
discard it. Declining discard keeps the input visible in the formula bar and
leaves the current workbook open.
If the original sheet is unavailable, retain the input visibly as a draft and
reject applying it to another sheet. New edits require explicitly discarding the
unavailable-target draft first; Cancel retains it for copying.

Open publishes a handle after metadata loading succeeds. Failed opens close an
unreturned engine and consume no new handle. A native file whose creation has
already committed remains available for reopening after a metadata-load failure.
Failed creation also preserves its destination for inspection, even if it may be
incomplete. Automatic pathname deletion could remove another process's replacement.

Each mutation requires a client-generated operation ID (UUID recommended,
1–100 characters of valid Unicode text) and an expected state token. The SDK
rejects invalid IDs before transmission or pending-write tracking.
Receipts deduplicate the entire committed operation. Reusing an ID for different
content is rejected. Receipt lookup is repeated under the SQLite writer lock
before validating a new operation, so concurrent retries preserve the recorded
outcome. Rejected and rolled-back responses are explicit. A lost
reply is outcome unknown: look up the ID before submitting any new write. A
missing receipt is not proof that an in-flight operation failed. Calculation is
evaluated against the current read transaction; no persisted cached result is
trusted on reopen. Follow-up side effects are unavailable in the initial tier.
Confirmed transaction failures roll back operation changes to a savepoint and
retain the writer lock until their failure receipt commits. This includes
invalid inputs and unexpected internal exceptions after admission; internal
details remain in service diagnostics. Receipt-storage or
unconfirmed-rollback failures return an unknown outcome requiring reconciliation.
Missing receipts and failed receipt lookups preserve the unknown outcome and
operation ID.
For transmitted mutations, only a terminal response with the matching operation
ID can clear a client's pending write. Generic request rejections and errors
before writer-lock admission leave the operation unknown: another request with
that ID may still commit. Admission failures retain the ID and release any
acquired transaction. A reused ID with different content requires receipt
lookup for the original operation. Local read-only and size checks reject before
transmission without creating a pending write.
Replacing a GUI transport session invalidates its earlier replies;
pending mutations are reconciled against the reopened service. Reviewed bulk
operations retain their original state and sheet preconditions through dialogs.
Workbook, sheet and state are captured before a confirmation or input dialog,
and checked again after any editor-draft resolution. A background refresh cannot
retarget a confirmed deletion, sheet change, fill, format or export. Changed
inputs require a new review rather than silently using the newly selected sheet.
CSV import captures its workbook, sheet, state and destination cell before
resolving an editor draft or opening the file picker. Those preconditions remain
explicit through preview, confirmation and submission; a changed target cancels.
An active in-cell draft must be resolved before import starts.

## Values and references

JSON boolean request fields require `true` or `false`; strings, numbers and
null are rejected without coercion. Open validates its create/read-only flags
before creating a file or allocating a handle. Invalid cell clear/style flags
roll back their entire managed operation.

Addresses are one-based A1, at most 1,048,576 rows and 16,384 columns. Sparse
storage does not promise that every dense workbook within that address space is
practical. Raw input, typed value, formula, format, and display are distinct.
Auto entry recognizes finite decimal numbers and TRUE/FALSE; ISO dates remain
text. Text entry preserves 00123. Explicit empty text and NULL differ from an
absent cell. Numbers use finite IEEE-754 binary64 for calculation; integers are
accepted only within the exact JSON interoperability range. Decimal/currency
accounting and Excel date serials are not supported. Entry uses an invariant
decimal point; CSV defaults to text and requires a deliberate typing choice.
GUI edits preserve explicitly typed Text, including leading-zero and
formula-looking inputs. Recovered formula-bar drafts retain their original Text
intent. Cells stored with automatic typing continue to infer types on entry.

The first formula tier is +, -, *, /, ^, unary signs, parentheses, comparisons,
cell and rectangular range references, quoted/unquoted cross-sheet references,
SUM and lazy IF. Absolute components survive copy/fill; relative components move.
SUM ignores text and blank cells and rejects non-finite intermediate totals;
scalar arithmetic accepts numbers, booleans
and blanks, and rejects text. Errors retain source. Cycles are errors. Evaluation
is bounded by cell, depth, range, and time budgets. Unsupported functions return
#NAME?; external links, names, arrays, volatile and Python functions are deferred.
An evaluated non-finite numeric literal is #NUM! before comparison, branching
or arithmetic can consume it; an unselected IF branch remains unevaluated.

Sheet rename, duplication and deletion reject cross-sheet formulas until their
reference transformer is proven. Structural row/column changes and
cut involving formulas require a separately tested reference transformer before
being enabled. Sorting freeform cells with formulas is initially restricted;
record identity and reference-safe sorting require M5 evidence. Filtered paste
is disabled until stable-target semantics are implemented. These restrictions
are visible capability limits, not placeholder successful operations.
Exclamation marks inside formula string literals do not constitute sheet
references. Quoted sheet names still retain their following reference separator;
incomplete quoted formulas are handled conservatively.

## Undo, lists and code

Undo covers bounded cell/range inputs and formats, including generated edits.
Undo/Redo uses the sheet IDs recorded in history, independently of the request's
default sheet name or the current tab.
It is a new managed operation, runs applicable rules, and may be rejected
atomically. History is session-scoped, capped at 100 operations and 16 MiB,
invalidated by
external edits or unsupported structure changes. A new edit clears Redo.
External effects are never represented as undoable. Structure history, external
change gaps, and persistent history UI are deferred.

List operations require an explicit rectangular scope and explicit header flag;
they never guess a record boundary from the viewport. Blank cells are retained.
The first list implementation is a full-scope presentation projection over
source row coordinates. Single-cell edits retain those targets; range operations
require clearing the projection. Persistent record IDs remain M5/M8 work.
Full paste restores stored input kind and presentation independently; applying
a Text number format to a numeric source does not retype its copied value.
An empty projection has no editor target. Display replacement changes only Text,
preserves its typing/escape and skips identical replacements. Changing numeric,
boolean or formula displays requires editing their stored source instead.

Owned desktop/SDK services use an absolute trusted launcher in Python isolated
mode. Only the resolved service installation root joins the interpreter's paths;
ambient working-directory modules and `PYTHONPATH` cannot select service code.
Handler dependencies belong to the selected interpreter environment.
Only a service operator can explicitly activate a trusted Python file. Workbook
metadata and imports cannot activate code. A separate Python worker receives a
transaction-aware broker context; reads and edits join the parent transaction.
Actual changed inputs are processed in deterministic coordinate order, with
generated changes in subsequent rounds. No-op assignments produce no event.
Limits: 16 rounds, 10,000 changed cells, 2 seconds per worker invocation and
5 seconds total operation time. Failure to load, exception, timeout or recursion
limit rolls back. This is process containment, not a secure sandbox. Trusted
transactional code must avoid external effects. After-commit work is disabled.

## Jobs, limits and evidence

Ordinary operations are atomic, at most 10,000 target cells, 4 MiB request body,
64 KiB cell source and 5 seconds engine time. There are no chunked commits in
this tier. No partial-cancel claims are made: accepted writes run to their known
outcome; long/chunked jobs remain unavailable. Desktop paste checks raw clipboard
bytes and cell counts before constructing its
matrix, and serialized REST requests over 4 MiB are rejected before transmission.
A viewport is at most 4,096 cells; the GUI retains at most 16 tiles of 32 by 16
cells. Its estimated cache budget is
14 MiB for tiles plus 2 MiB for complete editing inputs. GUI tiles request bounded
128-character source/display previews with explicit omitted-source/value flags.
An omitted input is loaded separately in full and must match the tile's state
before editing is enabled; preview inputs/values cannot be pasted as complete
data. Full reads remain the default for REST/SDK callers. Copy/Cut uses one
consistent clipboard read supporting 10,000 selected cells. Both range and
clipboard response payloads, including their JSON envelope and separators, are
limited to 4 MiB. Sheet deletion rejects more
than 10,000 stored cells before its cascade. Formula evaluation permits
10,000 referenced cells, 128 dependency levels and 0.25 seconds per viewport.
CSV previews allow at most 10,000 records as well as 10,000 fields; blank records
count toward the record limit. The CSV export limit covers the complete encoded
JSON response, including escaped characters and metadata. Export destinations
that identify the current native workbook are rejected before any file write.

M1 reference hardware is the available Apple Silicon macOS host, SSD, at least
8 GiB RAM; record actual OS/CPU in evidence. Target warm p95 viewport and distant
jump <150 ms, edit acknowledgement without a worker <100 ms, with worker <500
ms, cancellation/timeout rollback <3 seconds, GUI input stalls <50 ms. Initial
GUI cache budget 16 MiB, GUI RSS <256 MiB, service RSS <256 MiB on sparse tests.
Measure cold/warm 100 samples, 100k sparse cells, 10k dense cells, long text,
formula chains and a disk dataset larger than the cache. These are targets,
not achieved performance claims; missing measurements block milestone closure.

## Focus and unavailable states

Enter submits and advances one visible row only after acknowledgement;
Shift+Enter moves in the opposite direction, within the sheet boundary. Save
commits without moving. Rejection or an unknown outcome retains the original
cell and draft. In a sorted/filtered list, capture the next visible source row
before editing and select it after refresh if it remains visible. Escape
cancels the cell editor. The formula bar
shows source and returns focus to the grid after submission. The address field
validates before jumping. Native shortcuts use Qt standard key sequences.
Loading cells show an ellipsis and are not editable. Pending mutations lock
editing and file/sheet changes. Rejections retain an actionable issue message;
unknown outcomes lock writes until reconciled. Read-only opens permit browsing
and snapshot copies. Empty sheets show A1 selected. Issues have text and do not
depend on color. System palette, native dialog ordering and accessible control
names apply; screen-reader and narrow-window qualification remains M7 work.

## Compatibility

Native format v1 preserves supported inputs, formula sources, styles and sheets.
The first exchange tier is UTF-8 CSV values, one explicit rectangle, with text
as the default import type. Formula-looking imported text stays literal. Excel
and ODS are unavailable until independent import/export fixtures and conversion
reports pass; no misleading extension-based export is allowed. Initial styles
are text/general/fixed two decimals, bold, italic, alignment and background.
Additional styles, frozen panes and view persistence require their own evidence.

## M1 experiment specification

Compare stdlib SQLite explicit engine mutations with SQL trigger/preupdate
capture. Verify direct writes, stale edits, rollback and backup. Managed SQL
writes remain unavailable until inserts, deletes, upserts, replacements,
triggers, cascades and old/new iteration are all proven for a declared surface.
Use stdlib localhost HTTP and a spawned Python worker for the first bounded
experiment; compare dependencies and transaction/cancellation costs before
calling either a production service choice. Exercise QTableView tiles,
QNetworkAccessManager ordering and QAbstractItemModelTester. Record selected
toolchain, correctness tests, measurements and remaining blockers in
[implementation status](implementation-status.md).
