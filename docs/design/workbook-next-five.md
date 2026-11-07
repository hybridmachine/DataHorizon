# Workbook compatibility, recovery and qualification increments

November 6, 2026. The user authorized these five increments as separate locally
built, reviewed and merged PRs. Hosted Actions have no credits; do not infer
platform qualification from local results.

1. Whole-workbook XLSX interchange.
2. Whole-workbook ODS interchange.
3. Broader formula/formatting producer and consumer compatibility fixtures.
4. Recovery failure coverage and complete bounded input inspection.
5. Cross-platform qualification and packaging tooling, with local evidence and
   explicit unexecuted platform/manual gates.

## Whole-workbook XLSX

`scope=workbook` on preparation/export covers 1–64 sheets, including empty sheets,
with original names/order/coordinates, supported inputs/styles and cross-sheet
SUM/IF/arithmetic references. Names must be unique under SQLite ASCII NOCASE,
1–31 characters, nonblank, without native forbidden characters or leading/trailing
apostrophes. Missing/external sheet targets reject formula interchange; choosing
values explicitly uses unverified input caches or current export calculations.
Stable native sheet-ID references become quoted portable names on export.

Preparation is cancellable and never edits storage. The reviewed `workbook_import`
operation replaces the catalog **only when no stored cells exist**. An activated
required handler rejects before changes because structure-event semantics remain
undefined. Sheets, inputs, revision and receipt share one transaction; a failure
rolls back the entire catalog and all cells. Success clears Undo/Redo. Existing
native/source files remain the authoritative representation of native-only features.
GUI import uses Sheet number → All sheets; export offers whole-workbook formulas
or values in its input-mode chooser. REST/SDK and managed CLI apply the same engine
operation. Single-sheet operations retain their existing behavior.

Whole-workbook export streams at most 10,000 stored cells within one read transaction
and five-second cooperative budget, with a 4-MiB intermediate payload. Aggregate
import remains 10,000 stored cells / 3 MiB payload, 3-MiB ZIP, 16-MiB expansion,
128 package entries and a 4-MiB response. No chunked commits or raised limits.
Code, queries, relationships, dimensions, history, view state and advanced features
remain omitted with reports; imported code cannot activate.

Local evidence is recorded in `evidence/workbook-xlsx-verification.txt`.
