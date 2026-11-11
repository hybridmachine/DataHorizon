# Python Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a spreadsheet user write, approve, run and bind workbook Python from the desktop application, with click-to-insert cell references and a name manager.

**Architecture:** New engine features (names, address forms on the code context, event bindings, an owner-only approval channel) land first in the shared Python engine with REST, CLI and SDK parity. The Qt desktop then adds a separate top-level `PythonWorkspace` window that uses only REST and routes every write through `MainWindow::submit`. Native format moves to v6 for the two new catalog kinds.

**Tech Stack:** Python 3.11+ standard library service (`service/datahorizon`), SQLite, Qt 6 Widgets/C++17 (`desktop/`), `unittest`, Qt Test, CMake/CTest.

**Spec:** [python-workspace-design.md](python-workspace-design.md). Executors read both. The accepted storyboard is `output/pptx/datahorizon-python-workspace-storyboard.pptx`.

**Precondition:** pull requests 115–130 are merged to `main`. Branch from that `main`. Line numbers below refer to commit `c9a3b7d`; re-locate by symbol name if they have moved.

## Global Constraints

- The GUI never touches SQLite. Every new behavior is an engine operation reachable through REST, CLI and SDK, with an OpenAPI schema and an executed example.
- Stored code and bindings are inert: open, read, copy, export and inspect never import or run them.
- Approval is the exact key `<workbook UUID>:<code fingerprint>`, held in the service process only. No HTTP route or request field may grant it. The bearer token alone never grants it.
- A workbook with no bindings keeps today's fingerprint byte for byte.
- Limits: 256 names; 16 bindings; `ctx.values` at most 4,096 cells; name identifiers at most 64 characters; existing budgets unchanged (2 s worker round, 5 s operation, 10,000 edited fields, 8 KiB logs and tracebacks, 16 hook rounds).
- Names in formulas and names in XLSX/ODS are out of scope. Export reports names and bindings as native-only omissions.
- Direct SQLite edits run no Python and are never replayed.
- Every source file starts with the SPDX line `GPL-3.0-only`. Qt widgets get `objectName`s so tests can find them. User-visible strings use `tr()`.
- Errors use `require(condition, CODE, message, status)` from `engine.py`; failures after admission keep a durable rollback receipt.
- Commands, from the repository root:
  - One Python file: `PYTHONPATH=service python3 -m unittest discover -s tests -p <file> -v`
  - Python suite: `PYTHONPATH=service python3 -m unittest discover -s tests -v`
  - Build: `cmake --build build --parallel`
  - One Qt slot: `QT_QPA_PLATFORM=offscreen DATAHORIZON_VIEW_STATE=$PWD/build/test-views.ini ./build/python_workspace_tests <slot>`
  - Everything: `ctest --test-dir build --output-on-failure`
- One branch and pull request per task group: A (tasks 1–4), B (5), C (6–8), D (9), E (10–12), F (13–15), G (16). Record evidence as `docs/design/evidence/python-workspace-task<N>.json` using the keys of `python-automation-task6.json`.

## Review Focus

Inputs the design implies but that are easy to miss. Each has a test in the task that owns the code.

1. **Sheet names with spaces, apostrophes or non-ASCII letters** in a picked or typed reference (`'Bob''s Sheet'!B3`): the reference resolves to the same cell it was picked from. Tests in tasks 5 and 12.
2. **A name that could be read as something else** (`a1`, `XFD10`, `sum`, `TRUE`, `R1C1`-like text): rejected at creation with `NAME_INVALID`, in any letter case. Test in task 2.
3. **Code changed outside the application between review and approval** (a direct SQLite edit to a module): Approve refuses because the fingerprint no longer matches what was shown. Tests in tasks 9 and 13.
4. **Undo or Redo touching cells in a required binding's scope after approval was lost** (service restarted, or code edited): the Undo is rejected with the binding named, and the history entry is still there afterward. Test in task 7.
5. **The service restarting while the workspace holds unsaved drafts**: drafts stay in the editor, status returns to "not approved", and Run stays disabled until approval is granted again. Test in task 10.

---

## File structure

| File | Responsibility |
| --- | --- |
| `service/datahorizon/names.py` (new) | Name catalog objects: validate, read, mutate, resolve, structural adjustment |
| `service/datahorizon/references.py` (new) | Resolve an address string or name to sheet and rectangle for the code context |
| `service/datahorizon/bindings.py` (new) | Binding catalog objects: validate, read, mutate, scope filtering, structural adjustment |
| `service/datahorizon/names_api.py` (new) | OpenAPI schemas and action declarations for names |
| `service/datahorizon/code_api.py` | Add binding actions and catalog fields |
| `service/datahorizon/code_approval.py` | `ApprovalStore`, the mutable process-local grant set |
| `service/datahorizon/workbook_code.py` | Fingerprint includes bindings when present; version bump becomes a maximum |
| `service/datahorizon/hooks.py`, `macro_runtime.py` | Context address forms, `values`, binding worker |
| `service/datahorizon/engine.py`, `relational.py`, `formula.py`, `service.py`, `client.py`, `cli.py` | Wiring for the above |
| `desktop/pythonworkspace.h/.cpp` (new) | The workspace window: layout, state, REST calls, drafts |
| `desktop/codeeditor.h` (new) | `QPlainTextEdit` with line numbers, indentation and line marking |
| `desktop/pythonhighlighter.h` (new) | `QSyntaxHighlighter` for Python |
| `desktop/macrodeclarationpanel.h` (new) | Declaration form for one macro |
| `desktop/namemanager.h` (new) | `NamesPanel` widget and `NameManagerDialog` |
| `desktop/codeapprovaldialog.h` (new) | Review-and-approve dialog |
| `desktop/mainwindow.h/.cpp` | Tools menu, workspace ownership, external pick target, name display and jump, owner channel |
| `tests/test_names.py`, `test_names_clients.py`, `test_context_references.py`, `test_bindings.py`, `test_binding_execution.py`, `test_binding_clients.py`, `test_owner_channel.py` (new) | Python tests |
| `tests/test_python_workspace.cpp` (new) | Qt tests, built as `python_workspace_tests` |

---

## Group A — Names

### Task 1: Native format v6 gate

**Files:**
- Modify: `service/datahorizon/engine.py:393`, `service/datahorizon/service.py:35`, `service/datahorizon/relational.py:123-146`, `service/datahorizon/workbook_code.py:126`
- Modify: `docs/design/native-format-v1.md` (add a v6 section naming the two kinds)
- Test: `tests/test_format_v6.py`

**Interfaces:**
- Produces: `relational.ensure_version(book, minimum: int) -> None`, which runs `relational.ensure(book)` then `UPDATE dh_meta SET format_version=MAX(format_version, ?)`. Catalog kinds `'name'` and `'binding'` are accepted only when `format_version >= 6`.

