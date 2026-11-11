# Data model and calculation

Status: proposed conceptual model. Physical schema and formula semantics need
implementation experiments before they become stable contracts.

The experimental sparse-cell implementation is now described in
[native format v1](native-format-v1.md) and [compatibility tier 0](compatibility-v0.md).
Declared freeform lists now have bounded persistent record UUIDs under the
[implemented list contract](m3-m5-next-ten.md). Structured SQLite tables and
relational views are planned in the
[structured tables and query workbench batch](relational-workbench-plan.md);
their identities remain separate from list UUIDs and positional A1 references.

## Workbook objects

| Object | Meaning | Initial editing behavior |
| --- | --- | --- |
| Sheet | Freeform cells with positional references, formulas, and formatting | Editable through the grid and managed range operations |
| Table | Structured records with named columns, types, keys, and indexes | Editable through grid, SQL, and record operations |
| View | A saved relational query exposed as a grid | Read-only result; materialization creates an independent table |

A workbook contains these objects, code, relationships, and presentation
metadata. The GUI should present a consistent grid experience. The logical
distinction need not force users into separate applications or require SQL.
How tables appear within freeform sheets, and how a range becomes a table,
remain UI and schema decisions.

Views preserve their query definitions and explicit ordering. A view result
does not inherently have stable record identity or an unambiguous edit target.
Editable views and refreshable materializations need additional contracts.

## Identity and references

- Assign stable identifiers to workbooks, sheets, tables, views, and columns.
  Renaming an object should not silently break references using its identity.
- Distinguish table record keys from displayed row positions. Filtering and
  sorting change presentation without changing record identity.
- Keep freeform cell addresses and relative/absolute references meaningful.
  Stable object IDs do not replace spreadsheet reference-adjustment rules.
- Represent named ranges and table references explicitly. A positional
  reference and a lookup by record key express different intentions.
- Define insertion, deletion, copy/paste, sort, rename, and cross-sheet reference
  behavior before committing the formula representation.

External table inserts must be able to obtain valid keys through SQLite alone.
The physical key strategy must not require a Python callback. Changes to a key
or schema through independent tools may invalidate relationships or references;
direct access does not promise automatic metadata reconciliation.

## Proposed storage layout

Use SQLite tables for structured user data and sparse storage for freeform
cells. Keep application metadata in a documented reserved namespace. These are
logical categories, not final SQL table names or DDL:

| Category | Stored information |
| --- | --- |
| Workbook metadata | Format version, workbook identity, object catalog, and feature requirements |
| Sheet inputs | Cell coordinates, typed literals, formula source, and named ranges |
| User tables | Records, keys, declared columns, constraints, and indexes |
| Relationships and views | Relationship definitions, query source, parameters, and presentation |
| Presentation | Styles, dimensions, display order, filters, and number formats |
| Code | Python module source, entry points, handler bindings, and dependency declarations |
| Derived state | Cached formula results, dependency indexes, and calculation status |
| Optional operation state | Managed change records, jobs, and pending after-commit work |

Store formula source separately from cached results. A cached result is not an
authoritative replacement for its formula. Likewise, a displayed number format
does not change a stored value's type.

Ordinary data and schema access must work with standard SQLite tooling. Avoid
making persistent constraints, generated columns, or mandatory triggers depend
on application-registered Python functions. Python-computed values can be
materialized by the engine, with freshness explicitly described.

## Values and types

Define one typed value representation shared by storage, REST, Python, formulas,
and import/export. It must distinguish, where relevant:

- Absent/blank cells, database `NULL`, and empty text.
- Integers, floating-point values, booleans, and text.
- Dates, times, durations, and any timezone-aware values.
- Formula errors, pending calculations, and valid values.

Decimal precision, date serial compatibility, implicit conversion, error
propagation, binary data, and mixed-type columns remain explicit decisions.
Do not infer an identifier is numeric merely because its text contains digits.

Structured columns can introduce types and constraints progressively. Type
changes and imports should expose conversion failures and their locations;
silent coercion must not conceal loss of data. SQL, Python, and grid operations
must use the same declared application semantics on managed paths.

## Calculation model

The calculation engine needs a parser, a reference representation, dependency
tracking, invalidation, evaluation, and persisted freshness state. SQLite query
execution and spreadsheet evaluation are complementary engines; translation of
some formulas into SQL is a possible optimization, not the semantic foundation.

Proposed behavior:

1. An input change invalidates dependents, including consumers of changed tables
   or query results. Start conservatively where precise dependency tracking is
   unavailable.
2. Recalculate incrementally when dependencies are known. Large calculations
   can run as jobs with visible pending/error state and cancellation.
3. Associate results with the input state they evaluated. Never publish an old
   job's result as current after newer edits.
4. Detect cycles and report them. Iterative calculation, volatile functions,
   spill arrays, and external links require separate compatibility decisions.
5. Make custom Python worksheet functions return values without modifying the
   workbook during evaluation. See the [runtime design](python-runtime.md).

The exact relationship between transactional handlers and derived values must
be specified. A handler requiring a current result should request it explicitly;
it must not silently read a stale cache as current. If asynchronous calculation
is allowed at commit, final validation cannot assume those results already exist.

## Direct edits and format evolution

Direct SQLite writers can change inputs without updating dependency metadata or
formula results. On managed reopen/refresh, invalidate conservatively and do not
replay skipped edit handlers. Recalculation may invoke configured Python custom
functions; that is distinct from replaying the original external operation.

Version the native format. Migrations should be explicit, recoverable, and
separate from user edit events. Unknown newer formats must not be silently
rewritten. Document writable data areas and reserved metadata as the schema
stabilizes, including requirements for external inserts and deletes.

An internal managed revision can help coordinate engine clients but cannot be
treated as a complete revision of externally writable data. The initial safe
default is to treat persisted derived state as unverified on reopen. Efficient
validation and full change-history storage remain open design questions.
