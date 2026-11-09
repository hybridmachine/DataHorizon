# Native opening and SQLite page geometry

Task 2 of the macOS qualification batch. Native opening keeps its shared
five-second validation/integrity budget, read-only access and explicit failure
outcomes. Schema checks, temporal inputs, integer coordinates, sheet ownership,
metadata foreign keys and SQLite quick_check remain mandatory. Cell validation
uses one scan for temporal/coordinate/ownership checks instead of three scans.

New workbooks use 8-KiB SQLite pages. The native logical schema, metadata version,
source types, identities and direct-access contract are unchanged. With 4-KiB
pages, a typical 1-KiB Text cell plus its UUID/key fields exceeds the inline
payload threshold of the WITHOUT ROWID table and spills to an overflow page.
The 8-KiB geometry keeps that common payload inline. This is workload-specific;
it does not establish universal capacity or eliminate every opening limit.

Existing files retain their physical page size. An independent SQLite tool can
make an explicit compacted native copy without replacing the source:

```sql
PRAGMA page_size=8192;
PRAGMA synchronous=FULL;
VACUUM INTO 'new-unused-path.dhwb';
```

Use a new destination and retain the original. Run this outside a transaction;
WAL-mode page-size changes have SQLite restrictions. Reopen the copy through
DataHorizon before relying on it. An interrupted copy may be incomplete and must
not be reported as a successful backup. Native identities and stored operation
receipts are preserved; transient job sidecars and local presentation preferences
are separate files and are not transferred by SQLite. No historical hooks run.
See SQLite's [VACUUM contract](https://www.sqlite.org/lang_vacuum.html).

`tests/opening_benchmark.py` creates the legacy 270,000-row, 1-KiB Text shape,
copies it with 8-KiB pages and verifies every stored cell, native catalog and
operation receipt through a bounded-memory fingerprint. It records file sizes,
copy cost and each five-second opening outcome. The benchmark may also accept
an existing disposable fixture with `--legacy`; it never rewrites that file.
Each successful opening verifies the final stored Text cell through the engine.
OS cache state is uncontrolled and fingerprint/copy passes warm caches.

Local measurements and qualification are recorded beside the batch ledger.
Memory mapping was evaluated and was not adopted: it did not reliably resolve
the boundary, and changing storage failure behavior was unnecessary for this fix.
