# Next five qualification and hardening increments

November 4, 2026. Execute each item as a separate implemented, reviewed and
verified PR, merging before starting the next. These extend the remaining M1
dependencies, rather than closing M1 or moving ahead to relational features.
All five implementations now have local completion evidence below; hosted
platform qualification remains pending the recorded billing exception.

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

## HTTP admission

The stdlib development service now bounds admitted handler threads to 16 with a
nonblocking semaphore, returning a small HTTP 503 response under a bounded
socket-write timeout when full. A failed thread start returns its slot. The
[threading server extension points](https://docs.python.org/3.11/library/socketserver.html)
retain normal per-connection cleanup. A deadline-aware buffered socket reader
shares one ten-second deadline across request line, headers and body; it checks
remaining time on every socket receive, including continuous trickle input.
The standard parser retains its request-line bound, while aggregate headers are
limited to 64 KiB. Prefetched body bytes remain in the same buffer.

`tests/test_http_limits.py` exercises real header/body trickles, header overflow,
prefetched JSON, failed handler startup and saturation while an admitted write
is paused. The accepted write commits with a queryable receipt; the rejected
connection does not edit data or clear the SDK's pending operation ID. Recovery
after socket expiry is checked. These are local macOS results; hosted checks
remain unavailable under the recorded billing exception. This is a bounded
development server, not a production framework or remote-security decision.

## Python worker lifecycle

Required hooks retain the spawned worker and parent-owned transaction broker.
The two-second invocation budget now starts before process startup. A dedicated
I/O thread handles bounded JSON pipe frames and replies; only the transaction
thread dispatches engine calls. This avoids blocking that thread in a partial
frame read or a response to a peer that never reads. The parent can terminate
the worker and roll back on its existing deadline. Both pipe endpoints and the
reaped Process are explicitly closed, including construction/start and broker
thread-start failure. Python documents the
[frame length bound and Process cleanup](https://docs.python.org/3.11/library/multiprocessing.html#connection-objects).

`tests/test_worker_lifecycle.py` checks injected construction/process/thread
startup failures, startup budget consumption, real spawned malformed/oversized
frames and EOF, blocked response writes, maximum-size cell source round trips,
and repeated crash/recovery. It checks closed pipes/Process objects, no additional
active child and no remaining broker thread. A required-hook startup failure has
a durable rolled-back receipt, and subsequent successful edits still run the
handler. Existing exception/crash/timeout and generated-edit rollback tests
continue to apply. This is trusted-code containment, with no sandbox, external
effect rollback, memory quota or hard OS-I/O cancellation claim. Hosted platform
execution remains pending the recorded billing exception.

## Native capture binding

The final increment adds the isolated
[APSW preupdate comparison](native-capture-binding.md), pinned to an optional
wheel. Eighteen statement cases replay typed native images and compare the TEMP
journal's changed-row scope; fifteen optional failure/budget/access tests pass
locally. Native capture sees replacement deletes with recursive triggers off,
while its rollback control proves that attempted images need explicit discard.
The independent stdlib connection continues to work without callbacks. CI has
separate optional-binding jobs; the installed runtime remains stdlib-only and
public managed SQL remains unavailable. Production binding adoption, native
Session capture and hosted platform execution remain open.
