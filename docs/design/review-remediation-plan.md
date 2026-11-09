# Project review remediation plan

November 8, 2026. Status: all nine local remediation increments verified;
the follow-on platform release gate remains pending.

This plan addresses R1–R8 in the [project review](../project-review-2026-11-08.md),
whose baseline is commit `f9ed9d62258e68736e4502ea2d6746d789e14a46`.
It extends the existing [implementation plan](implementation-plan.md) and
[milestone ledger](implementation-status.md); their completion criteria remain
in force. This document records the corrective scope and its completion evidence.

## Outcome and boundaries

Deliver a hardened local preview in which exports preserve the native workbook,
retained jobs recover without operation-ID collisions or replay, cancellation and
close report known outcomes accurately, and the published API and compatibility
information describe the implemented behavior.

Preserve these contracts throughout:

- Qt Widgets/C++ remains the desktop stack. Managed GUI, CLI and SDK access uses
  REST and the shared engine, including applicable transactional Python hooks.
- Independent SQLite edits remain supported without Python callbacks. Refresh
  invalidates derived state without replaying historical change handlers.
- Atomic jobs retain one transaction; explicit batches retain separate durable
  receipts. Cancellation cannot undo committed batches. A missing receipt remains
  unknown, and unknown writes are never automatically replayed.
- Workbook identity, sheet/record identity, reviewed coordinates, input state,
  formula source, typed values and display remain distinct.
- Existing request, cell, history, time and memory budgets remain in place.
  Sidecar migration must not require a native workbook schema change.

The approach below is implemented and verified locally. Broad GUI redesign,
new spreadsheet functions, managed SQL, relational tooling, workbook code authoring
and remote service access remain outside this corrective batch. Platform release
qualification follows as a separate gate after the local fixes.

## Delivery sequence

Use one focused, reviewable change per increment, including its regression tests
and relevant contract updates. Follow the numbered sequence for execution; the
dependency column identifies the technical prerequisites. Avoid broad refactors
before the data-loss regression is fixed.

| Step | Deliverable | Review coverage | Required predecessors |
| --- | --- | --- | --- |
| 1 | Protect every export destination after final review | R1 | None |
| 2 | Give each durable job incarnation independent batch IDs | R3 | None |
| 3 | Enforce SQL scan deadlines and cooperative cancellation | R6 | None |
| 4 | Provide explicit reviewed resume after reopen/state changes | R4 | 2 |
| 5 | Complete asynchronous job cancellation and window close | R2 | 2, 3, 4 |
| 6 | Share bounded clipboard parsing for grid and records | R5 | None |
| 7 | Publish complete operation-job OpenAPI contracts | R7 | 2, 4, 5 |
| 8 | Correct conversion reports and current compatibility documents | R8 | 1–7 |
| 9 | Qualify the combined candidate and publish completion evidence | All findings; broader review improvements | 1–8 |

The ledger at the end records each verified increment. Local completion does not
close the separate platform/manual release gates or broader milestone criteria.

## 1. Protect native workbooks during export

**Primary files:** `desktop/mainwindow.cpp`, a small shared export-writing helper
if needed, `tests/test_desktop.cpp` and the applicable interchange contract.

First add the missing destination check to declared-record XLSX/ODS export after
the final modal review and immediately before opening `QSaveFile`. Capture the
native workbook path and reviewed target before asynchronous work begins; never
substitute a later current workbook or sheet.

Then route ordinary, list and record spreadsheet exports through the same bounded
binary-output helper. Keep conversion review in the caller, and keep CSV companion
handling intact. The helper must reject a destination identifying the captured
native file, validate the response bytes, check write/commit results and return an
actionable error. It must not turn a failed write into a successful export status.

**Acceptance and verification:**

- The reproduced final-review symlink substitution rejects for XLSX and ODS.
  The native file retains its SQLite signature, identity and original stored data.
- Direct native paths and pre-existing aliases reject across ordinary, list and
  record export. Exercise hard links where the test platform supports them.
- Successful exports remain readable by the existing independent consumers.
  Rejected/failed exports leave native inputs, history and receipts unchanged.
- Test a changed workbook context, failed output open, short write and failed
  commit. Report destination failure without claiming the native document failed.

This closes the modal-dialog race found in R1. Keep filesystem checks adjacent to
the actual write and document any remaining check/open race accurately; do not
claim general protection against arbitrary concurrent filesystem replacement.

## 2. Make job execution identities durable and unique

**Primary files:** `service/datahorizon/operation_jobs.py`,
`tests/test_operation_jobs.py`, SDK/CLI tests and the sidecar contract in
[M3–M5 increments](m3-m5-next-ten.md).

