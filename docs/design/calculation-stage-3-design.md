# Calculation stage 3: the v9 store and freshness (stage design)

Draft 0.1 | November 23, 2026 | Status: proposed, awaiting the owner's approval.
Parent: [stored results and scaled recalculation](calculation-design.md), approved November 18, 2026.
Earlier stages: [stage 1 result](calculation-experiment.md), [stage 2 result](calculation-stage-2.md).

This note fixes what stage 3 builds and where it departs from the parent design's
one-line description of the stage. Where it is silent, the parent design holds.
Paragraphs marked **Owner** were decided by the owner; **Call** marks a choice
made here that the owner can reverse.

## 1. Intent

Stage 2 removed the chain and range limits, but nothing is kept between reads
beyond a small in-memory cache, so a large formula is paid for again on every
read and after every reopen. Stage 3 keeps calculated results in the workbook
file and proves, by content digests, that a kept result still matches its
inputs, including after an edit made by another program.

The parent design gives stage 3 the store and leaves pending roots and the
background worker to stage 4. A store with nothing to fill it and nothing to
invalidate it cannot be served, so this stage brings forward the marking rules of
parent section 7.1 and fills the store from ordinary reads. **Owner** (November
23, 2026: stored results are served when stage 3 merges.)

## 2. Goals, non-goals and anti-goals

Goals:

- A formula calculated by a read is stored, with what it reads, and served to
  later reads and after reopening for as long as its inputs are unchanged.
- A managed edit, an Undo or Redo, a structural edit, a direct SQLite edit made
  while the workbook is open or closed, a crash at any point, and a service whose
  evaluator changed can never cause a stale result to be served as current.
- Format v9, documented for external readers, with upgrade on the first managed
  commit and a temporary store for read-only and older workbooks.
- The two measurements the earlier stages left to this one, and the limits of the
  store itself, recorded.

Non-goals (all stay with stage 4 unless noted):

- The background worker, visible-range ordering and any thread that calculates.
- The read contract: a default read still has 0.25 s and still shows `#LIMIT!`
  when it runs out; `pending` in place of `#LIMIT!` is stage 4. The 100,000-row
  running total still shows `#LIMIT!` in a default read until enough of it has
  been stored by reads with longer budgets.
- Calculation status, a wait call, REST, CLI and SDK shape changes, and any
  desktop change. Workbook info keeps `"calculation": "evaluated_on_read"`.
- The clock contract: `NOW` and `TODAY` are sampled on every read as today, and
  volatile formulas and what reads them are not stored at all.
- Storing `#LIMIT!` and `#CYCLE!`. Exactly the results the in-memory cache keeps
  today are stored: values and cell errors.
- Stored results inside transactions (parent 7.4) and inside snapshot jobs
  (parent 8.2). Hooks, macros, operation jobs, calculation jobs and chart jobs
  evaluate as they do today and take nothing from the store. Only calculation
  jobs give anything to it (5.3a).
- The background verification fallback of parent 6.3.
- Qualification (stage 5).

Anti-goals:

- A workbook that opens today fails to open, or opens outside its budget, because
  of the store.
- A read-only open, or any open of a workbook that is not yet v9, writes the file.
- Any result differs from what stage 2 gives for the same inputs.
- The store becomes a second cache with its own eviction that silently empties at
  scale.

## 3. Constraints

- Python standard library and SQLite only. No virtual tables, triggers or
  extensions in the file.
- Results are written only through the service's own connection (parent 5.4).
  Writing them changes neither `revision`, the state token nor Undo, makes no
  receipt and runs no hook. This holds for every session that has the workbook
  open, not only the one that writes: see 5.3b.
- The opening budget is five seconds. Edit acknowledgement has a recorded budget
  of 100 ms ([initial contracts](initial-contracts.md)).
- The 178-case characterization corpus passes with nothing re-recorded.
- Older services reject v9 files, as they reject any newer version.

## 4. Approaches

1. **The store replaces the cache.** A new module owns the derived tables.
   Reads write results and precedents through to it; dependents are found through
   a compact in-memory index loaded from the stored precedents; commits record
   roots and re-hash the blocks they touch. Most to build. It is the structure
   stage 4's worker needs and it has no cap that empties it.
2. **The store as a second tier under today's cache.** The bounded
   `DependencyIndex` stays in charge and mirrors saves and invalidations to the
   file. Smallest change. The index clears itself past 4,096 results or 50,000
   edges, and the file store would have to be emptied with it, so it stops
   helping at the 100,000-formula scale the parent design targets.
3. Dropped before comparison: dependents by SQL alone (41.6 s in stage 1) and a
   full synchronous recalculation inside the commit (a 3 s chain inside an edit).

**Owner** (November 23, 2026): approach 1.

## 5. Design