- [ ] **Step 1: Write the failing tests**

```python
def test_module_save_does_not_downgrade_v6(self):
    book = new_book(self)                      # Workbook(path, create=True)
    book.db.execute('UPDATE dh_meta SET format_version=6'); book.db.commit()
    save_module(book, 'm', 'def f(ctx):\n    return 1\n')
    self.assertEqual(format_version(book), 6)

def test_v7_is_rejected_on_open(self):
    ...set format_version=7 with sqlite3, then:
    with self.assertRaises(EngineError) as raised: Workbook(path)
    self.assertEqual(raised.exception.code, 'FORMAT_VERSION')

def test_name_kind_rejected_below_v6(self):
    ...insert a dh_relational row kind='name' into a v5 file with sqlite3
    with self.assertRaises(EngineError) as raised: Workbook(path)
    self.assertEqual(raised.exception.code, 'INVALID_FORMAT')
```

- [ ] **Step 2: Run** `-p test_format_v6.py`. Expected: first and third fail (version becomes 5; no error raised).
- [ ] **Step 3: Implement.** Add 6 to the accepted tuple and to both capability fields; add `ensure_version`; change the module path to `ensure_version(book, 5)`; add the two kinds to the whitelist behind `version>=6`.
- [ ] **Step 4: Run** `-p test_format_v6.py` and `-p test_workbook_code.py`. Expected: PASS.
- [ ] **Step 5: Commit** `Accept native format v6 and stop module saves lowering the version`.

### Task 2: Name catalog and operations

**Files:**
- Create: `service/datahorizon/names.py`
- Modify: `service/datahorizon/engine.py:925-950` (dispatch `name_save`, `name_delete`; add both to the structural tuple at `:932`)
- Test: `tests/test_names.py`

**Interfaces:**
- Consumes: `relational.ensure_version`, `relational.store`, `relational.objects`, `formula.split_ref`, `formula.ADDRESS`, `hooks.supports_event`.
- Produces:
  - Stored object: `{"id": uuid, "name": str, "sheet_id": uuid|None, "row1": int|None, "col1": int|None, "row2": int|None, "col2": int|None}`. Coordinates are 1-based inclusive. All four are `None` when the name is broken.
  - `names.names(db) -> list[dict]` ordered by `id`.
  - `names.valid_name(text) -> bool`: matches `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, is not a cell address in any case, is not `TRUE`/`FALSE`, and is not in the function catalog (compare upper-cased).
  - `names.find(db, key) -> dict|None`: by `id`, else by case-insensitive `name`.
  - `names.read(book, args) -> {"state": str, "names": [stored fields + "sheet": str|None, "reference": str|None, "broken": bool]}`. `reference` is always sheet-qualified and quoted, e.g. `'Supplies'!B2:B4`.
  - `names.mutate(book, request) -> {"name_id": str}` for `action` `name_save` (fields `name`, `reference`, optional `name_id` to rename or retarget) and `name_delete` (field `name_id`).
  - Error codes: `NAME_INVALID` 400, `NAME_DUPLICATE` 409, `NAME_NOT_FOUND` 404, `NAME_LIMIT` 409 (more than 256), `NAME_BROKEN` 409, `REFERENCE_INVALID` 400 (reference lacks a sheet, does not parse, or is outside the grid).
  - A configured handler must define top-level `on_name_structure(ctx, event)`, else `UNSUPPORTED_HOOK_SCOPE`; before and after events are invoked as `workbook_code.mutate` does for `on_code_structure`.

- [ ] **Step 1: Write the failing tests** in `tests/test_names.py`:

```python
def test_save_read_rename_retarget_delete(self):
    nid = save(book, 'Quantities', "'Supplies'!B2:B4")['name_id']
    self.assertEqual(read(book)['names'][0]['reference'], "'Supplies'!B2:B4")
    save(book, 'Qty', "'Supplies'!B2:B5", name_id=nid)       # same id, new name and target
    self.assertEqual([n['name'] for n in read(book)['names']], ['Qty'])
    delete(book, nid); self.assertEqual(read(book)['names'], [])

def test_first_name_upgrades_to_v6_and_rolls_back_on_failure(self): ...
def test_invalid_identifiers_rejected(self):
    for bad in ['a1', 'XFD10', 'sum', 'Sum', 'TRUE', '1x', 'has space', 'x'*65, '']:
        self.assertEqual(code_of(lambda: save(book, bad, "'Supplies'!A1")), 'NAME_INVALID')
def test_duplicate_is_case_insensitive(self): ...            # 'Total' then 'TOTAL' -> NAME_DUPLICATE
def test_reference_must_name_an_existing_sheet(self): ...    # 'B3' and "'Nope'!B3" -> REFERENCE_INVALID
def test_limit_256(self): ...                                # 257th -> NAME_LIMIT
def test_changes_clear_history(self): ...                    # an edit, then name_save -> undo list empty
def test_handler_must_opt_in(self): ...                      # handler without on_name_structure -> UNSUPPORTED_HOOK_SCOPE
def test_stale_state_rejects(self): ...                      # STALE_STATE, catalog unchanged
```

- [ ] **Step 2: Run** `-p test_names.py`. Expected: FAIL, `No module named 'datahorizon.names'`.
- [ ] **Step 3: Implement `names.py`** following `workbook_code.mutate` (`workbook_code.py:113-130`) as the template, and wire the two actions in `Workbook.mutate`.
- [ ] **Step 4: Run** `-p test_names.py`. Expected: PASS.
- [ ] **Step 5: Commit** `Add workbook names as native v6 catalog objects`.

### Task 3: Names follow structural edits

**Files:**
- Modify: `service/datahorizon/formula.py:203-234` (extract the nested `interval` closure)
- Modify: `service/datahorizon/names.py`, `service/datahorizon/engine.py` (`change_structure` near `:1350-1414`, `delete_sheet` near `:1273-1306`)
- Test: `tests/test_names_structure.py`

**Interfaces:**
- Produces:
  - `formula.adjust_interval(first: int, last: int, start: int, count: int, delete: bool) -> tuple[int, int] | None`, the existing expansion/contraction rule; `None` means wholly deleted. `transform_structure` calls it, with no behavior change.
  - `names.adjust_structure(book, sheet_id, axis: str, start: int, count: int, delete: bool) -> list[str]`, returning the names that became broken. `axis` uses the same values as `change_structure`.
  - `names.break_sheet(book, sheet_id) -> list[str]`.
  - Structural receipts gain `"broken_names": [str, ...]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_rectangles_match_independent_enumeration(self):
    # For every rectangle within a 6x6 block and every insert/delete of 1..3
    # rows or columns at every start: compute the expected rectangle by tagging
    # each named cell, moving the tags, and taking their bounds (None if no tag
    # survives). Compare with names.read after the managed structure edit.
