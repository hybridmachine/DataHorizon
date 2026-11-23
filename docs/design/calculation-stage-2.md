# Calculation stage 2: result

November 22, 2026 | Branch `claude/calculation-stage-2` | [Plan](calculation-stage-2-plan.md) | [Design](calculation-design.md) | [Stage 1 result](calculation-experiment.md)

Stage 2 changes how the service evaluates formulas, behind today's REST, CLI,
SDK and file contracts. A formula cell is evaluated where it is read, by
ordinary recursion as before, and the cells being evaluated move onto an
explicit stack only when the nesting gets too deep. A range is read as an
ordered stream instead of a list of cells, and the in-memory dependency cache
records a range as one rectangle. The file stays format v8. Nothing is stored
between reads; that is stage 3.

## Result

The 128-cell chain limit and the 10,000-cell range limit are gone. A chain of
20,000 formulas and a `SUM` over 20,000 cells return values wherever their time
budget allows, and the characterization corpus (178 cases, recorded from the
evaluator before stage 2) passes with no case re-recorded. The one target the
plan set, S1, was **missed**: a `SUM` over 100,000 stored cells still shows
`#LIMIT!` in a default 0.25 s read, in every run. The other five measures had
no target; their figures are below, with what they mean for stages 3 and 4.

Figures are medians of five runs in one process with a warm file cache. Host:
Apple M1 (8 cores), macOS 26.6.2, Python 3.14.6, SQLite 3.53.4. The load was
not controlled. The evidence records the load averages (1, 5 and 15 minutes) at
the end of each run: 5.14, 6.29 and 6.51 for the first run, 11.92, 12.25 and
9.35 for the second, and 2.69, 3.16 and 2.99 for the third, which is the final
run. Stage 1 also ran on a machine that was not idle, so compare the two sets of
figures with care.

Three runs are kept, all produced by `tests/calculation_stage2_benchmark.py`:

| Run | Revision | Evidence | What had changed |
| --- | --- | --- | --- |
| 1 | `e28415d` | [calculation-stage-2-run1.json](evidence/calculation-stage-2-run1.json) | The first complete implementation. Its measurement exposed a quadratic walk in the cache (see S2). |
| 2 | `09b21cc` | [calculation-stage-2-run2.json](evidence/calculation-stage-2-run2.json) | After that walk was fixed. The evaluator still evaluated a range's missing formulas ahead of the read. |
| 3 | `03c180d` | [calculation-stage-2.json](evidence/calculation-stage-2.json) | The final design: evaluation in place, a budget on invalidation, and cacheability decided from direct reads. |

What the benchmark uses: the fixtures are written with SQL through the
workbook's connection. S1 and the default-read column of S2 call
`Workbook.read`. The 2 s columns of S2, S3, S4, S5 and S6 call
`Workbook.calculator()` (the cached calculator, or the plain one with
`transactional=True`) directly, with the budget given, not the default read
path. S5 calls `book.dependencies.invalidate` directly, and every run clears or
reads `book.dependencies` (its results, ranges and `checks` counter). The probe
formulas of S3 and S4 are written with `book.db.execute`. S6 runs in a fresh
process and also uses a plain calculator. For run 3 the script gained one field,
`cached_after` for S5 (how many results were still cached after the
invalidation); no name or counter it reads had moved.

| # | Measure | Target | Measured (run 3) | Result |
| --- | --- | --- | --- | --- |
| S1 | `=SUM(Sheet1!A1:A100000)` in one default read (0.25 s) | ≤ 0.25 s | `#LIMIT!` in all five runs (0.2510 s to give up); the same sum takes 0.38 s with a 2 s budget (3.8 µs per cell) | **Missed** |
| S2 | Longest chained running total one read completes | none | Default read: 8,192 (0.23 s), 16,384 fails. 2 s budget, cached or plain calculator: 32,768, 65,536 fails | See below |
| S3 | Cost per stored cell of a streamed range | 2 µs assumed by the design | 3.6 µs numbers, 2.6 µs 40-character text, 4.7 µs dates, 17.4 µs formula results | Slower than assumed |
| S4 | Conditional aggregates and a lookup over 100,000 rows | none | `COUNTIF` 0.54 s; `SUMIFS` with three criteria 1.77 s; `XLOOKUP` to the last row 0.79 s | Each exceeds a default read |
| S5 | `invalidate` for one edit with 4,096 cached range readers | none | 2.2 ms when the edit falls inside 11 readers; 3.1 ms when it falls inside 2,098 | See below |
| S6 | Peak resident memory of a `SUM` over 1,000,000 stored cells | none | The sum added at most 32 KiB to the process peak; the process peaked at 44.0 to 46.4 MiB; the calculator cache held one entry. 3.39 s | Memory is bounded |

