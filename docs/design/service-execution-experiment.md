# M1 service and Python execution comparison

November 4, 2026. This isolated experiment compares two standard-library HTTP
arrangements around identical engine calls and compares spawned brokered hooks
with an inline fast-code baseline. It does not replace the development service
or qualify a production framework.

## Decision

Keep threaded localhost HTTP and spawned, transaction-brokered Python workers
for this development tier. Both tested HTTP adapters preserve the engine's
transaction/receipt semantics; changing transport does not improve containment.
The inline baseline is faster for small trusted functions, but offers no process
boundary to terminate a looping/crashing function. It is not enabled in the
service. Keep Python engine access inside the existing transaction broker.

| Candidate | Tested scope | Decision |
| --- | --- | --- |
| ThreadingHTTPServer adapter | Identical authenticated info/edit/outcome experiment routes, real sockets, shared engine | Retain the existing development arrangement; independent request threads fit the service's existing per-workbook locks |
| Serial wsgiref adapter | Same application dispatch, payloads and engine operations through WSGI | Correct for the bounded fixture; no reason to replace the current service with a serial reference server |
| Spawned worker with broker | Real required handler, generated edits, exception/crash/timeout, receipts | Retain containment and parent-owned transactions |
| Inline hook baseline | Only fast explicitly trusted code and the same broker dispatch | Timing baseline only; no crash/timeout containment qualification |

Python documents [ThreadingHTTPServer's request threads](https://docs.python.org/3.13/library/http.server.html)
and [wsgiref's reference-server role](https://docs.python.org/3.13/library/wsgiref.html).
Neither standard-library server is production qualification. A third-party
ASGI/WSGI framework, network hardening and packaged runtime remain later choices.
The explicit [spawn start method](https://docs.python.org/3.13/library/multiprocessing.html#contexts-and-start-methods)
is available on Windows and POSIX; hosted evidence must still verify each platform.

## Correctness and failure contract

Both adapters dispatch the same tiny benchmark application to the real Workbook
engine. This is a comparison of arrangement and containment costs, not full API
feature parity or a replacement for the production REST integration tests.
Only temporary local workbooks and an explicit trusted fixture are used.

The handler reads the pending A1 value and sets B1 to twice that value. Both
changes must appear in one committed receipt. Replaying the same operation
returns that receipt without rerunning the handler. Authentication rejection
occurs before engine mutation.

Three negative inputs first generate a B1 edit and then raise, exit the worker,
or loop until its required-handler deadline. Each operation must leave both
stored inputs unchanged, release the transaction, and return the same durable
rolled-back receipt through outcome lookup. The driver then closes a real client
connection after sending a complete write but before reading its acknowledgement.
Receipt lookup confirms the committed result; reopening the service fixture
retains that receipt. A missing receipt while admission is racing remains
unknown and is polled, never treated as rollback or retried as a new write.

Timeout rollback is the bounded cancellation evidence in this tier. Disconnect
does not cancel an accepted write. Public cancellable/chunked jobs remain
unavailable, and no external-side-effect execution guarantee is added.

## Repeatability and limits

Run `PYTHONPATH=service python3 tests/service_execution_experiment.py --output build/service-execution-benchmark.json`.
The default 20 samples per arrangement record warm local durable edit latency,
including spawned handler rounds or the inline baseline. `--samples` can raise
the sample count. Metadata retrieval precedes the timed edit; semantic checks
verify both resulting values outside the timing interval.

[Local JSON evidence](evidence/service-execution-benchmark.json) records the
platform, interpreter, failure outcomes/latencies and median/p95 values. CI runs
the same experiment on Windows/macOS/Linux and includes the report in the M1
artifacts. The three contract tests run in normal discovery. Measurements do not
qualify cold starts, concurrent load, RSS, OS I/O cancellation, remote security
or a production service/framework choice. The complete installed API continues
to be tested separately.

The next M1 item is physical representation validation: sparse inputs, styles,
identity, format versions, external insertion and conservative refresh/reopen.
