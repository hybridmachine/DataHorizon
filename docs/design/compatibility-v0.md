# Development compatibility tier 0

This is a narrow development surface, not Excel compatibility certification.
Native inputs and styles survive reopen; results are calculated within the
current read transaction. Work remains before a complete everyday preview.

## Structured table tier

Native v4 tables preserve typed values and stable table/column/record identities.
Reviewed selection creation freezes formula values and retains source sheets;
table values do not introduce worksheet formulas or structured references.
Atomic schema reviews support renaming, nullable-column addition, explicit column
deletion, deterministic type conversion and required/unique constraints. Required
means non-NULL; empty Text remains distinct. Text equality is case-sensitive.
Lossy/invalid conversion and duplicates block Apply with record locations.

Rebuilds preserve row identities and the AUTOINCREMENT high-water mark, and clear
session history only on commit. Data-affecting conversions dispatch transactional
table-change events alongside structural events; hooks can inspect the prior
schema with `context.table_schema(table, phase='before')`. Populated table drop
requires explicit data-loss review. Rebuild/drop operations retain the ordinary
10,000-field / 8-MiB before/after / five-second tier. Typed record operations share
cell/record hooks and Undo/Redo. Full-table typed sort/filter uses authenticated
keyset pages; registered indexes preserve dependencies. Relationships use native
same-type RESTRICT foreign keys and owned indexes. Self/cyclic graphs and parent
rebuilds with incoming relationships retain explicit restrictions. Restricted
repair handles admit reviewed orphan fixes without replaying skipped handlers.

Read-only SELECT/CTEs, joins, aggregates and named typed bindings run on private
snapshots. Complete bounded query spools preserve label order, duplicate labels
and exact large-integer tags. Saved definitions retain stable IDs, declarations,
explicit defaults, dependencies and result shape. Opening a definition does not
run it. Saved views are named definitions/results, not SQLite CREATE VIEW objects
or editable/materialized tables. SQL mutations remain unavailable.

Table/filter and completed-query CSV/XLSX/ODS export reads the complete frozen
scope, independent of the displayed page. This first interchange tier allows
10,000 values including headers and a 4-MiB response; larger exports reject
without truncation. Exact integers with more than 15 decimal digits use reviewed
Text fallback. Tables retain Boolean/temporal kinds; SQL expressions retain raw
SQLite scalar kinds. CSV is untyped, collapses NULL/empty Text and has no temporal
companion in this relational tier. XLSX/ODS retain supported explicit values and
literal Text. Reports name actual omitted native objects. Native Save/copy keeps
definitions, keys, relationships, indexes and data.

The [integrated verification](evidence/relational-task10.json) covers the complete
Customers/Orders example through managed REST, GUI, CLI and SDK, including a
required fixture handler and explicit independent-edit/orphan repair. Independent
CSV, openpyxl and odfpy consumers verify the complete baseline export. This is a
qualified local automated tier; physical GUI/VoiceOver, Windows/Linux and
clean-machine release acceptance remain pending. See the
[manual guide](relational-workbench-manual.md) for reproducible steps.

## Workbook Python tier

Native v5 preserves bounded Python modules and explicit typed scalar macro
declarations. Stored code is inert on open/read/copy. Approved short atomic
macros execute through REST, CLI and SDK, with required handlers and supported
sheet/table Undo/Redo. The executing service grants exact code fingerprints;
permissions do not travel in workbook metadata. CSV/XLSX/ODS omit executable
source and report native-only omissions. The
[automation guide](python-automation-guide.md) specifies limits and the database
rollback boundary for trusted Python. Custom worksheet functions, package
installation and durable external-effect execution remain separate increments.

Native v6 adds workbook names and event bindings; see the
[workspace guide](python-workspace-guide.md). Both are native-only. Names
resolve in Python, binding scopes and the address box, not in formulas. CSV
carries neither. XLSX and ODS exports, at workbook or selection scope, omit them
and list each name and binding in the report as a native-only omission; a
selection CSV export's report lists them the same way. Table, query,
declared-list and list-view value exports do not list them. XLSX/ODS import
does not create names or bindings. Names that look like R1C1 references
(`R1C1`, `R2`, `C3`) cannot be created, so a later formula or interchange
mapping cannot confuse them with cell references.

## Formula surface

`+ - * / ^`, scalar Text concatenation `&`, unary signs, parentheses and comparisons `= <> < > <= >=` are
supported. Power is right-associative and binds more tightly than unary signs
(`-2^2` is -4). Numbers use finite binary64. Overflow/nonreal results are #NUM!;
division by zero is #DIV/0!. Text is not implicitly converted to a number in
arithmetic; blanks become zero and booleans become 0/1. Numeric arithmetic
includes unary signs, so `+TRUE` is numeric 1 in this tier;
SUM can consume that number while still ignoring unconverted boolean arguments.
Comparisons of blank with
blank or numeric/boolean values use zero for the blank operand. Other mixed-type
comparisons are errors, including direct Boolean/number comparisons. Explicit
numeric conversion such as `+TRUE=1` is permitted. Text comparison is case-sensitive. Formatting an empty
auto cell does not turn it into empty Text; explicitly entered empty Text remains
distinct and retains text coercion rules.
Binary operations on ranges are #VALUE! because array expressions are outside
this tier. Overlong or out-of-grid reference rows are #REF!; these errors remain
local to the formula cell and do not fail the rest of a viewport.
Non-finite literals such as `1e9999` produce #NUM! when evaluated, including as
comparison operands or IF conditions. Lazy IF still skips unselected branches.
SUM rejects overflow as it accumulates, including when used inside a comparison
or condition; a caller cannot hide a non-finite subtotal behind a finite result.

References support A1, $A1, A$1 and $A$1, rectangles and quoted sheet names such
as `'Other sheet'!A1`. Quoted sheet names escape an apostrophe by doubling it.
Copy/paste and fill translate relative components, preserving absolute ones and
quoted string contents. Unsupported transformations reject before any commit.
Whole-row/column insertion/deletion adjusts both relative and absolute targets
across sheets. Ranges expand for inserted interior members and contract around
deleted members; deleted targets and overflowing references become #REF!.
Parenthesized range endpoints retain the same membership and sheet inheritance.
The [initial contracts](initial-contracts.md) define endpoints, identity, bounds
and source-preserving rejection for unsupported syntax.
Cross-sheet ranges must remain within one sheet. Whole-column ranges, named
ranges, structured references, external workbooks and 3D ranges are unsupported.

