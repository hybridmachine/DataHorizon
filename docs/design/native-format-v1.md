# Development native format v1

This format is experimental. No migration from future formats or production
durability qualification is claimed. SQLite foreign keys are enabled by the
engine; independent connections choose their own enforcement settings.

| Table | Purpose | Direct-access policy |
| --- | --- | --- |
| `cells` | Sparse source, input kind and style keyed by sheet ID, row and column | Ordinary SQLite reads/inserts/updates/deletes are intentional |
| `sheets` | UUID, case-insensitive name, display position | Read catalog; edit through managed operations |
| `dh_meta` | Singleton format version, workbook UUID, managed revision | Reserved |
| `operations` | Operation ID, request digest and committed receipt | Reserved |

Coordinates are one-based and constrained to 1,048,576 rows by 16,384 columns.
Sheet IDs come from `sheets.id`; no Python callback is needed for cell insertion.
Name matching follows SQLite's ASCII NOCASE collation in this development tier.
`source` is UTF-8 text, limited by managed entry to 64 KiB. `kind` is `auto`,
`text`, `number`, `boolean`, `null` or `formula`. `auto` recognizes formula source
beginning `=`, finite decimal numbers, TRUE/FALSE and an apostrophe text prefix;
other source is text. `formula` preserves source beginning `=` independently of
subsequent display formatting. Explicit `text` preserves leading zeros and
empty strings. Explicit `null` has a null typed value; absence has no row.
`style` is a JSON object: format (`general`, `text`, `fixed2`), bold/italic
booleans, align (`left`, `center`, `right`), background (`#RRGGBB`). Applying a
format never retypes existing input. Text format changes subsequent auto entry.

For example, from an independent SQLite connection:

```sql
SELECT id, name FROM sheets;
INSERT INTO cells(sheet_id,row,col,source,kind)
SELECT id, 2, 1, '00123', 'text' FROM sheets WHERE name='Sheet1';
UPDATE cells SET source='42' WHERE sheet_id=(SELECT id FROM sheets WHERE name='Sheet1')
  AND row=2 AND col=1;
```

These changes run no DataHorizon hooks. They do not increment managed revision
or promise history. Refresh uses the long-lived connection's data_version to
rotate the state token and discard session Undo/Redo. Formula values are derived
within read snapshots rather than persisted, so reopening never trusts an old
calculation cache. There is no replay of historical changes.

Only the exact v1 schema is accepted. Additional tables, schema edits, triggers,
metadata changes or newer versions require reopening a compatible file; the
preview does not attempt repair. User tables and managed SQL are later work.
The schema comparison deliberately restricts SQL rather than accepting writes
whose actual effects cannot yet be captured. Ordinary cell writes remain
independent of registering application functions or Python callbacks.

Committed receipts share the data transaction. Failed operations roll back and
return an explicit error; confirmed transactional failures attempt a separate
durable failure receipt. A crash or storage failure in that gap can still leave
an unknown ID. Rejected requests before transaction admission need not have a
receipt. Do not treat a missing
receipt as proof of rollback. The GUI locks further writes until a successful
lookup reconciles an unknown outcome. Session handles differ from workbook UUIDs
so a snapshot copy can be opened alongside its source.

One engine connection serializes operations for each open workbook. Engine
transactions use BEGIN IMMEDIATE and SQLite synchronous FULL with the default
rollback journal. No WAL sidecar portability claim is needed in this tier.
Snapshots use the SQLite backup API into an exclusively created destination.
The source is never overwritten by Save a copy. History is in service memory,
at most 100 operations and 16 MiB of serialized cell changes; it does not survive
a service restart or an external edit. Operation receipts remain in the file.
