# Calculation Stage 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep calculated results in the workbook file (native format v9), serve them to later reads and after reopening, and prove by content digests that no served result is stale.

**Architecture:** A new store module owns six derived tables, in the file for a writable v9 workbook and in an attached temporary database otherwise. Ordinary reads and finished calculation jobs write results and precedents through to it; a compact in-memory dependents index, loaded from the stored precedents, finds what an edit affects; commits record roots and re-hash the 64-row blocks they touch; opening and refresh compare every block's digest. The bounded in-memory `DependencyIndex` is removed. There is no background worker: "pending" is "no result row".

**Tech Stack:** Python standard library, SQLite, `unittest`. No new dependency.

**Spec:** [calculation-stage-3-design.md](calculation-stage-3-design.md) (approved by the owner on November 23, 2026), under [calculation-design.md](calculation-design.md). Section numbers below are the stage design's unless prefixed "parent".

## Global Constraints

- Standard library and SQLite only. No virtual tables, triggers or extensions in the file.
- Results are written only through the service's own connection. Writing them changes neither `revision`, the state token nor Undo, makes no receipt and runs no hook.
- A read-only open, and any open of a workbook below v9, never writes the file. A workbook that opens today still opens, inside the five-second budget.
- Every result equals what stage 2 gives for the same inputs. The 178-case characterization corpus passes with nothing re-recorded.
- Stored: values and cell errors (`formula.CELL_ERRORS`). Never stored: `#LIMIT!`, `#CYCLE!`, volatile (`NOW`, `TODAY`) and unparseable formulas, and anything that read one of those.
- The read contract does not change: 0.25 s by default, 2 s for lists, find, export and jobs, `#LIMIT!` with `Calculation budget exceeded`. Workbook info keeps `"calculation": "evaluated_on_read"`. No REST, CLI or SDK shape changes except the capabilities payload's three format numbers. `desktop/` does not change.
- Hooks, macros, and chart, query and operation jobs take nothing from the store and give nothing to it. Calculation jobs give and do not take.
- Edit acknowledgement stays under 100 ms where it is today. A budget met today and missed with this stage stops the work and goes to the owner.
- A block is one sheet and 64 rows: block `b` holds rows `64*b+1` to `64*b+64`. Its digest is SHA-256, unless Task 3 re-decides.
- Three pull requests, on branches `claude/calculation-stage-3-store`, `claude/calculation-stage-3-freshness` and `claude/calculation-stage-3-serving`, each cut from `main` after the previous one merges.
- Commits: one imperative sentence, sentence case, no prefix.
- Test commands: one module, `PYTHONPATH=service:tests python3 -m unittest <module> -v`; the suite, `PYTHONPATH=service python3 -m unittest discover -s tests` with a fresh `TMPDIR`.

**One departure from the design's pull-request split:** the wide-sheet digest measurement moves from pull request 2 to Task 3 in pull request 1, because it decides the digest that Task 4 writes into the format.

## What stage 3 does not do

No worker, no `pending` in place of `#LIMIT!`, no calculation status or wait call, no clock change, no stored results inside transactions or snapshot jobs, no background verification. The 100,000-row running total still shows `#LIMIT!` in a default read until reads with longer budgets have stored enough of it.

## Review Focus

1. **A result's type survives the store.** `=1=1` is `True`, not `1`; `=1/2` is `0.5`; `=""` is an empty string, not blank; a date result keeps its temporal kind; `="1"` stays text. A stored value that comes back as a different type changes comparisons downstream. Test in Task 1.
2. **A formula that stops being storable.** `B1` is `=A1` with a stored result and `C1` is `=B1`; a managed edit makes `B1` `=NOW()`. Neither `B1` nor `C1` may be served from the store afterwards. Test in Task 9.
3. **Undo and Redo.** Undo of an edit marks roots like any commit; Undo of the commit that upgraded the file leaves it v9 with a valid store. Test in Task 9.
4. **A range too large to digest inside the write allowance.** A `SUM` over 1,000,000 stored cells needs 15,625 block digests. The read's answer is unchanged, the result is simply not stored, and nothing stale appears after reopen. Test in Task 8.
5. **A store write that fails.** The file's directory becomes unwritable, or the disk is full, between a read's evaluation and its result write. The read still returns its response and the next read is correct. Test in Task 8.

## File Structure

| File | Responsibility |
| --- | --- |
| `service/datahorizon/calculation_store.py` (new) | The derived tables in either home: schema, sheet numbers, value encoding, results, precedents, digests, roots, metadata |
| `service/datahorizon/calculation_freshness.py` (new) | Block and `sheets` digests, the comparison at open and refresh, the calculation version |
| `service/datahorizon/calculation_index.py` (new) | The in-memory dependents index and its cap |
| `service/datahorizon/calculation_results.py` (new) | `CollectingCalculator` and `StoreCalculator`: precedent extraction, the storable rule, the batch |
| `service/datahorizon/engine.py` | `Workbook`: store lifecycle, upgrade, commit hooks, marking, result writes, verification calls |
| `service/datahorizon/calculation_jobs.py` | Hand a finished job's batch to the store |
| `service/datahorizon/chart_jobs.py`, `query_jobs.py`, `operation_jobs.py`, `recovery.py` | Open their read-only workbooks with `store=False` |
| `service/datahorizon/relational.py` | Accept the derived tables in `validate_schema`; `ensure_version` reaches 9 |
| `service/datahorizon/service.py` | Capabilities: `format_version` 9, 9 in `supported_format_versions`, `new_file_format_version` 9 |
| `service/datahorizon/dependencies.py` | Deleted in Task 8 |
| `tests/test_calculation_store.py`, `test_format_v9.py`, `test_calculation_freshness.py`, `test_calculation_index.py`, `test_stored_results.py`, `test_calculation_marking.py`, `test_calculation_differential.py`, `test_calculation_crash.py` (new) | One per task, as named in the tasks |
| `tests/test_dependencies.py` | Rewritten in Task 8 against the store |
| `tests/calculation_stage3_benchmark.py` (new) | Every measurement of design section 7 |
| `docs/design/native-format-v1.md`, `calculation-design.md`, `compatibility-v0.md`, `implementation-status.md`, `README.md`, `calculation-stage-3.md` (new) | Format v9 section, limits, status, result |

