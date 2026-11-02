# Implementation status

Execution started October 31, 2026 after the explicit instruction to execute the
plan. The design-only hold is superseded for this work. This is a working local
development preview, not completion of the full M0–M8 plan or a release.

## Current result

The Qt Widgets/C++ desktop runs against an authenticated localhost Python REST
service. The shared engine owns SQLite transactions, source typing, formulas,
history, operation receipts and explicitly activated Python handlers. The CLI
and thin Python client use the same REST resources. The GUI has no SQLite access.

Native create/open/read-only, recovery files, automatic storage, first Save and
consistent snapshot copies are implemented. Range operations include paste
(all/values/formats), relative formula translation, bounded Cut/Paste, fill,
numeric series and cell history. Sheet add/rename/duplicate/reorder/delete are
available with documented reference and handler restrictions. Styles preserve
source and types. Explicit list projections support multikey sorting and filters
over the entire declared scope, stable source-coordinate edits, counts and clear.
Scoped find/replace and UTF-8 CSV are implemented within the declared tier.

The Supplies arithmetic is verified: amounts 7, 5, 6; total 18; Pen quantity 6
produces 7.5 and total 20.5. Quantity >2 returns Pen and Folder. Reopen retains
formula source; values export contains 20.5. Formatting renders 20.50.

## Milestone ledger

| Increment | Evidence and scope | Remaining before full milestone closure |
| --- | --- | --- |
| M0 | [Initial contracts](initial-contracts.md), [compatibility](compatibility-v0.md), [native schema](native-format-v1.md), keyboard/state rules and numeric budgets recorded | Resolve deferred cases at their named milestones; no claim that historical PDFs have been regenerated |
| M1 | Qt 6.12/C++17 builds locally; stdlib SQLite/HTTP and spawned brokered workers exercised; sparse/dense/long-text/large-disk measurements recorded | Comparative production framework/capture experiments, formula chains, GUI input/scroll measurements and Windows/Linux performance measurements |
| M2 | Real GUI→REST→SQLite edit, shared CLI/SDK behavior, transactional handler fixture, rejection/timeout, receipts, direct access, stale writes, schema stop, API explorer, client handle lifecycle; restart/reopen and warned unknown-outcome recovery | Broader failure injection, clean-machine packaging evidence and service lifecycle hardening |
| M3 | Typed entry, A1 limits, SUM/IF/arithmetic, absolute/cross-sheet references, fill, cycle/errors, fresh snapshot evaluation and Supplies assertions | Wider coercion/compatibility fixtures, usability/accessibility qualification; dependency-index/background-calculation design |
| M4 | Bulk copy/paste/cut restrictions, series, styles, cell Undo/Redo, bounded sheet operations | Row/column reference transforms, unrestricted cut/rename/duplicate/delete, hook-aware structure events, wrap/borders/freeze/hide, dimensions/view persistence and platform conventions |
| M5 | Explicit full-scope projected lists, stable source row edits, multikey sort/filter, scoped source/display find/replace, text CSV preview and snapshot export | Persistent record identities/list definitions, filtered range semantics, column typing/date/encoding controls, progress/cancellation and large jobs |
| M6 | Native snapshots/recovery, read-only opens, conservative external refresh and bounded CSV | XLSX/ODS import/export fixtures and conversion reports, comprehensive disk/permission/crash recovery, bundled runtime, follow-up execution semantics |
| M7 | Automated local macOS tests and visual screenshot; local install smoke; hosted Windows/macOS/Linux build/test/install smoke passes after deployment-path fix | Native platform interaction, screen readers, high DPI, clean-machine installers, signing, compatibility matrices and distribution/license decisions |
| M8 | Shared engine and REST boundaries retained; unsupported APIs reject explicitly | Relational schema/tooling, proven managed SQL capture, workbook code/macros/functions/packages, remote TLS/mTLS/password access and additional wireframes |

Later-stage facilities in this preview are bounded development slices. They do
not waive the dependencies or completion criteria in the implementation plan.

