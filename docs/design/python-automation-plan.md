# Workbook Python automation implementation plan

The first automation batch adds portable workbook modules and explicitly invoked,
short atomic macros. The service executes approved code through the existing
transaction broker. REST, CLI and SDK provide the first interface; the Qt code
editor and custom worksheet functions follow in separate batches. Implementation,
PRs and review are authorized in this chat on November 9, 2026.

## Delivery order

| Increment | Scope | Completion evidence |
| --- | --- | --- |
| 1 | Native modules, scalar macro declarations and reviewed source changes | Version migration, static validation, no execution on open, native copy/reopen and export omissions |
| 2 | Service-local activation tied to workbook/code fingerprint | Default refusal, immutable operator grants, changed/external code refusal, no grant carried in native files |
| 3 | Atomic macros through the transaction broker | Typed arguments/results, sheet/table writes, required hooks, constraints, shared Undo/Redo and durable deduplication |
| 4 | Bounded execution diagnostics and cancellation | Success/failure logs, traceback, timeout, crash, cancellation, invalid protocol and no partial database commit |
| 5 | REST/OpenAPI, CLI and SDK completion | Real HTTP examples, client parity, lost acknowledgement reconciliation and explicit local activation |
| 6 | Integrated examples, review fixes and installed qualification | Full suites, pinned-runtime/installed smoke, reproducible example and evidence for each PR |

Each increment has a separate branch/PR based on its predecessor. Review its
diff against the contracts below, run relevant negative cases, correct findings
before proceeding, and record the tested source, commands, results and review
findings. Final qualification covers the combined batch. Hosted checks and
physical GUI/platform acceptance have separate statuses.

## Native module contract

Native format v5 extends v4's `dh_relational` catalog with `module` objects;
physical user-table DDL is unchanged. First module save upgrades formats v1–v4
inside the managed transaction. Earlier readers reject v5. Empty v5 catalogs
remain v5 after deletion. Code belongs to the workbook, independent of sheets.

Each module has a stable UUID, case-sensitive Python identifier name, source,
and an explicit list of macro declarations. Source is at most 128 KiB of UTF-8;
one catalog object remains capped at 256 KiB. There are at most 16 modules and
2 MiB of serialized module metadata, within the existing aggregate catalog cap.
Static syntax/declaration validation never imports or executes workbook code.
Names are ASCII identifiers of at most 64 characters, excluding Python keywords
and private `__` names. Duplicate module/macro/argument names reject.

A declaration names a top-level synchronous function, up to 32 named scalar
arguments and a scalar result. Scalars use existing Text, Integer, Number,
Boolean and ISO date/time/datetime kinds; each explicitly declares nullability.
Defaults are typed and explicit. Argument names cannot be `ctx`. Each invocation
is `function(ctx, **arguments)`. No import resolver between workbook modules,
package installation, automatic event registration or formula UDFs is introduced.
Standard-library and installed-package imports run with the trusted service
runtime's ordinary capabilities. A spawned worker is containment, not a sandbox.

Code save/delete requires the current code fingerprint and workbook state. It
clears session history on commit. A configured required external handler must
explicitly implement `on_code_structure(ctx,event)` for these operations; before
and after events join the parent transaction. Failure rolls back metadata,
migration and generated data. Existing data-event handlers remain applicable to
macro edits. Code metadata cannot authorize or activate itself.

## Activation contract

The code fingerprint is SHA-256 over a canonical, ordered collection of every
stored module's identity, name, source and declarations. The activation key is
`<workbook UUID>:<fingerprint>`. Fingerprints are computed from actual stored
content, never trusted from a stored claimed digest. Unrelated data edits do not
invalidate approval. Any code/declaration/name/identity change does.

An operator passes exact keys through the owned service's `--approve-code`
startup option (repeatable) or the embedded service/SDK constructor. The
allowlist lives only in that service process. A service started with
`--owner-channel` also accepts one JSON line per approval, `{"approve": key}`,
on its standard input, which only the parent process that started it holds
(`LocalService(owner_channel=True).approve(key)`). Through that channel the
allowlist is append-only for the life of the process: a grant reaches workbooks
already open, replaces any earlier granted key for the same workbook UUID,
never removes a start-up key, and the total of start-up and granted keys stays
within 256. Without the flag standard input is never read. REST exposes
inspection and refusal but no endpoint that grants execution; request-body
fields cannot create grants. The authenticated localhost bearer does not by
itself activate code, and a client attached to a service it did not start
cannot approve; that service's operator configures grants. Native copies with the same workbook identity and identical code match the same
explicit key within an already configured service; a fresh service needs its own
operator grant. Reverting to an identical approved code version matches again.

Opening, polling, refreshing, exporting and copying never invoke modules. An
ordinary direct SQLite edit bypasses hooks; its changed code must match an
operator grant before a later explicit macro call. No skipped events replay.

## Atomic macro contract

