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

The desktop launches a private, authenticated loopback service using the Python
interpreter found by CMake. DATAHORIZON_PYTHON overrides it. Normal grid operations
use REST; the GUI never opens SQLite. It only terminates a service it started.
The service remains useful without a desktop.

```sh
PYTHONPATH=service python3 -m datahorizon.service
```

The first stdout line contains the service URL and bearer token. Keep it private.
`/docs` is an interactive local explorer; `/openapi.json` describes the API.
Supply the bearer token in the explorer or SDK. External binding is disabled.
The GUI can attach using `DataHorizon --connect http://127.0.0.1:PORT` with the
DATAHORIZON_TOKEN environment variable. Attached services survive GUI shutdown.

## Everyday use

New creates a recoverable untitled native workbook. Submitted edits store
automatically. Save a copy creates a consistent snapshot at a new path. First
Save on an untitled file switches to the permanent copy; the recovery source
remains accessible through File > Recovery workbooks. Existing destinations
are rejected, even if a native save dialog offers an overwrite confirmation.
Save submits a formula-bar draft and continues once its storage outcome is known.

Enter submits the cell/source editor, Escape cancels its draft, and the address
field jumps to A1 coordinates. Changing a draft's cell or sheet asks whether to
Save, Discard or Cancel; losing focus alone retains the draft. Both source editors
preserve valid sources up to the service's 64-KiB limit. The grid fetches bounded
tiles, showing an
ellipsis until loaded. Select a rectangle for paste, fill, styles, CSV, search
or a declared list. Copy retains formulas in the application's clipboard format;
external applications receive displayed TSV. Cut retains its source until an
atomic managed Paste commits; it is restricted to the same sheet in workbooks
without formulas. An expired Cut is rejected. Cut supports full paste; use Copy
for values-only or formats-only paste. Paste values retains destination styles
and rejects error/pending calculation results. Copying an absent cell preserves
blank semantics. CSV review remains bound to the workbook, sheet, address and
state selected when the preview was requested.

If the owned service stops, File > Restart local service reopens stored inputs
and looks up any pending operation receipt without repeating hooks. Unknown
outcomes continue to block edits. The user may explicitly close after a warning;
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
or displayed values. The Issues dock shows counts and locations. Formula results
cannot be overwritten through displayed-value replacement; use formula source.
CSV import previews UTF-8 comma-separated text and imports it atomically as Text.
Dates, encodings, column typing and spreadsheet interchange remain incomplete.
Export is an explicit values-only selected snapshot and blocks on formula errors.

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

The SDK raises `UnknownOutcome` with an `operation_id` if acknowledgement is
lost, and blocks subsequent writes until `book.outcome(id)` reconciles it.
Missing receipts remain unknown; no automatic mutation retry occurs. Confirmed
transaction failures also attempt a durable rollback receipt. Workbook clients
can be context managers or closed explicitly to release their service handle;
other clients' handles remain valid. The service caps open engines and handles.

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
