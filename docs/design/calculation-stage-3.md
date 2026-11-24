# Calculation stage 3: result

November 24, 2026 | Stacked branches `claude/calculation-stage-3-store`, `claude/calculation-stage-3-freshness`, `claude/calculation-stage-3-serving` | [Plan](calculation-stage-3-plan.md) | [Design](calculation-design.md) | [Stage 3 design](calculation-stage-3-design.md) | [Stage 2 result](calculation-stage-2.md)

Stage 3 adds persistent calculated results in native format v9, content-digest
freshness verification at open and refresh, a compact in-memory dependents
index with interval-tree range lookup, pending invalidation roots with sliced
deletion, and store filling from ordinary reads and finished calculation jobs.
REST, CLI, SDK, the read contract (`0.25 s` / `2 s`, `#LIMIT!`,
`"calculation": "evaluated_on_read"`), and `desktop/` are unchanged. Background
recalculation and the new read contract remain stage 4.

## What was built

- **Format v9 calculation store (`service/datahorizon/calculation_store.py`,
  `engine.py`, `relational.py`, `service.py`)**: six reserved derived tables
  (`dh_calc_meta`, `dh_calc_sheets`, `dh_calc_results`, `dh_calc_precedents`,
  `dh_calc_digests`, `dh_calc_roots`) using integer sheet numbers, no secondary
  indexes, no triggers, no virtual tables, and no SQLite extensions. New
  workbooks start at v9; older writable workbooks upgrade transactionally on
  the first managed commit (never downgraded, including by Undo). Read-only
  workbooks and files below v9 keep the same six tables in a private temporary
  SQLite database (`dhcalc`) deleted on close and never write the workbook file.
  `dh_calc_results` preserves exact Python types (`None`, `bool`, `int`
  including outside 64-bit, `float` including `-0.0`, `str`, `Temporal`
  date/time/datetime, and `CELL_ERRORS`).
- **Input content digests and verification
  (`service/datahorizon/calculation_freshness.py`)**: 64-row blocks using sum
  mode (32-byte big-endian sum mod $2^{256}$ of per-cell SHA-256 digests over
  coordinates, type-tagged length-prefixed `source` and `kind`, excluding
  `style`), plus a `sheets` digest over `(id, name)`. Managed commits update
  digests of touched blocks by delta in the commit transaction. Opening a v9
  workbook verifies every block inside the 5-second opening budget; a changed
  block deletes its own formulas and adds a block root, and a changed `sheets`
  digest clears all results and precedents. External edits while open are
  verified on `refresh()` inside the caller's deadline (default 2 s); exceeding
  the deadline empties the store, and a locked file marks the session
  `distrusted` until a non-waiting `store_write` or managed commit can empty it.
- **Calculation version**: `CALCULATION_VERSION = 1` in
  `calculation_freshness.py`, pinned by a SHA-256 test over the 53 implemented
  functions in `formula.SUPPORTED_FUNCTIONS`. Any mismatch on open (older or
  newer) clears results and precedents while keeping input digests.
- **Dependents index and result serving
  (`service/datahorizon/calculation_index.py`, `calculation_results.py`)**:
  `DependentsIndex` packs cell coordinates into Python integers, stores
  single-cell reverse edges compactly with block buckets, and indexes ranges in
  lazy centered interval trees with per-call draining cursors. `StoreCalculator`
  replaces `dependencies.py` (deleted), serving stored results directly and
  prefetching streamed 1,024-row bands in one `results_in` query per band.
  Ordinary reads (`read`, `summary`, `read_workbook`, `list_view`,
  `list_values`, `find`, `csv_export`, and `record_operations.snapshot`) flush
  collected storable results after ending their read transaction via
  non-waiting `store_write` (`PRAGMA busy_timeout=0`), never changing
  `revision`, state token, Undo, or receipts, and never firing hooks.
  Transactional calculators (hooks, `replace_all`, promotion, operation jobs) do
  not use the store.