def test_insert_inside_expands_and_before_shifts(self): ...   # B2:B4, insert row 3 -> B2:B5; insert row 1 -> B3:B5
def test_delete_all_cells_breaks_name_and_reports_it(self):
    receipt = delete_rows(book, 'Supplies', 2, 3)
    self.assertEqual(receipt['broken_names'], ['Quantities'])
    self.assertTrue(read(book)['names'][0]['broken'])
def test_sheet_delete_breaks_rename_keeps_duplicate_does_not_copy(self): ...
def test_existing_formula_transform_results_unchanged(self): ...   # rerun two cases from test_structure.py
```

- [ ] **Step 2: Run** `-p test_names_structure.py`. Expected: FAIL.
- [ ] **Step 3: Implement** the extraction and the two `names` functions; call them inside the same transaction as the structural edit.
- [ ] **Step 4: Run** `-p test_names_structure.py` and `-p test_structure.py`. Expected: PASS.
- [ ] **Step 5: Commit** `Move named rectangles with row, column and sheet changes`.

### Task 4: Names through REST, OpenAPI, SDK and CLI

**Files:**
- Create: `service/datahorizon/names_api.py`
- Modify: `service/datahorizon/service.py` (`CAPABILITIES` `:34-53`, `ROUTES` `:55`, `openapi()` `:239-253`, `:310-321`, `_dispatch_book` near `:653`), `service/datahorizon/client.py` (near `:181-191`), `service/datahorizon/cli.py` (near `:99` and `:179`)
- Modify: `service/datahorizon/interchange.py` or the export report source so CSV/XLSX/ODS list names under native-only omissions
- Test: `tests/test_names_clients.py`

**Interfaces:**
- Produces:
  - Route `POST /v1/workbooks/{workbook}/names`, request schema `NamesReadRequest {expected_state}`, response schema `NameCatalog`.
  - Actions `name_save` and `name_delete` on `/operations`, with `additionalProperties: false`.
  - Capabilities `"names": True, "max_names": 256`.
  - SDK: `WorkbookClient.names() -> dict`; `save_name(name, reference, *, name_id=None, operation_id=None) -> dict`; `delete_name(name_id, *, operation_id=None) -> dict`.
  - CLI: `names read`, `names save` (JSON `{name, reference, name_id?}` on stdin), `names delete <name_id>`.

- [ ] **Step 1: Write the failing tests** over real HTTP with `LocalService`:

```python
def test_sdk_rest_and_cli_agree(self): ...          # save by SDK, read by raw REST and CLI: same catalog
def test_openapi_validates_and_examples_execute(self): ...   # openapi_spec_validator, then run each example with current state
def test_unknown_field_rejected(self): ...          # name_save with 'approved' -> 400
def test_export_reports_names_as_native_only(self): ...      # XLSX export report lists the names omission
```

- [ ] **Step 2: Run** `-p test_names_clients.py`. Expected: FAIL (404 route).
- [ ] **Step 3: Implement**, using `code_api.install` and the `/code` wiring as the template.
- [ ] **Step 4: Run** `-p test_names_clients.py` and `-p test_automation_openapi.py`. Expected: PASS.
- [ ] **Step 5: Commit** `Expose names through REST, OpenAPI, SDK and CLI`.

---

## Group B — References in code

### Task 5: Address and name forms on the code context

**Files:**
- Create: `service/datahorizon/references.py`
- Modify: `service/datahorizon/hooks.py:51-80` and whitelist `:169`, `service/datahorizon/macro_runtime.py:98`, `service/datahorizon/engine.py:803` (`hook_dispatch`)
- Modify: `docs/design/python-automation-guide.md` (context section)
- Test: `tests/test_context_references.py`

**Interfaces:**
- Consumes: `names.find`, `formula.split_ref`.
- Produces:
  - `references.resolve(book, ref: str, *, single: bool) -> tuple[str, int, int, int, int]` returning `(sheet_name, row1, col1, row2, col2)`. A string containing `!` is parsed as an address (a range uses `:`); otherwise it is looked up as a name. Errors: `REFERENCE_INVALID` 400, `REFERENCE_NOT_FOUND` 404 (unknown sheet or name), `REFERENCE_SHAPE` 400 (range where `single=True`), `NAME_BROKEN` 409, `REFERENCE_LIMIT` 400 (more than 4,096 cells).
  - `BrokerContext.get(*args)` and `value(*args)`: one argument is a reference, three are `(sheet, row, col)`.
  - `BrokerContext.set(*args, kind="auto")`: two arguments are `(ref, source)`, four are `(sheet, row, col, source)`. Any other count raises `TypeError` in the worker.
  - `BrokerContext.values(ref) -> list[list]`, rows of calculated values.
  - Broker messages carry `ref` in place of `sheet`/`row`/`col`; new method name `values`.
  - `set` accepts `str`, and `int`/finite `float`/`bool`. Numbers are stored as the same canonical source text the engine writes for a numeric series fill; `bool` as `TRUE`/`FALSE`. Anything else, including `nan` and `inf`: `CONTEXT_TYPE` 400.
  - Every one of these errors aborts the operation even if the Python code catches the exception, as existing broker errors do.

- [ ] **Step 1: Write the failing tests** using a macro stored with `module_save` and `book.approved_code` faked as in `tests/test_macros.py:23`:

```python
def test_address_and_name_forms_read_and_write(self):
    # macro: q = ctx.value("Supplies!B3"); ctx.set("PenQuantity", q + 2); return ctx.value("Total")
    self.assertEqual(run(book, 'restock')['result'], 20.5)
    self.assertEqual(book.raw('Supplies', 3, 2)['source'], '6')
def test_legacy_three_and_four_argument_forms_still_work(self): ...
def test_values_returns_rows(self): ...                 # ctx.values("Quantities") == [[2],[4],[3]]
def test_quoted_sheet_names_resolve(self):              # Review Focus 1
    for sheet in ["Bob's Sheet", 'Two Words', 'Übersicht']:
        ...ctx.value("'Bob''s Sheet'!B3") returns the value stored at that cell
def test_shape_and_lookup_errors_abort_even_when_caught(self):
    # macro wraps ctx.value("Supplies!B2:B4") in try/except and returns 1
    self.assertEqual(outcome['code'], 'REFERENCE_SHAPE'); self.assertEqual(outcome['outcome'], 'rolled_back')
