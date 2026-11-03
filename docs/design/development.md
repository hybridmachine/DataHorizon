# Building and using the development preview

Implementation began October 31, 2026 after an explicit instruction to execute
the plan. Read [status and limits](implementation-status.md) before treating any
milestone as complete. The PDFs are historical design snapshots.

## Build

Requirements: CMake 3.24+, C++17 compiler, Qt 6.8+ Widgets/Network/Test, Python
3.11+ with sqlite3. The local verified configuration is Qt 6.12.0, Apple Clang
17, Python 3.14.6 and CMake 4.4.2 on Apple Silicon macOS. No pip packages are
required. Qt's other installed toolchains can be selected with CMAKE_PREFIX_PATH.

```sh
cmake -S . -B build -DCMAKE_PREFIX_PATH="$HOME/Qt/6.12.0/macos" -DCMAKE_BUILD_TYPE=Debug
cmake --build build --parallel
ctest --test-dir build --output-on-failure
open build/DataHorizon.app
```

On Windows/Linux run the generated DataHorizon executable; set the Qt prefix to
the selected kit. Test execution needs permission to bind loopback sockets.
CTest uses Qt's offscreen platform for desktop tests. These tests do not
replace native-platform keyboard, accessibility or installer acceptance.
Whole-suite CTest limits are 180 seconds for desktop and 120 for Python on
Windows, where hosted runs take roughly 84 and 39–45 seconds respectively.
Other platforms retain 90/45-second suite limits. Per-operation, hook and
individual request deadlines are independent of these test-runner limits.
Hosted macOS CI uses `macos-15`; GitHub's `macos-14` image entered retirement
brownouts and is scheduled for removal on November 2, 2026.
The numeric loopback service binds without reverse-DNS lookup, avoiding the
macOS runner's slow hostname-resolution path without increasing startup budgets.