---

## Pull request 1: the store, the format and digests kept by commits

Nothing is served from the store in this pull request.

### Task 1: The derived tables

**Files:**
- Create: `service/datahorizon/calculation_store.py`, `tests/test_calculation_store.py`

**Interfaces:**
- Produces, all in `calculation_store.py`. A key is `(sheet_id, row, col)` with the text sheet identifier the engine uses everywhere; a rectangle is `(sheet_id, r1, c1, r2, c2)`.

```python
STORE_VERSION = 1
BLOCK_ROWS = 64
SCHEMA: str                      # CREATE TABLE statements with a {schema} placeholder
TABLES = ("dh_calc_meta", "dh_calc_sheets", "dh_calc_results",
          "dh_calc_precedents", "dh_calc_digests", "dh_calc_roots")

def blocks(r1: int, r2: int) -> range            # block numbers covering rows r1..r2
class CalculationStore:
    def __init__(self, db: sqlite3.Connection, schema: str)   # "main" for the file, "dhcalc" for the attached temporary database; kept as self.schema
    @staticmethod
    def create(db, schema) -> None                # the six empty tables and the metadata row; caller owns the transaction
    def result(self, key) -> tuple[bool, object]  # (False, None) when there is no row; a stored error comes back as a FormulaError
    def results_in(self, rectangle) -> dict       # key -> value or FormulaError, for the formula cells stored in the rectangle
    def write(self, items) -> None                # items: iterable of (key, result, rectangles); replaces result and precedents of each key
    def delete_results(self, keys) -> None        # results only; precedents stay
    def delete_formula(self, key) -> None         # result and precedents
    def precedents(self) -> Iterator[tuple]       # (key, rectangle), every stored precedent, for the index
    def formula_count(self) -> int                # formulas with a stored result
    def digest(self, sheet_id, block) -> bytes | None
    def digests(self) -> Iterator[tuple]          # (sheet_id, block, digest)
    def set_digests(self, items) -> None          # items: (sheet_id, block, digest or None); None deletes
    def drop_digests(self, sheet_ids) -> None
    def sheets_digest(self) -> bytes | None
    def set_sheets_digest(self, digest: bytes) -> None
    def add_roots(self, rectangles) -> None
    def roots(self) -> list                       # rectangles
    def clear_roots(self) -> None
    def clear_results(self) -> None               # results, precedents and roots; digests stay
    def empty(self) -> None                       # everything except the metadata row
    def calculation_version(self) -> int | None
    def set_calculation_version(self, version: int) -> None
    def generation(self) -> int                   # increases on every write of results
```

- Table shapes are fixed here: `dh_calc_sheets(number INTEGER PRIMARY KEY, sheet_id TEXT NOT NULL UNIQUE)`; results, precedents and digests are `WITHOUT ROWID`, keyed by the sheet number first, with no secondary index (stage 1: integers and no index hold file growth to 11.0%). A formula with no precedents has a result row and no precedent rows. Column names and the value encoding are the implementer's; Step 1 pins what they must preserve.
- No method begins or commits a transaction. The caller owns it.

- [ ] **Step 1: Write the failing tests** in `tests/test_calculation_store.py`, each run against both homes (`main` in a new file, `dhcalc` attached with `ATTACH DATABASE ? AS dhcalc`):

```python
def test_result_types_round_trip_exactly(self):
    values = [True, False, 1, 1.0, 0.5, -0.0, 0.1 + 0.2, 2**53 + 1, "", "1", "TRUE", "a" * 40000, None,
              Temporal("date", 46000), Temporal("datetime", 46000.5),
              FormulaError("#DIV/0!", "Division by zero"), FormulaError("#N/A", "Value is not available")]
    self.store.write(((self.sheet, row, 1), value, ()) for row, value in enumerate(values, 1))
    for row, value in enumerate(values, 1):
        found, back = self.store.result((self.sheet, row, 1))
        self.assertTrue(found)
        self.assertIs(type(back), type(value))
        if isinstance(value, FormulaError): self.assertEqual((back.code, str(back)), (value.code, str(value)))
        else: self.assertEqual(repr(back), repr(value))     # repr tells 1 from 1.0 and 0.0 from -0.0
```

  Build `Temporal` values as `formula.py` builds them; read its constructor first. Further tests, one assertion each: a missing key returns `(False, None)`; `write` replaces a formula's earlier precedents; `delete_results` leaves precedents and `delete_formula` removes both; `results_in` returns only keys inside the rectangle; `set_digests` with `None` deletes; `clear_results` keeps digests and `empty` removes them; `generation` rises after `write`; `blocks(1, 64) == range(0, 1)` and `blocks(64, 65) == range(0, 2)`; a sheet identifier seen twice gets one number.
- [ ] **Step 2: Run** `PYTHONPATH=service:tests python3 -m unittest test_calculation_store -v`. Expected: import error.
- [ ] **Step 3: Implement the module.**
- [ ] **Step 4: Run the module's tests.** Expected: pass.
- [ ] **Step 5: Commit.** `git commit -m "Add the calculation store's derived tables"`

### Task 2: Format v9, upgrade and the temporary store

