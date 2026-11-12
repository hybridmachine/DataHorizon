# Excel fidelity programme: design and increment plan

Draft 0.3 | November 11, 2026 | Status: approved by the owner on November 11, 2026. Increment 1 is built except for the open items in section 6 (the Excel-saved and LibreOffice-saved corpus); section 14 decisions remain open. Section 15 records how increment 1 was built.

The [increment 1 plan](excel-fidelity-increment-1-plan.md) turns section 6 into tasks.

This document plans the work that takes DataHorizon from its bounded XLSX tier
to holding, showing and round-tripping the data and formatting an Excel workbook
contains. It is a programme of ten increments. Each increment gets its own
task-level plan (in the style of [the Python workspace plan](python-workspace-plan.md))
when it starts; this document fixes what each one must deliver, the decisions
that cut across them, and the order.

Each decision is marked **Owner** (stated by the owner) or **Call** (chosen by
the author and open to reversal). Section 14 lists what only the owner can
settle.

## 1. Intent

A person should be able to open a real Excel workbook in DataHorizon and find
their data, formulas and formatting there, then save it back to Excel without
losing what DataHorizon understood. Today the importer reads a narrow tier and
rejects the whole file if a single cell falls outside it, and about half of what
a typical workbook contains (merged cells, custom number formats, validation,
conditional formatting, comments) has nowhere to live in the native format.

The owner's direction, November 11, 2026:

- Compare against the Excel file specification and aim to import what it
  defines. **Owner**
- Data first, then formatting. Charts are out for now. **Owner**
- Think big: add the missing native features, not only importer mappings. **Owner**
- The eight-part split in section 4. **Owner** (accepted as proposed)
- Add large-workbook editing and calculation as increments 9 and 10, so a
  large imported file can be worked in, not only opened. **Owner**

"The specification" here means ECMA-376 Part 1 (SpreadsheetML), transitional
and strict. The gap table in section 3 was first written from the author's
knowledge of that standard and from reading the importer. Increment 1 checked it
element by element: [the specification coverage table](excel-spec-coverage.md)
lists every worksheet and workbook element, cell and formula attribute, style
construct and related part with its status and owner, and the rows it added to
section 3 are marked **Call**.

## 2. Goals, non-goals and anti-goals

**Goals**

- Every cell-level and sheet-level data and formatting feature in section 3 has
  a native representation, is shown in the grid, survives native save/reopen
  and copy, and maps to and from XLSX.
- An import never fails because of one unsupported cell or feature. It
  degrades that item, says so, and imports the rest.
- Import and export limits are raised to measured, documented values that
  cover ordinary real workbooks.
- Each native feature is available through the shared engine, REST (with
  OpenAPI), the managed CLI and the SDK, and participates in history,
  structural edits, copy/paste and the applicable hooks.
- Each native feature can be created and changed from the desktop, not only
  imported. **Call**: basic authoring only (a command and a small dialog per
  feature); Excel-parity dialogs are not a goal.
- ODS gets the same mapping wherever OpenDocument has an equivalent.

**Non-goals**

- Charts, drawings, images, shapes, sparklines. **Owner** (charts); **Call** (the rest)
- Pivot tables, slicers, Power Query, data model, external data connections.
- VBA and macro sheets. Imported code never activates; that rule is unchanged.
- External workbook links. Formulas that use them degrade under section 5.
- Sheet and workbook protection, print setup, page layout, headers/footers. **Call**
- Carrying unknown package parts through untouched so they reappear on export.
  **Call**: DataHorizon exports what it holds natively and reports the rest.
- The legacy binary `.xls` and `.xlsb` formats.
- Locale-specific number and date rendering. Display stays invariant
  (English names, `.` decimal, `,` group) as today. **Call**
- Windows/Linux qualification, signing and the other open release gates.
- Added by the increment 1 coverage check (section 14, item 5): view state
  beyond sizes, hidden state and frozen panes (split panes, zoom, gridlines,
  custom views, watch window); what-if tools (scenarios, data consolidation,
  data tables); saved sort state; ignored-error flags, smart tags and web
  publishing; rich values in cells; custom sheet and document properties. Each
  is reported on import. **Call**

**Anti-goals: these make the programme a failure even if it works**

- Silent loss. Anything dropped, approximated or converted on import or
  export must appear in the conversion report before the user applies or saves.
- A value that differs from Excel's without a report entry: a number, date or
  text that imports as something else, or a formula that calculates differently
  where DataHorizon claims support for the function.
- Weakening the managed/direct boundary. New storage must stay readable by
  ordinary SQLite without Python callbacks, and managed imports must keep
  running the applicable hooks.
- Raising limits by assertion. Every new limit is backed by a recorded
  measurement.
- A third-party runtime dependency in the service. The importer stays on the
  standard library; openpyxl/odfpy remain build-only verification tools.

## 3. Where things stand

Verified by reading `service/datahorizon/interchange.py`, `xlsx_styles.py`,
`engine.py`, `metadata.py`, `formats.py`, `formula.py` and
`desktop/sheetmodel.cpp` on November 11, 2026, and updated when increment 1 was
built. Since increment 1 every feature in the "Needs" column that is not yet
imported is reported (`sheet.feature`, `workbook.feature`, `style.*`,
`formula.*` or `cell.*` entries) rather than dropped; the
[coverage table](excel-spec-coverage.md) names the entry for each element.

| Area | Today | Needs |
| --- | --- | --- |
| Numbers, Booleans, strings, ISO dates, serial dates | Imported | — |
| Error-valued cells | Imported as Text and reported (increment 1) | Engine literal (8a) |
| Formulas | Ordinary and shared; any formula DataHorizon cannot evaluate is kept and reported (increment 1) | More functions |
| Array formulas, data tables | Kept unevaluable or replaced by caches, reported (increment 1) | Engine (arrays 8b) |
| Defined names | Native names exist (v6) but not in formulas or interchange | Engine, importer |
| External references | Kept unevaluable, reported (increment 1) | — (non-goal) |
| Excel tables (`xl/tables`) | Omitted | Importer onto native lists |
| Merged cells | Absent | Native, grid, importer |
| Column widths, row heights | Native view settings exist (at most 1,024 per axis), not imported | Importer; raise bound |
| Hidden rows/columns, frozen panes | Native view settings exist, not imported | Importer |
| Hidden sheets, tab colour | Absent | Native, importer |
| Hyperlinks, comments, rich text | Absent | Native, grid, importer |
| Data validation | Absent | Native, engine, importer |
| Conditional formatting | Absent | Native, engine, grid, importer |
| Number formats | Twelve fixed formats | Format-code engine |
| Font family, size, RGB colour, bold, italic | Imported | — |
| Underline, strikethrough, super/subscript | Absent | Native, grid |
| Theme, indexed, tinted colours | Omitted | Importer resolves to RGB |
| Solid fill | Imported | — |
| Pattern and gradient fills | Omitted | Native, grid |
| Borders | Four edges, five line styles, default colour | Colour, diagonal, remaining styles |
| Horizontal alignment, wrap | Left/center/right, wrap | Remaining values |
| Vertical alignment, rotation, indent, shrink | Omitted | Native, grid |
| Named-style inheritance | Omitted | Importer flattens |
| Row and column default styles | Omitted | Native, importer |
| Strict OOXML variant | Imported (increment 1) | — |
| Size | Bounded path: 10,000 cells, 64 sheets, 3 MiB zipped, 16 MiB expanded; large path: 1,000,000 cells, 255 sheets, 48 MiB, 256 MiB expanded (increment 1, 5.2) | Large edits and calculation (9, 10) |
| Failure mode | Valid content degrades and is reported; only a malformed package rejects (increment 1) | — |
| Split panes, zoom, gridlines, right-to-left, custom views, watch window | Reported | — (non-goal: view state) **Call** |
| Scenarios, data consolidation, data tables as what-if tools | Reported | — (non-goal: what-if tools) **Call** |
| AutoFilter ranges and saved sort state | Reported | AutoFilter with Excel tables onto native lists (8a); sort state is a non-goal **Call** |
| Calculation settings (manual mode, iteration, precision as displayed) | Reported | 10 **Call** |
| Phonetic guides (`rPh`, `ph`) | Reported | 5, with rich text **Call** |
| Rich values in cells (`vm`: pictures in cells, data types) | Cached value imported; reported | — (non-goal: images, data types) **Call** |
| Ignored-error flags, smart tags, web publishing, custom sheet and document properties | Reported | — (non-goal) **Call** |
| Chart sheets, dialog sheets, macro sheets | Left out and reported (`workbook.feature` `chart_sheets`, `dialog_sheets`, `macro_sheets`; increment 1) | — (non-goal: charts, macro sheets) **Call** |
| Document core properties (author, dates) | Not reported | Owner decision (section 14) **Call** |

