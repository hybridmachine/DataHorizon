# Python runtime and macro semantics

Status: agreed role for user code and managed/direct boundary; proposed event
names, lifecycle, and runtime API. Examples describe future behavior, not an
implemented API.

The implemented development tier stores native v5 workbook modules with explicit
typed macro declarations and runs short atomic macros only after exact
service-local operator activation. Sheet/table reads and edits use a spawned
worker and the parent transaction broker. Required externally activated
`on_change`, `on_structure`, `on_table_structure`, `on_table_change` and
`on_code_structure` handlers participate in managed behavior. Failures remain
visible and roll back applicable database changes.

See the [automation implementation plan](python-automation-plan.md) and
[runnable guide](python-automation-guide.md) for the implemented API, activation,
bounds and examples. The additional event lifecycle below remains proposed.
Automatic workbook event bindings, a Qt code editor, custom formula functions,
package environments, durable follow-up work and remote code activation are
separate increments. Source inspection/open never executes workbook modules.

## Programming surfaces

| Surface | Purpose |
| --- | --- |
| Workbook modules | Python source stored with the native workbook |
| Macros | Explicitly invoked Python entry points with arguments and results |
| Event handlers | Python entry points bound to generic workbook/data events |
| Worksheet functions | Python functions used in formulas to return typed results |
| External SDK | Python client code operating through REST |

DataHorizon provides generic events and operations. Users implement their own
workflow and business logic through these surfaces. Embedded code executes in
the service, including when REST or the CLI is the only client.

Code source and handler bindings should travel with the workbook. Runtime
packages, compatible Python versions, and dependency installation need a
reproducible environment specification; bundling arbitrary installed packages
into every workbook is not assumed.

## Proposed events

| Event | Timing | Intended capabilities |
| --- | --- | --- |
| `workbook_open` | After format checks and code activation | Initialize workbook behavior; does not represent past edits |
| `before_operation` | Within a managed write transaction, before the requested change | Inspect operation intent, reject it, or add supported preparatory edits |
| `on_change` | After actual input changes are staged, before commit | Inspect old/new values, validate, and request additional edits |
| `before_commit` | After change processing and required calculations | Read-only final validation; reject the transaction |
| `after_commit` | After the database transaction succeeds | Run follow-up work; any additional data write starts a new managed operation |

Handler bindings should filter by object and event and have an explicit,
deterministic priority and tie-breaker. Registration order must not accidentally
depend on a directory listing or module import side effects.

Normal read requests, viewport fetches, and polling do not generate write events.
Python worksheet functions execute when evaluation requires them. Additional
read or calculation events can be designed later; simply scrolling a grid must
not repeatedly trigger unrelated business actions.

## Transaction context

Transactional code receives a context containing:

- Workbook and object identities, operation ID, actor, origin, and cause chain.
- The requested operation and its arguments, where applicable.
- Access to actual old/new input values through a bounded change iterator.
- Transaction-aware data reads and edits through engine operations.
- Explicit access to calculation state or requested evaluation.
- Logging, execution limits, and cancellation state.

The context must preserve the current transaction. An internal edit should not
open a second REST request waiting for the first transaction's write lock.
Database changes made through this context roll back with the parent operation.
Raw `sqlite3` access from user code is independent access and does not gain this
guarantee merely because it runs in the same Python process.

An explicitly invoked macro may be a short atomic data operation or a longer
orchestration that creates several managed transactions. Its execution mode must
be declared, and errors must identify which transactions have already committed.
Do not hold a database write transaction across arbitrary network waits.

## Processing changes

Proposed rules:

1. Capture the actual input changes made by the requested operation, including
   supported SQL trigger and cascade effects.
2. Dispatch matching `on_change` handlers in a documented order over a stable
   batch of changes.
3. Apply handler-generated edits through the context and process their changes
   in subsequent rounds, preserving origin and parent operation information.
4. Stop when no new changes remain; enforce limits on rounds, work, and elapsed
   time. A cycle or exceeded limit fails the transaction visibly.
5. Recalculate required values and perform read-only final validation before
   commit. Persist any pending calculation state explicitly.

Define whether repeated writes to one input are exposed individually or
coalesced, how no-op assignments are handled, and whether a handler observes
intermediate or final values. These choices require prototypes and must be
settled before publishing a stable handler API.

Derived result updates are distinct from user input edits. Calculation should
not recursively masquerade as new grid edits. If a handler needs a calculated
value to be current, it must request evaluation or receive a clear pending/error
result. Calculation events and permitted mutations during evaluation need their
own contracts.

## Managed SQL parity

For SQL, `before_operation` can inspect statement intent but must not pretend to
know the complete affected row set. Execute the statement inside the transaction,
capture actual old/new changes, then allow transactional handlers to validate
or add edits before commit.

An `on_change` rejection can roll back already staged SQL changes. It does not
require arbitrary SQL to be rewritten into individual pre-edit callbacks.
Database constraints can reject a statement before `on_change`; handlers are not
a mechanism for temporarily violating immediate SQLite constraints.

Schema changes need a separate event and metadata plan. Unsupported statements
must fail explicitly until their semantics are implemented. See
[architecture](architecture.md#sql-integration).

## Bulk operations

A paste, import, or SQL update can affect millions of values. Expose operation
context and streamable/chunked change access rather than constructing an
unbounded Python list or invoking Python once per cell by default.

Chunked iteration inside one transaction is different from chunked commits.
An operation must state which it uses. For explicit chunked commits, report
progress, committed ranges/batches, and cancellation behavior. Transactional
handlers run for each declared transaction; whole-import success events occur
only when the overall operation succeeds.

## After-commit work and errors

External side effects belong in explicit follow-up work. A transactional hook
must not claim that a file write or external API call can be undone by rolling
back SQLite. This is an execution contract, not proof of technical isolation.

If durable after-commit work is supported, write its pending record in the same
transaction as the data and dispatch after commit. Crash recovery may retry a
handler, so provide stable event IDs and attempt information and require
idempotent effects where needed. Exactly-once delivery to external systems is
not promised.

Distinguish these outcomes in the GUI and API:

- Request rejected before mutation.
- Transaction rolled back because a handler, calculation requirement, constraint,
  timeout, or cancellation failed.
- Data committed with calculations explicitly pending.
- Data committed, but follow-up work is pending or failed.

Expose useful tracebacks and operation logs to authorized users. A configured
required handler that cannot load or execute must not be silently skipped.
Failure records for rolled-back operations must not themselves imply that the
data committed.

## Runtime policy and isolation

The process model remains open. Compare in-process execution with a separate
worker using the transaction context as a brokered API. Evaluate cancellation,
crashes, package dependencies, performance, and packaging on all target systems.
A separate process alone must not be described as a secure Python sandbox.

Workbook code is executable code. Activation/trust policy, network and filesystem
capabilities, and who may change or invoke code must be designed before enabling
untrusted imports or remote code execution. A trust decision should be local to
the executing service, not self-authorized by metadata carried in the workbook.

## Direct SQLite operations

Independent SQLite connections do not execute these handlers or macros. On
refresh/reopen, reload and invalidate derived state as described in
[architecture](architecture.md#external-edits-and-reopen); do not fabricate
per-edit events or retrospectively run skipped workflows. Configured open events
and subsequent formula evaluation retain their ordinary meanings.