def test_values_over_4096_cells_rejects(self): ...      # REFERENCE_LIMIT
def test_set_rejects_nan_inf_and_objects(self): ...     # CONTEXT_TYPE
def test_broken_name_rejects(self): ...                 # NAME_BROKEN
def test_external_handler_gets_the_same_forms(self): ...  # on_change fixture using ctx.value("Total")
```

- [ ] **Step 2: Run** `-p test_context_references.py`. Expected: FAIL.
- [ ] **Step 3: Implement.** Resolve in `hook_dispatch` so macros and handlers share it; add `values` to both whitelists.
- [ ] **Step 4: Run** `-p test_context_references.py`, `-p test_macros.py`, `-p test_worker_lifecycle.py`. Expected: PASS.
- [ ] **Step 5: Commit** `Let workbook code address cells by reference text or name`.

---

## Group C — Event bindings

### Task 6: Binding catalog, fingerprint and scope movement

**Files:**
- Create: `service/datahorizon/bindings.py`
- Modify: `service/datahorizon/workbook_code.py:72` (`fingerprint`), `:101` (`read`), `service/datahorizon/engine.py` (dispatch, structural tuple, calls from `change_structure` and `delete_sheet`)
- Test: `tests/test_bindings.py`

**Interfaces:**
- Consumes: `names.find`, `formula.adjust_interval`, `workbook_code.find`, `relational.ensure_version`.
- Produces:
  - Stored object: `{"id", "event": "cell_change"|"table_change", "module_id", "function", "required": bool, "scope": {...}}`, where `scope` is one of `{"kind":"sheet","sheet_id"}`, `{"kind":"range","range_id","sheet_id","row1","col1","row2","col2"}`, `{"kind":"name","name_id"}`, `{"kind":"table","table_id"}`. `cell_change` allows sheet, range and name scopes; `table_change` allows only table.
  - `range_id` is a new UUID whenever `binding_save` sets or changes a range rectangle. Structural movement keeps it.
  - Request scope forms for `binding_save`: `{"kind":"sheet","sheet":name}`, `{"kind":"range","reference":"'Supplies'!B2:B4"}`, `{"kind":"name","name":"Quantities"}`, `{"kind":"table","table":name}`.
  - `bindings.bindings(db) -> list[dict]` ordered by `id`.
  - `bindings.fingerprint_view(binding) -> dict`: the stored object without `row1`, `col1`, `row2`, `col2`.
  - `workbook_code.fingerprint(db)`: unchanged when there are no bindings; otherwise `sha256(encoded({"modules": modules(db), "bindings": [fingerprint_view(b) ...]}))`.
  - `bindings.mutate(book, request) -> {"binding_id", "code_fingerprint"}` for `binding_save` (fields `event`, `scope`, `module`, `function`, `required` default `True`, `fingerprint`, optional `binding_id`) and `binding_delete` (`binding_id`, `fingerprint`). Both require the current code fingerprint (`CODE_CHANGED` 409 otherwise), clear history, and use the existing `on_code_structure` opt-in.
  - `workbook_code.read` adds `"bindings": [stored + "module": name, "reference": str|None, "broken": bool]`.
  - `bindings.adjust_structure(book, sheet_id, axis, start, count, delete) -> list[str]` and `bindings.break_sheet(book, sheet_id) -> list[str]`; receipts gain `"broken_bindings"`.
  - Errors: `BINDING_INVALID` 400 (bad event/scope pairing, function is not a top-level `def` in the module by AST scan), `BINDING_NOT_FOUND` 404, `BINDING_LIMIT` 409 (more than 16), `MODULE_NOT_FOUND` 404.
  - `module_delete` rejects with `MODULE_IN_USE` 409 while a binding refers to the module.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_bindings_keeps_existing_fingerprint(self):
    before = fingerprint(book.db)             # value recorded before this task's change, asserted as a literal
    self.assertEqual(before, sha256(encoded(modules(book.db)).encode()).hexdigest())
def test_adding_removing_or_redirecting_changes_fingerprint(self): ...
def test_moving_rows_keeps_fingerprint_and_moves_scope(self):
    fp = fingerprint(book.db); insert_rows(book, 'Supplies', 1, 1)
    self.assertEqual(fingerprint(book.db), fp)
    self.assertEqual(read(book)['bindings'][0]['reference'], "'Supplies'!B3:B5")
def test_retargeting_a_name_scope_keeps_fingerprint(self): ...
def test_deleted_scope_is_broken_and_reported(self): ...    # receipt['broken_bindings'], read(...)['broken'] is True
def test_function_must_be_top_level_def(self): ...          # BINDING_INVALID
def test_stale_fingerprint_rejects(self): ...               # CODE_CHANGED
def test_limit_16(self): ...                                # BINDING_LIMIT
def test_module_with_binding_cannot_be_deleted(self): ...   # MODULE_IN_USE
def test_open_and_read_never_execute(self): ...             # module writes a marker file at import; marker absent after open/read/copy
```

- [ ] **Step 2: Run** `-p test_bindings.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `-p test_bindings.py`, `-p test_workbook_code.py`, `-p test_code_activation.py`. Expected: PASS.
- [ ] **Step 5: Commit** `Store event bindings in the workbook and cover them by the code fingerprint`.

### Task 7: Running bindings inside managed operations

**Files:**
- Modify: `service/datahorizon/engine.py:822-839` (`run_hooks`), `service/datahorizon/macro_runtime.py`, `service/datahorizon/bindings.py`
- Test: `tests/test_binding_execution.py`

**Interfaces:**
- Consumes: `bindings.bindings`, `workbook_code.activation`, `references`, `names`.
- Produces:
  - Cell change records gain `"action": self.operation_action` (the same value table records already carry).
  - `bindings.plan(book, cell_batch, table_batch) -> list[dict]`, each `{"binding", "module_name", "source", "function", "changes"}` in stored order, omitting bindings whose filtered `changes` are empty or whose scope is broken.
  - `macro_runtime.invoke_bindings(calls, operation, dispatch, deadline) -> None`: one spawned worker per round runs every call in order as `function(ctx, changes)`, each module executed once in a fresh namespace. Before each call the worker sends a `{"starting": index}` frame so a timeout or crash can name the binding. Round budget `min(deadline, now + 2)`.
  - `run_hooks` no longer returns early without a handler. Per round: external handler first (unchanged), then `plan` and `invoke_bindings`. The 16-round bound and `HOOK_RECURSION` apply to the combined loop.
  - Before invoking, if the code is not approved or a module fails static validation: a required binding raises `BINDING_NOT_APPROVED` 403 or `BINDING_UNAVAILABLE` 409; a non-required one is skipped and added to the receipt as `"binding_notices": [{"binding_id", "function", "reason"}]`.
  - A binding that raises: `BINDING_FAILED` 409, message `Binding <module>.<function>: <error>`; timeout: `BINDING_TIMEOUT` 408 with the same prefix. Both roll back the whole operation, required or not.
  - Inserting or deleting rows and columns does not call cell-change bindings.

- [ ] **Step 1: Write the failing tests**

```python
def test_rule_rejects_negative_quantity_from_every_path(self):
    # check_quantity raises if ctx.value(c['sheet'], c['row'], c['col']) < 0
    # one case each: set_cell, paste, fill, CSV import apply, a macro's ctx.set
    self.assertEqual(outcome['code'], 'BINDING_FAILED'); self.assertIn('supplies.check_quantity', outcome['message'])
    self.assertEqual(book.raw('Supplies', 2, 2)['source'], '2')