**Files:**
- Modify: `service/datahorizon/engine.py` (`Workbook.__init__`, `validate`, `close`, `mutate`, `import_workbook`), `service/datahorizon/relational.py` (`ensure_version`, `validate_schema`), `service/datahorizon/service.py:47`, `chart_jobs.py`, `calculation_jobs.py`, `query_jobs.py`, `operation_jobs.py`, `recovery.py`, `docs/design/native-format-v1.md`
- Create: `tests/test_format_v9.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `Workbook(path, ..., store=True)`. With `store=False` the workbook has `self.store = None`, attaches nothing and never verifies. Every read-only `Workbook` a job or recovery inspection opens passes `store=False`.
  - `Workbook.store: CalculationStore | None`. For a writable v9 file it is `CalculationStore(self.db, "main")`. Otherwise the service attaches a database in a new private temporary directory as `dhcalc`, creates the tables there, and removes the directory in `close`.
  - `Workbook.upgrade_calculation()`: called once inside a managed commit's transaction when the file is below 9. It runs `relational.ensure_version(self, 9)`, `CalculationStore.create(self.db, "main")` and repoints `self.store` at `main`; after the commit the temporary database is detached and removed. Nothing is carried across. A transaction that only reserves a failed operation's receipt does not call it, and a rollback restores `self.store`.
  - `Workbook(create=True)` and `import_workbook` produce v9.

- [ ] **Step 1: Write the failing tests** in `tests/test_format_v9.py`:
  - `test_new_workbook_is_v9`: `info()["format_version"] == 9` and all six `TABLES` exist in `main`.
  - `test_first_commit_upgrades_a_v8_workbook`: build a v8 file by creating a workbook and setting `format_version=8` after dropping the six tables through a direct connection; reopen; `store.schema == "dhcalc"`; one edit; then `format_version == 9`, the tables exist and `store.schema == "main"`.
  - `test_open_below_v9_and_read_only_do_not_write`: SHA-256 of the file's bytes is equal before and after `Workbook(path)` plus a read plus `close()` on a v8 file, and the same for `Workbook(path, readonly=True)` on a v9 file.
  - `test_failed_operation_does_not_upgrade`: an edit rejected by validation on a v8 file leaves `format_version == 8`.
  - `test_temporary_store_is_removed_on_close`: the directory holding `dhcalc` does not exist after `close()`.
  - `test_job_snapshot_has_no_store`: `Workbook(path, readonly=True, store=False).store is None`.
  - `test_capabilities`: `format_version == 9`, `9 in supported_format_versions`, `new_file_format_version == 9`.
  - `test_older_levels_still_open`: fixtures at every level 1 to 8 (the existing `test_format_v6`, `v7`, `v8` builders show how) open and read.
- [ ] **Step 2: Run the module.** Expected: fail on `format_version`.
- [ ] **Step 3: Implement.** `validate` accepts 9 and requires the six tables exactly when the level is 9; `relational.validate_schema` must not reject them as unknown tables.
- [ ] **Step 4: Run the whole service suite.** Many of the 83 places that record a format level in `tests/` will fail. Update each to the level the workbook now has; do not touch `tests/fixtures/calculation-corpus.json`.
- [ ] **Step 5: Write the v9 section of `native-format-v1.md`**: every table and column, the value encoding, what a direct reader may assume (parent 5.3: a result can be stale; deleting every derived row is always safe; editing the derived tables is unsupported).
- [ ] **Step 6: Run the whole service suite and `ctest --test-dir build`.** Expected: pass.
- [ ] **Step 7: Commit.** `git commit -m "Upgrade workbooks to native format 9 and give older and read-only workbooks a temporary store"`

### Task 3: Decide the digest on a wide sheet

**Files:**
- Create: `tests/calculation_stage3_benchmark.py`, `docs/design/evidence/calculation-stage-3-digest.json`
- Modify: `docs/design/calculation-design.md` (6.1)

- [ ] **Step 1: Write measurement D1** in the benchmark, reusing the two digest modes prototyped in `tests/calculation_experiment.py`: the time to update the digest after a one-cell edit on sheets 10, 100 and 1,000 columns wide with every cell of the block stored, for stream mode (re-read and re-hash the 64-row block) and sum mode, medians of 101; and the full scan of 1,000,000 cells in each mode, medians of five.
- [ ] **Step 2: Run it** and write the evidence file.
- [ ] **Step 3: Decide and record in parent 6.1.** The rule: keep stream mode at 64 rows if its update on the 100-column sheet is at most 1 ms. Otherwise bound a block by columns as well (64 rows by 16 columns, matching the desktop's tile width) and measure again; choose sum mode only if a bounded block also misses 1 ms. If the block changes shape, change `blocks` in Task 1 to take columns and update its tests in the same commit. Sum mode has one property stream mode lacks, and the decision must say so: a delta update cannot absorb an edit the commit never read.
- [ ] **Step 4: Commit.** `git commit -m "Measure the block digest on wide sheets and fix its form"`

### Task 4: Digests kept current by commits

**Files:**
- Create: `service/datahorizon/calculation_freshness.py`, `tests/test_calculation_freshness.py`
- Modify: `service/datahorizon/engine.py` (`mutate`, the structural and large-structure paths)

**Interfaces:**
- Consumes: Tasks 1 to 3.
- Produces, in `calculation_freshness.py`:

```python
def block_digest(db, sheet_id: str, block: int) -> bytes | None
    # SHA-256 over the block's stored cells in key order: for each, row, col, source and kind, each length-prefixed,
    # source and kind hashed by their bytes with a one-byte type tag so that a BLOB or a number stored by a direct
    # edit hashes without error. Style is excluded. None when the block holds no cell.
