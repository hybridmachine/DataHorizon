# M3–M5 completion increments

Authorized November 8, 2026 after PR #94. Execute the ten tasks in order, with
one locally built/tested, committed/pushed, published-head reviewed and merged PR
per task. Review fixes require relevant tests again before merge. Hosted Actions
credits are unavailable. Final platform/manual evidence determines milestone
closure; this batch does not waive M0–M8 completion criteria.

| Task | Deliverable and acceptance |
| --- | --- |
| 1 | Contracts and executable baseline conformance matrix; distinguish current behavior from the following implementation work. |
| 2 | Bounded process-local dependency index and reusable nonvolatile results; transitive invalidation includes ranges, cross-sheet and hook-generated edits. Structure/external refresh invalidate conservatively; reopening has no trusted derived cache. |
| 3 | State/generation-bound background calculations with pending/current/error/stale/cancelled outcomes and one UTC clock; late results cannot become current. Hooks still request synchronous transactional values. Shared REST/SDK/CLI and desktop controls expose the work. |
| 4 | Explicit opt-in transactional structural events and reference fixtures. Before/after events have deterministic ordering and bounded broker access; rejection rolls back catalog, cell and generated edits together. Legacy handlers without the new event fail closed. |
| 5 | Versioned portable per-sheet view settings and named list definitions, managed operations, native reopen/copy and client use. Read-only local presentation remains possible. |
| 6 | Persistent identities for explicitly declared list records, separate from A1/display positions. Managed row structure maintains identities; external edits mark identity state unverified and require explicit reconciliation without replaying hooks. |
| 7 | Reviewed record-target bulk edits, visible formula-aware copy/export and one-operation Undo. Hidden records are not implicit targets; changing filter results does not retarget an admitted operation. Unsupported reference mappings reject. |
| 8 | Explicit locale number/date conversion profiles and precision/ambiguity reviews, plus bounded wider ISO/temporal import. No host-locale guessing or silent identifier conversion. |
| 9 | Reviewed large-operation jobs with bounded staging, progress, cancellation and durable operation outcomes. Atomic mode remains one transaction; explicit batches report each committed operation and resumable position. |
| 10 | Full local contract/native/installed qualification, larger-than-memory and calculation/job measurements, accessibility inspection, evidence and an honest milestone ledger. Windows/Linux/manual gates require their own evidence. |

## Shared acceptance contracts

Input source/kind, canonical value, display and record identity are distinct.
The existing scalar/coercion and Gregorian civil-date tiers remain authoritative;
broader Excel coercion is not inferred. Baseline examples are executable in
`tests/fixtures/m3_m5_conformance.json`. Formula source is retained after errors.
Supported structural transformations preserve intended references; unsupported
syntax and partially representable selections reject before any mutation.

Calculation results belong to both an input-state token and a calculation
generation/UTC clock. A stored-input state alone cannot validate volatile results.
Dependency caches are disposable, bounded and never authoritative native inputs.
Direct SQLite changes invalidate derived state without historical handler replay.
Transactional hook calculations use current uncommitted inputs and never publish
their results into a shared read cache before commit.

Structural handlers are generic programming hooks, not business workflows. A
legacy `on_change` file alone does not authorize structural edits. An explicit
`on_structure(context, event)` hook opts into the new contract; before and after
events surround the structural change, followed by coalesced change rounds.
Event snapshots use identities and old/new catalog facts. External side effects
remain outside database rollback guarantees.

Portable presentation changes are state-checked managed operations but do not
masquerade as input-change events. Their undo capability is explicit. Named list
definitions describe bounded projections, not arbitrary managed SQL or relational
views. Record IDs belong to declared lists; A1 references remain positional.
Sort/filter changes presentation only. List declarations are explicit and bounded.

Filtered edits bind a reviewed ordered list of record IDs, source columns and
state. Full/values/formats paste, fill and clear use one ordinary operation and
existing history/hook budgets. Formula copy uses an explicit source-coordinate
reference policy; projection exports must preserve referents or reject the mapping.

Locale conversion is an explicit import/entry choice, not a change to invariant
REST numbers or existing Text. Conversion failures retain locations and block Apply.
Native civil values carry no implicit host timezone. Interchange reports every
supported normalization and unsupported duration/offset/precision boundary.

Cancellation is cooperative until a declared commit boundary. Before commit,
confirmed cancellation rolls back the parent transaction. After commit it reports
committed, never cancelled-away. Atomic and explicitly batched jobs have distinct
contracts; batches expose operation IDs, receipts and committed progress. No
automatic retry of unknown writes or automatic replay of user code.