## 4. The ten increments

| # | Increment | Depends on |
| --- | --- | --- |
| 1 | Import foundations: degrade instead of block, larger files, strict variant, corpus | — |
| 2 | Sheet geometry: merged cells, sizes, hidden/frozen, sheet visibility and tab colour | 1 |
| 3 | Wider cell styles | 1 |
| 4 | Number-format engine | 1 |
| 5 | Cell attachments: hyperlinks, comments, rich text | 1 |
| 6 | Data validation | 1, 8a |
| 7 | Conditional formatting | 3, 8a |
| 8 | Formula reach: (a) names and error literals, (b) arrays, (c) function catalogue stages | 1; 8b and 8c after 10 |
| 9 | Large structural edits | 1 |
| 10 | Stored results and scaled recalculation | 1 |

Order of work: 1, 9, 10, 2, 3, 4, 8a, 5, 6, 7, with 8b and 8c running as stages
after 10. **Call**: 9 and 10 come straight after 1 for two reasons. They are
"data first": they decide whether a large file is usable. And they change the
structural-edit path and the calculation model that every later increment
plugs into; merged ranges, validation rules and conditional formats would
otherwise be built against the bounded paths and then reworked. After that,
"looks right" (2–4) comes ahead of the function catalogue (8c) because the
owner asked for data and formatting and did not pick between looking right
and calculating right. Increment 1 keeps every formula's source, so nothing
is lost by adding functions later.

Each increment is one or more reviewed pull requests, merged in order, each
leaving `main` working.

## 5. Cross-cutting decisions

### 5.1 Degrade, never block

Today any `UNSUPPORTED_FORMULA` or `UNSUPPORTED_CELL` blocks the entire import.
Replace that with per-item degradation. **Call** on the specific rules:

- **Formula DataHorizon cannot evaluate** (unknown function, array, external
  link, unsupported syntax): the formula source is stored as a formula. It
  evaluates to an error that names the reason (for example `#NAME?` with
  "VLOOKUP is not available"). The report counts these by reason and lists the
  first locations. When a later increment adds the function, the cell starts
  calculating with no re-import. The engine already stores any `=` source and
  shows `#NAME?`/`#VALUE!` with the source kept; the rejection is in
  `interchange.local_formula` and in the structural-reference transforms, and
  those are what increment 1 changes.
- **References inside unevaluable formulas.** A structural edit must not leave
  such a formula pointing at stale addresses that later start calculating.
  **Call**: increment 1 widens the reference tokenizer (not the evaluator) so
  that cell and range references, whole-row/column references (`A:A`, `1:1`),
  3D references, `%`, `@`, `#`, array constants and structured-reference
  brackets are recognised and ordinary references are adjusted in any formula
  that tokenizes. A formula that still does not tokenize is marked
  "references not maintained" by the first structural edit that touches its
  sheet or a sheet it could refer to; from then on it shows an error and is
  reported until the user re-enters it. It never silently calculates against
  moved cells.
- **Import mode `values`** keeps its current meaning: cached results become
  literals, with the existing freshness warning.
- **New import mode `mixed`**: formulas DataHorizon can evaluate are imported
  as formulas; the rest become their cached values. This is the mode offered
  first in the desktop. **Call**
- **Unsupported cell content** (an unknown cell type, text over the native
  64-KiB limit): the cell is skipped and reported by location. Text is never
  truncated silently.
- **Unsupported formatting**: omitted and reported, as today.
- **Malformed package** (bad ZIP, invalid XML, broken relationships, encrypted):
  still rejects the file. Degradation is for valid content DataHorizon does not
  support, not for corrupt input.
