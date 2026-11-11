# SPDX-License-Identifier: GPL-3.0-only
"""Operator activation key syntax shared with the thin REST client."""
import re
import threading
import uuid


def validate_approvals(values):
    if not isinstance(values,(list,tuple,set,frozenset)) or len(values)>256:
        raise ValueError('Provide at most 256 explicit code activation keys')
    for key in values:
        if not isinstance(key,str) or re.fullmatch(r'[0-9a-f-]{36}:[0-9a-f]{64}',key) is None:
            raise ValueError('Code approval requires workbook UUID:SHA256')
        try:valid=str(uuid.UUID(key[:36]))==key[:36]
        except ValueError:valid=False
        if not valid:raise ValueError('Code approval requires a canonical workbook UUID')
    return frozenset(values)


class ApprovalStore:
    """Process-local allowlist: start-up keys are fixed; granted keys are append-only per workbook."""

    def __init__(self, keys=()):
        self._fixed = validate_approvals(keys)
        self._granted = {}
        self._lock = threading.Lock()

    def __contains__(self, key):
        with self._lock:
            return key in self._fixed or key in self._granted.values()

    def grant(self, key):
        validate_approvals([key])
        with self._lock:
            kept = {book: value for book, value in self._granted.items() if book != key[:36]}
            if len(self._fixed | set(kept.values()) | {key}) > 256:
                raise ValueError('Provide at most 256 explicit code activation keys')
            kept[key[:36]] = key
            self._granted = kept

    def snapshot(self):
        with self._lock:
            return self._fixed | frozenset(self._granted.values())
