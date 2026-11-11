# Excel fidelity programme: design and increment plan

Draft 0.2 | November 11, 2026 | Status: approved by the owner on November 11, 2026; section 14 decisions remain open

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
and strict. The gap table in section 3 was written from the author's knowledge
of that standard and from reading the importer; it was not checked clause by
clause against the published text. Increment 1 includes that check.

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
`desktop/sheetmodel.cpp` on November 11, 2026.

| Area | Today | Needs |
| --- | --- | --- |
| Numbers, Booleans, strings, ISO dates, serial dates | Imported | — |
| Error-valued cells | Block the import | Importer, engine literal |
| Formulas | Ordinary and shared, if every function is one of 53 | Degrade policy; more functions |
| Array formulas, data tables | Block | Engine |
| Defined names | Native names exist (v6) but not in formulas or interchange | Engine, importer |
| External references | Block | Degrade policy |
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
| Strict OOXML variant | Rejected | Importer |
| Size | 10,000 cells, 64 sheets, 3 MiB zipped, 16 MiB expanded | Streaming, staged import |
| Failure mode | One unsupported cell rejects the file | Degrade policy |

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
- Targets to qualify, to be replaced by measured values: 1,000,000 stored cells
  and 256 MiB expanded per import or export, 255 sheets, with progress and
  cooperative cancellation throughout. **Call**. If measurement on the
  reference Mac shows a target cannot be met inside the existing service
  memory target, the limit is set at what was measured and the document says
  so.
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
