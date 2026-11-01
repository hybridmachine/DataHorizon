# DataHorizon design

Initial design: 2026-10-31. This directory captures the product direction and
access contract agreed during the initial discussion, followed by implementation
proposals. Implementation was authorized on October 31, 2026. The
[implementation status](implementation-status.md) distinguishes working local
behavior from the remaining plan; historical PDFs describe intended behavior.

## Vision

DataHorizon combines a familiar spreadsheet interface, SQLite storage, and an
embedded Python programming environment. People can start with a spreadsheet
and progressively introduce structure, relationships, queries, and automation
as their needs grow.

Users already build business processes around spreadsheets. DataHorizon gives
them reliable general-purpose data and programming capabilities while keeping
control of business behavior in their own code.

The desktop application runs natively on Windows, macOS, and Linux. The same
execution engine serves the GUI, command line, Python clients, external
applications, and AI agents through REST.

## Agreed decisions

1. **Spreadsheet-first interaction.** Familiar grid editing and spreadsheet
   functions are central. Excel function compatibility is a staged goal.
2. **SQLite storage.** The native workbook uses SQLite and remains accessible
   through ordinary SQLite tools.
3. **Python programmability.** Workbook code provides macros, custom functions,
   and event-driven behavior. External code can automate through the API.
4. **Generic application.** Users define workflows and business processes.
   DataHorizon provides the primitives and extension points.
5. **Shared managed execution.** GUI and REST operations participate in the same
   engine behavior and applicable Python hooks. Managed SQL does too.
6. **Explicit direct access.** Direct SQLite edits bypass the Python runtime and
   application recalculation. Skipped change handlers are not replayed later.
7. **Relational capabilities.** Support tables, joins, views, and SQL alongside
   spreadsheet operations.
8. **Interchange.** Support Excel and OpenDocument import/export with documented
   compatibility and export behavior for native-only features.
9. **Local and remote operation.** Bind REST to localhost by default, support
   opt-in network access with TLS, mTLS, and username/password authentication,
   and publish interactive API documentation.
10. **Qt Widgets/C++ desktop GUI.** Use Qt Widgets with C++ on Windows, macOS,
    and Linux.
11. **GUI license.** License the GUI under GPLv3. Licensing for the service,
    SDK, and other components remains undecided.
12. **Wireframes before implementation.** Everyday spreadsheet stories and
    wireframes preceded the explicit instruction to start implementation.
    Follow the plan and record evidence before claiming milestone completion.

## Document map

| Document | Contents |
| --- | --- |
| [Implementation status](implementation-status.md) | Current local evidence, milestone gaps and next dependencies |
| [Development guide](development.md) | Build/run, API, CLI, Python, Supplies and package smoke |
| [Initial contracts](initial-contracts.md) | M0 persistence, values, operations, budgets and explicit exclusions |
| [Native format v1](native-format-v1.md) | Actual development schema and callback-free direct SQLite access |
| [Compatibility tier 0](compatibility-v0.md) | Implemented formulas, formatting, interchange and editing limits |
| [Architecture](architecture.md) | Components, execution paths, transactions, external edits, and recovery |
| [Data model](data-model.md) | Proposed sheets/tables/views model, identity, types, formulas, and native storage |
| [Python runtime](python-runtime.md) | Workbook code, event lifecycle, transaction context, batches, and failures |
| [Interfaces and interoperability](interfaces-and-interoperability.md) | GUI, REST, CLI, SQL, SDK, network access, and file interchange |
| [Implementation plan](implementation-plan.md) | Dependency-ordered wireframe implementation increments, decision points, acceptance criteria, and verification |
| [Implementation plan (PDF)](../../output/pdf/datahorizon-implementation-plan.pdf) | Printable companion to the wireframes, from contract decisions through the cross-platform desktop preview |
| [Everyday spreadsheet user stories (PDF)](../../output/pdf/datahorizon-everyday-spreadsheet-user-stories.pdf) | 20 draft user stories, acceptance criteria, examples, and a sample session |
| [Desktop UI wireframes](ui-wireframes.md) | Interaction proposals, Qt Widgets mapping, review decisions, and story coverage |
| [Desktop UI wireframes (PDF)](../../output/pdf/datahorizon-ui-wireframes.pdf) | 18 landscape pages covering everyday editing, dialogs, storage/calculation states, and error recovery |

## How to read this design

The agreed decisions above and the access boundary are requirements. Component
boundaries, event names, schema concepts, API examples, and milestones are
proposals unless explicitly identified as agreed. They should be refined through
small implementation experiments before they become stable public contracts.

In these documents:

- **Workbook** means the native DataHorizon document and its stored data, code,
  formulas, and metadata.
- **Managed operation** means an operation executed by the DataHorizon engine,
  including applicable Python behavior.
- **Direct operation** means an operation through an independent SQLite
  connection, outside the engine.
- **Macro** means an explicitly invoked Python entry point; **handler** means
  Python invoked for a registered event. Both are workbook code.
- **Derived state** includes calculated values, dependency information, and
  caches that can become stale after external edits.

## Scope boundaries

The initial architecture does not define business-specific entities or workflow
features. Examples in future documents must remain illustrations, not built-in
application requirements.

Qt Widgets/C++ is selected for the GUI. The detailed grid design, REST framework,
advanced physical schema and production service/Python arrangements remain
open. The development tier uses stdlib HTTP/sqlite3, spawned brokered workers,
a bounded QTableView model, and the documented experimental native format v1.
Concurrent multiuser editing and complete Excel feature parity are not initial
release promises. The architecture should leave room for future capabilities
without treating them as already designed.