Across the three runs, the figures moved as follows (medians):

| | Run 1 | Run 2 | Run 3 |
| --- | --- | --- | --- |
| S1, 2 s budget | 0.41 s | 0.39 s | 0.38 s |
| S2, default read, longest completed | 512, then 4,096 | 8,192 | 8,192 |
| S3, numbers / text / dates / formula results (µs per cell) | 4.2 / 2.9 / 4.1 / 32.7 | 3.7 / 2.7 / 3.9 / 31.7 | 3.6 / 2.6 / 4.7 / 17.4 |
| S4, `COUNTIF` / `SUMIFS` / `XLOOKUP` | 0.64 / 2.18 / 0.94 s | 0.58 / 1.93 / 0.89 s | 0.54 / 1.77 / 0.79 s |
| S5, small edit / edit inside 2,098 readers | 2.7 ms / 0.48 s | 2.7 ms / 0.47 s | 2.2 ms / 3.1 ms |
| S6, seconds | 4.06 | 4.15 | 3.39 |

Most timings drifted down by 5 to 15% from run to run, run 3 having the lowest
load. Two changes are larger. The formula-results column of S3 fell from
31.7 to 17.4 µs per cell; that column is evaluated, not only read, and run 3 is
the first with evaluation in place, which does not push, set aside and
re-enter each formula, but the benchmark does not isolate that cause. The dates
column rose from 3.9 to 4.7 µs (about 20%) for no reason that was found; the
code on its path did not change between runs 2 and 3, so treat it as noise and
read the figures to one significant digit. The S5 change is understood (below).

### S1: why it is missed

The streamed `SUM` costs 3.8 µs per cell here (4.1 µs in the first run). Stage
1's prototype measured 2 µs, and the service adds the calculator's per-cell
accounting, the type conversion of each stored cell and the deadline check.
Reading 100,000 cells therefore takes about 0.4 s, above the 0.25 s default
budget, and the read gives up at 0.251 s with `#LIMIT!` and the message
`Calculation budget exceeded`. Before stage 2 the same read failed too, for a
different reason (a range over 10,000 cells), so no previous behaviour
regressed; the range is simply not yet fast enough to show in a grid read. By
the same rate a `SUM` over about 65,000 stored cells fits the default budget on
this machine (an estimate, not a run). A 2 s read (lists, find, export, jobs)
completes the 100,000-cell sum.

### S2: how long a chain one read completes

The chain is `=J<r-1>+A<r>` in column J, read at its last cell with nothing
cached. Each length is one read; the evidence files hold them all.

| Chain length | Default read (0.25 s) | 2 s, cached calculator | 2 s, plain calculator |
| --- | --- | --- | --- |
| 256 | 0.01 s, done | 0.01 s, done | 0.01 s, done |
| 512 | 0.02 s, done | 0.02 s, done | 0.02 s, done |
| 1,024 | 0.03 s, done | 0.04 s, done | 0.03 s, done |
| 2,048 | 0.06 s, done | 0.08 s, done | 0.06 s, done |
| 4,096 | 0.12 s, done | 0.16 s, done | 0.12 s, done |
| 8,192 | 0.23 s, done | 0.31 s, done | 0.26 s, done |
| 16,384 | `#LIMIT!` | 0.62 s, done | 0.51 s, done |
| 32,768 | `#LIMIT!` | 1.24 s, done | 1.03 s, done |
| 65,536 and above | `#LIMIT!` | `#LIMIT!` | `#LIMIT!` |

