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
An attached-service window locks editing and new workbook/sheet commands while its
handle is being released. Late read completions cannot unlock it, and a release
callback cannot close a different, newly opened handle. Cleanup failure still
permits the requested exit without treating stored work as rejected.
If the original sheet is unavailable, retain the input visibly as a draft and
reject applying it to another sheet. New edits require explicitly discarding the
unavailable-target draft first; Cancel retains it for copying.

Open publishes a handle after metadata loading succeeds. Failed opens close an
unreturned engine and consume no new handle. A native file whose creation has
already committed remains available for reopening after a metadata-load failure.
Failed creation also preserves its destination for inspection, even if it may be
incomplete. Automatic pathname deletion could remove another process's replacement.
Snapshots likewise preserve failed destinations. Creation and copy reserve a new
file exclusively and require a usable filesystem identity; copy verifies it
before and after SQLite backup and rejects detected replacement. A filesystem
that cannot identify the reserved file returns `FILE_IDENTITY_UNAVAILABLE`.

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
absent cell. Numbers use finite IEEE-754 binary64 for calculation. Plain integer
spellings, including a zero decimal suffix or zero exponent, must fit the exact
JSON interoperability range. Other decimal/scientific inputs use binary64
rounding; identifiers requiring exact digits must use Text. Explicit Number
input uses the same invariant decimal grammar as auto entry, without spaces or
digit separators. Decimal/currency
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

Sheet rename, duplication and deletion use the bounded parsed reference
transformations in [the editing contract](editing-csv-next-five.md). Rename
retargets qualified names, copies retarget explicit self name/UUID references,
and deletion replaces surviving targets with permanent #REF! expressions and
reports affected formula locations. Unsupported syntax and required handlers
without structural opt-in reject atomically; successful structure changes clear
Undo/Redo history. Opted-in handlers follow the transactional before/after
[structure event contract](m3-m5-next-ten.md).

