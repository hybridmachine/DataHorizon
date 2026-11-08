# Building and using the development preview

Implementation began October 31, 2026 after an explicit instruction to execute
the plan. Read [status and limits](implementation-status.md) before treating any
milestone as complete. The PDFs are historical design snapshots.

## Build

Requirements: CMake 3.24+, C++17 compiler, Qt 6.8+ Widgets/Network/Test, Python
3.11+ with sqlite3. The local verified configuration is Qt 6.12.0, Apple Clang
17, Python 3.14.6 and CMake 4.4.2 on Apple Silicon macOS. No pip packages are
required. Qt's other installed toolchains can be selected with CMAKE_PREFIX_PATH.

The [toolchain/packaging baseline](toolchain-packaging-baseline.md) records the
actual compiler, CMake, Qt, interpreter and SQLite versions. Hosted desktop
jobs use Qt 6.8.3/Python 3.13, with separate complete Python 3.11 contract jobs
on all three systems. The complete experiment suite needs SQLite 3.35+ for its
RETURNING fixtures; this does not add an engine-side version gate.

```sh
cmake -S . -B build -DCMAKE_PREFIX_PATH="$HOME/Qt/6.12.0/macos" -DCMAKE_BUILD_TYPE=Debug
cmake --build build --parallel
ctest --test-dir build --output-on-failure
open build/DataHorizon.app
```

On Windows/Linux run the generated DataHorizon executable; set the Qt prefix to
the selected kit. Test execution needs permission to bind loopback sockets.
The selected Shared Cell application icon is embedded in Qt, included in the
macOS bundle and Windows executable, and installed with a Linux desktop entry
and hicolor PNGs. Prebuilt assets and regeneration instructions are in
[desktop/icons](../../desktop/icons/README.md); normal builds need no image tools.
CTest uses Qt's offscreen platform for desktop tests. These tests do not
replace native-platform keyboard, accessibility or installer acceptance.
Whole-suite CTest limits are 300 seconds for Windows desktop, 180 for macOS
desktop, and 120 for Python on Windows. The Windows baseline run took 171.77
seconds after two 180-second suite timeouts. Hosted macOS twice exceeded the former
90-second desktop budget without an assertion failure; identical runs passed.
Linux desktop retains 90 seconds and macOS/Linux Python retain 45 seconds.
Per-operation, hook and
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

## Durable job recovery and close

Job create requests now have separate typed-edit, CSV, Find and list OpenAPI
schemas; append/start/resume/status/cancel/release use action-specific bodies.
The API explorer offers their examples. Stage edits, append bounded chunks, seal
and inspect the returned plan. Replace example state/digest/execution tokens with
those returned by workbook info and the inspected job before explicit execution.
`tests/requirements-openapi.txt` supplies optional build-only validators; the
service runtime still needs no pip packages.

