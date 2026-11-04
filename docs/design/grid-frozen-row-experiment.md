# M1 Qt grid and frozen-row display experiment

November 4, 2026. Retain QTableView and the bounded service-backed SheetModel.
The existing scroll/input harness now includes a read-only frozen-header display
probe. It is not installed, exposed as a desktop command or saved in workbooks.
M4 frozen-pane interactions remain unimplemented.

## Design and contract

The probe uses a one-row QAbstractTableModel over row zero of the real grid
model. It forwards data roles and invalidations, including reset and row/column
insertion/removal. It owns no cell cache and issues no separate REST requests.
The existing model fetches its bounded tiles normally, so frozen and body views
share the same 16-tile/14-MiB charge budget and 2-MiB complete-source budget.
Read-only cache diagnostics expose counts and estimated charges, not RSS.

A QTableView over the one-row model sits at the top of the body viewport.
Column widths, horizontal scrolling, row height and viewport resizing stay
synchronized. This follows the shared-view idea illustrated by Qt's
[frozen-column example](https://doc.qt.io/qt-6.8/qtwidgets-itemviews-frozencolumn-example.html),
without hiding a million source rows. The proxy is flat and read-only, and the
overlay passes mouse events through. It is a display experiment, not a complete
freeze interaction implementation.

The model contract test uses QAbstractItemModelTester on a bounded fixture. It
checks first-row values, irrelevant body-row changes, data notifications,
insertion/removal and an empty reset. The existing scrolling regression also
checks live tile counts/charges against the declared budgets.

Visual inspection caught an overlay moving offscreen with the scrolling
viewport even though its local cell geometry and data remained correct. The
probe is now a sibling of the viewport, with an explicit visibility/position
regression and benchmark checks. The input probe also reproduced tile replies
overwriting a focused, modified address field. The installed desktop now retains
that address draft through asynchronous source synchronization; a valid Enter
submission clears its modified flag before navigating. A real-service regression
fails before the fix and verifies retained text and subsequent B42 navigation.

## Measurement

After the ordinary 100 scrolling and 100 distant-jump samples, the benchmark
adds 256 explicit text headers to row one. It measures 100 more distant body
scrolls and horizontal changes with the frozen row visible. It resizes a column
and header row, narrows the window halfway through, checks exact cell alignment
and verifies the expected header text after every move. Synthetic address input
is dispatched during these reads. No navigation may emit a managed edit.

Reports include frozen-view latency, key-dispatch time, event-loop gaps, range
response counts, shared-cache charges and a screenshot. The existing ordinary
scroll/jump statistics retain their separate response counts. Timings remain
measurements rather than variable-runner pass thresholds; geometry, values,
model invariants and cache limits are correctness checks.
Focused address retention must be checked in every offscreen sample. Native
runs record an explicit flag: a background Cocoa process may render without
receiving active-window focus, so those samples cannot qualify focused native
typing. Synthetic widget-event dispatch time remains separate from that check.

[Native Cocoa JSON](evidence/grid-benchmark-native.json),
[native screenshot](evidence/grid-benchmark-native-frozen-row.png),
[offscreen JSON](evidence/grid-benchmark-offscreen.json) and
[offscreen screenshot](evidence/grid-benchmark-offscreen-frozen-row.png) record
the local experiment. [Full regression verification](evidence/grid-experiment-verification.txt)
records the unchanged Python suite and the new Qt case. CI preserves JSON and
screenshots for each hosted platform.

Run `build/desktop_benchmark build/desktop-benchmark.json`; set
`QT_QPA_PLATFORM=offscreen` for the CI mode. On Windows use the Release executable.
Native screenshots are local evidence, not native Windows/Linux acceptance.

Frozen editing, selection/focus, keyboard navigation into a frozen region,
multiple frozen edges, hidden/reordered columns, persisted view state,
accessibility, high DPI, physical input/compositor latency and RSS remain
unqualified. The next M1 work item is the toolchain/packaging baseline and its
minimum-runtime verification. Native-platform acceptance and cold-cache work
continue to block full M1 closure.
