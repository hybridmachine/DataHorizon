# Structured tables and query workbench implementation plan

Status: first M8 increment in implementation, requested November 9, 2026. This plan
defines ten implementation tasks for structured tables, relationships and a
read-only SQL workbench. It builds on the existing managed execution engine,
declared-list records and job infrastructure. Implementation evidence is recorded
as work lands in the completion ledger; later tasks retain their planned status.

The intended result is a workbook containing Customers and Orders tables: users
can create them from sheet selections, enforce keys and a relationship, edit
records, save a joined query, export its results and reopen the native workbook
with its definitions intact. The features remain general data operations;
business validation belongs to explicitly activated user Python code.

The agent can implement and verify the engine, REST, CLI, SDK and Qt Widgets
surfaces independently. A prepared GUI walkthrough lets the owner perform
physical interaction and accessibility acceptance later. Existing M1/M7 gates,
macOS release qualification and deferred Windows/Linux acceptance retain their
own completion requirements.

## Scope and starting point

The current engine stores sparse sheets in native formats v1–v3, checks an exact
schema, and rejects additional tables. Declared-list UUIDs identify records in
bounded sheet scopes; they are not structured SQLite tables. The new work must
extend native validation deliberately rather than accepting arbitrary schema.
See [implementation status](implementation-status.md),
[native formats](native-format-v1.md) and
[the preceding record and job contracts](m3-m5-next-ten.md).

This batch delivers value-only structured tables, scalar column types, required
and unique constraints, record editing, indexes, restrictive foreign-key
relationships, read-only SQL and saved read-only query views. Spreadsheet cells
retain their positional references and formulas. Initial table creation copies
a selection into an independent object; it does not replace or synchronize the
source range. Existing list identities and list operations keep their meaning.

Managed SQL mutations need their own proven capture integration and remain a
later increment. Table formulas, structured spreadsheet references, editable
query results, cascading relationships, arbitrary user triggers, workbook code
authoring, custom Python functions and remote binding are later work as well.

## Delivery order

Each task includes its engine behavior and useful client/UI surface. Ship one
reviewable branch/PR per task when implementation is undertaken. Build and run
the relevant checks before moving to its dependent task; record any review fixes
against the final tested source. Publication and merge follow the authorization
in the implementation session.

| Task | Deliverable | Dependencies | Completion criterion |
| --- | --- | --- | --- |
| 1 | Contracts, Qt wireframes and executable probes | Existing engine baseline | Storage, hooks, values, SQL policy and snapshot decisions have testable contracts |
| 2 | Native table storage and managed operation foundation | 1 | Create, inspect and reopen an empty table; migration, receipts and required hooks work atomically |
| 3 | Create a table from a sheet selection | 2 | Reviewed values and types create an independent table without changing the source |
| 4 | Column types and schema constraints | 3 | Reviewed schema changes enforce types, required values and uniqueness without partial conversion |
| 5 | Record editing and history | 4 | Single and bulk edits, insert/delete, generated edits and Undo/Redo share one transaction |
| 6 | Bounded table browsing and indexes | 5 | Full-table sort/filter and stable paging work without loading the table into GUI memory |
| 7 | Relationships | 6 | A reviewed foreign key blocks invalid writes and referenced deletes; violations remain inspectable |
| 8 | Read-only SQL workbench | 7 | Parameterized joins and aggregates execute through cancellable, bounded snapshot jobs |
| 9 | Saved query views and snapshot export | 8 | Definitions survive reopen; refreshed views and CSV/XLSX/ODS exports describe the same stored result |
| 10 | Integration evidence and manual acceptance package | 9 | Automated contracts pass, limits are measured and the GUI walkthrough has fixtures and expected results |

Task 1 produces focused Qt interaction wireframes before feature code. Task 2
must establish the table hook and receipt foundation before any table write is
exposed. Subsequent tasks extend that foundation; an intermediate GUI must never
advertise an operation that bypasses required handlers.

## Contracts for the first tier

