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

## Completed PRs

1. [Function catalog #55](https://github.com/hybridmachine/DataHorizon/pull/55): local build/full CTest and native help checks passed; self-reviewed and merged.
