# Preview qualification increments

Authorized November 8, 2026. Each task uses a separate branch, commit, pushed PR,
verification/fixes and merge. Completion requires evidence for the task's stated
scope; unavailable native/manual/distribution evidence stays pending. The existing
M0–M8 acceptance criteria and managed/direct access contract remain unchanged.

The user subsequently limited this batch to macOS and selected GPLv3 for the
service, CLI and SDK as well as the GUI. Windows/Linux acceptance (task 7) is
explicitly deferred until work can be performed on those systems. Existing CI
coverage remains enabled; its failures are not represented as qualified results.

| Task | Deliverable | Completion evidence |
| --- | --- | --- |
| 1 | Qualify the merged implementation on Windows, macOS and Linux; fix hosted failures and inspect artifacts | Full contract, native-plugin and installed smoke results tied to a source candidate |
| 2 | Improve large native workbook opening without relaxing the five-second deadline | Original 270,000-long-text-row boundary and validation/corruption regressions |
| 3 | Complete cache/memory/capacity measurements | Separate fresh-process, warm and genuinely OS-cold evidence; workload-specific RSS and temporary disk costs |
| 4 | Automatic background calculation for visible cells | Bounded scheduling, cancellation and state/generation-bound publication |
| 5 | Extend end-to-end failure recovery | Client-visible outcomes under disk, permission, sidecar and process failures |
| 6 | Complete native macOS interaction acceptance | Physical keyboard/clipboard, OS dialogs and lifecycle walkthrough |
| 7 | Complete native Windows/Linux interaction acceptance | Platform-specific input, dialogs, high DPI, appearance and Supplies walkthrough |
| 8 | Screen-reader workflows | Named controls/grid state and real VoiceOver/Windows/Linux reader acceptance |
| 9 | Production runtime and distribution baseline | Documented service/runtime decision, bundled Python, service/SDK licensing and dependency notices |
| 10 | Clean-machine installers | Installer artifacts, available signing/notarization and clean-system acceptance with published limits |

## Task 1: hosted baseline

The inherited PR #105 run and its merged-head run did not qualify all platforms.
Linux's CSV chooser fixture raced asynchronous filename selection. The macOS
toolchain reporter still expected superseded suite budgets, and the merged run
also hit the large-file opening deadline in the RSS workload. Windows's Qt
failure lost console output, while a single-slot HTTP fixture sometimes sent its
next request before bounded connection teardown released admission capacity.