def sheets_digest(db) -> bytes                  # over (id, name) of every sheet in id order
def ensure_digests(store, db, sheet_blocks) -> None   # compute and store the digest of each (sheet_id, block) that has none
```

  - In `Workbook.mutate`, immediately after `BEGIN IMMEDIATE`: if `self.data_version() != self.version`, call `self.store.empty()` and set `self._rehash = False` for this commit; the operation proceeds unchanged (design 5.4).
  - Before `COMMIT`, when `self._rehash`: for a structural edit, a change to table records or any commit that today calls `self.dependencies.clear()`, `self.store.clear_results()`, `self.store.drop_digests(touched sheet ids)` and `self.store.set_sheets_digest(sheets_digest(self.db))`; otherwise, for each block holding a changed cell that has a stored digest, `set_digests` with its new `block_digest`. A block with no stored digest is left without one.
  - `Workbook.touched_blocks() -> set[tuple[str, int]]` from `self.changes`.

- [ ] **Step 1: Write the failing tests**:
  - `test_digest_covers_source_kind_and_coordinates_not_style`: changing a cell's `source`, its `kind`, or moving it within the block changes the digest; changing only `style` does not.
  - `test_digest_of_an_empty_block_is_none`.
  - `test_blob_source_hashes`: `UPDATE cells SET source=x'00ff'` through a direct connection; `block_digest` returns 32 bytes and differs from the digest before.
  - `test_commit_rehashes_only_digested_blocks`: `ensure_digests` for block 0; edit `A1` and `A100`; block 0's stored digest equals a fresh `block_digest` and block 1 has none.
  - `test_structural_edit_drops_digests_and_results`: after `ensure_digests` and a `store.write`, a row insert leaves no digest for that sheet, no result, and a current `sheets_digest`.
  - `test_external_edit_before_the_write_lock_empties_the_store`: patch `Workbook.refresh` so that, after it returns inside `mutate`, a direct connection updates `A1`; the commit succeeds, and afterwards `list(store.digests()) == []` and no result remains.
  - `test_rollback_leaves_digests_as_they_were`: an edit rejected by a required hook leaves the stored digest of its block unchanged.
- [ ] **Step 2: Run the module.** Expected: fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole service suite.** Expected: pass.
- [ ] **Step 5: Commit, and open pull request 1.** `git commit -m "Keep block digests current in managed commits"`

---

## Pull request 2: freshness at open and refresh

### Task 5: Verification at open

**Files:**
- Modify: `service/datahorizon/calculation_freshness.py`, `service/datahorizon/engine.py` (`Workbook.__init__`), `tests/test_calculation_freshness.py`, `tests/calculation_stage3_benchmark.py`, `tests/opening_benchmark.py`

**Interfaces:**
- Produces:

```python
def compare(store, db, deadline: float) -> Verdict | None
    # One pass over cells in key order computing every block's digest, compared over the union of stored and found
    # block keys, plus the sheets digest. None when time.monotonic() passes deadline before the pass ends.
@dataclass
class Verdict:
    sheets_changed: bool
    changed: list            # (sheet_id, block) whose digest differs, is missing, or is stored for a block now empty
    found: dict              # (sheet_id, block) -> digest computed by this pass
def apply(store, db, verdict) -> None
    # sheets_changed: clear_results. Otherwise, for each changed block: delete_formula for every formula the store
    # holds in the block, add_roots of the block's rectangle (all columns). Then set_digests from verdict.found,
    # deleting the digest of a block now empty, and set_sheets_digest. Caller owns the transaction.