- **Roots and sliced deletion (`engine.py`)**: non-structural managed commits
  record one single-cell root per changed cell and delete the changed cell's
  own stored formula inside the commit transaction. After `COMMIT`, `mark()`
  adds `index.affected(roots)` to `unserved`, unlinks edited formulas, and
  calls `settle()` to delete up to `DELETE_SLICE` stale results per call
  (clearing `dh_calc_roots` in the same transaction when `unserved` empties).
  `refresh()` settles one slice per request; `close()` does not settle so
  reopening a file with roots handles unfinished deletion and crashes cleanly.
- **Calculation jobs fill the store (`service/datahorizon/calculation_jobs.py`)**:
  each calculation job uses one `CollectingCalculator` over its read-only
  snapshot, closes the snapshot, and calls `CalculationJobs.offer(state, batch)`
  under `book.lock` to write the batch if `book.state()` is unchanged. Job
  responses and the `calculations` resource are unchanged.

## Measurements

All stage 3 measurements are medians of five runs on an Apple M1 (8 cores),
macOS 26.6.2 (`arm64`), Python 3.14.6, SQLite 3.53.4, with warm OS cache and
uncontrolled host load (load averages 2.58–3.17 at start, 3.89–10.71 at end of
the final run). Evidence:
[calculation-stage-3.json](evidence/calculation-stage-3.json) (and
[calculation-stage-3-digest.json](evidence/calculation-stage-3-digest.json)
from Task 3's D1 gate).

| # | Measure | Target | Measured (median of 5) | Result |
| --- | --- | --- | --- | --- |
| D1 | 1-cell digest update on 10 / 100 / 1,000-col sheet (median of 101) | ≤ 1 ms at 100 cols | Stream 64: `0.62 / 6.46 / 64.25 ms`; 64×16 seek: `0.95 / 1.38 / 1.48 ms`; **Sum 64: `0.003 / 0.008 / 0.015 ms`** (1M full scan: `1.75 s`) | **Met** (sum mode chosen) |
| O1 | Open 270,000-row × 1-KiB text (316 MB), all blocks digested | ≤ 5.0 s | Validation `0.097 s`, compare `0.700 s`, **total `0.917 s`** (5/5 verified) | **Met** |
| O2 | Open stage 1 fixture (1M cells, 100,003 formulas, full store, 81.5 MB) | ≤ 5.0 s | Validation `0.212 s`, compare `1.739 s`, **open `2.152 s`** (5/5 verified); lazy index load `0.562 s` (open+index `2.729 s`) | **Met** |
| E1 | Edit acknowledgement with 1 / 1,000 / 100,000 affected formulas | ≤ 100 ms | 1: **`1.19 ms`** (`0.76` commit + `0.01` mark + `0.32` slice); 1,000: **`2.75 ms`** (`0.94 + 0.43 + 1.36`); 100,000 (`DELETE_SLICE=2,000`): **`75.68 ms`** (`1.68 + 60.52 + 13.36`) | **Met** |
| R1 | `=SUM(Sheet1!A1:A100000)` first read, job fill, repeat read, after reopen | none | First 0.25 s read: `#LIMIT!` (`0.255 s`); 2 s job fill: `0.386 s`; **repeat default read: `0.126 ms`**; **default read after reopen: `0.307 ms`** | Stored result served in < 1 ms |
| W1 | `flush_results` added to read of 1 / 256 / 4,096 formulas (digests warm) | none | **`0.41 ms`** / **`1.83 ms`** / **`26.27 ms`** (read totals `0.95 / 11.54 / 175.83 ms`) | Fits 100 ms write allowance |
| W2 | Cold digest fill for 100,000 / 1,000,000 cells | none | **`0.150 s`** (100k, 1,563 blocks) / **`1.500 s`** (1M, 1,563 blocks × 10 cols) | Deferred cleanly past 100 ms |
| I1 | Index load and memory on stage 1 fixture (100,003 formulas) | ≤ 64 MiB allowance | Load `0.562 s` (O2, no `tracemalloc`) / `1.237 s` (with `tracemalloc`); **current `42.90 MiB`**, **peak `44.39 MiB`** (`449.9 B/formula`) | **Met** |
| I2 | `affected` for 2,000 prefix sums over 2,000-row formula column | no quadratic blowup | **`2.76 ms`**, `7,995` checks for `3,999` affected formulas (vs `4.6 s` / ~2M checks in stage 1) | **Met** (~1,700× faster) |
| F1 | File growth from derived tables on stage 1 fixture (100,003 formulas) | ≤ 25% | `73.24 MB` → `81.54 MB` (**`+7.92 MiB` / `+11.34%`**, `83.1 B/formula`) | **Met** |
| X1 | Refresh after 1-cell direct edit at 10k / 100k / 1M cells | none | 10k: `0.021 s` (5/5 survived); 100k: `0.199 s` (5/5 survived); 1M: `2.018 s` (4/5 survived, 1/5 emptied past 2 s deadline) | Matches design 5.5 |
| V1 | Open 100-cell workbook at v1 vs v9 | none | v1: `1.811 ms` (temp `dhcalc` creation); v9: **`1.413 ms`** | v9 faster than v1 |
| S1 | Share of `store_write` skipped while tile jobs run over 100k-formula sheet | none | **`95.1%`** skipped (`1,995 / 2,089` attempts) under continuous concurrent foreground reads against active snapshot jobs; foreground reads never waited on SQLite locks | Non-waiting contract verified |

### Provisional constants fixed from measurements

1. **`DELETE_SLICE = 2_000`** (`service/datahorizon/engine.py`): E1 slice sweep
   at 100,000 affected formulas gave medians of `76.8 ms` (1,000), `75.7–77.8 ms`
   (2,000), and `80.3 ms` (5,000), with `mark()` itself taking `60.5 ms` and
   commit `1.7 ms`. `2_000` keeps edit acknowledgement safely inside 100 ms
   while finishing 1,000-dependent invalidations in one slice (`2.75 ms`) and
   draining 100,000 dependents in 50 slices (~13.4 ms per slice).
2. **`STORE_WRITE_SECONDS = 0.1`** (`service/datahorizon/engine.py`): W1 shows
   even a maximum 4,096-formula viewport flushes in `26.3 ms` when digests are
   warm, well inside 100 ms, while W2 shows a cold 100,000-cell (`150 ms`) or
   1,000,000-cell (`1.50 s`) digest fill is safely cut off instead of stalling
   a read.
3. **`STORED_FORMULA_LIMIT = 140_000`** (`service/datahorizon/calculation_index.py`):
   I1 measured `449.9 B/formula` steady state (`42.90 MiB` current, `44.39 MiB`
   peak after streaming sorted precedents in `DependentsIndex.__init__`). At
   `140_000` formulas the index stays at ~60.1 MiB steady state (~62.1 MiB peak
   load), inside the 64 MiB index memory allowance.

### Existing budgets re-measured (`tests/benchmark.py`)

Measured on the same Apple M1 over authenticated loopback REST (100 samples per
operation, 390,001-cell storage fixture / 335.7 MB database):

- Formula chains (depths 16, 64, 128, 129): median `0.47–0.55 ms`, p95
  `0.73–1.49 ms` (target `≤ 150 ms`).
- Edit + fresh read: median `3.06 ms`, p95 `4.72 ms`.
- First-read viewport: `1.33 ms`; warm viewport: median `0.88 ms`, p95
  `1.25 ms`; distant viewport: median `0.86 ms`, p95 `1.20 ms`; dense 32×16
  viewport: median `6.54 ms`, p95 `7.69 ms`; large-disk (336 MB) viewport:
  median `1.44 ms`, p95 `1.86 ms` (all target `≤ 50 ms`).
- Edit acknowledgement: median `1.20 ms`, p95 `2.53 ms` (target `≤ 100 ms`).
- Service peak RSS: `94.9 MiB` (target `≤ 256 MiB`).

No budget met on `main` regressed.

## Qualification and verification

- **Differential tests (`tests/test_calculation_differential.py`)**: 20 random
  seeds (0–19), 2 sheets (~200 cells combining inputs, chains, ranges over
  inputs and formulas, cross-sheet references, `IF` with untaken self-reference,
  volatile cells, and `#DIV/0!`), 60 random steps per seed (edits, clears, Undo,
  Redo, row inserts/deletes, sheet renames, direct SQLite edits, reopens, fresh
  and cached reads), plus runs with `DELETE_SLICE=1` and `STORE_WRITE_SECONDS=0`.
  After every step, every non-pending cached cell and every fresh cell matched a
  `store=False` plain transactional `Calculator` copy.
- **Crash tests (`tests/test_calculation_crash.py`)**: child `Workbook`
  processes killed with `SIGKILL` during an edit before `COMMIT`, inside
  `CalculationStore.write`, between two deletion slices, and inside
  `apply_verdict` during opening comparison; every reopened workbook matched the
  from-scratch oracle.
- **Characterization corpus**: all 178 cases in
  `tests/fixtures/calculation-corpus.json` pass with nothing re-recorded.
- **Full suites**: service suite (with fresh `TMPDIR`) and all CTest targets
  (`development_bundle_signature`, `chart_svg_probe_smoke`, `desktop`,
  `python_workspace`, `chart_authoring`, `service`) pass.

## Known limitations

- **Refresh after a direct edit at 1,000,000 cells (X1)**: the 1M-cell sum-mode
  comparison takes ~1.74 s, sitting on the 2-second default refresh deadline
  (4/5 survived in `info()`, 1/5 emptied the store and recalculated on demand).
- **Index cap (`STORED_FORMULA_LIMIT = 140_000`)**: past 140,000 linked
  formulas, new results are not stored and are evaluated on read as in stage 2;
  already-stored results remain served.
- **What is not stored**: `#LIMIT!`, `#CYCLE!`, volatile formulas (`NOW`,
  `TODAY`), unparseable formulas, and any formula reading one of those are
  never written to `dh_calc_results`.
- **Desktop relies on tile jobs to fill the store**: `desktop/` did not change;
  on first view of uncalculated formulas the desktop still requests
  `calculation=cached` and runs one tile calculation job, which now fills the
  store via `CalculationJobs.offer()` so reopening displays those tiles
  immediately.
- **High skip share under continuous concurrent snapshot jobs (S1)**: while a
  read-only snapshot job holds a SQLite `SHARED` lock in rollback-journal mode,
  foreground `store_write` calls skip immediately (`busy_timeout=0`, ~95%
  skipped under a tight foreground read loop) rather than stalling the read;
  the finished job itself writes its batch once its snapshot closes.
- **Not verified**: Windows and Linux (and `SIGKILL` crash tests are skipped on
  Windows), cold OS file cache, and an idle machine.

## Departures from the plan

- **CLI output-guard ordering (`service/datahorizon/cli.py`)**: writable v9
  opens may populate missing `dh_calc_digests`, which updates the workbook
  file's bytes; CLI export/render commands now run output-path overwrite guards
  before opening the workbook and open read-only `charts list/suggest/resolve/render`
  commands with `readonly=True`.
- **Streaming `DependentsIndex.__init__`**: added a streaming fast path for
  already-sorted `(key, rect)` streams from `store.precedents()` (falling back
  to merging on unsorted input), reducing I1 peak build memory from `96.7 MiB`
  to `44.4 MiB`.
- **No other architectural departures**: all three pull requests, interfaces,
  table layout, digest mode selection rule, verification rules, and tests match
  `docs/design/calculation-stage-3-plan.md`.

## What stage 4 needs

Stage 4 adds the background recalculation worker, dependency-order evaluation
for pending roots, the updated read contract (`calculation=fresh` waiting up to
2 s and returning `pending` instead of `#LIMIT!`, `CALCULATION_PENDING` for
synchronous consumers, `calculation: "stored"` and status/generation reporting),
snapshot-job wait-before-pin coordination, and desktop status/polling updates
replacing per-tile calculation jobs. Stage 4 will also decide the volatile
clock contract (parent design section 14 item 4) and whether to raise or retain
the 4,194,304 per-formula cell count once cumulative per-read time budgets are
lifted for background work.