The first increment reapplies the absolute CSV filename until selected, preserves
Qt's text log as a failure artifact, synchronizes the HTTP fixture's post-response
assertions with capacity release and aligns the reporter with CMake's actual
suite budgets. Application operation, hook and HTTP deadlines are unchanged.
The macOS job in [run 42545542412](https://github.com/hybridmachine/DataHorizon/actions/runs/42545542412)
passes the complete suites, experiments, toolchain report, native Cocoa checks at
scales 1/2 and installed-app smoke. Downloaded JSON artifacts and both native
screenshots were inspected. Local CTest passes 205 Qt checks and 451 Python tests
(19 optional skips; native capture is separately covered by hosted checks).
[Qualification record](evidence/task1-macos-qualification.json) identifies the
tested source. Task 1 is complete for the user's revised macOS automation scope;
physical input, VoiceOver and distribution are later tasks, and Windows/Linux
qualification is deferred. Large-file capacity remains workload-specific.

## Task 2: native opening

[The storage/validation change](native-opening-performance.md) keeps all opening
checks and the five-second budget, combines cell checks into one scan and uses
8-KiB pages for newly created workbooks. The 270,000-long-text-row comparison
preserves every logical row while reducing the fixture from 1,263,939,584 to
316,071,936 bytes. Five optimized opens pass, with a 1,187.10-ms worst sample;
four of five legacy-layout opens time out. These are uncontrolled OS-cache
measurements, not universal capacity or OS-cold evidence. Existing files are
unchanged; explicit independent SQLite copies are documented.

The isolated Python suite runs 453 tests successfully (19 existing optional
skips), including new late coordinate/orphan/temporal damage and storage/copy
regressions. Combined CTest passes 205 Qt checks and 453 Python tests (19 optional
skips). Relocated installed-app smoke passes. The updated 100,000-row sort/job
workload verifies every row, bounded dependency reuse/invalidation, cancellation
and resume on a database larger than 256 MiB, with 51,412,992-byte service peak
RSS and 9.485-ms cancellation confirmation.

Hosted macOS [run 42546711704](https://github.com/hybridmachine/DataHorizon/actions/runs/42546711704)
passes all contracts, experiments, native Cocoa scales 1/2 and installed smoke.
The downloaded native JSON and screenshots were inspected. A local native
activation/Tab-order probe failed on this desktop session; it remains an explicit
task 6 acceptance concern, not a passing local result. The hosted native probe
passes that same check. The [verification record](evidence/task2-verification.json)
identifies source and outcomes. Task 2 is complete within this documented native
storage tier; existing large legacy-layout files may still hit the opening budget
until explicitly copied, and broader cold-cache/platform gates remain open.

## Task 3: capacity measurements and remaining gate

[The macOS driver](macos-capacity-qualification.md) now distinguishes fresh
processes, warm reads and required OS cache eviction. At the frozen implementation,
100 fresh/warm samples, 100-sample sparse/dense/distant/edit/formula shapes and
bounded grid/cache/RSS measurements pass. Full native snapshot and staged-job
disk costs are reported separately, with release cleanup verified. The two
measurement-provenance regressions pass. PR/push CI follows the user's macOS
focus; manual `platforms=all` retains the deferred matrices.

True OS-cold evidence remains blocked: ordinary purge is denied by macOS, and
`sudo -n /usr/sbin/purge` reports that a password is required. The actual failed
attempt is saved with `os_cold_qualified=false`; task 3 and M1 cannot be declared
fully complete until an administrator-enabled run passes. All other evidence is
workload-specific, with no universal memory/latency claim.

## Task 4: automatic viewport calculation

[The bounded viewport scheduler](viewport-calculation.md) delivers cached inputs
before pending formula values, calculates visible/frozen tiles through REST,
cancels obsolete admitted work and publishes only matching state/generation
results. Per-stream generations isolate automatic and explicit calculations.
Local combined verification passes 219 Qt checks and 457 Python tests (19 optional skips). Hosted macOS [run 42551536569](https://github.com/hybridmachine/DataHorizon/actions/runs/42551536569) passes all three jobs, including native Cocoa scales 1/2 and relocated installed smoke. Downloaded native JSON and screenshots were inspected. The [source record](evidence/task4-verification.json) identifies the tested implementation. Task 4 is complete for this bounded tier; broader formula and native/manual gates remain open.

## Task 5: failure recovery

[The additional failure tier](macos-failure-recovery.md) adds real managed-client
permission and abrupt-process-exit checks, real protected-export failures and
cancellable native opening/post-copy validation. The new regressions and the
private 64-MiB disk-image ENOSPC run pass locally, with native bytes/counters and
unknown receipt IDs preserved. Combined CTest passes 220 Qt checks and 462 Python tests (19 optional skips). Hosted macOS [run 42552114385](https://github.com/hybridmachine/DataHorizon/actions/runs/42552114385) passes all three jobs, including native Cocoa scales 1/2 and installed smoke. Downloaded JSON and screenshots were inspected. The [verification record](evidence/task5-verification.json) and [real filesystem report](evidence/task5-real-filesystem.json) retain source and limits. Task 5 is complete within this failure tier; physical power loss and broader APFS durability remain outside its evidence.


## Task 6: native macOS interaction gate

[Development bundle signing](macos-native-interaction.md) now repairs and verifies
resource signatures, with a real damaged-resource regression. Native activation
and Tab diagnostics remain strict. Native UI automation timed out even with the
repaired signature; physical interaction and native OS dialog/lifecycle evidence
remain pending. This increment does not close task 6 or M7.

## Task 7: deferred platforms

Windows and Linux native acceptance is deferred by the owner's explicit scope
decision. It will be performed on those respective systems after more features.

## Task 8: accessible cell-state increment

[Bounded source-address descriptions and keyboard result inspection](macos-accessible-cell-state.md)
distinguish loading, pending, computed, blank and failed values. Actual VoiceOver
interaction and bounded full-grid accessibility enumeration remain open; this
increment does not close task 8 or the screen-reader acceptance gate.