These are the implementation defaults for this batch. Task 1 resolves any
contradiction found by its executable probes and updates the affected documents
before a dependent feature is implemented. A failed experiment must narrow the
supported surface explicitly rather than introduce a successful-looking fallback.

### Native storage and identity

Introduce native format v4. Use workbook-level catalogs for tables, columns,
indexes, relationships and saved queries; do not attach them to the sheet-scoped
`dh_objects` lifecycle. Table and column UUIDs survive presentation changes.
Registered user tables contain ordinary SQLite columns and native constraints.
The catalog records the mapping from stable IDs to quoted SQL identifiers.

Use `_dh_row_id INTEGER PRIMARY KEY AUTOINCREMENT` as the reserved record identity.
It is separate from user keys and display order, immutable through ordinary
managed edits, and generated by SQLite for independent inserts without Python
callbacks. API row IDs are decimal strings so 64-bit identities remain exact.
Undo can restore a deleted record with its original identity; an ordinary new
record never silently takes a deleted identity. External identity changes are
direct-access changes and invalidate outstanding managed state and history.

Table and column SQL names initially use 1–64 ASCII letters/digits/underscores,
beginning with a letter, with case-insensitive uniqueness in their scope.
Reserved native names and the `dh_`, `_dh_` and `sqlite_` prefixes are unavailable.
Always quote identifiers and bind values. Reject duplicate/invalid headers with
an editable mapping preview; do not silently rename them.

The first relational write upgrades v1/v2/v3 transactionally to v4 together with
its requested change and receipt. Read-only opening never upgrades. A failed
operation restores the previous schema and format version. Older services reject
v4; native copies preserve it. Offer the existing native snapshot command before
the upgrade review; preserve the current file through the transaction contract.

The v4 validator admits catalog-registered tables, constraints and indexes whose
schema matches supported engine-generated forms alongside the exact reserved
schema. It rejects unknown objects, unregistered triggers, virtual tables and
unsupported schema changes.
Include SQLite's AUTOINCREMENT bookkeeping in validation without exposing it as
user data. Stream schema/data validation within the existing five-second opening
budget, with actionable failure rather than repair or deadline relaxation.
Snapshot validation and read-only recovery inspection must understand v4 too.

### Values and schema changes

Initial columns support Text, Integer, Number, Boolean, Date, Time and Datetime.
Integer values use the existing exact JSON interoperability range; Number uses
finite binary64. Temporal values follow the existing canonical civil ISO tier.
Keep SQL storage representations, semantic column types and display formats
distinct. Formula-looking Text remains literal in a table.

Every column can be nullable or required. Empty Text differs from NULL. Omitted
insert fields become NULL; omitted update fields remain unchanged. A unique
column follows SQLite's NULL semantics: multiple NULLs are allowed unless the
column is also required. Initial Text key/comparison collation is SQLite BINARY,
with case-sensitive value equality; case-insensitive catalog names do not change
record value equality. User defaults and arbitrary CHECK expressions are
outside this tier. Enforce representable storage/type rules with SQLite-native
constraints and validate canonical semantic values in the engine; no stored
constraint depends on a registered Python function. Document what an independent
connection can enforce and what managed validation additionally checks.

Schema review supports adding nullable columns, renaming tables/columns,
changing a column's type/required/unique settings and explicit deletion.
Conversion requires a selected deterministic policy. Reject precision loss,
invalid dates, duplicates or missing required values with counts and locations.
Schema rebuilds preserve record/column IDs, data, indexes and relationship
definitions and commit as one operation. Block rename/drop/type changes that
would invalidate a relationship or saved query until the dependency is explicitly
resolved. Do not rewrite SQL text heuristically. A committed schema change clears
session Undo/Redo with a review warning; a rejected change retains history.

### Managed writes and Python hooks

All table/schema/relationship mutations enter the existing engine transaction
owner, require an operation ID and expected state, deduplicate receipts and
preserve known/unknown outcome handling. Refresh under the writer lock before
checking state. Known-target operations collect typed old/new row images
explicitly. Restrictive relationships and the prohibition on arbitrary triggers
keep the first write surface free of untracked cascade/trigger effects.

