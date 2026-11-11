# Workbook Python automation guide

Native v5 stores Python modules and explicit macro declarations; native v6 adds
workbook names and event bindings. Stored code stays inert until an operator
approves its exact workbook/code activation key and a client explicitly invokes
a macro or a managed edit reaches a binding. This guide covers REST, the managed
CLI and the SDK. The [Python workspace guide](python-workspace-guide.md) covers
writing, approving, running and binding code from the desktop application.
Custom formula functions follow separately.

## Run the example

From the repository root, create a new disposable native file:

```sh
PYTHONPATH=service python3 examples/python_automation.py create /tmp/Automation.dhwb
```

Read `examples/automation_module.py`, `examples/automation_rules.py` and the
printed declarations. Copy the printed `activation_key` only after reviewing the
code. Approval configures this owned service and is not stored in the workbook:

```sh
PYTHONPATH=service python3 examples/python_automation.py exercise /tmp/Automation.dhwb --approve-code '<workbook UUID>:<SHA256>'
```

The macro changes Measurements record 1 from 10 to 13 and writes Sheet1!A1.
The required fixture handler writes B1 in the same transaction. The example
checks the durable result, Undo, Redo, restoration to 10 and rejection of a
negative value with all macro/generated edits rolled back. JSON reports include
the loaded SDK path, source fingerprint, outcome, bounded logs and traceback.
The nonnegative rule belongs to the example's Python code.

## Store and inspect modules

Use `book.code()` for a state-checked catalog with source, declarations,
fingerprint, activation key and current service approval. `code(module)` limits
the returned modules. `save_module(name,source,macros,fingerprint=reviewed_hash,
module=existing_id)` updates an explicit identity; omit `module` to create a new
one. `delete_module(module,fingerprint=reviewed_hash)` removes it. Changed code or
declarations require a fresh review; stale state or fingerprint rejects.

The CLI equivalents are:

```sh
PYTHONPATH=service python3 -m datahorizon.cli /tmp/Automation.dhwb code read
PYTHONPATH=service python3 -m datahorizon.cli /tmp/Automation.dhwb code read MeasurementsCode
PYTHONPATH=service python3 -m datahorizon.cli /tmp/Automation.dhwb code save < reviewed-module.json
PYTHONPATH=service python3 -m datahorizon.cli /tmp/Automation.dhwb code delete MeasurementsCode --fingerprint '<SHA256>'
```

`reviewed-module.json` contains `name`, `source`, `macros`, `fingerprint` and an
optional existing `module` identity. A declaration has `name`, `parameters`
(named typed scalars with optional explicit defaults) and `result` (`type` and
optional `nullable`). Functions are invoked as `function(ctx, **arguments)`.
There are at most 16 modules, 32 macros per module and 32 arguments per macro.
Source is at most 128 KiB, one serialized module 256 KiB and all module metadata
2 MiB. Native Save/copy preserves modules; CSV/XLSX/ODS omit executable source
and report native-only omissions. Opening and inspection do not run imports.

## Invoke through managed clients

Owned SDK services accept exact keys with
`LocalService(approved_code=[activation_key])`. A configured external handler is
still required for its applicable events. Code changes need its explicit
`on_code_structure(ctx,event)` before/after support.

```python
with LocalService(approved_code=[reviewed_activation_key]) as client:
    with client.open(native_path) as book:
        code = book.code()
        receipt = book.run_macro('MeasurementsCode', 'adjust',
            fingerprint=code['fingerprint'], arguments={'delta': 3},
            operation_id=retained_operation_id)
```

For this example, also pass `examples/automation_rules.py` as the service's
handler to activate its validation and generated edit. The same CLI invocation:

```sh
printf '%s' '{"delta":3}' | PYTHONPATH=service python3 -m datahorizon.cli --handler examples/automation_rules.py --approve-code '<activation key>' /tmp/Automation.dhwb macro MeasurementsCode adjust --fingerprint '<SHA256>' --operation-id '<retained UUID>'
```

