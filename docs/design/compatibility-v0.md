# Development compatibility tier 0

This is a narrow development surface, not Excel compatibility certification.
Native inputs and styles survive reopen; results are calculated within the
current read transaction. Work remains before a complete everyday preview.

## Formula surface

`+ - * / ^`, unary signs, parentheses and comparisons `= <> < > <= >=` are
supported. Power is right-associative and binds more tightly than unary signs
(`-2^2` is -4). Numbers use finite binary64. Overflow/nonreal results are #NUM!;
division by zero is #DIV/0!. Text is not implicitly converted to a number in
arithmetic; blanks become zero and booleans become 0/1. Comparisons of blank with
blank or numeric/boolean values use zero for the blank operand. Other mixed-type
comparisons are errors. Text comparison is case-sensitive. Formatting an empty
auto cell does not turn it into empty Text; explicitly entered empty Text remains
distinct and retains text coercion rules.

References support A1, $A1, A$1 and $A$1, rectangles and quoted sheet names such
as `'Other sheet'!A1`. Quoted sheet names escape an apostrophe by doubling it.
Copy/paste and fill translate relative components, preserving absolute ones and
quoted string contents. Unsupported transformations reject before any commit.
Cross-sheet ranges must remain within one sheet. Whole-column ranges, named
ranges, structured references, external workbooks and 3D ranges are unsupported.

| Function | Signature and behavior |
| --- | --- |
| SUM | Zero or more scalar/range arguments; add numeric values, ignore text, blanks and booleans; propagate referenced formula errors |
| IF | Two or three arguments; numeric/boolean/blank condition; evaluate only the selected branch; omitted false branch is FALSE |

Double-quoted strings and TRUE/FALSE are supported. Unsupported function or
arity is #NAME?. Invalid syntax/coercion is #VALUE!; broken addresses are #REF!,
cycles are #CYCLE!, and resource exhaustion
is #LIMIT!. Errors retain editable source and never become an invented value.
An export with any selected formula error is blocked. Evaluation budgets include
AST work as well as referenced cells, so not every 10,000-cell dependency shape
fits the initial limit. Recalculation is synchronous and bounded; there are no
background results that can overwrite a newer input state.

## Import and export

| Feature | Native v1 | CSV import | CSV export | XLSX / ODS |
| --- | --- | --- | --- | --- |
| Typed values | Preserved | All Text by default | Canonical displayed values without number format | Unavailable |
| Leading-zero text | Preserved | Preserved | Preserved as characters; receiving application may infer types | Unavailable |
| Formula source | Preserved | Formula-like text remains literal | Evaluated values only | Unavailable |
| Supported styles | Preserved | Existing destination styles retained | Omitted, reported before export | Unavailable |
| Sheets | Preserved | At selected cell on one sheet | Explicit selected rectangle | Unavailable |
| Code / queries / relationships | Planned metadata, not implemented | No code activation | Omitted | Unavailable |

CSV is UTF-8, comma-separated in the GUI, with quote handling through the Python
CSV parser. The API accepts another single-character delimiter. Import preview
is bounded by 10,000 cells and must be reviewed. GUI import keeps all columns
Text; per-column numeric/date conversions and encoding controls remain M5 work.
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
Sheet rename/delete/duplicate reject cross-sheet references until their full
reference transformation is implemented. Row/column insertion/deletion, freeze,
hide/unhide, wrap, borders, fit-column sampling and saved view state are deferred.

Lists are explicit presentation projections over source row coordinates, capped
by the 10,000-cell ordinary scope. Sorting is stable with source row as tie-break;
up to eight keys and filters are accepted by REST (two sort keys/one filter in
the GUI). Type order is blank, number, text, boolean; text comparison is
case-sensitive. A list header is explicit. Filter edits can remove a record from
the view; Clear restores the source sheet. Range operations on projections are
blocked. Persistent lists and a stable record-ID schema are later work.

Find supports case-sensitive literal substring search over source or display,
returning a count and first 500 locations in row-major order. Replace All runs
in one managed transaction. Displayed formula results cannot be replaced.
Regular expressions and case-folded replacement are not implemented.