def test_runs_without_any_external_handler(self): ...
def test_external_handler_runs_before_bindings_in_stored_order(self): ...   # each appends to a log cell
def test_generated_edits_join_one_undo_entry(self): ...
def test_changes_are_filtered_to_scope(self): ...           # edit outside B2:B4 does not call the function
def test_unapproved_required_binding_blocks_edits_in_scope_only(self): ...  # BINDING_NOT_APPROVED in scope; edit elsewhere commits
def test_unapproved_optional_binding_is_skipped_with_notice(self): ...      # receipt['binding_notices']
def test_optional_binding_that_raises_still_rejects(self): ...
def test_undo_is_rejected_when_approval_was_lost_and_history_survives(self):   # Review Focus 4
    edit(book, 'Supplies', 2, 2, '5'); book.approved_code = frozenset()
    self.assertEqual(code_of(lambda: undo(book)), 'BINDING_NOT_APPROVED')
    self.assertEqual(len(book.undo), 1)
def test_undo_and_redo_pass_action(self): ...               # function records changes[0]['action']
def test_timeout_names_the_running_binding(self): ...       # second of two bindings spins -> message names it
def test_seventeen_rounds_is_hook_recursion(self): ...
def test_row_insert_does_not_call_cell_bindings(self): ...
def test_direct_sqlite_edit_runs_nothing_and_is_not_replayed(self): ...
def test_names_bindings_and_code_operations_stay_allowed_when_unapproved(self): ...
```

- [ ] **Step 2: Run** `-p test_binding_execution.py`. Expected: FAIL.
- [ ] **Step 3: Implement.** Reuse `macro_runtime._worker`'s module execution and `BrokerContext`; keep `HookFailure` handling so receipts stay durable.
- [ ] **Step 4: Run** `-p test_binding_execution.py`, `-p test_structural_hooks.py`, `-p test_macros.py`, then the full Python suite. Expected: PASS.
- [ ] **Step 5: Commit** `Run approved workbook bindings inside managed operations`.

### Task 8: Bindings through REST, OpenAPI, SDK and CLI

**Files:**
- Modify: `service/datahorizon/code_api.py`, `service/datahorizon/service.py` (`CAPABILITIES`, `:249` tuple), `service/datahorizon/client.py`, `service/datahorizon/cli.py`
- Test: `tests/test_binding_clients.py`

**Interfaces:**
- Produces:
  - Actions `binding_save`, `binding_delete` with `additionalProperties: false`; `CodeCatalog` gains `bindings`; `Receipt` gains `binding_notices`, `broken_names`, `broken_bindings`.
  - Capabilities `"event_bindings": True, "max_bindings": 16, "binding_events": ["cell_change", "table_change"]`.
  - SDK: `save_binding(event, scope, module, function, *, fingerprint, required=True, binding_id=None, operation_id=None)`; `delete_binding(binding_id, *, fingerprint, operation_id=None)`. `code()` already returns bindings.
  - CLI: `bindings read`, `bindings save` (JSON on stdin), `bindings delete <binding_id> --fingerprint <sha>`.

- [ ] **Step 1: Write the failing tests** over real HTTP:

```python
def test_gui_shaped_rest_edit_cli_edit_and_sdk_edit_hit_the_same_rule(self): ...
def test_openapi_validates_and_examples_execute(self): ...
def test_request_fields_cannot_claim_approval(self): ...     # binding_save with 'approved': true -> 400
def test_export_reports_bindings_as_native_only(self): ...
```

- [ ] **Step 2–4:** run `-p test_binding_clients.py` (FAIL), implement, run it and `-p test_automation_clients.py` (PASS).
- [ ] **Step 5: Commit** `Expose event bindings through REST, OpenAPI, SDK and CLI`.

---

## Group D — Approval channel

### Task 9: Owner-only approval over the service's standard input

**Files:**
- Modify: `service/datahorizon/code_approval.py`, `service/datahorizon/workbook_code.py:76-92`, `service/datahorizon/engine.py:277-280`, `service/datahorizon/service.py:369-375`, `:490`, `:818-832`, `service/datahorizon/client.py:481-503`
- Modify: `docs/design/python-automation-plan.md` (activation contract), `docs/design/python-automation-guide.md`
- Test: `tests/test_owner_channel.py`

**Interfaces:**
- Produces:
  - `class ApprovalStore` in `code_approval.py`: `__init__(keys=())` (validated by `validate_approvals`), `__contains__(key) -> bool`, `grant(key) -> None`, `snapshot() -> frozenset`. Thread-safe. `grant` validates the key format, replaces any earlier *granted* key for the same workbook UUID, never removes start-up keys, and raises `ValueError` beyond 256 total.
  - `Service` owns one `ApprovalStore`; every `Workbook` receives that same object as `approved_code`, so a grant reaches workbooks already open. `Workbook.__init__` wraps a plain iterable in a new store, so existing tests that assign a `frozenset` keep working.
  - `service.main()` flag `--owner-channel`. When set, a daemon thread reads standard input lines; a line `{"approve": "<key>"}` calls `grant`. Malformed lines and invalid keys write one line to standard error and are otherwise ignored. End of input stops the reader and nothing else. Without the flag standard input is never read. Standard output stays reserved for the start-up line.
  - `LocalService(handler=None, *, approved_code=(), owner_channel=False)`; when true it starts the service with `--owner-channel` and `stdin=PIPE`. `LocalService.approve(key: str) -> None` writes one line and flushes; raises `RuntimeError` if the channel is off.
  - No route is added. `CAPABILITIES["macro_activation"]` text gains `; owner channel` when the flag is set.

- [ ] **Step 1: Write the failing tests**

```python
def test_grant_reaches_an_open_workbook(self):
    with LocalService(owner_channel=True) as client, client.open(path) as book:
        key = book.code()['activation_key']; self.assertFalse(book.code()['approved'])
        client.service.approve(key)                      # name the handle as client.py exposes it
        wait_until(lambda: book.code()['approved'])
        self.assertEqual(book.run_macro('m', 'f', fingerprint=book.code()['fingerprint'])['result'], 1)
def test_regrant_replaces_previous_key_for_same_workbook(self): ...   # 300 save+approve cycles never hit the cap
def test_startup_keys_survive_grants(self): ...
def test_key_for_changed_code_does_not_approve(self):                  # Review Focus 3
    key = book.code()['activation_key']
    ...edit the module source with sqlite3 directly...
    client.service.approve(key); time.sleep(0.3)
    self.assertFalse(book.code()['approved'])