Add explicit `on_table_change(context, changes)` and
`on_table_structure(context, event)` opt-ins to the existing trusted-handler
mechanism. An activated handler lacking the applicable entry point rejects the
operation; a sheet-only handler never implicitly authorizes table changes.
Read-only table/query requests do not dispatch write hooks. Source/target object
IDs, row IDs, column IDs, operation origin and before/after values are explicit.

Extend the transaction broker with bounded table reads and record mutations.
Hook-generated table and cell edits join the parent transaction. Process cell
and table changes in a documented deterministic order, with generated edits in
subsequent rounds and shared work/recursion/time limits. Coalesce unchanged
assignments without change events. A handler failure, bad broker message or
budget overrun rolls back data, schema, generated edits and history together.
Activation remains service-operator controlled; importing a table or query
definition cannot activate code. After-commit external effects remain disabled.

Extend shared history with typed cell and table-record deltas. Supported record
Undo/Redo is a new managed operation, runs applicable hooks and can be rejected
atomically. One bulk operation has one history entry, including generated edits
in other tables or sheets. No-op operations preserve Redo. Schema changes and
external modifications invalidate history explicitly.

### Relationships and direct access

The initial relationship is an optional/required child column referencing one
required unique parent column of the same semantic type. Use a native SQLite
foreign key with RESTRICT for delete/update. Composite keys, cascades and
deferred relationships are later increments. Nullable child keys may be NULL.
Create required supporting indexes and retain their catalog provenance so an
index needed by a constraint cannot be dropped independently.

Before adding a relationship, review all existing unmatched non-NULL child keys
and reject Apply until they are resolved. Enable foreign keys on every managed
connection. Independent SQLite connections choose their own enforcement
settings; a relationship declaration does not promise enforcement on connections
that disable it. Orphans introduced directly produce inspection details and
block managed mutations until an explicit, reviewed repair operation succeeds.
With a valid supported schema but invalid stored types/relationships, offer an
explicit restricted repair handle through the shared REST interface. It permits
bounded raw inspection and reviewed data repairs under the same hooks/receipt
contract, with ordinary writes disabled. Do not require a successful normal open
as a prerequisite for repairing a data violation. Corrupt catalogs or unsupported
external schema changes require compatible reopen/recovery rather than this
data-repair path. Code activation keeps its existing explicit operator policy.

External DML refresh rotates managed state, invalidates pending pages/query
results and history, and revalidates stored types/relationships within declared
budgets. Reopen follows the same validation policy. Never replay skipped change
handlers. Document the physical table names, columns, identity and constraints
so ordinary SQLite tools can read/write data without registering callbacks.

### Table browsing and GUI behavior

Add a workbook object browser with Sheets, Tables and Queries. Opening an object
selects a central grid appropriate to that object; the sheet formula/address
controls keep their existing meaning. Table cells use column-type delegates,
record selection and a separate append action. Query grids are read-only.
Use Qt Widgets/C++, asynchronous REST transport and GUI-thread-only model updates.

All table reads include workbook state, table/schema identity and requested
columns. Sorting/filtering applies to the complete table. Use deterministic
ordering with row identity as the final tie-breaker and cursors tied to state,
criteria and schema. NULL ordering is explicit. A stale cursor requires refresh;
it never resumes at an unrelated offset. A selected row remains identified by
its record ID when its displayed position changes. Reviewed bulk targets do not
change when a filter changes or a row disappears after editing.

Keep drafts through rejection, protect reviewed targets through modal dialogs,
and discard late replies from another object/state. Show loading, empty,
read-only, busy, stale, rejected and unknown-outcome states. Expensive counts can
be pending or unavailable; do not substitute the loaded-row count for the total.
Provide named controls, keyboard actions and bounded accessibility enumeration.

### SQL execution and saved queries

