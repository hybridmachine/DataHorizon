# SPDX-License-Identifier: GPL-3.0-only
"""Prototypes for the calculation stage 1 experiment: input digests, dependency data, ordered evaluation.

Isolated test code (docs/design/calculation-stage-1-plan.md). Nothing here is used by the service.
"""
import hashlib

HASHES = ("blake2b16", "sha256")
MODES = ("sum", "stream")
_NEW = {"blake2b16": lambda data=b"": hashlib.blake2b(data, digest_size=16), "sha256": hashlib.sha256}
_SIZE = {"blake2b16": 16, "sha256": 32}


# ---- Input digests -------------------------------------------------------------------------------

def _encoded(row, col, source, kind):
    # Length-prefixed, so no two different cells share an encoding.
    source, kind = source.encode("utf-8", "surrogatepass"), kind.encode("utf-8", "surrogatepass")
    return b"%d:%d:%d:%d:" % (row, col, len(source), len(kind)) + source + kind


def cell_hash(hash_name, row, col, source, kind):
    return int.from_bytes(_NEW[hash_name](_encoded(row, col, source, kind)).digest(), "big")


def scan_digests(db, band, hash_name, mode):
    """Digest of every block of stored inputs, keyed by (sheet_id, (row - 1) // band). Style is not read."""
    new, size, digests = _NEW[hash_name], _SIZE[hash_name], {}
    rows = db.execute("SELECT sheet_id,row,col,source,kind FROM cells ORDER BY sheet_id,row,col")
    if mode == "sum":
        modulus, from_bytes = 1 << (8 * size), int.from_bytes
        for sheet, row, col, source, kind in rows:
            key = sheet, (row - 1) // band
            digests[key] = digests.get(key, 0) + from_bytes(new(_encoded(row, col, source, kind)).digest(), "big")
        return {key: (total % modulus).to_bytes(size, "big") for key, total in digests.items()}
    current, hasher = None, None
    for sheet, row, col, source, kind in rows:
        key = sheet, (row - 1) // band
        if key != current:
            if hasher is not None:
                digests[current] = hasher.digest()
            current, hasher = key, new()
        hasher.update(_encoded(row, col, source, kind))
    if hasher is not None:
        digests[current] = hasher.digest()
    return digests


def updated_digest(digest, hash_name, old, new):
    """A sum-mode block digest after one cell changed from old to new; each is (row, col, source, kind) or None."""
    size = _SIZE[hash_name]
    total = int.from_bytes(digest, "big")
    if old is not None:
        total -= cell_hash(hash_name, *old)
    if new is not None:
        total += cell_hash(hash_name, *new)
    return (total % (1 << (8 * size))).to_bytes(size, "big")


def sheets_digest(db, hash_name):
    size, total = _SIZE[hash_name], 0
    for sheet, name in db.execute("SELECT id,name FROM sheets"):
        sheet, name = sheet.encode(), name.encode("utf-8", "surrogatepass")
        total += int.from_bytes(_NEW[hash_name](b"%d:%d:" % (len(sheet), len(name)) + sheet + name).digest(), "big")
    return (total % (1 << (8 * size))).to_bytes(size, "big")