### 5.1 The store

One module owns the derived tables of parent 5.1: sheet numbers, results,
precedents, input digests, pending roots and calculation metadata. It has two
homes with one behaviour:

- **In the file**, for a v9 workbook opened writable.
- **In a private temporary SQLite database** deleted on close, for a workbook
  that is not yet v9 and for any workbook opened read-only (parent 5.2).

`DependencyIndex` and its 4,096-result cache are removed. `calculation=cached`
reads mark a formula pending when the store has no current result for it, where
they consult the cache today.

The store is not opened at all for the read-only workbook copies that
calculation, chart, query and operation jobs open on a snapshot, nor for recovery
inspection. Those neither verify digests nor create a temporary database. **Call**
(This reverses a call floated in conversation, that snapshot jobs use stored
results when their snapshot holds no roots. A job's snapshot is a second
connection that has not verified the file, and proving it may trust the store is
parent 8.2's work.) Calculation jobs do hand their results to the store when they
finish (5.3a).

### 5.2 Upgrade

A writable workbook below v9 becomes v9 in its first managed commit, in that
commit's transaction: the tables of every earlier format level that are missing
are created as existing upgrades create them, the empty derived tables are added
and the version is set. Nothing is copied. Workbooks created or imported by this
service are v9 from the start (parent 5.2).

This changes what a new file is. Today a new workbook is format 1 and each
feature raises the level when it is first used; from this stage every workbook the
service creates or commits to holds every level's tables and gets the per-open
metadata and relational validation of those levels. The capabilities payload
reports format 9, lists 9 as supported and gives 9 as the new-file version; that
is the one REST value that changes. Tests that record a format level are updated;
the characterization corpus records values, not levels, and is not re-recorded.
Opening cost for a small v9 workbook against the same workbook at v1 is measured.

A session that upgrades moves from the
temporary store to the file store at that commit; carrying the temporary store's
contents across is optional and left to the builder.

### 5.3 What a read stores

A calculator used by an ordinary read (grid read, summary, list, find, record
operations, values export) takes results from the store and collects the results
it calculates. After the read's transaction has ended, and still under the
workbook lock, the collected results are written in one write transaction. A
result is written only if all of these hold:

- It is a kind the cache keeps today: a value or a cell error, from a formula that
  is neither volatile nor unparseable and read no formula that is.
- Its precedents are written in the same transaction. A stored result therefore
  always has stored precedents, which is what makes invalidation complete without
  a pass over every formula.
- Every formula it read has a stored result, or is written in the same
  transaction.
- The block that holds the formula and every block its precedent rectangles
  intersect has a stored digest. Missing ones are computed and written in the same
  transaction. **Call**: digests fill lazily, here and at open (5.5), so the
  upgrade commit stays cheap without a worker.
- `PRAGMA data_version` and the state token are what they were when the read
  began. Otherwise the whole batch is discarded.

If the write fails or the allowance for it (builder's choice, measured) runs out,
the read still returns its response; storing is best effort and never changes what
a read returns.

A formula reading a very large range needs digests for every block of it. That
cost is measured (section 7) and, if it does not fit, the batch is skipped for
that formula and the blocks are left for the open scan.

### 5.3b Results stored by another session

A results-only commit moves `PRAGMA data_version` for every other connection,
which is otherwise the sign of an external change (new state token, Undo
cleared, validation and the digest comparison). A session tells the two apart
with three marks it keeps while the file is as it last saw it: `revision`,
`dh_calc_meta.generation`, and SQLite's file change counter (header bytes
24..27), read under a shared lock.

- Every results-only commit to the workbook's own store raises `generation`
  by exactly one, in the same transaction.
- SQLite raises the change counter once for each committed transaction that
  changed the file, in the rollback-journal format DataHorizon writes.
- When `data_version` has moved, the other commits were results-only if and
  only if `revision` is unmoved and the change counter rose by exactly as much
  as `generation` did. A managed commit moves `revision`; a direct SQLite edit
  raises the counter without raising `generation`.

When the test passes, the session keeps its state token and Undo, skips
validation and the digest comparison, and rebuilds its dependents index and
pending roots from the store, since the other session may have added, deleted
or finished rows. When it fails, or the marks are unavailable (a file in WAL
format, a workbook below format 9, anything this session wrote that the marks
do not account for), the change is treated as external, as before. The test
can only err toward treating a change as external.

A direct edit that also raises `generation` by one is indistinguishable from a
stored result. Direct access makes no freshness promise beyond the digests,
which still catch such an edit at the next open or external refresh.

### 5.3a What a calculation job stores

The desktop grid reads with `calculation=cached`, which never calculates, and
fills its tiles through calculation jobs. Without this section a desktop session
would store nothing. **Owner** (November 23, 2026: tile calculations fill the
store.)

- A calculation job's calculator collects the results it calculates and their
  precedents, as a read's does, and returns them with the job's result. The job
  still evaluates on its own snapshot and takes nothing from the store.
- When the job completes, the service, under the workbook lock and after a
  refresh, compares the state token the job started with against the current one.
  If they are equal, it writes the batch by the rules of 5.3, computing missing
  digests on its own connection. If they differ, the batch is dropped. The token
  changes with every managed commit and with every external change the service
  has noticed, and the refresh notices any made before the comparison, so a batch
  is stored only if the inputs it was calculated from are the current ones.
- A job's clock is its own; volatile formulas and their readers are excluded by
  5.3's first rule, so no stored result depends on it.
- The job's response and the `calculations` resource do not change. The desktop
  does not change: its next cached read of a stored cell returns the value.
- Chart, query and operation jobs store nothing.

### 5.4 Commits and marking

In the commit's own transaction a managed commit:

- records each changed input cell as a pending root (parent 7.1);
- re-hashes each block it touched that has a stored digest, and leaves a block
  without one as it is;
- for a structural edit, a change to table records, or any commit that today
  clears the whole cache: deletes every result and precedent and the digests of
  the sheets it touched, and records no root.

**A commit must not hide an edit it never saw.** `mutate` refreshes and then takes
the write lock, and another program can write in between. Re-hashing a block then
would store a digest that includes the foreign edit, and the next comparison would
find nothing wrong. So, once it holds the write lock, a commit reads
`PRAGMA data_version`; if it has moved since the session last verified, the commit
empties the store (5.5) in its own transaction and re-hashes nothing. The
operation itself proceeds as it does today. **Call**

A commit that only reserves the receipt of a failed operation is not a managed
commit for these purposes: it neither upgrades the file nor touches the store.

After the commit, before any read is served, the service derives the affected
formulas from the roots through the in-memory index and stops serving their
results. It then deletes those results from the store in bounded slices, the
first inside the edit's acknowledgement budget and the rest at the start of later
requests, and deletes a root in the transaction that removes the last result it
reached. A root formula's own precedents are replaced; its dependents' precedents
stay. **Call**: without a worker, "pending" is "no result row", and deletion in
slices keeps a 100,000-dependent edit inside the acknowledgement budget. The slice
size is measured.

Opening a file that holds roots (a crash between a commit and the end of its
deletion) derives the affected set again and finishes the deletion before it
serves any stored result.

### 5.5 Freshness at open and refresh

On opening a v9 workbook the service computes every block's digest and the
`sheets` digest and applies parent 6.2: a differing block becomes a root as a
rectangle, every formula stored in it loses its result and precedents, and a
differing `sheets` digest clears every result and precedent. A writable open then
writes the repairs and the digests of blocks that had none; a read-only open
applies them to its temporary store, into which it first copies what it trusts
from the file.

A workbook below v9 has nothing to verify and its opening does not scan.

Refresh after an external change (`data_version` moved) runs the same comparison.

**One rule when verification cannot finish**: if the scan does not complete inside
its allowance (the remainder of the opening budget; the caller's deadline at
refresh), the workbook still opens or refreshes and the store is emptied:
results, precedents, digests and roots are all deleted, which parent 5.3 makes
always safe. **Call**: this replaces the background fallback of parent 6.3 for
this stage.

- A writable session empties the file store at once, in a short transaction of its
  own. If that transaction cannot run (the file is locked), the session holds a
  "distrusted" mark: it serves nothing from the store, writes no result or digest,
  and tries again at each later request; a managed commit made while the mark is
  held empties the store in its own transaction.
- A session on the temporary store empties that.
- An emptied store is an ordinary empty store. The session stores and serves again
  from then on; nothing is disabled until reopen.
- **At open**, hitting this rule on the long-text shape or on the stage 1 fixture
  stops the stage and brings the background fallback back to the owner.
- **At refresh**, the allowance is the caller's deadline, two seconds for a read.
  Stage 1 measured 1.3 to 1.8 s for the scan at 1,000,000 cells, so on a workbook
  of that size a direct edit made while it is open will usually empty the store,
  and its formulas are calculated again as reads ask for them, which is what stage
  2 does on every read. Accepted for this stage, measured (section 7) and recorded
  as a limit. **Call**

Store writes never wait for the file. A snapshot job holds a shared lock on the
file for as long as it runs, and the connection's busy timeout is one second, so a
result write or a deletion slice that waited could hold the workbook lock that
long. They are attempted without waiting and skipped when the file is busy; a
skipped deletion slice leaves its results unserved in memory and its root in the
file. How often they are skipped while tile jobs run is measured.

The scan must not fail on a `source` or `kind` that a direct edit stored as a
BLOB; such a cell is hashed by its bytes and type.

### 5.6 The calculation version

As parent 6.4: one integer in the service, recorded in the store, compared at
open in both directions; a mismatch clears results and precedents and keeps
digests. A test fails when the set of implemented functions changes without the
integer changing.

### 5.7 The dependents index

Loaded from the stored precedents before the first stored result is served, in
the compact integer-keyed form stage 1 probed (45.7 MiB and 0.27 s for 102,003
formulas). Ranges are held in an interval structure whose lookup does not degrade
when many ranges cover a formula column; stage 1's column-and-bisect lookup must
not be carried over, and the 2,000-prefix-sums-over-a-formula-column shape (4.6 s
in stage 1) is a required measurement.

**Call**: the number of formulas with stored precedents is capped at a measured
figure that keeps the index inside a recorded memory allowance. Past the cap
further results are not stored and are evaluated on each read as in stage 2;
nothing already stored is discarded.

## 6. Verification

Written first, per parent section 10, limited to what this stage builds:

- The characterization corpus, unchanged.
- A differential test: for generated workbooks and random sequences of edits,
  Undo, Redo, structural edits and reads, every value served equals a from-scratch
  evaluation of the current inputs.
- Direct-edit tests with an independent SQLite connection, with the service
  running and stopped: change, add and delete cells; delete the only cell of a
  block that a formula elsewhere reads; give a formula with no precedents new text
  (`=1+1` to `=3+4`); rename a sheet to a name a `#REF!` formula refers to and then
  edit the cell it now reads; delete derived rows; store a BLOB as a `source`.
- A workbook prepopulated with an older calculation version's `#NAME?` reopens
  with the new value.
- Crash tests that kill the service during an edit's commit, during a result
  write, during the sliced deletion and during open verification, then reopen and
  compare with a from-scratch evaluation.
- A read-only open and an open of a v8 workbook leave the file's bytes unchanged.
- An external write between a read's evaluation and its result write discards the
  batch.
- A direct edit between a commit's refresh and its write lock empties the store,
  and the dependents of the edited cell are recalculated.
- A calculation job's results are stored when the state is unchanged, and dropped
  when a managed commit or a direct edit lands before the job completes; after a
  tile job, a cached read returns the stored value, and so does one after reopen.
- The ordinary service and desktop suites.

## 7. Measurements owed

On the reference Mac, medians of five, recorded with evidence:

- Digest update for a one-cell edit on a 100-column sheet, stream against sum
  mode, re-deciding parent 6.1 (or bounding blocks by columns).
- The opening benchmark's 270,000-row long-text shape with the digest scan.
- Opening the stage 1 fixture (1,000,000 cells, 100,000 formulas) with a full
  store: scan, index load and total.
- Edit acknowledgement with 1, 1,000 and 100,000 affected formulas.
- First and repeat read of `=SUM` over 100,000 cells, and the same after reopen.
- A read's store write: time added to a read that calculates 1, 256 and 4,096
  formulas, and the digest fill for a range of 100,000 and 1,000,000 cells.
- Index memory and lookup for the stage 1 fixture and for 2,000 prefix sums over a
  formula column.
- File growth from the derived tables on the stage 1 fixture.
- Refresh after a one-cell direct edit at 10,000, 100,000 and 1,000,000 cells, and
  whether the store survived.
- Opening a small workbook at v1 and at v9.
- The share of store writes skipped because the file was busy, with desktop tile
  jobs running.

## 8. Done when

- Reopening a workbook whose formulas were stored serves them without evaluating
  any, and a direct edit made while it was closed changes every dependent value
  and no other.
- Kill-and-reopen never shows a stale value as current.
- Every test in section 6 passes and every measurement in section 7 is recorded,
  with the limits they fix written into the parent design, the
  [native format document](native-format-v1.md) (a v9 section),
  [compatibility](compatibility-v0.md) and the
  [status](implementation-status.md).
- The opening, viewport and edit budgets are re-measured and none is missed that
  is met today; a miss stops the stage and goes to the owner.

## 9. Pull requests

**Call**: three, each leaving `main` working.

1. The store module, v9 schema and documentation, upgrade, the temporary store and
   digests kept current by commits. Nothing is served from it.
2. Verification at open and refresh, the calculation version, and the wide-sheet
   and long-text measurements, with the digest mode re-decided.
3. Serving: the dependents index, results written by reads and by calculation
   jobs, roots and marking, the
   removal of `DependencyIndex`, the remaining measurements and the result
   document.

## 10. Left to the builder

- Table and column names and the value encoding, consistent with the format's
  conventions; whether the temporary store is an attached database.
- The interval structure, the slice sizes, the write allowance and the index cap,
  each fixed by measurement.
- Whether the temporary store's contents are carried into the file at upgrade.
- The task split inside each pull request.