- **Exporting what was kept** (as built, final review). A workbook imported in
  `formulas` mode must export: whole-workbook export in `formulas` mode, bounded
  and large, writes every stored formula. XLSX writes a formula DataHorizon can
  evaluate as before and any other as stored (with the `_xlfn.` mapping and sheet
  names for sheet identities where the scanner's reference spans allow; a source
  no transform can read keeps its identities), counted in one `unevaluable` entry
  per `formula_admission` category (detail "written as stored; DataHorizon cannot
  evaluate it"); a source the scanner cannot tokenize is written as stored with
  an `approximated` `formula.syntax` entry, as `unmaintained` cells are. ODS
  writes a formula that `native_to_ods` translates as before; any other is not
  written as a formula (an XLSX-dialect string is not OpenFormula): the cell
  takes its current value when the engine has an error-free one
  (`formula.unsupported_syntax`, `converted`), else it is written empty
  (`omitted`; the large export evaluates nothing, so always empty there). A
  translated formula without an error-free current value is written without a
  cache in the bounded export as well (`formula.cache_omitted`). Selection-scope
  export keeps its strict checks. A `values` export still needs current,
  error-free values; its `CALCULATION_ERROR` now names the first 100 blocking
  cells (`cells`, `blocked_count`).

The report gains structure: each entry has a category, a severity
(`converted`, `approximated`, `omitted`, `unevaluable`), a count and up to 100
sample locations, so the desktop can group it and scripts can read it.

### 5.2 Size

The 10,000-cell cap is `MAX_CELLS` in the engine and is shared by ordinary
operations, paste, structure changes and interchange. Do not raise that
constant. **Call**: interchange gets its own path and limits.

- Parse sheet XML and shared strings as a stream (`iterparse`), not a tree.
- Stage prepared cells on disk using the existing durable job journal
  (`operation_jobs.py`), then apply.
- Importing into an empty workbook remains one atomic transaction that clears
  history, as now. Hooks follow the existing whole-workbook contract.
- **New**: importing a workbook's sheets as additional sheets of a non-empty
  workbook, also atomic. **Call**
- Export streams sheet by sheet from one read snapshot.
- **Measured limits** (qualified November 11, 2026 on the reference Mac, an
  Apple M1 MacBook Air with 8 GB of memory, macOS 26.6.2, Python 3.14.6,
  SQLite 3.53.4, warm and uncontrolled OS file cache; evidence:
  [excel-fidelity-1-capacity.json](evidence/excel-fidelity-1-capacity.json),
  produced by `tests/qualify_large_interchange.py` from workbooks written by
  `tests/fixtures/generate_large_xlsx.py`). These numbers hold for this host
  only. A size qualifies when every phase (upload, prepare in `mixed` mode,
  apply, close, reopen in a fresh service, first viewport, export job,
  download, and at the largest size a re-import of the export) finishes within
  300 seconds, the service process's peak resident memory stays under the
  256-MiB service target in every phase, and the reopened file opens and
  serves its first viewport within the five-second opening budget.

  | Case | Slowest phase | Peak service RSS | Open + first viewport |
  | --- | --- | --- | --- |
  | XLSX 100,000 cells | prepare 3.2 s | 93.0 MB | 0.08 s |
  | XLSX 500,000 cells | prepare 15.5 s | 92.8 MB | 0.32 s |
  | XLSX 1,000,000 cells | prepare 30.9 s | 93.0 MB | 0.79 s |
  | ODS 100,000 cells | prepare 6.3 s | 92.8 MB | 0.09 s |
  | ODS 500,000 cells | prepare 31.9 s | 92.9 MB | 0.33 s |
  | ODS 1,000,000 cells, no long text | re-import prepare 35.3 s | 100.3 MB | 0.66 s |

  Supplementary run after the final-review fixes (the bounded feature scanner
  in place; commit `aeb9fd0`, later amended as `17638e4` with only the CTest
  timeout changed; same host, recorded as
  `final_review_xlsx_1000000` in the evidence without replacing the runs
  above): XLSX at 1,000,000 cells still qualifies, slowest phase prepare 31.8 s
  (was 30.9 s), peak service RSS 83.2 MB (was 93.0 MB), open and first viewport
  0.97 s (was 0.79 s), every cell imported and re-imported. ODS was not re-run.

  The generated mix is about 60% numbers, 25% short text, 5% long text
  (200–2,000 characters), 5% dates and 5% formulas (one in five using a
  function DataHorizon lacks), 331 cell formats, three sheets. Service memory
  stays flat with size because every phase streams. At 1,000,000 cells the
  native file is 262 MB and the import stage 230 MB; the XLSX package is
  23 MB (92 MB expanded) and its export 28 MB (105 MB expanded).

  Limits in force: **1,000,000 stored cells** (`LARGE_CELLS`, kept), **256 MiB
  expanded** (`LARGE_EXPANDED`, kept), **48 MiB compressed package**
  (`LARGE_PACKAGE`, lowered from the 64-MiB target: the largest qualifying
  package measured was 31.2 MB, and the limit is that with 1.5× headroom,
  rounded up to 16 MiB), 255 sheets, 300 seconds per upload, preparation,
  apply, export job and download, at most 1 MiB of XML per cell or shared
  string, a native file of at most 2 GiB for an export snapshot, 2 concurrent
  uploads and 2 concurrent downloads, 1,024 distinct styles per export and
  65,536 distinct styles per import stage (`import_stage.MAX_STYLES`, `LIMIT`).
  The conversion report keeps at most 200 entries with 100 samples each, a
  sample's sheet name cut to 64 and its cell to 32 characters, so a full report
  can reach about 2.4 MiB before the stage summary trims samples to stay under
  1 MiB. Progress and cooperative cancellation run throughout.

  Bounds of the feature scanners and package surveys (final review), so that no
  accumulator grows with untrusted content: per worksheet, at most 16,384
  `<col>` elements (`xlsx_features.MAX_COLUMN_ELEMENTS`) and 1,048,576 rows
  (`MAX_ROW_ELEMENTS`), and at most 1,000,000 merged ranges, hyperlinks or
  validation rules (`MAX_FEATURE_ITEMS`, equal to `LARGE_CELLS`), each `LIMIT`
  beyond; column widths are kept as one count per distinct width, so at most
  16,384 keys. Distinct identifiers kept only for a report count (x14
  conditional-format ids, ODS validation names) are a `DistinctCount` of at most
  65,536 keys and 4 MiB of key text (`MAX_DISTINCT`, `DISTINCT_BYTES`); beyond
  it the count saturates and the entry carries `at_least: true` ("at least N").
  A sheet relationship part keeps only its comment targets. The XLSX streaming
  reader admits at most 4,096 `<sheet>` elements (`LARGE_PARTS`) and counts only
  worksheets toward 255 before it keeps their names, and keeps at most 65,536
  shared-formula groups open at once and 16 MiB of their master text per sheet
  (`SHARED_GROUPS`, `SHARED_BYTES`): a group is released once the rows have
  passed the end of its extent, and only groups still open beyond the bounds
  are `LIMIT`. A shared-formula extent may be any rectangle of the grid that
  holds its master; only stored members are expanded, so its size costs nothing
  and the path's cell limit applies to the cells stored. The ODS survey keeps at
  most 255 sheet names (checked before each is kept) with 1 MiB of names and
  table styles, 65,536 column, row and table styles inside the 16-MiB style
  budget (`ods_features.MAX_LAYOUT_STYLES`, `LIMIT`), and auto-filtered ranges
  for at most 255 sheet names of up to 31 characters; ranges beyond those are
  counted for the workbook. Sample lists stop at 100. The tree reader runs the
  same scanners, so both readers reach the same outcome.

  What was not qualified: ODS at 1,000,000 cells with the standard mix could
  not be produced. DataHorizon's ODS export writes each text twice and every
  space as an explicit element (about 406 expanded bytes per cell for this
  mix), so its export of the 1,000,000-cell workbook is rejected with `LIMIT`
  by the 256-MiB expanded limit. ODS at 1,000,000 cells is therefore
  qualified only within that byte limit; it was measured with the same mix
  minus long text (112 MB expanded). ODS at 500,000 cells without long text,
  inline-string XLSX, OS-cold opening and other hosts were not measured. The
  opening budget and `MAX_CELLS` are unchanged.
- **Transport.** Import and export today carry the file as base64 inside a
  4-MiB JSON body. **Call**: add raw-body streaming upload to a staged
  preparation job and streaming download of a prepared export, used by the
  desktop, CLI and SDK alike so remote services work; the review step returns
  the report and a bounded sample, not every cell, and Apply refers to the
  staged job by id. The small base64 routes remain for selection scope.
- **Selection scope** (import into an existing sheet at an offset, export of
  a rectangle) stays on the existing 10,000-cell path. It carries cell-level
  content: values, formulas, styles and, from increment 5, attachments.
  Sheet-level features (merges, sizes, validation and conditional-format
  rules, defaults) travel only at whole-sheet and whole-workbook scope and
  are reported as omitted otherwise. **Call**
- **Hooks.** Whole-workbook import and the new additional-sheets import
  create structure; they raise the existing structural event and no per-cell
  change events. An activated required handler that has not opted into that
  event rejects the import before any change, as now. A destination with
  modules or bindings can accept additional sheets under the same rule.
  Selection-scope import keeps running ordinary change hooks. As of
  increment 1 the bounded inline `sheets` form of whole-workbook import also
  raises no per-cell change events for the cells it imports. **Call**
- **What large workbooks can do afterwards.** Ordinary structural edits and
  formula evaluation are bounded today in ways a large import would hit.
  Increments 9 and 10 (section 12) lift those. **Owner**
- Package safety checks (no extraction to disk, no external resources, no
  DTDs/entities, compression-ratio and node-depth bounds) stay and are
  re-derived for the larger limits.

### 5.3 Native format

- Each increment that adds storage upgrades the workbook on first use of the
  new feature, in the same transaction, following the pattern of v2–v6. Older
  readers reject the newer version. Expect v7 onward; the exact number is
  assigned when each increment lands.
- **Cell style** stays the JSON object on `cells.style`; new properties are
  additive and validated by `validate_style`.
- **Per-sheet objects** (merged ranges, validation rules, conditional-format
  rules, row/column default styles, sheet visibility, tab colour) go in
  `dh_objects` under new namespaces. **Call**. Opening currently rejects
  unknown namespaces and more than 256 `dh_objects` rows per workbook; the
  version gate admits the new namespaces and the row bound is re-derived for
  255 sheets and measured against the opening budget.
- **Per-cell attachments** (hyperlink, comment, rich-text runs) go in a new
  table keyed by `(sheet_id,row,col,kind)` rather than in `cells`, so plain
  cell reads and direct SQLite edits of `cells` are unaffected. **Call**
- All of it is plain SQLite with JSON payloads, documented in
  [the native format document](native-format-v1.md), validated on open, copy
  and external refresh within the opening budget, and inert (reading never
  runs code).
- Every new object must define what happens on row/column insert and delete,
  sheet delete/duplicate/rename, cut/paste, fill, sort and undo. An increment
  is not complete until those are specified and tested.
- A direct SQLite edit that leaves new metadata inconsistent with cells (for
  example a value violating a validation rule, or text under a merged range)
  is not repaired and not replayed. Validation is not retroactively enforced;
  merged ranges keep showing the anchor cell.

### 5.4 Clients and display

- The service computes display. The grid already renders the `display` string
  and style the service returns, so number-format rendering, conditional
  formatting and effective style resolution live in Python once and reach
  every client.
- Viewport responses carry the effective style (cell over row default over
  column default, then conditional formatting on top) plus the stored style,
  kept distinct.
- New engine operations get REST routes, OpenAPI entries, CLI commands and SDK
  methods in the same pull request as the engine change.

### 5.5 Export and ODS

- Every native feature added here exports to XLSX in the increment that adds
  it. Import-only is not complete.
- ODS import and export are added in the same increment where OpenDocument
  has an equivalent. Where it has none, the ODS report says so. **Call**:
  XLSX leads; ODS may land as the increment's last pull request.
- CSV is unaffected except that display-formatted export, if offered, uses the
  new format engine.
- A stored formula never blocks a whole-workbook export (section 5.1, *Exporting
  what was kept*): XLSX writes it verbatim and counts what DataHorizon cannot
  evaluate; ODS writes the value, or an empty cell, for a formula OpenFormula
  cannot carry, and reports it.

### 5.6 Verification

- **Corpus.** Current fixtures are generated by openpyxl and say so. This
  programme needs workbooks saved by real Excel and by LibreOffice. Increment
  1 creates `tests/fixtures/corpus/` with a provenance file (producer,
  version, what each file exercises) and a runner that imports every file and
  records blocked/degraded/clean counts per category. That scorecard is the
  programme's progress measure. LibreOffice files can be produced locally;
  Excel-saved files must come from the owner (section 14).
- **Round trip.** For each feature: import, export, re-import, compare native
  state; and an independent openpyxl read of the export.
- **Per feature.** Engine tests for storage, history, structure transforms and
  hook rollback; REST/CLI/SDK parity tests; Qt checks for rendering and the
  authoring command; native macOS capture where appearance matters.
- **Performance.** Each increment re-runs the opening and viewport budgets
  with the feature populated at its limit.
- Evidence is recorded under `docs/design/evidence/` as now, and
  [the compatibility contract](compatibility-v0.md) and
  [implementation status](implementation-status.md) are updated per increment.

## 6. Increment 1: import foundations

Delivers: real files open, at real sizes, with an honest report.

- The degrade policy of 5.1, including the engine's unevaluable-formula path
  and the `mixed` mode, for XLSX and ODS.
- Structured conversion report (5.1) through REST/SDK/CLI and grouped in the
  desktop review dialog.
- Streaming, staged import and export with the limits of 5.2 measured and
  recorded; import as additional sheets.
- Strict OOXML namespaces accepted alongside transitional.
- Error-valued cells with no formula import as Text holding the error string
  and are reported as converted. A native error-literal kind arrives with 8a;
  until then `ISERROR` over such a cell differs from Excel, and the report
  says so.
- The corpus, provenance file and scorecard runner (5.6).
- A clause-by-clause check of section 3 against ECMA-376 Part 1 §18, with any
  missed feature added to this document and assigned to an increment.

Done when: every corpus file imports without a blocking error in `mixed` and
`formulas` modes; the scorecard is checked in; the measured limits replace the
targets in 5.2; a 1,000,000-cell generated workbook imports, reopens and
exports within recorded time and memory.

**Status, November 11, 2026.** Built on branches `claude/excel-fidelity-1a`
through `1e`; verification in
[excel-fidelity-1-verification.txt](evidence/excel-fidelity-1-verification.txt).

| Criterion | State | Evidence |
| --- | --- | --- |
| Every corpus file imports without a blocking error in `mixed` and `formulas` | Met for the corpus as it stands: no file is rejected in any mode | [scorecard](evidence/excel-fidelity-scorecard.json), `tests/test_corpus.py` |
| The scorecard is checked in | Met; `known_unreported` is empty in every mode, so no listed feature is dropped without a report entry | [scorecard](evidence/excel-fidelity-scorecard.json), [corpus README](../../tests/fixtures/corpus/README.md) |
| The measured limits replace the targets in 5.2 | Met | [capacity evidence](evidence/excel-fidelity-1-capacity.json), section 5.2 |
| A 1,000,000-cell generated workbook imports, reopens and exports within recorded time and memory | Met for XLSX; ODS at 1,000,000 cells only within the 256-MiB expanded limit (5.2) | [capacity evidence](evidence/excel-fidelity-1-capacity.json) |
| The corpus holds workbooks saved by real Excel and by LibreOffice (5.6) | **Not met.** The corpus is 6 openpyxl/odfpy fixtures and 18 hand-built approximations; no Excel-saved file has been supplied (section 14, item 1) and LibreOffice is not installed on the reference machine. Open on the owner: supply files or waive in writing here | [corpus README](../../tests/fixtures/corpus/README.md), scorecard `summary.gaps` |
| The clause-by-clause check of section 3 | Met; additions are marked **Call** in section 3 and listed in section 14 | [coverage table](excel-spec-coverage.md) |

Increment 1 is therefore complete except for the application-saved corpus. The
measures above are only as representative as the hand-built files: a `clean` or
fully reported result on them says nothing yet about files Excel writes.

## 7. Increment 2: sheet geometry

- **Merged cells.** Native merged ranges per sheet, non-overlapping. The
  anchor (top-left) cell holds the value and style; other cells in the range
  hold nothing. Merging a range that has values outside the anchor requires
  explicit confirmation and reports what is discarded, as Excel does. **Call**
  Operations: merge, unmerge, list. The grid shows spans (`QTableView::setSpan`
  maintained for the loaded window in both frozen and scrolling panes);
  selection, navigation, copy/paste and fill treat a merged range as one cell.
  Structure edits move, grow or shrink ranges; an edit that would split a
  range is rejected with a clear error. **Call**
- **Sizes.** Import column widths and row heights onto the existing
  presentation settings, converting Excel character widths and points to
  pixels; export back. Raise the 1,024-entries-per-axis bound to a measured
  value and add a sheet default width/height. Store run-length ranges if
  needed to stay within the 256-KiB payload bound.
- **Hidden rows/columns and frozen panes.** Import and export onto existing
  settings. Excel allows more frozen rows/columns than the native 16/8; raise
  if the grid supports it, otherwise clamp and report.
- **Sheet visibility and tab colour.** New native sheet properties, shown in
  the sheet tab bar, with unhide in the desktop. Excel's "very hidden" maps to
  hidden and is reported. **Call**
- Outline grouping (row/column levels) is imported as hidden state only and
  reported; native grouping is not in this increment. **Call**

Done when: corpus scorecard shows no geometry omissions except outline
grouping; round trip preserves merges, sizes, hidden state, panes, visibility
and tab colour; structure-edit and undo tests pass for merged ranges.

## 8. Increment 3: wider cell styles

Additive style properties, each rendered by the grid and round-tripped:

- Vertical alignment (top, middle, bottom); horizontal `justify`, `fill`,
  `distributed`, `center across selection`; indent; text rotation (angles and
  stacked); shrink to fit.
- Underline (single, double, accounting variants map to single/double and are
  reported), strikethrough, superscript/subscript.
- Border colour per edge; diagonal borders; the remaining Excel line styles
  (double, hair, medium-dashed, dash-dot variants, slant). The existing
  `CellDelegate` paints borders in the palette text colour and is extended.
- Pattern fills (the 18 Excel patterns with foreground and background colour)
  and two-stop/multi-stop gradient fills.
- **Colours.** Theme, indexed and tinted colours resolve to RGB at import
  using the workbook's theme part and Excel's tint formula; the native format
  stores RGB only, and export writes RGB. The theme itself is not kept. **Call**
- **Named styles.** `cellStyleXfs` inheritance is resolved at import and
  flattened onto each cell; native named styles are not introduced. **Call**
- **Row and column default styles.** Stored natively per sheet so that a
  formatted column of a million empty cells costs one entry. Effective style
  is cell, then row, then column. Formatting a whole row or column from the
  desktop writes the default rather than every cell.
- The 1,024-distinct-styles export bound is raised to Excel's limit of 64,000
  cell formats, or a measured lower value.

Done when: every property above appears in the style contract, renders in the
native capture set, and round-trips; the corpus scorecard shows no style
omissions outside the non-goals.

## 9. Increment 4: number-format engine

- Native style gains a custom format: `format:"custom"` with the Excel format
  code stored verbatim. The existing named formats remain and are defined as
  aliases of codes, so nothing stored today changes meaning.
- A format-code parser and renderer in the service covering: up to four
  sections (positive; negative; zero; text), conditions (`[>100]`), colours
  (`[Red]`, `[Color n]`), digit placeholders `0 # ?`, thousands scaling,
  percent, scientific, fractions, literal text/escapes/padding (`_` and `*`),
  `@`, date and time tokens including `AM/PM`, elapsed `[h] [m] [s]` and
  fractional seconds, and the built-in format ids 0–49.
- A colour produced by a format section is returned as part of the effective
  style, not written to the stored style.
- Locale-dependent built-ins and `[$-409]`-style locale/currency tags: the
  currency symbol is honoured; the locale is ignored and reported. **Call**
- A code the renderer cannot interpret is kept verbatim, displayed as General,
  reported, and exported unchanged.
- **Serial dates.** Today XLSX import converts a serial number to a native
  date/time automatically when its format is one of five recognised codes.
  With arbitrary codes the rule becomes: the format engine classifies a code
  as date, time, datetime or elapsed; import gains an explicit option, native
  (default) or serial. Native converts every classified cell using the
  workbook's 1900/1904 system and keeps the format code for display; serial
  leaves the number and applies the format. Elapsed-time formats always stay
  numbers. The report states the count converted. **Call**
- Desktop: a Format Cells number tab with the common categories and a custom
  code field with live preview.
- Verification: a table of (value, code, expected text) taken from Excel's
  documented behaviour and from the Excel-saved corpus, where cached display
  strings are not stored but can be captured by the owner once; LibreOffice
  rendering is a second reference.

Done when: the renderer passes the reference table; corpus number formats
either render or are reported as uninterpreted with none lost on export.

## 10. Increments 5–7

### Increment 5: cell attachments

- **Hyperlinks.** Per cell: target (URL, mailto, or a location in this
  workbook), optional display text and tooltip. The desktop opens external
  targets only on an explicit user action and shows the target first; file
  and UNC targets are stored but not opened. **Call**
- **Comments.** Legacy notes and threaded comments both import as a native
  comment thread per cell: entries with author name, timestamp and plain text.
  Rich formatting inside comments is dropped and reported. Grid shows an
  indicator and a hover/side display; add, edit, delete from the desktop.
- **Rich text.** Runs (font properties per character range) stored as an
  attachment alongside the plain-text source, so SQL and formulas see plain
  text. Editing the cell's text in the grid drops the runs with a warning;
  rich-text editing in the grid is not in this increment. **Call**
- Limits per sheet and per workbook are measured and documented.

### Increment 6: data validation

- Native rules per range: type (whole, decimal, list, date, time, text
  length, custom formula), operator and operands, allow-blank, list source
  (inline or range), input message, error message and error style.
- Enforcement: a rule with error style `stop` rejects a violating managed
  cell edit through every managed path (grid, REST, CLI, SDK, paste, fill,
  import into an existing sheet, and edits generated by hooks), with a
  structured error naming the rule and cell. `warning` and `information`
  rules never reject; the violation is returned in the operation result.
  **Call**: this is stricter than Excel, which checks only typed entry;
  consistency across managed paths is the product's contract. See section 14.
- Validation runs after hook-generated edits are collected and before commit,
  inside the operation's transaction.
- Existing values are not checked when a rule is added or imported; a
  "circle invalid data" style read operation lists current violations.
- List rules show an in-cell dropdown in the desktop.
- Custom-formula and range-source rules need names and the formula engine in
  rule context, hence the dependency on 8a.

### Increment 7: conditional formatting

- Native rules per range with priority and stop-if-true: cell value
  comparisons, formula, text contains/begins/ends, blanks/errors, duplicate
  and unique, top/bottom N and percent, above/below average, date occurring,
  two- and three-colour scales, data bars and icon sets.
- Evaluated in the service for the requested viewport and returned in the
  effective style. Range-wide rules (top N, average, duplicates, scales) need
  a bounded, cached range statistic invalidated with the dependency engine;
  ranges beyond the measured bound report "not evaluated" rather than guess.
- Differential formats (`dxfs`) map onto the increment 3 style properties.
- Data bars and icon sets need the cell delegate from increment 3.
- Desktop: a rules manager (list, add, edit, reorder, delete) with the common
  rule types.

Each of 5–7 is done when its features round-trip, appear in the scorecard as
clean, and pass history, structure-edit and hook-rollback tests.

## 11. Increment 8: formula reach

Runs as stages alongside the others.

- **8a. Names and error literals.** Workbook and sheet-scoped defined names
  usable in formulas (reversing the Python workspace milestone's non-goal),
  imported and exported; names that refer to constants or formulas, not only
  ranges. Error literals as a native value kind. Excel table structured
  references (`Table1[Column]`) resolve against native lists created from
  imported `xl/tables` parts. **Call**: Excel tables become native named
  lists on the sheet, not SQLite structured tables, because their cells stay
  in the grid.
- **8b. Arrays.** Legacy array formulas (`t="array"`) and dynamic arrays with
  spill, `#SPILL!`, and the implicit-intersection operator. This changes the
  calculation model (one formula producing many cells) and gets its own
  design note before planning.
- **8c. Function catalogue.** Stages of roughly ten functions, as in the
  existing formula batches, chosen by how often they appear as "unevaluable"
  in the corpus scorecard. The first stages are expected to cover VLOOKUP,
  HLOOKUP, SUMPRODUCT, COUNTBLANK, AVERAGEIF(S), MAXIFS/MINIFS, TEXT, VALUE,
  INT, MOD, POWER, SQRT, EDATE, EOMONTH, DATEDIF, WEEKDAY, NETWORKDAYS,
  CHOOSE, OFFSET, INDIRECT and the remaining IS* and rounding functions.
  TEXT depends on increment 4.
- Excel's full catalogue (roughly 500 functions) is not a completion
  criterion. The criterion is the scorecard: the share of corpus formulas
  that evaluate, and agreement of results with the cached values in
  Excel-saved files.

## 12. Increments 9 and 10: working in large workbooks

### Increment 9: large structural edits

Today row/column insert and delete, sheet delete and duplicate, and the
formula-reference scans that accompany them stop at 10,000 affected cells.

- A large-job path for those operations, taken automatically when the
  bounded path's limit would be exceeded, through the same engine operation
  and the same REST/CLI/SDK calls. Small edits keep their current path,
  history and behaviour.
- A structural edit stays atomic: one transaction, no batched commits, since
  a half-shifted sheet is corruption. It runs as a background job with
  progress and cooperative cancellation; cancelling rolls back. **Call**
- Coordinate shifts are set-based SQL rather than per-cell Python. Formula
  reference rewriting streams over formula cells only; whether that needs a
  persisted index of formula cells is the builder's choice, decided by
  measurement.
- History: an edit on the large path cannot keep per-cell undo images inside
  the 16-MiB history bound. It clears Undo/Redo after an explicit
  confirmation that says so, as existing history-clearing edits do. **Call**
- Hooks: the existing opt-in structural event runs inside the transaction
  and can reject it. No per-cell change events are raised.
- Every per-sheet object and attachment added by increments 2 and 5–7 must
  implement its transform on this path as well as the bounded one. This is
  why 9 precedes them.
- Large sort, fill, paste and find/replace are not in this increment; the
  existing staged-job tier for those keeps its limits. **Call**

Done when: on a generated 1,000,000-cell sheet with cross-sheet formulas,
inserting and deleting rows and columns near the top, and deleting and
duplicating the sheet, complete within recorded time and memory; references
are adjusted correctly against an independent check; cancel and a forced
process kill each leave the workbook in the before or the after state.

### Increment 10: stored results and scaled recalculation

Today formula values are derived inside each read within 0.25 seconds, a
128-cell dependency chain, 10,000-cell ranges and a 4,096-entry cache, and
are never persisted. This increment changes the calculation model and gets
its own design note, reviewed by the owner, before it is planned. That note
must settle the points below; the Calls here are its starting position.

- **Stored results.** Calculated values are kept so that opening a workbook
  and scrolling it do not recalculate everything. **Call**: in the workbook
  file, in a reserved table documented as derived state, each result stamped
  so that staleness is detectable. External tools may read it, as they can
  read Excel's cached values, with the documented caveat that it can be
  stale. A sidecar file is the alternative.
- **Never trust a stale result.** The current guarantee, that reopening
  never trusts an old calculation cache, is kept in substance: a result is
  used only if its stamp proves the inputs are unchanged. After a direct
  SQLite edit, refresh invalidates derived state as today, without replaying
  change handlers.
- **Incremental recalculation.** An edit marks dependents dirty and
  recalculates them in dependency order, iteratively rather than by
  recursion, so chain depth is bounded by memory, not by 128. The viewport
  is calculated first; the rest continues in the background with progress,
  and cells not yet current are shown as pending, as now.
- **Large ranges.** Aggregates over ranges beyond 10,000 cells stream rather
  than materialise. Limits that remain are measured and documented, and a
  cell that exceeds one shows `#LIMIT!` and appears in the report.
- **Consistency.** A read returns values from one state; hooks reading
  inside a transaction see values consistent with that transaction's edits;
  values export uses stored results and blocks, as now, if any required
  result is not current.
- **Volatile functions** (`NOW`, `TODAY`) keep their present clock contract.

Done when: a generated workbook with 1,000,000 cells, 100,000 formulas and a
100,000-row running total opens and shows current values within recorded
budgets; a single edit at the top of the running total recalculates within a
recorded budget; kill-and-reopen never shows a stale value as current; and
calculated results agree with the cached values in the Excel-saved corpus
for every function DataHorizon claims.

## 13. Left to the builder

- Module boundaries. `interchange.py`, `engine.py` and `mainwindow.cpp` are
  already large; splitting XLSX reading, writing and each feature's mapping
  into focused modules is expected.
- Exact JSON shapes, namespace names, table and column names, route names and
  CLI verbs, consistent with existing conventions.
- The pull-request split inside each increment.
- Whether the grid's richer rendering uses one delegate or several.
- Pixel/character/point conversion constants, chosen to round-trip.

## 14. Open owner decisions

1. **Excel-saved corpus.** Can you supply 10–20 workbooks saved by real Excel
   that are representative of what you want to open, and that may be checked
   into the repository (or kept outside it and referenced)? Without them the
   scorecard is LibreOffice- and openpyxl-only.
2. **Validation strictness** (increment 6). The Call is that a `stop` rule
   rejects violating edits from every managed path, including the API and
   hook-generated edits. Excel only checks typed entry. Confirm or reverse.
3. **Look before calculate.** The order in section 4 does formatting
   increments before most of the function work. Reverse if calculation
   matters more.
4. **Size target.** 1,000,000 cells / 256 MiB per import is a Call. Name a
   different target if you have a specific workbook in mind.
5. **Coverage-check additions for the owner to confirm** (from the
   [coverage table](excel-spec-coverage.md); each is a **Call** in section 3):
   - Split panes, zoom, gridlines, right-to-left, custom views and the watch
     window are view state and a non-goal.
   - Scenarios, data consolidation and data tables (as what-if tools) are a
     non-goal; data-table formulas keep their cached values.
   - AutoFilter ranges move to increment 8a with Excel tables; a saved sort
     state is a non-goal.
   - Calculation settings (manual mode, iteration, precision as displayed) go
     to increment 10, which owns the calculation model.
   - Phonetic guides go to increment 5 with rich text.
   - Rich values in cells (pictures in cells, data types), ignored-error flags,
     smart tags, web publishing and custom sheet and document properties are a
     non-goal.
   - Chart sheets, dialog sheets and macro sheets are left out and reported; no
     native sheet is created for them (charts and macro sheets are non-goals).
   - Document core properties (author, created and modified dates) are not
     reported because every file has them. Decide whether they should be kept,
     reported or ignored.
   - ODS sheet tab colours and page styles (headers, footers) are not detected
     (see the coverage table). Decide whether they need detection.

## 15. Increment 1 as built: rulings and known limitations

This section is the durable record of the decisions taken while building
increment 1 and of what was left for later. Each ruling says what it costs if
it turns out wrong. Task numbers refer to the
[increment 1 plan](excel-fidelity-increment-1-plan.md).

### Process

- Work ran on stacked local branches (`claude/excel-fidelity-1a` to `1e`) in the
  main checkout, not a separate worktree, because the plan files and build
  directory lived there. Cost if wrong: the checkout was occupied for the
  duration; nothing else.
- Nothing was pushed and no pull request was opened without the owner's
  say-so, although the plan asked for one pull request per group. Cost if
  wrong: review on GitHub happens later than the plan's cadence.

### Degrade policy and the report

- Array and data-table formulas degrade instead of blocking: the anchor keeps
  its formula as unevaluable (`formula.array`), or takes its cached value in
  `mixed` mode. Cost if wrong: array anchors show an error where Excel shows
  results, until increment 8b.
- A formula that cannot be translated, imported at a destination other than A1,
  is skipped and reported as `formula.untranslatable` (valid content must not
  reject the file). Cost if wrong: such a cell is dropped (reported) instead of
  kept as text.
- A selection-scope export still refuses formulas the parser cannot read; only
  whole-workbook export writes them through. Cost if wrong: such a selection
  needs values export until 8a/8c widen the parser.
- Every worksheet and workbook feature that is not imported produces one
  `omitted` entry per feature (`sheet.feature` or `workbook.feature`, detail the
  feature id, count of items, the sheets as samples with no cell); features in
  their default form produce nothing (Task 21). The defaults recognised are
  those of Excel, older Excel and openpyxl, and LibreOffice (as hand-built from
  its documented output): page margins, page setup print choices, stock
  LibreOffice header and footer texts, view settings, a row height only when
  marked custom, a column width only when marked custom or more than a tenth of
  a character from the sheet's default width (or, without one, the width most
  columns share), and the default row height only when marked custom or zero.
  Cost if wrong: a default the scanner misjudges is either noise in every such
  file or, if judged default wrongly, silent; the default-file tests guard both
  directions only for the forms they contain.
- In `values` mode the scorecard requires no entry for formula-only features
  (arrays, data tables, 3D references): that mode replaces every formula by its
  cache by request and says so. Cost if wrong: the array-ness of a formula lost
  in values mode is not itemized.
- Comments are counted in the sheet's comments part (or threaded-comments part),
  streamed under the package byte budget; a missing or unreadable part counts 1.
  Cost if wrong: a damaged comments part understates the count; the entry is
  still present.
- Chart, dialog and macro sheets are not imported: each is one `omitted` entry
  (`workbook.feature`, `chart_sheets`, `dialog_sheets` or `macro_sheets`, the
  sheet as sample), no native sheet is created, and formulas that refer to one
  are references to a missing sheet (`formula.missing_sheet`). They are found by
  the relationship type of the workbook's `<sheet>`. Choosing one at selection
  scope is `INVALID_ARGUMENT`. A package with no worksheets at all prepares with
  nothing to apply (`can_import` false, as Apply of a workbook without sheets is
  refused). Cost if wrong: a sheet of an unusual kind declared with a worksheet
  relationship still rejects as an invalid worksheet.
- An x14 conditional-format rule that is the twin of a rule already in
  `<conditionalFormatting>` (Excel writes one per data bar) is not counted again.
  Cost if wrong: none for loss; a count could be one low if a twin's id is reused.
- Auxiliary parts read only for the report never reject a valid file: an absent
  or empty sheet relationship part declares nothing, any other unreadable one is
  reported (`relationships_unreadable`), and an unreadable ODS `settings.xml`
  skips pane detection and is reported (`view_settings_unreadable`). A DTD or
  entity in a sheet relationship part still rejects (an attack shape, not an
  unusual file). Cost if wrong: comments of a sheet with damaged relationships
  are reported only as unreadable relationships.
- An XLSX drawing is reported on the sheet that holds it (`drawings`); charts,
  pivot tables, VBA, custom XML and the other advanced parts are reported once
  for the workbook from the package part names. Cost if wrong: none for loss;
  the counts are of parts, not of objects.
- ODS column widths are reported only for columns whose width differs from the
  sheet's most common column width, because LibreOffice writes a width for every
  column. Cost if wrong: a sheet whose columns are all widened by the same amount
  reports nothing for them.
- The feature scanner landed after the capacity measurements of 5.2. In
  isolation it costs about 0.13 s per 1,000,000 cells on the reference Mac; the
  1,000,000-cell XLSX case was re-run with the bounded scanner after the final
  review and still qualifies (section 5.2). Cost if wrong: the ODS sizes, not
  re-run, are slightly optimistic.

### Formulas and references

- In `first:second!ref`, a `first` that is a valid in-grid cell address reads as
  a cell ranged with `second!ref`, since an unquoted sheet name cannot look like
  a cell address. Cost if wrong: a 3D reference between sheets literally named
  like cells (unquoted) is misread.
- External references are untouched by structural edits, moves and sheet
  renames or deletions, but their relative parts shift under copy, fill and
  shared-formula expansion, as in Excel. Cost if wrong: copied external
  references point one row off.
- The receipt of a structural edit reports marked formulas as top-level
  `unmaintained` and `unmaintained_count`, following the existing receipt shape.
  Cost if wrong: clients read a different key.
- Operation-job staging does not accept the `unmaintained` kind; clients cannot
  author it. Cost if wrong: staged jobs cannot carry such cells.
- `has_sheet_reference` was left unchanged in Task 8, since no service caller
  needed it changed; Task 9 then replaced the conservative block that used it.
  Cost if wrong: Task 9 had to touch it (it did).

### Large import and export

- The streaming reader caps one cell or shared string, or any other record at
  the record depth (an `extLst` child included), at 1 MiB of XML and rejects the
  file with `LIMIT` beyond it, to bound memory on untrusted input. Cost if wrong:
  a valid file with a larger extension child or cell is rejected, not degraded.
- The ODS streaming reader accepts trailing empty row and column repeats beyond
  the grid, which real files commonly end with; the tree reader rejects them.
  Cost if wrong: the two readers disagree on that one edge.
- `LARGE_CELLS` stays at 1,000,000 for both formats; ODS at that size is
  qualified only when its ODS form fits the 256-MiB expanded limit. Cost if
  wrong: a text-heavy 1,000,000-cell workbook exports to XLSX but fails ODS
  export with `LIMIT`.
- The engine's staged import takes a stage file path resolved by the service
  and stays unaware of staging. Cost if wrong: a small refactor.
- The review summary always truncates sample cell sources to 256 characters.
  Cost if wrong: a review shows clipped text even for small files.
- Releasing an upload that is still receiving returns 409 `JOB_CONFLICT`. Cost
  if wrong: a client must wait before releasing.
- `source_path` is accepted as a local-use input (documented, capability
  `import_source_path`). Cost if wrong: a token holder can learn whether a file
  readable by the service exists.
- The bounded inline `sheets` form of whole-workbook import also raises no
  per-cell change events, matching 5.2 Hooks. Cost if wrong: existing
  `on_change` handlers no longer see those imported cells.
- The XLSX stylesheet is written last in one pass; the output equals the
  bounded exporter's byte for byte. Cost if wrong: a reader that needs styles
  before sheets would fail (none found).
- The export snapshot copy holds the workbook lock for its whole duration, but
  is capped at 2 GiB, checked for free space, cancellable and inside the
  300-second budget. Cost if wrong: editing is blocked while a large workbook is
  copied.

### Clients

- The CLI treats standard input at end of input as non-interactive (exit 2,
  nothing applied) and honours a piped `y`/`n`. Cost if wrong: a script piping
  `y` imports without `--yes`.
- The desktop offers "Replace this empty workbook" from a 256 × 16 cell probe
  plus catalog checks; the service's `DESTINATION_NOT_EMPTY` is the backstop.
  Cost if wrong: the option is offered for a workbook with cells far from A1 and
  Apply is then refused with a message.
- The SDK's `prepare_spreadsheet_file` and the CLI's `import-spreadsheet`
  default to mode `mixed`, matching the desktop's preselection, while the REST
  preparation route defaults to `formulas` when no mode is sent. This is
  intended and stated in the SDK docstring, the CLI help and the automation
  guide. Cost if wrong: a script that omits the mode gets cached values for
  formulas DataHorizon cannot evaluate, where a raw REST call keeps them.
- Every preview, review summary and export result carries `scope_lines`, the
  fixed scope statements its `report` starts with; clients read them and slice
  `report` only when talking to a service that omits them. The desktop counts
  with `tr("%n …(s)", nullptr, n)`, resolved for English by a small built-in
  translator (`desktop/englishplurals.h`) until real translations exist. Cost if
  wrong: a translation file must supply the numerus forms instead.

### Final review fixes

A whole-branch review after Task 21 reproduced four defects and listed smaller
items; all were fixed in one pass.

- **Feature-scanner memory.** The scanners kept every `<col>` width, every x14
  rule id, every relationship type and every ODS validation name, outside the
  reader's record bounds: a 498-KB XLSX of 16,000,000 `<col>` elements was
  accepted at about 1.76 GiB RSS, a 7.7-MB ODS of 3,000,000 distinct validation
  names at about 466 MiB. Every accumulator is now bounded (section 5.2): widths
  are counted per distinct width, impossible counts are `LIMIT`, report-only
  distinct counts saturate with `at_least`. In `tests/test_feature_bounds.py`
  every crafted input stays under 64 MiB traced and 192 MiB RSS growth and
  finishes within 20 s, among them 2,000,000 `<col>` elements (rejected) and
  500,000 distinct validation names (imported with a saturated count). Cost if
  wrong: a valid sheet with more than 65,536 shared-formula groups open at once
  (extents overlapping the same rows) or 65,536 ODS layout styles is refused
  with `LIMIT`; a saturated count understates.
- **Shared-formula extents and group counts.** Both readers rejected a
  shared-formula extent over 10,000 cells (a cap added with shared-formula
  expansion, mirroring the bounded path's `MAX_CELLS`, though no code ever
  iterated an extent), so an ordinary fill-down longer than 10,000 rows did
  not import in `formulas` or `mixed` mode; and the streaming reader counted
  every group of a sheet against `SHARED_GROUPS`, so a ledger with one group per
  row failed at row 65,537. An extent may now be any rectangle of the grid that
  holds its master. The streaming reader releases a group once a new master
  would exceed `SHARED_GROUPS` or `SHARED_BYTES` and the rows have passed the
  end of its extent; below the bounds nothing is released, so such files read
  exactly as before. Released keys are remembered in a 12.5-MB bitmap (keys
  are at most 8 digits), so a later member of a released group fails as before
  ("lies outside its master extent") and a reused key is still a duplicate
  master. A group with members stored before its master is kept to the end of
  the sheet. Neither reader requires ascending rows; every file Excel writes
  has them. Cost if wrong: a sheet over the bounds whose rows are out of order
  and that names a released group in an earlier row is `LIMIT` (the message
  says rows must ascend), where a reader keeping every group would import it.
  `tests/test_feature_bounds.py` imports 69,632 one-row groups correctly at
  about 43 MB traced, and rejects 300,000 groups that all run to the last row
  with `LIMIT` at about 30 MB traced.
- **Export after a `formulas` import.** Whole-workbook export passed through only
  formulas whose sole problem was an unsupported function, so one `=Net*TaxRate`
  blocked every export route of a workbook imported in `formulas` mode. Section
  5.1 *Exporting what was kept* now holds. Cost if wrong: an ODS export drops an
  untranslatable formula to its value or an empty cell (reported).
- **Staging left behind.** The service had no SIGTERM handling and the SDK and
  desktop stop it with terminate, so `server_close()` never ran and uploads,
  stages, exports and snapshots (up to 2 GiB) stayed in the temporary directory.
  SIGTERM, SIGINT and, on Windows, SIGBREAK now end `serve_forever` like Ctrl+C,
  so the orderly shutdown runs (jobs cancelled and closed, staging removed,
  workbooks closed) and the process exits 0 within `LocalService.close`'s 3-s and
  the desktop's 1.5-s wait. A staging directory holds an owner marker
  (`.datahorizon-staging`: magic, pid, creation time); at startup the service
  removes only marked directories named `datahorizon-<pid>-*` in the system
  temporary directory, owned by its user, whose pid is no longer running. A live
  pid, even if reused by another program, keeps its directory; unmarked
  directories and caller-supplied roots are never touched; `Staging.new` after
  `close()` is `SERVICE_STOPPING` (503). On Windows `Popen.terminate` and
  `QProcess::kill` end the process without a signal, so there the next
  service's sweep removes the directory. Cost if wrong: a directory whose pid was
  reused waits until that process exits.
- **Leftover directories on the reference Mac.** Before this fix the review
  found thousands of `datahorizon-<pid>-*` directories in the temporary
  directory; 7,737 were present (127 not empty) when this pass started, and
  8,184 (133 not empty) at its end: the pass's own test runs of the old shutdown
  code added 447. None has the owner marker, so the product's sweep never
  removes them; they were not deleted by hand. The owner decides whether to
  remove them.
- **Recovery inspection.** `/v1/recovery/inspect` strict-parsed every formula
  cell, so a degraded import was reported as invalid input. Formulas the
  calculator cannot evaluate but the scanner reads, and `unmaintained` cells,
  are now valid inputs counted under `unevaluable_formulas` and
  `unmaintained_formulas` ([native formats](native-format-v1.md#bounded-recovery-validation)).
  Cost if wrong: `=SUM(` (tokenizable, not parseable) no longer counts as an
  issue; only sources the scanner cannot read do.
- **Smaller items.** Both readers emit the fixed scope lines first, then one line
  per entry, for XLSX and ODS, and every result carries `scope_lines`. The tree
  reader's 64-sheet limit counts only worksheets, and a selection can name any
  worksheet's position. The desktop review pluralises its counts, and a close
  before the capabilities reply is resumed on a zero-delay timer instead of
  being dropped. The ODS import job's OpenAPI text states that the large-path
  reader accepts trailing empty repeats beyond the grid. A `READ_ONLY` refusal,
  which happens after the stored receipts are consulted and before any
  transaction, is now `rejected` with the request's `operation_id`; other
  admission refusals keep their outcome (`OPERATION_ID_REUSED`: the stored
  receipt belongs to a different request with that id; a failed file identity
  check: the receipt cannot be consulted; storage errors: unknown by nature).
  `docs/design/compatibility-v0.md` no longer says that a DTD or entity in ODS
  `settings.xml` rejects the file.

### Corpus

- Task 20 closed with the Excel-saved and LibreOffice-saved requirements not
  met: LibreOffice is not installed, no owner files were supplied, and nothing
  is installed without the owner. Cost if wrong: none hidden; section 6 keeps the
  criterion open.

### Known limitations

Defects and gaps found while building increment 1 and left for later, grouped
and de-duplicated; items already fixed are not listed.

- Document core properties and a cell's own `extLst` are not reported
  (section 14); ODS sheet tab colours and page styles are not detected.
- The large-path capacity was re-measured after the feature scanner and its
  bounds only for XLSX at 1,000,000 cells (section 5.2); ODS was not re-measured.
- Default detection is heuristic and covers only the producers named in the
  rulings (Excel, older Excel and openpyxl, LibreOffice as hand-built); files
  from other producers, or real LibreOffice-saved files that differ from the
  hand-built approximation, may show noise or miss a setting.
- The `theme_colours` scorecard check accepts the general font and fill
  messages, which mention theme colours among other causes.
- The `_xlfn` future-function table was written from knowledge, not checked
  against Microsoft's list.
- An unsupported function inside an array formula is reported only as
  `formula.array`. The ODS unsupported-syntax entry has an empty detail, so a
  kept cache cannot be told from text kept; matrix detection in ODS is loose.
- A missing or cyclic ODS style reference on an imported cell rejects the file,
  while one on a skipped cell goes unnoticed. A single wrong-variant styles or
  shared-strings relationship in an otherwise consistent XLSX package is
  ignored instead of rejected. The tree reader still accepts non-UTF-8 XML
  declarations; a ZIP that lies about its sizes surfaces as
  `INVALID_SPREADSHEET`, not `LIMIT`.
- Deleting an end sheet of a 3D span gives `#REF!` where Excel shrinks the span;
  a reversed whole range such as `XFD:A` clamps; `=IFS([1]!MyName,A1)` cannot be
  scanned. Any formula on an edited sheet that cannot be scanned is marked
  `unmaintained` even if its references would not have changed. Full paste can
  create an `unmaintained` cell from client clipboard data.
- In the streaming reader, a shared-formula member stored before its master is
  converted at the end of its sheet, so the samples and tie order of its report
  entries can differ from the tree reader's.
- A full report (200 entries × 100 samples) can reach about 2.4 MiB before the
  stage summary trims samples. `ConversionReport.add` accepts `True` as a count.
- Service: an oversized `Content-Length` returns 400 with an interpreter
  message; a 405 lacks `Allow`; the `/docs` explorer cannot send `PUT`; a retry
  racing job expiry ends `JOB_NOT_FOUND`; an unfinished stage opens as
  `INVALID_STAGE`; a finished stage keeps freed pages; the stage version was not
  bumped when normalisation changed; the writer lock can be held up to 300 s
  while a stage loads, and job retention expiry can delete a stage being read
  (harmless on POSIX); an exception in `on_commit` would report a committed
  import as `OUTCOME_UNKNOWN`; admission refusals other than `READ_ONLY` (for
  example `OPERATION_ID_REUSED`) are still reported with an unknown outcome.
- Interchange round trips are not lexically exact: numbers can come back as
  float text (`42` as `42.0`) and ODS formula text is rewritten. A large export
  gives no date display to an unformatted formula with a temporal result
  (reported as approximated).
- Windows has not been exercised: releasing during a download, shutdown during a
  copy and sweeping an open file may fail there. The CLI has not been run
  against a long-lived remote service; `publish_file` copies setuid/setgid bits.
- Desktop: the single-sheet review still shows the text report; there are no tests for a
  session change mid-import or a failed replace-target probe; two cancel tests
  are timing-sensitive; `mainwindow.cpp` grew by about 450 lines.
- Measurement: the limit-consistency test checks the hand-written decision, not
  each run's verdict; RSS excludes compressed and swapped pages; packages of
  48–64 MiB were never measured; an RSS-growth test can pass vacuously.
- Code structure: `formula_admission` duplicates `fold_name` and the formula
  walk; about 240 lines of reference fallback helpers live in `formula.py`;
  `ods_stream` uses private helpers of `xlsx_stream`; preparation logic lives in
  `service.py`.

