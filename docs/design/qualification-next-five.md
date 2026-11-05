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
