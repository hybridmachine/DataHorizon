# Stored results and scaled recalculation: design

Draft 0.1 | November 18, 2026 | Status: approved by the owner on November 18, 2026, with the four decisions in section 14 accepted as written. Stage 1 (measurement) is built on branch `claude/calculation-stage-1`: [plan](calculation-stage-1-plan.md), [result](calculation-experiment.md). Stage 1 is isolated test code. Stage 2 (iterative evaluation and streamed ranges) is built on branch `claude/calculation-stage-2`: [plan](calculation-stage-2-plan.md), [result](calculation-stage-2.md); the 128-cell and 10,000-cell limits are gone, stored results, digests and background recalculation (stages 3 to 5) are not built. Paragraphs marked **Measured** record what stages 1 and 2 fixed.

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
  jobs), a per-formula count of 4,194,304 stored cells read, an 8-MiB cap on
  cells read one at a time, and evaluation by an explicit stack, so a
  dependency chain has no fixed depth. Stage 2 replaced a 10,000-step count and
  recursive evaluation, which bounded a chain at 128 cells and a range at
  10,000.
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
- **Pending roots**: the cells that changed, or the block rectangles found
  changed at open (section 6.2), whose dependents have not all been
  recalculated (section 7).
- **Calculation metadata**: the store's own version, the calculation version
  that produced the stored results and precedents (section 6.4), and a
  generation counter.

No virtual tables, triggers or SQLite extensions are used in the file, so any
SQLite build can open it. **Call**

**Measured**: the derived tables refer to a sheet by a small integer from a
derived sheet-number table, and carry no secondary index. Repeating the text
sheet identifiers grew the fixture by 57.5%; integers grow it by 11.0%. This
fixes file growth only; memory needs a compact in-memory structure as well
(section 13).

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
  A pinned snapshot cannot see a result the worker commits later, so a job
  obtains pending values before it pins (section 8.2).
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
  `source` and the `kind`. Style is excluded: it does not change a value.
- **Measured**, and a close call: a block is 64 rows of one sheet and its
  digest is one SHA-256 over its cells in key order, each cell length-prefixed.
  A commit re-reads and re-hashes each block it touches (0.73 to 0.83 ms for a
  640-cell block). Summing per-cell hashes avoids the re-read and passed its
  threshold narrowly, but made the opening scan about 0.6 s slower. Sheets
  wider than 10 columns were not measured; stage 3 measures them and
  re-decides between the two, or bounds blocks by columns too.
- One further digest covers the `sheets` table (identity and name of each
  sheet), because renaming or removing a sheet changes what a formula means.
- The digest guards against accident, not attack. A person who can write the
  file can write the digests. **Call**

### 6.2 When it is checked

- **On open.** The validation scan computes every block's digest and compares
  over the union of the stored block keys and the keys the scan finds. A block
  differs if its digest changed, if it has a stored digest and no cells now, or
  if it has cells and no stored digest. All equal: every stored result that is
  not pending is current, and no formula is evaluated.
- **A block that differs** becomes a pending root as a whole: the root is the
  block's rectangle (its sheet and row band), not the cells now stored in it.
  Every formula with a precedent that intersects the rectangle is recalculated,
  including one that reads a coordinate which is now empty. Example: `A1` is
  the only cell in its block and `B100` holds `=A1+1`; a direct edit deletes
  `A1`; the block's stored digest has no cells to match, and `B100` loses its
  result.
- **Every formula now stored in a differing block is itself pending**, whether
  or not it has precedents, because its own text may be what changed. Example:
  `A1` holds `=1+1` with the stored result 2; a direct edit makes it `=3+4`;
  the formula reads nothing, so no precedent rule reaches it, and it must
  still be recalculated to 7. Precedents and results stored for formulas that
  no longer exist in the block are deleted, the precedents of the formulas now
  in it are extracted again, and its digest is replaced or removed.
- **A differing `sheets` digest** invalidates every result and every stored
  precedent. A sheet's name decides what a formula reads, and the formula's
  text and block do not change when a sheet is renamed, added or removed.
  Example: `=Budget!A1` gives `#REF!` and has no precedents while no sheet is
  called `Budget`; a direct edit renames `Draft` to `Budget`. Every formula's
  precedents are extracted again against the new sheet list before the
  dependents lookup is used for anything, so a later edit of `Budget!A1`
  finds its reader.
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
continues in the background. No result is released until the whole scan has
finished and the invalidation that follows from it has been applied. A result
is not released when its own block verifies: `=Z1000` in `A1` sits in a block
that verifies long before the scan reaches a changed `Z1000`. Releasing each
result once every block it transitively depends on has verified would show
values sooner and is not designed. **Call**
Either way no result is shown as current before every block has been verified
in this session.

