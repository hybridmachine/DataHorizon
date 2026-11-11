# SPDX-License-Identifier: GPL-3.0-only
"""A private directory for uploads, import stages and exports, with per-kind capacity and age-based expiry."""
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time

from .engine import require

SUFFIXES = {"upload": ".upload", "stage": ".stage", "export": ".export"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")
SIDE_FILES = ("-journal", "-wal", "-shm")


class Staging:
    """Files are named `<kind>-<identifier><suffix>`; the identifier is the only caller-controlled path part."""

    def __init__(self, root=None, *, capacity=4, retention=900):
        require(type(capacity) is int and capacity >= 1, "INVALID_ARGUMENT", "Staging capacity must be a positive integer")
        self.capacity, self.retention = capacity, retention
        self._lock = threading.RLock()
        if root is None:
            self.root = Path(tempfile.mkdtemp(prefix=f"datahorizon-{os.getpid()}-"))
            self._owned = True
        else:
            self.root = Path(root)
            self._owned = not self.root.exists()
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._closed = False

    def _file(self, kind, identifier):
        require(kind in SUFFIXES, "INVALID_ARGUMENT", "Staging kind must be upload, stage or export")
        require(isinstance(identifier, str) and IDENTIFIER.fullmatch(identifier), "INVALID_JOB",
                "Identifier needs 1–64 ASCII letters, digits, _ or -")
        return self.root/f"{kind}-{identifier}{SUFFIXES[kind]}"

    def _entries(self, kind):
        prefix, suffix = kind+"-", SUFFIXES[kind]
        return [p for p in self.root.iterdir() if p.name.startswith(prefix) and p.name.endswith(suffix)]

    def new(self, kind, identifier):
        """Reserve `identifier` by creating an empty private file; return its path."""
        with self._lock:
            path = self._file(kind, identifier)
            self.sweep()
            require(not path.exists() and not path.is_symlink(), "JOB_CONFLICT", "That staging entry already exists", 409)
            require(len(self._entries(kind)) < self.capacity, "JOB_LIMIT",
                    f"At most {self.capacity} {kind} entries can be staged at once; release one first", 409)
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return path

    def path(self, kind, identifier):
        with self._lock:
            path = self._file(kind, identifier)
            require(path.is_file() and not path.is_symlink(), "JOB_NOT_FOUND", "Staging entry is unavailable", 404)
            return path

    def release(self, kind, identifier):
        with self._lock:
            path = self._file(kind, identifier)
            for candidate in (path, *(path.with_name(path.name+side) for side in SIDE_FILES)):
                candidate.unlink(missing_ok=True)

    def sweep(self):
        """Delete entries whose modification time is older than the retention; return how many were removed."""
        removed, cutoff = 0, time.time()-self.retention
        with self._lock:
            for kind in SUFFIXES:
                for path in self._entries(kind):
                    try:
                        if path.lstat().st_mtime < cutoff:
                            path.unlink()
                            removed += 1
                    except FileNotFoundError:
                        pass
        return removed

    def close(self):
        with self._lock:
            if self._closed: return
            self._closed = True
            if self._owned:
                shutil.rmtree(self.root, ignore_errors=True)
            else:
                for kind in SUFFIXES:
                    for path in self._entries(kind):
                        path.unlink(missing_ok=True)
                try: self.root.rmdir()
                except OSError: pass

    def __enter__(self): return self

    def __exit__(self, *exc): self.close()