Same-sheet Cut follows the [bounded move/reference contract](editing-lists-next-ten.md#formula-aware-cut).
Whole-row/column insertion and deletion use the bounded reference transformer
described below. Sorting freeform cells with formulas is initially restricted;
record identity and reference-safe sorting require M5 evidence. Filtered paste
is disabled until stable-target semantics are implemented. These restrictions
are visible capability limits, not placeholder successful operations.
Exclamation marks inside formula string literals do not constitute sheet
references. Embedded NUL characters inside literals cannot hide a later
cross-sheet separator from structural guards. Quoted sheet names still retain
their following reference separator; incomplete quoted formulas are handled
conservatively.

Whole-row/column operations accept a 1-based `start` and positive `count`
(default 1). Insertion occurs before `start`; deletion removes that inclusive
span. Sheet UUIDs stay fixed; full stored cell inputs, kinds and styles move to
their new coordinates. This does not introduce persistent record identities.
References in all sheets follow the original target coordinates, including
references to absent cells. Both absolute and relative components adjust;
absolute markers only suppress copy/fill offsets. Insertion at a range's first
endpoint moves it, insertion inside a range expands it, and deletion contracts
the range around surviving members. Reversed endpoint order is preserved.
Deleted scalar targets, wholly deleted ranges and references shifted beyond
the grid become `#REF!`. String literals and typed formula-looking Text do not
change. Parentheses around either range endpoint preserve range membership and
sheet inheritance. Transformations use parsed ranges and retain source spelling
outside changed references; wholly broken ranges become one `#REF!` expression.
Ranges whose parsed endpoints are not cell references reject atomically.
Qualified ranges inherit their first endpoint's sheet. Sheet names use
SQLite's ASCII-only NOCASE matching, including quoted apostrophe escapes. Exact
quoted sheet UUIDs also follow the calculator's existing identity resolution.

Activated required handlers must explicitly implement the transactional structure
event contract; these operations never bypass an active handler. They clear Undo/Redo on
commit, and the desktop confirms that consequence before submission. A rejected
operation preserves the original cells, formulas and history. They reject
unsupported formula syntax instead of guessing at hidden references, and never
discard stored cells at the grid edge. The ordinary tier permits at most 10,000
affected coordinates, scans at most 10,000 workbook formulas, and bounds staged
source/style payloads to 8 MiB (moved formulas count in both scans). The existing
5-second operation and 8-MiB before/after budgets also apply. These are atomic
operations, with no chunked commits. Their receipts report newly broken
reference-expression counts and the first 100 affected formula locations after
the move. Projected list operations remain disabled; clear the list first.

## Undo, lists and code

Undo covers bounded cell/range inputs and formats, including generated edits.
Undo/Redo uses the sheet IDs recorded in history, independently of the request's
default sheet name or the current tab.
It is a new managed operation, runs applicable rules, and may be rejected
atomically. History is session-scoped, capped at 100 operations and 16 MiB of
serialized labels and cell changes across Undo and Redo; this is a payload
budget, not a bound on Python object overhead or process RSS. External edits or
unsupported structure changes invalidate it. Nonstructural operations clear Redo
only when stored cells actually change. No-op commits preserve Redo while still
receiving durable receipts and a new state.
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
5 seconds total operation time. The worker budget starts before process startup;
checks between startup calls and broker messages enforce the remaining budget.
Pipe messages use bounded JSON frames (1 MiB), not worker-supplied pickle.
Pipe reads/writes run off the transaction thread so a stuck peer can still be
terminated at its deadline. Construction/start failure, malformed/oversized
messages, EOF and broker startup failure all fail the required handler. Cleanup
closes both pipes and the reaped Process object. Diagnostic output retains the
last 8,192 characters without concatenating the full write into that tail.
OS process startup/termination and cleanup are not hard real-time guarantees.
Failure to load, exception, timeout or recursion
limit rolls back. This is process containment, not a secure sandbox. Trusted
transactional code must avoid external effects. After-commit work is disabled.

## Jobs, limits and evidence

The localhost development HTTP server admits at most 16 concurrent handlers.
Excess connections receive HTTP 503 `SERVICE_BUSY` with an unknown operation
outcome and no operation ID: no body was parsed, so clients must keep their
pending ID and reconcile it before another write. Accepted operations retain
their existing transaction and durable receipt semantics. Request line/headers
and body share a ten-second total read deadline, including trickled bytes;
aggregate headers are at most 64 KiB (the request line is separately bounded by
the standard parser). Responses have a five-second socket-write timeout.
These bounds do not qualify the development server for production or remote
binding. See [admission evidence](qualification-next-five.md#http-admission).

Connection teardown half-closes the response, then discards at most 64 KiB of
unread request bytes for at most 100 ms before closing. This bounded grace helps
deliver error responses on Windows without parsing or admitting rejected input.

Ordinary operations are atomic, at most 10,000 target cells, 4 MiB request body,
64 KiB cell source and 5 seconds engine time. Formula preflight queries check
that deadline inside SQLite even when no matching cells are returned, and remove
the interrupt handler before rollback/receipt recording. Managed validation uses
the remaining operation budget. Checks after hooks and immediately before COMMIT
reject operations whose budget is already expired.
Replace All uses that same deadline for SQL and display evaluation; standalone
Find has a two-second search budget. Snapshot backup and
validation also have a five-second budget, including busy retries. A timed-out
copy preserves its destination for inspection. These deadlines are cooperative
checks between SQLite instructions/backup steps, not hard cancellation of a
blocked operating-system I/O call. Ordinary operations and atomic jobs have no
chunked commits. Explicit [batched operation jobs](m3-m5-next-ten.md) commit each
batch separately and retain those receipts on cancellation. Before a commit,
confirmed cancellation rolls back that transaction; missing receipts remain
unknown. Native journal identity, reviewed resume and asynchronous close are
described in [the remediation contract](development.md#durable-job-recovery-and-close).
Desktop paste checks raw clipboard
bytes and cell counts before constructing its
matrix, and serialized REST requests over 4 MiB are rejected before transmission.
A viewport is at most 4,096 cells. Range/clipboard reads have a two-second engine
budget, including validation and SQL. Reads, Find and Replace All probe each
source row with indexed row-equality/column-range bounds instead of scanning
unrelated stored columns. All probes share one SQLite transaction and snapshot; results
retain row-major order, with projected source rows sorted and deduplicated.
The GUI retains at most 16 tiles of 32 by 16
cells. Its estimated cache budget is
14 MiB for tiles plus 2 MiB for complete editing inputs. GUI tiles request bounded
128-character source/display previews with explicit omitted-source/value flags.
An omitted input is loaded separately in full and must match the tile's state
before editing is enabled; preview inputs/values cannot be pasted as complete
data. Full reads remain the default for REST/SDK callers. Copy/Cut uses one
consistent clipboard read supporting 10,000 selected cells. Both range and
clipboard response payloads, including their JSON envelope and separators, are
limited to 4 MiB. Sheet deletion rejects more
than 10,000 stored cells before its cascade. Replace All checks each resulting
UTF-8 source size before allocating it, including any retained text-escape
prefix, so replacement expansion cannot bypass the 64-KiB cell limit.
Formula evaluation permits 10,000 referenced cells, 128 dependency levels and
0.25 seconds per viewport.
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
CSV values export is UTF-8 over one explicit rectangle. Import offers the strict
encoding/delimiter controls in [the CSV contract](editing-csv-next-five.md#csv-encoding-and-delimiters),
with Text as the default import type. Formula-looking imported text stays literal.
XLSX/ODS selection and workbook interchange are available within the independently
tested [compatibility tier](compatibility-v0.md); conversion reports distinguish
preserved, normalized and omitted features. Initial styles
are text/general/fixed two decimals, bold, italic, alignment, background and boolean text wrapping.
Additional style, frozen-pane and native v3 view tiers have separate bounded
contracts and evidence in the [implementation status](implementation-status.md).

## M1 experiment specification

Compare stdlib SQLite explicit engine mutations with SQL trigger/preupdate
capture. Verify direct writes, stale edits, rollback and backup. Managed SQL
writes remain unavailable until inserts, deletes, upserts, replacements,
triggers, cascades and old/new iteration are all proven for a declared surface.
The first bounded [capture comparison](sqlite-capture-experiment.md) now has
repeatable TEMP-journal evidence and negative controls. The optional
[native preupdate binding comparison](native-capture-binding.md) now exercises
APSW with the same typed-image oracle and bounded failure cases. Production
binding adoption and native session APIs remain separate decisions; SQL hook/history/receipt
integration remains unqualified and all public managed SQL stays unavailable.
Use stdlib localhost HTTP and a spawned Python worker for the first bounded
experiment; compare dependencies and transaction/cancellation costs before
calling either a production service choice. The bounded
[service/Python comparison](service-execution-experiment.md) retains that
development arrangement after real exception/crash/timeout and receipt tests;
it does not qualify a production framework or enable cancellable jobs.
Exercise QTableView tiles,
QNetworkAccessManager ordering and QAbstractItemModelTester. Record selected
toolchain, correctness tests, measurements and remaining blockers in
[implementation status](implementation-status.md).

Cell borders use the native style `border: "box"` or `"none"`. The desktop
Format menu applies or clears all four edges on each selected cell using one
managed edit. Borders use the system text color with a two-logical-pixel stroke.
Individual edges now support solid widths, dashed and dotted styles; see [the border edge contract](editing-formatting-next-ten.md#border-edges-and-line-styles). Color selection and merged-cell borders remain unavailable.

Numeric formatting extends the shared Style schema with Number/Percentage and
0–15 decimal places. It remains an ordinary bounded style edit, preserving typed
inputs, formula source, transaction ownership and history. See
[decimal precision](editing-formatting-next-ten.md#decimal-precision).

Style-only snapshots use state-checked REST `/styles` / SDK `read_styles`: one
dense rectangle, at most 10,000 cells / 4 MiB / two seconds, without formula
calculation, hooks or source/value payloads. Format painter copies exact style
patterns through ordinary edit/replace_style, preserving input types/formulas
and one operation's Undo. See [the painter contract](editing-formatting-next-ten.md#format-painter).
