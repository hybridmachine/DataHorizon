# Next five interchange and recovery increments

November 6, 2026. Continue after PR #23 in dependency order. Implement, locally
verify, publish/review/fix and merge each PR before starting the next. Hosted CI
remains unavailable under the user's local verification authorization.

1. Cancellable CSV preparation jobs, progress and explicit atomic Apply.
2. Bounded XLSX input/formula import and snapshot export with conversion reports.
3. XLSX formatting fidelity for the native style tier, with explicit loss reports.
4. Bounded ODS input/formula/style import and snapshot export.
5. Native recovery and interchange failure hardening, with destructive fixtures.

## Preparation jobs

CSV preparation runs outside HTTP admission and workbook locks. The service
retains at most four ephemeral jobs, one running at a time, with a ten-second
cooperative deadline and five-minute retention. Completed responses retain the
existing 4-MiB bound. Source remains at most 3 MiB and 10,000 cells. Progress
names its phase and processed units, without an invented percentage. Larger
imports and chunked commits remain unavailable.

Clients choose a bounded ASCII job ID before POST. Identical retries return the
same job; changed inputs conflict. GET returns running/completed/failed/cancelled
and the completed result or structured error. Cancellation interrupts parsing
and conversion at checkpoints, or discards a completed result. Release removes
terminal storage. Service restart loses jobs and returns JOB_NOT_FOUND; it never
implies an outcome for a workbook operation. Shutdown cancels and joins workers.

Qt shows a modal, cancellable progress window while leaving its event loop free.
Local cancellation fences the result even if completion wins the server race.
The original workbook/sheet/coordinate/state checks still guard review and Apply.
The SDK exposes start/status/cancel/release; all clients use these REST resources.
Preparation never runs hooks or mutates data. Confirmed Apply submits one ordinary
managed edit with hooks, durable receipts, rollback and Undo/Redo. Once Apply is
submitted its existing outcome lookup contract applies; cancelling preparation
cannot undo that commit.
