# M1 native representation validation

November 4, 2026. Keep the versioned sparse WITHOUT ROWID cell representation
for native format v1. The bounded comparison records alternative rowid costs;
it does not change the format or establish production durability.

## Contract evidence

The experiment uses the real Workbook engine and an independent SQLite
connection. It checks explicit text identifiers, NULL, empty text, absence,
styled blanks and formula source separately. Reading a formula changes neither
stored input rows nor the cell schema; derived values remain read-snapshot
results. The six cell columns contain identity/coordinates, source, kind and
style. Formatting and stable workbook/sheet UUIDs survive reopen and snapshot.

A direct connection updates A1 and inserts A2 without registering functions.
An explicitly enabled trusted handler would reject the new negative A1 and
change B1 if historical changes were replayed. Refresh and reopen do neither:
B1 remains NULL, history clears, the state token rotates, and managed revision
does not increment. A formula referencing A2 evaluates from the refreshed input.
A write carrying the old state fails with a durable STALE_STATE rollback receipt.

Snapshot rows and identity match the source. A future format version is rejected
without changing that external value. The alternative rowid schema is also
explicitly rejected when opened as native v1; comparison files cannot silently
become supported native workbooks. Existing native/schema/failure tests retain
their broader corruption, coordinate and catalog coverage.

## Physical comparison

`tests/native_storage_experiment.py` creates four isolated databases: sparse
and dense 10,000-cell shapes with the native WITHOUT ROWID definition and an
otherwise identical rowid table. Sparse rows are 100 addresses apart; dense
cells occupy 100 columns. Both candidates keep the same source/kind/style data,
primary key and sheet identity shape.

[Local JSON](evidence/native-storage-benchmark.json) records file/page sizes,
SQLite query plans and 100 warm narrow-read samples. Reads use sheet/row equality
and a bounded column range. The rowid candidate keeps a separate primary-key
index; native queries use the table's primary key. The evidence supports
retaining the current representation for this tier, not a universal layout
ranking. Existing large-disk measurements remain separate.

Run `PYTHONPATH=service python3 tests/native_storage_experiment.py --output build/native-storage-benchmark.json`.
Two contract tests join normal discovery. CI runs the same report on all three
platforms and preserves it with the M1 artifacts. Measurements exclude REST,
RSS, cold-cache qualification, advanced storage/indexes and migrations.
The next experiment is the remaining Qt grid/frozen-pane candidate work.
