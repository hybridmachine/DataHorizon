# Formula coverage: follow-on ten increments

Authorized November 7, 2026 as the next ten after the formula editing batch.
Implement in order; each has its own local build/tests, commit, push, PR,
published-head author review and merge. Hosted Actions credits are exhausted.
Shared engine operations, catalog, REST clients, resource limits, typed inputs,
transactional hooks and explicit XLSX/ODS compatibility remain authoritative.

1. ISBLANK, ISNUMBER, ISTEXT, ISLOGICAL, ISERROR and ISNA scalar inspection.
2. IFNA with lazy fallback only for #N/A.
3. EXACT case-sensitive scalar Text comparison.
4. SUBSTITUTE literal non-overlapping Text replacement and occurrence selection.
5. REPLACE positional Unicode Text replacement.
6. FIND case-sensitive Unicode Text position.
7. SEARCH case-insensitive bounded wildcard Text position.
8. TEXTJOIN with scalar delimiter, ignore-empty and row-major ranges.
9. COUNTIFS and SUMIFS with aligned AND criteria.
10. Scalar INDEX and exact MATCH with explicit mode 0.

## Shared contract and validation

Retain explicit scalar conversion and Unicode code-point semantics. Do not parse
stored Text identifiers, infer locale, support arrays implicitly or hide cycles/
resource errors. Text results remain bounded before allocation; work/deadlines
cover scans and wildcard matching. Missing or unsupported lookup modes reject;
no approximate-match default is silently changed. These are native tested tiers,
not every Excel coercion or target application's recalculation behavior.

The existing catalog drives Help/completion/hints and interchangeable supported
names. Verify each increment's typed success/error/boundary cases, every catalog
example, formula/value packages and independent readers; run the native source
editor against catalog examples. Finish with full local CTest, optional-binding
suite, native scales, installed relocation and archive inventory qualification.
Windows/Linux, physical-input, accessibility, clean-machine runtime/signing and
distribution/license gates remain open.

Common signatures are informed by Microsoft's [IS functions](https://support.microsoft.com/en-us/excel/functions/is-functions),
[IFNA](https://support.microsoft.com/en-us/excel/functions/ifna-function),
[SUBSTITUTE](https://support.microsoft.com/en-us/excel/functions/substitute-function),
[SEARCH](https://support.microsoft.com/en-us/excel/functions/search-function),
[COUNTIFS](https://support.microsoft.com/en-us/excel/functions/countifs-function),
[INDEX](https://support.microsoft.com/en-us/excel/functions/index-function) and
[MATCH](https://support.microsoft.com/en-us/excel/functions/match-function).
Modern XLSX identifiers follow [MS-XLSX](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-xlsx/5d1b6d44-6fc1-4ecd-8fef-0b27406cc2bf);
TEXTJOIN's ODS extension follows [LibreOffice](https://help.libreoffice.org/latest/en-US/text/scalc/01/func_textjoin.html).

## Completed increments

1. [Scalar inspection #65](https://github.com/hybridmachine/DataHorizon/pull/65): explicit type/error distinctions; locally built/tested, self-reviewed and merged.

2. [Lazy IFNA #66](https://github.com/hybridmachine/DataHorizon/pull/66): locally built/tested, self-reviewed and merged.

## Scalar inspection tier

ISBLANK recognizes blank/NULL, including a formula's scalar NULL result; empty
Text is Text. ISNUMBER excludes Boolean and numeric Text. ISTEXT and ISLOGICAL
recognize exact scalar types. Ordinary spreadsheet errors return false for type
predicates; ISERROR recognizes all ordinary errors and ISNA only #N/A. Ranges
reject before scanning; cycles and resource failures propagate even through
inspection. This native scalar tier does not promise Excel's reference-only
ISBLANK distinction or array behavior.

## IFNA tier

IFNA shares the two-scalar/lazy fallback contract with IFERROR but catches only
#N/A. All other errors, including cycles and limits, propagate; a successful
blank/NULL becomes empty Text. XLSX uses `_xlfn.IFNA`; ODS uses standard IFNA
([OpenFormula 1.3, §6.15.6](https://docs.oasis-open.org/office/OpenDocument/v1.3/os/part4-formula/OpenDocument-v1.3-os-part4-formula.pdf)).
