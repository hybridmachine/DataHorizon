# Python workspace guide

This guide covers writing, approving, running and binding workbook Python from
the desktop application. It describes the local development preview on macOS;
it is not a qualified release. The [workbook automation guide](python-automation-guide.md)
covers the underlying module, macro and approval contracts, and the
[workspace design](python-workspace-design.md) records the decisions behind
this window.

The walkthrough follows the Supplies storyboard: a sheet with a pen count, a
macro that restocks pens, and a rule that refuses negative quantities.

## The Supplies example

`examples/supplies_workspace.py` builds the same workbook through the SDK and
checks it end to end. From the repository root:

```sh
PYTHONPATH=service python3 examples/supplies_workspace.py create /tmp/Supplies.dhwb
PYTHONPATH=service python3 examples/supplies_workspace.py exercise /tmp/Supplies.dhwb
```

`create` writes a Supplies sheet (Item, Quantity, Unit price and Amount for a
Notebook, a Pen and a Folder, with `=B2*C2` and so on in D2:D4 and
`=SUM(D2:D4)` in D6), three names, the module `supplies` and one required rule,
then prints the activation key and declarations. It never runs the code.

`exercise` starts a service it owns with the owner channel, approves the
printed key over that channel, and prints a JSON report: the total goes from
18 to 20.5 after `restock_pens(extra=2)`, Undo returns it to 18, `extra=-10`
rolls back with `Below zero`, and typing -1 into `Supplies!B2` is rejected by
`supplies.check_quantity`.

Open `/tmp/Supplies.dhwb` in the application to follow the same steps by hand.

## Open the workspace

Choose **Tools ▸ Python workspace**. The workspace is a separate window that you
can place beside the sheet. It shares the sheet window's connection, so both
show the same workbook state, Undo history and results. Closing the sheet
window closes its workspace.

| Region | Contents |
| --- | --- |
| Toolbar | Save, Review & approve, Declaration, Names, Pick from sheet |
| Modules | The workbook's modules, with New module…, Rename module…, Delete module… and Discard draft… |
| Editor | One module's source with line numbers, Python colouring and find |
| Side panel | The selected macro's declaration, or the Names panel |
| Bottom tabs | Run, Output and Events |
| Status bar | Draft or saved state on the left, approval state on the right |

A read-only workbook opens the workspace for reading. Controls that cannot work
are disabled and say why.

## Write a module and declare a macro

Choose **New module…** and give it a name, for example `supplies`. Type the
code. A macro is an ordinary top-level function whose first argument is the
context `ctx`:

```python
def restock_pens(ctx, extra):
    quantity = ctx.value("'Supplies'!B3") + extra
    if quantity < 0:
        raise ValueError("Below zero")
    ctx.set("PenQuantity", quantity)
    return ctx.value("Total")
```

A function becomes a macro only when it is declared. Choose **Add declaration…**
in the side panel and enter the function name, each argument's name and type
(Text, Integer, Number, Boolean, Date, Time or Date and time), whether it may be
empty, an optional default, and the result type. If the function does not exist
yet, a matching stub is inserted. The declaration is part of the module and is
saved with it.

The editor offers completion in two places only: workbook names at the start of
a string literal (`ctx.value("To` offers `Total`) and context methods directly
after `ctx.`. Everywhere else Return and Tab keep their meaning; Tab inserts
four spaces. Nothing in the editor imports or runs the code.

### The context

`ref` below is a sheet-qualified address or a workbook name:

| Call | Result |
| --- | --- |
| `ctx.value(ref)` | The calculated value of one cell |
| `ctx.get(ref)` | The stored cell item (`source`, `kind`, `style`) or `None` |
| `ctx.set(ref, source)` | Writes one cell; `source` is text, an integer, a number or a Boolean |
| `ctx.values(ref)` | The calculated values of a rectangle as rows; blank cells are `None`; at most 4,096 cells |

