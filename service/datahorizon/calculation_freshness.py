# SPDX-License-Identifier: GPL-3.0-only
"""Block and sheets content digests, and open/refresh freshness comparison."""

from dataclasses import dataclass
import hashlib
import struct
import time

from .calculation_store import BLOCK_ROWS
from .formula import MAX_COLS

_MOD256 = 1 << 256
_MASK256 = _MOD256 - 1

_KNOWN_KINDS = {
    kind: (b"T%d:" % len(kind)) + kind.encode("ascii")
    for kind in ("auto", "text", "number", "boolean", "null", "formula", "unmaintained", "date", "time", "datetime")
}


def _encode_field(value) -> bytes:
    """Encode an arbitrary SQLite scalar with a 1-byte type tag and self-delimiting framing."""
    if value is None:
        return b"N:"
    if type(value) is int:
        return b"I%d:" % value
    if type(value) is str:
        raw = value.encode("utf-8", "surrogatepass")
        return (b"T%d:" % len(raw)) + raw
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return (b"B%d:" % len(raw)) + raw
    if type(value) is float:
        return b"F" + struct.pack("!d", value)
    raw = str(value).encode("utf-8", "surrogatepass")
    return (b"O%d:" % len(raw)) + raw


def cell_digest(row, col, source, kind) -> int:
    """256-bit unsigned integer digest of one stored cell (coordinates, source and kind; style excluded)."""
    if type(source) is str and type(kind) is str and type(row) is int and type(col) is int:
        src_b = source.encode("utf-8", "surrogatepass")
        knd_b = _KNOWN_KINDS.get(kind)
        if knd_b is None:
            kb = kind.encode("utf-8", "surrogatepass")
            knd_b = (b"T%d:" % len(kb)) + kb
        payload = (b"I%d:I%d:T%d:" % (row, col, len(src_b))) + src_b + knd_b
    else:
        payload = _encode_field(row) + _encode_field(col) + _encode_field(source) + _encode_field(kind)
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")


def block_digest(db, sheet_id: str, block: int) -> bytes | None:
    """Sum-mode 32-byte digest of every stored cell in (sheet_id, block), or None when empty."""
    r1 = block * BLOCK_ROWS + 1
    r2 = r1 + BLOCK_ROWS - 1
    sha256 = hashlib.sha256
    from_bytes = int.from_bytes
    kind_cache = _KNOWN_KINDS
    total = 0
    found = False
    for row, col, source, kind in db.execute(
        "SELECT row,col,source,kind FROM cells WHERE sheet_id=? AND row BETWEEN ? AND ? ORDER BY row,col",
        (sheet_id, r1, r2),
    ):
        found = True
        if type(source) is str and type(kind) is str and type(row) is int and type(col) is int:
            src_b = source.encode("utf-8", "surrogatepass")
            knd_b = kind_cache.get(kind)
            if knd_b is None:
                kb = kind.encode("utf-8", "surrogatepass")
                knd_b = (b"T%d:" % len(kb)) + kb
            payload = (b"I%d:I%d:T%d:" % (row, col, len(src_b))) + src_b + knd_b
        else:
            payload = _encode_field(row) + _encode_field(col) + _encode_field(source) + _encode_field(kind)
        total += from_bytes(sha256(payload).digest(), "big")
    if not found:
        return None
    return (total & _MASK256).to_bytes(32, "big")


def update_block_digest(db, sheet_id: str, block: int, current_digest: bytes, changes) -> bytes | None:
    """Update a block's sum-mode digest by delta for the commit's (row, col, before, after) cell changes."""
    total = int.from_bytes(current_digest, "big")
    deleted = False
    for row, col, before, after in changes:
        if before is not None:
            total -= cell_digest(row, col, before["source"], before["kind"])
        if after is not None:
            total += cell_digest(row, col, after["source"], after["kind"])
        else:
            deleted = True
    if deleted:
        r1 = block * BLOCK_ROWS + 1
        r2 = r1 + BLOCK_ROWS - 1
        if db.execute(
            "SELECT 1 FROM cells WHERE sheet_id=? AND row BETWEEN ? AND ? LIMIT 1",
            (sheet_id, r1, r2),
        ).fetchone() is None:
            return None
    return (total & _MASK256).to_bytes(32, "big")


