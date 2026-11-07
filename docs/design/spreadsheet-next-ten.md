# Next ten spreadsheet implementation increments

November 7, 2026. No next-ten batch was previously recorded after PR #34.
The executed default scope follows the remaining M3–M5 spreadsheet foundation.
Execute in order, one implementation PR at
a time: commit/push, publish, self-review/fix and merge before starting the next.
Hosted Actions have no credits; use local verification and preserve explicit
platform/release limits.

1. Aggregate functions: AVERAGE, MIN, MAX, COUNT and COUNTA.
2. Logical functions: AND, OR and NOT.
3. Lazy IFERROR with resource/cycle errors kept visible.
4. Numeric functions: ABS, ROUND, ROUNDUP and ROUNDDOWN.
5. Native percentage formatting with XLSX/ODS mapping.
6. Clear formats and Clear all, preserving atomicity and history.
7. Directional Fill down/right from independent row/column seeds.
8. Address-field rectangular range navigation.
9. Scoped Find Next/Previous with stale-result invalidation.
10. Bounded selected-range count/sum/average status summaries.

These add general spreadsheet primitives; no workflow semantics, new native
schema, larger job budgets, remote binding or managed SQL are introduced.
Cross-platform/manual/clean-machine and distribution gates remain open.

## Aggregate function contract

AVERAGE/MIN/MAX/COUNT/COUNTA accept 1–255 scalar/reference/range arguments,
within the existing parser, cell, chain, byte and execution budgets. Reference
text/booleans/blanks are ignored by numeric aggregates; direct booleans and
invariant numeric Text are converted. Other direct Text is ignored by COUNT,
and rejected by AVERAGE/MIN/MAX. Conversion accepts surrounding whitespace but
not locale separators/dates. COUNT ignores ordinary cell errors. COUNTA counts
non-NULL values, including explicit empty Text and ordinary cell errors. Cycles
and resource errors propagate through both counting functions. Neither counts a
native NULL or absent cell. MIN/MAX with no numeric values return 0; AVERAGE
returns #DIV/0!. AVERAGE rejects a nonfinite running subtotal even if the eventual
mathematical mean could be finite; this conservative binary64 stage is explicit.
The existing SUM coercion contract remains unchanged.

One function registry controls engine capabilities and XLSX/ODS formula review.
Sources retain their spelling, read snapshots remain current, and required
transactional handlers use the same evaluator. Unsupported functions remain
errors. This is a tested compatibility stage, not full Excel certification.

