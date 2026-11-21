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
was not idle:** the load average was 4 to 6 during the run, mostly from other
processes (a media-analysis daemon was holding about two cores). Stage 1 also ran on a machine that was not idle, so compare
the two sets of figures with care. Evidence: [calculation-stage-2.json](evidence/calculation-stage-2.json),
produced by `tests/calculation_stage2_benchmark.py` on revision `e28415d`, which
holds the full set of per-run timings. Everything is measured through
`Workbook`; no service code is called from the benchmark in any other way.

| # | Measure | Target | Measured | Result |
| --- | --- | --- | --- | --- |
| S1 | `=SUM(Sheet1!A1:A100000)` in one default read (0.25 s) | ≤ 0.25 s | `#LIMIT!` in all five runs (0.2509 s to give up); the same sum takes 0.41 s with a 2 s budget (4.1 µs per cell) | **Missed** |
| S2 | Longest chained running total one read completes | none | Default read: 512 in the doubling; 1,024 and 2,048 fail, 4,096 completes, 8,192 is at the edge (below). List budget (2 s), cached calculator: 1,024, then 2,048 fails, 4,096 to 32,768 complete. List budget, plain calculator: 32,768, fails at 65,536 | See below |
| S3 | Cost per stored cell of a streamed range | 2 µs assumed by the design | 4.2 µs numbers, 2.9 µs 40-character text, 4.1 µs dates, 32.7 µs formula results | Slower than assumed |
| S4 | Conditional aggregates and a lookup over 100,000 rows | none | `COUNTIF` 0.64 s; `SUMIFS` with three criteria 2.18 s; `XLOOKUP` to the last row 0.94 s | Each exceeds a default read |
| S5 | `invalidate` for one edit with 4,096 cached range readers | none | 2.7 ms when the edit falls inside 11 readers; 0.48 s when it falls inside 2,098 (8.6 million containment checks) | See below |
| S6 | Peak resident memory of a `SUM` over 1,000,000 stored cells | none | The sum added at most 32 KiB to the process peak (median 16 KiB); the process peaked at about 46 to 48 MiB; the calculator cache held one entry. 4.06 s | Memory is bounded |

### S1: why it is missed

The streamed `SUM` costs 4.1 µs per cell here. Stage 1's prototype measured
2 µs, and the service adds the calculator's per-cell accounting, the type
conversion of each stored cell and the deadline check. Reading 100,000 cells
therefore takes 0.41 s, above the 0.25 s default budget. The read gives up at
0.2509 s with `#LIMIT!` and the message `Calculation budget exceeded`. Before
stage 2 the same read failed too, for a different reason (a range over 10,000
cells), so no previous behaviour regressed; the range is simply not yet fast
enough to show in a grid read. By the same rate a `SUM` over about 60,000 stored cells fits the
default budget on this machine under this load (an estimate, not a run). A 2 s read (lists, find,
export, jobs) completes it.

### S2: how long a chain one read completes

The chain is `=J<r-1>+A<r>` in column J, read at its last cell with nothing
cached.

| Chain length | Default read (0.25 s) | 2 s, cached calculator | 2 s, plain calculator |
| --- | --- | --- | --- |
| 256 | 0.04 s, done | 0.04 s, done | 0.01 s, done |
| 512 | 0.14 s, done | 0.15 s, done | 0.02 s, done |
| 1,024 | `#LIMIT!` | 0.55 s, done | 0.04 s, done |
| 2,048 | `#LIMIT!` | `#LIMIT!` | 0.09 s, done |
| 4,096 | 0.13 s, done | 0.18 s, done | 0.16 s, done |
| 8,192 | `#LIMIT!` (0.2505 s) | 0.39 s, done | 0.32 s, done |
| 16,384 | `#LIMIT!` | 0.66 s, done | 0.91 s, done |
| 32,768 | `#LIMIT!` | 1.34 s, done | 1.15 s, done |
| 65,536 and above | `#LIMIT!` | `#LIMIT!` | `#LIMIT!` |

Each length is one read in the final run (the evidence file holds them all). At
8,192 the default read sits on its budget: it completed in 0.2474 s in an
earlier run and gave up at 0.2505 s in the final one.