## Evidence

Build: CMake 4.4.2, Apple Clang 17, Qt 6.12.0, Python 3.14.6, Apple Silicon,
macOS 26.6.2. Sources build with compiler warnings enabled. Tests exercise
actual REST and child processes; no substitute in-process GUI database path is
used. The automated suite includes:

- Atomic multi-cell validation, hook-generated edits, required load/timeout
  failures, rollback, no-op change coalescing, Undo/Redo and source retention.
- Durable operation deduplication/status, external source edits without handler
  replay, stale-write rejection, schema/trigger rejection, read-only access and
  consistent snapshot copies.
- Formula errors/recovery, lazy IF, absolute/cross-sheet references, bounded
  distant reads and long-text/calculation/export limits.
- Range offsets, overlapping moves, rejected Cut destination retaining its
  source, numeric series, sheet duplication/order, source-preserving styles,
  full list scope, projected targets and scoped Replace All.
- REST/SDK/CLI parity with the same required handler, authentication/origin
  rejection, CSV preview, headless file CLI and independent client handles.
- Qt model invariants on a bounded instance; actual Supplies edits, source-bar
  Save/Escape, Undo, formatted results, projected source targets, far-address
  navigation, required-hook rejection and a closable startup failure.

The review regression suite includes 111 Python tests and 38 Qt contract cases.
[Python test output](evidence/service-tests.txt),
[desktop test output](evidence/desktop-tests.txt),
[desktop process resources](evidence/desktop-resources.txt), and
[rendered Supplies window](evidence/desktop-supplies.png) are saved locally.
The [combined CTest result](evidence/verification.txt) and
[installed native-app smoke result](evidence/package-smoke.txt) are also recorded.
The screenshot uses Qt's offscreen platform and does not certify native platform
appearance. Qt warnings about offscreen size hints are expected test output.

[Benchmark JSON](evidence/local-benchmark.json) records a 390,001-cell dataset:
100k sparse cells, 10k dense cells and 280k one-KiB text cells. Its SQLite disk
size exceeds the declared 256 MiB service memory target. Warm/distant/dense/disk
viewports and edit acknowledgement use 100 authenticated REST requests each.
The report includes driver/service peak RSS and explicit missing measurements.
These results do not promise an unlimited workbook or every dense address-space
shape. The cache has both tile-count and estimated-byte limits; memory targets
apply to the measured workload, not all possible active workbook combinations.

