# Next ten spreadsheet implementation increments

November 7, 2026. No next-ten batch was previously recorded after PR #34.
The default scope follows the remaining M3–M5 spreadsheet foundation, with an
optional direction question pending. Execute in order, one implementation PR at
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
