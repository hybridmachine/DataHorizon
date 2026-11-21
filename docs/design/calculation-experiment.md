# Calculation stage 1: measurement result

November 18, 2026 | Branch `claude/calculation-stage-1` | [Plan](calculation-stage-1-plan.md) | [Design](calculation-design.md)

Stage 1 is isolated test code: prototypes of input digests, dependency data and
dependency-ordered evaluation, and a benchmark. No service or format code
changed; the branch also carries one unrelated desktop fix, described in the
[status](implementation-status.md). This document records what was measured and the choices it fixes
for stages 2 to 5.

## Result

Five of the seven thresholds passed. The two that failed were memory for
dependency data and file growth. File growth passes when sheets are numbered
with small integers instead of repeating their text identifiers. Memory passes
with a different, integer-keyed data structure, measured as an extra probe and
not as a tested implementation. The planned fallback for the memory failure,
finding dependents by SQL query, was not shown viable: the query as prototyped
took 41.6 s on this fixture.

Several passes are specific to the fixture's shape. See
[Limits of the fixture](#limits-of-the-fixture) before planning from them.

| # | Measure | Threshold | Measured (run 2) | Run 1 | Result |
| --- | --- | --- | --- | --- | --- |
| T1 | Digest of every block, added to opening | ≤ 2.0 s | 1.29 s best (BLAKE2b, stream); 1.80 s for the chosen SHA-256, stream, 64 rows | 1.29 s (SHA-256, stream, 64 rows) | Pass |
| T2 | Digest update for a one-cell edit | ≤ 1 ms | 3 µs (sum); 0.73–0.83 ms (stream, 64 rows) | 2 µs; 0.74–0.78 ms | Pass; thin for the chosen stream mode |
| T3 | Loading dependency data | ≤ 1.0 s and ≤ 64 MiB | 0.41 s, 131.7 MiB | 0.49 s, 131.6 MiB | **Fail** (memory) |
| T4 | Affected set for an edit of `A1` | ≤ 0.5 s | 0.057 s, 102,003 formulas | 0.053 s | Pass |
| T5 | Recalculating the 100,000-row chain | ≤ 10 s | 3.05 s (30.5 µs per formula) | not isolated | Pass |
| T6 | `SUM` over 100,000 cells, streamed | ≤ 0.25 s | 0.196 s | 0.191 s | Pass |
| T7 | File growth from derived tables | ≤ 25% | 57.5% | 57.5% | **Fail** |

Extra probes, with integer sheet numbers and integer cell keys:

| Measure | Measured |
| --- | --- |
| Loading dependency data | 0.27 s, 45.7 MiB (within T3) |
| Affected set for an edit of `A1` | 0.067 s |
| File growth, precedents and results, no secondary index | 11.0% (within T7) |

The fixture has 1,002,003 cells and 102,003 formulas in a 69.9-MiB native file:
900,000 inputs, the 100,000-row chained running total, 2,000 rows of the prefix
form and three summary formulas. Host: Apple Silicon, macOS 26.6.2, Python
3.14.6, SQLite 3.53.4. Timings are medians of five runs in one process with a
warm file cache, except: the dependency-data loads are five fresh processes;
digest updates are medians of 1,001 (sum) and 101 (stream) samples; the prefix
column and the precedents save are single runs. The benchmark's `passed` flags
for T1 and T2 take the best configuration for each, which are different modes;
the table above gives the chosen configuration's figures as well. Evidence: [run 2](evidence/calculation-experiment.json) on
revision `15af516`, [run 1](evidence/calculation-experiment-run1.json) on
`902d4d0`.

## Choices this fixes

**Digest: one sequential hash per block, 64 rows per block. A close call.**
Two modes were measured. Summing per-cell hashes makes a one-cell edit cost
2 to 3 µs whatever the sheet's shape, but its scan of a million cells took
1.87 to 2.75 s across hashes, block heights and runs (best medians 1.87 and
1.92 s), so it passes T1 only narrowly. Feeding one hasher per block in key
order scans in 1.29 to 1.80 s, about 0.6 s faster, but a one-cell edit must
re-read and re-hash its block: 0.73 to 0.83 ms at 64 rows (73 to 83% of the
T2 limit, with single samples up to 4.4 ms), 2.9 to 3.7 ms at 256 and 11 to
13 ms at 1,024. That was on a 10-column sheet and counts reading and hashing
only, not writing the digest row. The stream mode at 64 rows is chosen because
opening is the cost every user pays; stage 3 should re-decide after measuring
a wide sheet, where the sum mode's shape-independent update may win. BLAKE2b
(16 bytes) and SHA-256 are within the run-to-run noise of each other; SHA-256
is chosen because any external tool can compute it. **Call**

**Verification runs during opening.** For the chosen configuration the scan
took 1.29 s in run 1 and 1.80 s in run 2. Fetching the rows without hashing
took 0.77 s in run 2 and a noisy 1.37 s in run 1. Opening this workbook takes
0.37 to 0.66 s today, so with the digest it would take about 2.0 to 2.2 s,
inside the five-second budget. The background fallback in design section 6.3
is not needed for this workload. Stage 3 must still re-run the
[opening benchmark](native-opening-performance.md) with the digest on its
270,000-row long-text shape, which is already near the budget, before relying
on this.

**Dependency data is held in memory, in a compact integer-keyed form, for
workbooks of this size.** The tuple-keyed prototype needs 131.7 MiB for
102,003 formulas. A probe that packs each cell into one integer, and drops the
per-formula rectangle lists, flags and bisection arrays, needs 45.7 MiB and
loads in 0.27 s. It is a different data structure, validated only by the count
of dependents it finds. Sharing one object per sheet identifier is not enough
on its own: a review probe (not in the evidence files) measured 108.5 MiB that
way. The compact form scales linearly, at roughly 2,200 formulas per MiB on
this shape, so about 140,000 formulas fit the T3 limit and about 570,000 would
fill the whole 256-MiB service target. No measured option covers a million
formulas.

**The lookup files ranges by column and bisects on the first row.** It then
scans every range in that column that starts at or before the changed row.
That is fast here because the fixture's 2,001 ranges all sit over an input
column, which is looked up once. A review probe with 2,000 prefix sums over
the formula column (`SUM(J$1:J<r>)`) took 4.6 s, nine times T4's limit. Stage 3
needs an interval structure that does not degrade this way, and must measure
ranges over formula columns.

**Dependents by SQL query: not shown viable, not ruled out.** The recursive
query over the stored precedents took 41.6 s (48.8 s in run 1) against 0.057 s
in memory. The cost is the range join: the prototype's index cannot bound it,
so each of the 102,003 affected cells scans all 2,001 ranges. Review probes
(not in the evidence files) measured the same query at 0.23 s with no prefix
ranges in the workbook, 11.3 s with a wider index, and 1.3 s for the chain
using one indexed single-cell query per cell from Python. A SQL path with a
better range lookup remains open for workbooks too large for memory.

**Derived tables use small integer sheet numbers and no secondary index.** With
text identifiers the precedents table is 18.8 MiB, its two indexes 9.7 MiB,
flags 5.4 MiB and results 6.3 MiB. With integer sheet numbers and the cell as
primary key, precedents are 5.3 MiB and results 2.4 MiB. The compact tables
left out the flags and formulas with no precedents, so 11.0% slightly
understates a complete format. Format v9 therefore needs a derived table that
numbers the sheets.

**Per-formula cell limit: 4,194,304 cells (proposed).** A streamed range cost
about 2 µs per cell, which would make the limit about 8 s for one formula and
allow four whole columns. That rate was measured only on a column of short
numbers; text, dates and formula cells take a slower path that was not timed,
so 8 s is not a worst case. Reading a range cell by cell through the existing
functions costs 11 to 18 µs per cell (1.1 to 1.8 s for the 100,000-cell
`SUM`), so stage 2 must stream every range-reading function, not only `SUM`,
before this limit is honest. Stage 2 must also decide whether the limit counts
every coordinate in a range, as the prototype's cell-by-cell path does, or
only stored cells, as a stream sees. **Call**

**Result writes are short.** Writing 102,003 results in transactions of 1,000
took 0.18 s in total; the longest transaction was 3.4 ms. This is a best case:
first-time inserts in key order on a plain connection with no concurrent
writer. Whether they stay out of the way of edits inside the service was not
measured.

## The prefix-form running total

`=SUM(A$1:A<r>)` filled down 2,000 rows read 2,001,000 cells in 26 to 31 s
through the existing cell-by-cell path (65 to 76 formulas per second). The same
form over 100,000 rows reads about five billion cells: roughly 2.7 hours at the
streamed rate and 18 to 21 hours cell by cell. Under the design it shows no
`#LIMIT!` and simply takes that long in the background. That is correct as
designed and poor in practice; a later stage could recognise a column of
growing prefix ranges and reuse the previous row's partial sum. Not planned.

It also means an edit of `A1` in the fixture, which every prefix formula reads,
took 24 to 52 s in total, of which the chain is 3 s.

## What surprised

- The opening scan is not a free ride. Fetching a million cells into Python
  cost 0.77 s in run 2 before any hashing; opening today fetches none.
- Storing precedents saves less than expected: parsing 102,003 formulas takes
  0.83 to 0.98 s, against 0.41 to 0.49 s to load the tuple form and 0.27 s for
  the compact probe. The saving grows with the formula count, so the design
  stands, but see the memory limit above.
- Evaluating `=J<r-1>+A<r>` costs 30.5 µs per formula. Each one makes two SQL
  reads (its own source and `A<r>`) and one parse; the split was not profiled.
- The benchmark process itself peaked at 246 MiB holding all three prototypes
  and every result as Python objects. That is not a service figure, but the
  256-MiB service target will need the compact forms.
- Run-to-run variation was large for the digest scans (single hashing scans
  between 1.26 and 4.5 s, and the fetch alone between 0.74 and 3.2 s); the
  machine was not idle.
  Medians are reported and both evidence files are kept.

## Limits of the fixture

- Every range sits over an input column. No formula reads a range of other
  formulas, which is the shape that slows both the dependents lookup and the
  ordering step (one edge per formula cell inside a range).
- About 98% of precedents are single cells.
- The sheet is 10 columns wide and its values are short.
- `evaluate()` was always given a closed set of formulas. A caller that passes
  a set whose precedents are neither in it nor in the results gets a
  `KeyError`, and a cycle with a member outside the set is not detected.
- Formulas in or downstream of a possible cycle are evaluated by recursion.
  A long chain of them hits Python's recursion limit and gets `#LIMIT!`; the
  service needs an iterative form.
- `_encoded` assumes `source` and `kind` are text. A direct SQLite edit can
  store a BLOB there, and stage 3's scan must not fail on one.

## Not measured

- A cold file cache. Windows and Linux.
- Sheets wider than 10 columns. A 64-row block of a 100-column sheet holds ten
  times the cells, and its update would cost about ten times as much. Stage 3
  should measure this and, if it matters, bound blocks by columns as well as
  rows.
- Long text cells in the digest (the opening benchmark's shape).
- The integer-keyed form as a full implementation: it was a load, memory and
  traversal probe, not the tested `DependencyData`.
- Anything inside the service: locking, the worker, concurrent edits.

## Departures from the plan

- The first full run showed that T5's measure (an edit of `A1`) also
  recalculated the 2,000 prefix formulas. A chain-only measurement was added
  and T5 uses it; the edit-of-`A1` figure is kept beside it.
- Integer-keyed probes were added after T3 and T7 failed and the SQL fallback
  proved unusable. The thresholds are still judged on the plan's definitions.
- Two test methods and one extra assertion were added beyond the plan's 19
  tests, for a total of 21. Two rounds of pull-request review added seven
  more, for 28.
- A whole-branch review corrected this document: the opening cost for the
  chosen hash, the basis for choosing the stream mode, the cause of the memory
  failure and the standing of the SQL path. Its probes are cited where used
  and are not in the evidence files.

- The pull-request review found four defects in the prototype and benchmark,
  fixed after both evidence runs. The prototype gave `#CYCLE!` to every
  formula in a static cycle, where the service follows only the branch taken
  (`=IF(FALSE,A1,42)` in `A1` is 42); it matched sheet names with Python's
  `lower()`, where the service folds ASCII letters only; the streamed `SUM`
  had no `ORDER BY`, so its rounding depended on the query plan; and the
  benchmark failed when `--rows` was below `--prefix-rows`. The fixture has no
  cycles and one sheet-name spelling, and the ordered scan uses the same
  primary-key search (0.12 s for the 100,000-cell `SUM` when rechecked), so
  the evidence was not rerun.
- A second review round found three more. A formula reached by following
  references charged its reads to the formula that reached it, so the
  per-formula limit was not per formula; the streamed `SUM` reported its own
  overflow before a later cell's error, where the service reads the whole
  range first; and the benchmark needed the Unix-only `resource` module, so
  it now reports memory, and T3, as unmeasured where that is missing. None
  changes a measured figure.

## Next

Stage 2: iterative evaluation and streamed range functions in the service,
behind today's contracts, planned from these numbers.
