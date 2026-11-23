# Calculation Stage 2 Implementation Plan

> **As built, this plan differs in two ways.** Task 4's speculative batch (evaluating a range's missing formula cells ahead of the function that reads it) was replaced, after review found three quadratic shapes of it, by evaluating a missing formula where it is read, with the explicit stack of Task 2 kept only as the fallback when nesting goes beyond 128 cells or Python's stack. And the stage ships as one pull request, not two. The plan below is left as it was approved; the [result](calculation-stage-2.md) describes what was built and measured.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evaluate formulas iteratively and read ranges as streams inside the service, so the 128-cell chain limit and the 10,000-cell range limit go away, with every other contract unchanged.

**Architecture:** `Calculator.cell` becomes a loop over an explicit stack: a formula that reads a formula cell with no value yet is set aside, the missing cell is calculated, and the formula is evaluated again. Ranges come from one ordered SQL scan per range and are folded, not turned into lists. The in-memory dependency cache records a range as one rectangle. Nothing is stored between reads; that is stage 3.

**Tech Stack:** Python standard library, SQLite, `unittest`. No new dependency.

**Spec:** [calculation-design.md](calculation-design.md), sections 7.2, 9 and 12 (stage 2), with the stage 1 [result](calculation-experiment.md). Shape approved by the owner on November 21, 2026.

## Global Constraints

- Standard library only. No native format change: the file stays v8 and no table is added.
- No REST, CLI or SDK shape changes. `desktop/` does not change.
- The per-read time budgets stay: 0.25 s by default, 2 s for lists, find, export and jobs. Exceeding one still gives `#LIMIT!` with the message `Calculation budget exceeded`.
- The 10,000-step count is replaced by a per-formula count: `FORMULA_CELL_LIMIT = 4_194_304` stored cells read by one formula. Empty coordinates are not counted. **Owner**
- The 8 MiB input cap stays for cells read one at a time. Cells read through a stream are not held and are not charged to it.
- The dependency cache keeps its bounds: 4,096 results, 50,000 edges, 4 MiB. A rectangle is one edge.
- A range is read in row, then column order. Sheet names match as SQLite `NOCASE` does.
- Results for every case in the characterization corpus (Task 1) are identical before and after each task.
- Two pull requests: Tasks 1 to 3 (iterative evaluation), then Tasks 4 to 7 (streamed ranges).
- Commits: one imperative sentence, sentence case, no prefix.
- Test command: `PYTHONPATH=service python3 -m unittest discover -s tests` with a fresh `TMPDIR`.

## What stage 2 does not do

The 100,000-row running total still returns `#LIMIT!`: each read must finish inside its time budget and nothing is kept between reads. A formula near a time budget can succeed on one read and fail on the next, because the step count that used to trip first is gone. Both end in stage 4. Whole-column references, array results and 3D ranges stay unsupported.

## Review Focus

1. **A range that contains a cell which reads back through an untaken branch.** `B1` is `=SUM(A1:A2)`, `A1` is `=1/0`, `A2` is `=B1`. Today all three are `#DIV/0!`. Evaluating `A2` early, while `B1` is on the stack, must not leave `#CYCLE!` anywhere. Test in Task 4.
2. **Empty coordinates inside a streamed range.** A criterion that matches blanks (`=COUNTIF(A1:A9,"")`, `"<>x"`), a blank lookup key, `SUMIFS` where the criteria cell is empty and the sum cell is not. The stream yields stored cells only, so each of these must account for the gaps. Tests in Task 5.
3. **Which error a range reports.** The first error in row, then column order; a cell error before the function's own overflow; `#CYCLE!` and `#LIMIT!` from a cell pass through functions that tolerate cell errors. Tests in Tasks 4 and 5.
4. **The cache and ranges.** A volatile or unparseable formula inside a range makes its reader uncacheable; an edit to a coordinate inside a range, including one that was empty, invalidates the cached reader. Tests in Task 6.
5. **Memory.** A formula over 1,000,000 stored cells must not put those cells in `Calculator.cache`. Test in Task 4 asserts the cache's size after the read.

## File Structure