def test_http_cannot_grant(self):
    # POST /code and /operations with {'approve': key}, {'approved': True}, {'approved_code': [key]}
    # -> 400 or ignored, and code()['approved'] stays False
def test_service_without_flag_ignores_stdin(self): ...
def test_malformed_lines_do_not_stop_the_service(self): ...
def test_attached_client_has_no_approve(self): ...                      # Client(url, token) exposes no grant path
```

- [ ] **Step 2: Run** `-p test_owner_channel.py`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `-p test_owner_channel.py`, `-p test_code_activation.py`, `-p test_macros.py`. Expected: PASS.
- [ ] **Step 5: Commit** `Accept code approval only from the owning process over a private channel`.

---

## Group E — Workspace window and editing

### Task 10: Workspace window, modules, editor, drafts and save

**Files:**
- Create: `desktop/pythonworkspace.h`, `desktop/pythonworkspace.cpp`, `desktop/codeeditor.h`, `desktop/pythonhighlighter.h`, `desktop/macrodeclarationpanel.h`, `tests/test_python_workspace.cpp`
- Modify: `desktop/mainwindow.h`, `desktop/mainwindow.cpp` (`buildUi` `:330-349`, `acceptInfo` `:1265`, `setBusy` `:1449`, `closeEvent` before `:2686`), `CMakeLists.txt:47-51` and the test block near `:91-128`

**Interfaces:**
- Consumes: `RestClient::request`, `MainWindow::submit(QJsonObject)`, `MainWindow::operationReceipt(QJsonObject)`, `POST base()+"/code"`.
- Produces:
  - `class PythonWorkspace : public QMainWindow` with `explicit PythonWorkspace(RestClient *client, QWidget *owner)`, constructed with `Qt::Window` so it is a separate top-level window; `void bind(const QJsonObject &info)`; `void setLocked(bool)`; `bool resolveDrafts()` (Save/Discard/Cancel; false means stay open); `bool hasDrafts() const`.
  - Signals: `submitRequested(QJsonObject)`, `issue(QString)`.
  - `MainWindow`: `QAction` `pythonWorkspaceAction` in a new `&Tools` menu between `&Data` and `&View`, text `Python workspace`; member `PythonWorkspace *m_workspace`; slot `showPythonWorkspace()`. `closeEvent` calls `m_workspace->resolveDrafts()` and ignores the event on false.
  - `class CodeEditor : public QPlainTextEdit`: `void markLine(int line, bool error)`, `void clearMarks()`, `void insertAtCursor(const QString &)`; Tab inserts four spaces.
  - Find: `findAction` (standard Find shortcut) opens an inline field `findField`; Return selects the next match and wraps.
  - The side panel is a `QStackedWidget` `sidePanel` that shows the macro declaration form or, from task 11, the Names panel, switched by two toolbar toggles `showMacroAction` and `showNamesAction`.
  - Window geometry is saved to and restored from the existing local view-state settings under the key `pythonWorkspace/geometry`.
  - `class MacroDeclarationPanel : public QWidget`: `void setDeclaration(const QJsonObject &)`, `QJsonObject declaration() const`, signal `changed()`.
  - Object names: `pythonWorkspace`, `moduleList`, `codeEditor`, `saveModuleAction`, `newModuleButton`, `macroPanel`, `workspaceStatus`, `workspaceApproval`.
  - Status texts, exact: `Draft · not saved to the workbook`, `Saved in workbook`, `Not approved to run`, `Approved for this session`, `Read-only workbook`.
  - Save sends `{action:"module_save", name, source, macros, fingerprint, module?}` through `submitRequested`. If the main window's Undo history is not empty, the first save in a session asks `Saving code clears Undo history for this workbook. Continue?` with a `Don't ask again this session` checkbox.
  - CMake: `add_executable(python_workspace_tests tests/test_python_workspace.cpp)`, `add_test(NAME python_workspace ...)` with the same environment and a 600 s timeout as `desktop`.

- [ ] **Step 1: Write the failing Qt tests** (each starts a real owned service as `tests/test_desktop.cpp:327` does):

```cpp
void opensAsSeparateTopLevelWindow();      // action triggers; workspace->isWindow(); main window grid still visible
void emptyStateOffersNewModule();          // moduleList count 0; newModuleButton visible
void typingCreatesDraftNotStoredCode();    // status text; POST /code still returns no modules
void saveStoresModuleAndDeclaration();     // after Save, /code returns name "supplies", macro "restock_pens"
void addingDeclarationInsertsStub();       // editor contains "def restock_pens(ctx, extra):"
void staleSaveKeepsDraft();                // a second service edits code first; draft text unchanged; issue names CODE_CHANGED
void validationErrorKeepsDraft();          // syntax error -> draft kept, message shown
void firstSaveWithHistoryAsksOnce();       // dialog appears once, not on the second save
void closeWithDraftPromptsAndCancelStays();
void readOnlyWorkbookDisablesSaveWithReason();
void findSelectsNextMatchAndWraps();
void geometryIsRestoredOnReopen();
void serviceRestartKeepsDraftsAndClearsApproval();   // Review Focus 5
```

- [ ] **Step 2: Build and run** `python_workspace_tests`. Expected: build fails (no `pythonworkspace.h`).
- [ ] **Step 3: Implement.** Follow `RelationalWorkbench` (`relationalworkbench.h:17-22`, wiring at `mainwindow.cpp:643-656`) for `bind`, `setLocked` and signal routing.
- [ ] **Step 4: Run** `python_workspace_tests` and `ctest -R desktop`. Expected: PASS.
- [ ] **Step 5: Commit** `Add the Python workspace window with module editing and drafts`.

### Task 11: Names in the address box, Name Manager and Names panel

**Files:**
- Create: `desktop/namemanager.h`
- Modify: `desktop/mainwindow.cpp` (`syncSource` `:1471-1500`, address handler `:704-725`, `acceptInfo`, menu), `desktop/mainwindow.h`, `desktop/pythonworkspace.cpp`
- Test: `tests/test_python_workspace.cpp`

