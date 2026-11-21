# Calculation stage 2: result

November 21, 2026 | Branch `claude/calculation-stage-2` | [Plan](calculation-stage-2-plan.md) | [Design](calculation-design.md) | [Stage 1 result](calculation-experiment.md)

Stage 2 changes how the service evaluates formulas, behind today's REST, CLI,
SDK and file contracts. A dependency chain is evaluated with an explicit stack
instead of recursion, a range is read as an ordered stream instead of a list of
cells, and the in-memory dependency cache records a range as one rectangle.
The file stays format v8. Nothing is stored between reads; that is stage 3.

## Result

The 128-cell chain limit and the 10,000-cell range limit are gone. A chain of
20,000 formulas and a `SUM` over 20,000 cells return values wherever their time
budget allows, and the characterization corpus (178 cases) passes with no case
re-recorded. The one target the plan set, S1, was **missed**: a `SUM` over
100,000 stored cells still shows `#LIMIT!` in a default 0.25 s read. The other
five measures had no target; their figures are below, with what they mean for
stages 3 and 4.

Figures are medians of five runs in one process with a warm file cache. Host:
Apple M1 (8 cores), macOS 26.6.2, Python 3.14.6, SQLite 3.53.4. **The machine
was not idle:** the load was not controlled. The evidence records the load averages
(1, 5 and 15 minutes) at the end of each run: 11.92, 12.25 and 9.35 for the final
run, and 5.14, 6.29 and 6.51 for the first run, which was taken before the fix
described under S2. Stage 1 also ran on a machine that was not idle, so compare
the two sets of figures with care. Evidence: [calculation-stage-2.json](evidence/calculation-stage-2.json),
produced by `tests/calculation_stage2_benchmark.py` on revision `09b21cc`, which
holds the full set of per-run timings. The first run, on `e28415d` before the
cache fix described under S2, is kept as
[calculation-stage-2-run1.json](evidence/calculation-stage-2-run1.json).

What the benchmark uses: the fixtures are written with SQL through the
workbook's connection. S1 and the default-read column of S2 call
`Workbook.read`. The 2 s columns of S2, S3, S4, S5 and S6 call
`Workbook.calculator()` (the cached calculator, or the plain one with
`transactional=True`) directly, with the budget given, not the default read path.
S5 calls `book.dependencies.invalidate` directly, and every run clears or reads
`book.dependencies` (its results, ranges and `checks` counter). The probe
formulas of S3 and S4 are written with `book.db.execute`. S6 runs in a fresh
process and also uses a plain calculator.

| # | Measure | Target | Measured | Result |
| --- | --- | --- | --- | --- |
| S1 | `=SUM(Sheet1!A1:A100000)` in one default read (0.25 s) | ≤ 0.25 s | `#LIMIT!` in all five runs (0.2506 s to give up); the same sum takes 0.39 s with a 2 s budget (3.9 µs per cell) | **Missed** |
| S2 | Longest chained running total one read completes | none | Default read: 8,192 (0.23 s), 16,384 fails. List budget (2 s), cached or plain calculator: 32,768, 65,536 fails. Before the cache fix a default read completed 512 and then 4,096 but not 1,024 or 2,048 | See below |
| S3 | Cost per stored cell of a streamed range | 2 µs assumed by the design | 3.7 µs numbers, 2.7 µs 40-character text, 3.9 µs dates, 31.7 µs formula results | Slower than assumed |
| S4 | Conditional aggregates and a lookup over 100,000 rows | none | `COUNTIF` 0.58 s; `SUMIFS` with three criteria 1.93 s; `XLOOKUP` to the last row 0.89 s | Each exceeds a default read |
| S5 | `invalidate` for one edit with 4,096 cached range readers | none | 2.7 ms when the edit falls inside 11 readers; 0.47 s when it falls inside 2,098 (8.6 million containment checks) | See below |
| S6 | Peak resident memory of a `SUM` over 1,000,000 stored cells | none | The sum added at most 32 KiB to the process peak (median 16 KiB); the process peaked at 46.3 to 46.4 MiB (44.2 to 46.3 MiB in the first run); the calculator cache held one entry. 4.15 s | Memory is bounded |

