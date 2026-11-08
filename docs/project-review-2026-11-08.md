# Project review — November 8, 2026

Reviewed commit: `f9ed9d62258e68736e4502ea2d6746d789e14a46` (`main`).

## Assessment

The local preview has a strong foundation: the desktop uses REST, the engine
owns managed transactions, required hooks fail visibly, and independent SQLite
access remains an intentional separate path. The test suite covers substantial
formula, history, interchange, receipt and worker-failure behavior. The milestone
ledger also distinguishes local implementation from release qualification.

The immediate priority is hardening the recently added record and operation-job
workflows. This review found one reproduced native-file overwrite, several
recovery and interaction defects, and gaps in cancellation, API documentation
and compatibility reporting. Fix these before expanding the feature surface.

No production code was changed during this review.

## Verification and scope

Reviewed the project guidance, implementation status, accepted contracts,
build/CI configuration, engine, service, clients, formula/dependency code,
interchange, metadata/records, background jobs and relevant desktop/test paths.
Additional probes used temporary scripts, a temporary Qt harness and disposable
workbooks outside the repository.

| Check | Result |
| --- | --- |
| `cmake --build build --parallel` | Passed |
| `PYTHONPATH=service python3 -m unittest discover -s tests -v` | 435 tests run; passed with 15 optional-binding skips |
| `ctest --test-dir build --output-on-failure` | Both suites passed; desktop 102.30 seconds, service 36.51 seconds |
| Qt suite detail | 182 passed, zero failed or skipped |
| Targeted Qt record-paste probe | Reproduced rejection of a two-row CRLF clipboard with a final newline |
| Targeted Qt record-export probe | Reproduced replacement of the native SQLite file with XLSX bytes |
| Targeted engine job probes | Reproduced stale prepared-job start after reopen and operation-ID collision after release/recreation |
| Generated OpenAPI inspection | Confirmed missing operation-job fields and required properties |

The initial sandboxed Python run could not bind localhost sockets; the successful
runs above used localhost access. The environment was Apple Silicon macOS,
Qt 6.12.0 and Python 3.14.6. Windows/Linux, physical input, screen readers and
clean-machine packaging were not requalified. Passing baseline tests does not
cover the additional failure cases below.

## Prioritized findings

P1 means potential data loss and should be fixed first. P2 means a supported
workflow or contract needs correction. P3 means documentation or maintenance
work that should follow the functional fixes. “Reproduced” identifies an executed
probe; “source-confirmed” identifies a control-flow/schema finding without an
end-to-end reproduction.

| ID | Priority | Finding | Evidence |
| --- | --- | --- | --- |
| R1 | P1 | Record export can overwrite the open native workbook | Reproduced in Qt |
| R2 | P2 | Closing an attached window with an active job does not request cancellation or release its handle | Source-confirmed |
| R3 | P2 | Reusing a released job ID creates an unrecoverable journal | Reproduced in engine |
| R4 | P2 | Prepared-job recovery in the GUI selects a stale start instead of resume | Engine reproduced; GUI routing source-confirmed |
| R5 | P2 | Record paste mishandles ordinary spreadsheet line endings | Reproduced in Qt |
| R6 | P2 | New SQL scan paths omit instruction-level deadline/cancellation checks | Source-confirmed |
| R7 | P2 | Operation-job OpenAPI schemas omit essential inputs | Generated schema inspected |
| R8 | P3 | Current compatibility documents and conversion reports contradict implemented behavior | Source-confirmed |

### R1 — Record export can replace the native workbook