**Measured**: on the 1,000,000-cell fixture the chosen digest adds 1.3 to
1.8 s to an opening that takes 0.4 to 0.7 s today, so verification runs during
opening.
Stage 3 confirms this on the opening benchmark's long-text shape before
relying on it; the fallback stays designed.

A workbook opened read-only that turns out to have changed blocks cannot
persist the repair; it recalculates into the temporary store (5.2).

### 6.4 The calculation version

Digests cover a workbook's inputs. They do not cover the code that turned
those inputs into results. A workbook can hold `#NAME?` for a function that a
later service implements; every digest still matches, and without a further
rule the old error would be served for ever.

- The service has one integer, the calculation version. It is raised in any
  release that can change the result of some formula on unchanged inputs, or
  the precedents extracted from some formula: a function added or corrected,
  a change to coercion, error precedence or limits, a change to the parser or
  to reference resolution. Releases that change none of these keep it.
- The store records the calculation version that produced its results and
  precedents. On open, a stored version that differs from the service's, in
  either direction, invalidates every stored result and every stored
  precedent: precedents are extracted again and every formula is pending.
  Input digests stay valid, because they do not depend on the evaluator. A
  writable open then records the service's version; a read-only open
  recalculates into the temporary store.
- This is separate from the store's own version, which describes the layout of
  the derived tables, and from the native format version.
- Finer invalidation (only the formulas that use a changed function) is not
  designed. Recalculating everything after an upgrade is the cost. **Call**
- A test fails when the set of implemented functions changes without the
  calculation version changing, so that the commonest case cannot be
  forgotten. Other semantic changes rely on review.

## 7. Recalculation

### 7.1 Marking

- A managed commit records the changed input cells as pending roots in its own
  transaction. This is the durable fact. The commit does not persist the
  transitive set of dependents.
