# Development compatibility tier 0

This is a narrow development surface, not Excel compatibility certification.
Native inputs and styles survive reopen; results are calculated within the
current read transaction. Work remains before a complete everyday preview.

## Formula surface

`+ - * / ^`, unary signs, parentheses and comparisons `= <> < > <= >=` are
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
| IF | Two or three arguments; numeric/boolean/blank condition; evaluate only the selected branch; omitted false branch is FALSE |

Double-quoted strings and TRUE/FALSE are supported. Unsupported function or
arity is #NAME?. Invalid syntax/coercion is #VALUE!; broken addresses are #REF!,
cycles are #CYCLE!, and resource exhaustion
is #LIMIT!, including runtime recursion exhaustion. Numeric overflow remains
#NUM!. Errors retain editable source and never become an invented value.
An export with any selected formula error is blocked. Evaluation budgets include
AST work as well as referenced cells, so not every 10,000-cell dependency shape
fits the initial limit. Recalculation is synchronous and bounded; there are no
background results that can overwrite a newer input state.

## Import and export

| Feature | Native v1 | CSV import / export | XLSX | ODS |
| --- | --- | --- | --- | --- |
| Typed values | Preserved | Explicit reviewed types / canonical values | Text, finite numbers, Booleans; serial dates remain numbers | Text/numbers/Booleans; ISO dates/durations become Text |
| Leading-zero text | Preserved | Preserved as text; CSV receivers may infer types | Preserved as Text | Preserved as Text |
| Formula source | Preserved | Literal Text / evaluated values | Implemented local formulas or explicit cached/fresh values | Local SUM/IF and arithmetic except powers; canonical syntax |
| Supported styles | Preserved | Destination styles retained / omitted | Native direct styles mapped; unsupported display reported | Native direct styles mapped; unsupported display reported |
| Sheets | Preserved | At selected cell / selected rectangle | One sheet at destination / one rectangle at source coordinates | One sheet / rectangle, as XLSX |
| Code / queries / relationships | Planned metadata | No code activation / omitted | Omitted and reported; no code activation | Omitted and reported; no code activation |

CSV import supports explicitly selected UTF-8 (optional BOM), UTF-16 (required
BOM), Windows-1252 and Latin-1, with strict decoding. The GUI offers comma,
semicolon, tab and pipe delimiters; the API accepts one character excluding
quotes, NUL and line endings. Import preview is bounded by 10,000 cells/records,
3 MiB of raw bytes and decoded UTF-8, 64 KiB per cell and a 4-MiB complete JSON
request/response. Text is the default. Explicit per-column Number/Boolean uses
native strict conversion and precision rules; dates require YYYY-MM-DD,
M/D/YYYY or D/M/YYYY and normalize to ISO Text. A header option retains the
first record as Text. Invalid conversions block Apply and never fall back to
Text; users can explicitly change the type and review again. See
[CSV controls](editing-csv-next-five.md#csv-encoding-and-delimiters) and
[typing details](editing-csv-next-five.md#csv-column-typing).
Exports use round-trip-safe numeric text rather than the grid's display rounding.
Empty text, NULL and absence all map to
an empty CSV field; this loss is inherent in the initial values-only tier.
Native snapshot copies preserve the distinction. TSV clipboard input currently
uses literal tab/newline separation; embedded delimiters require CSV import.

## Editing and presentation

Supported: typed entry, full/value/format paste, copy/fill reference offsets,
explicit numeric series, cell/range Undo/Redo, source-preserving styles, bounded
sheet duplication/reorder, and limited rename/delete. Cut is an atomic same-sheet
move only when no formula source exists anywhere in the workbook. Structure
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
up to eight keys and filters are accepted by REST (two sort keys/one filter in
the GUI). Type order is blank, number, text, boolean; text comparison is
case-sensitive. A list header is explicit. Filter edits can remove a record from
the view; Clear restores the source sheet. Range operations on projections are
blocked. Persistent lists and a stable record-ID schema are later work.
Saving an edited cell keeps its source row and column selected through resorting
while that row remains visible. If a filter removes it, select the first
remaining row. Invalid non-object filter/sort criteria return a validation error.

Find supports case-sensitive literal substring search over source or display,
returning a count and first 500 locations in row-major order. Replace All runs
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
or while row 1 is hidden. Multiple edges and cross-pane range selection remain
unavailable.

### XLSX input tier

Bounded XLSX sheet import and selection export are now available through the
REST interface, SDK and desktop. Text/numbers/Booleans and implemented local
formulas have separate support from formatting and advanced features. Every
review includes a conversion report; unsupported cells block import, while
unsupported non-cell features are reported as omitted. See the exact
[interchange contract](interchange-next-five.md#xlsx-inputs-and-formulas).
ODS now has a separate [bounded tier](interchange-next-five.md#ods-inputs-formulas-and-native-styles). XLSX direct styles follow the separate
[native-style mapping](interchange-next-five.md#xlsx-native-styles).


## Whole-workbook interchange

XLSX and ODS now support `scope=workbook`: all sheet names/order, empty sheets, mapped
inputs/styles and supported cross-sheet formulas. Import replaces an empty
workbook atomically and rejects required handlers; success clears history.
Export uses one bounded read transaction and translates stable native IDs to names.
Aggregate limits and omissions remain explicit in the
[workbook contract](workbook-next-five.md). Single-selection tiers remain available.


The [formula/style fidelity increment](workbook-next-five.md#formula-and-style-fidelity)
adds bounded XLSX shared-formula expansion and explicit ODS parent-cell-style
inheritance. Unsupported arrays/data tables/default styles still report or block
as documented; this does not widen the calculation function tier.
