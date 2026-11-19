# Calculation stage 1: measurement result

November 18, 2026 | Branch `claude/calculation-stage-1` | [Plan](calculation-stage-1-plan.md) | [Design](calculation-design.md)

Stage 1 is isolated test code: prototypes of input digests, dependency data and
dependency-ordered evaluation, and a benchmark. No service, desktop or format
code changed. This document records what was measured and the choices it fixes
for stages 2 to 5.

## Result

Five of the seven thresholds passed. The two that failed (memory for dependency
data, and file growth) both pass when sheets are numbered with small integers
instead of repeating their text identifiers; that form was measured as an extra
probe. The planned fallback for the memory failure, finding dependents by SQL
query, is not usable.

| # | Measure | Threshold | Measured (run 2) | Run 1 | Result |
| --- | --- | --- | --- | --- | --- |
| T1 | Digest of every block, added to opening | ≤ 2.0 s | 1.29 s (stream mode) | 1.29 s | Pass |
| T2 | Digest update for a one-cell edit | ≤ 1 ms | 3 µs (sum); 0.73–0.83 ms (stream, 64 rows) | 2 µs; 0.74–0.78 ms | Pass |
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
warm file cache. Evidence: [run 2](evidence/calculation-experiment.json) on
revision `15af516`, [run 1](evidence/calculation-experiment-run1.json) on
`902d4d0`.

## Choices this fixes

**Digest: one sequential hash per block, 64 rows per block.** Summing per-cell
hashes costs 1.9 to 2.8 s per million cells, at or over the threshold in both
runs. Feeding one hasher per block in key order costs 1.3 to 1.8 s, of which
0.77 s is fetching the rows. A one-cell edit must then re-read and re-hash its
block: 0.73 to 0.83 ms at 64 rows, 2.9 to 3.7 ms at 256 and 11 to 13 ms at
1,024, so only 64 rows meets T2. BLAKE2b (16 bytes) and SHA-256 are within the
run-to-run noise of each other; SHA-256 is chosen because any external tool
can compute it. **Call**

**Verification runs during opening.** Opening this workbook takes 0.37 to
0.66 s today; with the digest it would take about 1.7 to 2.0 s, inside the
five-second budget. The background fallback in design section 6.3 is not
needed for this workload. Stage 3 must still re-run the
[opening benchmark](native-opening-performance.md) with the digest on its
270,000-row long-text shape, which is already near the budget, before relying
on this.

**Dependency data is held in memory, with integer keys.** The tuple-keyed
prototype needs 131.7 MiB for 102,003 formulas, because every row read from
SQLite carries its own copy of two 36-character sheet identifiers. The same
data keyed by integers needs 45.7 MiB and loads in 0.27 s. Finding dependents
with a recursive SQL query over the stored precedents took 41.6 s (48.8 s in
run 1) against 0.057 s in memory, so the plan's fallback is rejected.

**Derived tables use small integer sheet numbers and no secondary index.** With
text identifiers the precedents table is 18.8 MiB, its two indexes 9.7 MiB,
flags 5.4 MiB and results 6.3 MiB. With integer sheet numbers and the cell as
primary key, precedents are 5.3 MiB and results 2.4 MiB. Format v9 therefore
needs a derived table that numbers the sheets.

**Per-formula cell limit: 4,194,304 cells (proposed).** A streamed range costs
about 2 µs per cell, so the limit is about 8 s for one formula at worst and
allows four whole columns. Reading a range cell by cell through the existing
functions costs 11 to 15 µs per cell (1.1 s for the 100,000-cell `SUM`), so
stage 2 must stream every range-reading function, not only `SUM`, before this
limit is honest. **Call**

**Result writes do not block edits.** Writing 102,003 results in transactions
of 1,000 took 0.18 s in total; the longest transaction was 3.4 ms.

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
  costs 0.77 s before any hashing; opening today fetches none.
- Storing precedents saves less than expected: parsing 102,003 formulas takes
  0.83 to 0.98 s against 0.27 to 0.41 s to load them. It still matters at ten
  times the formula count, so the design stands.
- Evaluation cost per formula (30.5 µs for `=J1+A2`) is dominated by one SQL
  read per input cell and one parse per formula, not by arithmetic.
- The benchmark process itself peaked at 246 MiB holding all three prototypes
  and every result as Python objects. That is not a service figure, but the
  256-MiB service target will need the compact forms.
- Run-to-run variation was large for the digest scans (single runs between
  0.74 and 4.5 s for the same query plus hashing); the machine was not idle.
  Medians are reported and both evidence files are kept.

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
- Three tests were added beyond the plan's list, for a total of 21.

## Next

Stage 2: iterative evaluation and streamed range functions in the service,
behind today's contracts, planned from these numbers.
