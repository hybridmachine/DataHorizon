# Next five qualification and hardening increments

November 4, 2026. Execute each item as a separate implemented, reviewed and
verified PR, merging before starting the next. These extend the remaining M1
dependencies, rather than closing M1 or moving ahead to relational features.

1. **Native Qt acceptance automation.** Exercise the real REST-backed window
   with Cocoa, Windows and Xcb plugins at scale factors 1/2; check keyboard
   edit/calculation/Escape/navigation, clipboard, accessible names/table extent,
   system palette and cell geometry. Preserve JSON/screenshots in CI.
2. **Cache and RSS measurement.** Distinguish fresh-process/SQLite-cache reads
   from warm reads and OS-cache qualification; record cross-platform process
   memory for bounded and larger-than-target-memory fixtures.
3. **Bounded HTTP admission.** Bound concurrent handlers and slow socket reads,
   reject excess connections explicitly, and keep accepted operation outcomes
   and transaction ownership coherent.
4. **Python worker lifecycle.** Cover start failure, broken broker channels,
   worker resource cleanup and repeated failure/recovery without bypassing hooks.
5. **Native SQLite capture-binding comparison.** Exercise a real native capture
   API alongside the TEMP journal and record a supported scope/decision; keep
   public managed SQL unavailable pending its integration contracts.

## Native acceptance evidence

`native_acceptance` requires a native Qt platform plugin and rejects offscreen
or minimal plugins. It uses temporary native workbooks, shared REST operations
and QTest widget events. Clipboard formats/image data are preserved and restored
if the unique test content has not been superseded. It emits only generated
fixture data. Local [scale 1](evidence/native-acceptance-1.json) and
[scale 2](evidence/native-acceptance-2.json) reports have matching screenshots.
CI runs Xcb under Xvfb on Linux and native Cocoa/Windows plugins, preserving
the same reports and screenshots.

[Qt accessibility interfaces](https://doc.qt.io/qt-6.8/qaccessible.html) expose
names and table dimensions; checking them does not certify a screen reader.
Synthetic keyboard events, virtual displays and native-plugin rendering do not
qualify physical input, compositor latency, multi-monitor behavior or native
dialog interaction. Those remain explicit platform acceptance work.

## Cache and RSS evidence

`tests/cache_rss_benchmark.py` launches isolated copies of the real service with
one authenticated, benchmark-only memory route. Each process reports its own
peak RSS (`getrusage` on POSIX, native peak working set on Windows); it does not
use aggregated child-process statistics. The route and launcher are not installed.
The fixture has 100,000 sparse numeric cells and 270,000 one-KiB text cells,
inserted through ordinary callback-free SQLite during setup. Measured reads use
REST. [Local evidence](evidence/cache-rss-benchmark.json) records 100 fresh
process start/open/read samples, first viewport time, 100 warm viewports, and
driver/service peaks. CI is configured for ten samples per platform.

Fresh process startup includes format validation and integrity checks, which
can warm SQLite/OS caches. No OS-cache purge is attempted; true OS-cold
qualification remains open. The dataset exceeds the 256-MiB service target,
while the measured service stays below it. That is dataset-specific evidence,
not a limit for arbitrary input shapes or multiple engines. The desktop
benchmark now separately reports its process peak RSS for its 10,000-cell GUI
workload. [GUI evidence](evidence/desktop-rss-benchmark.json) keeps that scope
distinct from the larger service fixture.

The user authorized local verification/merge while hosted Actions are blocked
by account billing/spending limits. No successful new hosted result is claimed.
