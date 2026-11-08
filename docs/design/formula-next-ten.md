# Formula editing and everyday coverage: next ten increments

Authorized November 7, 2026 after PR #54. Complete each increment with local
build/tests, commit/push, published PR self-review and merge before the next.
Hosted Actions credits are exhausted. Preserve the shared engine/REST boundary,
exact stored inputs, transactional hooks, explicit compatibility tiers and
bounded reads/calculations. This batch does not qualify a cross-platform release.

1. Shared implemented-function catalog with signatures/examples and desktop help.
2. Formula autocomplete and argument hints in source and in-cell editors.
3. Click/drag reference insertion into an unsubmitted formula draft.
4. Reference highlighting and matching parentheses.
5. LEN, TRIM, UPPER, LOWER.
6. LEFT, RIGHT, MID.
7. Text concatenation with & and CONCAT.
8. COUNTIF and SUMIF.
9. Bounded exact-match XLOOKUP.
10. Bounded source-state-checked trace precedents/dependents.

## Function catalog

`GET /v1/functions` and SDK `Client.functions()` return the versioned metadata
for the functions implemented by the connected service: names, signatures,
categories, rules and worked examples, plus shared type/resource limitations.
The evaluator's supported-name set and interchange gates use that same registry.
Help → Formula functions displays searchable plain-text details without requiring
a workbook or reading/editing stored data. Errors are visible; service changes
invalidate cached metadata. This is development-tier help, not an Excel parity
claim. New function stages extend this registry with their tested semantics.

## Autocomplete and argument hints

Source and in-cell editors, including frozen panes, use the connected service's
catalog. Typing a function prefix in an automatic/formula input offers up to eight
visible suggestions. Ctrl+Space opens completion explicitly. Enter/Tab accepts
one suggestion into the unsubmitted draft; Enter with no popup retains ordinary
commit behavior. Escape first dismisses the popup, then ordinary Escape cancels
the draft. Up/Down chooses a suggestion. Existing opening parentheses are not
duplicated and the rest of the input stays intact.

Hints show the innermost known function signature and current argument, ignoring
quoted Text/sheet names and nested parentheses when counting separators. This is
a tolerant editing aid, not evaluation or validation of incomplete formulas.
Literal Text inputs and read-only/disabled editors do not complete. Function
metadata loads without workbook reads, is cleared on service changes and is never
substituted with a hardcoded client function list. Assistance does not submit an
operation, invoke hooks or change the draft's original target/type. Shared parser,
calculation and exact-input limits remain authoritative on submission.

## Click/drag reference insertion

Pick reference beside the source bar (also in Edit) enables one explicit gesture
in the current source sheet. A clicked cell or dragged rectangle is previewed in
the status; release inserts a relative A1 reference into the draft's operand or
selected text. Existing automatic selection/draft-resolution behavior is unchanged
outside this mode. The original current cell, target, input type and stored state
remain fixed. Picking in an in-cell editor transfers that unsubmitted input and
cursor to the source bar without committing it; editing then continues there.

Body, top, left and corner panes use source coordinates. One completed insertion
is a separate native text Undo action. Escape before release cancels picking and
retains the draft; ordinary Escape then discards the draft. Only visible cells
can finish a gesture; hidden/intersected or over-10,000-cell ranges reject without
inserting a partial range. No edge auto-scroll or cross-sheet picker is introduced.
Quoted Text, explicit literal Text, read-only and projected inputs are guarded.
Reset, service and busy-state changes cancel picking; changed target/text/cursor
prevents insertion. Enter still performs one ordinary managed edit with existing
formula, hook, rejection and Undo semantics. Picking itself makes no REST mutation.

## Reference and parenthesis cues

Automatic/formula inputs in both editors underline recognized A1 references with
stable colors and outline matching visible source cells through the shared grid
model. Repeated references share a color; overlapping cells show at most three
outlines. A plain-text legend identifies the reference spellings independently
of color. The same source-coordinate mapping works in frozen and projected views;
other-sheet references are underlined but only current-sheet visible cells are
outlined. No referenced cells are fetched merely to highlight them.

The adjacent matching parenthesis pair is boxed, unmatched parentheses are marked
and described, and quoted Text/sheet names do not contribute parentheses or false
references. Native glyphs, selection and cursor painting are retained; colored
underlines/boxes are drawn with Qt text-layout/cursor geometry and clipped to the
editor contents. Argument tips are kept within the available screen geometry.

This is tolerant, bounded visual analysis of incomplete drafts, not parser or
calculation validation. It recognizes ordinary A1/rectangular spellings; complex
parenthesized range spellings can appear as separate endpoint cues. Cues are capped
at 64 references/nesting 128, with an explicit visual-limit note; source remains
intact. Literal Text disables them. Selection, source/cell editor changes and
cancel/close update the visible cues without stored edits, hooks or history.
The service parser and calculation errors remain authoritative on submission.
Programmatic startup address clears also reset their modified flag, so they do
not masquerade as an unfinished user address draft; actual address drafts remain
guarded.

## Scalar text cleanup tier

LEN/TRIM/UPPER/LOWER each take one scalar. Blank/NULL becomes empty Text; Boolean
becomes TRUE/FALSE; finite numbers use invariant unformatted canonical text
(safe integral values have no decimal suffix, other values retain their canonical
Python spelling). Existing Text, including leading-zero identifiers and formula-
looking strings, is never parsed as a number or formula. Direct ranges are rejected
before reading their members. Ordinary referenced errors propagate; cycles and
resource failures remain visible through IFERROR.

LEN counts Unicode code points, including spaces, combining marks and variation
selectors separately. TRIM changes only U+0020 ASCII spaces: strip outer spaces
and collapse internal runs; tabs/newlines/NBSP remain. UPPER/LOWER use Python
Unicode casing, independent of locale, including possible expansions. Text
results are limited to 32,767 characters and 64 KiB UTF-8 (#LIMIT! with no
truncation); LEN may count a longer valid input. Work charges one calculation
step per 256 input characters in addition to ordinary AST/cell work and deadlines.
Native schema, stored sources and history grouping remain unchanged.

The common signatures are informed by Microsoft's [LEN](https://support.microsoft.com/en-us/excel/functions/len-function),
[TRIM](https://support.microsoft.com/en-us/excel/functions/trim-function) and
[UPPER](https://support.microsoft.com/en-us/excel/functions/upper-function) references.
This stage explicitly chooses code-point/casing/number rules rather than claiming
all Excel versions/locales: Unicode counting can depend on a target workbook's
compatibility version. XLSX/ODS formulas and typed values round-trip in the
supported tier; independent readers verify stored formulas/types, not target
recalculation. XLSX still enforces its separate UTF-16 text-size limit.

## Completed PRs

1. [Function catalog #55](https://github.com/hybridmachine/DataHorizon/pull/55): local build/full CTest and native help checks passed; self-reviewed and merged.

2. [Autocomplete and hints #56](https://github.com/hybridmachine/DataHorizon/pull/56): locally verified, self-reviewed and merged.

3. [Reference picking #57](https://github.com/hybridmachine/DataHorizon/pull/57): locally verified, self-reviewed and merged.

4. [Reference/parenthesis cues #58](https://github.com/hybridmachine/DataHorizon/pull/58): locally verified, visually inspected, self-reviewed and merged.

5. [Scalar text cleanup #59](https://github.com/hybridmachine/DataHorizon/pull/59): locally verified, self-reviewed and merged.