A chain costs about 30 µs per formula and is linear in its length on all three
paths: a 2 s budget completes 32,768 formulas and not 65,536, and a default read
completes 8,192 and sits close to its budget there (0.23 s). A chain of 20,000
needs a 2 s budget. The time at the longest length before the first failure is
a median of five reads: 0.23 s (8,192, default), 1.24 s (32,768, cached 2 s)
and 1.04 s (32,768, plain 2 s). Failed reads overrun their budget: a 0.25 s read
gave up after up to 0.28 s, and a 2 s read after up to 2.69 s, because the
deadline is checked between units of work and a unit can be long.

**A defect this measurement found, fixed in this stage.** Run 1 was not linear
for ordinary reads. The calculator that reads use keeps results in the
dependency cache, and for each result it saved, the cache walked every
precedent of the formula. For a chain that walk is as long as the chain, so the
cost was quadratic in chain length for as long as the cache accepted results,
until it turned itself off at its cap of 4,096 results. A default read completed
512 formulas (0.14 s) but failed at 1,024 and 2,048, completed 4,096 (0.13 s),
and sat on its budget at 8,192; a 2 s cached read took 0.55 s for 1,024 and
failed at 2,048 while completing 4,096 to 32,768. The plain calculator, which
has no cache, was linear throughout. The fix (commit `09b21cc`) made the cache
check only a formula's direct reads; run 2 and run 3 show the linear figures.
At 4,096 results the cache still turns itself off, as designed.

### S3: cost per stored cell

`SUM` over one column of 100,000 cells, plain calculator, 60 s budget.

| Column | Median | Per cell |
| --- | --- | --- |
| Numbers (`row % 97`) | 0.36 s | 3.6 µs |
| Text, 40 characters | 0.26 s | 2.6 µs |
| Dates | 0.47 s | 4.7 µs |
| Formula results (`=A<r>`) | 1.74 s | 17.4 µs |

The formula column's figure includes evaluating each formula (each one reads
one cell and is parsed and evaluated); it is the cost of an uncalculated column,
not of reading results that already exist. Nothing here separates the two, so
the cost of streaming already calculated results is not measured.

### Does 4,194,304 still stand?

The count stands as a count: it bounds how many stored cells one formula reads,
it is enforced where the count is charged, and the same inputs always give the
same result. What does not stand is the time the design attached to it. The
design note assumed 2 µs per cell, so about 8 s for a formula that reads the
limit. At the measured rates the same formula takes about 11 s over text, 15 s
over numbers, 20 s over dates and about 73 s over a column of formulas that
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
| `COUNTIF(A1:A100000,">50")` | 0.54 s | 5.4 µs |
| `SUMIFS(E,A,">10",F,"<500",B,"<>x")` (three criteria, four columns) | 1.77 s | 17.7 µs |
| `XLOOKUP(100000,E1:E100000,A1:A100000)` (match on the last row) | 0.79 s | 7.9 µs |

None fits a 0.25 s read at 100,000 rows. Each fits a 2 s read. `SUMIFS` at
about 4 µs per cell per column is in line with a plain `SUM`, so there is no
per-criterion surprise; the cost is the number of cells read.

### S5: invalidation with 4,096 cached readers

With 4,096 formulas cached, each reading a range, `invalidate` called directly:

| Shape | Edit | Readers invalidated | Results still cached | Containment checks | Median |
| --- | --- | --- | --- | --- | --- |
| `=SUM(A<r>:A<r+9>)`, ten cells each | one cell of column A | 11 | 4,086 | 45,056 | 2.2 ms |
| `=SUM(Z1:Z<r>)`, over an empty column | `Z2000`, an empty coordinate | 2,098 | 1,999 | 8,190 | 3.1 ms |

