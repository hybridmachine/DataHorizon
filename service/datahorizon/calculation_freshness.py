# SPDX-License-Identifier: GPL-3.0-only
"""Block and sheets content digests for calculation freshness."""

import hashlib
import struct

from .calculation_store import BLOCK_ROWS

_MOD256 = 1 << 256
_MASK256 = _MOD256 - 1


def _encode_field(value) -> bytes:
    """Encode an arbitrary SQLite scalar with a 1-byte type tag and length prefix."""
    if value is None:
        return b"N0:"
    if isinstance(value, str):
        raw = value.encode("utf-8", "surrogatepass")
        return b"T%d:" % len(raw) + raw
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return b"B%d:" % len(raw) + raw
    if type(value) is int:
        raw = str(value).encode("ascii")
        return b"I%d:" % len(raw) + raw
    if type(value) is float:
        return b"F8:" + struct.pack("!d", value)
    raw = str(value).encode("utf-8", "surrogatepass")
    return b"O%d:" % len(raw) + raw


def cell_digest(row, col, source, kind) -> int:
    """256-bit unsigned integer digest of one stored cell (coordinates, source and kind; style excluded)."""
    payload = _encode_field(row) + _encode_field(col) + _encode_field(source) + _encode_field(kind)
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")


def block_digest(db, sheet_id: str, block: int) -> bytes | None:
    """Sum-mode 32-byte digest of every stored cell in (sheet_id, block), or None when empty."""
    r1 = block * BLOCK_ROWS + 1
    r2 = r1 + BLOCK_ROWS - 1
    total = 0
    found = False
    for row, col, source, kind in db.execute(
        "SELECT row,col,source,kind FROM cells WHERE sheet_id=? AND row BETWEEN ? AND ? ORDER BY row,col",
        (sheet_id, r1, r2),
    ):
        found = True
        total += cell_digest(row, col, source, kind)
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
