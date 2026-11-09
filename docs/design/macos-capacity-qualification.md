# macOS capacity qualification

Task 3 extends repeatable capacity evidence without changing application limits.
PR/push checks now target macOS, as requested by the user. Manual dispatch with
`platforms=all` retains the Windows/macOS/Linux matrices for future qualification;
deferred platforms are not silently represented as passing.
Fresh processes and fresh SQLite connections do not imply cold OS caches. The
cache/RSS driver now records cache provenance explicitly and supports a required
macOS purge before every fresh sample. Failed eviction aborts that run and writes
an unsuccessful report; it never relabels warm measurements as OS-cold evidence.

```sh
python3 tests/cache_rss_benchmark.py --samples 100 --output build/cache-rss.json
python3 tests/cache_rss_benchmark.py --samples 100 --purge-os-cache --output build/os-cold.json
PYTHONPATH=service python3 tests/benchmark.py --samples 100 --output build/shapes.json
PYTHONPATH=service python3 tests/qualify_m3_m5.py build/jobs-memory-disk.json
```

The purge mode uses `sudo -n /usr/sbin/purge` and requires administrator access
already available to that process. It never prompts for or handles a password.
The current host rejects ordinary eviction and has no noninteractive sudo grant.
The actual failed run is retained as evidence. True OS-cold qualification remains
open; this is an external resource gate, not a passing result.

The 100-sample fresh/warm fixture contains 100,000 numeric cells and 270,000 1-KiB
Text cells in a 328,114,176-byte database, larger than the 256-MiB service target.
Fresh-process start/open/first-read p95 is 408.323 ms; the warm late viewport p95
is 1.417 ms. Peak isolated service RSS is 39,911,424 bytes. Separate 100-sample
sparse, dense, distant, edit, formula-chain and long-text measurements retain
their dataset descriptions and typed correctness checks. These measurements are
host/workload-specific and do not establish universal workbook capacity.

Job disk accounting reports retained file lengths after the frozen read has
completed, separately for full native snapshots, journals and other sidecars.
It confirms release removes those files. Edit staging is measured separately.
These are retained file lengths, not the peak allocated blocks or SQLite/OS
temporary space during execution. A narrow result scope can still need a full
native snapshot: reserve room for the workbook copy as well as result/stage
files. Do not infer disk cost from returned row counts alone.

GUI cache/RSS uses the existing bounded grid harness. Offscreen input events do
not certify physical interaction or compositor latency. Keep task 6 and VoiceOver
acceptance separate. The batch ledger links the actual reports and remaining gate.