def sheets_digest(db) -> bytes:
    """SHA-256 digest over (id, name) of every sheet in id order."""
    hasher = hashlib.sha256()
    for sheet_id, name in db.execute("SELECT id, name FROM sheets ORDER BY id"):
        hasher.update(_encode_field(sheet_id))
        hasher.update(_encode_field(name))
    return hasher.digest()


def ensure_digests(store, db, sheet_blocks) -> None:
    """Compute and store the digest of each (sheet_id, block) that currently has none."""
    if store.sheets_digest() is None:
        store.set_sheets_digest(sheets_digest(db))
    missing = []
    for sheet_id, block in dict.fromkeys(sheet_blocks):
        if store.digest(sheet_id, block) is None:
            digest = block_digest(db, sheet_id, block)
            if digest is not None:
                missing.append((sheet_id, block, digest))
    if missing:
        store.set_digests(missing)


@dataclass
class Verdict:
    sheets_changed: bool
    changed: list
    found: dict


def compare(store, db, deadline: float) -> Verdict | None:
    """One pass over cells computing every block's digest, compared against stored digests and sheets digest.

    Returns None if time.monotonic() exceeds deadline before the pass finishes.
    """
    monotonic = time.monotonic
    if monotonic() > deadline:
        return None
    current_sheets = sheets_digest(db)
    sheets_changed = store.sheets_digest() != current_sheets
    stored = {(sheet_id, block): digest for sheet_id, block, digest in store.digests()}
    if monotonic() > deadline:
        return None

    sha256 = hashlib.sha256
    from_bytes = int.from_bytes
    kind_cache = _KNOWN_KINDS
    block_rows = BLOCK_ROWS
    totals = {}
    seen = 0
    for sheet_id, row, col, source, kind in db.execute(
        "SELECT sheet_id,row,col,source,kind FROM cells ORDER BY sheet_id,row,col"
    ):
        seen += 1
        if (seen & 4095) == 0 and monotonic() > deadline:
            return None
        if type(source) is str and type(kind) is str and type(row) is int and type(col) is int:
            src_b = source.encode("utf-8", "surrogatepass")
            knd_b = kind_cache.get(kind)
            if knd_b is None:
                kb = kind.encode("utf-8", "surrogatepass")
                knd_b = (b"T%d:" % len(kb)) + kb
            payload = (b"I%d:I%d:T%d:" % (row, col, len(src_b))) + src_b + knd_b
        else:
            payload = _encode_field(row) + _encode_field(col) + _encode_field(source) + _encode_field(kind)
        key = (sheet_id, (row - 1) // block_rows)
        totals[key] = totals.get(key, 0) + from_bytes(sha256(payload).digest(), "big")
    if monotonic() > deadline:
        return None

    mask = _MASK256
    found = {key: (total & mask).to_bytes(32, "big") for key, total in totals.items()}
    changed = sorted(key for key in (stored.keys() | found.keys()) if stored.get(key) != found.get(key))
    return Verdict(sheets_changed=sheets_changed, changed=changed, found=found)


def apply(store, db, verdict: Verdict) -> None:
    """Apply a completed freshness verdict to store inside the caller's transaction."""
    if verdict.sheets_changed:
        store.clear_results()
    else:
        roots = []
        for sheet_id, block in verdict.changed:
            r1 = block * BLOCK_ROWS + 1
            r2 = r1 + BLOCK_ROWS - 1
            for formula_key in store.formulas_in_rows(sheet_id, r1, r2):
                store.delete_formula(formula_key)
            roots.append((sheet_id, r1, 1, r2, MAX_COLS))
        if roots:
            store.add_roots(roots)
    if verdict.changed:
        store.set_digests((sheet_id, block, verdict.found.get((sheet_id, block))) for sheet_id, block in verdict.changed)
    store.set_sheets_digest(sheets_digest(db))
