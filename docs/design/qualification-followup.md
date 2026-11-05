# Remaining M1 qualification increments

November 4, 2026. Continue the dependency list after PRs #9–#13. Implement,
locally verify, review and merge each increment before beginning the next.
Hosted Actions remain blocked by account billing/spending limits; local merges
are explicitly authorized. These bounded increments do not close M1.

1. Extend native keyboard, in-cell accessibility, modal cancellation and narrow
   window acceptance, retaining scale 1/2 reports.
2. Add an OS file-cache residency/eviction measurement path with explicit
   unsupported-platform results; never label a fresh process as OS-cold.
3. Measure more memory shapes and simultaneous open workbooks, separating
   service peaks from driver and worker memory.
4. Evaluate a bounded third-party service adapter against full managed REST
   operations, failure receipts and concurrent requests before runtime selection.
5. Compare native SQLite Session changesets separately from preupdate capture,
   including net-change and rollback semantics, before binding adoption.

## Native interaction coverage

The native-plugin acceptance executable now checks Shift+arrow range selection,
Tab/Backtab navigation, platform edit-key entry (Return on macOS, F2 elsewhere),
and Escape from the in-cell editor. Editors expose their source cell address to
Qt accessibility, including source-row mapping in projected views. The actual
Add Sheet Qt modal opens and cancels by Escape under a bounded watchdog. No
managed edit signal or workbook acknowledgement may result. A 560-pixel window
must retain a usable viewport and visible selected cell. The native screenshots
also show toolbar overflow at that width.

[Cocoa scale 1](evidence/native-interaction-1.json) and
[scale 2](evidence/native-interaction-2.json) retain matching screenshots. Existing
CI already runs the same executable at both scales on Cocoa/Windows/Xcb.
This checks Qt modal behavior with a native plugin; system file panels, physical
input, screen-reader speech/navigation and multiple monitors remain unqualified.
