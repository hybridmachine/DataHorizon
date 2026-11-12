# Development native formats v1 through v7

## Unmaintained formulas format v7

v7 reuses v6's schema and adds the cell kind `unmaintained`. A workbook enters
v7 in the same transaction as the first cell written with that kind, upgrading
v1-v3 to the v4 relational catalog layout as v5 and v6 do; no operation lowers
the version, including undo of the edit that marked the cell. v1-v6 readers
reject v7, and an `unmaintained` row in a file below v7 is rejected as
`INVALID_FORMAT`.

An `unmaintained` cell holds a formula whose references a structural edit could
not maintain: neither the strict parser nor the lenient reference scanner can
read it (`formula.Unmaintainable`). Its `source` is the original formula text,
starting with `=`. It has no dependencies and is never evaluated; its value is
the error `#REF!` with the message "References in this formula were not
maintained through a structural edit. Re-enter the formula." A formula that
refers to it sees that error.

Row/column insert and delete, sheet rename, delete and duplicate, and move mark
a formula they scan for rewriting and cannot read, instead of rejecting the
operation. A formula on a sheet the operation does not change is marked only
when it contains an unclassified `!` (`has_sheet_reference`), since only a
qualified reference can reach another sheet; a duplicated sheet's copy is
marked, never its source. The marked cell keeps its style. The marking is part
of the operation's transaction, history and changed-cell limits, and follows
the event contract of other generated reference rewrites. The receipt adds
`unmaintained` (the first 100 `{sheet, cell}` locations after the operation)
and `unmaintained_count` when any cell was marked.

An edit request naming the kind is `INVALID_INPUT`, and operation-job staging
does not accept it. Full paste and record paste preserve the kind a clipboard
cell carries (the clipboard is client data), without translation; fill copies
it unchanged; values paste rejects its error. Editing the cell with any other
kind clears the state. `replace_all` on an `unmaintained` cell changes its
source and keeps the kind. Whole-workbook XLSX and ODS formula exports write the
source unchanged as the cell formula (ODS with the `of:` prefix and no cached
value) and report `formula.unmaintained` with severity `approximated`; the
source is written verbatim, without the sheet-identity-to-name conversion
applied to `formula` cells. A selection export in formulas mode cannot check
its references against the selection and is refused with `UNSUPPORTED_FORMULA`;
values exports reject it like other formula errors. The row is inert: a direct SQLite edit can change its
kind or source, and DataHorizon does not replay or repair it.

## Workbook names and bindings format v6

v6 reuses v4's `dh_relational(kind,id,payload)` schema and adds the catalog
kinds `name` and `binding`. A workbook enters v6 in the same transaction as the
first use of either kind; module saves never lower an existing v6 version.
v1-v5 readers reject v6, and a `name` or `binding` row in a file below v6 is
rejected as `INVALID_FORMAT`.

A `name` payload has exactly `id` (canonical UUID), `name`, `sheet_id`, `row1`,
`col1`, `row2` and `col2`. The name is a case-insensitively unique identifier of
at most 64 characters that is not a cell address, `TRUE`/`FALSE` or a function
name. The target is held by sheet identity. A broken name has `row1` through
`col2` set to null, and `sheet_id` null or the identity of a sheet whose cells
it no longer covers. A workbook holds at most 256 names.

A `binding` payload has exactly `id` and `module_id` (canonical UUIDs), `event`
(`cell_change` or `table_change`), `function` (a top-level function name),
`required` and `scope`. The scope is one of `{kind:"sheet", sheet_id}`,
`{kind:"range", range_id, sheet_id, row1, col1, row2, col2}`,
`{kind:"name", name_id}` for `cell_change`, or `{kind:"table", table_id}` for
`table_change`. A broken range scope has null coordinates. A sheet or range
scope whose sheet was deleted keeps that sheet's identity and is broken because
the sheet no longer exists; readers also accept a null `sheet_id`. A workbook holds at
most 16 bindings. The code fingerprint covers each binding without the range
coordinates, so a range scope that moves with row/column changes does not need
fresh approval; a workbook without bindings keeps its v5 fingerprint. Name and
binding rows are inert: opening, reading and copying never import or run code.