The cache holds a range as one rectangle, so the number of edges is the number
of readers, not the number of cells they cover. It has no spatial index. In runs
1 and 2 an invalidation checked each affected cell against every rectangle on
the sheet, which cost 8,591,310 checks and 0.47 s for the second shape. Run 3
spends at most 100,000 containment checks exactly and, past that, replaces the
cells found in a step by their bounding box (see Known limitations). That is why
the second shape now costs 8,190 checks: the 2,097 readers it finds are checked
as one box against 4,096 rectangles, and none of those rectangles meets the box,
so the result is the same as the exact one. The benchmark calls `invalidate`
directly; the cost of an edit made through `Workbook`, which invalidates inside
its commit, was not measured by it. A reviewer's probe on the code before the
budget existed (one edit of 10,000 unrelated cells with 4,000 cached range
readers) took 1.95 s and 40,000,000 checks, and 0.19 s and 4,000 checks with the
budget, on a machine with a one-minute load between 3 and 11; that is a single
probe run, not benchmark evidence. Stage 4's dependents index should be an
interval structure; see stage 1.

### S6: memory

A `SUM` over 1,000,000 stored cells, in a fresh process, did not hold the
cells: the peak resident memory rose by at most 32 KiB, the process peaked at
44.0 to 46.4 MiB (44.2 to 46.4 MiB in the first two runs), and the calculator's
cache held one entry (the formula itself). It took 3.39 s (3.4 µs per cell). A
read of the same cells as a list would hold about a million Python objects. Peak
memory is measured with the operating system's resident-set counter and is not
available on Windows.

## What was built

- **Evaluation in place, with an explicit stack for depth.** A formula cell with
  no value yet is evaluated at the moment a formula reads it, by ordinary
  recursion, as the evaluator before stage 2 did. Each formula is evaluated
  once per calculator, parsed once, and its input read and charged once.
  When nesting passes `IN_PLACE_DEPTH`, 128 formula cells (the outermost
  included; input cells do not count), or when Python's own recursion limit is
  reached first, the evaluations in progress unwind and the cells they were
  evaluating are moved onto an explicit stack, still marked as being evaluated,
  and evaluated again from there. Python's limit comes first for range
  functions: a probe on Python 3.14.6 (limit 1,000, a bare script with one
  frame counter attached) found the deepest chain evaluated wholly in place was
  128 through a plain reference or `SUM`, 124 through `SUMIFS`, `COUNTIF` or
  `XLOOKUP`, 111 through `AVERAGE` or `MATCH` and 91 through `TEXTJOIN`. So a
  chain has no fixed depth; within the depth every formula is evaluated exactly
  once, as many times as before stage 2, and beyond it about twice, at most
  three times per cell in aggregate over any read. A formula whose own
  expression is nested too deeply is still `#LIMIT!` with the message
  `Calculation nesting limit exceeded`.
- **Cycles and untaken branches.** A cycle is a read of a cell that is being
  evaluated, in the chain of reads actually made, as before: `=IF(FALSE,A1,42)`
  in `A1` is 42, and the cells being evaluated on the explicit stack count as
  being evaluated. A formula cell in a range is evaluated when, and only if, the
  function reading the range reaches it, in the evaluator's own order: nothing
  after a range's first error, after a lookup's match, or under a non-matching
  criteria row is evaluated.
- **A per-formula cell count.** The 10,000-step count per read is replaced by
  `FORMULA_CELL_LIMIT = 4_194_304`: the cells one formula reads. A range read
  as a stream counts its stored cells and nothing for its empty coordinates; a
  single reference counts one whether the coordinate is stored or empty. It is
  enforced where the count is charged, so it holds in chart jobs too, and it
  restarts for each formula evaluated. The 8 MiB input cap stays for cells read
  one at a time; cells read through a stream are not held and not charged to it.
- **Streamed ranges.** A range is read in row, then column order from one
  ordered SQL scan, in bands of 1,024 rows (`formula.SCAN_ROWS`) with the time
  budget checked before each band, and folded rather than turned into a list.
  `SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, `COUNTA`, `AND`, `OR`, `CONCAT` and
  `TEXTJOIN` stream, and so do `COUNTIF`, `SUMIF`, `COUNTIFS`, `SUMIFS`,
  `XLOOKUP`, `MATCH` and `INDEX`, with criteria and lookups that account for
  empty coordinates between stored cells. The first error in row, then column
  order still wins, and `#CYCLE!` and `#LIMIT!` from a cell pass through
  functions that tolerate cell errors.