- A root that holds a formula is itself recalculated, as well as what reads
  it: editing `=1+1` to `=3+4` changes a formula that has no precedents.
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
- Precedents are every reference a formula could read, including the branch
  of an `IF` that is not taken. They decide what is recalculated. They do not
  decide what is a cycle: `=IF(FALSE,A1,42)` in `A1` is 42 today and stays 42.
  Formulas the dependency order cannot place are evaluated by following the
  references they really read, as today. `#CYCLE!` goes to a formula that
  reaches itself that way and to what reads it, stored like any other result.
  **Measured**: the prototype first gave `#CYCLE!` to the whole static cycle,
  which disagreed with the service.
  **Measured** (stage 2): the service now evaluates a chain with an explicit
  stack, so depth is no longer limited to 128. On the plain calculator a chain
  costs about 35 µs per formula, and a 2 s read completes 32,768 formulas and not
  65,536. The cached calculator that ordinary reads use is quadratic in chain
  length while the cache accepts results (it walks every precedent of each result
  it saves) and linear after it turns itself off at 4,096 results, so a default
  0.25 s read completes chains of 512 and 4,096 but not 1,024 or 2,048
  ([result](calculation-stage-2.md#s2-how-long-a-chain-one-read-completes)).
- Sheet names in references are matched as the `sheets` table matches them
  (SQLite `NOCASE`, ASCII letters only), in precedent extraction and in
  evaluation alike. `='é'!A1` does not read a sheet named `É`.
- A streamed range is read in row, then column order, the order the service
  visits a rectangle today. The order decides floating-point rounding and
  which cell's error surfaces first, so it must not depend on the query plan.
- Aggregates, conditional aggregates and lookups over a range read the range as
  a stream from SQLite, joined to stored results for the formula cells in it,
  and do not materialise it.
- Limits are per formula and are counts, not times: cells read by one formula
  and bytes held by one formula. They are measured, fixed and documented.
  **Measured**: 4,194,304 cells per formula is proposed (about 8 s at the
  2 µs per cell measured for a streamed column of short numbers; other cell
  kinds were not timed), on condition that stage 2 streams every
  range-reading function. **Measured** (stage 2): every range-reading function
  now streams, and a streamed cell costs 4.2 µs for numbers, 2.9 µs for
  40-character text, 4.1 µs for dates and 32.7 µs for a column of formulas that
  have to be evaluated, not 2 µs. A formula that read 4,194,304 cells would take
  about 12 to 17 s over stored values and over 2 minutes over uncalculated
  formulas. The count stands as a count; the time attached to it does not, and
  stage 4 must settle whether to keep, lower or weight it. The count is charged
  where cells are read and counts stored cells only, not empty coordinates. A
  range is read in bands of 1,024 rows, with the time budget checked before each. A
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
  small workbook today still succeeds. Jobs wait before they pin their
  snapshot (section 8.2). If a needed value is still pending at
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

### 8.2 Jobs that read a snapshot

Chart, export, query and calculation jobs read through a pinned SQLite
snapshot so that their inputs cannot change under them. Results are committed
to the same file, so a snapshot pinned while a needed formula is pending never
sees its value, however long the job waits.

- A job waits before it pins. It names the values it needs, they go to the
  front of the worker's queue, and it waits for them while holding no read
  transaction, inside its own deadline.
- It then pins the snapshot and checks, inside it, that every needed result is
  current: results, pending roots and inputs in one snapshot belong to one
  committed state, so a result that no pending root reaches is the value of
  the inputs the job reads. A job therefore cannot combine one state's inputs
  with another state's results.
- If an edit committed between the wait and the pin, some needed value is
  pending again in the snapshot. The job releases the snapshot and waits
  again, inside the same deadline. At the deadline it fails with
  `CALCULATION_PENDING`.
- A job that must report the state it captured (`expected_state`) compares it
  after pinning, as today, and reports `stale` if it moved.
- Evaluating the missing values privately against the captured inputs was the
  alternative. It would put a second evaluator beside the worker, with its own
  limits, for formulas of any depth; it is not designed. **Call**

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

**Measured** (stage 2): `=SUM(Sheet1!A1:A100000)` takes 0.41 s with a 2 s budget
and shows `#LIMIT!` in a default 0.25 s read, so the stage-1 target (at most
0.25 s) is missed on the service. A `SUM` over 1,000,000 stored cells took 4.1 s
and added at most 32 KiB to the process's peak resident memory, because the
cells are not held. `COUNTIF`, a three-criteria `SUMIFS` and an `XLOOKUP` to the
last row over 100,000 rows take 0.64, 2.18 and 0.94 s. With 4,096 cached range
readers an edit's invalidation takes 2.7 ms when it touches a few and 0.48 s
when it touches 2,098, because the cache has no spatial index (8.6 million
containment checks). Medians on a machine that was not idle
([result](calculation-stage-2.md)).

Two services opening one file is unsupported, as today: each one's result
writes would look like external edits to the other.

## 10. Verification

- Unit and service tests for each rule in sections 6 to 8, written first.
- A differential test: for generated workbooks, the stored results after any
  sequence of edits equal a from-scratch evaluation of the final inputs.
- Freshness tests that edit the file with an independent SQLite connection
  while the service is running and while it is stopped: changing, adding and
  deleting cells, renaming a sheet, and deleting derived rows. They include
  deleting the only cell of a block that a formula elsewhere reads, renaming a
  sheet to a name that a `#REF!` formula refers to and then editing the cell
  it now reads, and, under the background fallback, a formula in an early
  block that reads a changed cell in a late one.
- A workbook edited directly so that a formula with no precedents has new
  text (`=1+1` with a stored 2, changed to `=3+4`) reopens with 7; the same
  through a managed edit.
- A workbook prepopulated with results and precedents from an older
  calculation version (a stored `#NAME?` for a function that now exists)
  reopens with every formula pending and then the new value.
- A snapshot job whose needed value is pending when it starts gets the value
  once the worker finishes; one with an edit committed between its wait and
  its pin returns values of a single input state, or `CALCULATION_PENDING`,
  and never a mixture.
- Differential cases against today's evaluation for an untaken branch that
  refers to its own cell, a sheet name that differs only in a non-ASCII
  letter's case, and a sum whose result depends on the order of addition.
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
- The hash function, the combining operation and the block height: fixed by
  stage 1 (6.1).
- Dependency data is held in memory in a compact integer-keyed form for
  workbooks of about 100,000 formulas (a probe measured 45.7 MiB and 0.27 s to
  load for 102,003; the tuple-keyed prototype needed 131.7 MiB). That form
  reaches the 256-MiB service target at roughly 570,000 formulas, and the SQL
  lookup as prototyped took 41.6 s, so what serves larger workbooks is still
  open. How dependents of a cell inside a precedent rectangle are found stays
  the builder's choice; the prototype's column-and-bisect lookup degrades when
  many ranges cover a formula column and must not be carried over as it is.
  **Measured** (stage 2): stage 2's in-memory cache records a range as one
  rectangle and still has no interval structure; with 4,096 cached readers over
  a column one edit checked 8.6 million rectangles in 0.48 s, so the point stands.
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
