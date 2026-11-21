# Calculation Stage 1 (Measurement) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure, on a 1,000,000-cell workbook, the three things the calculation design leaves to measurement (input digests, dependency data and iterative evaluation), and record the numbers and choices that stages 2 to 5 are planned from.

**Architecture:** An isolated experiment in `tests/`, in the manner of the charts stage 1 experiment. One Python module holds three prototypes: block digests of stored inputs, precedent extraction with a dependents lookup, and a dependency-ordered evaluator that reuses the service's `Calculator`. A benchmark script builds the fixture, runs the prototypes, applies the thresholds below and writes evidence. Nothing is stored in workbooks or exposed through REST.

**Tech Stack:** Python 3.11+ standard library (`unittest`, `sqlite3`, `hashlib`, `resource`, `time`, `json`); the existing `datahorizon.formula` and `datahorizon.engine` modules, imported read-only.

**Spec:** [calculation-design.md](calculation-design.md), sections 6, 7.1, 7.2, 9, 12 (stage 1) and 13.

Stages 2 to 5 each get their own plan after this stage's result is written, because the result fixes the hash, the block height, where dependency data lives and whether verification runs during or after opening.

## Global Constraints

- Standard library only. No pip dependency, including test-only ones.
- The experiment lives in `tests/` and `docs/design/`. No file under `service/` or `desktop/` changes. No REST route, no native format change, no new table in any workbook the service opens: the experiment writes its own tables only into fixture copies it creates.
- No virtual tables, triggers or SQLite extensions, and no Python function registered with SQLite, in any candidate that could become the format.
- The digest covers coordinates, `source` and `kind`. It never covers `style`.
- Unit tests run on small inputs and are discovered by the existing `service` CTest (`python3 -m unittest discover -s tests`). They must add under 10 seconds to it. The 1,000,000-cell runs happen only in the benchmark script.
- Every measurement is recorded with host, OS, Python and SQLite versions, the git revision and the fixture shape. Medians of 5 runs unless stated.
- Commit messages follow the repository's style: one imperative sentence, sentence case, no prefix.
- Work on a branch, not `main`.

**Thresholds (Call, open to the owner).** Measured on the reference Mac with the fixture of Task 1:

| # | Measure | Threshold | If it fails |
| --- | --- | --- | --- |
| T1 | Digest of every block, 1,000,000 cells, added to opening | ≤ 2.0 s | Verification runs in the background after opening (design 6.3) |
| T2 | Digest update for a one-cell managed edit | ≤ 1 ms | Use the other digest mode |
| T3 | Loading dependency data for 100,000 formulas from the file | ≤ 1.0 s and ≤ 64 MiB added RSS | Query dependents from SQLite instead of memory |
| T4 | Affected set for an edit of `A1` (100,000 dependents) | ≤ 0.5 s | Same |
| T5 | Recalculating the 100,000-row chained running total | ≤ 10 s | Record; stage 2 plans evaluator optimisation first |
| T6 | `SUM` over a 100,000-cell range, streamed | ≤ 0.25 s | Record the measured per-formula cell limit |
| T7 | File growth from results, precedents and digests | ≤ 25% of the fixture's size | Record; revisit the value encoding |

The opening scan today is a filtered SQL query that returns no rows for a healthy file, so the digest is new per-cell work, not a free rider. T1 is therefore the measurement most likely to fail, and its fallback is already in the design.

## Review Focus

1. Field boundaries in the hash input: `source="ab", kind="c"` and `source="a", kind="bc"` must hash differently, as must cell (1,11) and (11,1).
2. Sources that are long (64 KiB), non-ASCII, empty, or contain NUL: hashed without error, and a one-character change is detected.
3. A formula that names a sheet that does not exist, or uses syntax the parser rejects: extraction reports it (`unsafe` or a `#REF!` precedent) and never raises.
4. A range precedent as large as a whole column (`A1:A1048576`): stored and looked up as one rectangle, never enumerated cell by cell.
5. A cycle, including a self-reference and a cycle reached only through a range: every member and every dependent gets `#CYCLE!`, and the evaluator terminates.

