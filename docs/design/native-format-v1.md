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
Managed validation also checks the stored SQLite types: INTEGER affinity and
BETWEEN constraints alone can admit fractional coordinates. Noninteger or
out-of-range coordinates are rejected on open, external refresh and copy
validation. No rounding, deletion or other automatic repair is performed.
Sheet IDs come from `sheets.id`; no Python callback is needed for cell insertion.
Name matching follows SQLite's ASCII NOCASE collation in this development tier.
`source` is UTF-8 text, limited by managed entry to 64 KiB. `kind` is `auto`,
`text`, `number`, `boolean`, `null` or `formula`. `auto` recognizes formula source
beginning `=`, finite decimal numbers, TRUE/FALSE and an apostrophe text prefix;
other source is text. `formula` preserves source beginning `=` independently of
subsequent display formatting. Explicit `text` preserves leading zeros and
empty strings. Explicit `null` has a null typed value; absence has no row.
CSV date conversions store normalized YYYY-MM-DD as `text`; they do not add a
native date kind or change format v1. See [the CSV typing contract](editing-csv-next-five.md#csv-column-typing).
`style` is a JSON object: format (`general`, `text`, `fixed2`, `percent2`,
`number`, `percent`, `currency`), numeric precision/grouping/currency label,
font family/size/text color, wrap/bold/italic booleans, alignment, RGB fill and
legacy/per-edge borders. The additive property contracts are specified below. Applying a
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
Validation requires workbook/sheet UUIDs, valid sheet names, distinct nonnegative
integer display positions, and no orphaned cell references. External changes to
the workbook UUID are rejected by an existing engine. Independent connections
still choose foreign-key enforcement themselves; invalid stored relationships
are detected when managed access validates the workbook. Save a copy validates
the copied catalog, relationships and schema before acknowledging success.
The schema comparison deliberately restricts SQL rather than accepting writes
whose actual effects cannot yet be captured. Ordinary cell writes remain
independent of registering application functions or Python callbacks.
Only the literal `sqlite_` internal prefix is excluded from the signature;
similarly named user objects, such as `sqliteX...` triggers, are still rejected.
The managed revision remains an integer from 0 through 2^63−1. At the maximum,
new mutations roll back with `REVISION_LIMIT`; reads and snapshots remain usable.
Sheet display positions also remain signed 64-bit integers. When the maximum
position reaches 2^63−1, Add Sheet and Duplicate Sheet roll back with a durable
`POSITION_LIMIT` receipt. Reordering the current sheets compacts positions and
allows appending again. Existing data, reopening and snapshots remain usable.

Committed receipts share the data transaction. Recognized failures roll back
operation changes to a savepoint while retaining the original writer lock, then
commit the failure receipt before returning a rolled-back outcome. Another
service cannot claim the ID between data rollback and receipt storage. An
unconfirmed rollback or receipt-storage failure returns an unknown outcome.
Rejected requests before transaction admission need not have a receipt. Do not treat a missing
receipt as proof of rollback. The GUI locks further writes until a successful
lookup reconciles an unknown outcome. Session handles differ from workbook UUIDs
so a snapshot copy can be opened alongside its source.

One engine connection serializes operations for each open workbook. Engine
transactions use BEGIN IMMEDIATE and SQLite synchronous FULL with the default
rollback journal. No WAL sidecar portability claim is needed in this tier.
Snapshots use the SQLite backup API into an exclusively created destination.
The source is never overwritten by Save a copy. History is in service memory,
at most 100 operations and 16 MiB of serialized labels and cell changes across
Undo and Redo (excluding Python object overhead); it does not survive a service
restart or an external edit. Operation receipts remain in the file.

Cell borders use the native style `border: "box"` or `"none"`. The desktop
Format menu applies or clears all four edges on each selected cell using one
managed edit. Borders use the system text color with a two-logical-pixel stroke.
Per-edge patterns, line/color selection and merged-cell borders are unavailable.

## Bounded recovery validation

Opening validates native schema/catalog/coordinates and SQLite quick_check in
one read transaction under a shared five-second cooperative deadline. Snapshots
include quick_check in their existing backup/validation budget. Open handles
retain filesystem identity and stop on observed path replacement/removal;
ordinary direct SQLite content edits still use the external-refresh contract.
A failed identity check around a mutation preserves its operation ID and unknown
outcome rather than claiming a receipt in a different file. Read-only inspection
through the common REST interface performs no repair, code activation or
recalculation. Process-crash fixtures and exact limits are documented in
[the recovery increment](interchange-next-five.md#native-recovery-and-failure-hardening).


`percent2` is an additive native-v1 style token: finite numeric fractions display
as two-decimal percentages without changing inputs/kinds. Older readers that do
not recognize it can reject that cell style; they must not rewrite the input or
silently substitute another format. See the [percentage tier](spreadsheet-next-ten.md#percentage-format-contract).

Configurable numeric display adds `format: number|percent` with optional integer
`decimals` from 0 to 15 (default 2). Precision without a format means Number;
General/Text ignore it and legacy fixed2/percent2 always remain two places.
Precision-only managed patches preserve percentage categories. This additive
style extension does not alter schema version 1, stored inputs or numeric types;
older services can reject the new properties instead of silently dropping them.

Numeric styles also accept boolean `grouping` (default false), with invariant
comma thousands and dot decimal separators. Grouping without a category means
Number; a managed grouping-only patch preserves percentage categories and their
effective precision. General/Text display ignores this metadata.

`format: currency` displays an explicit `currency` label after the numeric value.
The label is three uppercase ASCII letters, default USD; precision/grouping are
shared with Number. This metadata does not perform exchange conversion or
validate a currency registry. A precision/grouping-only patch preserves Currency.

Font styles add `font_family` (1–64 trimmed characters, no ASCII controls, comma,
quotes or backslash), `font_size` (integer 6–72pt) and `text_color` (`#RRGGBB`).
Unspecified properties use application defaults. Requested family names persist
when platform fallback renders them. These remain additive format-v1 metadata.

Per-edge styles add `border_left`, `border_right`, `border_top`, `border_bottom`:
none/thin/medium/thick/dashed/dotted. Missing edges inherit legacy box (thin),
otherwise none. Managed legacy border presets clear earlier edge overrides;
explicit new edge fields in that patch take precedence. Native metadata remains
exact; interchange can canonicalize all-thin edges to legacy box.
