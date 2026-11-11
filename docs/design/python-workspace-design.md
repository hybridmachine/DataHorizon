# Python workspace design

Draft 0.2 | November 10, 2026 | Status: approved by the owner on November 10, 2026

The [implementation plan](python-workspace-plan.md) turns this design into tasks.

This document describes the next milestone: writing, approving, running and
binding workbook Python from the desktop application. It follows the
[storyboard](../../output/pptx/datahorizon-python-workspace-storyboard.pptx)
the owner accepted on November 10, 2026, and builds on the
[workbook Python automation batch](python-automation-plan.md). It is a design to
plan from. It is not an implementation plan and claims no completed work.

Each decision below is marked **Owner** (stated by the owner) or **Call** (chosen
by the author of this document and open to reversal). No owner decisions remain
open.

## 1. Intent

Python is DataHorizon's macro language, but a macro can currently be written,
approved and run only through the CLI or SDK. Someone who works only in the
spreadsheet cannot reach the product's main programmability feature. This
milestone lets that person stay in the application: open a code window beside
the grid, point at cells instead of typing addresses, name the cells that
matter, run a macro and see the sheet change, and attach a rule to edits.

Success is the storyboard session completed in the installed macOS application
without a terminal: Maya writes `restock_pens`, approves it, runs it, undoes it,
sees a failure leave the sheet untouched, and binds `check_quantity` so a
negative quantity typed into the grid is rejected.

## 2. Goals, non-goals and anti-goals

**Goals**

- A Python workspace window that sits beside the spreadsheet window. **Owner**
- Clicking a cell or dragging a range inserts its reference at the code cursor. **Owner**
- A name manager for cells and ranges, usable from the sheet and from code. **Owner**
- Module editing, macro declaration, save, approval, run, cancel and diagnostics
  in the GUI, using the existing managed operations.
- Event bindings stored in the workbook that run workbook functions on managed
  edits, through every managed path.
- REST, CLI and SDK parity for every new engine operation, with OpenAPI entries.

**Non-goals for this milestone**

- Names inside formulas, and names in XLSX/ODS import or export. **Owner**
- Custom worksheet functions written in Python, package installation, imports
  between workbook modules, a debugger, breakpoints or a language server.
- Docking the workspace into the main window. **Call**
- Writing a whole range in one context call. **Call**
- Durable external-effect jobs, multi-transaction macros and remote access.
- Windows/Linux qualification, signing and the other open release gates.

**Anti-goals: these would make the milestone a failure even if it works**

- Any path where the GUI runs Python that the operator did not approve, or where
  the bearer token alone can grant approval.
- A binding that is silently skipped, letting an edit bypass a rule the workbook
  author marked required.
- The desktop reading or writing SQLite directly, or the editor holding state
  that the CLI and SDK cannot reach through the same operations.
- Picking a reference that changes the sheet's stored data or leaves it in edit
  mode.
- Built-in business behavior. Rules remain the author's Python.

## 3. Constraints carried from existing contracts

- The GUI uses REST only. New behavior lives in the shared engine.
- Stored code is inert. Opening, copying, exporting and inspecting never import
  or run modules.
- Approval is for the exact activation key `<workbook UUID>:<code fingerprint>`
  and lives in the service process, never in the workbook file.
- A macro is one managed operation: one transaction, one durable receipt, one
  Undo/Redo entry, existing budgets (two-second worker, five-second operation,
  10,000 fields, 8 KiB logs and tracebacks).
- Module save and delete require the current fingerprint and workbook state and
  clear session history.
- Direct SQLite edits run no Python and are never replayed later.
- Workers contain crashes and timeouts. They are not a sandbox.

## 4. The workspace window

**Owner:** a separate window that can be placed side by side with the sheet.

**Call:** one workspace window per open workbook window, opened from
Tools ▸ Python workspace and closed independently. It is a top-level Qt window
that shares the main window's REST session and workbook handle, so both see the
same state, history and receipts. Closing the workbook window closes its
workspace after resolving drafts. Window geometry is a local preference.

