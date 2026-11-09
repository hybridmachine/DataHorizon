# Preview qualification increments

Authorized November 8, 2026. Each task uses a separate branch, commit, pushed PR,
verification/fixes and merge. Completion requires evidence for the task's stated
scope; unavailable native/manual/distribution evidence stays pending. The existing
M0–M8 acceptance criteria and managed/direct access contract remain unchanged.

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
Hosted reruns and inspection are required before marking task 1 complete. The
large-opening failure may require task 2 before the whole platform matrix closes.