The desktop launches a private, authenticated loopback service using the Python
interpreter found by CMake. DATAHORIZON_PYTHON overrides it. Normal grid operations
use REST; the GUI never opens SQLite. It only terminates a service it started.
The service remains useful without a desktop.
Owned desktop and SDK services invoke an absolute trusted launcher with
[Python isolated mode](https://docs.python.org/3.13/using/cmdline.html#cmdoption-I).
The launcher adds only the resolved service installation root to the interpreter's
paths; the working directory and ambient `PYTHONPATH` cannot select service
modules. Choose a trusted interpreter and install handler dependencies in its
environment. Startup uses UTF-8 streams and disables bytecode writes.

```sh
python3 -I -B -X utf8 "$PWD/service/datahorizon/launch.py"
```

The first stdout line contains the service URL and bearer token. Keep it private.
`/docs` is an interactive local explorer; `/openapi.json` describes the API.
The [OpenAPI 3.0](https://spec.openapis.org/oas/v3.0.3.html) operation request uses
action-specific schemas and examples for every advertised mutation. Select an
operation in the explorer to load its example. Replace placeholder operation IDs,
state tokens, cut state and sheet IDs with values from the reviewed workbook;
Undo/Redo need current history, and named sheets in examples must exist first.
Schemas document individual field bounds; the engine also checks combined cell
and byte budgets, ordered ranges, current state and reference/hook restrictions.
Supply the bearer token in the explorer or SDK. External binding is disabled.
The GUI can attach using `DataHorizon --connect http://127.0.0.1:PORT` with the
DATAHORIZON_TOKEN environment variable. Attached services survive GUI shutdown.
Desktop, SDK and managed CLI requests use direct connections independently of
system/application proxy settings and `*_proxy` environment variables. The SDK
uses a private [proxy-disabled opener](https://docs.python.org/3.13/library/urllib.request.html#urllib.request.ProxyHandler);
Qt sets [NoProxy](https://doc.qt.io/qt-6.8/qnetworkaccessmanager.html#setProxy).
Clients reject service redirects instead of forwarding credentials or workbook
payloads to another endpoint. A redirected mutation retains its pending ID for
reconciliation; a redirect body cannot supply a terminal operation receipt.

## Everyday use

New creates a recoverable untitled native workbook. Submitted edits store
automatically. Save a copy creates a consistent snapshot at a new path. First
Save on an untitled file switches to the permanent copy; the recovery source
remains accessible through File > Recovery workbooks. Existing destinations
are rejected, even if a native save dialog offers an overwrite confirmation.
Copying includes validation of the copied native catalog and relationships and
has a five-second cooperative budget, including busy retries. A failed or timed
out destination is preserved for inspection; choose a new path to retry.
Save submits a formula-bar draft and continues once its storage outcome is known.

Enter submits the cell/source editor, Escape cancels its draft, and the address
field jumps to A1 coordinates. Changing a draft's cell or sheet asks whether to
Save, Discard or Cancel; losing focus alone retains the draft. Both source editors
preserve valid sources up to the service's 64-KiB limit. The grid fetches bounded
display previews, showing an ellipsis until loaded, and loads complete long
inputs separately before enabling editing. Select a rectangle for paste, fill, styles, CSV, search
or a declared list. Copy retains formulas in the application's clipboard format;
external applications receive displayed TSV. Cut retains its source until an
atomic managed Paste commits; it is restricted to the same sheet in workbooks
without formulas. An expired Cut is rejected. Cut supports full paste; use Copy
for values-only or formats-only paste. Paste values retains destination styles
and rejects error/pending calculation results. Copying an absent cell preserves
blank semantics. CSV review remains bound to the workbook, sheet, address and
state selected when the preview was requested.
Sheet confirmations and other reviewed operations retain their original
workbook, sheet and state. If a background refresh changes that context while a
dialog is open, the operation stops and asks for a new review of the target.
Rejected in-cell edits are restored in the formula bar for correction. An active
in-cell draft survives a service interruption at its original coordinates and
must be reviewed against reopened inputs before applying it.
Copy/Cut supports up to 10,000 selected cells in one consistent read, subject to
the 4-MiB response limit. Oversized clipboard reads fail without replacing the
clipboard; select a smaller range. Viewport previews are never clipboard data.
An older asynchronous Copy/Cut reply cannot replace a newer clipboard command
or newer clipboard content, including a copy made in the source editor or
another application.

If the owned service stops, File > Restart local service reopens stored inputs
and looks up any pending operation receipt without repeating hooks. Unknown
outcomes continue to block edits. Unsubmitted formula-bar drafts retain their
original sheet and coordinates; review the reopened input before applying the
draft. Restart reopens the current workbook, including when the application was
launched with a different workbook path. The user may explicitly close after a warning;
the application first saves a private JSON record containing the workbook path
and operation ID under the application-data recovery directory. Reopen the
workbook and use CLI `outcome ID` or the API receipt lookup before retrying. A
missing receipt remains unknown; closing never claims rollback or discards
already stored data.

For Supplies, enter these inputs (blank row 5):

| Row | A | B | C | D |
| --- | --- | --- | --- | --- |
| 1 | Item | Quantity | Unit price | Amount |
| 2 | Notebook | 2 | 3.5 | =B2*C2 |
| 3 | Pen | 4 | 1.25 | =B3*C3 |
| 4 | Folder | 3 | 2 | =B4*C4 |
| 6 | | | Total | =SUM(D2:D4) |

Use Two decimal places on D2:D6. The initial total is 18.00. Change B3 to 6
for 20.50. Select A1:D4, choose Data > Sort / filter selected list, confirm a
header, enable a numeric filter on source column 2 with `> 2`. The projection
shows Pen and Folder; Clear list view restores the sheet. Sorting is a view,
not a physical permutation of stored cells. Row headers retain source row
numbers. Single-cell edits use those coordinates; range operations require
clearing the projection. After an edit, an active filter is reevaluated.

Find/Replace All is case-sensitive within the selected rectangle, against source
or displayed values. The Issues dock shows counts and locations. Display
replacement preserves Text typing and leading zeros; identical replacements do
nothing. Numeric, boolean and formula results require stored-source replacement.
Replacement expansion is checked against the 64-KiB UTF-8 source limit before
allocating the result, including any retained apostrophe text escape.
CSV import previews UTF-8 comma-separated text and imports it atomically as Text.
Dates, encodings, column typing and spreadsheet interchange remain incomplete.
Export is an explicit values-only selected snapshot and blocks on formula errors.
CSV import captures its destination before the file picker and checks it again
after file selection and confirmation. A refreshed or removed sheet cancels the
import; an active cell draft must first be saved, discarded or cancelled. File
reads are limited to 3 MiB even if a file grows after its initial size check.

Export cannot overwrite the open native workbook, including paths that identify
the same file through a filesystem alias. CSV previews are capped at 10,000
records and 10,000 fields; exports are capped by the complete 4-MiB JSON response.

## CLI and Python

```sh
PYTHONPATH=service python3 -m datahorizon.cli /tmp/example.dhwb create
PYTHONPATH=service python3 -m datahorizon.cli /tmp/example.dhwb set A1 '=SUM(7,13.5)'
PYTHONPATH=service python3 -m datahorizon.cli /tmp/example.dhwb read A1
```

File mode starts an owned service and executes through REST. `--url` attaches
to a service and reads DATAHORIZON_TOKEN. `apply` accepts a JSON operation from
stdin for bulk work; preserve its operation_id for outcome lookup. `direct-sql`
is explicitly independent SQLite access and reports handlers=false.
`--readonly` also applies to direct SQL: SQLite opens the existing file in
read-only mode, and rejected writes return a structured JSON error.
Direct SQL results encode SQLite BLOB values as
`{"type":"blob","base64":"AAH/"}` (the example represents bytes `00 01 FF`).
Other SQLite values keep their ordinary JSON scalar representation; nonfinite
numbers are rejected with a structured error.
Direct SQL prepares its JSON output within the transaction and prints results
only after commit succeeds. Commit or serialization failure returns an error
without printing successful rows. The CLI explicitly starts the transaction,
including for `WITH`-prefixed writes and DDL, instead of relying on
[Python's implicit transaction detection](https://docs.python.org/3.13/library/sqlite3.html#transaction-control-via-the-isolation-level-attribute).
User transaction controls, savepoints, `PRAGMA`, `ATTACH` and `DETACH` return
`UNSUPPORTED_DIRECT_SQL`; commands requiring autocommit, including `VACUUM`,
also fail without execution. Use an independent SQLite tool for those commands.
Ordinary external SQLite connections remain unrestricted by this CLI policy.
Truncated or malformed HTTP responses also
return structured CLI errors; transmitted mutations retain unknown outcomes
through the SDK receipt contract.
Once a command has a valid result, later workbook/service cleanup failures do
not replace it. The CLI returns success with the original JSON result and emits
a separate `CLEANUP_FAILED` diagnostic on stderr. For mutations, that diagnostic
retains `operation_id` and `operation_outcome`; it never reports the confirmed
write as rejected. An unconfirmed acknowledgement still exits with an unknown
outcome and its operation ID, so scripts must reconcile before another write.
Output failures use `OUTPUT_FAILED` with `phase=output` and a nonzero exit. For a
confirmed mutation, stderr retains its operation ID, known outcome and receipt;
a failed pipe does not mean the write rolled back. Direct SQL reports its
confirmed commit and direct mode without inventing a managed operation ID.

The SDK raises `UnknownOutcome` with an `operation_id` if acknowledgement is
lost, and blocks subsequent writes until `book.outcome(id)` reconciles it.
Missing receipts remain unknown; no automatic mutation retry occurs.
Retries that fail before writer-lock admission also remain unknown, since the
original request can still commit. A transmitted mutation needs a terminal
response carrying its matching operation ID before desktop/SDK writes resume;
generic HTTP rejection alone cannot clear it. The SDK rejects read-only writes
and requests over 4 MiB locally before creating a pending operation.

Confirmed transaction failures retain the writer lock while rolling back data
changes and committing their failure receipt. An unconfirmed receipt remains unknown and
requires lookup before retry. Workbook clients
can be context managers or closed explicitly to release their service handle;
other clients' handles remain valid. The service caps open engines and handles.
A confirmed SDK close is idempotent. Closed clients reject later reads, refreshes,
lookups and writes locally, without creating a pending mutation ID; open a new
handle to continue. A malformed close acknowledgement does not mark it closed.
Slow construction, metadata loading and close operations serialize only the
same workbook path. The shared registry remains available for unrelated
workbooks; concurrent opens reserve capacity and share one engine per path.

SDK context managers preserve a primary exception if cleanup also fails; its
`cleanup_errors` details and exception notes report the secondary failure. When
the body succeeds but cleanup fails, the SDK raises `CleanupError` with
`phase=cleanup` and the last confirmed receipt in `body["last_operation"]`, when
available. This error does not reject earlier operations. Pending unknown writes
retain their `UnknownOutcome` guard instead of being relabeled as cleanup errors.
If receipt confirmation succeeds but workbook metadata cannot refresh, the SDK
raises `MetadataRefreshError`. Its `receipt`, operation ID and outcome remain
available; writes stay blocked until `refresh()` or `outcome(id)` refreshes valid
metadata. Retrying `outcome(id)` reuses the immutable confirmed receipt. Closing
the handle is also safe once its operation outcome is known. A failed metadata
refresh never turns a committed receipt into a rejected or unknown mutation.

```python
from datahorizon.client import LocalService

with LocalService() as client:
    book = client.open('/tmp/example.dhwb')
    book.set(2, 1, '00123', kind='text')
    print(book.read(r1=1, c1=1, r2=2, c2=1))
```

Activate only trusted service-side code with `--handler path.py` (desktop,
service or CLI), or `LocalService(handler=path)`. Nothing in a workbook or CSV
can activate it. `tests/fixtures/validation_hook.py` is an opt-in fixture: A1
must be nonnegative numeric and B1 becomes twice A1. It is not a product rule.
The worker uses context.get/value/set in the parent transaction. A failure,
load error, timeout or recursion limit rolls back all changes. Workers are not
security sandboxes, and transactional code must not perform external effects.

## Install smoke and measurements

```sh
cmake --install build --prefix "$PWD/build/package"
PYTHONPATH=service python3 tests/benchmark.py
```

The install step deploys Qt and copies service sources. Use an absolute install
prefix: Qt 6.8 deployment requires an absolute path when writing qt.conf on Linux
and Windows. Python is a separately
installed dependency, not a bundled runtime. The resulting package is a local
development artifact, not a signed/notarized or clean-machine-qualified release.
GUI GPLv3 text is in desktop/COPYING. Other component licensing remains open;
no repository-wide license grant is implied.
`python3 tests/package_smoke.py build/package` checks the installed native app;
on headless Linux run it with `xvfb-run -a`. Offscreen Qt is used for the regular
desktop tests, not required in the deployed end-user bundle.