Each has a test in the task that owns the code.

## File Structure

| File | Responsibility |
| --- | --- |
| `tests/calculation_fixture.py` | Builds the measured workbook and computes its expected values independently |
| `tests/calculation_experiment.py` | The three prototypes: digests, dependency data, ordered evaluation |
| `tests/test_calculation_experiment.py` | `unittest` cases on small inputs |
| `tests/calculation_benchmark.py` | Runs the prototypes at scale, applies thresholds, writes evidence |
| `docs/design/evidence/calculation-experiment.json` | The measurements |
| `docs/design/calculation-experiment.md` | The written result and the choices it fixes |

---

### Task 1: The fixture

**Files:**
- Create: `tests/calculation_fixture.py`
- Test: `tests/test_calculation_experiment.py`

**Interfaces:**
- Produces:
  - `create_fixture(path: Path, rows: int = 100_000, prefix_rows: int = 0) -> dict` returning `{"data": sheet_id, "summary": sheet_id}`.
  - `expected_value(rows: int, sheet: str, row: int, col: int) -> int | float | str | None`, computed by arithmetic, never by the engine.
  - Shape, for `rows = 100_000`: sheet `Sheet1`, columns A to I stored inputs (A: `row % 97` as `number`; B to H numbers; I: the text `"item-<row>"`), column J the chained running total (`J1` is `=A1`, `J<r>` is `=J<r-1>+A<r>`). 900,000 inputs and 100,000 formulas. Sheet `Summary`: `A1` `=SUM(Sheet1!A1:A<rows>)`, `A2` `=Sheet1!J<rows>`, `A3` `=A1-A2`. With `prefix_rows > 0`, `Sheet1` column K rows 1 to `prefix_rows` hold `=SUM(A$1:A<r>)`.

- [ ] **Step 1: Write the failing tests**

```python
class FixtureTests(unittest.TestCase):
    def test_shape_and_expected_values(self):
        ids = create_fixture(self.path, rows=200, prefix_rows=50)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM cells WHERE sheet_id=?", (ids["data"],)).fetchone()[0], 200*10+50)
            self.assertEqual(db.execute("SELECT count(*) FROM cells WHERE kind='formula'").fetchone()[0], 200+50+3)
            self.assertEqual(db.execute("SELECT source FROM cells WHERE sheet_id=? AND row=2 AND col=10", (ids["data"],)).fetchone()[0], "=J1+A2")
        self.assertEqual(expected_value(200, "Sheet1", 200, 10), sum(r % 97 for r in range(1, 201)))
        self.assertEqual(expected_value(200, "Summary", 3, 1), 0)

    def test_fixture_opens_and_engine_agrees_on_a_short_chain(self):
        create_fixture(self.path, rows=100)
        with closing(Workbook(self.path)) as book:
            cell = book.read("Sheet1", dict(r1=100, r2=100, c1=10, c2=10))["cells"][0]
        self.assertEqual(cell["value"], expected_value(100, "Sheet1", 100, 10))
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=service:tests python3 -m unittest test_calculation_experiment.FixtureTests -v`
Expected: FAIL with `ModuleNotFoundError: calculation_fixture`

- [ ] **Step 3: Implement `create_fixture` and `expected_value`**

Create the workbook through `Workbook(path, create=True)` and `sheet_add`, then insert cells with `executemany` in one transaction, as `create_fixture` in `tests/qualify_large_structure.py` does.

- [ ] **Step 4: Run to verify they pass**

Run: the Step 2 command. Expected: 2 tests, OK.

- [ ] **Step 5: Commit**

```bash
git add tests/calculation_fixture.py tests/test_calculation_experiment.py
git commit -m "Add the calculation experiment's workbook fixture"
```

### Task 2: Block digests

**Files:**
- Create: `tests/calculation_experiment.py`
- Test: `tests/test_calculation_experiment.py`

