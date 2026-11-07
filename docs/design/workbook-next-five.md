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

## Recovery failures and bounded input inspection

Read-only inspection now checks source types, native value rules, style JSON and
formula syntax in one bounded input snapshot. It reports up to 100 located issues,
total issues, checked cells/bytes and an explicit complete/valid flag. Limits are
10,000 cells, 8 MiB and five seconds, with a 1-MiB SQLite record limit; hitting any
bound cannot claim unchecked inputs are valid. Style JSON is limited to 64 KiB.
Formula syntax checks do not evaluate formulas or promise function/export parity.
The scan is tied to the state whose opening integrity checks passed; an external
commit before final metadata requires a new inspection. No repair, recalculation,
code activation or historical hook replay occurs. The desktop shows issues/limits,
and REST/SDK/CLI retain structured results.

Snapshot API errors now identify an incomplete copy and a destination that must
be preserved/inspected, including reservation and backup failures. Existing files
are never overwritten by native snapshot creation. Real SQLite page-capacity
exhaustion and read-only backup targets test storage failure behavior. Controlled
ENOSPC/EACCES reservation failures supplement those tests. A SQLite full error that
ends its transaction without a durable receipt remains unknown for reconciliation;
it is never reported as a successful write or silently retried.

A Qt child process exits abruptly before/after QSaveFile commit, verifying that
an existing interchange destination retains either its old bytes or the committed
replacement. Existing native transaction-crash and lost-acknowledgement fixtures
remain in force. These are process/SQLite/injected-I/O fixtures, **not** physical
disk-full, power-loss or filesystem durability qualification. Evidence is in
`evidence/recovery-failures-verification.txt`.

## Local qualification and packaging — November 7, 2026

`python3 tests/qualify_preview.py --build build --config Debug --output-dir
build/qualification-CANDIDATE` performs a local C++ build, complete CTest, native
Qt automation at scales 1/2, installation, neutral-directory relocated smoke and
a local tar.gz archive. Run under a real desktop session (or an explicitly
identified virtual display); Linux can use `xvfb-run -a`. The same driver supports
macOS, Windows and Linux and writes per-step logs, source fingerprint, native
reports/screenshots, package checksum/inventory and `qualification.json`. Output
directories must be new so failed or stale evidence cannot be reused.

Installed apps require their installed launcher and never fall back to the source
checkout. Development executables alone retain their configured interpreter and
source paths. Installed interpreter order is explicit DATAHORIZON_PYTHON, an
explicitly staged adjacent runtime, then an external Python on PATH (Python 3.11+
with SQLite is required). An incomplete staged runtime fails explicitly. This
increment **does not select, bundle or redistribute a Python runtime**.

`tests/package_smoke.py` copies the installation with framework symlinks preserved,
removes Python/development/plugin overrides, runs a managed edit/calculation from
a neutral working directory, then removes the installed launcher and requires an
explicit failure. This exposes hidden dependencies on development service files.
It retains the host's external interpreter, OS, display and system libraries;
it is not clean-machine evidence.

`tests/preview_package.py` creates an exclusive local development archive, rejects
external symlinks/missing required files, preserves Qt framework links and file
modes, and verifies archived content against a SHA-256 inventory. Its manifest
records toolchain/source provenance and unresolved distribution obligations. The
archive is a local build artifact, not a redistributable installer or public release.

The qualification matrix only marks the current platform's completed automation.
Absent platforms, physical input, native dialogs, screen readers, multiple monitors,
clean-machine runtime/installer, signing/notarization and distribution licenses
remain pending. No successful new hosted jobs are claimed. Local macOS evidence
is recorded in `evidence/local-qualification.json` and
`evidence/qualification-packaging-verification.txt`; Windows/Linux require actual
runs on those systems when capacity is available.

## Implementation PR record

The first four implementation PRs were locally verified, self-reviewed and merged
in order: [XLSX #30](https://github.com/hybridmachine/DataHorizon/pull/30),
[ODS #31](https://github.com/hybridmachine/DataHorizon/pull/31),
[fidelity #32](https://github.com/hybridmachine/DataHorizon/pull/32), and
[recovery #33](https://github.com/hybridmachine/DataHorizon/pull/33).
The final qualification/packaging implementation retains the unexecuted release
gates listed above. This batch does not close M1–M8 or qualify a public preview.