Layout, as storyboarded:

| Region | Contents |
| --- | --- |
| Toolbar | Save, Review & approve, Run, Pick from sheet |
| Modules list | The workbook's modules; add, rename, delete |
| Editor | One module's source with line numbers |
| Side panel | Switches between the selected macro's declaration and Names |
| Bottom panel | Run, Output and Events tabs |
| Status bar | Draft/saved state and approval state, shown separately |

A read-only workbook opens the window for reading. On an attached service the
workspace works fully, including saving and running code the operator approved
at start-up; only granting approval is unavailable (section 8). Controls that
cannot work are disabled with the reason shown, not hidden. **Call**

## 5. Editing, drafts and saving

**Call:** the editor is a plain text editor with Python syntax colouring, line
numbers, four-space indentation, find, and completion limited to defined names
and the context methods. Nothing in the editor imports or executes code.

**Call:** a macro declaration is edited in a form (name, typed arguments with
nullability and defaults, result type), not inferred from source. Adding a
declaration inserts a matching function stub. Static validation errors from the
service are shown against the module.

**Call:** editor contents are a local draft until Save. A draft is kept in
memory per module, survives a service restart, and is not persisted across an
application crash in this milestone. Closing with drafts asks Save, Discard or
Cancel, matching cell-draft behavior.

Save uses the existing module save operation with the reviewed fingerprint and
state. A stale fingerprint or state rejects, keeps the draft, and shows what
changed. Because a code save clears session history, the first save while
history is not empty asks for confirmation, with an option not to ask again in
that session. **Call**

## 6. Picking references from the sheet

**Owner:** clicking cells places the reference in the Python text.

**Call:** picking is an explicit mode. Pick from sheet in the workspace toolbar
(or a held modifier key, to be chosen with platform conventions) arms it. Both
windows show that picking is active. A click inserts one cell; a drag inserts a
range; Escape or a second press of the button ends the mode. If the picked cells
exactly match a defined name, the name is inserted instead.

While picking, the sheet does not enter edit mode, emits no edit operation and
restores its previous selection afterward. An active in-cell draft in the sheet
must be resolved first.

**Call: reference text.** The inserted text is a quoted address that always
includes the sheet name, for example `"Supplies!B3"` or `"Supplies!B2:B4"`,
using the formula engine's existing quoting rules for sheet names.

**Call: context methods.** The broker context gains an address form alongside
the existing `(sheet, row, col)` form, which stays valid:

- `ctx.get(ref)`, `ctx.value(ref)` and `ctx.set(ref, source, kind="auto")`
  accept one cell given as an address or a name.
- `ctx.set` accepts a Python number or Boolean as well as source text and
  stores its canonical source, so `ctx.set("PenQuantity", qty + 2)` works. Today
  the source must be text.
- `ctx.values(ref)` is new. It reads a range given as an address or a name and
  returns rows of values, capped at the managed range read limit of 4,096
  cells. The context has no range read today.
- An address or name that does not resolve to the required shape aborts the
  operation with a specific error, even if Python catches it, as other broker
  validation errors do.

Address text inside code is not rewritten when rows, columns or sheets change.
The picker and the documentation say so, and point to names as the way to keep
code stable.

## 7. Names

**Owner:** a name manager so named cells and ranges are easy to reference.

Names do not exist in the engine today, so this is a new native feature.

**Call: model.** A name is workbook-scoped, has a stable identity, a
case-insensitive unique identifier of at most 64 characters that cannot be
mistaken for a cell address or a function, and a target of one rectangle on one
sheet, held by sheet identity rather than sheet name. Names are stored in the
native catalog under a new format version, with an in-transaction upgrade on
first use and the usual rejection by older readers. Limit: 256 names.

**Call: operations.** Create, rename, retarget and delete are managed
operations with expected state and operation IDs, available through REST, CLI
and SDK. They clear session history with confirmation, as other structural
changes do. A configured external handler must opt in to a name structure event
before these operations are allowed, following the existing pattern for code and
table structure.

