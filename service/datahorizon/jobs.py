"""Bounded, ephemeral preparation jobs. These never mutate a workbook."""
import hashlib
import re
import threading
import time

from .engine import EngineError, encoded, require


class PreparationJobs:
    def __init__(self, *, capacity=4, seconds=10, retention=300):
        self.lock = threading.RLock()
        self.entries = {}
        self.capacity, self.seconds, self.retention = capacity, seconds, retention
        self.stopping = False

    def start(self, request, prepare):
        job_id = request.get("job_id")
        require(isinstance(job_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id),
                "INVALID_JOB", "job_id must be 1-100 ASCII letters, digits, underscores or hyphens")
        payload = {key:value for key,value in request.items() if key != "job_id"}
        fingerprint = hashlib.sha256(encoded(payload).encode()).hexdigest()
        with self.lock:
            require(not self.stopping, "SERVICE_STOPPING", "Service is stopping", 503)
            now = time.monotonic()
            self.entries = {key:entry for key,entry in self.entries.items()
                            if entry["finished"] is None or now-entry["finished"] < self.retention}
            if job_id in self.entries:
                entry = self.entries[job_id]
                require(entry["fingerprint"] == fingerprint, "JOB_CONFLICT", "Job ID was used for different inputs", 409)
                return self._view(job_id, entry)
            require(len(self.entries) < self.capacity, "JOB_LIMIT", "Preparation job storage is full; release a completed job", 409)
            require(not any(e["status"] == "running" for e in self.entries.values()),
                    "JOB_BUSY", "Another preparation job is running", 409)
            entry = {"status":"running", "completed":0, "phase":"Queued", "fingerprint":fingerprint,
                     "cancel":threading.Event(), "finished":None}
            self.entries[job_id] = entry
            worker = threading.Thread(target=self._run, args=(entry,payload,prepare), daemon=True)
            entry["worker"] = worker
            try:
                worker.start()
            except BaseException:
                self.entries.pop(job_id)
                raise
            return self._view(job_id, entry)

    def _run(self, entry, payload, prepare):
        deadline = time.monotonic()+self.seconds
        def checkpoint(completed, phase):
            require(not entry["cancel"].is_set(), "CANCELLED", "Preparation cancelled; no workbook changes were made")
            require(time.monotonic() <= deadline, "TIMEOUT", "Preparation exceeded its time budget")
            with self.lock:
                entry.update(completed=completed, phase=phase)
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

    @staticmethod
    def _view(job_id, entry):
        return {"job_id":job_id, **{key:entry[key] for key in ("status","completed","phase","result","error") if key in entry}}

    def access(self, job_id, action=None):
        with self.lock:
            entry = self.entries.get(job_id)
            if entry is not None and entry["finished"] is not None and time.monotonic()-entry["finished"] >= self.retention:
                self.entries.pop(job_id)
                entry = None
            require(entry is not None, "JOB_NOT_FOUND", "Preparation job expired or is unavailable after service restart", 404)
            if action == "cancel":
                entry["cancel"].set()
                # Even a completed result can be discarded before Apply.
                if entry["status"] == "completed":
                    entry.pop("result", None)
                    entry.update(status="cancelled", phase="Cancelled")
            elif action == "release":
                require(entry["status"] != "running", "JOB_RUNNING", "Cancel and wait before releasing a running job", 409)
                self.entries.pop(job_id)
                return {"released":True, "job_id":job_id}
            return self._view(job_id, entry)

    def close(self):
        with self.lock:
            self.stopping = True
            workers = [entry["worker"] for entry in self.entries.values()]
            for entry in self.entries.values():
                entry["cancel"].set()
        for worker in workers:
            worker.join(timeout=self.seconds+1)
        with self.lock:
            self.entries.clear()
