# Interfaces and interoperability

Status: agreed interface goals; proposed resource model, execution details, and
compatibility stages. No endpoint names or command syntax are stable yet.

The development API, CLI and Python client are now executable. `/openapi.json`
and `/docs` expose the implemented localhost resources. [Development instructions](development.md)
and [compatibility tier 0](compatibility-v0.md) describe actual scope; the remainder
of this document retains the wider target design.

## Desktop experience

Use Qt Widgets with C++ for the native desktop application on Windows, macOS,
and Linux. License the GUI under GPLv3. Provide familiar grid navigation, editing,
copy/paste, formula entry, and formatting. The grid fetches bounded viewports
instead of loading the entire workbook into memory.

Tables and views should expose filters, sorting, column information, and query
results through the same interaction vocabulary. Offer SQL and Python tools
without requiring either language for ordinary spreadsheet operations. Detailed
grid design and native integration remain open within the selected Qt Widgets
stack. Licensing for components outside the GUI is a separate decision.

Create UI wireframes from the everyday spreadsheet user stories before writing
application code. Start with the workbook window, grid and formula editing,
sheet navigation, and the visible states for pending work and errors. Refine
these interactions before creating executable UI prototypes.

Show calculation freshness, background progress, validation failures, and macro
errors where users can act on them. A successful data commit followed by a
failed after-commit handler must not be presented as an uncommitted edit.

All normal GUI operations use REST. A local service can be managed by the
desktop application's lifecycle; the service also runs independently for
headless or remote use.

## REST API

Publish OpenAPI documentation with an interactive Swagger-style explorer.
Version the public API independently of the native workbook format.

Proposed resource families are:

| Resource family | Operations |
| --- | --- |
| Workbooks | Open/register, inspect, close, and create consistent snapshots |
| Sheets and ranges | Bounded reads, cell/range edits, structure, and formatting |
| Tables | Schema inspection, record reads, bulk edits, indexes, and relationships |
| Views and SQL | Saved queries, parameterized execution, result cursors, and supported mutations |
| Code and macros | Inspect modules/bindings and invoke permitted entry points |
| Calculations | Request evaluation and inspect pending/error/current state |
| Jobs | Submit, inspect progress/results, cancel, and inspect failures |
| Charts | List, suggest, create, change and delete charts; resolve and render them as jobs |

Design requirements:

- Bulk operations and bounded/streamed results, with typed values that preserve
  blanks, errors, dates, and numeric choices without ambiguous JSON coercion.
- Structured errors with stable codes, relevant object/range information,
  operation IDs, and an explicit committed/rolled-back/pending outcome.
- Defined ordering and consistency for pagination, including stale cursors.
- Expected-version or equivalent conflict handling for edits. Account for
  external writers rather than trusting only managed revision counters.
- Retry semantics for mutations. If idempotency keys are supported, deduplicate
  the whole operation so a network retry does not repeat its macros.
- Jobs for expensive work, with documented cancellation and transaction units.
- Discoverable capabilities and compatibility information for scripts and agents.