**Interfaces:**
- Consumes: `POST base()+"/names"`, actions `name_save`/`name_delete`, `CodeEditor::insertAtCursor`.
- Produces:
  - `MainWindow::names() const -> QJsonArray` (refreshed in `acceptInfo`), signal `namesChanged()`.
  - `class NamesPanel : public QWidget`: `void setNames(const QJsonArray &)`; signals `insertRequested(QString name)`, `createRequested()`, `editRequested(QString nameId)`, `deleteRequested(QString nameId)`. Object name `namesPanel`; list `namesList`; button `newNameButton` with text `New name from selection…`.
  - `class NameManagerDialog : public QDialog` hosting a `NamesPanel` plus name and reference fields; object name `nameManagerDialog`. Opened by `nameManagerAction` (`Name manager…`) in the Data menu.
  - Address box shows the name when the selection exactly equals a name's rectangle; typing a name and pressing Return selects its rectangle. A broken name reports `Name <name> no longer refers to any cells` through `issue`.
  - Name operations confirm `Changing a name clears Undo history for this workbook. Continue?`.
  - Editor completion offers defined names and the context method names `get`, `value`, `values`, `set`, `table_schema`, `table_get`, `table_set`, `table_insert`, `table_delete`.

- [ ] **Step 1: Write the failing Qt tests**

```cpp
void addressBoxShowsAndJumpsToName();       // select D6 -> "Total"; type "Quantities" -> selection B2:B4
void newNameFromSelectionCreatesName();
void doubleClickInsertsQuotedNameAtCursor();   // editor gains "\"Total\""
void invalidNameShowsServiceMessage();
void brokenNameIsMarkedAndDoesNotJump();
void completionOffersNames();
```

- [ ] **Step 2–4:** run (FAIL), implement, run `python_workspace_tests` and `ctest -R desktop` (PASS).
- [ ] **Step 5: Commit** `Add the name manager and name-aware address box`.

### Task 12: Pick from sheet

**Files:**
- Modify: `desktop/mainwindow.h` (`:193-200`), `desktop/mainwindow.cpp` (`startReferencePick` `:1098`, `referenceMouseEvent` `:1143`, `finishReferencePick` `:1132`, `eventFilter` `:2715`), `desktop/pythonworkspace.cpp`
- Test: `tests/test_python_workspace.cpp`

**Interfaces:**
- Consumes: `ReferencePickerDialog::reference()` quoting rule (`referencepickerdialog.h:43`), `MainWindow::names()`.
- Produces:
  - `struct ReferencePickTarget { std::function<void(const QString &)> insert; std::function<bool()> stillValid; };`
  - `bool MainWindow::startExternalReferencePick(ReferencePickTarget target)`; false with an `issue` when busy, unknown, a cell draft is active, or the sheet is a projected list. `void MainWindow::cancelExternalReferencePick()`. Signal `externalPickFinished(bool inserted)`.
  - The mouse handling in `referenceMouseEvent` is shared by both modes; the formula mode keeps its current behavior.
  - Text passed to `insert`: the exact-match name if one exists, else the always-quoted qualified address `'Supplies'!B3` or `'Supplies'!B2:B4`. The workspace wraps it in double quotes, so the editor receives `"'Supplies'!B3"` or `"Total"`.
  - While picking: main window status bar shows `Picking a reference for Python · Esc to cancel`; the grid never enters edit mode; the previous current cell and selection are restored on finish or cancel. Escape in either window cancels.
  - Workspace action `pickFromSheetAction`, checkable, text `Pick from sheet`.

- [ ] **Step 1: Write the failing Qt tests**

```cpp
void clickInsertsQualifiedCellAtCursor();        // "\"'Supplies'!B3\""
void dragInsertsRange();                         // "\"'Supplies'!B2:B4\""
void exactNameMatchInsertsName();
void quotedSheetNameRoundTrips();                // sheet "Bob's Sheet": text is "'Bob''s Sheet'!B3"; a macro using it reads that cell  (Review Focus 1)
void pickingLeavesDataSelectionAndStateUnchanged();   // workbook state string and selection equal before/after; no operation submitted
void escapeCancelsFromEitherWindow();
void refusedWhileCellDraftActive();
void formulaReferencePickStillWorks();           // rerun the existing formula pick scenario
```

- [ ] **Step 2–4:** run (FAIL), implement, run `python_workspace_tests` and `ctest -R desktop` (PASS).
- [ ] **Step 5: Commit** `Insert picked cell references into the Python editor`.

---

## Group F — Approve, run, bind

### Task 13: Review and approve from the desktop

**Files:**
- Create: `desktop/codeapprovaldialog.h`
- Modify: `desktop/mainwindow.cpp` (`startService` `:1188-1226`), `desktop/mainwindow.h`, `desktop/pythonworkspace.cpp`
- Test: `tests/test_python_workspace.cpp`

**Interfaces:**
- Consumes: service flag `--owner-channel` and the `{"approve": key}` line from task 9; `/code` fields `fingerprint`, `activation_key`, `approved`, `modules`, `bindings`.
- Produces:
  - Owned services start with `--owner-channel`. `bool MainWindow::ownsService() const`. `bool MainWindow::grantCodeApproval(const QString &key)` writes one JSON line to `m_service` and returns false when attached or the process is not running.
  - `class CodeApprovalDialog : public QDialog`, object name `codeApprovalDialog`, title `Approve Python code for <file name>?`, rows `Changed since last approval` and `Code fingerprint`, warning text exactly: `Approved code runs with your own account's access to files and the network. It is not sandboxed, and undo cannot reverse effects outside the workbook.` Buttons `Cancel` and `Approve for this session` (`approveButton`).
  - Workspace action `reviewApproveAction` (`Review & approve`), enabled only when there are no drafts. On approve the workspace re-reads `/code`, and grants only if the fingerprint still equals the one shown; otherwise it reports `The stored code changed while it was being reviewed. Review it again.` It then polls `/code` for up to 3 s until `approved` is true.
  - Attached mode: the dialog shows the activation key and the text `This service was started separately. Its operator approves code by starting it with --approve-code <key>.`; no approve button.

- [ ] **Step 1: Write the failing Qt tests**

```cpp
void approveMakesCodeRunnable();                 // status "Approved for this session"; /code approved true
void editAfterApprovalReturnsToNotApproved();
void changedCodeDuringReviewIsRefused();         // direct SQLite edit while the dialog is open (Review Focus 3)
void attachedServiceShowsKeyAndCannotApprove();  // second window with options.url/token, as test_desktop.cpp:1172
void approveDisabledWithDrafts();
```

- [ ] **Step 2–4:** run (FAIL), implement, run `python_workspace_tests` (PASS).
- [ ] **Step 5: Commit** `Approve workbook code from the desktop over the owner channel`.

### Task 14: Run, cancel and output

**Files:**
- Modify: `desktop/pythonworkspace.h/.cpp`
- Test: `tests/test_python_workspace.cpp`