Both `"Supplies!B3"` and the quoted form `"'Supplies'!B3"` work; a sheet name
with spaces or apostrophes needs the quoted form, with an apostrophe doubled.
The older `(sheet, row, col)` forms remain valid. Reads see the operation's own
earlier writes, so `ctx.value("Total")` after `ctx.set` returns the new total.
An address or name that does not resolve, or a range where one cell is needed,
stops the macro even if your code catches the error; the run reports
`MACRO_CONTEXT` with the specific code, for example `REFERENCE_SHAPE`.

Address text inside code is not rewritten when rows, columns or sheets change.
`"'Supplies'!B3"` keeps pointing at B3 after a row is inserted above it. Names
follow those changes, so use a name for anything that must stay attached to its
cells.

## Pick references from the sheet

Put the cursor where the reference belongs and choose **Pick from sheet**. Both
windows show `Picking a reference for Python · Esc to cancel`. Click a cell to
insert it, or drag across a range. Each pick inserts a quoted, sheet-qualified
address such as `"'Supplies'!B3"` or `"'Supplies'!B2:B4"`; if the picked cells
are exactly a defined name, the name is inserted instead. Picking stays on until
you choose Pick from sheet again or press Escape in either window.

Picking never edits the sheet, never opens a cell editor and restores the
previous selection. If a cell edit is in progress, finish or cancel it first.
While picking is on, the main window's commands that could change the workbook
(Edit, Format, Sheet and Data commands, Import, Save view in workbook) are
disabled, along with their shortcuts. Copy, Find, freezing, navigation and
switching sheets still work, and starting a formula reference pick ends
picking for Python.

## Names

A name is a workbook-wide label for one cell or one rectangle on one sheet, for
example `PenQuantity` for `'Supplies'!B3`, `Quantities` for `'Supplies'!B2:B4`
and `Total` for `'Supplies'!D6`.

- Select cells and choose **New name from selection…** in the workspace's Names
  panel or in **Data ▸ Name manager…**. The same places rename, retarget and
  delete names.
- In the Names panel, double-click a name to insert it as a string at the code
  cursor.
- The address box shows the name of an exactly matching selection, and typing a
  name there selects its cells.

Names are case-insensitive, start with a letter or underscore, contain only
letters, digits and underscores, are at most 64 characters, and cannot look like
a cell address, an R1C1-style reference such as `R1C1`, `R2` or `C3`,
`TRUE`/`FALSE` or a function name. A workbook holds at most 256. A workbook
that already stores an R1C1-style name still opens, and the name can be
retargeted, but no new name of that form can be created.

Inserting or deleting rows and columns moves and resizes a name like a range
reference. Renaming a sheet does not affect names. Deleting every cell a name
covers, or its sheet, leaves a broken name; it is marked in the lists and any
use of it fails. Duplicating a sheet does not copy names, and sorting does not
move them. Deleting a name, or breaking it this way, also breaks every rule
scoped to it. Before you delete a name that rules use, the confirmation lists
them: `Rules that use this name will stop covering any cells: rules.check_quantity`.
After the change, Issues shows `Rules that no longer cover any cells: …`.

In this version names work in Python, in rule scopes and in the address box.
They do not work in formulas: `=SUM(Quantities)` is not supported. XLSX and ODS
exports, of a selection or the whole workbook, list names and rules as
native-only omissions in their report; CSV carries neither, and a CSV export's
report lists them the same way.

## Save

Your edits are a draft until you choose **Save**; the status bar shows
`Draft · not saved to the workbook`. Drafts are kept per module while the
application runs, including across a service restart, but not after an
application crash. Closing the workspace or opening another workbook with
unsaved drafts asks you to Save, Discard or Cancel; Save there saves the
selected module and leaves the window open.

Saving stores the module through the same managed operation the CLI and SDK
use. If the stored code changed since you started (for example, someone saved
the same module from another window), a dialog lets you keep your draft or take
the stored version; nothing is overwritten silently. A validation error, such as
a syntax error or a declaration without its function, keeps the draft and shows
the reason.