## Workbook code format v5

The first managed module save transactionally upgrades v1–v4 to v5 and reuses
v4's physical `dh_relational(kind,id,payload)` schema. The additional `module`
kind has a stable UUID, unique case-sensitive Python identifier name, source
and explicit scalar macro declarations. It belongs to the workbook rather than
a sheet. Static syntax/declaration validation never imports or executes source;
v4 readers reject module metadata. Empty code catalogs retain version 5.

Source is capped at 128 KiB, each serialized object at 256 KiB, and workbook code
at 16 modules/2 MiB within the existing 512-object/8-MiB catalog bounds. Managed
save/delete requires current state and reviewed code fingerprint, runs required
code-structure hooks and clears supported history on commit. DDL upgrades,
metadata and generated edits share rollback. Native copies preserve source and
declarations. Spreadsheet exports report executable source among native-only
omissions; whole-workbook import refuses a destination containing modules.

Execution permission is never stored in the native file. An executing service's
operator must grant the exact workbook UUID and SHA-256 of its canonical code
collection. Refresh after independent code edits recomputes the fingerprint;
changed source cannot reuse a previous grant. Normal data edits do not revoke
identical code. See the [automation guide](python-automation-guide.md).

## Structured table format v4

The first managed table creation upgrades v1/v2/v3 transactionally to v4. It adds
workbook-level `dh_relational(kind,id,payload)` metadata, independent of sheets,
plus catalog-registered ordinary SQLite user tables. Each table has stable table
and column UUIDs and `_dh_row_id INTEGER PRIMARY KEY AUTOINCREMENT` identity.
Independent SQLite inserts obtain identities without Python callbacks. API row
identities use decimal strings. Native copy/reopen validates the supported table
DDL against its catalog and streams type validation within the opening budget.
Unknown tables/triggers/schema changes remain rejected.

The first tier creates/inspects/drops empty tables through shared
managed operations, REST, CLI/SDK and Data → Tables and queries. Required handlers
must explicitly define `on_table_structure(context,event)`; hook failure rolls
back table DDL, metadata upgrades and generated cell edits together. Whole-workbook
imports reject relational destinations; sheet XLSX/ODS exports name omitted
relational objects. Selection promotion also populates a new table with reviewed
typed values and independent row identities. Record reads use bounded pages;
transactional table hooks can read/write records and participate in shared
cell/record history. Direct table formulas or sheet/table synchronization are
not introduced. Reviewed column-schema rebuilds now preserve identities and the
AUTOINCREMENT high-water mark, with atomic constraints/conversion and explicit
column loss. Populated drops require explicit loss review and run transactional
data hooks. Typed record insert/update/delete and shared history are now public.
Registered native indexes preserve column identity through rebuilds and block
dependent column/table removal. Unique column constraints retain their native
autoindex ownership. Full-table typed predicates and authenticated keyset cursors
run bounded reads; counts can explicitly be unavailable. Index builds observe
the operation deadline and cooperative cancellation. SQLite write interruption
may release the writer transaction; the engine reacquires it and checks receipt
ownership before recording the confirmed rollback.

Registered relationships now generate native same-type foreign keys to required
UNIQUE parent columns, with RESTRICT updates/deletes and catalog-owned child
indexes. No connection requires Python callbacks. This tier rejects self/cyclic
graphs and requires removing incoming relationships before rebuilding a parent.
Child rebuilds preserve outgoing constraints, indexes and identities atomically.