Between the first run and the final one every timing moved by 0 to 12%, mostly
faster (`SUMIFS` 2.18 s to 1.93 s, `COUNTIF` 0.64 s to 0.58 s, numbers 4.2 µs
to 3.7 µs per cell), although the load at the end of the second run was about twice that of the first.
The cause is not known; the code on the read path of S1, S3, S4 and S6 did not change
between the runs. Treat differences of that size as run-to-run noise on this
machine and read the figures to one significant digit. The only change that is
understood is S2.

### S1: why it is missed

The streamed `SUM` costs 3.9 µs per cell here (4.1 µs in the first run). Stage 1's prototype measured
2 µs, and the service adds the calculator's per-cell accounting, the type
conversion of each stored cell and the deadline check. Reading 100,000 cells
therefore takes about 0.4 s, above the 0.25 s default budget. The read gives up at
0.2506 s with `#LIMIT!` and the message `Calculation budget exceeded`. Before
stage 2 the same read failed too, for a different reason (a range over 10,000
cells), so no previous behaviour regressed; the range is simply not yet fast
enough to show in a grid read. By the same rate a `SUM` over about 60,000 stored cells fits the
default budget on this machine under this load (an estimate, not a run). A 2 s read (lists, find,
export, jobs) completes it.

### S2: how long a chain one read completes

The chain is `=J<r-1>+A<r>` in column J, read at its last cell with nothing
cached. Each length is one read; the evidence file holds them all.

| Chain length | Default read (0.25 s) | 2 s, cached calculator | 2 s, plain calculator |
| --- | --- | --- | --- |
| 256 | 0.01 s, done | 0.01 s, done | 0.01 s, done |
| 512 | 0.02 s, done | 0.02 s, done | 0.02 s, done |
| 1,024 | 0.04 s, done | 0.04 s, done | 0.03 s, done |
| 2,048 | 0.07 s, done | 0.09 s, done | 0.06 s, done |
| 4,096 | 0.12 s, done | 0.16 s, done | 0.13 s, done |
| 8,192 | 0.23 s, done | 0.33 s, done | 0.26 s, done |
| 16,384 | `#LIMIT!` | 0.64 s, done | 0.52 s, done |
| 32,768 | `#LIMIT!` | 1.31 s, done | 1.05 s, done |
| 65,536 and above | `#LIMIT!` | `#LIMIT!` | `#LIMIT!` |

Chains are now linear for all three: about 30 to 40 µs per formula, so a 2 s
budget completes 32,768 and not 65,536, and a default read completes 8,192 and
sits close to its budget there (0.23 s here). A chain of 20,000 needs a 2 s
budget. The time at the longest length before the first failure is a median of
five reads: 0.24 s (8,192, default), 1.33 s (32,768, cached 2 s) and 1.05 s
(32,768, plain 2 s).

