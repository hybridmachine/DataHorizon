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

## Whole-workbook ODS

ODS now supports the same `scope=workbook` and atomic Apply contract. OpenFormula
cell/range references translate between native qualified names and ODF named
sheet locators, including spaces, apostrophes, punctuation and absolute cell
components. Export repeats the sheet locator on both range endpoints to preserve
native inherited range targets. Different-sheet range endpoints, external files,
unknown sheets, matrix formulas and power syntax reject explicitly. String
literals are tokenized separately from references and function separators.
Names, inputs, current caches and mapped direct styles share one snapshot;
empty sheets remain present. Date/duration values remain ISO Text; percentage
and currency keep their magnitudes without unsupported display/currency codes.
Sheet-relative movement semantics are canonicalized to named sheet references.

The translation follows the named-sheet reference grammar in the
[OASIS OpenFormula specification](https://docs.oasis-open.org/office/v1.2/cd05/OpenDocument-v1.2-cd05-part2.html).
This is a bounded compatibility tier, with independent XML fixtures and odfpy
consumption; it does not claim native LibreOffice acceptance. Local evidence is
in `evidence/workbook-ods-verification.txt`.

## Formula and style fidelity

XLSX ordinary formula metadata (`t=normal`, supported recalculation flag) and
bounded shared groups are supported. Stored members are translated relative to
one unambiguous master, including absolute components and untouched string literals.
Only actual stored members expand; a declared rectangle never creates absent cells.
Missing/duplicate masters, invalid extents/members and unsupported metadata block
all inputs. Arrays, data tables and unsupported functions remain unavailable.
See Microsoft's [shared formula metadata](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.spreadsheet.cellformula?view=openxml-3.0.1).

ODS explicit table-cell parent chains resolve at most 32 levels with cycle and
missing-parent rejection. Child attributes override supported inherited properties,
including explicit normal fonts, transparent fills and no-wrap. Default styles,
row/column formatting and unsupported properties remain outside this tier.

`tests/fixtures/generate_fidelity.py` creates checked, hashed openpyxl/odfpy files;
`fidelity-provenance.json` identifies their actual producers. Core tests need no
third-party dependencies. `tests/verify_workbook_consumers.py` independently reads
formula/values exports for both formats, 16 styled formula rows, three sheets and
a cross-sheet total. Build-only dependencies are pinned in
`tests/requirements-fidelity.txt`. These files are not represented as having been
produced or accepted by native Excel/LibreOffice. Their application qualification
remains an explicit platform gate. Evidence: `evidence/fidelity-verification.txt`
and `evidence/workbook-consumers.json`.