| Function | Signature and behavior |
| --- | --- |
| SUM | Zero or more scalar/range arguments; add numeric values, ignore text, blanks and booleans; propagate referenced formula errors |
| AVERAGE/MIN/MAX/COUNT/COUNTA | 1–255 arguments; numeric/reference coercions, error counting and bounded behavior follow the [new formula tier](spreadsheet-next-ten.md#aggregate-function-contract) |
| AND/OR/NOT | Numeric/Boolean logical evaluation, explicit Text/reference rules and eager aggregate errors; see the [logical tier](spreadsheet-next-ten.md#logical-function-contract) |
| IFERROR | Two scalar arguments; lazy fallback for ordinary spreadsheet errors, with cycle/resource failures kept visible; see the [IFERROR tier](spreadsheet-next-ten.md#iferror-contract) |
| ABS/ROUND/ROUNDUP/ROUNDDOWN | Scalar numeric operations, explicit decimal rounding and [-15,15] integer digit bounds; see the [numeric tier](spreadsheet-next-ten.md#numeric-function-contract) |
| LEN/TRIM/UPPER/LOWER | One scalar; explicit invariant text conversion, Unicode code-point length/casing, ASCII-only TRIM, 32,767-character/64-KiB results; see [text tier](formula-next-ten.md#scalar-text-cleanup-tier) |
| LEFT/RIGHT/MID | Unicode scalar substrings, optional LEFT/RIGHT count, 1-based MID start, explicit numeric count rules and bounded results; see [substring tier](formula-next-ten.md#substring-tier) |
| CONCAT | 1–253 scalar/range arguments, row-major flattening, invariant Text and bounded incremental results; see [concatenation tier](formula-next-ten.md#concatenation-tier) |
| ISBLANK/ISNUMBER/ISTEXT/ISLOGICAL/ISERROR/ISNA | Scalar type/error inspection without coercion; cycle/resource failures remain visible |
| INDEX/MATCH | Scalar bounded INDEX, typed forward exact MATCH with explicit mode 0; no arrays/approximate/reference-valued results |
| COUNTIFS/SUMIFS | 1–127 aligned AND criteria, strict same shapes, matching-only sum reads, shared work bounds |
| TEXTJOIN | Scalar delimiter/flag, row-major ranges, explicit empty fields and bounded output |
| SEARCH | Unicode-folded wildcard scalar Text search, original code-point positions and bounded matcher |
| FIND | Case-sensitive literal scalar Text search, 1-based Unicode positions |
| REPLACE | Positional Unicode scalar Text replacement/insertion, bounded output |
| SUBSTITUTE | Literal non-overlapping scalar Text replacement, optional 1-based occurrence, preflighted result bounds |
| EXACT | Case-sensitive invariant scalar Text equality, no Unicode normalization or formatting |
| IFNA | Two scalar expressions, lazy fallback only for #N/A; other errors/cycles/limits propagate |
| XLOOKUP | Typed exact forward first match, lazy scalar fallback, same-shaped vectors; see [lookup tier](formula-next-ten.md#exact-lookup-tier) |
| COUNTIF/SUMIF | Explicit bounded criteria, Unicode-folded/wildcard Text, numeric Text comparisons, aligned same-shape sums and lazy matching sum reads; see [conditional tier](formula-next-ten.md#conditional-aggregate-tier) |
| IF | Two or three arguments; numeric/boolean/blank condition; evaluate only the selected branch; omitted false branch is FALSE |

Double-quoted strings and TRUE/FALSE are supported. Unsupported function or
arity is #NAME?. Invalid syntax/coercion is #VALUE!; broken addresses are #REF!,
cycles are #CYCLE!, and resource exhaustion
is #LIMIT!, including runtime recursion exhaustion. Numeric overflow remains
#NUM!. Errors retain editable source and never become an invented value.
An export with any selected formula error is blocked. Evaluation budgets include
AST work as well as referenced cells, so not every 10,000-cell dependency shape
fits the initial limit. Managed reads/transactional hook reads retain bounded
evaluation. The GUI uses cached preview inputs plus automatic bounded read-only
[viewport calculations](viewport-calculation.md); state, sheet, model and stream
generation checks prevent older results from overwriting a newer input state.

## Import and export

| Feature | Native v1/v2 | CSV import / export | XLSX | ODS |
| --- | --- | --- | --- | --- |
| Typed values | Preserved; temporal inputs upgrade to v2 | Native temporal column conversions; optional hash-bound type companion | Native ISO dates/times; supported formatted serials use 1900/1904 epochs | Native ISO dates/datetimes and sub-day clock durations |
| Leading-zero text | Preserved | Preserved as text; CSV receivers may infer types | Preserved as Text | Preserved as Text |
| Formula source | Preserved | Literal Text / evaluated values | Every formula kept (unevaluable ones reported) or replaced by caches by mode | As XLSX; canonical OpenFormula syntax; syntax it cannot translate (such as powers) is kept as text or replaced by the cache, and reported |
| Supported styles | Preserved | Destination styles retained / omitted | Native direct styles mapped; unsupported display reported | Native direct styles mapped; unsupported display reported |
| Sheets | Preserved | At selected cell / selected rectangle | One sheet at destination, or the whole workbook (bounded or large path) / one rectangle or the whole workbook | As XLSX |
| Code / queries / relationships | Planned metadata | No code activation / omitted | Omitted and reported; no code activation | Omitted and reported; no code activation |

CSV import supports explicitly selected UTF-8 (optional BOM), UTF-16 (required
BOM), Windows-1252 and Latin-1, with strict decoding. The GUI offers comma,
semicolon, tab and pipe delimiters; the API accepts one character excluding
quotes, NUL and line endings. Import preview is bounded by 10,000 cells/records,
3 MiB of raw bytes and decoded UTF-8, 64 KiB per cell and a 4-MiB complete JSON
request/response. Text is the default. Explicit per-column Number/Boolean uses
native strict conversion and precision rules; dates require YYYY-MM-DD,
M/D/YYYY or D/M/YYYY and normalize to native ISO dates. Explicit ISO time/datetime columns are supported. A header option retains the
first record as Text. Invalid conversions block Apply and never fall back to
Text; users can explicitly change the type and review again. See
[CSV controls](editing-csv-next-five.md#csv-encoding-and-delimiters) and
[typing details](editing-csv-next-five.md#csv-column-typing).
Exports use round-trip-safe numeric text rather than the grid's display rounding.
Empty text, NULL and absence all map to
an empty CSV field; this loss is inherent in the initial values-only tier.
Native snapshot copies preserve the distinction. TSV clipboard input currently
uses literal tab/newline separation; embedded delimiters require CSV import.

Percentage presentation now has an explicit [percent2 tier](spreadsheet-next-ten.md#percentage-format-contract), preserving numeric fractions and native input types through formatting, history and XLSX/ODS.

## Editing and presentation

Supported: typed entry, full/value/format paste, copy/fill reference offsets,
explicit numeric series, cell/range Undo/Redo, source-preserving styles, bounded
sheet duplication/reorder, and limited rename/delete. Cut is an atomic same-sheet
move with bounded reference retargeting; see the [move contract](editing-lists-next-ten.md#formula-aware-cut). Structure
operations clear Undo/Redo and are restricted with active required handlers.
Cut rejects values-only and formats-only paste; Copy supports both. Absent
clipboard cells clear inputs while values-only paste retains destination styles;
explicit empty Text remains text. Values-only paste rejects formula errors or
pending results atomically.
Full paste preserves stored input kinds independently of copied number formats;
a number displayed with Text formatting remains a number, and apostrophe-escaped
Text retains its escape and displayed value.
The grid and formula bar preserve explicitly typed Text during edits, including
CSV-imported identifiers and formula-looking text. Automatic entry remains
available for cells stored with automatic typing.
Sheet rename retargets qualified names across sheets while retaining stable UUID
references. The bounded parser, source/scan limits and required-hook exclusions
are recorded in [the editing contract](editing-csv-next-five.md#sheet-rename).
Sheet duplication retargets copied explicit self-references (names and UUIDs),
leaves unqualified references local to the copy, and preserves other-sheet
targets. Only the copied cells are scanned; unsupported copied formulas reject.
Sheet deletion replaces surviving references to the deleted sheet (including
UUIDs and inherited ranges) with permanent #REF! errors and reports locations.
Unsupported surviving syntax rejects the operation. Deleted inputs and rewritten
formulas share the 10,000-cell limit. See [the deletion contract](editing-csv-next-five.md#sheet-deletion).
Literal exclamation marks in strings
and formula error tokens such as #REF! are not sheet separators; copied broken
references retain their error without blocking these operations.
Bounded row/column insertion/deletion is available from the Sheet menu and the
shared operation API, with confirmation and broken-reference locations. It
clears history and rejects activated required handlers, unsupported formula
syntax, oversized operations and insertion that would discard stored cells.
Wrap/borders are native styles; sizing, hiding and freezing use local view state
as described below.

Lists are explicit presentation projections over source row coordinates, capped
by the 10,000-cell ordinary scope. Sorting is stable with source row as tie-break;
up to eight keys and eight AND filters are accepted by REST and the GUI
[criteria editor](editing-lists-next-ten.md#expanded-criteria-editor). Type order is blank, number, text, boolean; text comparison is
case-sensitive. A list header is explicit. Filter edits can remove a record from
the view; Clear restores the source sheet. Range operations on projections are
blocked. Portable native list definitions and a stable record-ID schema are later work.
Saving an edited cell keeps its source row and column selected through resorting
while that row remains visible. If a filter removes it, select the first
remaining row. Invalid non-object filter/sort criteria return a validation error.

Find supports case-sensitive literal substring search over source or display,
returning a full-scope count and state-checked pages of up to 500 locations in row-major order. Later pages require the initial state. Replace All runs
in one managed transaction. Display replacement supports Text only and preserves
its type and apostrophe escape. Unchanged replacements do not rewrite inputs.
Use source replacement for numbers, booleans and formulas; their displays may
be rounded or derived.
Regular expressions and case-folded replacement are not implemented.

Text wrapping is a stored boolean style. The GUI measures loaded visible preview
text within 64 rows × 16 columns and caps automatic row height at 260 pixels;
full text remains available in the source editor.

Row/column sizing is stored in local desktop preferences per path/workbook/sheet,
not native workbook metadata. Fit measures only loaded visible previews; see
[the sizing limits](everyday-next-five.md#row-and-column-sizing).

Hide/unhide is a local source-coordinate preference, with marked gaps and Show
all controls. GUI range operations intersecting hidden sections are blocked;
CSV import requires unhidden sections. Managed API coordinates are unchanged.

Freeze top row supports one editable source row with shared cached values,
synchronized columns and local persistence. It is suspended in projected lists
or while row 1 is hidden. Cross-pane source-rectangle selection and Freeze first column (with an editable
intersection) are implemented. Multiple frozen rows/columns are bounded to 16/8
and available from the active cell; see [pane semantics](editing-lists-next-ten.md#freeze-selected-panes).

### XLSX and ODS input tier

Increment 1 of the [Excel fidelity programme](excel-fidelity-plan.md) (built
November 11, 2026; [section 15](excel-fidelity-plan.md#15-increment-1-as-built-rulings-and-known-limitations)
records its rulings) replaces the earlier block-on-anything tier. This is the
current contract; the earlier stages are recorded in the
[interchange](interchange-next-five.md) and [workbook](workbook-next-five.md)
documents.

**Degrade, never block.** A valid package always prepares. Sheets DataHorizon
cannot hold (chart, dialog and macro sheets) are left out, each reported as
`workbook.feature` `chart_sheets`, `dialog_sheets` or `macro_sheets` with the
sheet as sample; formulas referring to one are references to a missing sheet.
Choosing such a sheet at selection scope is `INVALID_ARGUMENT`, and a package
with no worksheets at all prepares with nothing to apply (`can_import` false).
A cell or feature DataHorizon does not support is kept in another form, approximated or left out,
and each such item is itemized in the conversion report before Apply. Only a
malformed or unsafe package rejects: bad ZIP or XML, DTDs or entities, non-UTF-8
parts, encryption, broken relationships, duplicate cells, broken shared-formula
groups, invalid style or string indexes, or a bound exceeded (`LIMIT`).

**Modes.** `formulas` keeps every formula; one DataHorizon cannot evaluate is
stored as a formula that shows an error naming the reason and starts
calculating when the function is added. `mixed` (the desktop default) keeps the
formulas DataHorizon can evaluate and replaces the others by their cached value
when one exists. `values` keeps only cached values; cache freshness cannot be
verified. Export always writes formulas without cached values (the bounded
selection export can also write values).

**Exporting formulas.** A whole-workbook export in `formulas` mode, bounded or
large, writes every stored formula, so a workbook imported in `formulas` mode
always exports. To XLSX, a formula DataHorizon can evaluate is written as
before; any other is written as stored (with `_xlfn.` prefixes and sheet names
in place of sheet identities) and counted in one `unevaluable` entry per reason
(`formula.name`, `formula.external_reference`, `formula.unsupported_reference`,
`formula.unsupported_function`, `formula.array`, `formula.syntax`, …); one whose
references cannot even be read is written as stored with an `approximated`
`formula.syntax` entry, as `unmaintained` cells are. To ODS, a formula that
translates to OpenFormula is written as before; one that does not is not
written as a formula, since an XLSX-dialect source is not OpenFormula: the cell
takes its current value when the engine has an error-free one (bounded export;
`formula.unsupported_syntax`, `converted`), and is otherwise written empty
(`formula.unsupported_syntax`, `omitted`; always so in the large export, which
evaluates nothing). A translated formula without an error-free current value
is written without a cached value in the bounded export too
(`formula.cache_omitted`). Selection-scope export stays strict: it refuses a
formula it cannot check against the selection. A `values` export still needs a
current, error-free value in every cell; its `CALCULATION_ERROR` names the
first 100 blocking cells (`cells`, `blocked_count`).

**Formats.** Transitional and strict (ISO/IEC 29500 strict namespaces) XLSX and
ODS 1.3. Error-valued cells import as Text and are reported as converted.
Formulas are read with a lenient reference scanner, so references inside
formulas DataHorizon cannot evaluate are still maintained by structural edits;
a formula the scanner cannot read becomes the `unmaintained` kind (native format
v7, see [native formats](native-format-v1.md#unmaintained-formulas-format-v7))
when a structural edit touches its sheet, and is exported unchanged with a
report entry. Recovery inspection counts the formulas a degraded import stores
(unevaluable but scannable, or `unmaintained`) as valid inputs under
`unevaluable_formulas` and `unmaintained_formulas`, not as issues; only a
formula source the scanner cannot read is an issue (see
[native formats](native-format-v1.md#bounded-recovery-validation)).

**XLSX packages LibreOffice reads.** Four leniencies follow LibreOffice, whose
own test files need them. A row or cell written without its optional `r`
attribute takes the next position: a row follows the previous row and a cell
the previous cell of its row. A row index is read as a decimal number (`0005`
is row 5); after one that is not a number on the grid, a row or cell without
its reference rejects. Part names written with backslashes
(`xl\workbook.xml`) are read as the forward-slash names. A cell style index
past the end of `cellXfs` is read as unstyled and reported
(`style.named_style`, `omitted`). A workbook `<sheet>` whose relationship id
names no relationship has no part: it is left out and reported as
`workbook.feature` `sheets_without_part`, like the sheet kinds above. A cell
reference that is present but not an A1 address, a cell off the grid and a
sheet relationship of an unknown type still reject.

**ODS packages LibreOffice reads.** Three leniencies follow LibreOffice, whose
own test files need them. The `mimetype` entry must exist and hold exactly the
spreadsheet type, but it may be compressed or stored after other entries. A
cell style, parent style or number style that the file references without
defining is ignored and reported (`style.named_style` or `style.number_format`,
`omitted`); `number:boolean-style` is a known data style. A `table:table`
named like `'file:///other.ods'#Sheet1` (a quoted URL with a known scheme, `#`
and a sheet name) is LibreOffice's cache of another document's sheet: it is not
imported as a sheet, takes no sheet index, and counts as one
`external_references` item together with its own `table:table-source`. Any
other quoted name, such as `'FY2025'#Actuals`, is a sheet. Encrypted packages, sheet names outside
the native rule (longer than 31 characters, or absent) and the size limits
still reject.

**The report.** Each entry has a category, a severity (`converted`,
`approximated`, `omitted`, `unevaluable`), a detail, a count and up to 100
samples (`{sheet, cell}`; a sample without a cell stands for a whole sheet,
one without a sheet for the workbook). At most 200 entries; totals per severity
count every item. The tree reader (bounded path) and the streaming reader
(large path) produce the same entries with the same counts; only the order of
samples (and of entries that tie when sorted) can differ, for shared-formula
members stored before their master, which the streaming reader converts at the
end of their sheet.

| Category | Meaning |
| --- | --- |
| `cell.*` | A cell value converted or skipped: `error_value`, `invalid_value`, `missing_cache`, `number_out_of_range`, `text_too_long`, `unsupported_type`, `date_conversion` |
| `formula.*` | A formula kept unevaluable, replaced by its cache or skipped: `unsupported_function`, `name`, `missing_sheet`, `external_reference`, `unsupported_reference`, `syntax`, `unsupported_syntax`, `array`, `data_table`, `untranslatable`, `unmaintained` (export), `cache_omitted` and `display_not_evaluated` (large export) |
| `style.*` | Formatting outside the native style tier: `number_format`, `font`, `fill`, `border`, `alignment`, `named_style` |
| `sheet.feature` | A worksheet feature not imported; the detail is its feature id (`merged_cells`, `column_widths`, `row_heights`, `hidden_rows_columns`, `frozen_panes`, `conditional_formatting`, `data_validation`, `hyperlinks`, `comments`, `excel_tables`, `auto_filter`, `sheet_protection`, `page_setup`, `drawings` (not reported for a drawing that held only charts; see [Charts](#charts)) and others); the count is items (ranges, rules, links, rows or columns) |
| `workbook.feature` | A workbook feature or package part not imported: `defined_names`, `external_references`, `hidden_sheets`, `workbook_protection`, `calculation_settings`, `pivot_tables`, `vba`, `custom_xml`, `rich_text` and others |
| `chart.*` | A chart converted or skipped on import: `chart.converted` and `chart.skipped`, see [Charts](#charts) |
| `report.truncated` | Further distinct entries beyond 200, counted but not itemized |

The [specification coverage table](excel-spec-coverage.md) lists every
SpreadsheetML element with the entry it produces. Features in their default form
(Excel's, openpyxl's and LibreOffice's default margins, page setup and header
and footer, a view without panes, row heights and column widths that are not
custom, `dimension`) produce no entry. Auxiliary parts read only for the report
never reject a valid file. Any problem reading an ODS `settings.xml`, a DTD or
entity included, skips pane detection and is reported
(`view_settings_unreadable`). An unreadable XLSX sheet relationship part is
reported (`relationships_unreadable`), but one with a DTD or entity rejects the
file. A workbook relationship typed as a worksheet whose part is a chart sheet
is an inconsistent package and rejects.

A count no valid file can reach rejects with `LIMIT` (more `<col>` elements
than 16,384 columns, more rows than 1,048,576, more merged ranges, hyperlinks
or validation rules than 1,000,000 in a sheet); a count kept only for the
report that reaches a scanner bound saturates instead, and its entry carries
`at_least: true` and reads "at least N" (for example more than 65,536 distinct
ODS validation names in a sheet). Section 5.2 of the
[fidelity plan](excel-fidelity-plan.md#52-size) lists every bound.

**Two paths.**

| | Bounded path | Large path |
| --- | --- | --- |
| Scope | Selection (one sheet at an offset, or one rectangle) and whole workbook | Whole workbook only |
| Transport | base64 in a JSON body of at most 4 MiB | `PUT /v1/uploads/{upload}` raw body (or `source_path` on the same machine), staged preparation job, `GET /v1/exports/{export}` download |
| Limits | 10,000 stored cells, 64 worksheets for a whole workbook (chart, dialog and macro sheets do not count; a selection can name any worksheet), 3 MiB package, 16 MiB expanded, 384 parts (enough for this service's own bounded export of 64 sheets and 64 charts, 325 parts), 100,000 XML nodes per part | 1,000,000 stored cells, 255 worksheets, 48 MiB package, 256 MiB expanded, 4,096 parts, 1 MiB of XML per cell or record, 65,536 shared-formula groups open at once per sheet, 300 s per phase, 2 concurrent uploads and downloads, 1,024 distinct styles per export |
| Review | Every prepared cell | The report and a bounded sample; cells are staged on disk |
| Apply | One managed edit (selection) or one atomic `workbook_import` | One atomic `workbook_import` from the stage |

Whole-workbook import replaces the sheets of an empty workbook, or
(`target=add_sheets`) adds the imported sheets after the existing ones when no
name collides (`SHEET_NAME_CONFLICT` otherwise); either way one transaction that
clears history. It raises the structural event and no per-cell change events;
an activated required handler that has not opted into structural events
rejects before any change. Export streams sheet by sheet from one read snapshot
of the native file. Selection scope carries cell content only; sheet-level
features travel, when a later increment adds them, only at whole-sheet and
whole-workbook scope.

The large-path limits were measured on November 11, 2026 on an Apple M1
MacBook Air with 8 GB (macOS 26.6.2, Python 3.14.6, SQLite 3.53.4, warm cache)
and hold for that host only; see
[section 5.2 of the fidelity plan](excel-fidelity-plan.md#52-size) and the
[evidence](evidence/excel-fidelity-1-capacity.json). XLSX qualified at
1,000,000 cells (slowest phase 30.9 s, peak service RSS 93 MB). ODS qualified at
500,000 cells with the generated standard mix; at 1,000,000 cells it qualified
only for a mix without long text, because DataHorizon's ODS export of the
standard mix exceeds the 256-MiB expanded limit and is rejected with `LIMIT`.
ODS at other mixes and sizes, OS-cold opening and other hosts were not measured.

**Clients.** REST with OpenAPI; the SDK's `prepare_spreadsheet_file`,
`import_staged` and `export_spreadsheet_file`; the CLI's `import-spreadsheet`
and `export-spreadsheet`; the desktop's File import and export with a grouped
report review (see the [automation guide](python-automation-guide.md#importing-and-exporting-workbooks)).

**Evidence.** The interchange corpus (`tests/fixtures/corpus/`) and its
[scorecard](evidence/excel-fidelity-scorecard.json): no file is rejected and no
listed feature is dropped without a report entry. The corpus has no file saved
by Excel or LibreOffice yet, so this is not an Excel or LibreOffice
compatibility claim.

### Charts

Chart import was built on November 14-15, 2026 on branch `claude/charts-import-view`
(merged to `main` through pull request 140; the design is [charts-import-view-design.md](charts-import-view-design.md)).
The development preview imports charts from XLSX (DrawingML `c:chartSpace`
reached from a worksheet's drawing) and ODS (a `draw:frame` holding a
`draw:object` with a `chart:chart` sub-document) as native charts, and the
desktop draws them view-only ([status](implementation-status.md#charts-import-and-view-only-display--november-15-2026)).
Chart export was built on November 15, 2026 on branch `claude/charts-export`
(not merged; the design is [charts-authoring-export-design.md](charts-authoring-export-design.md#6-export),
the record is the [status](implementation-status.md#charts-export--november-15-2026)
and the [evidence](evidence/charts-export-verification.txt)); see
[Chart export](#chart-export) below. Neither is a qualified cross-platform
release. CSV carries no charts.

Cached values in the file (`c:numCache`, `c:strCache`) are never used as data;
the chart reads the imported cells, so in `values` mode it reads the cached
cell values.

**What an imported chart keeps**

| In the file | Native result |
| --- | --- |
| Bar chart, vertical; bar chart, horizontal; line; area; pie; scatter | `column`; `bar`; `line`; `area`; `pie`; `scatter` |
| Clustered or standard grouping | `clustered` (`null` where the type takes none) |
| Stacked grouping | `stacked` |
| Chart title text (literal rich text) | `title.text`, cut to 255 characters |
| Category and value axis titles (literal) | `axes.category.title`, `axes.value.title`, each cut to 255 characters |
| Value axis fixed minimum and maximum | `axes.value.min`, `axes.value.max` (both dropped silently when minimum exceeds maximum) |
| Legend at right; at bottom; absent | `right`; `bottom`; `none` |
| Series name by cell reference; by literal | `name.source`; `name.text`, cut to 255 characters |
| Series values, categories, scatter x by one contiguous single-row or single-column reference | Range sources |
| Categories of the first series | The chart's `categories` |
| Frame name ("Chart 1") | Chart name, made unique on its sheet |
| Top-left anchor cell and offset | `anchor`, `offset` in pixels at 96 DPI, offset clamped to 0..4096 |
| Frame extent | `size` in pixels, clamped to 64x48..4096x4096 |
| Drawing order on the sheet | `z` |

**Converted (`chart.converted`; the chart is still imported).** Each entry
names the chart as its sample and the sheet; `count` counts charts.

| Detail | Meaning |
| --- | --- |
| `styling: <property>` | One entry per dropped property: `colours`, `data labels`, `gap width`, `markers`, `smoothing`, `gridlines`, `number formats`, `fonts` |
| `grouping` | A stacked grouping on a type that takes none (line, pie, scatter) became none |
| `percent_stacked` | Percent-stacked became `stacked` |
| `three_d` | A 3D variant became the 2D type |
| `doughnut` | A one-series doughnut became `pie` |
| `series_name` | A series name that could not be bound exactly was replaced by `Series N` |
| `legend_position` | A legend at top or left became `right` |
| `title_from_cell` | A title or axis title taken from a cell became that cell's cached text, no longer following the cell |
| `title_cut` | A chart or axis title over 255 characters was cut |
| `series_name_cut` | A literal series name over 255 characters was cut (one entry per chart) |
| `categories_differ` | Series with categories other than the first series' use the first series' |
| `size_approximate` | The size was computed from default cell sizes (64x20 px) because the sheet declares custom sizes; on ODS, any declared column-width style |
| `anchor_absolute` | An absolute anchor was placed at the nearest cell |
| `name` | A name over 64 characters, empty or unprintable was cut or replaced by `Chart` |

**Skipped (`chart.skipped`; no chart is created, the cells import).**

| Detail | Meaning |
| --- | --- |
| `type` | Any other chart type: radar, stock, surface, bubble, treemap, sunburst, waterfall, histogram, funnel, map, and Excel's extended (`cx:`) charts |
| `combination` | A plot area with more than one chart type |
| `secondary_axis` | A secondary axis |
| `pie_series` | A pie or doughnut with other than one series |
| `source` | A source that is not one contiguous single-row or single-column range on an imported worksheet: multi-area or multi-level reference, defined name, external workbook, literal array, a sheet that is not imported, table structured reference; also a chart with no series |
| `limit` | More than 64 series, a source over 10,000 cells (1,000 for a pie's values or categories), or over 100,000 source cells in all; a whole-column reference is over the source limit; or a converted chart over 64 KiB as JSON (after a one-sheet import's offset too) |
| `off_grid` | The anchor, or after a one-sheet import's offset any anchor or source, lies outside the grid |
| `workbook_limit` | More than 64 charts in the imported file, or charts that would take one import's charts past 1 MiB as JSON in all, or past the room a bounded preview's cells leave; the excess is left out in file order. Once 64 charts are held no further chart part is read, so a frame whose part was not yet read is reported `workbook_limit` without being checked (on ODS even when its object is not a chart). Charts that would pass 64 once the destination's own charts are counted (`add_sheets`, one-sheet) are decided at apply and named in the receipt's `charts_left_out` instead |
| `unreadable` | The chart or its drawing could not be parsed or read (a corrupt compressed part included), a chart part that would pass the chart allowance, a worksheet's second and later `<drawing>` elements (one entry with their count), or frames past the 256th of the import (one entry per sheet with their count) |
| `grouped` | The chart is inside a group shape |

Chart sheets keep their `workbook.feature` `chart_sheets` entry. A malformed
chart never fails the import.

**Limits and budgets.** Chart, drawing and relationship parts are read against
their own allowance of 16 MiB decompressed per import, separate from the
package's expanded budget, and each distinct part is read and parsed once; a
chart that would pass it is reported `unreadable`. An XLSX worksheet's charts
come only from the drawing its `<drawing r:id>` names. At most 256 chart frames
are read per import, across all its sheets; once 64 charts are held no further
chart part is read. A draft keeps at most 65 series, enough for the conversion
to skip the chart as `limit`. Series names and axis titles are cut to 255
characters like the title; a converted chart is at most 64 KiB as JSON and one
import's charts at most 1 MiB, and records a client sends (`charts` of
`workbook_import` and `edit`) are refused with `INVALID_ARGUMENT` past the same
two caps. A bounded preview answers within 4 MiB and its cells come first:
charts get the room the cells and their report leave, and on a one-sheet
import also the room the cells leave in the `edit` request that applies them,
so a preview that fits without charts is never refused because of them. The
charts that do not fit are left out in file order and reported as
`workbook_limit`. XML is read through the same entity-safe readers as the rest
of the package and relationship targets resolve inside the package only.

**Import paths.** All three apply shapes carry charts: the staged
whole-workbook import (stage format version 2 holds a `charts` table covered by
the stage digest; the review summary lists `charts` as name, sheet and type),
the inline whole-workbook import (the bounded preview returns
`workbook_charts`, the `workbook_import` request carries `charts`), and the
one-sheet import, where `edit` takes `charts` (see
[interfaces](interfaces-and-interoperability.md#chart-import-paths)). Receipts
report `imported.charts` (whole workbook) or `changed_charts` (one sheet), and
`charts_left_out` names charts past the workbook's chart limit. The CLI has no
one-sheet import, so only the SDK and the desktop carry charts on that path.

**Known limits and inconsistencies.**

- An import reports no `workbook.feature` `charts` entry, so chart parts
  that no drawing reaches (orphans) are not itemized. It reports a sheet's
  `drawings` unless that sheet's one drawing was read and held only chart
  frames (imported, or skipped with their own `chart.skipped` entry); a
  picture, shape, connector, group or other object keeps it, as does a drawing
  that was not read. On ODS the count is of the sheet's drawing items that are
  not charts read as such, and `embedded_objects` does not count an object
  directory read as a chart (imported, or skipped with a `chart.skipped` entry);
  a formula or OLE object, an object that could not be read and an object never
  reached because of a reading bound are still counted.
- Pixel sizes use default cell sizes (64x20 px); where the sheet declares
  custom sizes the chart is imported with `size_approximate` and its extent can
  differ from the file's.
- An imported chart keeps roughly its source pixel size, but because the import
  measures cells at 64x20 px while the desktop grid's default cells are larger,
  it covers fewer cells in the desktop than in the source.
- A one-sheet import skips a chart with a source on another sheet (`source`) and a chart
  pushed off the grid by the offset (`off_grid`), and takes only the selected worksheet's charts.
  A chart it skips this way does not count toward the 64-chart limit.
- Excel's colours, data labels, gap width, markers, smoothing, gridlines,
  number formats and fonts are not carried; specification version 1 has no
  place for them.
- Verified against fixtures built in code and one owner-supplied XLSX sample
  through the service; the evidence file lists what was not verified (the
  desktop window on that file, Windows, Linux, LibreOffice).
  See the [evidence](evidence/charts-import-view-verification.txt).

### Chart export

The whole-workbook XLSX and ODS exports (inline and the streamed export job) and
the selection export write each native chart whose data comes from cell ranges
as a live chart: a DrawingML chart in XLSX, a chart object in ODS, anchored at
its cell with its offset and size, in drawing order. List, relational and CSV
exports never carry charts.

**What is written**

| Native | In the file |
| --- | --- |
| `column`, `bar`, `line`, `area`, `pie`, `scatter` | The matching chart type |
| `clustered`, `stacked` | The matching grouping; XLSX stacked column and bar charts also carry full overlap (`c:overlap` 100) |
| Title, axis titles | Literal text; an empty title is written as no title |
| Value axis `min`, `max` | Fixed minimum and maximum |
| Legend `right`, `bottom`, `none` | Legend position, or no legend |
| Series name by cell reference | A cell reference, in XLSX and ODS |
| Series name by text | XLSX: literal text. ODS: not carried (see below) |
| Values, categories, scatter x | Cell references by sheet name |
| Name, anchor, offset, size, drawing order | Frame name, anchor, extent and drawing order |

No markers or gridlines are written (specification version 1 holds neither).
Characters XML 1.0 cannot hold are dropped from written text; a literal tab in a
title is written as is. A line break in an ODS chart or axis title is written as
`text:line-break` inside the title's one paragraph (ODF allows a title one
`text:p`, and LibreOffice joins the lines of a title written as several), and
the ODS importer reads `text:line-break` in a title as a line break.

**Format details.** XLSX references always quote the sheet name
(`'Sales'!$A$2:$A$13`, a `'` in the name doubled), so a sheet named `R`, `C`,
`TRUE` or `FALSE` cannot be read as something else, and the chart is written
with square corners (`c:roundedCorners` 0; Excel draws rounded corners when it
is absent). An ODS chart object says its data comes from the spreadsheet
(`xlink:href=".."` on `chart:chart`) and lists its source ranges, space-separated
and in the same order, in the plot area's `table:cell-range-address` (with
`chart:data-source-has-labels="none"`) and the frame's
`draw:notify-on-update-of-ranges`, as LibreOffice Calc writes them. The frame
sits in its anchor cell, so document order is anchor order; each frame carries
`draw:z-index`, its place in drawing order on its sheet. The importer orders a
sheet's ODS charts by `draw:z-index` when every chart frame on the sheet carries
one (document order breaks ties) and keeps document order otherwise, so files
from applications that write no `draw:z-index` read as before. An ODS scatter
chart carries no marker or line settings: LibreOffice's points-only form
(`chart:symbol-type` and `chart:lines`) would make the importer report a dropped
`markers` style, so how an application draws it by default is not controlled
(the XLSX scatter chart is markers only).

**ODS series named by text.** ODS cannot carry a series name given as text
without an embedded copy of the data. The chart is still written, the export
reports `chart.converted` with detail `series_name` for it, and on import the
series is named "Series N". A series named by a cell reference round-trips.

**No cached values.** The export writes no cached values for charts in either
format, as for formulas ("Formula caches are omitted; target recalculation is
requested"), and no ODS local data table. In XLSX a chart shows its data once
Excel fills the caches on load, when it recalculates on open. In ODS the chart
reads the sheet's cells directly.

**Left out.** A chart is left out whole, never in part, and reported
`chart.skipped` with one of seven reasons; a chart with several reasons is
reported under the first that applies, checked in the order `unsupported`,
`broken_source`, `table_source`, `query_source`, `source`.

| Reason | Meaning |
| --- | --- |
| `table_source` | A source is a table column |
| `query_source` | A source is a saved-query column |
| `broken_source` | A source is broken |
| `source` | A range source the format cannot express as one reference, a source on a sheet that is not in the export, or (selection export) a chart whose anchor or a source lies outside the selection |
| `unsupported` | A stored specification this service cannot read: an unsupported specification version or an invalid specification inside a valid stored record; the export does not fail. A stored chart record that is itself malformed (not JSON, not an object, or a bad envelope) is refused when the workbook is opened or refreshed, before any export runs, as it was before chart export |
| `unwritable` | Writing the chart failed for an unexpected reason; the export continues. If packaging the built charts fails, every chart is left out with this reason and the export continues without charts |
| `size_limit` | (Inline exports) The chart was built, but the file or its response had no room for it within the size limits; the cells and the charts before it are exported |

**Report.** `Charts: N written.` or `Charts: N written, M left out; each is
listed below with its reason.`; no line when the workbook has no charts.
`ConversionReport` merges entries with the same reason: the chart names are in
an entry's `samples` and `count` counts charts. The CLI's report names every
chart of a `chart.skipped` entry, not only the first.

**Size limits.** Chart parts count against the inline exports' 16 MiB expanded
limit and 4 MiB response limit (64 charts of 64 series add about 1.8 MB expanded
and 100 KB compressed). Charts never fail an inline export that its cells alone
would pass: when the export with every chart is over a limit, the charts are
kept in order as far as they fit and each later one is left out and reported
`size_limit`. Cells that are over a limit on their own fail with `LIMIT` or
`READ_LIMIT`, as before charts were written. The limits themselves are
unchanged. Finding what fits assembles the export again (at most eight more
times for 64 charts), only in this case. The streamed export job counts chart
parts against its own, larger expanded budget and has no such fallback.

**Bounded import of a bounded export.** A bounded package may hold 384 parts
(it was 128): an XLSX export writes a drawing, two relationship parts and a
chart part per sheet with one chart, so charts spread over 25 sheets already
made 130 parts, which the bounded import refused.

**Selection export.** A chart is written only when its anchor and every source
lie inside the selection. The selection is written at its original coordinates
on a sheet named `Sheet1`, so the chart keeps its anchor and its references name
`Sheet1`. Charts on other sheets are not reported.

**Round trip.** Exported and imported again, a chart keeps its type, grouping,
title, axes, legend, sources, series names, name, anchor, offset, size and
order, in both formats and through both export routes
(`tests/test_chart_round_trip.py`). Three differences come from the importer's
own rules and are documented, not fixed: text over 255 characters (an axis
title or a series name given as text) is cut on import; duplicate chart names on
a sheet are made unique; an empty series name comes back as "Series N". The
fourth, an ODS series named by text, is above. An import reads XLSX stacked
column and bar charts written with full overlap without reporting a dropped
`gap width` style (only that exact case, stacked grouping with overlap 100).

**Not verified.** The files were not opened in Excel or LibreOffice (neither is
installed on the machine that ran the checks), and the owner's sample
`Column Chart_Complete.xlsx` was not exported. The ODS chart data binding and
`draw:z-index` were written from the format's documentation and LibreOffice's
known output and are unverified until a file is opened there. Two sample files are written for
the owner to open; see the [evidence](evidence/charts-export-verification.txt).

## Whole-workbook interchange

Both paths above support `scope=workbook` with all sheet names and order, empty
sheets, mapped inputs and styles and cross-sheet formulas. Stable native sheet
references become portable names on export. Code, queries, relationships,
history and view state are omitted and reported; imported code never
activates. Keep the native or source file for features outside this tier.

The [formula/style fidelity increment](workbook-next-five.md#formula-and-style-fidelity)
adds bounded XLSX shared-formula expansion and explicit ODS parent-cell-style
inheritance. Only stored shared-formula members expand, so a group's extent may
be any rectangle of the grid that holds its master. Unsupported arrays, data tables and default styles are kept or
reported as above (they no longer block); this does not widen the calculation function tier.


Selected-range status summaries follow the [snapshot summary contract](spreadsheet-next-ten.md#selection-summary-contract).
Find navigation, clear distinctions, independent directional fills and address
ranges follow the corresponding contracts in the same batch record. They do not
expand filtered-edit semantics, reference-transform promises or release status.

Filtered list values can be copied in display order with typed native clipboard styles; formulas become values. See [the editing/list contract](editing-lists-next-ten.md#copy-filtered-values) for visibility and snapshot limits.

Filtered CSV/XLSX/ODS export uses one ordered values snapshot compressed to A1, with review of formula conversion and hidden-section exclusions. See [the filtered export contract](editing-lists-next-ten.md#export-filtered-values).

Named sort/filter definitions persist locally per workbook/sheet and are reviewed
before re-applying after reopen, including read-only opens. View → Save/Load view
in workbook explicitly transfers the supported presentation/named-list tier to
native v3 metadata; standalone named list metadata is also available through
managed operations. Local preferences are not automatically portable. See
[saved-view limits](editing-lists-next-ten.md#saved-named-list-views) and
[portable native views](m3-m5-next-ten.md).

Configurable Number/Percentage display supports 0–15 decimal places, portable
through simple XLSX zero-placeholder formats and ODS decimal-place styles.
Two-place imports may use legacy native tokens; effective display and numeric
magnitude are retained. Arbitrary/custom/locale display remains reported
outside this grammar. See [the contract](editing-formatting-next-ten.md#decimal-precision).

Numeric grouping maps to simple XLSX `#,##0[.zeros][%]` formats (including
built-ins 3/4) and ODS `number:grouping`. This is an invariant comma/dot tier;
localized separators and arbitrary custom patterns are not inferred.

Explicit three-letter currency suffixes map to XLSX quoted literals and ODS
currency-symbol styles with unscaled numeric values. Arbitrary symbols, locale
layouts and accounting patterns remain reported outside this tier. See
[explicit currency labels](editing-formatting-next-ten.md#explicit-currency-labels).

Explicit portable font families, integer 6–72pt sizes and RGB text colors map to
XLSX fonts and ODS text properties, including ODS named font-face references.
Fonts are not embedded; target fallback/layout can differ. Theme/tinted colors,
fractional sizes and advanced typography remain reported as outside this tier.

Individual thin/medium/thick/dashed/dotted border edges map to XLSX styles and
bounded ODS per-edge width/pattern attributes. Border colors explicitly map to
native palette/export black. Diagonal edges, other patterns and widths remain
reported outside this tier; adjacent-cell conflict/layout can differ by target.