**Interfaces:**
- Consumes: action `macro_run` `{module, macro, fingerprint, arguments}` via `submitRequested`; `MainWindow::operationAdmitted(QString)`, `operationReceipt(QJsonObject)`, `operationFinished(bool)`; receipt fields `outcome`, `result`, `macro` (logs, traceback, execution), `changed_cells`, `binding_notices`; `POST base()+"/operations/<id>/cancel"` (confirm the exact path at `service.py:685`).
- Produces:
  - Bottom `QTabWidget` `workspaceTabs` with tabs `Run`, `Output`, `Events`.
  - Run tab: `macroSelect`, one typed field per declared argument named `argument_<name>`, `runButton`, `cancelRunButton`. Run is enabled only when code is saved, approved, the workbook is writable, and no outcome is unknown; otherwise its tooltip states which condition fails.
  - Output view `runOutput`: first line is one of `Committed`, `Rolled back`, `Cancelled`, `Timed out`, `Outcome unknown`, followed by ` · returned <result> · <n> cells changed` on success, then the log tail and traceback. Activating a traceback line for a workbook module calls `CodeEditor::markLine(line, true)` and scrolls to it.
  - Arguments are type-checked locally against the declaration before sending; a bad value marks the field and sends nothing.

- [ ] **Step 1: Write the failing Qt tests**

```cpp
void runUpdatesSheetAndReportsResult();          // restock_pens(extra=2): grid B3 "6", D6 "20.50"; output starts "Committed · returned 20.5"
void undoReversesWholeMacroFromMainWindow();     // Edit ▸ Undo text names the macro; B3 back to "4"
void failureMarksLineAndLeavesSheet();           // extra=-10: output starts "Rolled back"; editor line 5 marked; B3 still "4"
void cancelStopsSpinningMacro();                 // output starts "Cancelled"
void badArgumentIsNotSent();
void runDisabledUntilApprovedWithReason();
void unknownOutcomeBlocksFurtherRuns();          // socket fixture drops the reply, as the existing unknown-outcome tests do
```

- [ ] **Step 2–4:** run (FAIL), implement, run `python_workspace_tests` (PASS).
- [ ] **Step 5: Commit** `Run and cancel macros from the workspace with outcome-first output`.

### Task 15: Events tab and rejection display

**Files:**
- Modify: `desktop/pythonworkspace.h/.cpp`, `desktop/mainwindow.cpp` (`submit` rejection path `:1345-1360`, `issue`)
- Test: `tests/test_python_workspace.cpp`

**Interfaces:**
- Consumes: actions `binding_save`/`binding_delete`; `/code` `bindings`; `startExternalReferencePick`; `NamesPanel`; receipt `binding_notices`; error codes `BINDING_FAILED`, `BINDING_NOT_APPROVED`, `BINDING_UNAVAILABLE`, `BINDING_TIMEOUT`.
- Produces:
  - Events tab list `bindingList`; editor row with `bindingEvent` (`cells change`, `table records change`), `bindingScope` (accepts a name, a qualified address, a sheet or table name; a pick button fills it), `bindingFunction` (top-level functions of the stored modules), `bindingRequired` checkbox default checked, `addBindingButton`, `removeBindingButton`. A broken binding row is labelled `Broken: its cells no longer exist`.
  - Binding changes require saved code, send the current fingerprint, and return the workspace to `Not approved to run`.
  - A grid edit rejected by a binding keeps its draft (existing path) and shows the service message in the status bar and Issues: `Rejected by <module>.<function>: <error> · nothing stored`.
  - On open, if required bindings exist and code is not approved, Issues shows once: `This workbook has rules that are not approved. Edits to the cells they cover are blocked until its Python code is approved (Tools ▸ Python workspace).`
  - `binding_notices` in any receipt are appended to Issues.

- [ ] **Step 1: Write the failing Qt tests**

```cpp
void bindRuleThenNegativeGridEditIsRejectedAndDraftKept();   // type -1 in B2: status text, draft "-1" still in the editor, stored "2"
void scopeAcceptsNameAndPickedRange();
void addingBindingRequiresFreshApproval();
void unapprovedRequiredBindingBlocksOnlyItsScopeWithNotice();
void optionalBindingSkipIsReportedInIssues();
void brokenBindingIsLabelled();
```

- [ ] **Step 2–4:** run (FAIL), implement, run `python_workspace_tests` and `ctest -R desktop` (PASS).
- [ ] **Step 5: Commit** `Bind workbook functions to edits from the Events tab`.

---

## Group G — Integration

### Task 16: Storyboard walkthrough, guide and evidence

**Files:**
- Create: `examples/supplies_workspace.py` (builds the Supplies workbook with the storyboard's module, names and binding through the SDK), `docs/design/python-workspace-guide.md`, `docs/design/evidence/python-workspace-task16.json`
- Modify: `tests/native_acceptance.cpp` (a `pythonWorkspace` scenario), `docs/design/implementation-status.md`, `docs/design/README.md`, `docs/design/python-automation-guide.md`, `docs/design/compatibility-v0.md`, `README.md`
- Test: `tests/test_workspace_example.py`

**Interfaces:**
- Consumes: everything above.
- Produces: a reproducible example and the installed-application evidence.

- [ ] **Step 1: Write the failing test** `tests/test_workspace_example.py`: run `examples/supplies_workspace.py create <path>` then `exercise <path>` and assert its JSON report shows total 18 → 20.5 after `restock_pens(extra=2)`, Undo back to 18, `extra=-10` rolled back with `Below zero`, and a `-1` edit to `Supplies!B2` rejected by `supplies.check_quantity`.
- [ ] **Step 2: Run** `-p test_workspace_example.py`. Expected: FAIL (no example).
- [ ] **Step 3: Write the example and the guide.** The guide's code uses the real record shape (`change['after']`, `ctx.value(...)`), states that address text is not rewritten by structural edits, that row and column insert/delete do not call cell-change bindings, and that approved code is not sandboxed.
- [ ] **Step 4: Add the native scenario**: open the workspace, type the module, pick B3, name cells, save, approve, run, undo, fail, bind, reject. Run `build/native_acceptance out.json` at scale requests 1 and 2.
- [ ] **Step 5: Qualify.** Run the full Python suite, `ctest --test-dir build --output-on-failure`, `cmake --install build --prefix build/package`, `python3 tests/package_smoke.py build/package`, and the example against the installed bundled runtime. Record commands, counts, source commit and artifact hash in the evidence file.
- [ ] **Step 6: Update the status ledger** (M8 row and a new batch section) with what is implemented and what remains: names in formulas and interchange, custom functions, packages, structure and open events, docking, physical input, VoiceOver, Windows/Linux.
- [ ] **Step 7: Commit** `Qualify the Python workspace walkthrough and record evidence`.

---

## Not in this plan

Names in formulas and in XLSX/ODS files; Python custom worksheet functions; package installation; imports between workbook modules; a debugger; docking the workspace; range writes in one context call; workbook-open, structure and code events for bindings; changed-cell highlighting after a run; crash-persistent editor drafts; Windows/Linux qualification, signing and notarization.
