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
11. **License.** All DataHorizon components use GPL-3.0-only, including the GUI,
    service, CLI and SDK. Dependencies retain their own licenses.
12. **Wireframes before implementation.** Everyday spreadsheet stories and
    wireframes preceded the explicit instruction to start implementation.
    Follow the plan and record evidence before claiming milestone completion.

## Document map

| Document | Contents |
| --- | --- |
| [Implementation status](implementation-status.md) | Current local evidence, milestone gaps and next dependencies |
| [Python automation plan](python-automation-plan.md) | Native modules, exact operator activation, atomic macros, clients and verification/review ledger |
| [Python automation guide](python-automation-guide.md) | Runnable example, module API, activation, cancellation, durable results and execution limits |
| [Python workspace design](python-workspace-design.md) | Approved design for the desktop workspace, names, owner-channel approval and event bindings, with as-built rulings |
| [Python workspace guide](python-workspace-guide.md) | Supplies example, workspace window, picking, names, Review & approve, running, rules, SDK/CLI equivalents and known limits |
| [Structured tables and query workbench plan](relational-workbench-plan.md) | Implemented bounded first M8 batch: native tables, managed record edits, relationships, read-only SQL, saved views and verification evidence |
| [Table and query wireframes](relational-workbench-wireframes.md) | Qt object browser, table/schema review, record editing, relationships and query/result interactions |
| [Relational manual acceptance](relational-workbench-manual.md) | Installed Customers/Orders fixture, required Python rule, independent edits, repair and pending physical GUI/VoiceOver steps |
| [Preview qualification increments](preview-qualification-next-ten.md) | Authorized next ten tasks; revised macOS scope and per-task delivery evidence |
| [Native opening performance](native-opening-performance.md) | One-pass validation, page geometry and explicit legacy-copy guidance |
| [macOS capacity qualification](macos-capacity-qualification.md) | Cache provenance, workload/RSS/disk measurements and the administrator-dependent OS-cold gate |
| [Automatic viewport calculation](viewport-calculation.md) | Bounded background jobs, projected targets, stream isolation and stale/failure behavior |
| [macOS failure recovery](macos-failure-recovery.md) | Actual permissions, private-volume ENOSPC, process exits and native cancellation |
| [Project review](../project-review-2026-11-08.md) | November 8 findings, reproductions and correction priorities |
| [Review remediation plan](review-remediation-plan.md) | Dependency-ordered R1–R8 fixes, compatibility decisions and qualification gates |
| [Development guide](development.md) | Build/run, API, CLI, Python, Supplies and package smoke |
| [Initial contracts](initial-contracts.md) | M0 persistence, values, operations, budgets and explicit exclusions |
| [SQLite capture experiment](sqlite-capture-experiment.md) | M1 capture comparison, typed old/new evidence, limits and remaining managed SQL gates |
| [Native capture binding](native-capture-binding.md) | Optional APSW preupdate comparison, rollback controls, bounds and adoption gates |
| [Service/Python experiment](service-execution-experiment.md) | M1 HTTP arrangements, transactional worker containment and durable outcomes |
| [Native storage experiment](native-storage-experiment.md) | M1 input/derived separation, identity, external refresh and physical-layout comparison |
| [Qt frozen-row experiment](grid-frozen-row-experiment.md) | M1 shared-cache frozen-header display, geometry/input measurements and limits |
| [Toolchain/packaging baseline](toolchain-packaging-baseline.md) | M1 build/runtime records, minimum Python matrix, install scope and remaining qualification |
| [Next five qualification increments](qualification-next-five.md) | Native plugin checks, cache/RSS, HTTP/worker hardening and native capture binding |
| [Everyday spreadsheet increments](everyday-next-five.md) | Wrap, borders, sizing, hide/unhide and freeze |
| [Editing and CSV increments](editing-csv-next-five.md) | Sheet reference changes, CSV dialects and column types |
| [Charts design](charts-design.md) | Approved design: native chart object and specification, service-side resolve and SVG rendering (route confirmed with a pie point limit and a bundled font), storage, interfaces and stages; only the stage 1 experiment is built |
| [Charts stage 1 plan](charts-stage-1-plan.md) | Task-level plan for the renderer experiment: standard-library SVG renderer, Qt SVG probe, thresholds and evidence; executed |
| [Chart renderer experiment](chart-render-experiment.md) | Stage 1 result: measurements against the thresholds, two unmet (10,000-slice pie in Qt, text width under a wide substitute font), findings, limits and the owner's ruling to stay on the service SVG route |
| [Excel fidelity programme](excel-fidelity-plan.md) | Approved design and ten-increment plan for native features and full XLSX data/formatting import and export; increment 1 as built (section 15) |
| [Excel fidelity increment 1 plan](excel-fidelity-increment-1-plan.md) | Task-level plan for import foundations: degrade instead of block, reference scanner, large staged import/export, clients, corpus and limits |
| [SpreadsheetML specification coverage](excel-spec-coverage.md) | Every worksheet/workbook element, cell and formula attribute, style construct and related part, with today's status, report entry and owning increment |
| [Increment 1 verification](evidence/excel-fidelity-1-verification.txt) | Commands, commit and results of the final increment 1 run; capacity in [excel-fidelity-1-capacity.json](evidence/excel-fidelity-1-capacity.json), corpus in [excel-fidelity-scorecard.json](evidence/excel-fidelity-scorecard.json) |
| [Interchange and recovery increments](interchange-next-five.md) | Preparation jobs, XLSX/ODS fidelity stages and failure recovery |
| [Workbook compatibility, recovery and qualification](workbook-next-five.md) | Authorized next five increments and bounded whole-workbook contracts |
| [Next ten spreadsheet increments](spreadsheet-next-ten.md) | Formula, formatting, editing, navigation and selection-summary stages |
| [Editing and list usability](editing-lists-next-ten.md) | Authorized next ten tasks after PR #44 |
| [Editing and formatting usability](editing-formatting-next-ten.md) | Authorized ten tasks after PR #74 |
| [Formula coverage follow-on](formula-coverage-next-ten.md) | Additional ten authorized after the formula editing batch |
| [Formula editing and coverage](formula-next-ten.md) | Authorized ten tasks after PR #54 |
| [M3–M5 completion increments](m3-m5-next-ten.md) | Dependency/calculation, structural hooks, portable lists and qualification |
| [Date and time increments](date-time-next-ten.md) | Native types, UTC clock, arithmetic and CSV/XLSX/ODS date tiers |
| [Native formats v1–v7](native-format-v1.md) | Actual development schema and callback-free direct SQLite access |
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

- [macOS native interaction qualification](macos-native-interaction.md)

- [Accessible cell-state increment](macos-accessible-cell-state.md)

- [macOS distribution baseline](macos-distribution.md)

- [macOS development disk image](macos-development-installer.md)