Select a versioned sidecar format with a randomly generated `execution_id` stored
when a journal is created. The friendly `job_id` remains the lookup/display name.
Derive future batch operation IDs from `execution_id`, staged offset and attempt;
keep them within the engine's 100-character limit. Persist each exact request
before native admission and retain its ID for receipt reconciliation.

Support existing version-1 journals explicitly:

- Preserve recorded attempt IDs, serialized requests, fingerprints, receipts,
  committed progress and the sealed plan. Never rename admitted attempts.
- Reconcile existing pending attempts before any new execution. Upgrade eligible
  journals transactionally and assign a namespace for future attempts only.
- Provide an explicit recovery path for legacy ID collisions. Under the engine's
  transaction/identity protections, distinguish a proven fingerprint conflict with
  an older receipt from a missing receipt. Retain the conflicting request and
  original receipt as evidence; do not fabricate a rollback receipt for a request
  that was refused before admission. Any subsequent execution needs a new review
  and fresh namespaced IDs. If non-admission cannot be proven, remain unknown.
- Reject unsupported sidecar versions without altering either file. Document
  downgrade limitations; older clients must not silently interpret new journals.

**Acceptance and verification:**

- Complete, release, recreate and execute the same friendly ID with different
  content. Both generations retain distinct native receipts, and both journals
  can be released normally.
- Crash after native commit but before sidecar acknowledgement. Reopen/resume
  recovers that receipt without executing the committed batch or its hooks again.
- Cover unstarted, partial, completed, collided and genuinely unknown legacy
  journals, including an interrupted migration. Missing receipts still block
  resume and release.
- Preserve file-identity binding, four-journal capacity and native receipt
  retention. Reuse must not consume a permanently stranded slot.

## 3. Interrupt SQL scans within the existing budgets

**Primary files:** `service/datahorizon/engine.py`, `records.py`,
`operation_jobs.py`, `tests/test_operation_jobs.py`, `tests/test_record_identity.py`
and related engine tests.

Reuse bounded row-equality/column-range probes for narrow scopes, including
fingerprint calculation and large-job Find. Carry one deadline across all probes;
do not reset the budget per row or per helper. Preserve row-major ordering and
the existing snapshot transaction.

Extend or reuse the existing SQLite progress-handler mechanism to observe both
deadline expiry and a supplied cancellation event while SQLite executes. Give
each connection one clear owner of the handler; avoid nested helpers accidentally
clearing an outer handler. Remove the handler before rollback and durable receipt
recording. Translate interruptions into the established `TIMEOUT`/`CANCELLED`
outcomes rather than exposing an ambiguous generic SQLite error.

Record verification inside a managed operation uses the remaining parent budget
and cancellation event. Standalone verification and read jobs retain their own
declared budgets. Preserve cooperative semantics for blocked OS I/O.

**Acceptance and verification:**

- Populate many unrelated columns while requesting an empty/narrow interval.
  Deadline/cancellation must be observed even when no result row is yielded.
- Use deterministic clock/progress injection to prove interruption during SQL,
  plus a real adversarial dataset to verify that unrelated columns are not scanned
  unnecessarily. Avoid tests whose correctness depends on a machine being slow.
- Cancel before admission, during fingerprinting, during a read scan and before
  commit. Verify atomic rollback where admitted, preserved earlier batch commits,
  unchanged failed-operation history/cache state and subsequent connection reuse.
- Measure cancellation latency against the existing reference-host target and
  retain actual results. Do not relax operation deadlines to pass qualification.

## 4. Make prepared-job resume an explicit GUI action

**Primary files:** `desktop/mainwindow.cpp`, a focused job controller/helper if
needed, `tests/test_desktop.cpp`, and service/client tests where status changes.

Replace routing based solely on `status == prepared` with an explicit distinction
between starting an unchanged plan and resuming against newly reviewed inputs.
Use the journal's recorded state and a fresh workbook state. Show the retained
plan, original/current target state, committed batches and remaining work.

Capture workbook identity, service session, job execution identity, digest and
current state for the review. Recheck that context after the dialog. Submit
`/resume` only after the user accepts the updated review; another state change
requires another review. Keep `/start` stale-state refusal intact. A transport
failure leads to inspection/reconciliation, never an automatic execution retry.

**Acceptance and verification:**

- Stage/seal, close, reopen, inspect and resume entirely through the GUI.
- Repeat after an intervening managed edit and after external SQLite refresh.
  Confirm that the new review binds the current state without replaying skipped
  historical hooks or retargeting the stored plan.