Primary references: Microsoft's
[AVERAGE VBA contract](https://learn.microsoft.com/en-us/office/vba/api/excel.worksheetfunction.average),
[MIN](https://support.microsoft.com/en-us/excel/functions/min-function),
[MAX](https://support.microsoft.com/en-us/excel/functions/max-function),
[COUNT](https://support.microsoft.com/en-us/excel/functions/count-function), and
[COUNTA](https://support.microsoft.com/en-us/excel/functions/counta-function).

## Logical function contract

AND/OR accept 1–255 scalar/reference/range arguments. Numeric and Boolean inputs
participate; referenced Text and empty/NULL cells are ignored. Direct TRUE/FALSE
Text is accepted case-insensitively; other direct Text is #VALUE!. An aggregate
with no participating values is #VALUE!. Both functions evaluate all arguments
and propagate errors, including after a decisive FALSE/TRUE value. NOT requires
one scalar, treats blank/NULL as FALSE, accepts numeric/Boolean/TRUE-or-FALSE Text,
and rejects arrays and other Text. Existing lazy IF is unchanged. The ordinary
calculation/parser budgets still apply, and both file formats use the shared
function registry.

References: Microsoft [AND](https://support.microsoft.com/en-us/excel/functions/and-function),
[OR](https://support.microsoft.com/en-us/excel/functions/or-function), and
[NOT](https://support.microsoft.com/en-us/excel/functions/not-function).

## IFERROR contract

IFERROR requires two arguments. It returns the first scalar result, evaluating
its fallback only after an ordinary spreadsheet error (#REF!, #DIV/0!, #VALUE!,
#NAME?, #NUM!, #N/A or #NULL!). A failed fallback remains visible. Cycles and
parser/calculation resource limits are never converted to a fallback. Storage,
invalid native input and required-hook failures are not spreadsheet errors and
are not caught. Empty/NULL results become empty Text; native inputs stay intact.
Direct first-argument ranges and evaluated array results are unavailable. An
unused fallback is not evaluated, including a range or error expression.

The function participates in the shared registry and XLSX/ODS scalar formula tier.
See Microsoft's [IFERROR contract](https://support.microsoft.com/en-us/excel/functions/iferror-function).

## Numeric function contract

ABS takes one scalar; ROUND/ROUNDUP/ROUNDDOWN take a number and an integer digit
count in [-15,15]. Numbers, Booleans and blank/NULL use existing arithmetic
coercion; Text and arrays are #VALUE!. Invalid arity is #NAME?, fractional digits
are #VALUE!, and out-of-range digits/nonfinite results are #NUM!. Rounding uses
the shortest decimal spelling of the binary64 value, a bounded decimal context,
and returns binary64. ROUND ties go away from zero; ROUNDUP goes away from zero,
ROUNDDOWN toward zero. Negative digits round tens/hundreds/etc.; zero results
normalize their sign. Locale parsing, broader digit ranges and complete Excel
numeric equivalence are not promised. Shared budgets, hooks and file tiers apply.

References: Microsoft [ROUND](https://support.microsoft.com/en-us/excel/functions/round-function),
[ROUNDUP](https://support.microsoft.com/en-us/excel/functions/roundup-function), and
[ROUNDDOWN](https://support.microsoft.com/en-us/excel/functions/rounddown-function).

## Percentage format contract

Native style `format: percent2` displays numeric fractions as two-decimal
percentages (0.125 → 12.50%). It changes no source, input kind or numeric magnitude.
Text, Booleans and NULL retain their usual displays. Scaling/formatting uses an
isolated bounded decimal context with half-even display rounding, including
finite extremes that would overflow a binary64 multiplication by 100. This is a
presentation operation with ordinary history and format-copy behavior. Values
CSV export keeps the underlying unscaled number. Percent-suffixed entry and locale
parsing remain outside this tier; enter numeric fractions or explicit Text.

XLSX maps built-in format 10 or exact custom `0.00%`; ODS maps a simple two-decimal
percentage style with a trailing % token. Other percentage patterns still report
an omitted display. Exports retain ratios and write supported percentage styles;
ODS numeric values use the percentage value type. Native v1 stores the new style
token in its existing JSON field. Older readers may reject the unsupported token;
no schema mutation or silent input/format substitution is introduced.
Independent openpyxl/odfpy producers/consumers are exercised by
`tests/verify_percentage_consumers.py` using the existing build-only environment.

Percentage mapping references: Microsoft [built-in number formats](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.spreadsheet.numberingformat?view=openxml-3.0.1)
and the [ODF 1.3 schema](https://docs.oasis-open.org/office/OpenDocument/v1.3/os/part3-schema/OpenDocument-v1.3-os-part3-schema.pdf).

## Clear command contract

Edit → Clear contents removes inputs and retains styles. Format → Clear formatting
replaces each selected style with an empty style, preserving exact source/kind,
including numeric inputs shown as Text, formulas, explicit empty Text and NULL.
Edit → Clear all removes both inputs and styles. Clearing a styled empty cell can
remove its sparse native record. All three use one reviewed `edit` operation and
ordinary named Undo/Redo; required input-change handlers can reject the entire
clear. Format-only changes do not invent input-change events. Existing selection,
hidden/projection, stale-state and size guards apply; read-only clears are refused.

## Directional fill contract

Fill down (Ctrl+D) snapshots each selected column's top cell and copies it down.
Fill right (Ctrl+R) snapshots each selected row's left cell and copies it right.
Both copy exact input kinds/styles and translate relative formula references
from each seed, retaining absolute reference components. Empty seeds clear
corresponding destinations; explicit empty Text and NULL keep their kinds.
The seed edge is included and stays unchanged. Each fill is one managed,
undoable operation; required handlers and all existing range/history/time limits
apply atomically. Seed snapshots are additionally capped at 4 MiB. Top-left copy
and row-major numeric series remain separate menu actions. Multi-step patterns
and Excel's automatic series inference are outside this tier.

## Address range contract

The address field accepts one current-sheet cell or rectangle, case-insensitive,
with optional absolute markers and surrounding whitespace: `$B$2:D20`. Reversed
endpoints normalize the rectangle and retain the first endpoint as the active
cell. Bounds remain A1:XFD1048576 and rectangles are limited to 10,000 cells.
Invalid, hidden-intersecting or projected targets retain the old selection.
Single frozen-row cells navigate in the visible pane; unfreeze before selecting
a rectangle that includes the frozen top row. Drafts require existing
Save/Discard/Cancel resolution before navigation, with target/state rechecked
after the dialog. Navigation does not mutate data or invoke hooks. Sheet names,
multiple areas, entire row/column notation and named ranges remain outside this tier.

## Find navigation contract

Find in selection retains its original case-sensitive source/display rectangle;
selecting a match does not narrow that scope. Next/Previous wrap in source
row/column order over the returned matches. Up to the first 500 are navigable;
when truncated the status names both the 500-result boundary and total count,
and asks users to narrow the search for the remainder. No matches/errors disable
navigation. Busy operations, refreshed/reset models, new workbooks/sheets and
list projections discard cached results; each navigation checks workbook/sheet/
state again. Unsubmitted drafts use Save/Discard/Cancel. A hidden next match is
reported and retains the current result position until shown. Single frozen-row
matches focus the visible pane. Find/Next/Previous are read-only and do not
invoke change handlers. Replace All retains its separate reviewed atomic flow.

## Selection summary contract

`POST /v1/workbooks/{workbook}/summary` and SDK `book.summary(sheet=..., **scope)`
read one rectangle of stored inputs in one SQLite read transaction. Limits are
10,000 selected cells, 4 MiB selected source/style bytes, two seconds and the
existing shared formula parser/dependency/cache budgets. A concurrent WAL writer
cannot mix old and new values in a summary; the next read observes its new state.
No history, receipts, hooks or stored calculation-cache updates are produced.
Read-only workbooks are supported. Projected row lists are refused.

Count includes every non-NULL value (including empty Text and formula errors),
but excludes absent cells, native NULL and styled empty auto cells. Numeric
count/sum/average use actual numeric values, excluding Text and Booleans. Formula
results participate and percentage fractions remain unscaled. Empty numeric sets
have sum 0 and no average. Any formula error, including cycles/resource errors,
makes both sum and average unavailable, with total errors and the first 100
locations/codes reported. Running-subtotal overflow is #NUM! and also makes both
unavailable even if a mathematical mean could be finite. No partial numeric
aggregate is presented. Budget or invalid-input failures return structured errors.

The desktop debounces selection changes by 250 ms, coalesces to one outstanding
summary read per service connection, and rejects superseded selection/state or
connection replies. Operations, service loss, model reset and external refresh
invalidate summaries; detected state changes request ordinary stored-data refresh,
never historical change handlers. One visible source rectangle is required;
oversized, hidden-intersecting, multiple-area and list selections show an explicit
unavailable explanation. The summary has its own accessible status label, with
full text in its tooltip and stored-data/draft semantics. It does not replace
storage/calculation status or show per-viewport totals. Sum and average use 12
significant display digits; REST returns the underlying finite binary64 values.

## PR record

1. [Aggregate functions #35](https://github.com/hybridmachine/DataHorizon/pull/35): locally verified, self-reviewed and merged.
2. [Logical functions #36](https://github.com/hybridmachine/DataHorizon/pull/36): locally verified, self-reviewed and merged.
3. [IFERROR #37](https://github.com/hybridmachine/DataHorizon/pull/37): locally verified, self-reviewed and merged.
4. [Numeric functions #38](https://github.com/hybridmachine/DataHorizon/pull/38): locally verified, self-reviewed and merged.
5. [Percentage formatting #39](https://github.com/hybridmachine/DataHorizon/pull/39): locally verified, self-reviewed and merged.
6. [Clear commands #40](https://github.com/hybridmachine/DataHorizon/pull/40): locally verified, self-reviewed and merged.
7. [Directional fill #41](https://github.com/hybridmachine/DataHorizon/pull/41): locally verified, self-reviewed and merged.
8. [Range navigation #42](https://github.com/hybridmachine/DataHorizon/pull/42): locally verified, self-reviewed and merged.
9. [Find navigation #43](https://github.com/hybridmachine/DataHorizon/pull/43): locally verified, self-reviewed and merged.

10. [Selection summaries and batch verification #44](https://github.com/hybridmachine/DataHorizon/pull/44): local build, contract, native and package qualification evidence recorded.

[Final local evidence](evidence/spreadsheet-batch-verification.txt) and the
[qualification report](evidence/spreadsheet-ten-qualification.json) preserve the
source candidate and archive inventory/hash. Platform/manual/distribution gates remain open.
