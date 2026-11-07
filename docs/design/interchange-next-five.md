# Next five interchange and recovery increments

November 6, 2026. Continue after PR #23 in dependency order. Implement, locally
verify, publish/review/fix and merge each PR before starting the next. Hosted CI
remains unavailable under the user's local verification authorization.

1. [Cancellable CSV preparation, PR #24](https://github.com/hybridmachine/DataHorizon/pull/24), progress and explicit atomic Apply.
2. [XLSX inputs/formulas, PR #25](https://github.com/hybridmachine/DataHorizon/pull/25), with snapshot export and conversion reports.
3. [XLSX native styles, PR #26](https://github.com/hybridmachine/DataHorizon/pull/26), with explicit loss reports.
4. [ODS interchange, PR #27](https://github.com/hybridmachine/DataHorizon/pull/27), with bounded input/formula/style import and snapshot export.
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

XLSX review corrections exclude phonetic annotations from shared/inline text,
decode UTF-16 surrogate escapes once, preserve XML control/noncharacter text
through Excel escapes, reject unavailable formula characters and enforce Excel's
32,767 UTF-16-unit text limit on export. Unsupported inputs never yield partial
Apply data. [Local verification](evidence/xlsx-verification.txt) records complete
CTest, final service regressions, native Cocoa scale 2 and independent readers.

## XLSX native styles

Import maps direct cell formats to the existing native style tier. General,
Text and fixed2 map from built-ins 0/49/2 and exact custom equivalents. Bold,
italic, left/center/right alignment, wrap, solid RGB fill and four thin
borders map to native properties. Borders use the native default color. Default
and styled-empty cells replace destination styles, including removing prior
formatting, in the same managed edit as the imported source. Input kinds stay
explicit: a numeric cell with Text display formatting remains numeric. Undo and
required hooks cover sources and styles together.

The report lists unsupported number formats, date/time display serials,
partial/thick borders, theme/indexed/tinted fills and alignment properties.
Font family/size/color, rich-text runs, named-style inheritance, conditional
formatting, row/column defaults and dimensions are outside this tier and reported
as omitted. Dates are not silently interpreted as native dates. Styles and shared
strings follow local workbook part relationships; invalid indices or missing
related parts block the entire applicable input payload.

Export writes direct cell styles with a complete stylesheet and package
relationship. At most 1,024 distinct native styles are exported; input tables
are bounded at 4,096 entries. Existing ZIP/XML/response limits remain. Independent
openpyxl producer/consumer checks exercise every mapped property; the Qt fixture
is produced by openpyxl and verifies calculated fixed2 display and emphasis.
Mapping follows [Microsoft's cell format documentation](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.spreadsheet.cellformat).

[Style verification](evidence/xlsx-styles-verification.txt) records full local
CTest and final review regressions, independent readers and Cocoa scale 2.
Review corrected ARGB handling (alpha is irrelevant to cell fills), and now
reports tint, patterned fills and diagonal borders. The
[native screenshot](evidence/xlsx-styles-native.png) was visually inspected.

## ODS inputs, formulas and native styles

ODS 1.3 uses the same ZIP/XML, preparation, response, managed Apply and export
selection budgets as XLSX. One sheet's stored cells retain sparse coordinates;
repeated empty extents are skipped without materializing a dense grid. Repeated
nonempty/styled cells share the 10,000-cell limit, including row repetition.
Text, finite numeric/percentage/currency values and Booleans retain explicit
kinds. ISO dates/durations become Text. Paragraph whitespace follows ODF collapse
rules while explicit spaces/tabs/line breaks remain; annotations/ruby readings
are excluded. Formula caches are an explicit freshness-unverified alternative.
Missing/error caches and invalid inputs block the entire applicable payload.

The formula tier translates of:= local bracketed A1 references, ranges, argument
separators, TRUE()/FALSE(), SUM/IF, arithmetic and comparisons. Quoted strings
remain literal. Export renders the parsed expression with explicit parentheses;
source spelling becomes canonical. Powers, cross-sheet/external references,
matrix formulas, names and other functions are unavailable in formula mode.
Powers are excluded until OpenFormula's different precedence has its own tested
contract. Export requires current, error-free values, including formula caches;
references outside the exported rectangle reject. Target coercion/function
semantics remain a compatibility limitation, stated in the report.

Direct native styles map through ODF cell/text/paragraph properties and exact
Text/fixed2 data styles. Unsupported display and inherited/default/conditional
formats are reported as omitted. Other sheets, code, charts, comments, merged
presentation, names, validation, queries, relationships and view preferences are
omitted; no code or external resource activates. Native NULL becomes empty.
Export includes the first, uncompressed mimetype entry and manifest. Repeated input payloads preflight a 3-MiB staged byte budget. Whitespace
expansion is bounded before allocation; export caps explicit whitespace nodes
at 50,000 and expanded package XML at 16 MiB. Independent odfpy fixtures and
consumer checks supplement XML tests and real Qt flows; they do not qualify
LibreOffice or native Excel interoperability.

The mapping follows the [ODF schema](https://docs.oasis-open.org/office/OpenDocument/v1.3/os/part3-schema/OpenDocument-v1.3-os-part3-schema.html)
and [OpenFormula standard](https://docs.oasis-open.org/office/OpenDocument/v1.3/OpenDocument-v1.3-part4-formula.html).

ODS review corrected paragraph whitespace and excluded annotation/ruby text,
rejected unknown cell types, and preflighted repeated source bytes before
expansion/serialization. A repeated-long-text regression checks traced peak
allocations stay below 12 MiB while rejecting an otherwise 600-MB payload.
[ODS verification](evidence/ods-verification.txt) records the final regressions,
full suites, independent producer/consumer and native Cocoa scale 2.