- Rejecting review admits no operation. Changing inputs during review refuses
  execution. Read-only edit jobs and unknown pending writes remain blocked.
- Partial jobs preserve their committed batch IDs and progress. Missing receipts
  cannot be bypassed by the resume control.
- Verify equivalent explicit behavior through REST, SDK and managed CLI.

## 5. Close active-job windows through a known lifecycle

**Primary files:** `desktop/mainwindow.cpp/.h`, a small lifecycle controller if
needed, recovery-record helpers, `tests/test_desktop.cpp` and service lifecycle
tests.

Implement an asynchronous close sequence with explicit phases: review close,
request cancellation, inspect until stopped, release this client's handle, then
close the window. Capture the service session, workbook and job before starting.
Keep editing and conflicting file actions disabled during this sequence; ignore
duplicate close requests and obsolete replies. Keep the GUI event loop responsive.

Cancellation acknowledgement alone may still report `running`; do not describe
the job as stopped until its terminal status is known. Preserve every committed
receipt. Release the attached handle without terminating an independent service
or invalidating another client's handle. For owned services, graceful cancellation
and handle cleanup precede stopping the owned child.

If status/cleanup cannot be confirmed within the bounded request flow, retain a
private recovery record containing the native path, job ID/execution identity
when available, and last known progress. Offer the established explicit choice
to stay open or close with an unresolved outcome. Closing in that state must not
claim cancellation or rollback, remove the sidecar, or retry writes.

**Acceptance and verification:**

- Cover owned/attached services, a second live handle, repeated close requests,
  user cancellation of the close review, and a job already completed.
- Close during staging, a read scan, an admitted atomic edit and a partial batch
  run. Assert cancellation precedes handle release and no new batch starts after
  cancellation is confirmed.
- Race close/cancel with the last commit: committed remains committed.
- Inject lost cancellation/status/close replies and service interruption. Verify
  accurate UI wording, retained recovery information and receipt reconciliation
  on reopen without hook replay.

## 6. Unify clipboard decoding for record and grid paste

**Primary files:** `desktop/mainwindow.cpp`, a small clipboard parser/helper,
`tests/test_desktop.cpp` and applicable record-operation tests.

Extract the existing bounded grid TSV normalization and reuse it for record
paste. Normalize CRLF, remove one terminal record separator consistently, preserve
intentional empty rows/fields, and enforce byte/cell limits before building a
large matrix. Do not trim cell content or infer additional conversion rules.

Share native clipboard validation as well: parse errors, dimensions, omitted
sources/values, Cut restrictions, source coordinates and input kinds must retain
their existing semantics. Keep record target IDs and columns fixed to the reviewed
operation. Values/formats modes must not silently retype or discard formula errors.

**Acceptance and verification:**

- Paste LF/CRLF with and without a final newline into grid and declared records.
  The R5 two-row example succeeds with two numeric values and no retained CR.
- Cover trailing empty fields, intentional blank rows, empty Text, native typed
  cells, formulas and per-cell origin coordinates.
- Oversized/malformed data fails before mutation. A rejected handler rolls back
  the entire paste; Undo restores all affected inputs/styles in one operation.
- Filtered-out records remain unchanged. Preserve clipboard-generation guards
  so an old asynchronous Copy cannot replace newer clipboard content.

## 7. Publish executable operation-job API schemas

**Primary files:** `service/datahorizon/service.py`, API explorer examples,
`tests/test_service.py`, `tests/test_operation_jobs.py` and SDK/CLI examples.

Replace the generic job request shapes with action-specific OpenAPI 3.0 schemas
for edit/CSV/Find/list creation and append/seal/status/start/resume/cancel/release.
Declare required fields, enums, scalar/criteria types and meaningful bounds.
Reuse cell-edit, sort/filter, encoding, locale and conversion definitions.

Include the currently missing `needle`, `field`, `sort`, `filters`, `header`,
`encoding`, `delimiter`, `column_types`, `locale` and applicable destination
coordinates. Represent the updated status/recovery fields and distinguish job
state from native operation outcome. Keep unsupported combinations explicit and
preserve additional-field compatibility deliberately rather than tightening
unrelated endpoints incidentally.

Provide examples with a discoverable sequence: create, append or prepare, seal,
inspect, explicitly execute, page/cancel/reconcile/resume and release. Examples
use reviewed state/digest/IDs from preceding responses rather than suggesting that
placeholder tokens are usable.

**Acceptance and verification:**

