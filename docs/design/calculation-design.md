# Stored results and scaled recalculation: design

Draft 0.1 | November 18, 2026 | Status: approved by the owner on November 18, 2026, with the four decisions in section 14 accepted as written. Nothing here is built. [Stage 1 plan](calculation-stage-1-plan.md).

This is the design note that [increment 10 of the Excel fidelity programme](excel-fidelity-plan.md#increment-10-stored-results-and-scaled-recalculation)
requires before the increment is planned. It is a design to plan from, not a
task plan. Each decision is marked **Owner** (stated by the owner) or **Call**
(chosen by the author and open to reversal).

## 1. Intent

A person who opens a large workbook should see its calculated values at once,
and an edit should recalculate what depends on it and nothing else. Today every
read recalculates what it shows, inside small fixed limits, so a workbook with
long formula chains or large ranges shows `#LIMIT!` where Excel shows a number.
Increment 1 made million-cell files importable and increment 9 made them
editable; this increment makes them calculate.

The calculation model built here is the one later increments plug into: data
validation, conditional formatting, arrays and the function catalogue.

## 2. Goals, non-goals and anti-goals

Goals:
- Calculated values are kept in the workbook and used on open and on scroll
  without recalculating.
- A stored value is shown as current only when it is proven to match the stored
  inputs, including after direct SQLite edits made while the workbook was
  closed.
- An edit recalculates its dependents in dependency order, iteratively, with
  the visible cells first and the rest in the background.
- Aggregates and lookups over ranges larger than 10,000 cells work, within
  measured limits.
- Every consumer of calculated values (grid, REST, CLI, SDK, hooks, lists,
  find, charts, export) reads the same results.

Non-goals:
- New functions, arrays and spill (increment 8).
- Manual calculation mode, iterative calculation of circular references and
  precision as displayed. Import reports each of these settings when a file
  declares it; DataHorizon calculates automatically, reports `#CYCLE!` for a
  cycle, and keeps full precision. **Call**
- Change events for recalculated values. Results are derived state; a
  recalculation runs no hook and no binding, as today.
- Multi-process or multi-core calculation.
- Moving stored results with their cells through a structural edit (section 8).

Anti-goals (a failure even if everything else works):
- A stale value shown as current, in any client, after any sequence of managed
  edits, direct edits, crashes and reopens.
- A background recalculation that makes clients stale, clears Undo, or delays
  an edit's acknowledgement beyond its existing budget.
- A workbook file that ordinary SQLite tools can no longer open and edit.

## 3. How calculation works today

- Each read builds a `Calculator` ([formula.py](../../service/datahorizon/formula.py))
  with a 0.25-second budget (2 seconds for lists, find, export and calculation
  jobs), a 10,000-step count, an 8-MiB input cap and recursive evaluation, which
  bounds a dependency chain at 128 cells.
- [dependencies.py](../../service/datahorizon/dependencies.py) keeps up to
  4,096 results and 50,000 edges in memory. It clears itself when it overflows,
  on every structural edit, and on every external change. Volatile formulas
  (`NOW`, `TODAY`) and their dependents are never kept.
- The desktop reads with `calculation=cached`, which returns `pending` for a
  formula with no kept result, and fills visible tiles through read-only
  [calculation jobs](viewport-calculation.md) on a private snapshot.
- Hooks read through a transactional calculator with no cache.
- The managed `revision` in `dh_meta` moves only on managed commits. A direct
  edit is noticed through SQLite's `data_version`, which works only while the
  service holds its connection open; `refresh` then validates the file again,
  rotates the state token and clears Undo and the cache. A direct edit made
  while the workbook is closed leaves no trace in the file.
- Opening validates the cells with one filtered SQL scan inside a five-second
  budget ([native opening](native-opening-performance.md)). The scan returns
  only invalid or temporal rows to Python, so a healthy file costs no per-cell
  Python work today.

## 4. Approaches to freshness

The question the programme left open is how a stored value proves it is fresh
after an independent SQLite edit. Three approaches were considered.

**A. Content fingerprint, checked in the scan opening already does.** Digests of
the stored inputs are kept beside the results. The validation scan recomputes
them; a difference marks the affected cells and their dependents for
recalculation. It holds whatever the external tool did and asks nothing of it.
Its cost is that opening does more work per cell.

**B. SQL triggers on `cells` that log each changed cell.** Exact, and nearly
free at open. Every managed write pays for it too, including million-cell
imports and large structural shifts. An external tool can drop or bypass a
trigger, after which a stale value would show as current, so it would need A as
a backstop anyway. The native format rejects files that contain triggers.

**C. Each result stores a digest of its own inputs and is verified when read.**
No cost at open. Verifying a sum over 100,000 cells costs about as much as
recalculating it, and a long chain must be walked link by link, which defeats
the purpose.

**Decision: A.** **Owner** (November 18, 2026). It is the only one of the three
that keeps the anti-goal without help, and its cost falls where a full scan
already happens.

## 5. Storage

### 5.1 Where

Results live in the workbook file, in reserved tables documented as derived
state. **Call** (the programme's starting position; a sidecar file was the
alternative and is not taken, because a result that travels with the file
survives copy, Save As and backup, and external tools can read it as they read
Excel's cached values).

This is native format v9. The tables hold:

- **Results**: per formula cell, the calculated value with its value kind, or
  the error code and message. Only cells whose value comes from the calculator
  have a row. A missing row means "not calculated yet".
- **Precedents**: per formula cell, what it reads, as rectangles (a single cell
  is a one-cell rectangle) with the sheet identity, plus flags for volatile and
  for formulas whose references cannot be extracted. Opening must not need to
  parse every formula.
- **Input digests**: one digest per block of cells (section 6).
- **Pending roots**: the cells whose inputs changed and whose dependents have
  not all been recalculated (section 7).
- **Calculation metadata**: the store's own version and a generation counter.

No virtual tables, triggers or SQLite extensions are used in the file, so any
SQLite build can open it. **Call**

Table and column names and the value encoding are left to the builder
(section 13). The [native format document](native-format-v1.md) gains a v9
section that documents every table for external readers.

### 5.2 Upgrade, read-only workbooks and snapshots

- A workbook is upgraded to v9 by the first managed commit made by a service
  that has this increment, in that commit's transaction. The upgrade creates
  the empty derived tables, sets the version and records that commit's roots;
  it copies nothing else, so the first edit is not delayed. The worker fills
  digests, precedents and results afterwards. A block with no digest is an
  unverified block and a formula with no result is pending, so a partly filled
  v9 file is valid at every point. Workbooks created or imported by such a
  service are v9 from the start. **Call**
- Opening a workbook that is not yet v9 never writes the file. Opening a v9
  workbook writable may write the derived tables only (repaired digests and
  precedents, roots, results), so that work done after an open is not repeated
  at the next one; it never writes `cells`, `sheets`, metadata, `revision` or
  receipts. A read-only open never writes anything. **Call**
- Until a workbook is v9, and for a workbook opened read-only, the same engine
  keeps the same store in a private temporary SQLite database that is deleted
  on close. Behaviour is identical except that nothing survives the close.
  **Call**
- A read-only snapshot connection (calculation jobs, chart jobs, query jobs)
  reads the stored results of the state it captured and never writes them.
- Older services reject v9 files, as they reject any newer version today.

### 5.3 What external tools may assume

A direct reader may read results, with the documented caveat that a result can
be stale: it reflects the inputs as of the last managed recalculation. A direct
writer need not touch any derived table. A direct edit to the derived tables
themselves is not detected and is documented as unsupported. Deleting every row
of the derived tables is always safe and forces a full recalculation. **Call**

### 5.4 What does not change

Writing results does not change `revision`, the state token or Undo/Redo, makes
no operation receipt and runs no hook. A client's `expected_state` stays valid
across a background recalculation. Results are written only through the
service's own long-lived connection, because a write from any other connection
would look like an external edit to `data_version` and discard everything.

## 6. Freshness

### 6.1 The digest

- Cells are grouped into blocks: a sheet and a band of rows. The band height is
  measured and fixed in the format (section 13).
- A block's digest covers, for every stored cell in it, the coordinates, the
  `source` and the `kind`. Style is excluded: it does not change a value. The
  per-cell hashes are combined so that the block digest does not depend on scan
  order and one cell's change updates it without rereading the block.
- One further digest covers the `sheets` table (identity and name of each
  sheet), because renaming or removing a sheet changes what a formula means.
- The digest guards against accident, not attack. A person who can write the
  file can write the digests. **Call**

### 6.2 When it is checked

- **On open.** The validation scan computes every block's digest and compares.
  All equal: every stored result that is not pending is current, and no formula
  is evaluated. A block that differs: its cells become pending roots
  (section 7), its formulas' precedents are extracted again, and its digest is
  replaced. A differing `sheets` digest invalidates every result.
- **On refresh after an external change** (`data_version` moved): the same
  comparison, in the validation scan `refresh` already runs. The existing
  consequences stay: new state token, Undo cleared, no handler replay.
- **On a managed commit.** The commit updates the digests of the blocks it
  touches, in its own transaction, together with the pending roots. No scan.

### 6.3 Cost and the fallback

Opening's validation is a filtered SQL query that returns no rows for a healthy
file, so the digest is new work for every cell, done at the point where a full
pass over the cells already happens. Stage 1 (section 12) measures it on the
1,000,000-cell fixture before anything else is built. If the
five-second opening budget cannot hold it, verification moves off the opening
path: the workbook opens, every result is shown as pending, and verification
continues in the background and releases results block by block. **Call**
Either way no result is shown as current before its block has been verified in
this session.

A workbook opened read-only that turns out to have changed blocks cannot
persist the repair; it recalculates into the temporary store (5.2).

## 7. Recalculation

### 7.1 Marking

- A managed commit records the changed input cells as pending roots in its own
  transaction. This is the durable fact. The commit does not persist the
  transitive set of dependents.
- After the commit, the service derives the set of affected formulas from the
  roots and the precedents. No read may return an affected formula's stored
  result as current, from the moment the commit is visible.
- After a crash, the roots are still in the file. Opening derives the affected
  set again before it serves any result. This is what makes kill-and-reopen
  safe at every point: a result is either written after its inputs and not
  reachable from a root, or it is treated as pending.
- A root is removed in the transaction that writes the last result that
  depended on it.

### 7.2 Evaluating

- Evaluation is iterative, in dependency order. Chain depth is bounded by
  memory and time, not by 128.
- A cycle gives `#CYCLE!` for its members and for what depends on them, stored
  like any other result.
- Aggregates, conditional aggregates and lookups over a range read the range as
  a stream from SQLite, joined to stored results for the formula cells in it,
  and do not materialise it.
- Limits are per formula and are counts, not times: cells read by one formula
  and bytes held by one formula. They are measured, fixed and documented. A
  formula that exceeds one stores `#LIMIT!` and is counted in the calculation
  status. Because the limit is a count, the same inputs always give the same
  result, so `#LIMIT!` is stored like any other error, is recalculated only
  when its inputs change, and the differential test can assert equality.
  There is no cumulative budget for a recalculation: a legal workbook that
  needs a long time is calculated for a long time, visible cells first, with
  progress in the status. **Call**
- **Volatile formulas** (`NOW`, `TODAY`) and their dependents are calculated by
  the same worker and follow the same current, error and pending rule. Their
  results are kept in the session's temporary store (5.2), never in the file,
  so a new session never inherits one. One clock sample is taken at open, at
  each managed commit and at an explicit recalculation request; a new sample
  makes the volatile formulas pending, and a dependent is recalculated only if
  the value it reads changed (a `TODAY()` column is not recalculated by every
  edit within one day). **Call**, and a change to the present clock contract,
  under which every read takes a new sample: see section 14.
- A formula whose references cannot be extracted (unsupported syntax) cannot
  be ordered. It and its dependents are treated like volatile formulas, with
  every managed commit making them pending. **Call**

### 7.3 Scheduling

- One calculation worker per open workbook runs in the service. It takes
  pending work in this order: cells a read is waiting for, cells in a range a
  client has declared visible, then everything else in dependency order.
- It writes results in short transactions through the service's connection and
  gives way to edits between them. An edit never waits for a full
  recalculation. The existing edit-acknowledgement budget is re-measured with a
  recalculation running.
- Work computed against inputs that an edit has since changed is discarded,
  not written.
- Whether the worker is a thread in the service or a child process that
  computes on a snapshot and hands results back to be checked and written is
  the builder's choice, decided by measurement (section 13).

### 7.4 Transactions and hooks

A hook or macro reading a value inside an operation sees the transaction's own
uncommitted edits. The transactional calculator uses stored results for
formulas the transaction has not affected and evaluates affected ones on
demand, iteratively, within the operation's existing budget. A formula that is
pending from earlier background work is evaluated the same way. It does not
wait on the worker, which cannot see the uncommitted transaction. A value that
cannot be produced within the budget raises `#LIMIT!` to the hook, as today, so
the failure is visible and the handler or the operation decides. **Call**
Nothing it evaluates is written to the store before commit. A rollback leaves
the store and the roots as they were.

### 7.5 Structural edits

A structural edit on the ordinary or the large path (insert or delete rows or
columns; add, delete, duplicate, rename or reorder a sheet) rewrites the
precedents and digests of what it touched and makes every formula in the
workbook pending. Results are recalculated in the background. **Call**: this
keeps today's rule (a structural edit clears the cache) and keeps the
increment-9 paths simple. Moving results with their cells is a later
optimisation, taken only if the measured full recalculation after an edit is
unacceptable.

Undo and Redo of any edit are ordinary managed commits and mark roots the same
way.

## 8. Reading values

One rule serves every reader: a formula cell is `current` with its stored
value, `error` with its stored error, or `pending` with no value.

- **`calculation=cached`** (the desktop grid): returns at once; pending cells
  are marked pending. The read's range is taken as a visible range for the
  worker's ordering.
- **`calculation=fresh`** (the default for REST, CLI and SDK): the requested
  cells go to the front of the worker's queue and the read waits for them,
  inside its existing two-second deadline. Cells still pending at the deadline
  are returned as `pending`, not as `#LIMIT!`. **Call**: this is a visible
  contract change. Today such a cell returns `#LIMIT!` after 0.25 seconds;
  under this design it returns its value if two seconds suffice and otherwise
  says truthfully that the value is on its way.
- **Lists, find, record operations, charts**: read stored results. The values
  one needs go to the front of the worker's queue and it waits for them inside
  its existing deadline (two seconds for the synchronous calls, the job's own
  deadline for chart, export and operation jobs), so a call that succeeds on a
  small workbook today still succeeds. If a needed value is still pending at
  the deadline it fails with a structured `CALCULATION_PENDING` error that
  carries the calculation status, and never substitutes a stale or empty
  value. **Call**
- **Values export**: uses stored results and blocks if a required result is not
  current, as now. **Owner** (programme)
- **Calculation jobs** (`calculations` resource): the resource and its contract
  stay, served from the store. The desktop stops using one job per tile.

### 8.1 Status and progress

- Workbook info reports `calculation: "stored"` and a calculation status:
  the count of pending formulas, the count at `#LIMIT!`, whether verification
  is still running, and the store's generation counter, which increases
  whenever results are written.
- REST, the CLI and the SDK expose the status, and a call that waits until
  calculation is complete or a deadline passes.
- The desktop polls the generation while anything is pending, re-reads the
  visible tiles when it moves, and shows "Calculating… N cells" in the status
  bar. Pending cells show the ellipsis they show today. No new window or
  dialog. **Call**

## 9. Limits

Every limit that remains is measured on the reference Mac and recorded:
formulas per workbook, precedent rectangles, the size of one streamed range,
time for one formula, service memory with the dependency data loaded, and the
file growth from the derived tables. The existing 256-MiB service memory target
applies to the measured workload.

The programme's "100,000-row running total" is read as the chained form
(`=B1+A2`), which is linear. **Call** The prefix form (`=SUM(A$1:A2)` filled
down) reads about five billion cells for 100,000 rows. Each of its formulas is
within the per-formula limits, so none shows `#LIMIT!`; the whole column simply
takes long in the background. Its throughput is measured and documented, and it
is not part of the completion criteria.

Two services opening one file is unsupported, as today: each one's result
writes would look like external edits to the other.

## 10. Verification

- Unit and service tests for each rule in sections 6 to 8, written first.
- A differential test: for generated workbooks, the stored results after any
  sequence of edits equal a from-scratch evaluation of the final inputs.
- Freshness tests that edit the file with an independent SQLite connection
  while the service is running and while it is stopped: changing, adding and
  deleting cells, renaming a sheet, and deleting derived rows.
- Crash tests that kill the service during an edit's commit, during a result
  write and during open verification, then reopen and compare against the
  from-scratch evaluation.
- A capacity driver in the style of
  [the increment 9 qualification](../../tests/qualify_large_structure.py).
- Agreement with Excel's cached values needs workbooks saved by real Excel,
  which the repository does not have
  ([programme section 14, item 1](excel-fidelity-plan.md#14-open-owner-decisions)).
  The comparison harness is built against the cached values in the existing
  fixtures and the owner's sample file; the criterion stays open until the
  corpus exists.

## 11. Done when

From the programme, unchanged:

- A generated workbook with 1,000,000 cells, 100,000 formulas and a
  100,000-row running total opens and shows current values within recorded
  budgets.
- A single edit at the top of the running total recalculates within a recorded
  budget.
- Kill-and-reopen never shows a stale value as current.
- Calculated results agree with the cached values in the Excel-saved corpus for
  every function DataHorizon claims.

Added by this note:

- A direct SQLite edit made while the workbook is closed is reflected in every
  dependent value after reopening, with unaffected blocks not recalculated.
- The ordinary service and desktop suites pass, and the opening, viewport and
  edit budgets are re-measured.

## 12. Stages

Each stage is one or more reviewed pull requests that leave `main` working.

1. **Measure.** Isolated test code, no product change: digest cost in the
   opening scan at 1,000,000 cells, block size, dependency data size and load
   time for 100,000 formulas, and an iterative evaluator on the running total.
   The result fixes the numbers this note leaves open and confirms or triggers
   the fallback in 6.3.
2. **Iterative evaluation and streamed ranges**, behind today's contracts and
   the in-memory cache. Removes the 128-cell and 10,000-cell limits on their
   own.
3. **The v9 store and freshness**: results, precedents, digests, upgrade,
   temporary store, open and refresh verification.
4. **Incremental recalculation**: roots, the worker, read contracts, status,
   REST/CLI/SDK and the desktop.
5. **Qualification**: capacity, crash and direct-edit runs, documents and
   status.

## 13. Left to the builder

- Table, column, route and CLI names and JSON shapes, consistent with existing
  conventions.
- The hash function, the combining operation and the block height, from stage 1.
- Whether dependency data is held in memory or queried from SQLite, and how
  dependents of a cell inside a precedent rectangle are found.
- Thread or child process for the worker.
- Batch sizes and the worker's yielding rule.
- The pull-request split inside each stage.

## 14. Owner decisions

All four were accepted as written on November 18, 2026. **Owner**

1. **Default read contract** (section 8). A default read that cannot finish
   returns `pending` after up to two seconds where it returns `#LIMIT!` today.
   Confirm or reverse.
2. **Excel-saved corpus.** Still owed from the programme; without it the fourth
   completion criterion cannot be met. **Call**: the stages are built and
   merged regardless, and that criterion is recorded as open until the corpus
   exists, as increment 1 did.
3. **Structural edits recalculate everything** (7.5). Confirm as a first
   position.
4. **Volatile clock contract** (7.2). The programme says `NOW` and `TODAY`
   keep their present contract, a new clock sample for every read. Kept as it
   is, a column that depends on `TODAY()` can never be stored and stays inside
   today's per-read limits. This note samples the clock at open, at each
   managed commit and on request, as Excel does. Confirm or reverse.