The public SQL surface accepts one read-only SELECT, including supported CTEs,
joins and aggregates, with named bound scalar parameters. Execute on a private
read-only query connection against a consistent stored snapshot. Combine
SQLite preparation/authorization, read-only connection settings and an explicit
function allowlist. Do not authorize by keyword matching. Deny DML, DDL,
transaction/savepoint control, PRAGMA, attachments, extensions, reserved metadata,
internal bookkeeping and unregistered objects/functions, including indirect
access through table-valued functions or nested queries.

Reuse the job lifecycle and consistent native-copy machinery for query snapshots
and disk-backed result spooling, extending validation to v4. The query worker
must not hold the live workbook connection/lock for its execution duration.
Snapshot creation keeps its existing five-second budget; report its disk cost
and failure separately from query execution. No full snapshot is taken for an
ordinary table viewport read. Cancellation/disconnect/close release worker,
snapshot and spool resources through explicit terminal-state handling.
Pin snapshot creation to one admitted read state, verify file identity and the
expected state before admission/publication, and mark a later source change
stale. Task 1 must prove this behavior with concurrent independent writers.

Only a completed result can be paged or exported. A single execution's ordinal
pages come from its frozen spool, not repeated LIMIT/OFFSET queries. Honor SQL
ORDER BY; without it, document that order may differ across executions. Bind jobs
to source state and execution generation. A later run or external/managed change
marks old output stale and prevents it from being presented/exported as current.
Query results never become record-edit targets.

Preserve result column order, duplicate-label distinctions and per-value SQLite
types. Expressions do not acquire date/Boolean semantics from their aliases.
Represent integers outside the JSON exact range losslessly with tagged decimal
strings; unsupported result types fail clearly. Apply the existing interchange
rules for exact export representation, including Text fallback review when
needed. Parameters are data, never SQL interpolation.

Saved queries store a stable ID, name, SQL, parameter declarations, referenced
object IDs and expected result shape in the native catalog. Saved defaults are
explicit ordinary workbook data; invocation-only parameter values are not
silently persisted. Refresh rechecks authorization, dependencies and shape.
Opening/importing a definition does not execute it automatically. Dependencies
are discovered through SQLite preparation and catalog mapping, including CTEs
and SELECT *, rather than regex rewriting. A view result has no implied stable
record identity. Materializing a query into an editable table is later work.

### Export and existing workbook operations

Native Save/copy/recovery retains all table data, keys, relationships and query
definitions. Existing sheet imports/exports must recognize v4 features as soon
as v4 is introduced. A spreadsheet conversion review reports native-only or
omitted content with actual object names; a whole-workbook import cannot
silently replace relational objects. Existing empty-destination import checks
must include tables and queries when deciding whether a workbook is empty.

Task 9 exports table or completed query results as a consistent values snapshot
to CSV/XLSX/ODS using the protected export writer and native-file alias checks.
Report type transformations and the absence of native relationships, keys,
indexes and query definitions in the target tier. Review destination size limits
before publication; never export only the current page or truncate silently.

## Proposed resource and code boundaries

Resource names are frozen in task 1 alongside OpenAPI examples. The initial
shape is `/v1/workbooks/{handle}/tables`, table schema/read resources,
`/relationships`, `/queries` and `/query-jobs`. Mutations use the existing
managed-operation envelope and receipt lookup; avoid an independent transaction
path for each resource family. Provide matching managed CLI commands and Python
SDK methods with structured results/errors and existing pending-write guards.

| Area | Likely implementation boundary |
| --- | --- |
| Native catalog and migration | New relational schema/catalog module, integrated with `engine.py` validation, refresh, snapshots and recovery |
| Record/schema operations | Shared table-operation module called by the existing transaction owner |
| Hooks and history | `hooks.py`, transaction broker, and a tagged history representation shared with current cell operations |
| Browsing | Table read/filter/cursor module with indexed snapshot reads |
| Relationships | Catalog/DDL planner plus constraint and orphan inspection |
| Queries | Separate authorization/execution module and bounded snapshot/result job manager |
| REST and clients | `service.py`, OpenAPI definitions, `client.py` and `cli.py` |
| Qt desktop | Object browser, table model/delegates and query dock/result model integrated with the existing window and REST client |
| Interchange and packaging | Existing exporters, protected writer, installed runtime resources and source/artifact inventories |

