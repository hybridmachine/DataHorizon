# SPDX-License-Identifier: GPL-3.0-only
"""Bounded, ephemeral preparation jobs. These never mutate a workbook."""
import hashlib
import re
import sys
import threading
import time
import traceback

from .engine import EngineError, encoded, require


class PreparationJobs:
    """`lock` is reentrant and callers may hold it around start/access. on_discard callbacks run outside any
    critical section this object opens, but inside `lock` when the caller already holds it, so a callback may
    only take locks ordered after it. Service lock order: jobs.lock, then the upload lock, then the staging lock."""
    CLOSE_WAIT = 30

    def __init__(self, *, capacity=4, seconds=10, retention=300):
        self.lock = threading.RLock()
        self.entries = {}
        self.capacity, self.seconds, self.retention = capacity, seconds, retention
        self.stopping = False

    def start(self, request, prepare, kind="csv", *, seconds=None, on_discard=None):
        """Start a job, or return the existing job for a retry with the same ID and inputs.

        `seconds` overrides the time budget of this job. `on_discard` is adopted only when a new job is
        created; it runs exactly once, after the job stops running, when its result becomes unreachable."""
        job_id = request.get("job_id")
        require(isinstance(job_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id),
                "INVALID_JOB", "job_id must be 1-100 ASCII letters, digits, underscores or hyphens")
        payload = {key:value for key,value in request.items() if key != "job_id"}
        fingerprint = hashlib.sha256(encoded({"kind":kind,"request":payload}).encode()).hexdigest()
        expired = []
        try:
            with self.lock:
                require(not self.stopping, "SERVICE_STOPPING", "Service is stopping", 503)
                expired = self._expire()
                if job_id in self.entries:
                    entry = self.entries[job_id]
                    require(entry["fingerprint"] == fingerprint, "JOB_CONFLICT", "Job ID was used for different inputs", 409)
                    return self._view(job_id, entry)
                require(len(self.entries) < self.capacity, "JOB_LIMIT", "Preparation job storage is full; release a completed job", 409)
                require(not any(e["status"] == "running" for e in self.entries.values()),
                        "JOB_BUSY", "Another preparation job is running", 409)
                entry = {"status":"running", "completed":0, "phase":"Queued", "fingerprint":fingerprint,
                         "cancel":threading.Event(), "finished":None,
                         "deadline":time.monotonic()+(self.seconds if seconds is None else seconds)}
                if on_discard is not None: entry["on_discard"] = on_discard
                worker = threading.Thread(target=self._run, args=(entry,payload,prepare), daemon=True)
                entry["worker"] = worker
                self.entries[job_id] = entry
                try:
                    worker.start()
                except BaseException:
                    # Not created: the caller still owns what on_discard would release.
                    self.entries.pop(job_id)
                    raise
                return self._view(job_id, entry)
        finally:
            self._discard(*expired)

    def _expire(self):
        """Drop finished entries past retention; return them for discarding outside the lock."""
        now = time.monotonic()
        expired = [key for key,entry in self.entries.items() if entry["finished"] is not None and now-entry["finished"] >= self.retention]
        return [self.entries.pop(key) for key in expired]

    def _discard(self, *entries):
        for entry in entries:
            with self.lock:
                callback = entry.pop("on_discard", None)
            if callback is None: continue
            try:
                callback()
            except Exception:
                print("Preparation storage could not be released:", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)

    def _run(self, entry, payload, prepare):
        deadline = entry["deadline"]
        def checkpoint(completed, phase):
            require(not entry["cancel"].is_set(), "CANCELLED", "Preparation cancelled; no workbook changes were made")
            require(time.monotonic() <= deadline, "TIMEOUT", "Preparation exceeded its time budget")
            with self.lock:
                entry.update(completed=completed, phase=phase)
        checkpoint.cancel_event=entry['cancel']
        try:
            result = prepare(payload, checkpoint)
            require(len(encoded(result).encode()) <= 4*1024*1024, "READ_LIMIT", "Preparation result exceeds 4 MiB")
            # Publication and cancellation are ordered by the same lock.
            with self.lock:
                checkpoint(entry["completed"], "Ready for review")
                entry.update(status="completed", result=result)
        except EngineError as error:
            with self.lock:
                entry.update(status="cancelled" if error.body["code"] == "CANCELLED" else "failed", error=error.body)
        except Exception:
            with self.lock:
                entry.update(status="failed", error={"code":"PREPARATION_FAILED", "message":"Preparation failed; no workbook changes were made", "outcome":"rejected"})
        finally:
            with self.lock:
                entry["finished"] = time.monotonic()
                # A completed result stays reachable; anything else is discarded now. A cancel that raced
                # publication has already cleared the result and set the status.
                unreachable = entry["status"] != "completed"
            if unreachable: self._discard(entry)

    @staticmethod
    def _view(job_id, entry):
        return {"job_id":job_id, **{key:entry[key] for key in ("status","completed","phase","result","error") if key in entry}}

    def access(self, job_id, action=None):
        discarded = []
        try:
            with self.lock:
                discarded = self._expire()
                entry = self.entries.get(job_id)
                require(entry is not None, "JOB_NOT_FOUND", "Preparation job expired or is unavailable after service restart", 404)
                if action == "cancel":
                    entry["cancel"].set()
                    # Even a completed result can be discarded before Apply.
                    if entry["status"] == "completed":
                        entry.pop("result", None)
                        entry.update(status="cancelled", phase="Cancelled")
                        discarded.append(entry)
                elif action == "release":
                    require(entry["status"] != "running", "JOB_RUNNING", "Cancel and wait before releasing a running job", 409)
                    discarded.append(self.entries.pop(job_id))
                    return {"released":True, "job_id":job_id}
                return self._view(job_id, entry)
        finally:
            self._discard(*discarded)

    def consume(self, job_id):
        """The completed result was applied: forget the job without running on_discard, because the caller now
        releases what that callback owned. An unknown or expired job is already gone."""
        with self.lock:
            entry = self.entries.get(job_id)
            if entry is not None and entry["status"] != "running":
                self.entries.pop(job_id).pop("on_discard", None)

    def close(self):
        with self.lock:
            self.stopping = True
            entries = list(self.entries.values())
            for entry in entries:
                entry["cancel"].set()
        # Cancellation is honoured within 1,000 cells, so this normally returns at once; a worker is waited
        # for at most until its own deadline (plus a grace second), capped at CLOSE_WAIT.
        for entry in entries:
            entry["worker"].join(timeout=min(max(entry["deadline"]-time.monotonic(), 0)+1, self.CLOSE_WAIT))
        with self.lock:
            self.entries.clear()
        # A worker still running discards its own storage when it stops.
        self._discard(*(entry for entry in entries if not entry["worker"].is_alive()))