- **Rectangles in the dependency cache.** `DependencyIndex` records a range as
  one rectangle edge and finds readers of an edited coordinate (including one
  that was empty) by containment. It keeps its bounds (4,096 results, 50,000
  edges, 4 MiB) and has no spatial index. An invalidation spends at most
  100,000 containment checks exactly and is conservative past that. Whether a
  result may be kept is decided from the formula's direct reads only, and
  whether a range holds a cell that must not be kept is found along rows or
  columns, not by walking every such cell.
- **Corpus.** `tests/calculation_corpus.py` and a recorded fixture hold 178
  cases from the evaluator before stage 2. They pass unchanged: none was
  re-recorded.

The stage ships as one pull request, not the two the plan first proposed: the
state after the iterative-evaluation tasks alone regressed ranges over formulas
that had no value yet, and the design requires every pull request to leave
`main` working.

### Checked against the evaluator before stage 2

The final evaluator was compared with `main`'s on generated workbooks, by a
reviewer, in several ways. Against `main` itself: 764,520 comparisons (12 read
modes, 300 deep workbooks) with 0 differences. Against `main` with its two
limits removed: 721,756 comparisons, 0 differences. Across the depth boundary,
12,174,720 results on 1,760 hand-built shapes, 0 differences. For the cache,
7,478 runs with a recursion error forced at random places and 72,602 checks
after edits found 0 stale values. On 23,723 single reads that `main` completes,
the implementer counted formula evaluations on both sides: the final code
evaluated the same number as `main` on every one (143,467 evaluations on each
side), never more. These are probe measurements by the reviewer and the
implementer, not benchmark evidence.

## How it got here

The first design evaluated the missing formula cells of a range ahead of the
function that reads it, in one batch, the later ones speculatively, so that the
range could be folded in one pass. A speculative result was kept only if no
`#CYCLE!` was raised beneath it, and a cell that met one was set aside and
tried again later. That machinery could not keep its work linear. Review of the
whole branch at `42f30fb` and its follow-ups found three shapes of one class,
each a sheet on which `main` gives a value and the first design either took
quadratic work or showed `#LIMIT!` in a default read. Each is a running column
`A<r>` over a total `B1` `=SUM(A1:An)`:

1. A circular total: `A<r>` `=A<r-1>+B1`, with `A1` `=1/0`, n = 1,500: 2,108,042
   evaluations at `42f30fb`; 2,999 after the first fix; 2 after the redesign.
   (`main` gives `#DIV/0!`; a real cycle gave `#LIMIT!` from n = 400 where `main`
   gives `#CYCLE!`.)
2. A look-ahead that is never read: `A<r>` `=A<r-1>+MATCH(1,C<r>:D<r>,0)`, `D<r>`
   `=B1`, n = 1,000: 6,998 evaluations at `42f30fb`, 1,003,002 after its first
   fix, 5,001 after the second fix; 2,001 after the redesign.
3. A look-ahead into the far end of the chain: `D<r>` `=A<n>`, n = 1,000:
   1,004,001 evaluations at the second fix, `#LIMIT!` in a default read from
   n = 300 where `main` gives the value; 2,001 after the redesign.

The first two were patched in turn, each patch adding machinery to the loop that
drove the stack, and the third surfaced after the second. On November 21, 2026
the owner decided to stop patching and replace speculation with the evaluation
described above: evaluate a missing formula where it is read, as `main` does,
and use the explicit stack only beyond `main`'s own depth. The review of the
replacement compared it with `main` (figures above) and found no wrong value or
error. It found two costs, both accepted and described under Known limitations,
and neither changes a result or is a regression against `main`.