Keep substantial new behavior in focused modules rather than adding all of it
to `mainwindow.cpp` or the existing engine dispatch. Reuse the current transaction
and client lifecycle; refactor only the portions required by the new contracts.

## Implementation tasks and verification

### Task 1 Contracts and wireframes

Write the v4 schema proposal, operation/event/value schemas, unsupported cases
and focused Qt wireframes for the object browser, table creation/schema review,
record editing, relationships, SQL parameters/results and saved views. Annotate
keyboard order and all draft/error/read-only/loading states.

Add executable probes for transactional DDL/rebuild rollback, AUTOINCREMENT
identity and Undo restoration, native constraints without callbacks, foreign-key
RESTRICT and query authorization/CTE/dependency capture. Prove that snapshot
execution cannot mutate the source or read reserved metadata. Specify how query
jobs reuse existing snapshot/resource ownership before expanding those classes.

Complete when these probes pass on the supported development runtime, decisions
are written into the relevant design contracts, and the fixture/acceptance matrix
maps each promised operation to a test. No mock UI control claims functionality.

### Task 2 Native storage and managed operation foundation

Implement v4 catalogs, exact registered-schema validation, transactional upgrade,
empty table create/inspect/delete, shared mutation dispatch, required structural
hook opt-in and the broker/change-history foundations needed by subsequent writes.
Add REST/SDK/CLI operations and an object browser with an empty-table creation
dialog. Update capabilities, recovery, native copy and conversion reporting.

Verify v1/v2/v3 migration and rollback, read-only/unknown-version behavior,
catalog corruption, reserved-name collisions, unrelated external tables/triggers,
independent SQLite inserts and snapshot reopen. Inject failure between DDL,
metadata and receipt stages. Test lost acknowledgement and deduplication.

Complete when no intermediate schema escapes a rejected operation, failed
required hooks preserve the original workbook, and installed clients can create,
inspect, copy and reopen the same empty table through REST.

### Task 3 Create a table from a selection

Implement a state-bound preview for the explicit source rectangle and header
choice, editable header mapping, column types and conversion errors. Evaluate
selected formulas against one current snapshot and clock; copy reviewed values
only, reporting formula/style/identity omissions. Pending/error/unrepresentable
values block Apply until corrected. Default to a lossless type mapping or an
explicit Text choice rather than guessing locale or identifier intent.

Create catalog, rows, constraints and generated hook changes in one operation.
Extend table-change events/broker access before exposing populated creation.
The source sheet and existing declared-list identities remain independent.

Verify leading-zero IDs, empty Text/NULL/blank mapping, Booleans, temporal values,
formula errors, invalid/duplicate headers, stale previews, cancellation, read-only
files, source preservation and late GUI replies. A handler rejection must leave
neither a partial table nor generated edits.

### Task 4 Column types and schema constraints

Implement schema inspection and the reviewed changes defined above. Preflight
existing rows with bounded streaming validation and exact conversion reports.
Apply native constraints and any required rebuild atomically, preserving stable
IDs and protecting reserved identity fields. Show structural history clearing.

Verify NULL/empty Text, integer boundaries, finite numbers, canonical temporal
values, required/unique failures, duplicates under the selected collation,
rename identity, index preservation and failed rebuild rollback. Independent
SQLite tests exercise both enforced native rules and managed semantic checks.
Oversized rebuilds reject before admitting a partial change.

### Task 5 Record editing and history

Implement typed single-cell editing, row append/insert/delete and reviewed bulk
paste/clear over explicit record/column IDs. Row position is presentation only.
Support transaction-aware table/cell hooks and one Undo/Redo entry per supported
data operation. Schema commands retain their explicit structural history rule.