- Validate the OpenAPI document and representative requests with an OpenAPI 3.0
  compatible validator in the test environment; add no runtime package dependency.
- Execute the documented examples through REST and exercise their SDK/CLI paths.
- Missing mandatory fields and malformed scalar types fail schema validation
  and service admission consistently. Test combined bounds/state rules at runtime
  where schema alone cannot express them.
- A recovery example must prove that lost execution acknowledgement is followed
  by inspection, not automatic start/resume retry.

## 8. Reconcile current contracts and conversion reviews

**Primary files:** `docs/design/initial-contracts.md`, `compatibility-v0.md`,
`native-format-v1.md`, `m3-m5-next-ten.md`, `development.md`,
`implementation-status.md`, and `service/datahorizon/ods.py`.

Update current references as each earlier step lands, then perform one consolidated
audit. Explain ordinary atomic operations separately from explicitly batched jobs;
document native receipts, sidecar version/migration and close/recovery behavior.
Correct structural-handler opt-in and the explicit native v3 Save/Load transfer
of otherwise local view definitions.

Correct ODS selection and whole-workbook conversion messages to describe native
date/time/datetime restoration, supported currency/style mapping and actual
normalizations/omissions. Keep unsupported durations, offsets and features explicit.
Leave the dated project review as evidence of the original findings; record closure
here instead of rewriting the review as if the defects never existed.

**Acceptance and verification:**

- For representative ODS fixtures, report assertions agree with the imported
  kinds, values, styles and blocked features. Review both selection and workbook
  messages through the desktop flow.
- Current documentation contains no contradictory availability claim for the
  scoped capabilities addressed by R8. Historical contracts link to superseding
  sections where their original wording is retained.
- Documentation and capabilities distinguish local hardening from platform release
  acceptance and leave the managed/direct boundary intact.

## 9. Qualify the combined candidate

Freeze a candidate commit and record its source fingerprint, toolchain, commands,
results and omissions under `docs/design/evidence/`. Build and run the relevant
focused regressions during each implementation step. Once the combined candidate
passes the full suites, broaden testing only for unresolved risks or newly changed
behavior.

Use the existing build configuration for local checks:

```sh
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

CTest already runs the Python and Qt suites; a separate full Python run is useful
for diagnosis, not an additional mandatory duplicate run. Record optional-binding
skips explicitly and use the existing isolated capture environment when claiming
that experiment's coverage. Tests requiring REST need localhost socket access.

Extend the existing native-acceptance and installed-app checks for the repaired
export, record clipboard and job recovery/close flows. Use the existing
`tests/qualify_preview.py`, `tests/package_smoke.py`,
`tests/installed_m3_m5.py` and `tests/qualify_m3_m5.py` entry points as appropriate;
read their current arguments and output requirements before running them. Do not
overwrite historical evidence or treat offscreen runs as native interaction.

**Required combined evidence:**

- All eight findings have permanent verification or a documented, evidence-based
  reclassification. Capture the original R1 and R5 GUI negative cases in the
  repository test suite rather than relying on the review's temporary harness.
- Reproduce job commit/sidecar-acknowledgement gaps and unknown-outcome recovery
  through clients. Verify that required hooks run once per committed batch.
- Inject disk-full/permission/output failures at the changed export and sidecar
  persistence boundaries. Preserve native data, admitted IDs and truthful outcomes.
- Recheck supported larger-than-memory shapes, global sort paging, dependency
  invalidation and cancellation latency. Include narrow/no-output SQL scopes and
  native snapshot/staging disk costs. Separate fresh-process from warm-cache
  timings and retain the documented large-file opening failure boundary.
- Run native-plugin checks at scales 1/2, accessible-control inspection and
  installed relocation/smoke for the available platform. Record any remaining
  physical-input, screen-reader, native-dialog and clean-machine gaps explicitly.

Complete this batch only after the repaired local candidate meets those checks
and the milestone ledger is updated. Production hardening and cross-platform
release readiness remain separate assessments.

## Follow-on platform gate

After local remediation, execute the existing Windows/macOS/Linux qualification
matrix at the same candidate where practical. Cover native keyboard/clipboard,
dialogs, accessibility/screen readers, high DPI, installation on clean systems,
runtime distribution, signing and applicable license/distribution decisions.
Record unavailable hosted/manual evidence as pending. Historical platform results
do not qualify the changed lifecycle and filesystem behavior automatically.

Use the resulting evidence to update M1/M2/M6/M7 closure decisions before wider
M8 expansion. Keep this follow-on work visible without delaying the immediate R1
correction or claiming that the corrective batch alone produces a release.

## Final local candidate

The frozen implementation is commit
`574937bfdfb4092352592db71f0665cecfeed00b` on `codex/review-remediation`.
Its source fingerprint is
`94b3504779af9fd1508b5eff4f6038cac230c7da6d6c2b9890e271afedf0cf0d`.
The [verification record](evidence/review-remediation-verification.json) binds
that source to the complete [qualification report](evidence/review-final-qualification.json).
The archive inventory retains its original working-tree provenance; its code bytes
match the subsequently frozen implementation commit.

Full CTest passes both suites: 205 Qt checks and 451 Python tests run, with 19
optional skips. The optional four OpenAPI validator tests and fifteen native
capture tests pass separately in their isolated environments. The repaired flows
pass 23 Cocoa checks at each scale, using Qt widget dialogs. A native NSAlert
automation stall was isolated; native OS dialog/physical-input acceptance remains
a manual gate. Native acceptance screenshots at both scales were inspected.

Independent consumers accept workbook, temporal, formatting and filtered-record
exports. Relocated installed-app/SDK/service smoke and the 86-file archive inventory
pass. The local package is under `build/review-remediation-final/`; it is not a
qualified cross-platform distribution.

The [large-workload measurements](evidence/review-m3-m5-measurements.json) verify
100,000 globally sorted rows on a 319.35-MiB fixture, with about 54.09 MiB service
peak RSS and 8.42-ms batch cancellation. The separate
[no-output scan](evidence/review-scan-measurements.json) cancels in 6.46 ms without
changing native state. [Fresh/warm measurements](evidence/review-cache-rss-measurements.json)
record 20 fresh-process samples (468.24-ms start/open/first-read p95) and 1.82-ms
warm viewport p95. These are dataset-specific measurements with warm OS caches.

## Completion ledger

Populate commit/change references and evidence when work is actually verified.

| Step | Status | Completion reference | Verification evidence |
| --- | --- | --- | --- |
| 1 — Protected exports | Verified locally | Shared protected writer and final-review guard | [Build](evidence/review-step1-build.txt), [8 Qt checks](evidence/review-step1-tests.txt), [independent consumers](evidence/review-step1-consumers.json) |
| 2 — Job identities and legacy recovery | Verified locally | Sidecar v2 execution namespaces; explicit legacy conflict recovery | [Build](evidence/review-step2-build.txt), [14 job tests](evidence/review-step2-tests.txt) |
| 3 — SQL interruption | Verified locally | Indexed record/Find probes and nested cooperative SQLite guards | [Build](evidence/review-step3-build.txt), [114 engine/record/job/scan tests](evidence/review-step3-tests.txt) |
| 4 — Reviewed resume | Verified locally | Fresh metadata, explicit current-state review and execution identity checks | [Build](evidence/review-step4-build.txt), [7 Qt checks](evidence/review-step4-tests.txt), [16 service/client job tests](evidence/review-step4-service-tests.txt) |
| 5 — Cancellation and close | Verified locally | Asynchronous cancel/status/release controller and private unresolved-job recovery | [Build](evidence/review-step5-build.txt), [15 Qt checks](evidence/review-step5-tests.txt), [16 job tests](evidence/review-step5-service-tests.txt) |
| 6 — Shared clipboard parsing | Verified locally | Common bounded native/TSV decoder for grid and records | [Build](evidence/review-step6-build.txt), [21 Qt checks](evidence/review-step6-tests.txt), [4 record tests](evidence/review-step6-service-tests.txt) |
| 7 — Operation-job schemas | Verified locally | Action-specific OpenAPI 3.0 schemas, response contracts and executable examples | [Build](evidence/review-step7-build.txt), [4 schema/example tests](evidence/review-step7-schema-tests.txt), [59 service/job tests](evidence/review-step7-service-tests.txt), [validator environment](evidence/review-openapi-environment.txt) |
| 8 — Contracts and reports | Verified locally | Current job/handler/view contracts and fact-checked ODS conversion reports | [Build](evidence/review-step8-build.txt), [18 interchange tests](evidence/review-step8-tests.txt); local documentation links checked |
| 9 — Combined local qualification | Verified locally | Frozen candidate, complete suites, Cocoa/installed checks and measured limits | [Qualification](evidence/review-final-qualification.json), [test summary](evidence/review-final-test-summary.txt), [native scale 1](evidence/review-native-regressions-1.txt), [native scale 2](evidence/review-native-regressions-2.txt), [verification record](evidence/review-remediation-verification.json) |
| Follow-on platform release gate | Pending | — | — |