**Interfaces:**
- Consumes: `create_fixture` (Task 1).
- Produces:
  - `HASHES = ("blake2b16", "sha256")`, `MODES = ("sum", "stream")`.
  - `cell_hash(hash_name: str, row: int, col: int, source: str, kind: str) -> int`. Fields are length-prefixed before hashing.
  - `scan_digests(db: sqlite3.Connection, band: int, hash_name: str, mode: str) -> dict[tuple[str, int], bytes]`, keyed by `(sheet_id, (row - 1) // band)`. `sum` adds the per-cell hashes modulo 2 to the hash's bit width. `stream` feeds one hasher per block in primary-key order.
  - `updated_digest(digest: bytes, hash_name: str, old: tuple | None, new: tuple | None) -> bytes` for `sum` mode, where a tuple is `(row, col, source, kind)`.
  - `sheets_digest(db: sqlite3.Connection, hash_name: str) -> bytes` over every sheet's `id` and `name`, independent of row order.

- [ ] **Step 1: Write the failing tests**

```python
class DigestTests(unittest.TestCase):
    # setUp: create_fixture(self.path, rows=300); self.db = sqlite3.connect(self.path)
    def digests(self, mode="sum", hash_name="blake2b16"): return scan_digests(self.db, 64, hash_name, mode)

    def test_field_boundaries_are_unambiguous(self):
        for name in HASHES:
            self.assertNotEqual(cell_hash(name, 1, 1, "ab", "c"), cell_hash(name, 1, 1, "a", "bc"))
            self.assertNotEqual(cell_hash(name, 1, 11, "x", "text"), cell_hash(name, 11, 1, "x", "text"))

    def test_each_kind_of_change_alters_only_its_block(self):
        for mode in MODES:
            before = self.digests(mode)
            for sql in ("UPDATE cells SET source='5' WHERE row=70 AND col=1",
                        "UPDATE cells SET kind='text' WHERE row=70 AND col=2",
                        "DELETE FROM cells WHERE row=70 AND col=3",
                        "INSERT INTO cells(sheet_id,row,col,source,kind) SELECT id,70,40,'new','text' FROM sheets WHERE name='Sheet1'"):
                self.db.execute(sql)
                after = self.digests(mode)
                self.assertEqual([k for k in before.keys() | after.keys() if before.get(k) != after.get(k)], [(self.data, 1)])
                before = after

    def test_style_is_not_covered(self):
        before = self.digests()
        self.db.execute("""UPDATE cells SET style='{"bold":true}' WHERE row=5""")
        self.assertEqual(self.digests(), before)

    def test_sum_update_equals_rescan(self):
        before = self.digests()
        old = self.db.execute("SELECT row,col,source,kind FROM cells WHERE row=70 AND col=1").fetchone()
        self.db.execute("UPDATE cells SET source='99' WHERE row=70 AND col=1")
        self.assertEqual(updated_digest(before[(self.data, 1)], "blake2b16", old, (70, 1, "99", old[3])), self.digests()[(self.data, 1)])

    def test_awkward_sources(self):
        for source in ("", "é" * 10, "a\x00b", "x" * 65536):
            self.db.execute("UPDATE cells SET source=? WHERE row=1 AND col=9", (source,))
            first = self.digests()
            self.db.execute("UPDATE cells SET source=? WHERE row=1 AND col=9", (source + "!",))
            self.assertNotEqual(self.digests()[(self.data, 0)], first[(self.data, 0)])

    def test_empty_workbook_and_sheet_rename(self):
        # a workbook with no cells yields {} ; renaming a sheet changes sheets_digest and no block digest
        ...
```