`cmake --install build --prefix build/package` deploys Qt and service sources.
`python3 tests/package_smoke.py build/package` exercises a real installed native
application, managed edit and calculation using an ephemeral workbook. Python
remains separately installed. This is an install smoke, not clean-machine or
distribution qualification. CI describes Windows/macOS/Linux build/test/install
jobs. The first hosted green matrix is recorded in
[PR #1](https://github.com/hybridmachine/DataHorizon/pull/1), following commit
08d1d81's absolute Qt deployment prefix correction.
The installed smoke uses the bundled native platform plugin (Cocoa here);
Linux CI provides a display with Xvfb. Unit/interaction tests use offscreen Qt.

## Next dependencies

Finish the remaining M1 measurements and extend platform acceptance. Resolve the
M4 structural reference/identity contracts with destructive-operation fixtures
before enabling those controls. Then finish list/CSV typing and explicit jobs,
followed by independent XLSX/ODS fidelity stages and failure recovery. Full
desktop acceptance and licensing/runtime distribution decisions precede a
public preview. M8 remains a later set of independently designed increments.

Managed SQL support matrix for this tier: SELECT/DDL/DML/PRAGMA/ATTACH/transaction
control through an application SQL resource are all unavailable. No statement
is silently run outside hooks. Ordinary external SQLite cell changes are tested
and intentionally supported; reserved schema/metadata changes are rejected on
managed refresh. Proving capture for triggers/cascades/upsert/replace is still
required before the first managed SQL mutation can be exposed.

## Review corrections — November 1, 2026

All 16 initial review findings have targeted fixes and regression coverage:
CSV previews capture and validate their original target; service crashes permit
restart/reconciliation or warned exit with a durable client record; editors
preserve long sources and drafts; Windows recovery paths normalize separators;
Cut rejects special paste modes; malformed/truncated receipts retain operation
IDs and block further writes; CLI apply accepts the JSON sheet; receipt paths
encode/decode opaque operation IDs; style-only changes preserve blank inputs
and coalesce events by input; clipboard blanks and formula errors retain their
semantics; CSV numbers round-trip; and blank comparisons remain cell-local.
Late mutation/lookup callbacks are associated with their captured operation ID,
and metadata refreshes cannot overwrite a reopened workbook's handle.

The next 11 review findings also have regression coverage: display replacement
preserves Text and rejects implicit numeric/formula conversion; the initial
command-line workbook opens once; restart preserves drafts at their original
coordinates for explicit review; empty lists have no editor target; Copy/Cut
supports 10,000 cells through a consistent clipboard resource; bounded viewport
previews and state-checked full-input reads keep long cells editable; invalid
CLI addresses produce JSON errors; sheet deletion checks the cell budget before
cascading; extreme references and range comparisons remain cell-local errors;
and the SDK parity test now verifies an actual HOOK_FAILED rollback after refresh.

Additional review cycle 1 addressed seven substantive findings. CSV export
rejects the current native workbook and its aliases; direct SQL honors explicit
read-only mode and returns structured storage errors; receipt deduplication is
rechecked under the writer lock without misclassifying an existing outcome;
new and duplicated sheets append after position gaps; list/search/export reads
refresh external state within their read transactions; CSV limits include the
serialized response and blank-record count. Regressions inject commits at the
receipt-lookup/writer-lock and initial-refresh/read-transaction boundaries.

Additional review cycle 2 published no inline issues, but its summary named
three unpublished concerns. All three were reproduced and fixed: GUI edits and
recovered drafts retain explicit Text typing; range/clipboard byte limits count
the complete JSON envelope and array separators; non-finite numeric literals
become #NUM! before scalar operations can consume them, while lazy IF retains
its selected-branch semantics. Boundary and editor regressions cover these cases.

Additional review cycle 3 also published no inline issues, but named omitted
concerns about overflow, pasted values and receipt consistency. Reproductions
confirmed three defects and targeted fixes: SUM rejects overflowing intermediate
totals before callers can hide them; full paste preserves stored input kinds
independently of Text formatting; failure receipts commit under the original
writer lock after savepoint rollback, preventing another service from claiming
the ID in between. Receipt-storage failure injection verifies an unknown outcome
and transaction cleanup. The first three-cycle limit was reached here; the user
subsequently authorized two more cycles with the same cosmetic-only stopping rule.

Additional review cycle 4 published no inline issues, but named moderate
concerns about asynchronous state, edit recovery and transport errors. Targeted
fixes discard callbacks from replaced transport sessions, retain rejected and
interrupted in-cell drafts for correction/review at their original coordinates,
preserve reviewed state/sheet preconditions through submission, and report HTTP
failures even when their JSON message is empty. Missing receipts return an
unknown outcome with the operation ID; failed SDK lookups retain the pending
operation. Delayed socket replies, HTTP failures, restart/rejection recovery and
stale reviewed-operation regressions exercise these paths.
The CI rerun encountered GitHub's macOS 14 retirement brownout before checkout;
the matrix now uses `macos-15`, following the
[runner retirement notice](https://github.com/actions/runner-images/issues/13518).
That image exposed the upstream
[slow reverse-DNS lookup](https://github.com/actions/setup-python/issues/1223)
in Python HTTPServer startup. The loopback service now binds numerically and
sets its fixed local server name without DNS; a regression denies hostname lookup
during construction. Startup and test timeouts are unchanged.

Additional review cycle 5 named a confirmation-target race despite posting no
inline issues. Desktop operations now capture workbook, sheet and state before
their dialogs and retain those preconditions through editor resolution and
submission. Sheet management, numeric fill, formatting, CSV export, list review
and recovered-draft confirmation use the captured context. A real two-service
regression removes the original sheet while its deletion dialog is open,
refreshes the desktop to another sheet, confirms the old dialog, and verifies
that no new deletion is submitted. This completes the two newly authorized
cycles; final fixes are validated without requesting another review.

The user authorized another two review/fix cycles after cycle 5. Cycle 6
returned four substantive findings. External and native clipboard payloads are
now bounded before matrix construction; the REST client rejects oversized
serialized requests locally with a known, asynchronous rejection. Direct SQL
encodes BLOB results as typed base64 JSON. Undo/Redo resolves targets from
recorded history without requiring a sheet named Sheet1. Formula tokenization
distinguishes digit-bearing function names from cell references during
evaluation and copying. Regressions cover each behavior.

Cycle 6 also includes a recovery regression for a draft whose original sheet was removed
while the service was stopped. Its input remains visible and modified after
reopening; submission cannot use the fallback sheet, and later edits require an
explicit discard. The draft stays available for copying until that choice.

Cycle 7 posted no new inline issues, but its summary raised a critical CSV
import-target race and editing/resource concerns. A two-service regression
reproduced a CSV import into another sheet after the original sheet was removed
while the file picker was open. Imports now capture workbook, sheet, state and
coordinates before draft resolution and file selection, validate them after
each modal step, and pass them explicitly through preview and submission.
An active in-cell draft is retained for Save/Discard/Cancel before import starts;
Cancel preserves its input without submitting an import. CSV file reads are
bounded during reading as well as by the initial size check. The reproduced
race now cancels with no mutation. This completes the two further authorized
cycles; final fixes are validated without requesting an eighth review.

The user then authorized two further cycles. Cycle 8 published no inline issues
but named omitted findings about reference detection, Enter navigation and
boolean validation. Confirmed fixes distinguish literal exclamation marks from
sheet references while retaining restrictions on real quoted references;
reject non-boolean open and clear/style flags without creating files or
partially editing cells; and move Enter/Shift+Enter selection only after a
confirmed commit. Save keeps its cell, rejection retains its target and draft,
and sorted-list navigation follows the originally intended next source row.
An independent regression also reproduced closing over an active in-cell draft
without a discard prompt. Open and Close now capture those drafts and preserve
them visibly when discard is declined. The regression suite covers both paths.

Cycle 9 posted no new inline issues but identified a critical retry/admission
race. A real two-service regression pauses the original transaction before
commit, makes an identical retry time out on the writer lock, and verifies that
the SDK stays pending until the original commit is reconciled. Unconfirmed
admission failures now return the operation ID with an unknown outcome and
release any acquired transaction. Desktop and SDK mutation errors need a
matching terminal operation ID before unlocking writes; a generic rejection is
insufficient. Local SDK read-only and size checks fail before transmission
without creating a pending write. Failure injection covers admission cleanup;
a Qt socket fixture checks generic-error recovery records and blocked editing.
This completes the two further authorized cycles. Final fixes are validated
without requesting another review.

The user authorized two further cycles. Cycle 10 published no inline issues but
identified validation paths that rolled back without durable failure receipts,
leaving the SDK pending indefinitely. Malformed styles, ordering, clipboard
sources/values/origins, series steps, labels, sheet identifiers and Unicode now
return explicit validation failures. Unexpected failures after admission also
record a durable rollback receipt after savepoint rollback; replay does not
execute the request again. Invalid operation IDs fail SDK preflight. Long
integer sources retain the exact numeric boundary without Python's integer-text
conversion limit. An independent Qt regression found that range actions could
commit before resolving an active cell draft. Common submission now captures
that draft first: Cancel submits nothing and leaves the input editable. The
long-input regression now exercises actual editor commit signals.

Cycle 11 posted no inline issues but raised service-launch security, formula
correctness and resource-lifecycle concerns. Marker-file regressions reproduced
owned services importing code from the working directory and ambient
PYTHONPATH before any handler activation. Desktop and SDK launch by absolute
trusted path in Python isolated mode; hostile-directory tests also verify
explicit required hooks still execute normally. Failed opens now publish no
unreturned handle and close a new engine if metadata loading fails; existing
leases remain valid. Runtime formula recursion is #LIMIT!, with unrelated cells
still calculated in the same viewport. Installed native smoke and the complete
suite verify the final behavior. This completes the two further authorized
cycles; final fixes are validated without requesting another review.

The user authorized two further cycles. Cycle 12 published no inline issues but
identified formula error literals being mistaken for cross-sheet references. A
copy/paste regression reproduced #REF! unexpectedly blocking sheet rename,
duplication and deletion. Reference detection now skips known formula error
literals while retaining restrictions for real sheet separators and unclassified
exclamation marks. The regression verifies all three operations preserve the
broken formula and its error. Additional cases cover errors inside expressions,
quoted names and actual sheet references adjacent to error literals.

Cycle 13 published no inline issues but raised proxy-mediated exposure of local
credentials and workbook contents. A capture-server regression confirmed the
SDK sent its bearer token through an ambient HTTP proxy. The SDK and managed
CLI now use a private proxy-disabled opener; Qt also explicitly disables proxies.
Redirect regressions reproduced SDK credential forwarding and Qt forwarding of
a redirected POST. Both transports now reject redirects, discard redirect bodies
as receipts and preserve pending mutation IDs. Tests cover SDK/CLI edits with
ambient proxies, HTTP redirect variants and a Qt redirect carrying a forged
rollback receipt. This completes the two further authorized cycles; final fixes
are validated without requesting another review.

The user authorized two further cycles. Cycle 14 published no inline issues but
identified unhandled HTTP failures, premature SQL output and incomplete API
operation schemas. A truncated real HTTP reply now produces a structured CLI
error. Direct SQL serializes within its transaction and prints rows only after
successful commit; a held SQLite read lock reproduces commit failure without
false success output, and serialization failure still rolls back. OpenAPI now
defines every action's arguments and required fields, typed POST bodies, bounds
and examples; the explorer selects action examples. Integration tests execute
all advertised operation examples with current IDs/state, and the specification
is checked with an OpenAPI validator.

Cycle 15 published one high-severity issue: WITH-prefixed direct SQL could commit
before serialization failed because Python did not implicitly open a transaction.
The CLI now explicitly owns the transaction through serialization and commit.
A SQLite authorizer rejects user transaction controls, savepoints, PRAGMA and
attachment commands; commands such as VACUUM that require autocommit fail in the
owned transaction. An external SQLite tool remains available for administration.
Regressions verify CTE failure rollback, successful CTE and DDL writes, rejected
transaction/administrative commands and the absence of created attachment or
VACUUM files. This completes the two further authorized cycles; final fixes are
validated without requesting another review.

The user authorized up to four further cycles, stopping early for cosmetic-only
feedback. Cycle 16 published no inline issues but raised post-commit cleanup
failures being reported as rejected mutations without operation IDs. A real HTTP
regression reproduced a committed CLI receipt followed by a contradictory error
from handle release. The CLI now preserves the completed result and successful
exit status while reporting cleanup separately with the mutation ID and outcome.
Regressions cover HTTP, malformed-JSON and truncated cleanup failures, plus lost
mutation acknowledgements that must retain an unknown outcome and nonzero exit.

Cycle 17 published no inline issues but identified SDK context cleanup masking
primary outcomes and shared registry locking stalling unrelated workbooks. SDK
context exits now preserve the primary exception and attach cleanup details;
cleanup-only failures use a separate error with the last confirmed receipt.
The service uses per-path access gates and brief registry updates, with reserved
engine/handle capacity during slow opens. Slow metadata, construction and close
regressions fail against the prior code and pass with independent reads kept
available. Parallel opens respect the engine cap and concurrent opens of one
file share one engine; releasing one handle preserves the other client.

Cycle 18 published no inline issues but identified metadata refresh after receipt
confirmation replacing a committed result with a generic rejection and clearing
its pending ID. The SDK now preserves the receipt in MetadataRefreshError and
blocks further writes until valid metadata refreshes. Retrying reconciliation
uses the already confirmed receipt; closing a handle after a known outcome is
safe. Regressions cover committed and rolled-back outcomes, HTTP and malformed
metadata failures, guarded writes, successful recovery and context cleanup.

Cycle 19 published no inline issues but identified a critical output failure
that could relabel a committed CLI write as rejected without its ID. Output is
now flushed within an explicit reporting phase; failures preserve the confirmed
receipt and ID on stderr and suppress a second interpreter-shutdown flush.
Broken-pipe regressions cover buffered/unbuffered output, simultaneous cleanup
failure and direct SQL commits. The review log also pointed to receipt lookup
bookkeeping and boolean arithmetic: queried receipts now remain available in SDK
cleanup diagnostics, and unary signs follow the documented numeric 0/1 coercion
instead of returning a boolean that SUM would ignore. Both have regressions.
This completes all four newly authorized cycles; final fixes are validated
without requesting another external review.

The user authorized up to four further cycles with the same cosmetic-only stop.
Cycle 20 published no inline issues, but identified a schema-validation bypass,
failed-create deletion, revision overflow, invalid list criteria and list focus.
The schema signature now excludes only the literal sqlite_ prefix, so user
triggers named sqliteX... cannot evade validation and bypass tracked writes.
Failed creation preserves the destination, and a changed file identity is checked
before schema initialization. Revision exhaustion rolls back with a durable
failure instead of storing a floating-point revision. List criteria reject
non-object members, and Save retains source row/column through a sorted-list
refresh. An independent SDK regression also fixes confirmed-close idempotence
and prevents requests or pending IDs after a client has closed.

Cycle 21 published no inline issues but found the same destructive cleanup
pattern in snapshot failures. Failed copies now preserve their destination.
Creation/copy share exclusive reservation and filesystem identity checks; copies
check the destination before and after backup and use existing-file mode when
opening it. Missing identities reject the operation before target initialization.
Creation also rechecks identity after initialization before returning its engine.
Regressions replace paths before backup, after backup and during a failed backup,
and verify foreign contents survive and a changed copy is not reported committed.

Cycle 22 published no inline issues but identified omitted history costs, an
unbounded duplication preflight under the writer lock, and failed-open handle
publication. History budgets now include serialized labels through Undo/Redo;
this is explicitly a payload budget rather than a Python RSS guarantee. Sheet
duplication stops counting after 10,001 cells before rejecting oversized sheets.
Open responses are assembled and checked for JSON serialization before handles
are published; malformed native metadata releases fresh engines and reservations
without consuming handles or closing an existing client engine. All three
regressions fail on the previous head and pass with these fixes, including a
100,000-cell fixture checked against a bounded SQLite instruction budget.

Cycle 23 published no inline issues but identified uninterruptible formula
preflight scans and native validation, copy, projected-read and numeric-input
risks. SQLite progress handlers now enforce formula-scan deadlines even when
queries return no rows; handlers are removed before rollback and receipt storage.
Validation checks workbook/sheet UUIDs, names, display positions and foreign-key
relationships, including external changes to an active workbook identity.
Snapshots use bounded backup steps with a busy-retry deadline and validate the
actual copied schema/catalog/relationships before acknowledging success. Failed
destinations remain available for inspection. Explicit projected reads default
to their requested starting column, like rectangular reads. Number entry uses
the invariant decimal grammar and applies integer precision checks to zero
decimal suffixes and zero exponents as well as plain integers; other floating
notation retains documented binary64 rounding. Regressions reproduce the old
failures and verify durable timeout rollback, real SQLite backup contention,
invalid external catalogs, incompatible copies and source-preserving rejection.
All four newly authorized cycles are complete. Final fixes are validated and
pushed without requesting a fifth external review.