| File | Change |
| --- | --- |
| `service/datahorizon/formula.py` | `Calculator`: explicit stack, per-formula count, `stored`, `aligned`, streamed functions |
| `service/datahorizon/engine.py` | `Workbook.scan`; pass it to calculators |
| `service/datahorizon/dependencies.py` | Rectangle edges, range-aware invalidation and cacheability |
| `tests/calculation_corpus.py` | Builds the corpus workbooks and reads them |
| `tests/fixtures/calculation-corpus.json` | Results recorded from the evaluator before stage 2 |
| `tests/test_calculation_corpus.py` | Asserts the service still gives the recorded results |
| `tests/test_iterative_evaluation.py` | Tasks 2 and 3 |
| `tests/test_streamed_ranges.py` | Tasks 4 and 5 |
| `tests/test_dependencies.py` | Task 6 additions |
| `tests/calculation_stage2_benchmark.py` | Task 7 measurements |
| `docs/design/evidence/calculation-stage-2.json` | Task 7 evidence |
| `docs/design/calculation-stage-2.md` | Task 7 result |

---

### Task 1: Characterization corpus

Records what today's evaluator returns, before anything changes, so that later tasks can be held to it.

**Files:**
- Create: `tests/calculation_corpus.py`, `tests/fixtures/calculation-corpus.json`, `tests/test_calculation_corpus.py`

**Interfaces:**
- Produces: `calculation_corpus.CASES` (a list of `{"name": str, "cells": {(sheet name, row, col): source}}`), `calculation_corpus.results(case) -> {"alone": {...}, "together": {...}}` and `calculation_corpus.record(path)`.
- `results` creates the case's workbook in a temporary directory through `Workbook`, then reads every formula cell twice: `alone` opens a fresh `Workbook` per cell and reads that one cell; `together` reads each sheet's used rectangle in one `read`. Each result is `[calculation, display, value]` with the value as JSON. Reads use `calculation="fresh"`.

