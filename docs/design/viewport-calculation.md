# Bounded automatic viewport calculation

Task 4 adds automatic calculation to visible Qt grid tiles. The GUI requests
cached preview reads: stored inputs/styles arrive independently of pending
formula values. The source editor remains usable while a formula is pending.
The cell shows an ellipsis and a calculation tooltip until a reviewed read-only
snapshot supplies its result. Stored inputs, transactional hook reads, clipboard,
export and default REST/SDK reads keep their existing calculation contracts.

One background job per model runs through the existing REST calculation resource;
at most sixteen cached tiles are queued. Each tile is at most 32 rows by sixteen
columns, including explicit source-row projections. Existing 16-MiB grid/source
cache, four-job service retention, response/evaluation/opening budgets remain.
Visible ranges include frozen panes. Scrolling/hiding prunes queued work and
requests cancellation of obsolete admitted work; cancellation waits for the start
acknowledgement. A lost reply triggers one captured-ID status lookup without
resubmitting creation. A newer binding or service generation
cannot receive an older tile's results. Cancellation is cooperative, including
the existing native opening/evaluation checkpoints, not a guarantee about blocked
OS I/O. Worker snapshots still incur native validation and file read costs.

Successful publication checks the captured handle/model generation, input state,
sheet, stream generation and all pending cell sources. It replaces derived value
fields only, retaining source/type/style and editor drafts. An external-state
stale result requests ordinary refresh, without replaying skipped handlers.
Incomplete, invalid or failed calculation results display Unavailable with a
visible issue and retain inputs. Refresh permits an explicit retry. The GUI does
not silently switch to synchronous evaluation after a required snapshot fails.
An active in-cell editor owns its draft/caret until commit, cancellation or a
binding reset. The delegate ignores repeat editor-data reloads after initial
entry, because [Qt's item-view implementation](https://github.com/qt/qtbase/blob/6.8/src/widgets/itemviews/qabstractitemview.cpp)
refreshes editors on dataChanged even when only display roles were supplied.
Regressions verify retained drafts and selections in the main and frozen panes.

`CalculationRequest.stream_id` is optional, 1–64 ASCII letters/digits/_/-. Jobs
within one stream supersede older generations of that stream; the omitted default
preserves the previous shared behavior. The GUI gives automatic viewport work its
own stream so explicit GUI/SDK calculations remain independently inspectable.
Stream-generation bookkeeping is bounded by retained jobs. State changes stale
every affected stream regardless of generation. `preview=true` uses the same
bounded source/display projection as ordinary previews; pending formula previews
are truncated before response-byte accounting. Full reads retain their limits.

Explicit GUI selection calculation pauses automatic admission until its terminal
reply. This is read-only work: it does not create native operation receipts, run
historical change hooks, change revision, or promise durable calculation jobs.
Service/workbook-handle teardown owns ephemeral worker cleanup on disconnect.

Regression coverage includes publication/source retention, rebind/completion races,
admission/cancel ordering, scroll cancellation, projected coordinates, stale refresh,
failed/invalid replies, service changes, queue limits, per-stream isolation and
oversized pending source previews. Existing structural and catalog UI tests now
wait for calculated values separately from stored-input readiness.