The doubling does not behave as a simple threshold, and the table says why.
The plain calculator (hooks, calculation jobs) is linear: about 35 µs per
formula, so a 2 s budget completes 32,768 and not 65,536. The calculator that
ordinary reads use keeps results in the dependency cache, and for each result it
saves, `DependencyIndex.cacheable` walks every precedent of the formula. For a
chain that walk is as long as the chain, so the cost is quadratic in chain
length for as long as the cache accepts results. The cache gives up and turns
itself off at 4,096 results, after which the cost is linear again. That is why
1,024 and 2,048 fail a default read while 4,096 completes. A default read
therefore completes a chain of 512 and one of 4,096 (about 30 µs per formula) on
this machine, and sits on its budget at 8,192; a chain of 20,000 needs a 2 s
budget. The time at the longest length before the first failure is a median of
five reads: 0.14 s (512, default), 0.55 s (1,024, cached 2 s) and 1.20 s (32,768,
plain 2 s).
This quadratic walk is a finding of this measurement, not of the plan or the
review; it is not fixed here, because this task changes no service code.
Stage 4 replaces the in-memory cache.

Reads that fail also overrun their budget: a 0.25 s read gave up after 0.25 to
0.38 s, and a 2 s read after up to 2.5 s, because the deadline is checked
between units of work and a unit can be long.

### S3: cost per stored cell

`SUM` over one column of 100,000 cells, plain calculator, 60 s budget.

| Column | Median | Per cell |
| --- | --- | --- |
| Numbers (`row % 97`) | 0.42 s | 4.2 µs |
| Text, 40 characters | 0.29 s | 2.9 µs |
| Dates | 0.41 s | 4.1 µs |
| Formula results (`=A<r>`) | 3.27 s | 32.7 µs |

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
limit. At the measured rates the same formula takes about 12 s over text, 17 s
over numbers, 17 s over dates and over 2 minutes over a column of formulas that
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
| `COUNTIF(A1:A100000,">50")` | 0.64 s | 6.4 µs |
| `SUMIFS(E,A,">10",F,"<500",B,"<>x")` (three criteria, four columns) | 2.18 s | 21.8 µs |
| `XLOOKUP(100000,E1:E100000,A1:A100000)` (match on the last row) | 0.94 s | 9.4 µs |

None fits a 0.25 s read at 100,000 rows. Each does fit a 2 s read except the
three-criteria `SUMIFS`, which needs a little over 2 s here. `SUMIFS` at
5 µs per cell per column is in line with a plain `SUM` at 4 µs, so there is no
per-criterion surprise; the cost is the number of cells read.

### S5: invalidation with 4,096 cached readers

With 4,096 formulas cached, each reading a range:

| Shape | Edit | Readers invalidated | Containment checks | Median |
| --- | --- | --- | --- | --- |
| `=SUM(A<r>:A<r+9>)`, ten cells each | one cell of column A | 11 | 45,056 | 2.7 ms |
| `=SUM(Z1:Z<r>)`, over an empty column | `Z2000`, an empty coordinate | 2,098 | 8,591,310 | 0.48 s |

The cache holds a range as one rectangle, so the number of edges is the number
of readers, not the number of cells they cover. An invalidation checks each
affected cell against every rectangle on the sheet and has no spatial index,
which is why the second shape costs 8.6 million checks: 2,098 affected cells
times about 4,096 rectangles. Both cases are inside the cache's bounds (4,096
results, 50,000 edges, 4 MiB), so the cost is bounded by them. A managed edit
pays this inside its own commit, so a sheet with thousands of range readers
cached makes an edit that touches them take about half a second. Stage 4's
dependents index should be an interval structure; see stage 1.

### S6: memory

A `SUM` over 1,000,000 stored cells, in a fresh process, did not hold the
cells: the peak resident memory rose by at most 32 KiB, the process peaked at
about 46 to 48 MiB, and the calculator's cache held one entry (the formula
itself). It took 4.06 s (4.1 µs per cell). A read of the same cells as a list
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
  completes is in the S2 table (a default read: 512, and 4,096 where the cache
  has turned itself off; a 2 s read: 32,768 on the plain calculator). Stage 4
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

These were found in review and deferred. The ones a user or a later stage
would notice:

- **A formula can reach `#LIMIT!` by time, not by count.** At the measured
  per-cell costs the count limit is far from binding in any read with a budget
  (see S3). The count is the bound for reads without one.
- **Time is checked between bands of 1,024 rows, not within one.** On a very
  wide, dense sheet a single band is one SQL statement, so a read can overrun its
  budget by that statement's duration. Budget overruns of 0.1 to 0.5 s were
  measured above.
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
  formulas on every save, and walks a formula's precedents for each result it
  saves, which makes ordinary reads quadratic in chain length until the cache
  turns itself off at 4,096 results (S2).
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
- Memory for the conditional aggregates and lookups (S6 measured `SUM` only).
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
- The stage shipped as one pull request, not two.

## Next

Stage 3: the v9 store and freshness. Stage 4 will want the per-cell cost, the
quadratic cache walk and the invalidation cost in this document before it
chooses its limits.