Saving code, changing a name and changing a rule clear the workbook's Undo
history. While history is not empty, each such change asks for confirmation
until you tick **Don't ask again this session**.

## Review & approve

Saved code does not run until it is approved. Choose **Review & approve**. The
dialog lists the modules and rules that changed since the last approval in this
session and the code fingerprint, and states what approval means:

- Approved code runs with your own account's access to files and the network.
  It is not sandboxed. Workers contain crashes and time limits; they are not a
  security boundary.
- Undo reverses changes the code makes to the workbook. It cannot reverse
  anything outside it, such as files written or messages sent.
- Approval covers this exact code for this service session. It is not stored in
  the workbook. Any change to a module, a declaration, a module name or a rule
  returns the workbook to `Not approved to run` and needs a new approval.
  Restarting the service also clears it.

The dialog does not show the source or a diff; review the code in the editor
before approving. Save all drafts first; Review & approve is disabled while
drafts exist.

Approval travels only over a private channel from the application to the
service it started. No HTTP request can grant it. When the application is
attached to a service someone else started (`--connect`), the dialog shows the
activation key and the service operator's `--approve-code` option instead of an
Approve button.

## Run, cancel and read the output

In the **Run** tab, choose the macro (shown as `module.function`). The tab builds
a field for each declared argument and checks its type before sending. Choose
**Run**. Run needs saved, approved code and no unresolved earlier run.

The **Output** tab states the outcome first: `Committed · returned 20.5 · 1 cells
changed`, `Rolled back`, `Cancelled` or `Timed out`, followed by the result, the
last 8 KiB of printed output, and the traceback. Press Return on, or
double-click, a traceback line in a workbook module to move the editor to that
line. A run is one transaction: if it fails, nothing it wrote is stored.

A committed run refreshes the sheet and adds one Undo entry named after the
macro, for example **Undo Run supplies.restock_pens**. The changed cells are not
highlighted; the Output tab reports how many changed.

**Cancel** stops a running macro by its operation ID. If the application loses
the reply to a run, the outcome is reconciled by operation ID before another run
is allowed.

## Rules (event bindings)

A rule calls a function in a workbook module whenever managed edits change
cells or table records in its scope. Rules are stored in the workbook and are
part of the code fingerprint, so adding, removing or retargeting one needs a
fresh approval.

Write the function with two arguments, the context and the list of changes:

```python
def check_quantity(ctx, changes):
    for change in changes:
        if change["after"] is None:
            continue
        value = ctx.value(change["sheet"], change["row"], change["col"])
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError("Below zero")
```

Each cell change is a dictionary with `sheet` (the sheet's stable identity, not
its display name), `row`, `col`, `before`, `after` and `action`, the operation that produced it
(for example `edit`, `paste`, `fill`, `macro_run`, `undo` or `redo`). `before`
and `after` are stored cell items with `source`, `kind` and `style`, or `None`
when the cell is empty. Read calculated values with `ctx.value`. Table changes
carry `table`, `row_id`, `before`, `after` and `action`.

In the **Events** tab, choose the event (`cells change` or `table records
change`), type or pick the scope, choose the function, leave **Required**
ticked, and choose **Add rule**. A cell scope is a sheet, an address range or a
name; a table scope is a table. Then approve the code again. To change a rule,
remove it and add the new one.

How rules run:

- A rule runs for every managed edit in its scope: typing in the grid, paste,
  fill, CSV import, macros, Undo and Redo, and the same operations through REST,
  the CLI and the SDK. It runs inside the edit's transaction, after any external
  `--handler`. Several rules run in catalog order (by their stored identity),
  not an order you choose. Edits it makes join the same
  transaction and Undo entry.
- If the function raises or exceeds its two-second budget, the whole edit is
  rejected and nothing is stored. The grid shows
  `Rejected by supplies.check_quantity: ValueError: Below zero · nothing stored`
  and keeps your typed value in the editor for correction.
