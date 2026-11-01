# DataHorizon

## Product direction

DataHorizon is a native, cross-platform spreadsheet application that combines
SQLite storage with Python 3 programmability. It runs on Windows, macOS, and
Linux. A familiar spreadsheet interface is the primary way users work with data;
SQL, Python, a CLI, and REST APIs provide additional ways to interact with it.

People already use spreadsheets as ad hoc databases and build business processes
around them. DataHorizon supports that progression with reliable storage,
relational capabilities, and consistent application behavior. Large datasets are
an important capability, alongside correctness, interoperability, and automation.

Keep the application generic. Users implement workflows, approvals, assignments,
and other business behavior in their own Python code or external applications.
Provide general data operations and programming hooks rather than built-in
business workflows.

## GUI technology and license

Use Qt Widgets with C++ for the desktop GUI on Windows, macOS, and Linux.
Develop wireframes and interaction designs around the selected Qt Widgets stack.

The GUI will be licensed under the GNU General Public License version 3
(GPLv3). This decision applies to the GUI; licenses for the service, SDK, and
other components have not been selected.

## Current work phase

The project is in the design phase. Create UI wireframes based on the everyday
spreadsheet user stories before building application code. Keep current work
focused on documentation, user stories, wireframes, and interaction design.
Do not create application code, scaffolding, or executable prototypes yet.

## Architecture and access contract

The GUI, managed CLI, Python client SDK, and external integrations use the REST
interface. A shared execution engine implements data operations, calculations,
and the applicable user-defined Python hooks. The Python runtime belongs to the
service so behavior is consistent with or without a GUI.

There are two intentional access paths:

| Path | Contract |
| --- | --- |
| Through DataHorizon | GUI, REST, managed CLI, and SDK operations participate in application behavior and applicable Python hooks. SQL submitted through DataHorizon follows this contract too. |
| Direct SQLite access | External tools operate on stored data without running DataHorizon's Python macros, event handlers, or recalculation engine. This is analogous to editing an Excel file outside Excel. |

Direct SQLite access is an intentional capability. It does not promise the same
validation, calculation freshness, history, or automation as managed access.
SQLite-native constraints are distinct from Python rules and depend on the
schema and the external connection's settings.

Do not retrospectively replay skipped change handlers when externally edited
data is reopened or refreshed. Refresh stored data and invalidate derived state
as needed; ordinary configured open events remain separate from historical
change events.

## Storage and spreadsheet behavior

- Store native workbooks in SQLite, with documented, versioned metadata.
- Support familiar spreadsheet editing, formulas, and formatting. Excel function
  compatibility is a goal developed in explicit, tested stages.
- Support structured tables, relationships, joins, saved queries, and views.
  The detailed mapping between these and freeform sheets is a design proposal.
- Support SQLite SQL over user data through the application, including managed
  writes. Internal metadata and transaction ownership must remain coherent.
- Design for datasets larger than memory through bounded reads, a virtualized
  grid, indexes, and background work. Establish measured limits rather than
  claiming unlimited size.
- Keep ordinary direct database access independent of registering Python
  callbacks with SQLite.

## Python programmability

Python serves the role that macros and embedded code serve in other spreadsheet
applications. Support workbook code, explicitly invoked macros, event handlers,
custom functions, and an SDK for external programs.

Applicable hooks must run consistently across managed access paths. A configured
handler failure must be visible; do not silently fall back to an operation that
bypasses required application behavior.

Database changes made by transactional hooks should participate in their parent
operation. External side effects require explicit execution semantics because a
database rollback cannot undo them. Batch operations, hook-generated edits,
recursion, cancellation, and execution order need defined contracts.

## Interfaces and interoperability

- REST is the common interface and must have OpenAPI documentation and an
  interactive Swagger-style documentation interface.
- Bind to localhost by default. External network binding is opt-in and must
  support TLS, mTLS, and username/password authentication.
- The CLI supports local workbook files and remote services. Managed file mode
  starts or attaches to a local service; direct database mode is explicit.
- Design GUI, CLI, and API operations to be useful to people, scripts, and AI
  agents, including structured errors and bulk operations.
- Support Excel and OpenDocument spreadsheet import/export, plus tabular
  interchange. Track values, formulas, formatting, and advanced features
  separately in the compatibility contract.
- Preserve native queries, relationships, and Python code in the native format.
  Specify export representations and report unsupported or transformed features.

## Design documentation and implementation guidance

Start with [docs/design/README.md](docs/design/README.md). Keep design documents
under `docs/design/` and update them when architectural decisions change.

The project is currently at the design stage. The documents distinguish agreed
product and access decisions from proposed implementation details and open
questions. Qt Widgets/C++ is selected for the desktop GUI. The REST framework,
physical schema, Python isolation strategy, and detailed grid design remain open.

When implementing:

- Preserve the managed/direct access boundary and keep workflow semantics in
  user code.
- Reuse shared engine operations across clients. Embedded Python can call the
  engine through a transaction-aware context rather than loopback REST calls.
- Verify managed SQL change capture before promising parity with grid edits.
- Keep identity, display order, stored inputs, and derived values distinct.
- Add appropriate verification for transaction, formula, and hook semantics;
  document compatibility limitations and avoid silent data loss.

See [the implementation plan](docs/design/implementation-plan.md) for proposed
milestones and the decisions that must be resolved before implementation.