A service you start can also be approved while it runs: pass
`LocalService(owner_channel=True)` and call `service.approve(activation_key)`
(keep the service object; `with service as client` yields the client). The
service reads `{"approve": key}` lines from its standard input only when started
with `--owner-channel`; a later grant for the same workbook replaces the earlier
one, start-up keys are kept, and changed code needs a new key. No HTTP request
can grant approval.

The service entry point accepts repeatable `--approve-code` options. Attached
CLI clients use `--url` and `DATAHORIZON_TOKEN`; their operator configures grants
when starting the service. A remote CLI cannot use `--approve-code` to change
another service's policy. REST has code inspection and managed operations, with
OpenAPI examples and the interactive explorer, but no code-grant endpoint.

To test shared GUI/CLI history, start one service explicitly and retain its
printed URL/token locally:

```sh
PYTHONPATH=service python3 -m datahorizon.service --handler examples/automation_rules.py --approve-code '<activation key>'
```

Launch the installed GUI with `DATAHORIZON_TOKEN` set to that token,
`--connect <printed URL>` and the example workbook path. Invoke the macro using
an attached CLI (`--url <printed URL>` and the same token, without an approval
flag), then refresh the GUI and test Undo/Redo there. The GUI's open handle keeps
the shared history alive between CLI commands. Closing the last service handle
ends session history; durable operation receipts remain in the native file.
Opening the workbook in a separate GUI-owned service starts a separate session.

Cells are addressed either as `(sheet, row, col)` or by reference text:
`ctx.get`/`ctx.value` take a reference, `ctx.set(ref, source)` writes one, and
`ctx.values(ref)` returns the calculated values of a rectangle as a list of rows
(blank cells are `None`, at most 4,096 cells). A reference is a sheet-qualified
address such as `Supplies!B3`, `'Bob''s Sheet'!B2:B4`, or a defined name such as
`Total`. `get`, `value` and `set` need a single cell. `set` accepts text, finite
numbers and booleans. Shape, lookup, broken-name, limit and type errors abort
the operation even if the code catches them; macros keep the specific code
(for example `REFERENCE_SHAPE`) as `context_code` in the receipt. Address text
inside code is not rewritten when rows, columns or sheets change; names follow
those changes, so prefer a name for anything that must survive them.

The broker context exposes `get/value/values/set`, `table_schema/table_get/table_set`,
`table_insert/table_delete` and `operation_id`. Reads see staged writes. Table
values use stable column IDs; schema inspection maps names to those IDs. Table
insert returns its new decimal-string record ID. Context operations obey normal
types/constraints, field/history limits and required handlers. Unsupported
structural or recursive macro requests abort the operation even if caught by
Python. One successful macro forms one supported data Undo/Redo entry; required
handlers still run during Undo/Redo and the macro itself does not rerun.

## Inspect outcomes and cancel

Keep the operation ID before sending. `book.outcome(id)` reconciles an unknown
response and retrieves the durable scalar result/diagnostics. Do not submit a
new macro while its outcome is unknown. Exact engine retries replay receipts;
the SDK should reconcile rather than create a newly reviewed request under an
old ID. Receipts persist after service restart; execution approval does not.

`book.cancel_operation(id)` or `cancel-operation <id> --handle <existing handle>`
with `--url` requests cancellation;
inspect the final receipt afterward. For a long-running call, use another
connection to the same live service/handle. A new CLI-owned service cannot
cancel an operation in another process. The CLI uses an existing handle so its
cancellation cannot queue behind a new workbook-open request. The example's `spin` macro is a bounded
busy loop for testing this route. Cancellation reaches the active writer without
waiting behind its workbook path lock.

Workers share a two-second spawn/import/execution/broker budget and the parent
operation's five-second limit. Stdout/stderr and tracebacks retain at most 8 KiB
each; argument JSON is capped at 128 KiB, frames at 1 MiB, scalar Text/results at
64 KiB. Failed/cancelled operations expose no successful result or partial
database commit. Diagnostics distinguish Python errors, result/context failure,
startup/crash/protocol failure, timeout and cancellation. `macro.execution`
describes the worker phase; the outer `outcome` determines whether data committed.