Two other defects found on the way are fixed in the cache: its per-result walk
(S2), and, in the final review, an invalidation that cost affected cells times
rectangles and a cacheability check that walked every uncacheable cell for each
range on every save. For the second, with 4,000 cached `=SUM(A<r>:A<r+5>)` and
one edit of 10,000 unrelated cells: 1.95 s and 40,000,000 containment checks
before, 0.19 s and 4,000 after. For the third, with 2,000 `=TODAY()` cells read
first and then 1,900 `=SUM(D<r>:F<r>)` beside them in a default read: 881
values and 1,019 `#LIMIT!` before, 1,900 values in 0.11 s after. These are
single probe runs on a machine with a one-minute load between 3 and 11.

## What stage 2 does not do

- The 100,000-row running total still returns `#LIMIT!` in a default read. The
  chain at that length needs about 3 s here (31 µs per formula on the plain
  calculator, extrapolated) and the budget is 0.25 s, and nothing is kept
  between reads. The longest chain a read completes is in the S2 table (a
  default read: 8,192; a 2 s read: 32,768). Stage 4 ends this.
- A formula near a time budget can succeed on one read and fail on the next,
  because the step count that used to trip first is gone and the time budget is
  all that is left. S1, S2 and S4 all sit near their budgets. Stage 4 ends this.
- Whole-column references, array results and 3D ranges stay unsupported.
- Nothing is stored between reads, there is no result table, no stored
  dependency data, no background recalculation and no change to the status the
  clients show. Those are stages 3 and 4.
- The dependency cache is still disposable and in memory, with the bounds it had.

## Known limitations

These are known and not fixed in this stage. The first two are costs the
redesign's review accepted; the rest are older.

- **A formula waiting on the explicit stack rescans its range for each
  too-deep precedent beneath it.** A cell that is moved onto the explicit stack
  is evaluated again from its start each time a cell beneath it is refused for
  depth, so a range it reads is scanned again each time. A column total over many
  deep chains is quadratic in cells scanned. The reviewer's figures, for a total
  over rows each holding a chain 130 cells deep: 500 rows scan 125,750 cells,
  1,000 rows scan 501,500, and 2,000 rows took 25.7 s (5.7 s before the
  redesign). `main` gives `#LIMIT!` for all of these, so nothing that `main`
  calculates is affected. A test pins today's cost (150 rows: the total is
  evaluated 151 times and scans 11,475 stored cells), so that a change in either
  direction is noticed. Stage 4's worker evaluates in dependency order, where a
  too-deep precedent is already calculated, and does not have this cost.
- **Inside `main`'s depth but beyond Python's stack, a formula can be evaluated
  twice.** Range functions nest more frames per cell than a plain reference, so
  Python's recursion limit is reached at 91 to 111 nested cells, short of 128.
  The cells beyond that point are moved to the explicit stack and evaluated
  again. The reviewer measured at most 1.99 times as many evaluations as `main`
  on one read, and 2.5% more in aggregate over 63,710 reads, with the same
  results, far inside the budgets. A test pins the case of a 112-cell chain
  through `AVERAGE`.
- **A formula can reach `#LIMIT!` by time, not by count.** At the measured
  per-cell costs the count limit is far from binding in any read with a budget
  (see S3). The count is the bound for reads without one.
- **Time is checked between bands of 1,024 rows, not within one.** On a very
  wide, dense sheet a single band is one SQL statement, so a read can overrun its
  budget by that statement's duration. Overruns were measured above, in run 3:
  reads with a 0.25 s budget gave up up to 0.03 s late and reads with a 2 s
  budget up to 0.69 s late (2.69 s, plain calculator, 131,072 formulas).
- **A range reached through a function is charged twice.** `SUM(IF(TRUE,A1:A60,0))`
  counts 120 cells toward the per-formula limit where `SUM(A1:A60)` counts 60.
- **A chart job counts more than a read.** A chart job replaces the calculator's
  check with its own counter, which adds one for each call. In a probe over
  10,000 stored cells a `SUM` was counted as 10,050 and a two-range `SUMIFS` as
  30,090, so the per-formula limit is reached after fewer cells there. Calculation
  and chart job progress counters can also drop or double-count when a formula
  starts.
- **A range read does not validate stored style JSON.** A bad style on a cell
  read through a range does not raise `INVALID_INPUT`, as a direct read of the
  cell does. A probe of `SUM` over a cell with an invalid style returned its
  value.