Write the last test in full: create a second empty workbook and assert `scan_digests(...) == {}`; then `UPDATE sheets SET name='Renamed' WHERE name='Summary'` on the fixture and assert `sheets_digest` changed and `scan_digests` did not.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=service:tests python3 -m unittest test_calculation_experiment.DigestTests -v`
Expected: FAIL with `ModuleNotFoundError: calculation_experiment`

- [ ] **Step 3: Implement the four functions**

One `SELECT sheet_id,row,col,source,kind FROM cells ORDER BY sheet_id,row,col` per scan; no per-block query.

- [ ] **Step 4: Run to verify they pass**

Run: the Step 2 command. Expected: 6 tests, OK.

- [ ] **Step 5: Commit**

```bash
git add tests/calculation_experiment.py tests/test_calculation_experiment.py
git commit -m "Prototype block digests of stored inputs"
```

### Task 3: Dependency data

**Files:**
- Modify: `tests/calculation_experiment.py`
- Test: `tests/test_calculation_experiment.py`

**Interfaces:**
- Consumes: `datahorizon.formula.Parser`, `split_ref`, `coordinates`; the node kinds walked in `service/datahorizon/dependencies.py:49-61` (`ref`, `range`, `call`, `binary`, `unary`).
- Produces:
  - `Rect = tuple[str, int, int, int, int]` (`sheet_id, r1, c1, r2, c2`).
  - `extract_precedents(source: str, sheet_id: str, resolve: Callable[[str], str | None]) -> tuple[list[Rect], bool, bool]` returning `(rectangles, volatile, unsafe)`. `resolve` maps a sheet name to its id or `None`. An unknown sheet yields no rectangle and `unsafe=False` (the formula evaluates to `#REF!` on its own); a parse failure yields `([], False, True)`. Volatile is true for a call to `NOW` or `TODAY`.
  - `class DependencyData` with:
    - `DependencyData.build(db: sqlite3.Connection) -> DependencyData` (parses every formula),
    - `save(db: sqlite3.Connection) -> None` and `DependencyData.load(db) -> DependencyData` using an experiment-only table `x_precedents(sheet_id,row,col,p_sheet,r1,c1,r2,c2)` plus `x_flags(sheet_id,row,col,volatile,unsafe)`,
    - `dependents(cells: Iterable[tuple[str,int,int]]) -> set[tuple[str,int,int]]` (transitive, excluding the inputs unless they depend on each other),
    - `order(cells: set) -> tuple[list[tuple[str,int,int]], set[tuple[str,int,int]]]` returning a dependency order of the formulas in `cells` and the set of those in or downstream of a cycle.
  - `sql_dependents(db, cells) -> set`, the same answer as `dependents` computed by queries against `x_precedents` with no in-memory graph (the T3/T4 fallback candidate).

- [ ] **Step 1: Write the failing tests**

```python
class DependencyDataTests(unittest.TestCase):
    # helper: self.book(cells) creates a workbook from {(sheet,row,col): source} and returns (db, ids)
    def test_extraction(self):
        r = lambda name: {"Sheet1": "S1", "Other": "S2"}.get(name)
        self.assertEqual(extract_precedents("=A1+Other!B2", "S1", r), ([("S1",1,1,1,1), ("S2",2,2,2,2)], False, False))
        self.assertEqual(extract_precedents("=SUM(A1:A1048576)", "S1", r), ([("S1",1,1,1048576,1)], False, False))
        self.assertEqual(extract_precedents("=TODAY()+A1", "S1", r)[1:], (True, False))
        self.assertEqual(extract_precedents("=Missing!A1", "S1", r), ([], False, False))
        self.assertEqual(extract_precedents("=A1 +* 2", "S1", r), ([], False, True))

    def test_dependents_through_cells_ranges_and_sheets(self):
        db, ids = self.book({("Sheet1",1,1):"1", ("Sheet1",1,2):"=A1", ("Sheet1",1,3):"=SUM(A1:B5)",
                             ("Other",1,1):"=Sheet1!C1", ("Sheet1",9,9):"=7"})
        for find in (DependencyData.build(db).dependents, lambda c: sql_dependents(db, c)):
            self.assertEqual(find([(ids["Sheet1"],1,1)]), {(ids["Sheet1"],1,2), (ids["Sheet1"],1,3), (ids["Other"],1,1)})
            self.assertEqual(find([(ids["Sheet1"],5,2)]), {(ids["Sheet1"],1,3), (ids["Other"],1,1)})
            self.assertEqual(find([(ids["Sheet1"],6,1)]), set())

    def test_whole_column_range_is_one_rectangle(self):
        db, ids = self.book({("Sheet1",1,2):"=SUM(A1:A1048576)"})
        data = DependencyData.build(db); data.save(db)
        self.assertEqual(db.execute("SELECT count(*) FROM x_precedents").fetchone()[0], 1)
        self.assertEqual(data.dependents([(ids["Sheet1"],1048576,1)]), {(ids["Sheet1"],1,2)})

    def test_save_and_load_round_trip(self):
        # build on create_fixture(rows=300, prefix_rows=20); save; load; equal dependents for A1, A150 and J300
        ...

    def test_order_and_cycles(self):
        db, ids = self.book({("Sheet1",1,1):"=B1", ("Sheet1",1,2):"=SUM(A1:A2)", ("Sheet1",1,3):"=A1+1",
                             ("Sheet1",2,1):"=A2", ("Sheet1",3,1):"1", ("Sheet1",3,2):"=A3", ("Sheet1",3,3):"=B3"})
        data = DependencyData.build(db); s = ids["Sheet1"]
        ordered, cyclic = data.order({(s,1,1),(s,1,2),(s,1,3),(s,2,1),(s,3,2),(s,3,3)})
        self.assertEqual(cyclic, {(s,1,1),(s,1,2),(s,1,3),(s,2,1)})
        self.assertLess(ordered.index((s,3,2)), ordered.index((s,3,3)))
```