Approved Python runs with the service user's normal capabilities. A worker is
not a secure sandbox. SQLite rollback covers context-mediated database edits;
it cannot undo file/network effects. This tier provides short atomic database
macros and cell/table change bindings (see the
[workspace guide](python-workspace-guide.md#rules-event-bindings)). Durable
external-effect queues, package installation, workbook-open and structure
events, multi-transaction macros and formula UDFs require separate contracts.
Independent SQLite edits bypass hooks and never replay skipped events.

## Importing and exporting workbooks

Whole `.xlsx` and `.ods` workbooks (up to 48 MiB packed and 1,000,000 cells)
use the large interchange path. The SDK streams files in 64-KiB chunks in both
directions, checks SHA-256 on the way, and waits up to 330 seconds for
preparation, apply and transfers; ordinary calls keep their 15-second timeout.

```python
with LocalService() as client, client.open(native_path) as book:
    summary = client.prepare_spreadsheet_file('Budget.xlsx', mode='mixed')
    print(summary['totals'], summary['report_entries'][:3])
    receipt = book.import_staged(summary)            # an empty workbook
    # book.import_staged(summary, target='add_sheets') adds the sheets instead
    written = book.export_spreadsheet_file('Budget-copy.ods', overwrite=False)
```

`prepare_spreadsheet_file` uploads the file and always releases the upload
afterwards; `local=True` instead sends its absolute path for a service on the
same machine to read. The format comes from the extension unless `format` is
given. It returns the review summary (sheets, cell counts, `report_entries`,
`totals`, a sample) and stages the cells; nothing changes until
`import_staged`, which applies the stage as one `workbook_import` operation and
returns a receipt with `imported: {sheets, cells}`. A failed preparation raises
`APIError` with the job's error (status `None`); `STALE_PLAN`,
`SHEET_NAME_CONFLICT` (with `conflicts`) and `DESTINATION_NOT_EMPTY` raise from
`import_staged` and keep the stage (a stage that is already gone is a rejected
`JOB_NOT_FOUND`; nothing was applied), so release an unused summary with
`client.discard_job(summary['stage_id'])`. `export_spreadsheet_file` runs an
export job, downloads to a temporary file beside the destination, checks its size
and SHA-256, moves it into place, and releases the export; an existing
destination needs `overwrite=True`. `client.upload`, `client.download` and
`client.release_upload`/`release_export` are the underlying primitives.

The CLI prints the grouped report and asks before applying (the question goes to
stderr); `--yes` skips the question, and without it a standard input with nothing
to answer exits 2 and imports nothing (a declined prompt exits 1). Text from the
file or the service is printed with control, format and bidi characters escaped.
`--report-json` is written atomically and refuses an existing file unless
`--overwrite` is given (for `export-spreadsheet`, `--overwrite` covers both the
destination and the report). Published files take ordinary permissions, or the
replaced file's permissions:

```sh
PYTHONPATH=service python3 -m datahorizon.cli /tmp/Budget.dhwb create
PYTHONPATH=service python3 -m datahorizon.cli /tmp/Budget.dhwb import-spreadsheet Budget.xlsx --report-json report.json
PYTHONPATH=service python3 -m datahorizon.cli /tmp/Budget.dhwb import-spreadsheet Budget.xlsx --local --yes
PYTHONPATH=service python3 -m datahorizon.cli /tmp/Other.dhwb import-spreadsheet Extra.ods --add-sheets --yes
PYTHONPATH=service python3 -m datahorizon.cli /tmp/Budget.dhwb export-spreadsheet Budget-copy.xlsx --overwrite
```

Both commands also work against `--url` with `DATAHORIZON_TOKEN`; `--local`
then names a path on the service's machine. Errors print the structured error
to stderr and exit 1.

Each report entry has a severity: *converted* (stored in another form, such
as a formula replaced by its cached value or an error value kept as text),
*approximated* (kept with a close but different meaning, such as a border color
mapped to the default), *omitted* (dropped) or *unevaluable* (a formula kept that
DataHorizon cannot calculate). The import `--mode` decides what a formula
becomes: `formulas` keeps every formula, `mixed` (the default) uses the file's
cached value only for formulas DataHorizon cannot evaluate, and `values` keeps
only cached values. Export always writes formulas, without cached values.