**Call: structural behavior.** Row and column insertion and deletion adjust a
name's rectangle by the same rules as range references, including expansion and
contraction. Deleting every targeted cell, or the sheet, leaves a broken name
that is reported with the other broken references and resolves to an error.
Sheet rename does not affect names. Duplicating a sheet does not copy names.
Sorting does not move names.

**Call: where names work in this milestone.**

- The address box shows the name of an exactly matching selection and accepts a
  name to jump to it.
- The Names panel in the workspace and a Name Manager dialog in the main window
  list, create from selection, rename, retarget and delete.
- The Python context resolves names wherever it accepts an address.
- Event binding scopes accept names.

**Owner: names in formulas and in Excel/ODS files are a later batch.** Decided
November 10, 2026. In this milestone formulas do not resolve names, and XLSX/ODS
export reports names as a native-only omission. Supporting `=SUM(Quantities)`
affects the formula parser, dependency cache, structural transforms and
interchange, and is planned separately.

## 8. Approval from the GUI

Today approval is fixed when the service starts, and REST deliberately has no
way to grant it.

**Owner: the GUI grants approval over a private channel.** Decided November 10,
2026. This changes the activation contract as follows.

- A service that the desktop started accepts approval only over a private
  channel to its parent process (its standard input), never over HTTP. The
  bearer token cannot grant approval. The process-local allowlist changes from
  fixed-at-start to append-only for the life of the process.
- Review & approve shows the modules changed since the last approval in this
  session, the fingerprint, and a plain statement that approved code runs with
  the user's own access and that Undo cannot reverse effects outside the
  workbook. Approve applies to the exact stored code for this service session.
- For a service the desktop merely attached to, the dialog shows the activation
  key and the operator's start-up option, and cannot approve.

Each open workbook currently copies the allowlist when it opens, and the list
is capped at 256 keys. A grant must reach workbooks that are
already open, and repeated save-and-approve cycles must not exhaust the cap, for
example by replacing the previous grant for the same workbook.

The alternative considered and rejected was restarting the owned service with
the key on its command line. It keeps the allowlist immutable but drops session
history and open handles on every approval.

Approval requires saved code. Any later code, declaration, name of module or
binding change alters the fingerprint and returns the workbook to "not
approved".

## 9. Running and diagnostics

The Run tab builds an argument form from the selected macro's declaration and
checks types before sending. Run needs saved, approved code and no unresolved
unknown outcome. It submits the existing macro operation with a retained
operation ID. Cancel uses the existing cancellation route on the same handle.

The Output tab states the outcome first (committed, rolled back, cancelled,
timed out or unknown), then the scalar result, the bounded log tail and
traceback. Selecting a traceback frame in a workbook module moves the editor to
that line. An unknown outcome is reconciled by operation ID and blocks further
runs until resolved, as in the SDK.

After a committed run the sheet window refreshes through its normal path, and
Undo names the macro. **Call:** the receipt holds only counts of changed cells
and records, so the Output tab reports the count and the storyboard's cell
highlight is left out of this milestone.

## 10. Event bindings

A binding connects a workbook function to managed edits. It replaces the need
for a separate handler file in ordinary use; the external `--handler` file
remains supported.

**Call: model.** A binding has an event, a scope, a module and top-level
function, and a required flag (default on). Bindings are stored in the native
catalog. Limit: 16 bindings.

**Call: fingerprint.** The code fingerprint covers each binding's event,
function, required flag and the identity of its scope (the sheet, table, name
or stored range it points at), so adding, removing or redirecting a binding
needs fresh approval. It does not cover where that scope currently sits on the
sheet. A workbook with no bindings keeps exactly today's fingerprint, so
existing approvals stay valid.

**Call: scopes move with the sheet.** An address-range scope is stored by sheet
identity and rectangle and adjusts on row and column changes by the same rules
as a name. A name scope follows the name, including when the name is
retargeted. Neither movement changes the fingerprint or needs re-approval. A
scope whose cells or name no longer exist is reported in Issues as a broken
binding and covers nothing until repaired.

