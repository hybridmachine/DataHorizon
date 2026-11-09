# macOS filesystem and process failure qualification

Task 5 extends failure evidence through the SDK/REST service and the GUI's shared
export writer. It retains unknown outcomes when a native receipt cannot be made
durable, instead of inferring rollback from a lost reply or missing receipt.

The new managed-client checks use actual unprivileged directory permission
denials for native journaling and operation-job sidecars. Native inputs and
transactional handler-generated counters remain unchanged after failed writes;
the SDK retains its pending ID and blocks unreviewed mutations. Failed staging
retains an empty valid journal and allows a subsequent explicit reviewed run.
The Qt check exercises a real denied export directory while a real managed GUI
workbook and its handler effects remain unchanged.

A separate child HTTP service deliberately exits while a transaction has spilled
to a hot SQLite journal, or immediately after data/hook changes and their receipt
commit but before the response. Reopen recovers the former without historical
handler replay and reports its missing receipt as unknown. The latter exposes its
committed receipt and exactly one transactional counter increment. Neither SDK
silently resubmits the lost operation.

Cancellation now propagates into read-only worker opening, native validation,
integrity and read scans, and snapshot post-copy validation. Read-job cancellation
has its own lifetime token rather than retaining a completed write operation's
cancelled event. SQL-guard cleanup also preserves the original failure if the
connection owner has already closed/replaced a rejected file. Existing nested
guard/rollback/connection-reuse contracts remain in force. A cancelled snapshot
can leave a valid or incomplete destination for inspection; it never reports
successful publication solely because the backup phase finished.

## Real disk exhaustion

```sh
cmake --build build --parallel 2
python3 tests/macos_filesystem_failures.py --probe build/export_failure_probe --output build/filesystem-failures.json
```

The opt-in driver creates and mounts its own 64-MiB HFS+ image. Before writing
filler it checks the mount belongs to that image, has a different device from the
host directory and remains bounded in size. It never fills the host filesystem.
The shared protected Qt writer attempts a bounded opaque payload, while REST/SDK
attempt a supported large cell edit with an activated transactional counter.
After freeing filler, the driver checks original native bytes/inputs and pending
receipt semantics, closes its service and detaches the owned image. Failed
cleanup preserves the private work directory and fails qualification.

The local real ENOSPC run passes: export reports No space left on device, the
native hash is unchanged, the managed edit remains unknown when no receipt can
be confirmed, stored input/counter values remain 7 and 1, and the image detaches.
The image reports 67,067,904 bytes total. This is mounted HFS+ failure evidence,
not physical power-loss or comprehensive APFS/SSD durability qualification.
