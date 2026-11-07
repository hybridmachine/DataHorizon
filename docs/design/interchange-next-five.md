# Next five interchange and recovery increments

November 6, 2026. Continue after PR #23 in dependency order. Implement, locally
verify, publish/review/fix and merge each PR before starting the next. Hosted CI
remains unavailable under the user's local verification authorization.

1. [Cancellable CSV preparation, PR #24](https://github.com/hybridmachine/DataHorizon/pull/24), progress and explicit atomic Apply.
2. Bounded XLSX input/formula import and snapshot export with conversion reports.
3. XLSX formatting fidelity for the native style tier, with explicit loss reports.
4. Bounded ODS input/formula/style import and snapshot export.
5. Native recovery and interchange failure hardening, with destructive fixtures.

## Preparation jobs

CSV preparation runs outside HTTP admission and workbook locks. The service
retains at most four ephemeral jobs, one running at a time, with a ten-second
cooperative deadline and five-minute retention. Completed responses retain the
existing 4-MiB bound. Source remains at most 3 MiB and 10,000 cells. Progress
names its phase and processed units, without an invented percentage. Larger
imports and chunked commits remain unavailable.

Clients choose a bounded ASCII job ID before POST. Identical retries return the
same job; changed inputs conflict. GET returns running/completed/failed/cancelled
and the completed result or structured error. Cancellation interrupts parsing
and conversion at checkpoints, or discards a completed result. Release removes
terminal storage. Service restart loses jobs and returns JOB_NOT_FOUND; it never
implies an outcome for a workbook operation. Shutdown cancels and joins workers.

Qt shows a modal, cancellable progress window while leaving its event loop free.
Local cancellation fences the result even if completion wins the server race.
The original workbook/sheet/coordinate/state checks still guard review and Apply.
The SDK exposes start/status/cancel/release; all clients use these REST resources.
Preparation never runs hooks or mutates data. Confirmed Apply submits one ordinary
managed edit with hooks, durable receipts, rollback and Undo/Redo. Once Apply is
submitted its existing outcome lookup contract applies; cancelling preparation
cannot undo that commit.

[Job verification](evidence/jobs-verification.txt): complete CTest passes 231
Python tests (15 optional-binding skips) and 89 existing Qt checks. The added
Qt cancellation check passes offscreen and on Cocoa at scale 2. Review verified
no write phase, cancellation/publication ordering, bounded admission and storage,
replay and worker shutdown; expiry now applies to status reads as well as starts.

## XLSX inputs and formulas

The UTF-8 transitional SpreadsheetML tier imports one selected sheet's stored
cells (at most 10,000), preserving sparse coordinates relative to destination
A1, literal Text/leading zeros, finite numbers, Booleans and implemented local
formulas. Offsetting translates relative references; absolute markers remain.
Absent source cells leave destination cells unchanged. The service prepares the
entire applicable input payload before review; any invalid/unsupported cell
blocks all of it. Apply uses the ordinary shared engine with hooks and history.
A separate values option imports cached results with an explicit freshness
warning. Missing/error caches block it. Shared/array formulas, external and
cross-sheet references, names and functions outside SUM/IF are unavailable in
formula mode. ISO date cells stay Text; serial dates remain numbers with omitted
display formatting in this initial tier. Native date support is not implied.

Export produces one selected-sheet rectangle at its original coordinates from
one immutable engine read. Formula mode preserves implemented local source and
requests recalculation without cached values. References outside the rectangle
or to other sheets block it; users may expand the selection or choose fresh
calculated values. Calculation errors block values export. Native NULL becomes
empty; other sheets/code/queries/relationships/history/view preferences are
omitted. The native workbook remains the authoritative copy. Import/export
reports state these transformations before Apply/save; cancellation writes no
file or cells. Qt writes export files through QSaveFile after native-path/alias
and reviewed-state checks.

The stdlib ZIP/XML implementation never extracts files, resolves external
resources or activates code. Packages are limited to 3 MiB compressed, 16 MiB
expanded, 128 unique parts, 100,000 XML nodes/64 levels per part and four MiB per
response. Encrypted packages, DTDs/entities and non-UTF-8 XML reject explicitly.
These are interchange limits, not a large-workbook or full Excel compatibility
claim. Format mapping follows [Microsoft's cell schema](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.spreadsheet.cell)
and [formula documentation](https://learn.microsoft.com/en-us/office/open-xml/spreadsheet/working-with-formulas).
Independent, build-only openpyxl checks read exported inputs/formulas and write
fixtures accepted by the importer. No third-party service dependency is added.