**Call: first events.** Cell change, scoped to a sheet, an address range or a
name; and table change, scoped to a table. Workbook open, structure and code
events are left for a later batch.

**Call: invocation.** The function is called as `function(ctx, changes)` with
the same change records an external handler receives, filtered to the scope,
inside the parent transaction, in the existing bounded rounds. Cell change
records stay dictionaries of sheet, row, column and before/after cell items
(source, kind and style, or nothing for an absent cell); they gain an action
field so Undo and Redo can be told apart, as table records
already allow. A rule that needs a calculated value reads it with `ctx.value`.
The storyboard's `change.new` is illustrative; the guide's example will use the
real record shape.

Order is the external handler first, then bindings in stored order. The engine
currently skips the hook rounds when no external handler is configured; that
changes so bindings run without one. All bindings in a round share one worker
and the existing two-second round budget inside the five-second operation; a
timeout names the binding that was running. Binding-generated edits join the
transaction and the same Undo entry. Bindings run for every managed operation
that produces change records in scope: GUI, REST, CLI and SDK edits, paste,
fill, import, macros, and Undo/Redo. They never run for direct SQLite edits and
are never replayed.

**Call: structure.** Structure events are not in this first tier. Inserting or
deleting rows and columns stays allowed and moves scopes with the cells, but
does not call cell-change bindings. The guide states this limit.

**Call: unapproved or failing bindings.** A binding that runs and raises or
times out always rejects the whole operation, required or not; nothing is
stored. The required flag governs only code that cannot run because it is
unapproved or fails to load: a required binding then rejects every operation
that would deliver changes in its scope, with a visible reason naming the
binding, while a binding not marked required is skipped with a persistent notice
in Issues for that session. A freshly opened workbook with required bindings
therefore reports that edits in scope are blocked until its code is approved.
Code, name and binding operations stay allowed so the author can repair and
approve the workbook.

The Events tab lists bindings and edits them with the same pick-or-name scope
entry as the editor. A rejected edit in the grid keeps its draft for correction
and shows the reason, as existing rule rejections do.

## 11. Delivery order

The 16 open pull requests for the relational and automation batches (115–130)
land on main first; this milestone builds on them.

| Increment | Scope |
| --- | --- |
| 1 | Contracts: update this design's decisions into the contract documents; names and bindings format version |
| 2 | Names in the engine, REST, CLI, SDK; structural adjustment; address box and Name Manager |
| 3 | Address and name forms in the broker context |
| 4 | Workspace window: modules, editor, declarations, drafts, save |
| 5 | Pick from sheet and the Names panel |
| 6 | Approval from the GUI |
| 7 | Run, cancel, output and traceback navigation |
| 8 | Event bindings in the engine, REST, CLI, SDK |
| 9 | Events tab and grid rejection display |
| 10 | Integrated walkthrough, guide, evidence and status updates |

Each increment delivers its engine operation and its interface together and is
verified before the next, as in earlier batches.

## 12. Verification expectations

- Engine: name and binding migration and rollback; structural adjustment
  against independently computed expectations; fingerprint changes; scope
  filtering; required and optional binding failure; Undo/Redo; no replay after
  direct edits; limits.
- Managed-path parity: the same name, reference and binding behavior through
  REST, CLI, SDK and the GUI, over real HTTP.
- Approval: no grant through HTTP or request fields; attached services cannot be
  approved from the GUI; changed code is refused.
- Qt: picking leaves stored data and selection unchanged; drafts survive
  rejection and restart; stale saves keep the draft; traceback navigation;
  rejected grid edits keep their draft.
- Installed macOS application: the storyboard session end to end without a
  terminal, on the bundled runtime.

## 13. Left to the builder

Class and file structure; the exact REST resource shapes and error codes,
following existing conventions; the syntax-colouring implementation; the
modifier key for picking; panel sizing; how the workspace shares the REST
client; test fixture layout; and the split of increments into pull requests.