**A defect this measurement found, fixed in this stage.** The first run (on
`e28415d`) was not linear for ordinary reads. The calculator that reads use
keeps results in the dependency cache, and for each result it saved,
`DependencyIndex.cacheable` walked every precedent of the formula. For a chain
that walk is as long as the chain, so the cost was quadratic in chain length for
as long as the cache accepted results, until it turned itself off at its cap of
4,096 results. A default read completed 512 formulas (0.14 s) but failed at
1,024 and 2,048, completed 4,096 (0.13 s), and sat on its budget at 8,192; a 2 s
cached read took 0.55 s for 1,024 and failed at 2,048 while completing 4,096 to
32,768. The plain calculator, which has no cache, was linear throughout. It was
fixed in commit `09b21cc` ("Decide whether a result is cacheable
from its direct reads only"): `cacheable` now checks only a formula's direct
reads. After the fix a default read completes 512, 1,024, 2,048 and 4,096, and
8,192 as well, and the cached 2 s read completes 1,024 and 2,048 in 0.04 and 0.09
s where it took 0.55 s and failed. The first run is kept as
[calculation-stage-2-run1.json](evidence/calculation-stage-2-run1.json) so the
before and after are both on record. At 4,096 results the cache still turns
itself off, as designed.

Reads that fail also overrun their budget: a 0.25 s read gave up after up to
0.35 s, and a 2 s read after up to 2.74 s, because the deadline is checked
between units of work and a unit can be long.

### S3: cost per stored cell

`SUM` over one column of 100,000 cells, plain calculator, 60 s budget.

| Column | Median | Per cell |
| --- | --- | --- |
| Numbers (`row % 97`) | 0.37 s | 3.7 µs |
| Text, 40 characters | 0.27 s | 2.7 µs |
| Dates | 0.39 s | 3.9 µs |
| Formula results (`=A<r>`) | 3.17 s | 31.7 µs |

The formula column's figure includes evaluating each formula (each one reads
one cell and is parsed and evaluated); it is the cost of an uncalculated
column, the same order as the 30.5 µs per formula measured in stage 1, and not
the cost of reading results that already exist. Nothing here separates the two,
so the cost of streaming already calculated results is not measured.

### Does 4,194,304 still stand?

The count stands as a count: it bounds how many stored cells one formula reads,
it is enforced where the count is charged, and the same inputs always give the
same result. What does not stand is the time the design attached to it. The
design note assumed 2 µs per cell, so about 8 s for a formula that reads the
limit. At the measured rates the same formula takes about 11 s over text, 16 s
over numbers, 16 s over dates and over 2 minutes over a column of formulas that
have to be evaluated. Under today's time budgets (0.25 s, 2 s) that does not
matter, because the budget ends the read first; the limit is reached only by a
read with no time budget. Stage 4 removes the cumulative budget for background
recalculation, so it will have to decide whether to keep 4,194,304 as it is,
lower it, or weight formula cells more heavily than stored ones. This document
does not make that call.

### S4: conditional aggregates and lookups

Each over 100,000 rows, plain calculator, 60 s budget. All three run in one
pass over the streamed columns.

| Formula | Median | Per row |
| --- | --- | --- |
| `COUNTIF(A1:A100000,">50")` | 0.58 s | 5.8 µs |
| `SUMIFS(E,A,">10",F,"<500",B,"<>x")` (three criteria, four columns) | 1.93 s | 19.3 µs |
| `XLOOKUP(100000,E1:E100000,A1:A100000)` (match on the last row) | 0.89 s | 8.9 µs |

None fits a 0.25 s read at 100,000 rows. Each fits a 2 s read, the
three-criteria `SUMIFS` with little to spare (1.9 s here; 2.2 s in the first run). `SUMIFS` at
5 µs per cell per column is in line with a plain `SUM` at 4 µs, so there is no
per-criterion surprise; the cost is the number of cells read.

### S5: invalidation with 4,096 cached readers

With 4,096 formulas cached, each reading a range:

| Shape | Edit | Readers invalidated | Containment checks | Median |
| --- | --- | --- | --- | --- |
| `=SUM(A<r>:A<r+9>)`, ten cells each | one cell of column A | 11 | 45,056 | 2.7 ms |
| `=SUM(Z1:Z<r>)`, over an empty column | `Z2000`, an empty coordinate | 2,098 | 8,591,310 | 0.47 s |

The cache holds a range as one rectangle, so the number of edges is the number
of readers, not the number of cells they cover. An invalidation checks each
affected cell against every rectangle on the sheet and has no spatial index,
which is why the second shape costs 8.6 million checks: 2,098 affected cells
times about 4,096 rectangles. Both cases are inside the cache's bounds (4,096
results, 50,000 edges, 4 MiB), so the cost is bounded by them. The benchmark calls `invalidate` directly; the cost of an edit made through
`Workbook`, which invalidates inside its commit, was not measured, so what this
adds to an edit is an inference, not a figure. Stage 4's
dependents index should be an interval structure; see stage 1.

### S6: memory