After a lost execution reply, inspect the retained job ID and native batch
receipts before deciding whether to resume. A missing receipt remains unknown.
Version-2 sidecars store a durable execution namespace; releasing/recreating a
friendly name never reuses earlier receipt IDs. Legacy upgrade/conflict semantics
are documented in [native storage](native-format-v1.md#operation-job-sidecars).

The desktop fetches current workbook metadata before execution review. A sealed
plan whose state changed, including after reopen, offers explicit reviewed resume
against current inputs. An input/session/job-identity change during review refuses
admission rather than retargeting the operation. SDK/CLI callers explicitly supply
the reviewed current state and digest to resume; an execution identity can also
bind the particular v2 journal incarnation.

Closing a window with an active job requests cancellation asynchronously, inspects
terminal status, then releases this window's workbook handle. A cancellation reply
that still says running is not proof of termination. Committed batches remain
committed. Independent services and other client handles remain usable; owned
services are stopped only after cleanup or an explicit unresolved-close choice.
If cancellation/status/release cannot be confirmed, a private `job-*.json` recovery
record retains the native path, handle, job identity and last known status. Staying
open preserves inspection; closing with the warning does not claim rollback or
confirmed cancellation and does not delete the sidecar.

Grid and record paste share bounded TSV/native decoding. CRLF and LF normalize
consistently, one terminal record separator is removed, and intentional empty
rows/fields remain. Grid plain text/native clipboard limits remain 4 MiB; record
plain text remains 3 MiB, with the same 10,000-cell bound. Native source coordinates,
types, Cut restrictions and omitted/error-value guards remain in force.

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
field jumps to A1 coordinates. Confirming an unchanged input preserves its native
kind, including NULL versus absence and automatic numbers with Text formatting;
it does not run input-change handlers. Changing a draft's cell or sheet asks whether to
Save, Discard or Cancel; losing focus alone retains the draft. Both source editors
preserve valid sources up to the service's 64-KiB limit. The grid fetches bounded
display previews, showing an ellipsis until loaded, and loads complete long
inputs separately before enabling editing. Select a rectangle for paste, fill, styles, CSV, search
or a declared list. Copy retains formulas in the application's clipboard format;
external applications receive displayed TSV. Cut retains its source until an
atomic managed Paste commits; same-sheet references follow the bounded
[formula-aware move contract](editing-lists-next-ten.md#formula-aware-cut). An expired Cut is rejected. Cut supports full paste; use Copy
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
CSV import offers explicit UTF-8, UTF-16 with BOM, Windows-1252 and Latin-1
decoding, plus comma/semicolon/tab/pipe delimiters. Strict decoding errors stop
the preview. Text is the default. In the preview, Column types offers Number,
Boolean, ISO/M-D-Y/D-M-Y native dates, and a first-record header
option. The service reports failed and ambiguous conversions; Apply stays
disabled until all conversions succeed. The reviewed typed cells use one managed
edit, retaining destination styles, history and required hooks. Ragged records
are not padded; empty converted fields become blank. A failed conversion can be
corrected explicitly to Text and reviewed again. Native dates/times use explicit ISO kinds; locale number parsing remains outside this tier.
Export is an explicit values-only selected snapshot and blocks on formula errors.
CSV import captures its destination before the file picker and checks it again
after file selection and confirmation. A refreshed or removed sheet cancels the
import; an active cell draft must first be saved, discarded or cancelled. File
reads are limited to 3 MiB even if a file grows after its initial size check.
Decoded UTF-8 text is also limited to 3 MiB; request/response JSON remains
bounded by 4 MiB, including base64 overhead. Select a smaller file if needed.

Export cannot overwrite the open native workbook, including paths that identify
the same file through a filesystem alias. CSV previews are capped at 10,000
records and 10,000 fields; exports are capped by the complete 4-MiB JSON response.

## CLI and Python

Use Edit → Enter date/time… for an explicit ISO value in the active cell. The
service validates the selected date/time/datetime kind; ordinary entry keeps
date-looking text as Text. Existing typed cells retain their kind when edited.
REST `edit` cells and SDK `book.set(..., kind="date")` accept the same kinds;
CLI uses `set A1 2026-11-07 --kind date`. Time and datetime require seconds,
optional 1–6 fractional digits and no timezone offset. See the
[date/time contract](date-time-next-ten.md).

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
PYTHONPATH=service python3 tests/benchmark.py --formula-only --output build/formula-benchmark.json
python3 tests/sqlite_capture_experiment.py --output build/sqlite-capture-benchmark.json
PYTHONPATH=service python3 tests/service_execution_experiment.py --output build/service-execution-benchmark.json
PYTHONPATH=service python3 tests/native_storage_experiment.py --output build/native-storage-benchmark.json
QT_QPA_PLATFORM=offscreen build/desktop_benchmark build/desktop-benchmark.json
```

The formula harness reads independent 16/64/128-cell chains from their tails in
100 fresh REST snapshots, checks the 129-cell limit, and measures edits followed
by fresh reads. `--samples` changes the default 100 measurements. The full run
also retains the sparse/dense/large-disk fixture; `--formula-only` skips it.
Peak RSS is reported on POSIX and explicitly null on Windows.

The isolated [SQLite capture experiment](sqlite-capture-experiment.md) compares
known-target edits with a bounded connection-local trigger journal, verifies
typed old/new images against independent snapshots, and records incomplete
RETURNING/recursive-trigger controls. It needs no service or pip dependencies
and does not expose managed SQL. Its 16 contract tests run in normal discovery;
CI preserves the report with the other M1 measurements.

The [service/Python comparison](service-execution-experiment.md) runs identical
small engine routes through threaded HTTP and serial WSGI, plus spawned-worker
and inline timing baselines. It verifies generated-edit rollback after failure,
crash and timeout, and durable receipt reconciliation after disconnect/reopen.
Its default is 20 timing samples per arrangement. Inline is only an isolated
fast-code baseline, never a service execution option.

The [native storage experiment](native-storage-experiment.md) verifies input
kinds, identity/styles, snapshot/reopen, external refresh without handler replay,
stale writes and format boundaries. It compares four 10,000-cell physical
fixtures with 100 warm indexed reads; its rowid variant is not native format v1.

The desktop harness measures 100 scrolling viewports and 100 distant jumps,
synthetic address input during reads, event-loop gaps, and range-response counts
against its actual service and QTableView. Run without `QT_QPA_PLATFORM=offscreen`
to measure the native platform plugin. On Windows the executable is
`build/Release/desktop_benchmark.exe`. It records single-process first-viewport
time, not a cold OS-cache distribution or physical/compositor latency. Timing
targets are reported without making variable hosted-runner timings pass/fail
gates. CI retains per-platform JSON artifacts named `m1-measurements-*`.

The [frozen-row display probe](grid-frozen-row-experiment.md) adds 100 shared-cache
scroll/horizontal samples with header/value/geometry and zero-edit checks. It
records cache charges and saves a `*-frozen-row.png` screenshot beside the JSON.
CI preserves those screenshots too. This read-only probe is not installed or
exposed as a Freeze command; M4 interactions and view persistence remain open.

After measuring the grid, run `python3 tests/toolchain_report.py --build build --output build/toolchain-benchmark.json`.
Use `--grid-report PATH` for a report stored outside build, or `--runtime-only`
to record the minimum-runtime job without a Qt build. PR updates and main
pushes run CI; redundant branch-push runs are omitted and newer runs supersede
older runs for the same ref.

Whole-row/column edits use `rows_insert`, `rows_delete`, `columns_insert` or
`columns_delete` with a sheet name/ID, 1-based `start`, and `count` (default 1).
For example, `book.apply("rows_insert", sheet="Sheet1", start=3, count=2)`
inserts two rows before row 3. The managed CLI accepts the same fields through
`apply` JSON. The Sheet menu applies these operations to the selected row or
column span, including whole-header selections. The full span moves, even when
only part of a row/column is selected. Confirmation explains history clearing;
newly broken references appear with their locations in Issues. Required handlers
and unsupported formula syntax reject atomically. Clear projected list views
first. See the initial contracts for the ordinary-operation and grid-edge limits.

The install step deploys Qt and copies service sources. Use an absolute install
prefix: Qt 6.8 deployment requires an absolute path when writing qt.conf on Linux
and Windows. Python is a separately
installed dependency, not a bundled runtime. The resulting package is a local
development artifact, not a signed/notarized or clean-machine-qualified release.
GUI GPLv3 text is in desktop/COPYING. Other component licensing remains open;
no repository-wide license grant is implied.
The optional native capture experiment has a separate pinned test dependency;
see [its isolated setup and evidence](native-capture-binding.md). It is not
installed by the application or base runtime tests.
`python3 tests/package_smoke.py build/package` checks the installed native app;
on headless Linux run it with `xvfb-run -a`. Offscreen Qt is used for the regular
desktop tests, not required in the deployed end-user bundle.

## Interchange and recovery

File → Import… accepts CSV, TSV, TXT, XLSX and ODS, detecting the type from the
filename extension without a separate format choice. The picker filters to
supported files; unsupported extensions also reject after selection. TSV defaults
to Tab while CSV/TXT default to Comma, with the existing encoding/delimiter review.
XLSX/ODS retain sheet/input options and conversion review.

File → Export… opens one save dialog with a Format dropdown for CSV, XLSX or ODS.
The filename follows the selected format, including a conflicting supported
suffix; normalization happens before overwrite confirmation. The Qt file dialog
keeps this dropdown available on all platforms, following Qt's
[documented filters and suffix behavior](https://doc.qt.io/qt-6.8/qfiledialog.html). CSV retains its values/loss
review; spreadsheet exports retain their formula/value choice and conversion
report. Imports target the selected cell and exports use the selected rectangle.
See [the exact input/formula/style tiers](interchange-next-five.md). CSV/XLSX/ODS
preparation has cancellable progress; Apply remains one managed operation with
required hooks and a durable receipt. The independent openpyxl/odfpy tools in
`build/interchange-venv` are verification-only, not service dependencies.

`PYTHONPATH=service python3 -m datahorizon.cli /path/book.dhwb inspect` performs
read-only native metadata/integrity inspection through REST without activating
code or repairing the file. Qt exposes the same operation in the File menu.
Preserve the native workbook and operation ID when an outcome remains unknown.

## Repeatable local qualification and development archive

Hosted Actions currently have no credits. Run the local driver on each available
platform using a **new** output directory:

```sh
python3 tests/qualify_preview.py --build build --config Debug --output-dir build/qualification-candidate
```

Use `--config Release` for a Release/multiconfig build; on headless Linux, run the
command under `xvfb-run -a`. It builds/tests, runs native Qt at scales 1/2, installs,
checks relocation and missing-service failure, and creates an inventoried local
archive. Inspect `qualification.json` and step logs. Source changes during the
run invalidate its candidate. See the [scope and remaining gates](workbook-next-five.md#local-qualification-and-packaging--november-7-2026).
Python remains separately installed. Installed apps select a trusted override,
an explicitly staged runtime or Python on PATH; the build's absolute development
interpreter/source paths are not installed fallbacks. This is local automation
and a development archive, not signing, a clean-machine installer or release approval.

The qualification/archive tools also require Git on PATH for source provenance.
Source fingerprints use Git's clean conversion and stable relative-path ordering
so ordinary LF/CRLF checkout differences do not invent different candidates.