As built in charts stage 2 (range sources, branch `claude/charts-stage-2`, not
merged): chart writes are the operation actions `chart_create`, `chart_update`
and `chart_delete` on the existing operations route, with undo and redo. Reads
are `POST /v1/workbooks/{workbook}/charts` (the state-checked catalog) and
`POST .../charts/suggest` (a specification suggested from a range). Resolve and
render are read-only jobs: start with `POST .../charts/jobs`, poll
`GET .../charts/jobs/{job}`, and finish with `.../cancel` or `.../release`. A
render returns SVG and one mark rectangle per drawn value. A job's result is
withheld as `stale` once the workbook changes, and every limit is refused with
a code that names it. The SDK and the managed CLI `charts` command wrap the
same routes. See [the charts design](charts-design.md#8-managed-operations-and-interfaces).

Table and saved-query sources (branch `claude/charts-table-query-sources`, not
merged) use the same routes. A source in a request is one of three input forms
(`ChartSource` in OpenAPI): `{"kind": "range", "sheet", "ref"}`,
`{"kind": "table", "table", "column"}` or `{"kind": "query", "query",
"column"}`, with a table, column or saved query given by name or identity and
a query column by its result name. The catalog returns each source's stored
identities with current names and `broken`. `POST .../charts/suggest` also
takes `{expected_state, table, type?}` (not combined with a rectangle); the SDK
has `suggest_chart(table=...)` and the CLI `charts suggest --table NAME`.
Receipts of `table_drop`, `table_schema` and `query_delete` list the charts
whose sources they broke in `broken_charts`. A saved query a chart reads runs
inside the chart job, not as a query job, so it is not visible on the query
job routes. The query engine's error codes pass through in the chart job's
error, except that a timeout becomes `CALCULATION_FAILED` and a result too long
for a series `LIMIT_POINTS`.

Server-local paths and remote client paths are different. Remote open/import
operations need a defined upload or registered-workbook mechanism; a client's
filesystem path must not be interpreted as an arbitrary server file request.

## CLI and Python SDK

| Mode | Behavior |
| --- | --- |
| Managed local file | Start or attach to a local service for the selected SQLite workbook; normal Python hooks apply |
| Managed remote | Connect to a configured REST service; the same hooks run on that service |
| Explicit direct SQLite | Use an independent SQLite connection; no DataHorizon macro execution or application recalculation |

Managed access is the default CLI behavior. Direct mode is an explicit choice
and reports its execution mode. Exact command names remain open. Provide
structured output, useful exit codes, batch inputs, and noninteractive operation.

The external Python SDK wraps REST and shares the public operation and value
model. Embedded workbook code uses a transaction-aware engine context. Both
should expose similar concepts without pretending a remote HTTP request can
join an arbitrary in-process transaction.

## SQL

Support SQL over user data, including queries, joins, views, indexes, and managed
mutations as their semantics are implemented. This is a full-SQL product goal,
not a commitment to unrestricted connection control through one endpoint.

The service owns transactions and application metadata. Define policies for
DDL, pragmas, attached databases, extensions, and custom SQL functions. Use
SQLite-aware preparation/authorization facilities rather than relying on SQL
keyword matching. SQLite's
[authorizer API](https://www.sqlite.org/c3ref/set_authorizer.html) is one candidate
for enforcing operation boundaries; it does not replace Python change capture.

All accepted managed mutations must participate in applicable handlers. An
unsupported SQL capability returns a clear error; direct mode remains a separate
way to use SQLite independently. See [SQL integration](architecture.md#sql-integration).

## Local and network access

Bind to localhost by default and authenticate local clients with a service-local
credential or equivalent mechanism; loopback reachability alone is not identity.
The local credential and service discovery details remain open.

External binding is opt-in. Support TLS, client certificates through mTLS, and
username/password authentication. Define how these methods combine, how actors
are identified, and who can execute or modify workbook code before remote write
access ships. Keep credentials and trust decisions outside portable workbook
metadata. Remote clients execute applicable macros on the server, never by
requiring a desktop session to be open.

## File formats

The native format is SQLite with documented DataHorizon metadata. Excel and
OpenDocument are interchange formats with explicit compatibility reports.
CSV is useful for tabular interchange, with delimiter, encoding, type inference,
and conversion behavior made explicit.

Proposed export policy:

| Native feature | Interchange representation |
| --- | --- |
| Literal values | Corresponding cells where representable |
| Supported formulas and formatting | Translate within the declared compatibility tier |
| Table | Export records as a sheet/table representation supported by the target |
| Query or joined view | Export a consistent result snapshot; native query definition remains in the source workbook |
| Relationships and indexes | Retain natively; report any target representation or omission |
| Python modules and event handlers | Retain natively; report the absence of an equivalent executable export |
| Unsupported formula or feature | Preserve only where a defined round-trip mechanism exists; otherwise require an explicit fallback and report the loss |

Track import and export support independently for values, formulas, formatting,
charts, names, tables, and advanced features. Excel function compatibility does
not imply automatic compatibility with every workbook feature or VBA program.
Foreign macros are not translated into Python automatically.

Imports should stage and report conversions before managed changes are applied.
Imported code must not activate itself. Export from a consistent snapshot and
describe whether calculated values are current. An Excel worksheet is limited
to 1,048,576 rows by 16,384 columns, so oversized exports need explicit selection,
aggregation, splitting, or another format. See [Microsoft's specifications](https://support.microsoft.com/en-us/excel/excel-specifications-and-limits?content_language=English).

Do not silently truncate records or substitute stale cached values for
unsupported calculations. The precise fidelity tiers and fallback choices are
open decisions to be backed by representative round-trip fixtures.

As built in Excel fidelity increment 1 (November 11, 2026): XLSX and ODS
imports degrade instead of blocking, and every item kept in another form,
approximated or left out, including each worksheet and workbook feature that is
not imported, appears as a structured report entry before Apply. Cached formula
values are used only when the user chooses `values` or `mixed` mode, and the
report says the cache's freshness cannot be verified. Whole workbooks of up to
1,000,000 cells travel by streamed upload and download with a staged review;
see [the compatibility contract](compatibility-v0.md#xlsx-and-ods-input-tier) and
[the specification coverage](excel-spec-coverage.md).