A `SUM` over 1,000,000 stored cells, in a fresh process, did not hold the
cells: the peak resident memory rose by at most 32 KiB, the process peaked at
46.3 to 46.4 MiB, and the calculator's cache held one entry (the formula
itself). It took 4.15 s (4.1 µs per cell). A read of the same cells as a list
would hold about a million Python objects. Peak memory is measured with the
operating system's resident-set counter and is not available on Windows.

## What was built

- **Iterative evaluation.** `Calculator.cell` is a loop over an explicit stack.
  A formula that reads a formula cell with no value yet is set aside, the
  missing cell is calculated, and the waiting formula is evaluated again. Each
  formula is parsed once per calculator. Cycles are still decided by the
  references actually followed, so `=IF(FALSE,A1,42)` in `A1` is 42.
- **A per-formula cell count.** The 10,000-step count per read is replaced by
  `FORMULA_CELL_LIMIT = 4_194_304`: the stored cells one formula reads, counting
  stored cells for streams and nothing for empty coordinates. It is enforced
  where the count is charged, so it holds in chart jobs too, and it resets with
  each top-level read. The 8 MiB input cap stays for cells read one at a time;
  cells read through a stream are not held and not charged to it.
- **Streamed ranges.** A range is read in row, then column order from one
  ordered SQL scan, in bands of 1,024 rows (`formula.SCAN_ROWS`) with the time
  budget checked before each band, and folded rather than turned into a list.
  `SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, `COUNTA`, `AND`, `OR`, `CONCAT` and
  `TEXTJOIN` stream, and so do `COUNTIF`, `SUMIF`, `COUNTIFS`, `SUMIFS`,
  `XLOOKUP`, `MATCH` and `INDEX`, with criteria and lookups that account for
  empty coordinates between stored cells. The first error in row, then column
  order still wins, and `#CYCLE!` and `#LIMIT!` from a cell pass through
  functions that tolerate cell errors.
- **Missing formula cells in a range.** A range that holds formulas with no
  value yet has its missing cells evaluated in one pass, the later ones
  speculatively: a speculative result is kept only if no `#CYCLE!` was raised
  beneath it, so a branch that is not taken does not leave a cycle behind.
- **Rectangles in the dependency cache.** `DependencyIndex` records a range as
  one rectangle edge, finds readers of an edited coordinate (including one that
  was empty) by containment, and treats a reader of a volatile or unparseable
  formula inside a range as uncacheable. It keeps its bounds (4,096 results,
  50,000 edges, 4 MiB) and has no spatial index.
- **Corpus.** `tests/calculation_corpus.py` and a recorded fixture hold 178
  cases from the evaluator before stage 2. They pass unchanged: none was
  re-recorded.

The stage ships as one pull request, not the two the plan first proposed: the
state after the iterative-evaluation tasks alone regressed ranges over formulas
that had no value yet, and the design requires every pull request to leave
`main` working.

## What stage 2 does not do

- The 100,000-row running total still returns `#LIMIT!` in a default read. The
  chain at that length needs about 3.5 s here (35 µs per formula on the plain calculator, extrapolated) and the
  budget is 0.25 s, and nothing is kept between reads. The longest chain a read
  completes is in the S2 table (a default read: 8,192; a 2 s read: 32,768). Stage 4
  ends this.
- A formula near a time budget can succeed on one read and fail on the next,
  because the step count that used to trip first is gone and the time budget is
  all that is left. S1, S2 and S4 all sit near their budgets. Stage 4 ends this.
- Whole-column references, array results and 3D ranges stay unsupported.
- Nothing is stored between reads, there is no result table, no stored
  dependency data, no background recalculation and no change to the status the
  clients show. Those are stages 3 and 4.
- The dependency cache is still disposable and in memory, with the bounds it had.

## Known limitations

These are known and not fixed in this stage. The ones a user or a later stage
would notice:

- **A formula can reach `#LIMIT!` by time, not by count.** At the measured
  per-cell costs the count limit is far from binding in any read with a budget
  (see S3). The count is the bound for reads without one.