Ordinary open/refresh rejects external orphans. Explicit `repair:true` opens a
restricted handle: inspection reports up to 100 orphan locations; only reviewed
orphan child-key updates/deletions are admitted. Required hooks still participate,
and all relationship orphans must be resolved before commit. Repair clears
history; reopen normally afterward. Invalid scalar/schema corruption remains
separate from orphan repair. Read-only repair inspection is supported.
Read-only queries use private native snapshots and bounded, ephemeral result
spools. Saved definitions/defaults/dependencies/shape live in v4 metadata; opening
them does not execute SQL. See [saved definitions and export](#saved-definitions-and-relational-export)
and the [relational plan](relational-workbench-plan.md).

The sections below describe the earlier formats and their retained behavior.

New files use 8-KiB SQLite pages to keep common 1-KiB Text cells inline. Existing
files retain their page size; page geometry does not change the logical schema or
metadata version. See [opening performance](native-opening-performance.md) for
validation, measured capacity and explicit independent SQLite copy guidance.

Format v3 adds reserved `dh_objects(namespace,id,sheet_id,payload)` for portable
presentation and named list definitions. The first managed metadata write upgrades
transactionally; v1/v2 remain readable with their original exact schema. Older
clients reject v3. Metadata is bounded, validated on open/copy/refresh, and
independent SQLite cell access still needs no callbacks. See [the completion
contract](m3-m5-next-ten.md).

Format v2 adds explicit `date`, `time`, and `datetime` input kinds with strict ISO
sources defined in [the date/time contract](date-time-next-ten.md). The physical
schema is unchanged. New files start at v1; the first temporal write upgrades the
metadata to v2 in its parent transaction, including hook rollback. Undo does not
downgrade a file. This client reads v1/v2; older clients reject v2. Unknown versions
and v1 files containing temporal inputs are rejected without repair. Temporal
sources are checked on open, copy and external refresh within the opening budget.
Independent SQLite access still needs no callbacks and does not run hooks.

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
The earlier CSV date tier stored normalized YYYY-MM-DD as `text`; those existing
inputs remain Text. See [the CSV typing contract](editing-csv-next-five.md#csv-column-typing).
`style` is a JSON object: format (`general`, `text`, `fixed2`, `percent2`,
`number`, `percent`, `currency`, `date_iso`, `date_mdy`, `date_dmy`, `time24`, `datetime_iso`), numeric precision/grouping/currency label,
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

For v1–v3, only the exact corresponding physical schema is accepted. Additional tables, schema edits, triggers,
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
Per-edge patterns follow the additive edge-style tier documented below; border
color selection and merged-cell borders remain unavailable.

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

Input inspection (`POST /v1/recovery/inspect`, `inputs`) checks stored inputs
without evaluating them and distinguishes three kinds of formula input:

- a `formula` cell the calculator can evaluate is valid;
- a `formula` cell it cannot evaluate but whose references the lenient scanner
  reads (an unknown function or name, a whole-row or 3D reference, an external
  reference, an array constant, as degraded imports store them), and any cell
  of kind `unmaintained`, are valid inputs. They are counted under
  `unevaluable_formulas` (each sample with its `reason`, a `formula.*` category)
  and `unmaintained_formulas`, each `{count, samples}` with up to 100
  `{sheet, row, col}` samples, and never as issues;
- a `formula` cell whose source the scanner cannot even read is an issue
  (`INVALID_FORMULA`), as before.

`inputs_valid` is true when inspection completed with no issue, so the first
two kinds leave it true.


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

## Operation-job sidecars

Reviewed operation jobs use version-2 SQLite journals under
`.<native-filename>.dhjobs/`, separately from native format versions 1–3. They
retain sealed inputs, progress, admitted operation IDs and per-batch receipts.
They are bound to workbook ID and filesystem identity; copying a workbook does
not transfer an execution plan or authorize replay. Native snapshots, XLSX/ODS
and CSV exclude these journals. Explicit release removes staged data and frozen
read snapshots, while durable native operation receipts remain in `operations`.
Missing receipts do not prove rollback; unknown attempts block automatic execution
and release. See [job bounds and recovery](m3-m5-next-ten.md).

Each v2 journal stores a random `execution_id` independent of its friendly job
name; future batch IDs include that namespace. Releasing and recreating a name
does not reuse earlier native receipt IDs. Version-1 journals remain readable;
eligible journals reconcile old requests before transactional upgrade. Exact
admitted requests and IDs remain unchanged. Explicit reviewed legacy conflict
recovery retains the unrelated original receipt as `not_admitted` sidecar evidence,
without inventing a native rollback receipt. Missing receipts still block work.
This sidecar version does not change native schema versions 1–3; older services
reject v2 sidecars rather than silently downgrading them.

## Read-only SQL job tier

Query jobs do not add native schema objects. SQL accepts one SELECT/CTE over
registered user tables, named `:name` bindings and an explicit safe function
allowlist. Private snapshot connections enforce query-only authorization and
SQLite limits; reserved metadata, writes, DDL, transaction controls, attachments,
extensions and indirect pragma access are denied. SQL aliases preserve labels
and order, including duplicates; expression values retain SQLite scalar kinds.
Integers beyond the JSON exact range use `{type:integer,value:decimal-string}`.
BLOB, nonfinite and oversized Text results fail explicitly.

Snapshots have five seconds; execution has sixty seconds. One query executes and
four jobs are retained per workbook, with five-minute lazy expiry. Result storage
is capped at 64 MiB, 100,000 rows, 1,000,000 values and 256 columns. Result labels
are bounded to 1 KiB each / 64 KiB total; Text values to 64 KiB. Pages have the
ordinary two-second / 4-MiB / 4,096-value bounds. Only complete current results
are published. New generations or managed/direct source changes make old results
stale. SQL ORDER BY controls order; otherwise order may differ across runs.
The native snapshot and SQLite temporary work disk are separate from result-spool
storage and are measured in final qualification. Table reads take no full copy.

Close cancels/releases query work. A new service has no previous query handles
and requires rerun; startup conservatively removes private directories only when
an ownership marker matches and its service process is proven dead. Unknown or
still-live owners are retained. The CLI `query run` reads all frozen ordinal pages
before returning, rather than presenting the visible page as a complete result.
Managed SQL mutations remain a later capture/behavior contract.

## Saved definitions and relational export

The v4 catalog's `query` kind stores ID/name, original SQL, typed parameter
declarations, explicit defaults, stable table/column dependencies and ordinal
result labels. Names may contain spaces. The catalog retains its 256-KiB object /
8-MiB aggregate bounds and a 64-query cap; catalog/info responses contain query
summaries, with definitions read separately. A saved view is a named read-only
result definition, not native SQLite VIEW DDL or a materialized table.

Definition review/open validation prepares authorized SQL with zero output rows;
outer LIMIT replacement preserves duplicate labels. No data rows or Python code
execute. Schema changes prepare affected SQL against an empty proposed schema to
catch SELECT-star expansion and new ambiguity, without rewriting stored SQL.
Referenced rename/drop/type changes block until the definition is revised or
removed. Run rechecks stored dependencies/shape and binds saved defaults only
when an invocation value is absent. GUI definition digests reject unreviewed
external definition changes. Invocation values remain session drafts unless
explicitly saved as ordinary workbook defaults. Definition mutations require
structural hook opt-in and clear history on commit; failures roll back generated
edits and catalog changes together. Structural events include the affected
object's before/after metadata as well as the retained table catalog contract.

Relational exports consume the entire table/filter or complete frozen spool,
not the loaded page. Initial CSV/XLSX/ODS export is capped at 10,000 values
including headers and the ordinary 4-MiB response. Oversize/incomplete/stale
output rejects without a partial file. Exact integers beyond 15 decimal digits
or tagged SQLite integers become explicitly reported Text. Table temporal and
Boolean values retain their kinds in XLSX/ODS; query aliases do not infer them.
CSV is UTF-8, untyped and collapses NULL/empty Text, with ISO temporal text and
no relational temporal companion. Reports list actual native-only object names.
Qt reuses QSaveFile publication and checks native aliases after final review;
the SDK/CLI use atomic protected client publication with a final state/alias
check and explicit overwrite. Native inputs and definitions remain intact.