**Location:** [desktop/mainwindow.cpp:1620](../desktop/mainwindow.cpp#L1620).

Record XLSX/ODS export checks `sameFile()` before opening its final conversion
review. After that modal dialog returns, it checks the workbook state and draft,
then writes through `QSaveFile` without checking the destination again. Ordinary
spreadsheet/list export already repeats the destination check after review
([example](../desktop/mainwindow.cpp#L2635)).

**Reproduction:** Create a workbook with one declared record and choose record
XLSX values export to a different, initially absent path. While “Save this
projection snapshot?” is open, create a symbolic link at that export path pointing
to the native workbook. Accept the review. The disposable native file's first
bytes changed from `SQLite format 3` to `PK\x03\x04`: the export overwrote the
workbook, while the interface reported that native IDs were retained.

**Fix:** Revalidate the destination against the captured native path after the
last modal review and immediately before opening the output. Share the protected
export-writing path across record, list and ordinary exports so this safeguard
does not diverge again.

**Regression check:** Change the destination to a native-file alias during final
review for both XLSX and ODS. Export must reject, and the native file must remain
a readable SQLite workbook with unchanged inputs.

### R2 — Active-job close skips attached-service cleanup

**Locations:** [closeEvent](../desktop/mainwindow.cpp#L2463),
[normal attached-handle cleanup](../desktop/mainwindow.cpp#L2485),
[destructor](../desktop/mainwindow.cpp#L248).

The active-job branch tells the user “Closing requests cancellation,” but Yes
only accepts the close event and returns. It sends neither `/cancel` nor `/close`.
It also bypasses the normal attached-service handle-release branch below it.
The destructor only terminates an owned service.

**Impact:** With `--connect`, the independent service retains the handle and can
continue committing remaining batches after the user closes in response to a
cancellation promise. Repeated occurrences also retain service resources. Owned
services are terminated rather than following a confirmed cancellation exchange.

**Fix:** Request cancellation of the captured job and release this client's
handle through an explicit close sequence. Preserve the ID and journal when an
acknowledgement is unavailable; describe the outcome accurately. Keep independent
services running.

**Regression check:** Close an attached window during a multi-batch job. Verify
the cancellation request, retained committed receipts, handle release and the
continued availability of another client's handle. Include a lost-reply case.

### R3 — Released job IDs collide with retained operation receipts

**Locations:** [journal creation](../service/datahorizon/operation_jobs.py#L64),
[batch IDs](../service/datahorizon/operation_jobs.py#L229),
[release](../service/datahorizon/operation_jobs.py#L301).

Release removes the journal but deliberately retains native operation receipts.
Creating a job with the same user-selected ID is then allowed and resets its
attempt counter. Batch IDs are generated solely from the job ID, staged offset
and attempt counter, so the new job reuses IDs from its predecessor.

**Reproduced sequence:** Execute job `retained`, release it, create another job
`retained`, stage a new value, seal and start. The new job enters `unknown` with
`OPERATION_ID_REUSED`. Release then fails with `OUTCOME_UNKNOWN`. Resume cannot
reconcile the new request against the older request's fingerprint. This also
consumes one of the four retained journal slots.

**Fix:** Give each journal incarnation a durable random execution namespace and
include it in batch IDs, preserving those IDs across recovery of that incarnation.
Alternatively, explicitly reject reused IDs before creating a new journal.

**Regression check:** Complete/release/recreate the same friendly job ID without
deleting native receipts. The new job must either execute independently or reject
before staging/admission; it must not strand an unknown journal.

### R4 — Prepared-job recovery in the GUI selects a stale start

**Locations:** [GUI execution routing](../desktop/mainwindow.cpp#L1664),
[start/resume preconditions](../service/datahorizon/operation_jobs.py#L194).

The GUI chooses `/start` whenever the journal status is `prepared`; the execution
button cannot directly choose `/resume` in that state. A prepared journal retains
its original state token, while reopening creates a new epoch. An intervening managed edit
also changes the state without changing the journal's `prepared` status.

**Reproduction:** Stage and seal an edit job, close the workbook, reopen and
inspect the retained journal. It is still `prepared`. A GUI-style start returns
`STALE_STATE`; an explicit engine resume with the same reviewed digest and current
state completes. Inspecting again leaves the GUI choosing `/start` again. Cancelling
the prepared job first can change its status to permit the resume route, but that
is an indirect workaround for the advertised recovery action.

**Fix:** Provide an explicit reviewed resume/rebase action when the journal's
expected state differs from the current workbook. Preserve stale-start refusal;
do not automatically accept a changed target state.

**Regression check:** Exercise the entire dialog flow after reopen and after an
intervening edit, including rejecting the new review.

### R5 — Record paste has a separate, incompatible TSV parser

**Locations:** [record paste](../desktop/mainwindow.cpp#L1626),
[ordinary grid normalization](../desktop/mainwindow.cpp#L1865).

Ordinary paste removes a final newline and strips CR from each CRLF line.
Record paste directly splits on LF and tabs. A common clipboard containing
`10\r\n20\r\n` therefore produces three rows, retains CR characters, and fails
the reviewed two-record dimension check. The Qt probe reproduced
`Clipboard dimensions must match reviewed records/columns` with no commit.
Without the final newline, retained CR characters can also make automatic number
entry become Text.

**Fix:** Share bounded clipboard parsing and newline normalization between grid
and record paste. Preserve intentional empty fields while removing the terminal
record separator consistently.

**Regression check:** Paste LF and CRLF data, with/without a final newline, into
two declared records. Verify dimensions, numeric input kinds, values and Undo.

### R6 — SQL scans can run without observing their advertised deadline

**Locations:** [large-job Find scan](../service/datahorizon/operation_jobs.py#L271),
[record fingerprint scan](../service/datahorizon/records.py#L26),
[existing protected range reader](../service/datahorizon/engine.py#L442).

The large-job Find query checks cancellation/time only after SQLite yields a
matching stored cell. Record fingerprinting similarly checks time inside the
result loop. These scans do not install a SQLite progress handler around the
query. With the `(sheet_id,row,col)` primary key, a row-range query restricted to
a narrow column interval can scan many unrelated columns before yielding—or
yield no rows at all.

**Impact:** Declared output/scope limits do not bound the SQL work. The job's
60-second budget/cancellation, and record-verification deadlines, are not observed
while such a scan runs. Record capture is part of managed writes, so verification
can also delay a writer beyond its intended cooperative budget. This is a missing
check between SQLite instructions, separate from unavoidable blocked OS I/O.

**Fix:** Reuse row-equality/column-range probes where appropriate and install
progress callbacks that observe the shared deadline and cancellation event.
Remove them before rollback or receipt recording, as existing engine paths do.

**Regression check:** Use populated rows with data outside the requested columns
and no matching output. Expire/cancel during SQLite execution and verify prompt
termination, valid receipts where applicable, and subsequent connection reuse.

### R7 — OpenAPI does not describe essential operation-job inputs

**Location:** [OperationJobRequest and OperationJobAction](../service/datahorizon/service.py#L111).

The generated `OperationJobRequest` has no `required` list and omits `needle`,
`field`, `sort`, `filters`, `header`, `encoding`, `delimiter`, `column_types` and
`locale`, although implementations and advertised workflows consume those fields.
`OperationJobAction.cells` also exposes generic objects instead of the available
cell-edit schema, and action-specific required inputs are only described in prose.

**Impact:** Schema-driven clients and AI tools cannot discover or construct key
advertised Find, sorted-list and typed-CSV jobs reliably. Schema validation also
accepts requests that the service immediately rejects for missing required data.

**Fix:** Define action-specific create/append/start/resume/status schemas, reuse
cell/criteria/conversion definitions, declare required fields, and include usable
examples for the supported job modes.

**Regression check:** Validate representative executable job requests against the
published schemas, and check that missing mandatory fields fail schema validation.
Keep endpoint execution checks alongside schema checks.

### R8 — Current compatibility information has drifted

**Locations:** [initial job contract](design/initial-contracts.md#jobs-limits-and-evidence),
[whole-workbook compatibility](design/compatibility-v0.md#whole-workbook-interchange),
[named-view statement](design/compatibility-v0.md#L219),
[ODS selection report](../service/datahorizon/ods.py#L238),
[ODS workbook report](../service/datahorizon/ods.py#L434).

Examples of current contradictions:

- Initial contracts still say long/chunked jobs are unavailable and accepted
  writes have no partial-cancellation tier, despite explicit batched jobs.
- Compatibility says whole-workbook import rejects required handlers, although
  opted-in structural handlers are supported.
- Named-view documentation says definitions are not portable native metadata,
  without explaining the explicit native v3 Save/Load transfer now available.
- ODS conversion reports still say dates/durations become ISO Text and currency
  information is omitted. The implementation now restores native temporal kinds
  and supports the declared currency-style tier. These messages appear in the
  user's conversion review, not only in historical documentation.

**Fix:** Reconcile the current compatibility reference and user-facing conversion
reports with the latest contracts. Mark historical statements as superseded and
link to the replacement rather than leaving contradictory present-tense promises.
Add targeted assertions for material conversion-report claims.

## Broader improvements and remaining gates

These are priorities already supported by the project's direction and milestone
ledger, rather than newly discovered implementation bugs:

1. **Finish lifecycle and failure qualification.** Extend failure injection around
   job admission, final export review, close/restart, sidecar loss, disk-full and
   permission errors. Test shared components through GUI and REST/SDK paths;
   engine-only recovery coverage missed the GUI routing defects above.
2. **Consolidate duplicated desktop policies.** `mainwindow.cpp` is approximately
   2,665 lines, with several complete async flows compressed into single lines.
   Extract shared clipboard parsing, protected export writing and job lifecycle
   control while fixing the related defects. Keep Qt dialogs focused on review
   and presentation, with target/state policy in testable helpers.
3. **Turn measured limits into explicit product guidance.** Keep the existing
   larger-than-memory qualification and documented large-opening failure boundary.
   Add cancellation measurements for scans with little/no output, separate cold
   from warm runs, and distinguish scoped-result bounds from full-workbook snapshot
   disk costs. Do not describe sparse addressing as unlimited capacity.
4. **Complete M7 evidence before a release claim.** Native Windows/Linux interaction,
   keyboard/accessibility and screen-reader work, high-DPI/native-dialog behavior,
   relocated clean-machine installation, runtime distribution and signing still
   need their own evidence. This review's offscreen checks do not close them.
5. **Preserve dependency order for M8.** Managed SQL, relational tooling, workbook
   code authoring/macros/custom functions and remote TLS/mTLS/password service
   access remain deliberate roadmap work. Keep them explicit and unavailable until
   their shared-engine, hook, transaction and compatibility contracts are proven.

## Suggested correction order

Fix R1 first and add permanent XLSX/ODS regression coverage. Then address attached
close/cancellation and job identity/reopen recovery (R2–R4). Share clipboard parsing
for R5, harden scan cancellation for R6, and complete the published schemas and
conversion guidance (R7–R8). Re-run the full suites after those fixes and repeat
the specific negative cases before treating the current feature batch as hardened.