Write `test_save_and_load_round_trip` in full as its comment describes.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=service:tests python3 -m unittest test_calculation_experiment.DependencyDataTests -v`
Expected: FAIL with `ImportError: cannot import name 'extract_precedents'`

- [ ] **Step 3: Implement extraction, `DependencyData` and `sql_dependents`**

`order` must be iterative (no recursion): the fixture's chain is 100,000 deep. How `dependents` finds the formulas whose rectangles contain a cell is the implementer's choice; record the choice in the result document, because stage 3 inherits it.

- [ ] **Step 4: Run to verify they pass**

Run: the Step 2 command. Expected: 5 tests, OK.

- [ ] **Step 5: Commit**

```bash
git add tests/calculation_experiment.py tests/test_calculation_experiment.py
git commit -m "Prototype stored precedents and the dependents lookup"
```

### Task 4: Ordered evaluation and streamed ranges

**Files:**
- Modify: `tests/calculation_experiment.py`
- Test: `tests/test_calculation_experiment.py`

**Interfaces:**
- Consumes: `DependencyData.order` (Task 3); `datahorizon.formula.Calculator`, `FormulaError`; `datahorizon.engine.typed`.
- Produces:
  - `evaluate(db: sqlite3.Connection, data: DependencyData, cells: set, results: dict, *, cell_limit: int = 2_000_000) -> dict[tuple[str,int,int], object]`. Evaluates the formulas in `cells` in dependency order and returns the new results, each a value or a `FormulaError`. `results` holds values already current. A formula that reads more than `cell_limit` cells gets `FormulaError("#LIMIT!")`; there is no time limit.
  - `stream_sum(db, results: dict, rect: Rect) -> float`, a `SUM` computed from one SQL range scan plus `results` for the formula cells inside the rectangle.

Approach, which the signature does not determine: subclass `Calculator` so that `cell()` returns a formula cell's value from `results` and an input cell's value from the database, and so that `check()` counts cells against `cell_limit` and never looks at the clock. Each formula is then evaluated with its precedents already present, so recursion never goes deeper than one formula's own nesting.

- [ ] **Step 1: Write the failing tests**

```python
class EvaluationTests(unittest.TestCase):
    def test_chain_deeper_than_128_and_range_wider_than_10000(self):
        ids = create_fixture(self.path, rows=12_000)
        db = sqlite3.connect(self.path); data = DependencyData.build(db)
        formulas = {k for k in data.formulas()}
        results = evaluate(db, data, formulas, {})
        self.assertEqual(results[(ids["data"], 12_000, 10)], expected_value(12_000, "Sheet1", 12_000, 10))
        self.assertEqual(results[(ids["summary"], 1, 1)], expected_value(12_000, "Summary", 1, 1))
        self.assertEqual(results[(ids["summary"], 3, 1)], 0)

    def test_agrees_with_the_engine_on_every_formula_of_a_small_workbook(self):
        # create_fixture(rows=100, prefix_rows=40) plus cells: =1/0, =A1&"x", =IF(A2>1,"y",NOW()), =Missing!A1, =B200
        # compare evaluate() with Workbook.read(...) cell by cell: same value, or same error code
        ...

    def test_cycles_get_cycle_errors_and_evaluation_terminates(self):
        # the workbook of DependencyDataTests.test_order_and_cycles; plus a self-reference =D4 in D4
        # every member and dependent: FormulaError with code "#CYCLE!"; (3,2) and (3,3) evaluate to 1
        ...

    def test_cell_limit_is_a_deterministic_count(self):
        ids = create_fixture(self.path, rows=500); db = sqlite3.connect(self.path); data = DependencyData.build(db)
        first = evaluate(db, data, {(ids["summary"],1,1)}, {}, cell_limit=499)
        self.assertEqual(first[(ids["summary"],1,1)].code, "#LIMIT!")
        self.assertEqual(evaluate(db, data, {(ids["summary"],1,1)}, {}, cell_limit=500)[(ids["summary"],1,1)], expected_value(500, "Summary", 1, 1))

    def test_edit_recalculates_only_dependents(self):
        # evaluate everything on rows=300; UPDATE A150; affected = data.dependents([A150]);
        # assert affected is J150..J300 plus Summary A1..A3; evaluate(db, data, affected, old_results) merged
        # equals a from-scratch evaluate
        ...

    def test_stream_sum_equals_evaluated_sum_over_inputs_and_formulas(self):
        # rows=2_000: stream_sum over Sheet1!A1:A2000 equals expected; over Sheet1!J1:J2000 (formula cells) equals sum of results
        ...