- [ ] **Step 1: Write `CASES`.** Every case stays under today's limits (chains under 128, ranges under 10,000 cells) and uses no `NOW` or `TODAY`. Cover, each as its own named case:
  - every range-reading function (`SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, `COUNTA`, `AND`, `OR`, `CONCAT`, `TEXTJOIN`, `COUNTIF`, `SUMIF`, `COUNTIFS`, `SUMIFS`, `XLOOKUP`, `MATCH`, `INDEX`) over a 6 by 3 range that holds numbers, text, a Boolean, a date, an empty coordinate, a formula and a formula that gives `#DIV/0!`;
  - the same functions over a range with no stored cell at all, and over a single cell;
  - criteria that match blanks: `""`, `"<>x"`, `"="`, `"<>"`; a blank lookup key in `XLOOKUP` and `MATCH`; `SUMIFS` with an empty criteria cell beside a stored sum cell;
  - two errors of different codes in one range, in each order, for every function above;
  - `1e308`, `1e308`, `=1/0` in a row under `SUM`, and under `SUMIF`;
  - `1e16`, `-1e16`, `1` down a column under `SUM` and `AVERAGE`;
  - cycles: `A1` `=A1`; `A1` `=B1`, `B1` `=A1`; `=IF(FALSE,A1,42)` in `A1`; `=IF(TRUE,A1,1)` in `A1`; `=IFERROR(B2,5)` with `B2` `=A2`; a range that includes its own formula; Review Focus item 1; a formula that reads a cycle member;
  - a chain of 128 cells, the longest today's limit allows;
  - a range used where a scalar is needed: `=A1:A3` as a whole formula, `=A1:A3+1`, `=IF(A1:A3,1,2)`, `=ISBLANK(A1:A3)`, each with and without an error cell in the range;
  - references across sheets, to a missing sheet, and to sheets named `É` through `'é'` and `'É'`;
  - a 100 by 100 range (the largest today's limit allows) under `SUM` and `COUNTIF`.
- [ ] **Step 2: Write `tests/test_calculation_corpus.py`**: one test that loads the JSON and, for each case under `subTest(case=name)`, asserts `results(case)` equals the recorded value; and one that asserts the JSON's case names equal `[case["name"] for case in CASES]`.
- [ ] **Step 3: Run it.** `PYTHONPATH=service:tests python3 -m unittest test_calculation_corpus`. Expected: ERROR, the fixture file does not exist.
- [ ] **Step 4: Record.** `PYTHONPATH=service:tests python3 -c "import calculation_corpus; calculation_corpus.record('tests/fixtures/calculation-corpus.json')"`, on a tree where `service/` equals `main`. The file records the revision it was made from.
- [ ] **Step 5: Run again.** Expected: OK. Run it three times; it must pass each time. A case that differs between runs is timing-dependent: shrink it until it is not.
- [ ] **Step 6: Commit.** `git commit -m "Record what the evaluator returns before stage 2"`

### Task 2: Iterative evaluation

**Files:**
- Modify: `service/datahorizon/formula.py` (`Calculator.__init__`, `Calculator.cell`)
- Create: `tests/test_iterative_evaluation.py`
- Modify: `tests/test_chart_jobs.py:359` and any other test that provokes an error with a 129-cell chain (find with `grep -rn "128" tests/`)

**Interfaces:**
- Produces: `formula.Pending(Exception)`, raised inside evaluation with `keys`, a list of formula cells that have no value yet; never escapes `Calculator.cell`. `Calculator.following`, the list of cells being evaluated, outermost first; `Calculator.active` stays a set of the same cells. `Calculator.cell(sheet, row, col)` keeps its signature and its return and raise behaviour.

- [ ] **Step 1: Write the failing tests** in `tests/test_iterative_evaluation.py`, each through `Workbook` with a calculator budget of 30 s obtained by `book.calculator(seconds=30, transactional=True)` so that only structure is under test:
  - `test_a_chain_of_129_gives_its_value`: `A1` `1`, `A<r>` `=A<r-1>+1` to row 129; `A129` is 129. Today 128 is the longest chain that works.
  - `test_a_chain_of_20000_gives_its_value_without_recursion`: the same to row 20,000 with `sys.setrecursionlimit(200)` for the duration; `A20000` is 20000. (This also needs Task 3's count; mark it `expectedFailure` until Task 3 removes the mark.)
  - `test_a_formula_is_evaluated_at_most_once_per_missing_precedent`: `=A1+B1+C1` over three formula cells; count calls to `Parser.parse` through a wrapper; the reader is parsed once and evaluated at most four times.
  - `test_cycles_are_decided_by_references_followed`: the cycle cases of Task 1, asserted directly.
  - `test_an_error_in_one_precedent_does_not_evaluate_later_ones`: `=A1+B1` where `A1` is `=1/0` and `B1` is `=C1` with `C1` `=B1`; the result is `#DIV/0!`, and `B1` is not in `calculator.cache`.
- [ ] **Step 2: Run them.** Expected: the 129 chain fails with `Dependency chain exceeds 128 cells`; the 20,000 chain fails; the others pass or fail on `following`.
- [ ] **Step 3: Rewrite `Calculator.cell` as a loop.** Keep the current body as `_evaluate_cell(key)`, which returns a value or raises. In it, a read of a formula cell that is neither in `cache` nor in `active` raises `Pending([key])` where today it recurses. `cell` then:

```text
if the key is in cache: return or raise it          (as today)
if the key is in active: raise #CYCLE!              (as today)
push the key
while the stack is not empty:
    top = stack[-1]
    try:    result = _evaluate_cell(top)
    except Pending as need:  push need.keys[0]; continue      # Task 4 adds the rest
    except FormulaError as error:  result = error
    cache[top] = result; pop
return or raise cache[key]
```

  Parse each formula once per calculator and keep the tree. Reset the per-formula count before each evaluation of a cell. Remove the `len(self.active) >= 128` check. `RecursionError` from a deeply nested expression still gives `#LIMIT!` with `Calculation nesting limit exceeded`.
- [ ] **Step 4: Run the new tests and the corpus.** Expected: all pass except the marked 20,000 chain; `test_calculation_corpus` OK with nothing re-recorded.
- [ ] **Step 5: Fix the tests that used the 128 limit to provoke an error.** Give each another cause of `#LIMIT!` (a calculator budget of 0 s, or a patched `FORMULA_CELL_LIMIT` after Task 3); keep what each test asserts about the consumer.
- [ ] **Step 6: Run the whole suite.** Expected: OK.
- [ ] **Step 7: Commit.** `git commit -m "Evaluate dependency chains iteratively"`

### Task 3: Per-formula count

**Files:**
- Modify: `service/datahorizon/formula.py` (`Calculator.check`, `text_work`, the count's reset)
- Test: `tests/test_iterative_evaluation.py`

**Interfaces:**
- Produces: `formula.FORMULA_CELL_LIMIT = 4_194_304`. `Calculator.check()` still raises `#LIMIT!` `Calculation budget exceeded` past the deadline; it raises `#LIMIT!` `Formula reads more than 4,194,304 cells` when one formula's count passes the limit. `text_work` keeps charging the same count.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_count_is_per_formula`: with `FORMULA_CELL_LIMIT` patched to 3, `A1` `=B1+B2+B3` over inputs is a value and `C1` `=A1+B1+B2+B3` is `#LIMIT!`; a chain of 10 formulas that each read two cells all give values.
  - `test_the_count_limit_is_repeatable`: the same workbook read ten times gives the same result each time.
  - `test_a_read_past_its_deadline_is_a_limit_error`: a calculator with `seconds=0` gives `#LIMIT!` `Calculation budget exceeded`.
  - Remove the `expectedFailure` mark from the 20,000 chain.
- [ ] **Step 2: Run.** Expected: the first fails (today's count is cumulative and 10,000), the chain fails on the count.
- [ ] **Step 3: Implement.** The count restarts for each evaluation of each cell and is saved and restored around nested evaluation, as the stage 1 prototype's `follow` does.
- [ ] **Step 4: Run the new tests, the corpus and the whole suite.** Expected: OK.
- [ ] **Step 5: Commit.** `git commit -m "Count cells per formula instead of steps per read"`. Open pull request 1.

### Task 4: Range scan and streamed aggregates

**Files:**
- Modify: `service/datahorizon/engine.py` (`Workbook.scan`, `Workbook.calculator`), `service/datahorizon/formula.py` (`Calculator.__init__`, `range_geometry`, `stored`, `evaluate` for `SUM`, `argument_values`)
- Create: `tests/test_streamed_ranges.py`

**Interfaces:**
- Consumes: `formula.Pending`, `Calculator.following` (Task 2).
- Produces:
  - `Workbook.scan(sheet_id, r1, c1, r2, c2)`: an iterator of `(row, col, item)` for the stored cells of the rectangle, `ORDER BY row, col`, with `item` as `typed` returns it and the style not parsed. It uses the workbook's own connection, so a hook's calculator sees uncommitted cells.
  - `Calculator(read, resolve_sheet, seconds=0.25, *, clock=None, scan=None)`. With `scan=None` a range falls back to `read` per coordinate.
  - `Calculator.stored(scope, *, errors=False)`: an iterator of `(row, col, value)` over the stored cells of `scope` (a `range_geometry` tuple), in order. A formula cell's value comes from `cache`. With `errors=True` a cell error in `CELL_ERRORS` is yielded as its `FormulaError`; otherwise it is raised. Each cell yielded counts one toward the formula's limit. Nothing it reads from the scan is added to `cache`.
  - Missing formula cells: `stored` goes on to the end of the range collecting them and then raises `Pending(keys)` in range order. `Calculator.cell` evaluates `keys[0]` as before and the rest speculatively: a speculative result is kept only if no `#CYCLE!` was raised anywhere beneath it.

- [ ] **Step 1: Write the failing tests:**
  - `test_sum_over_more_than_10000_cells`: 20,000 numbers; `=SUM(A1:A20000)` equals their sum.
  - `test_a_range_of_formulas_is_evaluated_in_one_pass`: 5,000 formulas `=B<r>*2` in column A and a `SUM` over them; count the calls to `scan`; at most three for the summing formula.
  - `test_review_focus_1`: the three cells give `#DIV/0!` when `B1` is read first, when `A2` is read first, and when all are read together.
  - `test_a_real_cycle_through_a_range`: `B1` `=SUM(A1:A2)`, `A1` `1`, `A2` `=B1`: both `#CYCLE!`.
  - `test_first_error_in_row_then_column_order` and `test_cell_error_before_overflow`, for `SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, `COUNTA`, `AND`, `OR`, `CONCAT`.
  - `test_order_of_addition`: `1e16`, `-1e16`, `1` with `PRAGMA reverse_unordered_selects=ON`; `SUM` is 1.
  - `test_streamed_cells_are_not_cached`: after `=SUM(A1:A20000)`, `len(calculator.cache)` is under 10.
  - `test_a_hook_calculator_sees_uncommitted_cells_in_a_range`: inside a transaction, through `book.calculator(transactional=True)`.
  - `test_the_limit_counts_stored_cells`: with the limit patched to 100, `=SUM(A1:A1000)` over 50 stored cells is a value and over 101 is `#LIMIT!`.
- [ ] **Step 2: Run.** Expected: the large ranges fail with `Range exceeds 10,000 cells`; the rest fail on missing names or pass.
- [ ] **Step 3: Implement** `Workbook.scan`, `Calculator.stored` and the speculative rule; remove the 10,000 bound from `range_geometry`; make `SUM`, `argument_values` and through it `AVERAGE`, `MIN`, `MAX`, `COUNT`, `COUNTA`, `AND`, `OR`, `CONCAT` and `TEXTJOIN` consume `stored`. `SUM` keeps reading its whole range before reporting its own overflow. An empty coordinate was `None` to these functions and each of them ignores `None`; check each against the corpus, not by reading.
- [ ] **Step 3a: A range where a scalar is needed.** `evaluate` of a range node used to return a list of every value, and every consumer of that list only tests for a list and raises `#VALUE!` (confirm with `grep -n "isinstance(.*list)" service/datahorizon/formula.py`). Keep that: read the range through `stored` so that the first cell error is raised as today, then return an empty `RangeValue(list)` marker. No list of the range's values is built.
- [ ] **Step 4: Run the new tests, the corpus and the whole suite.** Expected: OK, no corpus case re-recorded.
- [ ] **Step 5: Commit.** `git commit -m "Read ranges as ordered streams for aggregates"`

### Task 5: Streamed criteria and lookups

**Files:**
- Modify: `service/datahorizon/formula.py` (`aligned`, `conditional_aggregate`, `multiple_criteria`, `xlookup`, `match_exact`)
- Test: `tests/test_streamed_ranges.py`

**Interfaces:**
- Consumes: `Calculator.stored` (Task 4).
- Produces: `Calculator.aligned(scopes)`: for same-shaped scopes, an iterator of `(index, values)` in order, where `index` is the row-major position inside the shape and `values` holds one entry per scope, `None` where that scope has no stored cell. It yields only positions where at least one scope has a stored cell, so the caller knows the number of wholly empty positions as the area minus the positions yielded. Cell errors arrive as `FormulaError` values.

- [ ] **Step 1: Write the failing tests**, each asserting the value and that `read` was not called per coordinate (count calls):
  - `COUNTIF`, `SUMIF`, `COUNTIFS`, `SUMIFS` over 20,000 rows, with one criterion and with three.
  - Blank-matching criteria over a 20,000-row range holding 10 stored cells: `=COUNTIF(A1:A20000,"")` is 19,990 and is computed from the area, and `"<>x"` likewise.
  - `SUMIFS` with an empty criteria cell beside a stored sum cell, and the reverse.
  - `XLOOKUP` and `MATCH` over 20,000 rows: a key in the last row; a missing key; a blank key that matches the first gap, including a gap before the first stored cell and a range with no stored cell.
  - A cell error in a criteria range does not match a criterion and does not raise; a cell error in the sum range under a matching row raises; `#CYCLE!` in either raises.
- [ ] **Step 2: Run.** Expected: values are right where the old path still runs, and the call counts fail.
- [ ] **Step 3: Implement `aligned` as a merge of the scopes' `stored` iterators, and move the four functions onto it.** A lookup stops at its first match and so does its scan.
- [ ] **Step 4: Run the new tests, the corpus and the whole suite.** Expected: OK, no corpus case re-recorded.
- [ ] **Step 5: Commit.** `git commit -m "Stream conditional aggregates and lookups"`

### Task 6: Rectangles in the dependency cache

**Files:**
- Modify: `service/datahorizon/dependencies.py`
- Test: `tests/test_dependencies.py`

**Interfaces:**
- Produces: `DependencyIndex.edges[key]` is a set of rectangles `(sheet, r1, c1, r2, c2)`; a single cell is a rectangle of one. `invalidate(keys)` returns the cells forgotten, as today, and reaches a formula through any rectangle that contains a changed or forgotten cell. `cacheable(key)` is false if a volatile or unparseable formula lies inside any rectangle reached from `key`. `info()` keeps its keys; `edges` counts rectangles.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_large_range_does_not_clear_the_cache`: after reading `=SUM(A1:A60000)` twice, `info()["cached"]` is at least 1 and `info()["hits"]` rose on the second read.
  - `test_an_edit_inside_a_range_invalidates_its_reader`, for a stored cell, for a coordinate that was empty, and for a formula two steps upstream of a cell in the range.
  - `test_a_volatile_formula_inside_a_range_is_not_cached`, and the same for an unparseable one.
  - `test_invalidation_with_4096_cached_range_readers_stays_bounded`: 4,096 cached formulas that each read a different range; one edit; assert the number of rectangle containment checks through a counter, not a time.
- [ ] **Step 2: Run.** Expected: the first fails because 60,000 edges clear the index.
- [ ] **Step 3: Implement.** `IndexedCalculator.input` records a range as one rectangle and no longer calls `range_targets`. Keep single cells in the reverse dictionary; keep ranges in a list per sheet.
- [ ] **Step 4: Run the new tests, the corpus and the whole suite.** Expected: OK.
- [ ] **Step 5: Commit.** `git commit -m "Record a range as one edge in the dependency cache"`

### Task 7: Measure and document

**Files:**
- Create: `tests/calculation_stage2_benchmark.py`, `docs/design/evidence/calculation-stage-2.json`, `docs/design/calculation-stage-2.md`
- Modify: `docs/design/calculation-design.md` (7.2 and 13: what stage 2 fixed), `docs/design/implementation-status.md`, `docs/design/README.md`, and each document that states the 128-cell, 10,000-cell or 10,000-step limit as current (`grep -rln "128 cells\|128-cell\|10,000 cells\|10,000-cell\|10,000 steps" docs`); historical plans and evidence are left as they are.

- [ ] **Step 1: Write the benchmark** over `calculation_fixture.create_fixture`, through `Workbook` only, medians of five, `resource` optional as in stage 1. It records:
  - S1: `=SUM(Sheet1!A1:A100000)` through `book.read` with the default budget. Target ≤ 0.25 s, so that it shows in a grid read.
  - S2: the longest chained running total that one default read completes, and one 2 s read, found by doubling.
  - S3: microseconds per stored cell for a streamed column of numbers, of 40-character text, of dates and of formula results. The design's 4,194,304 limit assumed 2 µs.
  - S4: `COUNTIF`, `SUMIFS` with three criteria and `XLOOKUP` to the last row over 100,000 rows.
  - S5: `invalidate` for one edit with 4,096 cached range readers.
  - S6: peak resident memory for a `SUM` over 1,000,000 stored cells.
- [ ] **Step 2: Run it** and write the evidence file. A target that is missed is recorded as missed with its figure; it does not block the stage, and the result document says what it means for stages 3 and 4.
- [ ] **Step 3: Write the result document**: what was built, the measurements, what stage 2 does not do (from this plan), and whether 4,194,304 still stands given S3.
- [ ] **Step 4: Update the documents listed above.**
- [ ] **Step 5: Run the whole service suite with a fresh `TMPDIR`, and the desktop, Python workspace and chart suites through `ctest --test-dir build`.** Expected: all pass.
- [ ] **Step 6: Commit.** `git commit -m "Measure and document iterative evaluation and streamed ranges"`. Open pull request 2.

## Done when

- A chain of 20,000 formulas and a `SUM` over 20,000 cells return values wherever their time budget allows.
- No message `Dependency chain exceeds 128 cells` or `Range exceeds 10,000 cells` remains in `service/`.
- The characterization corpus passes with nothing re-recorded.
- The service, desktop, Python workspace and chart suites pass.
- S1 to S6 are recorded, and the design note says what they fix.
