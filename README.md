# DataHorizon

DataHorizon is a native spreadsheet application in early development, combining
SQLite storage with Python 3 programmability. The desktop GUI uses Qt Widgets
with C++ on Windows, macOS, and Linux. GUI, managed CLI, Python SDK, and external
integrations share application behavior through a REST service.

The repository now includes a Qt Widgets/C++ desktop, SQLite engine, authenticated
localhost REST service, Python client and managed CLI. The local development
preview supports spreadsheet editing, bounded views, formulas, range operations,
Undo/Redo, list projections, search and CSV. The full implementation plan is
still in progress; Excel/ODS, structural references, advanced programmability
and cross-platform release qualification remain unfinished.

## Build and run

With Qt installed in `~/Qt` on macOS:

```sh
cmake -S . -B build -DCMAKE_PREFIX_PATH="$HOME/Qt/6.12.0/macos" -DCMAKE_BUILD_TYPE=Debug
cmake --build build --parallel
ctest --test-dir build --output-on-failure
open build/DataHorizon.app
```

Python 3.11+, CMake 3.24+ and Qt 6.8+ are required. No pip dependencies are needed.
See [development instructions](docs/design/development.md) for CLI, Python,
service operation, install smoke and the Supplies walkthrough.

## Start here

- [Design overview and document map](docs/design/README.md)
- [Implementation plan](docs/design/implementation-plan.md)
- [Implementation status and evidence](docs/design/implementation-status.md)
- [Initial accepted contracts](docs/design/initial-contracts.md)
- [Development compatibility](docs/design/compatibility-v0.md)
- [Wireframe notes and interaction proposals](docs/design/ui-wireframes.md)
- [Contributor and agent guidance](AGENTS.md)

## PDF documents

- [Everyday spreadsheet user stories](output/pdf/datahorizon-everyday-spreadsheet-user-stories.pdf)
- [Desktop UI wireframes](output/pdf/datahorizon-ui-wireframes.pdf)
- [Implementation plan](output/pdf/datahorizon-implementation-plan.pdf)

## Access and programmability

Managed operations use the shared execution engine and applicable workbook
Python hooks. Independent SQLite tools can access native stored data directly;
they do not run DataHorizon's Python handlers or recalculation engine. Refreshing
externally edited data does not replay skipped change handlers.

DataHorizon provides general spreadsheet, relational, and programming capabilities.
Users define business workflows in their own code or external applications.

## License direction

The desktop GUI is licensed under GPLv3; see [license scope](LICENSES.md).
Licenses for the service, SDK,
and other components remain undecided; this is not a repository-wide license
grant. See the design documents for the current decisions and open questions.