```

Write the four elided tests in full as their comments describe. `DependencyData.formulas()` returns every formula key; add it to Task 3's class if it is not already there.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=service:tests python3 -m unittest test_calculation_experiment.EvaluationTests -v`
Expected: FAIL with `ImportError: cannot import name 'evaluate'`

- [ ] **Step 3: Implement `evaluate` and `stream_sum`**

- [ ] **Step 4: Run to verify they pass**

Run: the Step 2 command. Expected: 6 tests, OK. Then the whole file: `PYTHONPATH=service:tests python3 -m unittest test_calculation_experiment -v`, expected OK in under 10 seconds.

- [ ] **Step 5: Commit**

```bash
git add tests/calculation_experiment.py tests/test_calculation_experiment.py
git commit -m "Prototype dependency-ordered evaluation and a streamed range sum"
```

### Task 5: Benchmark, evidence and the written result

**Files:**
- Create: `tests/calculation_benchmark.py`
- Create: `docs/design/evidence/calculation-experiment.json`
- Create: `docs/design/calculation-experiment.md`
- Modify: `docs/design/calculation-design.md` (sections 6.3 and 13, status line)
- Modify: `docs/design/implementation-status.md` (new top section)
- Modify: `docs/design/README.md` (document map)

**Interfaces:**
- Consumes: everything from Tasks 1 to 4.
- Produces: `python3 tests/calculation_benchmark.py --output <json> [--rows 100000] [--prefix-rows 2000] [--runs 5]`, exit status 0 when it ran to completion whatever the thresholds say. The JSON has `host`, `revision`, `fixture`, `measurements` (one entry per row below, each with `runs`, `median` and `unit`) and `thresholds` (T1 to T7, each with `limit`, `measured`, `passed`).

Measurements, each on a fresh copy of the fixture:

| Key | What |
| --- | --- |
| `open_today` | `Workbook(path)` to first read of `A1`, as the baseline the digest is added to |
| `digest_scan.<hash>.<mode>.<band>` | `scan_digests` for both hashes, both modes and bands 64, 256, 1024 (T1 uses the best `sum` and the best `stream`) |
| `digest_update` | `sum`: `updated_digest` for one cell; `stream`: re-reading and re-hashing one block, per band (T2) |
| `precedents_build` | `DependencyData.build` (the cost opening would pay with no stored precedents) |
| `precedents_load`, `precedents_rss` | `DependencyData.load` in a fresh process; RSS added (T3) |
| `affected_memory`, `affected_sql` | `dependents` and `sql_dependents` of `Sheet1!A1` (T4) |
| `recalculate_all`, `recalculate_edit_top`, `recalculate_edit_bottom` | `evaluate` for every formula; after editing `A1`; after editing `A<rows>` (T5 uses `recalculate_edit_top`) |
| `range_cell_by_cell`, `range_streamed` | `Summary!A1` through `evaluate` and through `stream_sum` (T6) |
| `prefix_throughput` | Formulas per second and cells read per second for the `prefix_rows` column |
| `results_write` | Writing 100,000 results to an experiment table `x_results` in transactions of 1,000; total time and the longest single transaction |
| `file_growth` | Fixture size before and after `x_precedents`, `x_results` and a digests table (T7) |
| `peak_rss` | Peak RSS of the whole benchmark process |

- [ ] **Step 1: Write `tests/calculation_benchmark.py`**

Verify correctness inside the benchmark before timing anything: after `recalculate_all`, assert `J<rows>` and `Summary!A1:A3` equal `expected_value`, and exit non-zero if not. Use `tests/qualify_large_structure.py` as the model for host and revision capture and for `--output`.

- [ ] **Step 2: Smoke-run it small**

Run: `PYTHONPATH=service:tests python3 tests/calculation_benchmark.py --rows 2000 --prefix-rows 100 --runs 1 --output "$TMPDIR/calc-smoke.json"`
Expected: exit 0; the JSON has all twelve measurement groups and seven thresholds.

- [ ] **Step 3: Run it at scale and keep the evidence**

Run: `PYTHONPATH=service:tests python3 tests/calculation_benchmark.py --output docs/design/evidence/calculation-experiment.json`
Expected: exit 0. Do not rerun to get better numbers; if a run was disturbed, say so in the result document and keep both files.

- [ ] **Step 4: Write `docs/design/calculation-experiment.md`**

It states, with the measured numbers: each threshold and whether it passed; the chosen hash, digest mode and block height; whether verification runs during opening or in the background; whether dependency data is held in memory or queried; the per-formula cell limit proposed for stage 2; the prefix-form throughput and what 100,000 rows of it would take; anything that surprised. Where a threshold failed, it names the fallback from the table above and does not argue for an exception.

- [ ] **Step 5: Update the design note, the status and the document map**

- `calculation-design.md`: fill the numbers section 13 leaves open, record in 6.3 which opening path was taken, and change the status line to say stage 1 is built.
- `implementation-status.md`: a dated section at the top with what was measured, what was chosen and what is not verified (Windows, Linux, cold cache).
- `README.md`: add the design note, this plan and the result to the document map.

- [ ] **Step 6: Run the whole service suite**

Run: `PYTHONPATH=service:tests python3 -m unittest discover -s tests` with a fresh `TMPDIR`.
Expected: OK, with the existing skips and the 19 new tests.

- [ ] **Step 7: Commit**

```bash
git add tests/calculation_benchmark.py docs/design/evidence/calculation-experiment.json docs/design/calculation-experiment.md docs/design/calculation-design.md docs/design/implementation-status.md docs/design/README.md
git commit -m "Record the calculation experiment's measurements and the choices they fix"
```

## After this stage

The owner reviews `calculation-experiment.md`. Stage 2 (iterative evaluation and streamed ranges in the service, behind today's contracts) is planned from it next.
