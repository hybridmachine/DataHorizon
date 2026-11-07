# Development toolchain and packaging baseline

November 4, 2026. This records the M1 development baseline and verification
matrix. It does not qualify a public release or select a production service
framework, bundled Python runtime, or licenses for the service/SDK.

| Area | Baseline | Evidence |
| --- | --- | --- |
| Desktop | Qt Widgets/Network/Test, C++17; Qt 6.8+ build requirement | Hosted Qt 6.8.3, local Qt 6.12.0; Qt contract tests and grid measurements |
| Build | CMake 3.24+, native C++17 compiler | Configure records actual CMake, generator, compiler ID/version, system and Qt version |
| Service/SDK/CLI | Python 3.11+ with stdlib sqlite3 | All Python contracts run on Python 3.11 in Windows/macOS/Linux CI; desktop jobs use Python 3.13 |
| Complete experiment suite | SQLite 3.35+ for RETURNING fixtures | Runtime reports record the actual SQLite library; this is not a new engine-side version gate |
| Hosted systems | ubuntu-24.04, macos-15, windows-2022 | Native build/test/install smoke and separate minimum-Python jobs |
| Local system | Apple Silicon macOS, Apple Clang 17, Qt 6.12.0, Python 3.14.6 | [Local build/runtime report](evidence/toolchain-benchmark.json) |
| Packaging | CMake Qt deployment plus service sources and GUI GPLv3 text; absolute install prefix | Existing installed-native-application smoke on all three systems |

The runner's compiler/tool versions are reported rather than guessed from image
names. Hosted runner labels describe test environments, not minimum end-user OS
versions or native interaction certification. Additional combinations need the
same contract, measurement and install checks.

## Repeatable evidence

CMake writes `build-toolchain.json` in the build directory without user paths
or credentials. After the Qt benchmark, `tests/toolchain_report.py` verifies that
the configured interpreter, Qt version and platform match the measured runtime,
checks C++17 and the absolute configured prefix, and inspects the generated CTest
suite budgets. It also verifies the offscreen focused-input flag and bounded
grid/no-edit contract. CI preserves `toolchain-benchmark.json` beside the other
M1 measurements. These are checks of actual artifacts, not unit tests of a
duplicated configuration.
CI passes the setup action's explicit
[interpreter path output](https://github.com/actions/setup-python/blob/v6/action.yml)
to CMake so another preinstalled interpreter cannot select the desktop service.
Native Windows separators are normalized before embedding that path in the C++
launcher definition. CI reproduced unrecognized C++ escapes from the explicit
`C:\...\x64\python.exe` spelling and a desktop startup timeout before this fix;
the same selected interpreter remains the CTest/SDK interpreter.

The minimum-Python jobs run the complete engine, service, SDK, CLI, SQL capture,
execution and native representation contracts under Python 3.11. They preserve
`minimum-runtime-*` reports with the actual interpreter, SQLite and platform.
No Qt install or third-party Python packages are needed in those jobs.

PR updates run one platform matrix plus the minimum-runtime matrix. Main pushes
verify merged heads, manual dispatch remains available, and a newer run cancels
the superseded run for the same ref. Branch-push duplicates are removed.

Desktop whole-suite budgets are 300 seconds on Windows, 180 on macOS and 90 on Linux;
Python suites are 120 on Windows and 45 on macOS/Linux in CTest. The standalone
minimum-runtime CI job has a ten-minute job ceiling. Operation, hook and HTTP
deadlines remain separate and unchanged. Timings are evidence rather than
variable-host performance gates.
The Windows budget follows two 180-second timeouts and a passing 171.77-second
suite, while the focused startup check completes in six seconds. Keep that
startup check and plugin/failure diagnostics for early failure localization.

## Distribution limits and next dependency

Python is still an external dependency. Installed smoke does not establish a
clean-machine installer, signing/notarization, runtime redistribution or native
accessibility/keyboard/high-DPI qualification. GUI GPLv3 remains the selected
license; other component licenses and dependency distribution obligations stay
open before distribution. The probe/report scripts are not installed.

The five bounded M1 experiment slices now have repeatable evidence. M1 remains
open for native-platform interaction/performance acceptance, cold-cache/RSS
qualification, production framework/runtime decisions and any selected native
capture binding. The restricted managed SQL matrix and M4/M7 completion gates
remain in force; no full-milestone or public-preview completion is claimed.

The November 7 [local qualification/package driver](workbook-next-five.md#local-qualification-and-packaging--november-7-2026)
adds repeatable local evidence while hosted credits are exhausted. Installed
runtime resolution no longer depends on the developer's absolute interpreter
or source checkout. A relocated smoke tests that distinction explicitly, and a
verified development archive retains checksums, executable modes and framework
symlinks. Python redistribution and the release/distribution gates above remain open.