- **A range reader is classified by what was read.** The cache refuses to keep a
  result that depends on a volatile or unparseable formula, but it learns that
  only for a formula cell that was read in this evaluation or classified earlier.
  A formula cell in a range that the function never reached (after a first error,
  or past a lookup's match) is not classified by that read. An edit to it still
  invalidates the reader through its rectangle.
- **Invalidation has no spatial index and is conservative past a budget.** One
  invalidation spends at most 100,000 containment checks (plus one pass over a
  sheet's rectangles). Within that it is exact. Past it, the cells found in a
  step are replaced by their bounding box, so a reader whose range meets the
  box is forgotten even if no edited cell is inside the range: exact for a
  filled block, more than necessary for scattered cells. Formulas chained
  through ranges that spend the budget clear the whole cache. Neither leaves a
  stale value; both can cost a recalculation that was not needed.
- **The check for a range's uncacheable cells is not a spatial index either.**
  Whether a range holds one is decided along rows or columns, whichever has
  fewer lines to try. A sheet with uncacheable cells in thousands of different
  rows and thousands of different columns, read by ranges thousands of cells
  wide and tall, would still cost up to the number of uncacheable cells (at most
  4,096) per range saved.
- **The cache survives a recursion error by a margin.** A recursion error can
  strike inside the cache's own bookkeeping. The result is dropped and the cell
  is evaluated again from a shallower stack; the worst left behind is an edge
  count that is too high, which makes the cache give up earlier and never keep
  something stale. No test forces the error at exactly that line.
- **The bound on evaluations is an argument.** At most three evaluations per cell
  reached, in aggregate, holds for any workbook by the argument recorded with the
  redesign and is asserted by tests under several stack sizes. It is not a proof,
  and it does not bound the rescans described first.
- **`DependencyIndex.hits` counts more than reuse.** It counts every read of a
  cell the calculator already holds a result for, including one calculated
  moments before in the same read, not only results kept from an earlier read; it
  is a diagnostic only. A chain of 399 formulas read once counted 384.
- **Stray `ResourceWarning` lines.** The full suite printed about three
  "unclosed database" warnings, raised by garbage collection. Two came from
  `tests/test_bindings.py`, where two tests assigned a second `Workbook` to
  `self.book` that was never closed; both now close it. The third, printed
  during `test_charts.test_limit_charts`, was not traced.
- **Tests that patch the limit.** The tests keep a few cases that patch the
  per-formula limit instead of reaching it.

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
- The time an edit through `Workbook` spends invalidating, beyond the single
  probe figures under S5.
- Deep chains through `AVERAGE`, `MATCH` and `TEXTJOIN` in the benchmark: S2
  measures a plain reference chain, and the double evaluation near the Python
  limit is known only from the reviewer's and the implementer's probes.
- The test suites on Windows and Linux. See the
  [status](implementation-status.md) for what was run on which revision.

## Departures from the plan

- The built evaluator differs from the plan's Task 4: evaluation in place
  replaced the speculative batch of a range's missing formulas, and the explicit
  stack is only the fallback for depth. The plan was not rewritten; it carries a
  note pointing here.
- The benchmark's S1 and S2 were extended because the first runs did not behave
  as the plan assumed. S1 records `#LIMIT!` rather than stopping, with a 2 s
  figure beside it. S2 does not stop at the first failure: it keeps going to the
  longest chain built (262,144) and measures three ways (a default read, and 2 s
  with the cached and the plain calculator), because in run 1 a longer chain
  completed where a shorter one did not.
- The first S2 run exposed a quadratic walk in the dependency cache; it was fixed
  in the same stage (`09b21cc`) and S2 re-run, with the runs kept.
- `tests/benchmark.py`, the earlier formula harness, asserted that a chain of
  129 formulas gives `#LIMIT!`. It now expects the value.
- The stage shipped as one pull request, not two.

## Next

Stage 3: the v9 store and freshness. Stage 4 will want the per-cell cost, the
cost of evaluating formulas that are not yet calculated, and the invalidation
cost in this document before it chooses its limits.