`macro_run` is one ordinary managed operation, with expected state, operation ID,
module ID/name, macro name, reviewed code fingerprint and scalar argument object.
Its receipt contains the scalar result, bounded diagnostic log, module/macro
identity and executed fingerprint. Repeated identical operation IDs return the
durable receipt without executing Python again; different arguments with an old
ID reject. Reconcile an unknown acknowledgement before starting another edit.

Inside the writer transaction, revalidate stored source and activation after
refresh, bind defaults and reject missing/extra/invalid arguments, then spawn the
worker. Its context reuses `get/value/set`, `table_schema/table_get/table_set`,
`table_insert/table_delete`. All context reads see staged writes. There is no
loopback REST and no structural/code mutation or recursive macro method in the
broker. Every broker validation error aborts even if Python catches the error.
Result typing and serialization complete before commit and before required
change handlers finish. Hooks run in the existing bounded deterministic rounds.

All sheet/table changes and hook-generated edits commit or roll back together.
Supported data changes form one Undo/Redo entry; Undo/Redo runs the existing
applicable handlers and never reruns the macro. Read-only and repair handles
reject macro mutations. The existing 10,000-field, 8-MiB before/after and history
budgets apply. Native constraints remain immediate. A macro is allowed to return
without edits, but still receives a durable execution receipt.

## Execution and diagnostics

The worker gets at most two seconds including spawn/import and shares the
parent operation's five-second budget. It is reaped after success, failure,
timeout, cancellation and protocol loss. Cooperative parent polling observes
the existing operation cancellation event while Python is busy. A worker-side
watchdog bounds ordinary Python execution if its service disappears. Arbitrary
subprocess trees and hostile code containment are outside this trusted-code tier.

Broker frames are capped at 1 MiB. Arguments are capped at 128 KiB of serialized
UTF-8, scalar Text/results at 64 KiB. Captured stdout/stderr retain only the last
8 KiB of valid UTF-8; tracebacks are bounded to 8 KiB. Success and confirmed
rollback receipts retain diagnostics. Errors distinguish activation refusal,
invalid arguments/result, Python exception, worker crash/protocol failure,
timeout and cancellation; failed operations expose no successful result.

Only context-mediated database edits have rollback semantics. File/network
effects from trusted Python cannot be undone by SQLite. This batch supplies no
durable external-effect queue, multi-transaction orchestration or exactly-once
external effects. Documentation and examples use database-only atomic macros.

## Verification and handoff

Use temporary native files and actual HTTP clients. Cover v1–v4 migration and
rollback; required code hooks; independent edits; source/declaration bounds;
copy/reopen; source hash review races; default/changed/missing activation;
parameter/default/result typing; table constraints; mixed sheet/table rollback;
hook-generated changes and recursion; Undo/Redo; no-op executions; durable retry,
crash/restart and lost response; cancellation, timeout, noisy logs and broker
failure; native-only export omission; read-only/repair and malformed REST inputs.

Run the full Python/Qt suites, optional OpenAPI/capture checks, and the installed
bundled-runtime example. Verify installed service imports, source/artifact hashes
and sealed bundle resources after smoke. Keep generated bytecode outside the
signed app by using `-I -B` for installed interpreter probes. Supply a workbook,
source, exact activation command and expected macro/Undo/failure results. Manual
GUI/editor, VoiceOver, Windows/Linux and existing release gates remain open.

## Completion ledger

| Increment | PR | Implementation and review | Evidence |
| --- | --- | --- | --- |
| 1 | [PR 125](https://github.com/hybridmachine/DataHorizon/pull/125) | Native v5 module storage, migrations, static declarations, required code hooks and inert reads/copies; implementation self-review performed | [Verification and review](evidence/python-automation-task1.json) |
| 2 | [PR 126](https://github.com/hybridmachine/DataHorizon/pull/126) | Exact immutable service-local operator grants and source-change refusal; implementation self-review performed | [Verification and review](evidence/python-automation-task2.json) |
| 3 | [PR 127](https://github.com/hybridmachine/DataHorizon/pull/127) | Atomic brokered mixed-data macros, required hooks, history and durable results; implementation self-review performed | [Verification and review](evidence/python-automation-task3.json) |
| 4 | [PR 128](https://github.com/hybridmachine/DataHorizon/pull/128) | Bounded UTF-8 diagnostics, worker crashes/timeouts/cancellation and no partial commits; implementation self-review performed | [Verification and review](evidence/python-automation-task4.json) |
| 5 | [PR 129](https://github.com/hybridmachine/DataHorizon/pull/129) | REST/OpenAPI/capabilities, CLI/SDK, live cancellation and unknown-response reconciliation; implementation self-review performed | [Verification and review](evidence/python-automation-task5.json) |
| 6 | [PR 130](https://github.com/hybridmachine/DataHorizon/pull/130) | Runnable examples, final review fixes, full suites and installed qualification; implementation self-review performed | [Verification and review](evidence/python-automation-task6.json) |