Verify equivalent GUI/REST/CLI/SDK operations, generated edits across objects,
hook recursion/failure/timeout, atomic invalid paste, insert/delete restoration,
no-op Redo retention, rejected Undo, unknown acknowledgements and external-edit
stale rejection. Table formula-looking inputs must remain literal Text.

### Task 6 Bounded browsing and indexes

Implement full-table multikey sort/filter, keyset paging, count status, stable
record selection and bounded Qt caches. Provide index create/list/drop review,
including columns, order, uniqueness, constraint ownership and dependencies.
Index creation is a cancellable atomic schema operation within the same budget.

Verify duplicate sort keys, NULLs, page boundaries in both directions, no-match
states, edits that remove their own row from the filter, stale/forged cursors,
unloaded selected targets and index failures. Measure full-scope correctness,
request sizes, cache estimates, peak RSS and input responsiveness. A timed-out
count or index build must have an explicit result and leave usable prior state.

### Task 7 Relationships

Implement relationship creation/removal, parent-key selection, orphan review,
constraint-owned indexes and atomic DDL/rebuild. Provide a read-only violation
inspector plus narrowly scoped, reviewed repair operations for direct-edit
orphans. Successful repair validates the complete affected relationship state
before commit; it cannot authorize an unrelated write that leaves violations.

Verify valid/NULL/unknown child keys, referenced parent delete/key update,
required child columns, incompatible types, relationship removal, dependency
blocking, handler failure and external foreign-key settings. Show that SQL joins
will use stored key equality and the declared collation. No cascade effect may
be omitted from change capture; this tier rejects cascade declarations.

### Task 8 Read-only SQL workbench

Implement the SQL editor, schema browser, typed named parameter controls,
Run/Cancel/status, paged results and private snapshot jobs. Expose REST/CLI/SDK
submission, status, paging, cancel and release with documented cleanup/restart
semantics. Results are ephemeral; service restart requires rerun.

Verify joins, aggregates, CTEs, bound parameters, SQL NULL, duplicate output names,
exact large integers and SQLite errors. Exercise denial through comments,
CTE-prefixed writes, nested access, functions, table-valued pragmas, attachments,
multiple statements and reserved objects. Test pathological joins/recursive CTEs,
spool limits, disk-full, snapshot timeout, cancellation, close and generation
races. A failed run publishes no incomplete result as a successful query.

### Task 9 Saved views and export

Implement query save/rename/delete, dependency and result-shape validation,
reopen without automatic execution, explicit parameter entry and Refresh. Add
table/query values export to CSV/XLSX/ODS from the complete frozen scope.

Verify rename/drop blockers, SELECT * schema dependencies, stale results,
concurrent edits, expired jobs, parameter persistence rules, native copy/reopen,
destination aliases and all-or-nothing protected publication. Independent
XLSX/ODS consumers verify typed outputs, row order, full result counts and the
reported transformations. Exports retain native source definitions unchanged.

### Task 10 Integrated verification and handoff

Run the full Python/Qt suites and new managed-path conformance matrix. Run Cocoa
automated checks at scales 1/2, inspect screenshots, and exercise the relocated
installed GUI/CLI/SDK/service with bundled Python. Keep optional dependency
checks and their coverage explicit. Compare results with the task 1 baseline.

Measure sparse sheets and relational tables together, indexed/unindexed reads,
data larger than the declared service memory budget, table validation/opening,
snapshot disk overhead, full-result paging and query cancellation. Record
fresh-process/warm provenance;
true OS-cold evidence retains its separate administrator-dependent gate.

Deliver reproducible Customers/Orders fixtures, an activated test-handler
fixture, direct-edit scripts, an automated expected-results check and the manual
GUI guide below. Tie evidence to exact source and installed artifact hashes.
Update implementation status, compatibility/native schema/API docs and the
per-task evidence ledger. Automated completion and manual acceptance have
separate statuses; pending manual work does not become an automated pass.

## Initial budgets and failure behavior

