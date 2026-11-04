# M1 SQLite change-capture experiment

November 3, 2026. This records the first bounded capture comparison in M1;
it does not close M1 or enable managed SQL. The experiment is isolated under
`tests/`, is not installed, and does not change native format v1.

## Decision and alternatives

Retain explicit shared-engine mutations for the current preview. A connection-local
TEMP trigger journal is a viable candidate for a restricted future SQL prototype,
with mandatory recursive triggers, captured tables fixed before execution,
transaction ownership, reserved-state protection and hard capture limits.
Integrating it with Python hooks, history, state tokens and receipts needs a
separate contract and engine increment before any SQL resource is exposed.

| Approach | Evidence | Decision |
| --- | --- | --- |
| stdlib `sqlite3`, known-target engine edits | Current engine integration tests; explicit old/new reads in the measurement baseline | Keep for existing operations; callers naming their targets cannot describe arbitrary SQL effects |
| stdlib `sqlite3`, TEMP AFTER trigger journal | 18 statement cases replay all old/new images against independent fixture snapshots; 16 contract tests cover failure and bounds | Candidate for a declared subset; no application SQLite functions are required |
| `RETURNING` and `changes()` | Deleting one parent returns/counts one row while three stored rows change | Insufficient capture; no replacement for trigger/cascade row images |
| Native update hook | [SQLite documents](https://sqlite.org/c3ref/update_hook.html) omissions for WITHOUT ROWID tables and replacement deletes; stdlib exposes no update hook | Unsuitable for the native cell representation |
| Native preupdate/session binding | Local SQLite compile flags include both extensions, but Python's public stdlib connection has no corresponding API | Unmeasured alternative requiring a binding/build/distribution decision; do not infer support from compile flags |
| Whole-store before/after snapshots | Independent correctness oracle over the small fixture | Not a production candidate for datasets larger than memory |

The [session extension](https://sqlite.org/sessionintro.html) also requires
declared primary keys, ignores NULL keys and excludes virtual tables. A future
binding comparison must retain those restrictions and measure bounded iteration,
rollback and packaging; no third-party binding was installed for this experiment.

## Fixture and correctness

The fixture has a rowid parent table, a composite-primary-key WITHOUT ROWID
record table with another UNIQUE key, a rowid effects table, foreign-key
cascades and an updatable view. A stored AFTER trigger writes effects; an
INSTEAD OF view trigger writes the underlying records. Values have no affinity,
so the tests distinguish INTEGER from REAL as well as NULL, TEXT and BLOB.
The snapshot oracle is deliberately independent of the journal. It validates
each captured old image against the replayed row, applies every new image,
and compares the final rows and storage types with the actual database.

The 18 cases cover insert/update/delete, both upsert paths, primary-key and
UNIQUE-key replacement, UPDATE OR REPLACE, primary-key movement, delete/update
cascades, AFTER and INSTEAD OF triggers, CTE-prefixed writes, delete-all,
no-op/ignored writes, multiple coalesced writes and typed values including
embedded NUL and non-ASCII text. Replacement records the old deletion and new
insertion. No-ops retain raw SQL events but produce no final changed rows.
This distinction is necessary before scheduling application change handlers.

Failure tests verify explicit rollback, private savepoint rollback, constraint
ABORT/FAIL/ROLLBACK, a deferred foreign-key failure at COMMIT, instruction-budget
cancellation, trigger-generated reserved-state writes and raw event/payload
exhaustion. Partial changes under SQLite's FAIL policy still roll back with the
parent operation. Capture rows and budget counters participate in the same
transaction; clearing the interrupt handler and authorizer permits rollback
and subsequent successful operations.

Negative controls are executable: disabling recursive triggers loses the old
row deleted by a replacement into another composite key. SQLite requires
[recursive triggers for REPLACE delete triggers](https://sqlite.org/lang_conflict.html).
The `RETURNING` control misses cascade rows, consistent with its
[documented trigger/cascade exclusions and buffered output](https://sqlite.org/lang_returning.html).

## Bounds and access boundary

The candidate enforces at most 10,000 raw events and 4 MiB of charged payload
inside its triggers, before appending each journal row. Charges include old and
new value byte lengths plus 128 bytes/event. These are experiment limits,
not SQLite/Python memory guarantees or the final managed SQL contract.
Repeated/no-op updates and trigger/cascade effects consume the same budget.
Tests hit exact UTF-8/BLOB byte boundaries and prove that outer IGNORE/REPLACE
cannot suppress capture-limit failure. A million-row statement with RETURNING
is stopped after the small test event cap, rolling back all earlier writes.
Journal reads use at most 128 events per fetch; the report materializes only the
already bounded event set. Fixture snapshots are not part of that candidate path.

The driver owns BEGIN IMMEDIATE through COMMIT; a SQLite authorizer rejects user
transaction/savepoint control, schema changes, PRAGMA, attachments, private
journal access and reserved-table writes, including writes through stored
triggers. No keyword-prefix parsing grants mutation access. The experiment does
not accept arbitrary schemas or functions as trusted input.

[TEMP triggers are connection-local](https://sqlite.org/lang_createtrigger.html).
An independent connection can update stored data without registering callbacks
and without adding managed journal entries. SQLite-native fixture triggers still
run normally. A backup contains the main stored data and no TEMP journal/schema.
This preserves the managed/direct distinction; it does not add historical Python
handler replay on refresh.

## Measurements and exclusions

[Local JSON evidence](evidence/sqlite-capture-benchmark.json) records Python and
SQLite versions, compile options, typed row images, negative controls, independent
access, snapshot checks and 100 warm in-memory samples for each candidate.
[Full regression verification](evidence/sqlite-capture-verification.txt) passes
162 Python tests (including the 16 capture tests) and 73 Qt checks including
suite setup/cleanup. Local loopback access is required for the existing service
and desktop integration tests.
The explicit baseline performs one known-target edit with old/new reads; the
journal path includes capture, policy setup, iteration and transaction completion.
These are different operation costs, not a binding-speed ranking. Neither timing
includes REST, disk durability, spawned Python workers or cold-cache qualification.
Hosted CI runs the same experiment and preserves its JSON with the existing
`m1-measurements-*` artifacts. Hosted results require inspection before claiming
cross-platform evidence.

Arbitrary/virtual tables, user BEFORE triggers, same-row trigger rewrites,
untrusted user-defined SQL functions, schema evolution, concurrent independent
schema changes and unrestricted result sets are not qualified. This journal
does not establish hook ordering for arbitrary SQL. Native cell typing/formula
validation and history/receipt integration remain required before SQL mutations.

The public matrix remains unchanged: managed SELECT, DML, DDL, PRAGMA,
ATTACH and transaction control are unavailable. Explicit direct SQLite access
remains available. The next dependency is the M1 service/Python execution
comparison, followed by the remaining grid and native-platform experiments;
none of those are waived by this evidence.