- **Time is checked between bands of 1,024 rows, not within one.** On a very
  wide, dense sheet a single band is one SQL statement, so a read can overrun its
  budget by that statement's duration. Overruns were measured above: reads
  with a 0.25 s budget gave up up to 0.10 s late and reads with a 2 s budget up to
  0.74 s late (2.74 s, plain calculator, 131,072 formulas).
- **A range reached through a function can be charged twice.** `SUM(IF(TRUE,A1:A60,0))`
  counts its cells twice toward the per-formula limit.
- **Chart jobs charge more than reads.** A chart job charges two to three per
  streamed cell (its own check replaces the calculator's), so about 1.5 million
  streamed cells can reach `#LIMIT!` there. Calculation and chart job progress
  counters can drop or double-count when a formula starts.
- **A range read does not validate stored style JSON.** A bad style on a cell
  read through a range does not raise `INVALID_INPUT`, as a direct read of the
  cell does.
- **Missing formula cells are evaluated speculatively.** Every pending cell of a
  range is evaluated even when an earlier one has turned out to be an error,
  which is extra work and never a different result. A formula whose value is
  blank is not charged on the scan path, as it is on the stream path.
- **Invalidation has no spatial index.** Its cost is affected cells times
  rectangles on the sheet (S5). `cacheable` also rebuilds its list of tainted
  formulas on every save.
- **Untested cases.** There is no test for a deleted cell inside a range, for an
  edit to a range on another sheet, or for a volatile formula written into the
  rectangle of a cached reader. Whether a deleted cell's key is in the engine's
  change set is pre-existing behaviour.
- **`DependencyIndex.hits` counts re-reads** during re-evaluation; it is a
  diagnostic only.
- **Stray `ResourceWarning` lines.** The full suite prints about three
  "unclosed database" warnings, in the corpus test and in one chart test, raised
  by garbage collection; their source was not traced.
- **Stale wording in code.** A comment in `chart_jobs.py` still mentions a
  10,000-step budget, and the tests keep a few cases that patch the limit
  instead of reaching it.

## Not measured

- A cold file cache. Windows and Linux. Any other machine, or this one idle.
- Sheets wider than 10 columns: the 1,024-row band is one statement and its
  cost on a wide sheet was not timed.
- Dates, text and formula results mixed in one range, and ranges across
  several sheets.
- Streaming results that are already calculated, as distinct from evaluating
  formula cells (S3).
- The default read through the desktop: only the service's `Workbook.read` was
  measured.
- `AVERAGE`, `MIN`, `MAX`, `CONCAT`, `TEXTJOIN`, `MATCH` and `INDEX` over large
  ranges. They share the stream with `SUM` and the criteria functions but their
  own cost was not timed.
- Memory for the conditional aggregates and lookups (S6 measured `SUM` only),
  and any read path other than `Workbook.read` and `Workbook.calculator()`.
- The time an edit through `Workbook` spends invalidating (S5).
- The test suites on Windows and Linux. The desktop, Python workspace and chart
  suites and the service suite pass on this Mac (see the
  [status](implementation-status.md)).

## Departures from the plan

- The benchmark's S2 and S1 were extended because the first runs did not
  behave as the plan assumed. S1 records `#LIMIT!` rather than stopping, with a
  2 s figure beside it. S2 does not stop at the first failure: it keeps going to
  the longest chain built (262,144) and measures three ways (a default read, and
  2 s with the cached and the plain calculator), because a longer chain can
  complete where a shorter one did not (above).
- `tests/benchmark.py`, the earlier formula harness, asserted that a chain of
  129 formulas gives `#LIMIT!`. It now expects the value.
- The first S2 run exposed a quadratic walk in the dependency cache; it was fixed in the same stage (`09b21cc`) and S2 re-run, with both runs kept.
- The stage shipped as one pull request, not two.

## Next

Stage 3: the v9 store and freshness. Stage 4 will want the per-cell cost and the invalidation cost in this document before it
chooses its limits.