- **Required** governs code that cannot run. A required rule whose code is not
  approved, or whose module fails to load, blocks every edit in its scope with a
  reason naming it. A rule that is not required is skipped instead, and the
  skip is reported in Issues.
- Code, name and rule changes stay allowed while edits are blocked, so the
  workbook can be repaired and approved.

What rules do not cover:

- Inserting or deleting rows and columns, and renaming, deleting or duplicating
  sheets, do not call cell-change rules for the cells those actions move or
  rewrite. A scope still moves with its cells. Edits that a configured external
  handler makes during those actions do reach rules.
- Direct SQLite edits run no Python and are never replayed later.
- A rule whose scope cells, name or table no longer exist is marked broken and
  covers nothing until it is repaired; edits it used to check then commit
  without it. Deleting the scope's name or dropping its table reports the rule
  in Issues (`Rules that no longer cover any cells: …`); dropping a table does
  not list the rules in its confirmation.

## SDK and CLI equivalents

| Workspace action | SDK (`WorkbookClient`) | CLI (`python3 -m datahorizon.cli <workbook> …`) |
| --- | --- | --- |
| Read code, rules and approval | `book.code()` | `code read`, `bindings read` |
| Save a module | `book.save_module(name, source, macros, fingerprint=…, module=…)` | `code save` (JSON on stdin) |
| Names | `book.names()`, `book.save_name(name, reference, name_id=…)`, `book.delete_name(name_id)` | `names read`, `names save` (JSON on stdin), `names delete <id>` |
| Rules | `book.save_binding(event, scope, module, function, fingerprint=…, required=True)`, `book.delete_binding(binding_id, fingerprint=…)` | `bindings save` (JSON on stdin), `bindings delete <id> --fingerprint …` |
| Approve | `LocalService(owner_channel=True).approve(activation_key)` | Start the service with `--approve-code` |
| Run | `book.run_macro(module, macro, fingerprint=…, arguments=…, operation_id=…)` | `macro <module> <macro>` (arguments as JSON on stdin) |
| Cancel | `book.cancel_operation(operation_id)` | `cancel-operation <id> --handle … --url …` |

Rule events are `cell_change` and `table_change`. A scope is
`{"kind": "sheet", "sheet": …}`, `{"kind": "range", "reference": …}`,
`{"kind": "name", "name": …}` or `{"kind": "table", "table": …}`. REST exposes
names at `POST /v1/workbooks/{id}/names` and the operations `name_save`,
`name_delete`, `binding_save` and `binding_delete`, with OpenAPI entries.
Rejections carry `BINDING_FAILED`, `BINDING_TIMEOUT`, `BINDING_NOT_APPROVED` or
`BINDING_UNAVAILABLE` with a `binding` object naming the rule; skipped optional
rules appear in the receipt's `binding_notices`.

## Limits

| Item | Limit |
| --- | --- |
| Names per workbook | 256, each at most 64 characters |
| Rules per workbook | 16 |
| `ctx.values` | 4,096 cells |
| Modules | 16 per workbook, 128 KiB of source each |
| Worker time | Two seconds per macro or rule round, inside a five-second operation |
| Edited fields per operation | 10,000 |
| Printed output and tracebacks | Last 8 KiB each |
| Hook rounds per operation | 16 |

## Known limits in this version

- Names cannot be used in formulas, and XLSX/ODS files neither import nor export
  them.
- No custom worksheet functions in Python, no package installation and no
  imports between workbook modules.
- Rules respond to cell and table changes only; there are no workbook-open,
  structure or code events, and you cannot choose the order in which several
  rules run.
- The approval dialog does not show source or a diff.
- Changed cells are not highlighted after a run.
- The workspace is a separate window; it cannot be docked.
- Drafts are lost if the application crashes.
- Physical-input, VoiceOver and Windows/Linux acceptance have not been done.