```

  - `Workbook.__init__`, v9 and `store=True` only: `compare` runs inside the opening read transaction with the opening deadline. After that transaction ends, a writable open applies the verdict to `main` in one `BEGIN IMMEDIATE`. A read-only open first copies the six tables from `main` into `dhcalc` (`INSERT INTO dhcalc.t SELECT * FROM main.t`) and applies it there.
  - `compare` returning `None`: a writable open empties `main` in a transaction of its own; a read-only open leaves `dhcalc` empty. The workbook opens either way.
  - A workbook below v9 is not scanned.
  - `Workbook.verified: bool`, true after a completed comparison in this session; false sets nothing else in this task.

- [ ] **Step 1: Write the failing tests** (each closes the workbook, edits through `sqlite3.connect(path)`, reopens):
  - `test_unchanged_file_keeps_every_result`: write three results and their digests; reopen; all three are found and `store.roots() == []`.
  - `test_changed_cell_roots_its_block`: change `A1` directly; after reopen `store.roots() == [(sheet, 1, 1, 64, 16384)]` and block 0's digest is the new one.
  - `test_cell_added_to_an_empty_block_roots_it`: insert `A200` directly into a block that had no cell and no digest; after reopen block 3 has a root and a digest.
  - `test_blob_source_does_not_stop_the_scan`: store `x'00ff'` as a `source` directly; the comparison completes and roots that block. (Whether the workbook then opens is `validate`'s existing decision and does not change.)
  - `test_deleting_the_only_cell_of_a_block`: `A1` alone in block 0, `B100` `=A1+1` with a stored result; delete `A1` directly; after reopen block 0 has a root and no stored digest.
  - `test_formula_with_no_precedents_given_new_text`: `A1` `=1+1` with stored `2`; set its source to `=3+4` directly; after reopen `store.result(A1) == (False, None)`.
  - `test_renamed_sheet_clears_everything`: rename a sheet directly; after reopen no result and no precedent remains and the sheets digest is current.
  - `test_deleted_derived_rows_are_tolerated`: `DELETE` from every derived table directly; the workbook opens and reads correctly.
  - `test_scan_past_its_deadline_empties_and_still_opens`: patch `compare` to return `None`; the workbook opens, the store is empty, a read returns correct values.
  - `test_read_only_open_applies_to_the_temporary_store`: after a direct edit, a read-only open leaves the file's bytes unchanged and `dhcalc` holds the root.
  - `test_v8_open_does_not_scan`: patch `compare` to raise; opening a v8 file succeeds.
- [ ] **Step 2: Run the module.** Expected: the new tests fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Measure opening (O1, O2).** Add a store-and-digest variant to `tests/opening_benchmark.py`'s 270,000-row long-text shape, and to the benchmark the stage 1 fixture (`calculation_fixture.create_fixture`) with every block digested: validation, the comparison and the total, medians of five. **If either total exceeds five seconds, stop: design 5.5 sends the background fallback back to the owner.**
- [ ] **Step 5: Run the whole service suite.** Expected: pass.
- [ ] **Step 6: Commit.** `git commit -m "Verify block digests when a format 9 workbook opens"`

### Task 6: Verification at refresh, and writes that do not wait

**Files:**
- Modify: `service/datahorizon/engine.py` (`refresh`), `tests/test_calculation_freshness.py`

**Interfaces:**
- Produces:
  - `Workbook.store_write(work: Callable[[], None]) -> bool`: runs `work` inside `BEGIN IMMEDIATE` on the service connection with `PRAGMA busy_timeout=0`, restoring 1000 afterwards; returns `False`, having written nothing, when SQLite reports busy, locked, read-only or full, or when `self.data_version() != self.version` once the lock is held. For the temporary store it runs `work` in a plain transaction. Every store write outside `mutate` in Tasks 5 to 10 goes through it. `self.store_skips` counts the `False` returns.
  - `Workbook.distrusted: bool`. While true the store serves nothing and accepts no result or digest. `refresh` and `mutate` try `self.store.empty()` (through `store_write`, or inside the commit's transaction) and clear the mark when it succeeds.
  - `refresh`, when `data_version` moved: after `validate`, run `compare` with the caller's deadline (two seconds from now when none was passed). A verdict is applied through `store_write`; `None`, or a `False` from `store_write`, sets `distrusted`.

- [ ] **Step 1: Write the failing tests**:
  - `test_direct_edit_while_open_roots_the_block`: with the workbook open, change `A1` directly; `info()`; the block has a root and the new digest; an unrelated block's stored result is still found.
  - `test_refresh_scan_past_its_deadline_empties_the_store`: patch `compare` to return `None`; after `info()` the store is empty, `distrusted` is false, and a later `store.write` is accepted.
  - `test_distrust_holds_while_the_file_is_locked`: hold a `BEGIN` read transaction on a second connection; change the file so verification must write; `distrusted` is true and `store_skips` rose; release the lock; the next `info()` clears it.
  - `test_store_write_does_not_wait`: with the second connection holding a shared lock, `store_write` returns `False` in under 100 ms.
  - `test_commit_while_distrusted_empties_in_its_own_transaction`.
- [ ] **Step 2: Run, implement, run the suite.**
- [ ] **Step 3: Commit.** `git commit -m "Verify digests after an external change and never wait to write the store"`

### Task 7: The calculation version

**Files:**
- Modify: `service/datahorizon/calculation_freshness.py`, `service/datahorizon/engine.py` (`Workbook.__init__`, `upgrade_calculation`), `tests/test_calculation_freshness.py`

**Interfaces:**
- Produces: `CALCULATION_VERSION = 1` in `calculation_freshness.py`, with the rule of parent 6.4 as its comment. A new store records it. At open, a stored version that differs in either direction makes the session call `store.clear_results()` and `set_calculation_version`, in the same transaction as Task 5's repairs for a writable open and in `dhcalc` for a read-only one. Digests stay.

- [ ] **Step 1: Write the failing tests**:
  - `test_older_version_clears_results_and_keeps_digests`: write a `#NAME?` result and a digest, set the stored version to `0` directly; reopen; the result is gone, the digest is there, the stored version is `CALCULATION_VERSION`.
  - `test_newer_version_clears_too`: the same with `CALCULATION_VERSION + 1`.
  - `test_function_set_is_pinned_to_the_version`: `hashlib.sha256("\n".join(sorted(<every function name the evaluator implements>)).encode()).hexdigest()` equals a literal recorded in the test beside the literal `CALCULATION_VERSION` it was recorded at, and the failure message says to raise `CALCULATION_VERSION` and re-record both. Read `functions.py` and `formula.py` to find how the function set is enumerated.
- [ ] **Step 2: Run, implement, run the suite and `ctest --test-dir build`.**
- [ ] **Step 3: Commit, and open pull request 2.** `git commit -m "Invalidate stored results when the calculation version changes"`

---

## Pull request 3: serving

### Task 8: The dependents index, and results written by reads

**Files:**
- Create: `service/datahorizon/calculation_index.py`, `service/datahorizon/calculation_results.py`, `tests/test_calculation_index.py`, `tests/test_stored_results.py`
- Modify: `service/datahorizon/engine.py` (`calculator`, every ordinary read: `read`, `summary`, `read_workbook`, `list_view`, `list_values`, `matches`, `csv_export`, and the callers in `record_operations.py`), `tests/test_dependencies.py`
- Delete: `service/datahorizon/dependencies.py`

**Interfaces:**
- Consumes: Tasks 1, 4 and 6.
- Produces, in `calculation_index.py`:

```python
STORED_FORMULA_LIMIT = 140_000     # provisional: stage 1's compact form held about 140,000 formulas in 64 MiB; Task 11 fixes it
class DependentsIndex:
    def __init__(self, precedents=())               # (key, rectangle) pairs, as CalculationStore.precedents yields them
    def link(self, key, rectangles) -> None         # replaces key's rectangles
    def unlink(self, key) -> None
    def __len__(self) -> int                        # formulas linked
    def affected(self, rectangles) -> set           # every formula with a rectangle meeting one of these, and their readers, transitively
```

  Cells are packed into integers. Ranges are held per sheet in a structure whose lookup for one cell costs time proportional to the ranges that contain it plus a logarithm, not to the ranges on the sheet. `affected` is exact: it has no check budget and never answers "everything". It counts rectangle comparisons in `self.checks`.
- Produces, in `calculation_results.py`:

```python
class CollectingCalculator(Calculator):
    batch: list      # (key, result, rectangles) for each formula completed in this calculator that may be stored, in completion order
    # A formula may be stored when its result is a value or a CELL_ERRORS error, it is neither volatile nor unparseable,
    # and no precedent rectangle of it contains a cell that this calculator evaluated and found not storable.
class StoreCalculator(CollectingCalculator):
    def __init__(self, book, seconds, clock=None)
    # A cell absent from self.cache is looked up in book.store before it is evaluated, unless book.distrusted or the
    # key is in book.unserved. A streamed range fetches the stored formula results of each 1,024-row band with one
    # results_in call. Results taken from the store are not put in batch.
```

  - `Workbook.calculator(transactional=False)` returns a `StoreCalculator` and remembers it; `transactional=True` returns a plain `Calculator` as today.
  - `Workbook.index: DependentsIndex`, built from `store.precedents()` by `Workbook.ready()`.
  - `Workbook.unserved: set`, empty in this task (Task 9 fills it).
  - `Workbook.ready() -> None`: builds the index if the session has none. `calculator`, `flush_results` and (Task 9) `settle` call it first.
  - `Workbook.reset_calculation() -> None`: discards the index and clears `unserved`, so that the next `ready()` rebuilds from the store. Called after every `store.empty()`, every `store.clear_results()` and every applied verdict (Tasks 4 to 7 call sites included).
  - `Workbook.flush_results() -> None`, called by every ordinary read after its read transaction has ended and before the lock is released: through `store_write`, for the batch in order, stops when `STORE_WRITE_SECONDS = 0.1` (provisional; Task 11) has passed or `len(self.index) >= STORED_FORMULA_LIMIT`; for each item it calls `ensure_digests` for the formula's own block and the blocks of its rectangles, `store.write`, `index.link`, and discards the key from `unserved`. An item is written only if every earlier item of the batch it could depend on was written, which stopping at the first unwritten item guarantees. Any exception from the write is swallowed after `store_skips += 1`: the read has already built its response.
  - `calculation=cached` reads mark a formula pending when `store.result(key)` finds nothing or the key is unserved, where they consult `self.dependencies.values` today.
  - Until Task 9, `mutate` keeps correctness crudely: after any commit it calls `store.clear_results()` inside the commit's transaction and rebuilds the index empty. Task 9 replaces this.