These are proposed acceptance ceilings, not measured capacity claims. Keep the
existing budgets unless task 1 explicitly documents a justified narrower tier.
Capacity exhaustion returns a structured error and does not truncate or split
an atomic commit.

| Surface | Initial bound |
| --- | --- |
| Catalog | 64 user tables, 256 columns per table, 64 saved queries; bounded catalog response/payloads |
| Ordinary writes | 10,000 affected scalar fields across requested and generated changes; 4 MiB request, 8 MiB before/after payload, five-second transaction |
| Inserts/deletes | Charge complete before/after row payloads; also cap affected records at 10,000, including rows with few fields |
| Schema changes and promotion | Same ordinary write/history payload and five-second budget; a rebuild touching more rows/fields rejects until an explicit large-schema job tier exists |
| Hooks/history | Existing two-second worker, five-second operation, 16 rounds, 1-MiB broker frame and 100-entry/16-MiB history limits shared across cell/table work |
| Table page | At most 256 rows and 4,096 requested values, 4 MiB encoded response, two-second read; explicit projected columns for wide tables |
| GUI | Retain existing 16-MiB estimated cache and 256-MiB RSS targets; table/query caches count toward the same total |
| Service memory | Retain the 256-MiB target on declared workloads; report engine/query-worker peaks separately and combined, including job spooling and concurrent work |
| Query request | 64 KiB SQL; at most 64 declared parameters within the existing 4-MiB request ceiling |
| Query job | One active execution and four retained query jobs per workbook; five-second source snapshot, 60-second execution, 64-MiB result spool, 100,000 rows, 1,000,000 values and 256 result columns |
| Query page | At most 256 rows, 4,096 values and 4 MiB encoded response; column projection does not change the frozen result |
| Temporary storage | Account for each full native snapshot, its temporary SQLite files and result spool; do not describe the 64-MiB spool cap as a cap on total job disk use |
| Responsiveness | Preserve warm p95 viewport/jump <150 ms, edit without worker <100 ms / with worker <500 ms, input stalls <50 ms, confirmed cancellation <3 seconds on recorded workloads |

Use instruction/deadline checks inside SQL and cooperative cancellation between
bounded fetches. Include serialization and publication in response/result
budgets. Rollback and durable receipt recording must remain possible after
cancellation or timeout. Cleanup errors cannot change a confirmed commit into
rollback. Disk/permission failure preserves the source and reports any retained
private artifact for recovery. Enforce combined resource admission when query
jobs coexist with existing preparation/calculation/operation jobs; reject
exhaustion rather than multiplying unmeasured memory or disk use.

## GUI acceptance walkthrough for the owner

Task 10 supplies clean native and tabular fixtures plus a separate runnable
handler/direct-edit setup. The following is the expected manual sequence.

1. Open the installed app and create a workbook. Enter/import the two source
   selections below. Create Customers and Orders tables through the reviewed
   selection command. Use Text for IDs/names, Integer for Quantity and Number
   for UnitPrice. Confirm their headers/types and that source cells remain.
2. Open the tables from the object browser. Navigate and edit using keyboard,
   cancel a draft, switch objects and verify source/formula controls do not
   retarget a table edit. Inspect empty, loading and read-only states.
3. Make Customers.CustomerID and Orders.OrderID required/unique, and make
   Orders.CustomerID required without uniqueness. Attempt duplicate IDs and an
   invalid Integer quantity, including one invalid row in a bulk paste. Confirm
   the entire rejected operation leaves data unchanged.
4. Sort/filter Orders and edit a visible record. Confirm identity follows the
   record and that its disappearance from the filter is explained. Undo/Redo
   restores the complete supported data operation. Finish with Undo so the
   original fixture values are restored for the query checks.
5. Define Orders.CustomerID → Customers.CustomerID. Attempt C999 in an order and
   deletion of C001 from Customers. Both reject without partial changes.
