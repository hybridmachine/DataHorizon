# SPDX-License-Identifier: GPL-3.0-only
"""Measurements for calculation stage 3 (docs/design/calculation-stage-3-design.md section 7)."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "service"), str(ROOT / "tests")]

from datahorizon.engine import Workbook
import calculation_experiment as exp

INSERT = "INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,?,?,?)"
BLOCK_ROWS = 64
TILE_COLS = 16


def _loadavg():
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except OSError:
        return None


def _meta():
    return {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "loadavg": _loadavg(),
    }


def _stream_block_64(db, sheet_id, block):
    r1 = block * BLOCK_ROWS + 1
    r2 = r1 + BLOCK_ROWS - 1
    h = hashlib.sha256()
    found = False
    for row, col, source, kind in db.execute(
        "SELECT row,col,source,kind FROM cells WHERE sheet_id=? AND row BETWEEN ? AND ? ORDER BY row,col",
        (sheet_id, r1, r2),
    ):
        found = True
        h.update(exp._encoded(row, col, source, kind))
    return h.digest() if found else None


def _stream_block_64x16_seek(db, sheet_id, row_block, col_block):
    r1 = row_block * BLOCK_ROWS + 1
    r2 = r1 + BLOCK_ROWS - 1
    c1 = col_block * TILE_COLS + 1
    c2 = c1 + TILE_COLS - 1
    h = hashlib.sha256()
    found = False
    for row in range(r1, r2 + 1):
        for col, source, kind in db.execute(
            "SELECT col,source,kind FROM cells WHERE sheet_id=? AND row=? AND col BETWEEN ? AND ? ORDER BY col",
            (sheet_id, row, c1, c2),
        ):
            found = True
            h.update(exp._encoded(row, col, source, kind))
    return h.digest() if found else None


def _scan_64x16(db):
    hashers = {}
    for sheet, row, col, source, kind in db.execute(
        "SELECT sheet_id,row,col,source,kind FROM cells ORDER BY sheet_id,row,col"
    ):
        key = (sheet, (row - 1) // BLOCK_ROWS, (col - 1) // TILE_COLS)
        h = hashers.get(key)
        if h is None:
            h = hashlib.sha256()
            hashers[key] = h
        h.update(exp._encoded(row, col, source, kind))
    return {k: h.digest() for k, h in hashers.items()}


def measure_d1():
    """D1: wide-sheet digest update (10, 100, 1000 cols, median of 101) and 1M-cell full scan (median of 5)."""
    widths = {}
    with tempfile.TemporaryDirectory(prefix="dh-d1-") as temp:
        root = Path(temp)
        for cols in (10, 100, 1000):
            path = root / f"wide_{cols}.dhwb"
            book = Workbook(path, create=True)
            try:
                sheet = book.sheet("Sheet1")
                book.db.execute("BEGIN")
                book.db.executemany(
                    INSERT,
                    (
                        (sheet, row, col, str(row * cols + col), "number")
                        for row in range(1, BLOCK_ROWS + 1)
                        for col in range(1, cols + 1)
                    ),
                )
                book.db.execute("COMMIT")
                base_sum = exp.scan_digests(book.db, BLOCK_ROWS, "sha256", "sum")[(sheet, 0)]
                old_cell = (1, 1, str(cols + 1), "number")

                # Warm SQLite page cache
                _stream_block_64(book.db, sheet, 0)
                _stream_block_64x16_seek(book.db, sheet, 0, 0)

                stream_samples = []
                sum_samples = []
                tile_samples = []
                for i in range(101):
                    new_src = str(10_000_000 + i)
                    t0 = time.perf_counter()
                    _stream_block_64(book.db, sheet, 0)
                    stream_samples.append((time.perf_counter() - t0) * 1000)

                    t0 = time.perf_counter()
                    exp.updated_digest(base_sum, "sha256", old_cell, (1, 1, new_src, "number"))
                    sum_samples.append((time.perf_counter() - t0) * 1000)

                    t0 = time.perf_counter()
                    _stream_block_64x16_seek(book.db, sheet, 0, 0)
                    tile_samples.append((time.perf_counter() - t0) * 1000)

                widths[str(cols)] = {
                    "cells_in_64_rows": BLOCK_ROWS * cols,
                    "stream_64_median_ms": round(statistics.median(stream_samples), 4),
                    "stream_64_p95_ms": round(sorted(stream_samples)[95], 4),
                    "sum_64_median_ms": round(statistics.median(sum_samples), 4),
                    "stream_64x16_seek_median_ms": round(statistics.median(tile_samples), 4),
                    "stream_64x16_seek_p95_ms": round(sorted(tile_samples)[95], 4),
                }
            finally:
                book.close()

        # Full scan of 1,000,000 cells (100,000 rows x 10 cols), medians of 5.
        scan_path = root / "scan_1m.dhwb"
        book = Workbook(scan_path, create=True)
        try:
            sheet = book.sheet("Sheet1")
            book.db.execute("BEGIN")
            book.db.executemany(
                INSERT,
                (
                    (sheet, row, col, f"item-{row}" if col == 9 else str(row * col), "text" if col == 9 else "number")
                    for row in range(1, 100_001)
                    for col in range(1, 11)
                ),
            )
            book.db.execute("COMMIT")
            # Warm cache
            exp.scan_digests(book.db, BLOCK_ROWS, "sha256", "stream")

            stream_scan = []
            sum_scan = []
            tile_scan = []
            for _ in range(5):
                t0 = time.perf_counter()
                exp.scan_digests(book.db, BLOCK_ROWS, "sha256", "stream")
                stream_scan.append(time.perf_counter() - t0)

                t0 = time.perf_counter()
                exp.scan_digests(book.db, BLOCK_ROWS, "sha256", "sum")
                sum_scan.append(time.perf_counter() - t0)

                t0 = time.perf_counter()
                _scan_64x16(book.db)
                tile_scan.append(time.perf_counter() - t0)

            full_scan = {
                "cells": 1_000_000,
                "stream_64_median_s": round(statistics.median(stream_scan), 4),
                "stream_64_samples_s": [round(v, 4) for v in stream_scan],
                "sum_64_median_s": round(statistics.median(sum_scan), 4),
                "sum_64_samples_s": [round(v, 4) for v in sum_scan],
                "stream_64x16_median_s": round(statistics.median(tile_scan), 4),
                "stream_64x16_samples_s": [round(v, 4) for v in tile_scan],
            }
        finally:
            book.close()

    return {
        "meta": _meta(),
        "D1": {
            "update_by_width": widths,
            "full_scan_1m": full_scan,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measure", default="D1", choices=("D1",))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = measure_d1()
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