- [ ] **Step 1: Write the failing index tests** in `tests/test_calculation_index.py`:
  - `test_single_cells_ranges_and_transitive_readers`: `B1` reads `A1`, `C1` reads `B1`, `D1` reads `A1:A10`, `E1` reads `Z1`; `affected([(s,1,1,1,1)]) == {B1, C1, D1}`; `affected([(s,5,1,5,1)]) == {D1}`.
  - `test_rectangle_roots`: `affected([(s,1,1,64,16384)])` finds a reader of `A64` and not a reader of `A65`.
  - `test_relink_and_unlink`.
  - `test_prefix_sums_over_a_formula_column_stay_cheap`: 2,000 formulas `K<r>` reading `J1:J<r>`, where each `J<r>` reads `J<r-1>`; `index.checks = 0; affected([(s,1,10,1,10)])` returns all 4,000 and `index.checks < 200_000` (stage 1's column-and-bisect lookup needed about 2,000,000 here).
  - `test_other_sheets_are_not_affected`.
- [ ] **Step 2: Run, implement `calculation_index.py`, run.** Commit: `git commit -m "Add an in-memory dependents index with an interval lookup"`
- [ ] **Step 3: Write the failing store-serving tests** in `tests/test_stored_results.py`:

```python
def test_second_read_and_reopen_do_not_evaluate(self):
    self.apply(cells=[dict(row=1, col=1, source="2"), dict(row=1, col=2, source="=A1*3")])
    self.assertEqual(self.value(1, 2), 6)
    with patch.object(Calculator, "_evaluate_cell", side_effect=AssertionError("evaluated")):
        self.assertEqual(self.value(1, 2), 6)
        self.reopen()
        self.assertEqual(self.value(1, 2), 6)
```

  - `test_precedents_and_digests_are_written_with_the_result`: after the read, `store.result(B1)` is found, `index.affected([A1 rectangle]) == {B1}`, and block 0 has a digest equal to `block_digest`.
  - `test_volatile_and_its_readers_are_not_stored`: `A1` `=NOW()`, `B1` `=A1`, `C1` `=7+8`; after reading all three only `C1` has a result.
  - `test_limit_and_cycle_are_not_stored`: `A1` `=A1`; a chain that exceeds a `seconds=0` calculator; neither they nor their readers have results.
  - `test_cell_errors_are_stored`: `=1/0` is found as a `FormulaError` with code `#DIV/0!`.
  - `test_cached_read_serves_stored_and_marks_the_rest_pending`: before any fresh read a `calculation=cached` read shows `…`; after one it shows `6`.
  - `test_batch_is_discarded_when_the_file_changed_under_the_read`: patch `flush_results`' entry so that a direct connection edits `A1` first; no result is written.
  - `test_write_failure_does_not_fail_the_read` (Review Focus 5): patch `Workbook.store_write` to raise `sqlite3.OperationalError("database or disk is full")`; the read returns `6`, `store_skips` rose, and the next read returns `6`.
  - `test_range_too_large_for_the_allowance_is_not_stored` (Review Focus 4): with `STORE_WRITE_SECONDS` patched to `0` and `=SUM(A1:A1000)`, the read returns the right sum, no result is stored, and after reopen the value is still right.
  - `test_cap_stops_storing_without_discarding`: with `STORED_FORMULA_LIMIT` patched to `2`, reading three formulas stores two, and all three read correctly twice.
  - `test_transactional_calculators_do_not_use_the_store`: a hook's `value` read returns the right value with `store.result` patched to raise.
- [ ] **Step 4: Run.** Expected: fail.
- [ ] **Step 5: Implement `calculation_results.py` and the engine changes; delete `dependencies.py`.** Precedent extraction is the tree walk now in `IndexedCalculator.input`; keep its rule that an unparseable formula is unsafe and `NOW`/`TODAY` are volatile.
- [ ] **Step 6: Rewrite `tests/test_dependencies.py`** so that each existing case asserts the same behaviour through the store (`book.store.result`, `book.index`) in place of `book.dependencies`. Cases that pin the 4,096-result and 50,000-edge caps or the 100,000-check invalidation budget are deleted: those bounds no longer exist.
- [ ] **Step 7: Run the whole service suite, the corpus and `ctest --test-dir build`.** Expected: pass, the corpus with nothing re-recorded.
- [ ] **Step 8: Commit.** `git commit -m "Store results calculated by reads and serve them from the store"`

### Task 9: Roots and marking

**Files:**
- Modify: `service/datahorizon/engine.py` (`mutate`, `refresh`, `calculator`, `snapshot`)
- Create: `tests/test_calculation_marking.py`

**Interfaces:**
- Consumes: Tasks 4, 6 and 8.
- Produces:
  - In `mutate`, in the commit's transaction and when `self._rehash`: for a commit that is not structural, `store.add_roots` of one single-cell rectangle per changed cell, and `store.delete_formula` for each changed cell (its own result and precedents). This replaces Task 8's `clear_results` for such commits. Structural commits keep Task 4's clear and record no root.
  - `Workbook.mark() -> None`: `self.unserved |= self.index.affected(self.store.roots())`, and `index.unlink` for each root cell that held a formula. It runs after `COMMIT` and before the commit returns, and inside `ready()` whenever the index has just been built and the store holds roots. That second case covers a file opened with roots left by a crash, and roots added by verification at open or refresh, because an applied verdict calls `reset_calculation()`.
  - `Workbook.settle() -> None`, called at the end of `mark` and at the start of `refresh`. It calls `ready()` first, so roots are never cleared before they have been marked in this session. Then, through `store_write`, `store.delete_results` for up to `DELETE_SLICE = 2_000` keys of `unserved` (provisional; Task 11), removing them from `unserved`; when `unserved` is empty, `store.clear_roots()` in the same transaction.
  - `Workbook.snapshot(path)` copies the file as today; the copy may hold roots, which opening it handles.

- [ ] **Step 1: Write the failing tests**:
  - `test_edit_unserves_dependents_at_once`: `A1` `2`, `B1` `=A1*3`, `C1` `=B1+1`, `D1` `=7+8`, all stored; edit `A1` to `5`; `C1` reads `16`, and `D1` is still served without evaluation (patch `_evaluate_cell` to fail for `D1`'s key only).
  - `test_edited_formula_with_no_precedents`: `A1` `=1+1` stored as `2`; a managed edit to `=3+4`; reads `7`.
  - `test_formula_that_becomes_volatile` (Review Focus 2): `B1` `=A1` stored, `C1` `=B1` stored; edit `B1` to `=NOW()`; neither has a result row after the next read of both.
  - `test_undo_and_redo_mark_roots` (Review Focus 3): edit `A1` from `2` to `5`, read `B1` (`15`), Undo, read `6`, Redo, read `15`.
  - `test_undo_of_the_upgrading_commit_stays_v9`: on a v8 file, one edit then Undo; `format_version == 9` and `B1` reads correctly.
  - `test_slices_and_roots`: with `DELETE_SLICE` patched to `2` and five stored dependents of `A1`, after the edit `len(store.roots()) == 1` and three keys remain unserved; two more `info()` calls empty `unserved` and `store.roots() == []`.
  - `test_reopen_with_roots_finishes_the_deletion`: as above, close after the edit (close must not settle), reopen; no dependent is served stale, and after reads `store.roots() == []`.
  - `test_roots_survive_a_request_that_makes_no_calculator`: reopen a file holding a root and stored dependents and call only `info()`; the dependents are unserved or deleted, never served, and no root was cleared while a dependent's result remained.
  - `test_result_recalculated_before_its_slice_is_kept`: with `DELETE_SLICE` patched to `0`, edit `A1`, read `B1`; `B1`'s new result is found and is not in `unserved`.
  - `test_snapshot_copy_with_roots_opens_fresh`: `snapshot` taken while roots remain; opening the copy serves no stale value.
  - `test_structural_edit_records_no_root`.
  - `test_direct_edit_recalculates_dependents_and_no_others`: with the workbook closed, change `A1` directly; after reopen `B1` and `C1` are re-evaluated and `D1` is served without evaluation. The same with the workbook open.
  - `test_renamed_sheet_then_edit_reaches_the_reader`: `A1` `=Budget!A1` is `#REF!`; rename `Draft` to `Budget` directly; reopen; `A1` reads the value; edit `Budget!A1` through the service; `A1` follows.
- [ ] **Step 2: Run, implement, run the whole service suite and `ctest --test-dir build`.**
- [ ] **Step 3: Commit.** `git commit -m "Record pending roots in commits and stop serving what they affect"`

### Task 10: Calculation jobs fill the store

**Files:**
- Modify: `service/datahorizon/calculation_jobs.py`, `tests/test_calculation_jobs.py`

**Interfaces:**
- Consumes: `CollectingCalculator` (Task 8), `Workbook.flush_results` (Task 8).
- Produces:
  - The job's snapshot calculator is a `CollectingCalculator` built with the arguments the snapshot's plain calculator gets today (`seconds=2`, the job's clock); its `check` is wrapped as now. One calculator serves the whole job.
  - `CalculationJobs.offer(state: str, batch: list) -> bool`: called by the job's thread after the snapshot workbook is closed and before `calculate` returns. Under `self.book.lock` it calls `self.book.refresh()`; if `self.book.state() != state` it returns `False`; otherwise it writes the batch by the rules of `flush_results` and returns whether anything was written. The job's result and the `calculations` resource are unchanged; the batch is not part of the result.
  - `Workbook.flush_results(batch=None)`: with a batch, writes that one in place of the remembered calculators'.

- [ ] **Step 1: Write the failing tests**:
  - `test_finished_job_stores_its_results`: `B1` `=A1*3`; a `calculation=cached` read shows it pending; start a job over the tile and wait for `completed`; a cached read shows `6` and `store.result(B1)` is found; after reopen a cached read still shows `6`.
  - `test_job_batch_is_dropped_after_a_commit`: hold the job (patch `CollectingCalculator._evaluate_cell` to wait on an event), commit an edit to `A1`, release; the job reports `stale` and no result for `B1` computed from the old `A1` is stored.
  - `test_job_batch_is_dropped_after_a_direct_edit`: the same with a direct connection.
  - `test_job_response_is_unchanged`: the completed result's keys equal the keys recorded before this task (assert the exact set).
  - `test_volatile_tile_stores_nothing`.
- [ ] **Step 2: Run, implement, run the whole service suite and `ctest --test-dir build`** (the desktop suite exercises tile jobs).
- [ ] **Step 3: Commit.** `git commit -m "Store the results of finished calculation jobs"`

### Task 11: Differential and crash tests, measurements and documents

**Files:**
- Create: `tests/test_calculation_differential.py`, `tests/test_calculation_crash.py`, `docs/design/evidence/calculation-stage-3.json`, `docs/design/calculation-stage-3.md`
- Modify: `tests/calculation_stage3_benchmark.py`, `calculation_index.py` and `engine.py` (the three provisional constants), `docs/design/calculation-design.md`, `compatibility-v0.md`, `implementation-status.md`, `README.md`, `native-format-v1.md`

- [ ] **Step 1: Write the differential test.** With `random.Random(seed)` for seeds 0 to 19: a workbook of two sheets and about 200 cells drawn from inputs, chains, ranges over inputs and over formulas, cross-sheet references, `IF` with an untaken self-reference, a volatile cell and a `#DIV/0!`; 60 steps drawn from edit, clear, Undo, Redo, row insert, row delete, sheet rename, a direct edit through a second connection, close-and-reopen, a fresh read of a random rectangle and a cached read. After every step, every cell read through the service equals the same cell read from a copy of the file opened with `store=False` and a plain transactional `Calculator`. Run it also with `DELETE_SLICE` patched to `1` and `STORE_WRITE_SECONDS` patched to `0`.
- [ ] **Step 2: Write the crash tests**, in the style of `tests/qualify_large_structure.py`'s `kill` mode: a child process runs the service's `Workbook` and is killed with `SIGKILL` at a named point reached through an environment variable the child checks (during an edit's commit before `COMMIT`; inside a result write; between two deletion slices; during the opening comparison). The parent reopens and asserts the comparison of Step 1 for every cell. Skipped on Windows, where `SIGKILL` is unavailable.
- [ ] **Step 3: Run both.** Expected: pass. A failure here is a freshness defect: fix it in the task that owns the code, with a named regression test there.
- [ ] **Step 4: Complete the benchmark** with the measurements of design section 7 not yet taken, medians of five on the reference Mac, recording load average where the platform has it:
  - E1: edit acknowledgement with 1, 1,000 and 100,000 affected formulas, split into the commit, `mark` and the first slice.
  - R1: first and repeat read of `=SUM` over 100,000 cells, and the read after reopen.
  - W1: time `flush_results` adds to a read that calculates 1, 256 and 4,096 formulas; W2: digest fill for a range of 100,000 and of 1,000,000 cells.
  - I1: index load time and memory for the stage 1 fixture; I2: `affected` for 2,000 prefix sums over a formula column.
  - F1: file growth from the derived tables on the stage 1 fixture with every formula stored.
  - X1: refresh after a one-cell direct edit at 10,000, 100,000 and 1,000,000 cells, and whether the store survived.
  - V1: opening a 100-cell workbook at v1 and at v9.
  - S1: the share of `store_write` calls skipped while calculation jobs run over a 100,000-formula sheet.
- [ ] **Step 5: Fix the provisional constants** from the measurements: `DELETE_SLICE` so that E1's 100,000 case acknowledges within 100 ms; `STORE_WRITE_SECONDS` from W1 and W2; `STORED_FORMULA_LIMIT` so that I1's memory, scaled, stays within the allowance recorded in the result. If E1's 100,000 case cannot meet 100 ms at any slice size because `mark` alone exceeds it, stop and take it to the owner.
- [ ] **Step 6: Write the result document** `calculation-stage-3.md` in the form of stage 2's: what was built, the measurements with targets met and missed, known limitations (the refresh rule at 1,000,000 cells, the cap, what is not stored, the desktop depending on jobs to fill the store), departures from this plan, and what stage 4 needs.
- [ ] **Step 7: Update the documents**: parent design (status line, the **Measured** paragraphs of 5, 6 and 13, limits in 9), `compatibility-v0.md`, `implementation-status.md` (a stage 3 section at the top and the M6 ledger row), `README.md`, and the v9 section of `native-format-v1.md` if the encoding changed since Task 2.
- [ ] **Step 8: Run the whole service suite with a fresh `TMPDIR`, and the desktop, Python workspace and chart suites through `ctest --test-dir build`.** Expected: all pass. Re-measure the opening, viewport and edit budgets with `tests/benchmark.py` and record them.
- [ ] **Step 9: Commit, and open pull request 3.** `git commit -m "Qualify stored results with differential and crash tests and record the measurements"`

## Done when

- Reopening a workbook whose formulas were stored serves them without evaluating any, and a direct edit made while it was closed changes every dependent value and no other (Tasks 8 and 9).
- Kill-and-reopen never shows a stale value as current (Task 11).
- A desktop tile calculated once is shown at once after reopening (Task 10).
- The characterization corpus passes with nothing re-recorded, and the service, desktop, Python workspace and chart suites pass.
- Every measurement of design section 7 is recorded, the three provisional constants are fixed from them, and the parent design, native format document, compatibility document and status say what stage 3 built and what its limits are.
- No budget met on `main` today is missed.