6. Run the joined query below with named parameter `minimum_total=0`. Confirm
   C001/Ada has two orders totaling 35 and C002/Grace has one totaling 21.
   Set the parameter to 30 and confirm only Ada remains.
7. Save the query as Customer totals. Change O100 Quantity from 2 to 4. Confirm
   prior output becomes stale; Refresh with minimum_total=0 gives 60 and 21.
   Undo the quantity change and refresh; the totals return to 35 and 21.
8. Export the complete query to CSV/XLSX/ODS, review native-only omissions, open
   the exports independently and confirm the two records and total 56. Verify
   an overwrite/cancel flow and rejection of the native workbook as destination.
9. Close/reopen and Save a copy. Confirm tables, identities, constraints,
   relationship and saved query persist; the query runs only when requested.
10. Use the supplied trusted-handler setup. Confirm a rejected Quantity <= 0
    leaves both table data and its generated fixture-cell edit unchanged; a
    valid edit and its generated change commit and Undo together. The rule
    belongs to the fixture's Python code, not built-in Orders behavior.
11. Use the supplied independent SQLite script, then refresh. Confirm changed
    stored data appears without replaying handlers, pending results are stale,
    and a deliberately introduced orphan can be inspected and explicitly repaired.
12. Cancel a supplied slow query, close with work active, and exercise native
    dialogs/clipboard, narrow windows, scale changes and VoiceOver reading of
    table headers, cells, result/error/status text and parameter controls. Record
    observations and failures separately from automated Qt results.

Customers source:

| CustomerID | Name |
| --- | --- |
| C001 | Ada |
| C002 | Grace |

Orders source:

| OrderID | CustomerID | Quantity | UnitPrice |
| --- | --- | --- | --- |
| O100 | C001 | 2 | 12.5 |
| O101 | C001 | 1 | 10 |
| O102 | C002 | 3 | 7 |

```sql
SELECT c.CustomerID, c.Name,
       COUNT(o.OrderID) AS OrderCount,
       COALESCE(SUM(o.Quantity * o.UnitPrice), 0) AS Total
FROM Customers AS c
LEFT JOIN Orders AS o ON o.CustomerID = c.CustomerID
GROUP BY c.CustomerID, c.Name
HAVING COALESCE(SUM(o.Quantity * o.UnitPrice), 0) >= :minimum_total
ORDER BY c.CustomerID;
```

## Completion and evidence ledger

The batch is implemented when all ten tasks meet their automated criteria and
the installed artifact performs the complete example through shared managed
operations. Record manual GUI/accessibility results later against the same
source/artifact or rerun after fixes. Passing this batch establishes the bounded
relational tier; full M8 and cross-platform release acceptance retain their
separate contracts.

| Task | Implementation | Automated evidence | Owner GUI acceptance |
| --- | --- | --- | --- |
| 1 | Contracts, [Qt wireframes](relational-workbench-wireframes.md) and SQLite probes implemented; [PR 115](https://github.com/hybridmachine/DataHorizon/pull/115) | [Five isolated SQLite contract probes pass](evidence/relational-task1.json) | Wireframes supplied before feature code; physical acceptance later |
| 2 | Native v4 empty-table storage, managed hooks, catalog, SDK/CLI and Qt shell implemented | 17 targeted Python checks and the new Qt integration check pass; full-suite result recorded separately | Pending |
| 3 | Planned | Pending | Pending |
| 4 | Planned | Pending | Pending |
| 5 | Planned | Pending | Pending |
| 6 | Planned | Pending | Pending |
| 7 | Planned | Pending | Pending |
| 8 | Planned | Pending | Pending |
| 9 | Planned | Pending | Pending |
| 10 | Planned | Pending | Walkthrough and fixture acceptance pending |

For each task, record the tested commit/source fingerprint, relevant commands,
pass/fail/skip counts, native/installed artifact identity, measured limits,
review fixes and unresolved acceptance items under `docs/design/evidence/`.
Do not invent completion evidence or close a manual/platform gate because a
PR merged. Scheduling estimates follow task 1's migration, hook and query probes.
