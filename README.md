# DataHorizon

DataHorizon is a planned native, cross-platform spreadsheet application combining
SQLite storage with Python 3 programmability. The desktop GUI uses Qt Widgets
with C++ on Windows, macOS, and Linux. GUI, managed CLI, Python SDK, and external
integrations share application behavior through a REST service.

The project is currently in the design phase. This repository contains product
and architecture documents, everyday user stories, UI wireframes, and an
implementation plan. No application code or executable prototype has been built.

## Start here

- [Design overview and document map](docs/design/README.md)
- [Implementation plan](docs/design/implementation-plan.md)
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

The desktop GUI will be licensed under GPLv3. Licenses for the service, SDK,
and other components remain undecided; this is not a repository-wide license
grant. See the design documents for the current decisions and open questions.
