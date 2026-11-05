# Native SQLite capture binding comparison

November 4, 2026. This optional M1 experiment adds a real native preupdate API
to the [TEMP journal comparison](sqlite-capture-experiment.md). It is isolated
under `tests/`; the service still uses stdlib SQLite and exposes no managed SQL.
Native format v1 and the direct-access contract are unchanged.

## Decision

Keep explicit engine mutations in the preview. APSW preupdate is a candidate
for a future restricted SQL integration prototype: it captures typed old/new
images on the composite-key WITHOUT ROWID fixture, including replacement
deletes without enabling recursive delete triggers. TEMP capture remains a
callback-free alternative with its documented recursive-trigger requirement.
This experiment selects neither binding for the installed service. Migration
must first integrate typing/formula validation, hooks, history, state tokens and
receipts, and qualify the distribution/runtime on each supported platform.

The pinned optional wheel is APSW 3.54.1.0, with its private SQLite 3.54.1.
Local stdlib SQLite is 3.53.4; these are different builds. The
[provider installation contract](https://rogerbinns.github.io/apsw/install.html)
describes the private SQLite and platform wheels. CI installs the binding only
in a separate experiment job. Base/minimum-runtime jobs and application packages
do not install it. Future redistribution must evaluate wheel coverage, SQLite
options, interpreter/ABI support and the [upstream license](https://github.com/rogerbinns/apsw/blob/3.54.1.0/LICENSE);
this test dependency does not settle other-component licensing.

## Correctness and bounds

All 18 existing statement cases replay every typed native image against the
same independent fixture snapshot oracle used for TEMP capture. Inserts,
updates/deletes, upserts, both replacement keys, UPDATE OR REPLACE, key moves,
cascades, stored AFTER/INSTEAD OF triggers, CTE writes, no-op/ignored/coalesced
changes, NULL/INTEGER/REAL/TEXT/BLOB and embedded NUL are exercised. Logical
identity comes from declared primary-key columns, never undefined WITHOUT ROWID
rowid fields. Callback images are copied while the transient object is valid.

The driver owns BEGIN through COMMIT, authorizes the same fixed table surface,
denies user transaction/savepoint/schema/attachment/private-state access and
rejects multi-statement entries. It streams result iteration and retains at
most 10,000 raw events / 4 MiB of charged payload. Charges include old/new scalar
bytes and 128 bytes/event; copying one callback row is not an RSS guarantee.
Capture overflow sets a failure flag and interrupts SQLite; the driver checks
the flag and rolls back even if the VM finishes before noticing the interrupt.
A callback exception alone cannot promise rejection, as
[APSW documents](https://rogerbinns.github.io/apsw/connection.html#apsw.Connection.preupdate_hook).

Fifteen optional tests verify constraint ABORT/FAIL/ROLLBACK, deferred COMMIT
failure, exact UTF-8/BLOB budget boundaries, trigger/cascade charges, outer
IGNORE/REPLACE, callback errors with empty diagnostics, instruction cancellation, a million-row RETURNING statement
stopped at a small event cap, reserved-state protection and recovery. Raw native
callbacks remain after ROLLBACK; an executable negative control demonstrates
this. The driver explicitly discards attempted images on every failure or
rollback. Private savepoint reconciliation is unqualified and user savepoints
are denied. An independent stdlib connection writes without a Python callback
and produces no native capture events; skipped changes are never replayed.

No Session object is created on this connection: the provider warns against
combining it with a registered preupdate hook. Native session capture is a
separate unmeasured alternative. Virtual/system tables, incremental BLOB writes,
arbitrary schemas/functions, BEFORE/same-row trigger rewrites, schema races and
unrestricted result sets remain unqualified. SQLite describes the
[native preupdate scope](https://sqlite.org/c3ref/preupdate_blobwrite.html).

## Repeatability and evidence

Create an isolated venv, install `tests/requirements-capture.txt` with
`python -m pip install --only-binary=:all: -r tests/requirements-capture.txt`,
then run `python -m unittest discover -s tests -p test_native_capture.py -v`
and `python tests/native_capture_experiment.py --output build/native-capture-benchmark.json`.
Without APSW, ordinary test discovery explicitly skips these optional tests.

[Local evidence](evidence/native-capture-benchmark.json) records versions,
compile options, typed events, negative controls, direct access and 100 warm
in-memory samples per candidate. [Verification](evidence/native-capture-verification.txt)
passes 196 Python tests (including the fifteen optional contracts) and 76 Qt
checks; absent-binding discovery skips only the optional contracts. Different SQLite builds and policy/iteration
costs prevent attributing timings to binding overhead. REST, Python handlers,
disk durability, RSS and cold caches are outside these timings. Hosted jobs are
configured for Windows/macOS/Linux but cannot start under the current billing
block; the user authorized local verification and merge. This is local evidence,
not cross-platform or production qualification. M1 remains open.