Task 3 exposes POST `/calculation/jobs` and GET/cancel/release resources under
an open workbook. Read-only snapshots are bounded by 10,000 cells/4 MiB and a
two-second evaluation, carry state/generation/clock, and withhold stale results.
Job storage is ephemeral and bounded; cancelling discards results, never inputs.
REST/SDK cached reads return pending for uncached formulas; fresh reads retain
synchronous behavior. `calculate` in the managed CLI waits for a terminal snapshot.
Data → Recalculate selection runs with a modeless cancel/progress control and
reports its original snapshot without replacing newer grid values or drafts.
Automatic continuous viewport recalculation is outside this first explicit-job tier.

Task 5 lazily upgrades to native v3 with a reserved metadata table. Portable
presentation supports current bounded dimensions/hiding/freezing and up to 16 named
list definitions; standalone named criteria use a separate namespace. State-checked
metadata_set/delete operations have durable receipts and do not emit input events.
Metadata changes are not cell Undo entries; local read-only preferences remain
available. View → Save/Load view in workbook explicitly transfers between local
preferences and native storage. Native copies and sheet duplication retain the
portable tier; unsupported full SQL/query views remain M8 work.

Task 6 stores persistent UUIDs per explicitly declared source row in native v3;
A1 coordinates and display order remain separate. Up to 16 lists and 10,000 total
declared cells are supported. Managed edits refresh provenance; row/column structure
updates extents and identities in the same transaction. Deleting an entire scope
forgets its declaration; partial header deletion or deletion of a criteria column
rejects until explicitly redeclared. Sorting/filtering never renumbers identities.
Input fingerprints detect out-of-band scope changes across reopen; live external
refresh conservatively distrusts declarations. Explicit retain_positions/regenerate
reconciliation reviews current state and never replays skipped handlers. REST/SDK,
CLI apply and Data → Declare selected list records share the engine operations.

Task 7 adds list_edit with reviewed unique record IDs and source columns. Full,
values, formats, repeated-row fill and clear operations share ordinary atomic
history/hooks; filters are not reapplied midway through a write. Copy carries
per-cell source coordinates so relative formulas translate from their original
positions, including paste into an ordinary sheet. XLSX/ODS projection export
rewrites included referents into compressed coordinates and rejects excluded or
reordered range referents instead of changing their meaning; values export is the
explicit fallback. Native IDs remain in the native workbook. Data → Work with
declared records provides reviewed operations, copy, fill, export and reconciliation.

Task 8 adds deterministic invariant/en_US/de_DE/fr_FR conversion profiles,
strict grouping, explicit date order and reported ambiguity. Default Text and
invariant native entry are unchanged. CSV review offers locale date/datetime and
UTC timestamp columns; malformed/unsafe conversions withhold Apply. A read-only
conversion endpoint, SDK set_localized, managed CLI --locale/--timezone-policy and
Data → Enter localized value all use the same converter before ordinary edits.
ISO import accepts omitted seconds and a space datetime separator. XLSX/ODS
explicit timezone_policy=utc normalizes offset timestamps, reporting that the
original offset is omitted; default offset handling still rejects. Native values
remain civil ISO, with no named-zone/DST inference or host-locale dependency.

## Evidence ledger

Task 4 opts in through a top-level `on_structure(context,event)` function in the
explicitly activated trusted handler file (source preflight capped at 1 MiB).
Before/after events include bounded old/new catalogs (up to 256 sheets), action
and structural arguments. The same transaction broker and one parent clock serve
structural hooks and final coalesced `on_change` rounds. Files using cell changes
must continue to implement `on_change`; no implicit fallback bypasses a required
handler. Sheet add/rename/duplicate/reorder/delete, row/column changes and whole
workbook import use the event contract. Existing reference-transform limits remain
explicit; generic user code chooses business behavior. Structure still clears
session history on commit. Failed events retain the original catalog, references,
inputs, history and derived caches, with a durable rolled-back receipt.

Task 2 implements a process-local index capped at 4,096 nodes/50,000 dependency
edges and 4 MiB of estimated cached-result payload. Both branches of parsed
conditionals are indexed conservatively; unresolved/unrepresentable dependencies
disable reuse rather than changing lazy evaluation. Capacity exhaustion discards
the index and leaves ordinary bounded evaluation available. Volatile dependencies
are transitive; hook reads use independent transactional calculators. Committed
cell changes invalidate their dependent closure; structure/external refresh clears
the index. No native derived cache is trusted after reopen.

Each PR records local build/test commands and reviewed head in its description.
Final consolidated evidence lives under `docs/design/evidence/`. The ten tasks
target the documented remaining engineering gaps; full Excel compatibility,
native accessibility and cross-platform release acceptance are evidence-based
gates rather than assumed outcomes.
