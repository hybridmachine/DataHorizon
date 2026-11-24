# SPDX-License-Identifier: GPL-3.0-only
"""Measurements for calculation stage 3 (docs/design/calculation-stage-3-design.md section 7)."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "service"), str(ROOT / "tests")]

from datahorizon import engine
from datahorizon.calculation_freshness import (
    CALCULATION_VERSION,
    apply as apply_verdict,
    compare as freshness_compare,
    missing_block_digests,
)
from datahorizon.calculation_index import DependentsIndex
from datahorizon.calculation_store import BLOCK_ROWS, TABLES as CALC_TABLES
from datahorizon.engine import Workbook
import calculation_experiment as exp
from calculation_fixture import create_fixture, expected_value

INSERT = "INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,?,?,?)"
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


def _populate_fixture_store(path: Path, rows: int = 100_000, prefix_rows: int = 0) -> dict:
    """Create the stage 1 fixture and fill its store with every formula result, precedent, and digest."""
    ids = create_fixture(path, rows=rows, prefix_rows=prefix_rows)
    before_bytes = path.stat().st_size
    data, summary = ids["data"], ids["summary"]
    book = Workbook(path)
    try:
        assert book.verified
        items = []
        running = 0
        for row in range(1, rows + 1):
            running += row % 97
            if row == 1:
                rects = [(data, 1, 1, 1, 1)]
            else:
                rects = [(data, row - 1, 10, row - 1, 10), (data, row, 1, row, 1)]
            items.append(((data, row, 10), float(running), rects))
            if row <= prefix_rows:
                items.append(((data, row, 11), float(running), [(data, 1, 1, row, 1)]))
        items.append(((summary, 1, 1), float(running), [(data, 1, 1, rows, 1)]))
        items.append(((summary, 2, 1), float(running), [(data, rows, 10, rows, 10)]))
        items.append(((summary, 3, 1), 0.0, [(summary, 1, 1, 1, 1), (summary, 2, 1, 2, 1)]))

        book.db.execute("BEGIN IMMEDIATE")
        book.store.write(items)
        book.db.execute("COMMIT")
        formula_count = book.store.formula_count()
    finally:
        book.close()
    after_bytes = path.stat().st_size
    return {
        **ids,
        "before_store_bytes": before_bytes,
        "after_store_bytes": after_bytes,
        "formula_count": formula_count,
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
        "update_by_width": widths,
        "full_scan_1m": full_scan,
    }


def _measure_open_breakdown(path, samples=5, *, load_index=False):
    """Measure validation+integrity, digest comparison, optional index load, and total open time."""
    totals = []
    compares = []
    validations = []
    index_loads = []
    verified_flags = []

    real_compare = engine.compare
    real_validate = Workbook.validate

    for _ in range(samples):
        rec = {"compare_s": 0.0, "validate_s": 0.0}

        def timed_compare(*args, **kwargs):
            t0 = time.perf_counter()
            res = real_compare(*args, **kwargs)
            rec["compare_s"] = time.perf_counter() - t0
            return res

        def timed_validate(self_book, *args, **kwargs):
            t0 = time.perf_counter()
            res = real_validate(self_book, *args, **kwargs)
            rec["validate_s"] = time.perf_counter() - t0
            return res

        with patch.object(engine, "compare", timed_compare), patch.object(Workbook, "validate", timed_validate):
            t0 = time.perf_counter()
            book = Workbook(path)
            total = time.perf_counter() - t0
            verified = book.verified
            if load_index:
                ti = time.perf_counter()
                book.ready()
                index_loads.append(time.perf_counter() - ti)
            book.close()
        totals.append(total)
        compares.append(rec["compare_s"])
        validations.append(rec["validate_s"])
        verified_flags.append(verified)

    out = {
        "samples": samples,
        "all_verified": all(verified_flags),
        "validation_median_s": round(statistics.median(validations), 4),
        "compare_median_s": round(statistics.median(compares), 4),
        "total_median_s": round(statistics.median(totals), 4),
        "total_samples_s": [round(v, 4) for v in totals],
        "budget_s": 5.0,
        "within_budget": statistics.median(totals) <= 5.0 and all(verified_flags),
    }
    if load_index:
        out["index_load_median_s"] = round(statistics.median(index_loads), 4)
        out["open_plus_index_median_s"] = round(
            statistics.median([t + i for t, i in zip(totals, index_loads)]), 4
        )
    return out


def measure_o1(rows=270_000, samples=5):
    """O1: opening the 270,000-row 1-KiB long-text workbook with every block digested."""
    with tempfile.TemporaryDirectory(prefix="dh-o1-") as temp:
        path = Path(temp) / "long_text.dhwb"
        book = Workbook(path, create=True)
        sid = book.sheet("Sheet1")
        book.db.execute("BEGIN")
        book.db.executemany(
            "INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'text')",
            ((sid, r, "x" * 1024) for r in range(1, rows + 1)),
        )
        book.db.execute("COMMIT")
        book.close()
        with closing(Workbook(path)) as populated:
            assert populated.verified
        size = path.stat().st_size
        res = _measure_open_breakdown(path, samples=samples)
        res.update(rows=rows, file_bytes=size)
        return res


def measure_o2_i1_f1(rows=100_000, samples=5):
    """O2 (open with full store), I1 (index load time & memory), F1 (file growth) on stage 1 fixture."""
    with tempfile.TemporaryDirectory(prefix="dh-o2-") as temp:
        path = Path(temp) / "stage1_full.dhwb"
        info = _populate_fixture_store(path, rows=rows, prefix_rows=0)

        o2 = _measure_open_breakdown(path, samples=samples, load_index=True)
        o2.update(rows=rows, formula_count=info["formula_count"], file_bytes=info["after_store_bytes"])

        # I1: measure DependentsIndex load time and tracemalloc memory over 5 runs.
        load_times = []
        mem_current = []
        mem_peak = []
        precedent_count = 0
        with closing(Workbook(path)) as book:
            for _ in range(samples):
                gc.collect()
                tracemalloc.start()
                t0 = time.perf_counter()
                idx = DependentsIndex(book.store.precedents())
                dt = time.perf_counter() - t0
                cur, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                load_times.append(dt)
                mem_current.append(cur)
                mem_peak.append(peak)
                precedent_count = len(idx)
                del idx

        i1 = {
            "samples": samples,
            "formulas_linked": precedent_count,
            "load_median_s": round(statistics.median(load_times), 4),
            "load_samples_s": [round(v, 4) for v in load_times],
            "memory_current_median_mib": round(statistics.median(mem_current) / (1024 * 1024), 2),
            "memory_peak_median_mib": round(statistics.median(mem_peak) / (1024 * 1024), 2),
            "bytes_per_formula": round(statistics.median(mem_current) / max(1, precedent_count), 1),
        }

        before_b = info["before_store_bytes"]
        after_b = info["after_store_bytes"]
        growth_b = after_b - before_b
        f1 = {
            "rows": rows,
            "formula_count": info["formula_count"],
            "before_store_bytes": before_b,
            "after_store_bytes": after_b,
            "growth_bytes": growth_b,
            "growth_mib": round(growth_b / (1024 * 1024), 2),
            "growth_percent": round(100.0 * growth_b / before_b, 2),
            "bytes_per_stored_formula": round(growth_b / max(1, info["formula_count"]), 1),
        }
        return o2, i1, f1


def measure_i2(count=2_000, samples=5):
    """I2: index.affected for 2,000 prefix sums over a 2,000-row formula column."""
    sheet = "s1"
    idx = DependentsIndex()
    for row in range(2, count + 1):
        idx.link((sheet, row, 10), [(sheet, row - 1, 10, row - 1, 10)])
    for row in range(1, count + 1):
        idx.link((sheet, row, 11), [(sheet, 1, 10, row, 10)])
    # Warm tree construction once
    idx.affected([(sheet, 1, 10, 1, 10)])

    times_ms = []
    checks_list = []
    found_count = 0
    for _ in range(samples):
        idx.checks = 0
        t0 = time.perf_counter()
        got = idx.affected([(sheet, 1, 10, 1, 10)])
        times_ms.append((time.perf_counter() - t0) * 1000)
        checks_list.append(idx.checks)
        found_count = len(got)

    return {
        "samples": samples,
        "prefix_rows": count,
        "affected_count": found_count,
        "checks_median": int(statistics.median(checks_list)),
        "time_median_ms": round(statistics.median(times_ms), 3),
        "time_samples_ms": [round(v, 3) for v in times_ms],
    }


def _measure_e1_case(affected_count: int, slice_size: int, samples: int = 5):
    """Measure edit ack for affected_count formulas in a chain, split into commit, mark, and first slice."""
    with tempfile.TemporaryDirectory(prefix=f"dh-e1-{affected_count}-") as temp:
        template = Path(temp) / "template.dhwb"
        book = Workbook(template, create=True)
        sheet = book.sheet("Sheet1")
        book.db.execute("BEGIN")
        book.db.execute("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,1,1,'1','number')", (sheet,))
        book.db.executemany(
            INSERT,
            (
                (sheet, row, 2, "=A1+1" if row == 1 else f"=B{row-1}+1", "formula")
                for row in range(1, affected_count + 1)
            ),
        )
        book.db.execute("COMMIT")
        # Populate store and digests
        verdict = freshness_compare(book.store, book.db, time.monotonic() + 30)
        book.db.execute("BEGIN IMMEDIATE")
        apply_verdict(book.store, book.db, verdict)
        book.store.clear_roots()
        items = [
            (
                (sheet, row, 2),
                float(row + 1),
                [(sheet, 1, 1, 1, 1)] if row == 1 else [(sheet, row - 1, 2, row - 1, 2)],
            )
            for row in range(1, affected_count + 1)
        ]
        book.store.write(items)
        book.db.execute("COMMIT")
        book.close()

        totals_ms = []
        commits_ms = []
        marks_ms = []
        slices_ms = []
        unserved_after = 0

        for s in range(samples):
            work_path = Path(temp) / f"run_{s}.dhwb"
            shutil.copyfile(template, work_path)
            b = Workbook(work_path)
            try:
                b.ready()  # index already loaded in an open session
                rec = {"mark_total": 0.0, "slice": 0.0}
                real_mark = b.mark
                real_settle = b.settle

                def timed_settle():
                    t0 = time.perf_counter()
                    real_settle()
                    rec["slice"] = time.perf_counter() - t0

                def timed_mark(roots=None):
                    t0 = time.perf_counter()
                    real_mark(roots)
                    rec["mark_total"] = time.perf_counter() - t0

                b.settle = timed_settle
                b.mark = timed_mark
                with patch.object(engine, "DELETE_SLICE", slice_size):
                    t0 = time.perf_counter()
                    b.mutate(dict(
                        action="edit",
                        sheet="Sheet1",
                        operation_id=uuid.uuid4().hex,
                        expected_state=b.state(),
                        cells=[dict(row=1, col=1, source="2")],
                    ))
                    total = time.perf_counter() - t0
                mark_only = max(0.0, rec["mark_total"] - rec["slice"])
                commit_only = max(0.0, total - rec["mark_total"])
                totals_ms.append(total * 1000)
                commits_ms.append(commit_only * 1000)
                marks_ms.append(mark_only * 1000)
                slices_ms.append(rec["slice"] * 1000)
                unserved_after = len(b.unserved)
            finally:
                b.close()

        return {
            "affected_formulas": affected_count,
            "delete_slice": slice_size,
            "samples": samples,
            "commit_median_ms": round(statistics.median(commits_ms), 3),
            "mark_median_ms": round(statistics.median(marks_ms), 3),
            "first_slice_median_ms": round(statistics.median(slices_ms), 3),
            "total_median_ms": round(statistics.median(totals_ms), 3),
            "total_samples_ms": [round(v, 3) for v in totals_ms],
            "unserved_remaining_after_first_slice": unserved_after,
            "budget_ms": 100.0,
            "within_budget": statistics.median(totals_ms) <= 100.0,
        }


def measure_e1(samples=5):
    """E1: edit acknowledgement for 1, 1,000, and 100,000 affected formulas (plus slice sweep at 100,000)."""
    out = {
        "1": _measure_e1_case(1, engine.DELETE_SLICE, samples=samples),
        "1000": _measure_e1_case(1_000, engine.DELETE_SLICE, samples=samples),
        "100000": _measure_e1_case(100_000, engine.DELETE_SLICE, samples=samples),
        "slice_sweep_100000": {},
    }
    for sl in (500, 1_000, 2_000, 5_000):
        out["slice_sweep_100000"][str(sl)] = _measure_e1_case(100_000, sl, samples=samples)
    return out


def measure_r1(rows=100_000, samples=5):
    """R1: first and repeat read of =SUM(Sheet1!A1:A100000), and the read after reopen."""
    with tempfile.TemporaryDirectory(prefix="dh-r1-") as temp:
        template = Path(temp) / "sum100k.dhwb"
        book = Workbook(template, create=True)
        book.mutate(dict(action="sheet_add", name="Summary", operation_id=uuid.uuid4().hex, expected_state=book.state()))
        data, summary = book.sheet("Sheet1"), book.sheet("Summary")
        book.db.execute("BEGIN")
        book.db.executemany(INSERT, ((data, r, 1, str(r % 97), "number") for r in range(1, rows + 1)))
        book.db.execute(INSERT, (summary, 1, 1, f"=SUM(Sheet1!A1:A{rows})", "formula"))
        book.db.execute("COMMIT")
        book.close()

        # Pre-digest the file (typical open state after initial open)
        with closing(Workbook(template)) as populated:
            assert populated.verified

        first_default_times = []
        first_default_displays = []
        first_2s_times = []
        repeat_times = []
        reopen_times = []

        for s in range(samples):
            path = Path(temp) / f"r1_{s}.dhwb"
            shutil.copyfile(template, path)
            b = Workbook(path)
            try:
                # 1. Default 0.25 s read on cold store
                t0 = time.perf_counter()
                cell0 = b.read("Summary", dict(r1=1, c1=1, r2=1, c2=1))["cells"][0]
                first_default_times.append(time.perf_counter() - t0)
                first_default_displays.append(cell0["display"])

                # 2. First 2 s calculation (e.g. calculation job) filling the store
                t0 = time.perf_counter()
                jobs = b.calculation_jobs()
                job = jobs.start(dict(job_id=uuid.uuid4().hex, expected_state=b.state(), sheet="Summary", r1=1, c1=1, r2=1, c2=1))
                while job["status"] == "running":
                    time.sleep(0.005)
                    job = jobs.access(job["job_id"])
                first_2s_times.append(time.perf_counter() - t0)
                assert job["status"] == "completed" and b.store.result((summary, 1, 1))[0]

                # 3. Repeat default 0.25 s read (served from store)
                t0 = time.perf_counter()
                cell1 = b.read("Summary", dict(r1=1, c1=1, r2=1, c2=1))["cells"][0]
                repeat_times.append((time.perf_counter() - t0) * 1000)
                assert cell1["calculation"] == "current"
            finally:
                b.close()

            # 4. Reopen and default 0.25 s read (served from store)
            b2 = Workbook(path)
            try:
                t0 = time.perf_counter()
                cell2 = b2.read("Summary", dict(r1=1, c1=1, r2=1, c2=1))["cells"][0]
                reopen_times.append((time.perf_counter() - t0) * 1000)
                assert cell2["calculation"] == "current" and cell2["value"] == cell1["value"]
            finally:
                b2.close()

        return {
            "samples": samples,
            "rows": rows,
            "first_default_read_median_s": round(statistics.median(first_default_times), 4),
            "first_default_read_displays": first_default_displays,
            "first_job_store_median_s": round(statistics.median(first_2s_times), 4),
            "repeat_default_read_median_ms": round(statistics.median(repeat_times), 3),
            "reopen_default_read_median_ms": round(statistics.median(reopen_times), 3),
        }


def measure_w1_w2(samples=5):
    """W1: flush_results time for 1, 256, 4,096 formulas; W2: digest fill for 100,000 and 1,000,000 cells."""
    w1 = {}
    with tempfile.TemporaryDirectory(prefix="dh-w1-") as temp:
        for count in (1, 256, 4_096):
            template = Path(temp) / f"w1_{count}.dhwb"
            book = Workbook(template, create=True)
            sheet = book.sheet("Sheet1")
            book.db.execute("BEGIN")
            book.db.executemany(INSERT, ((sheet, r, 1, str(r), "number") for r in range(1, count + 1)))
            book.db.executemany(INSERT, ((sheet, r, 2, f"=A{r}*2", "formula") for r in range(1, count + 1)))
            book.db.execute("COMMIT")
            book.close()
            # Pre-digest blocks so W1 isolates result + precedent write cost, and also test cold-digest
            with closing(Workbook(template)) as populated:
                assert populated.verified

            flush_ms = []
            read_total_ms = []
            for s in range(samples):
                path = Path(temp) / f"w1_{count}_{s}.dhwb"
                shutil.copyfile(template, path)
                b = Workbook(path)
                try:
                    rec = {"flush": 0.0}
                    real_flush = b.flush_results

                    def timed_flush(*args, **kwargs):
                        t0 = time.perf_counter()
                        res = real_flush(*args, **kwargs)
                        rec["flush"] = time.perf_counter() - t0
                        return res

                    b.flush_results = timed_flush
                    with patch.object(engine, "STORE_WRITE_SECONDS", 10.0):
                        t0 = time.perf_counter()
                        b.read("Sheet1", dict(r1=1, c1=2, r2=count, c2=2))
                        read_total = time.perf_counter() - t0
                    assert b.store.formula_count() == count
                    flush_ms.append(rec["flush"] * 1000)
                    read_total_ms.append(read_total * 1000)
                finally:
                    b.close()
            w1[str(count)] = {
                "samples": samples,
                "flush_median_ms": round(statistics.median(flush_ms), 3),
                "flush_samples_ms": [round(v, 3) for v in flush_ms],
                "read_total_median_ms": round(statistics.median(read_total_ms), 3),
            }

    w2 = {}
    with tempfile.TemporaryDirectory(prefix="dh-w2-") as temp:
        for cells_n, rows_n, cols_n in ((100_000, 100_000, 1), (1_000_000, 100_000, 10)):
            path = Path(temp) / f"w2_{cells_n}.dhwb"
            book = Workbook(path, create=True)
            sheet = book.sheet("Sheet1")
            book.db.execute("BEGIN")
            book.db.executemany(
                INSERT,
                ((sheet, r, c, str(r * c), "number") for r in range(1, rows_n + 1) for c in range(1, cols_n + 1)),
            )
            book.db.execute("COMMIT")
            block_count = (rows_n - 1) // BLOCK_ROWS + 1
            times_s = []
            for _ in range(samples):
                t0 = time.perf_counter()
                ok, computed = missing_block_digests(book.db, sheet, range(block_count), time.monotonic() + 30.0)
                assert ok and len(computed) == block_count
                book.db.execute("BEGIN IMMEDIATE")
                book.store.set_digests(computed)
                book.db.execute("COMMIT")
                times_s.append(time.perf_counter() - t0)
                book.db.execute("BEGIN IMMEDIATE")
                book.store.drop_digests([sheet])
                book.db.execute("COMMIT")
            book.close()
            w2[str(cells_n)] = {
                "samples": samples,
                "rows": rows_n,
                "cols": cols_n,
                "blocks": block_count,
                "fill_median_s": round(statistics.median(times_s), 4),
                "fill_samples_s": [round(v, 4) for v in times_s],
            }
    return w1, w2


def measure_x1(samples=5):
    """X1: refresh after a one-cell direct edit at 10,000, 100,000 and 1,000,000 cells, and store survival."""
    out = {}
    with tempfile.TemporaryDirectory(prefix="dh-x1-") as temp:
        for total_cells, rows_n, cols_n in ((10_000, 1_000, 10), (100_000, 10_000, 10), (1_000_000, 100_000, 10)):
            template = Path(temp) / f"x1_{total_cells}.dhwb"
            book = Workbook(template, create=True)
            sheet = book.sheet("Sheet1")
            book.db.execute("BEGIN")
            book.db.executemany(
                INSERT,
                ((sheet, r, c, str(r * c), "number") for r in range(1, rows_n + 1) for c in range(1, cols_n + 1)),
            )
            # One formula in last block (unrelated to block 0) to observe store survival.
            book.db.execute(INSERT, (sheet, rows_n + 100, 1, "=7+8", "formula"))
            book.db.execute("COMMIT")
            book.close()
            with closing(Workbook(template)) as populated:
                assert populated.verified
                populated.read("Sheet1", dict(r1=rows_n + 100, c1=1, r2=rows_n + 100, c2=1))
                assert populated.store.formula_count() == 1

            times_s = []
            survived = []
            for s in range(samples):
                path = Path(temp) / f"x1_{total_cells}_{s}.dhwb"
                shutil.copyfile(template, path)
                b = Workbook(path)
                try:
                    with closing(sqlite3.connect(path)) as direct, direct:
                        direct.execute("UPDATE cells SET source='99999' WHERE sheet_id=? AND row=1 AND col=1", (sheet,))
                    t0 = time.perf_counter()
                    b.info()
                    times_s.append(time.perf_counter() - t0)
                    survived.append(bool(b.verified and b.store.formula_count() == 1))
                finally:
                    b.close()
            out[str(total_cells)] = {
                "samples": samples,
                "rows": rows_n,
                "cols": cols_n,
                "refresh_median_s": round(statistics.median(times_s), 4),
                "refresh_samples_s": [round(v, 4) for v in times_s],
                "store_survived_count": sum(survived),
                "store_survived_all": all(survived),
            }
    return out


def measure_v1(samples=5):
    """V1: opening a 100-cell workbook at v1 and at v9."""
    with tempfile.TemporaryDirectory(prefix="dh-v1-") as temp:
        v9_path = Path(temp) / "small_v9.dhwb"
        book = Workbook(v9_path, create=True)
        sheet = book.sheet("Sheet1")
        book.db.execute("BEGIN")
        book.db.executemany(INSERT, ((sheet, r, 1, str(r), "number") for r in range(1, 51)))
        book.db.executemany(INSERT, ((sheet, r, 2, f"=A{r}*2", "formula") for r in range(1, 51)))
        book.db.execute("COMMIT")
        book.close()
        with closing(Workbook(v9_path)) as populated:
            populated.read("Sheet1", dict(r1=1, c1=2, r2=50, c2=2))

        v1_path = Path(temp) / "small_v1.dhwb"
        shutil.copyfile(v9_path, v1_path)
        with closing(sqlite3.connect(v1_path)) as db, db:
            # Drop all v3+ tables to leave exact v1 schema (dh_meta, sheets, cells, operations)
            for (name,) in db.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT IN ('dh_meta','sheets','cells','operations') AND name NOT GLOB 'sqlite_*'"
            ).fetchall():
                db.execute(f"DROP TABLE {name}")
            db.execute("UPDATE dh_meta SET format_version=1")

        v1_ms = []
        v9_ms = []
        for _ in range(samples):
            t0 = time.perf_counter()
            b1 = Workbook(v1_path)
            v1_ms.append((time.perf_counter() - t0) * 1000)
            b1.close()

            t0 = time.perf_counter()
            b9 = Workbook(v9_path)
            v9_ms.append((time.perf_counter() - t0) * 1000)
            b9.close()

        return {
            "samples": samples,
            "cells": 100,
            "v1_median_ms": round(statistics.median(v1_ms), 3),
            "v9_median_ms": round(statistics.median(v9_ms), 3),
        }


def measure_s1(rows=100_000, tiles=20, samples=5):
    """S1: share of store_write calls skipped while tile calculation jobs run over a 100,000-formula sheet."""
    with tempfile.TemporaryDirectory(prefix="dh-s1-") as temp:
        template = Path(temp) / "s1_100k.dhwb"
        book = Workbook(template, create=True)
        sheet = book.sheet("Sheet1")
        book.db.execute("BEGIN")
        book.db.executemany(INSERT, ((sheet, r, 1, str(r % 97), "number") for r in range(1, rows + 1)))
        book.db.executemany(INSERT, ((sheet, r, 2, f"=A{r}*3+1", "formula") for r in range(1, rows + 1)))
        book.db.execute("COMMIT")
        book.close()
        with closing(Workbook(template)) as populated:
            assert populated.verified

        shares = []
        attempts_list = []
        skips_list = []
        for s in range(samples):
            path = Path(temp) / f"s1_{s}.dhwb"
            shutil.copyfile(template, path)
            b = Workbook(path)
            try:
                attempts = 0
                real_store_write = b.store_write

                def counted_store_write(work):
                    nonlocal attempts
                    attempts += 1
                    return real_store_write(work)

                b.store_write = counted_store_write
                jobs = b.calculation_jobs()
                state = b.state()
                stop_fg = threading.Event()
                fg_errors = []

                def foreground_reads():
                    try:
                        row = 50_001
                        while not stop_fg.is_set() and row < rows:
                            b.read("Sheet1", dict(r1=row, c1=2, r2=row + 19, c2=2))
                            row += 20
                    except Exception as err:
                        fg_errors.append(err)

                fg_thread = threading.Thread(target=foreground_reads)
                fg_thread.start()
                try:
                    for idx in range(tiles):
                        r1 = idx * 100 + 1
                        r2 = r1 + 49
                        job = jobs.start(dict(
                            job_id=f"j-{s}-{idx}",
                            stream_id="viewport",
                            expected_state=state,
                            sheet="Sheet1",
                            r1=r1,
                            c1=1,
                            r2=r2,
                            c2=2,
                        ))
                        deadline = time.monotonic() + 30
                        while job["status"] == "running" and time.monotonic() < deadline:
                            time.sleep(0.005)
                            job = jobs.access(job["job_id"])
                        assert job["status"] == "completed", job
                        jobs.access(job["job_id"], "release")
                finally:
                    stop_fg.set()
                    fg_thread.join(timeout=10)
                assert not fg_errors, fg_errors
                skips = b.store_skips
                share = (skips / attempts) if attempts else 0.0
                shares.append(share)
                attempts_list.append(attempts)
                skips_list.append(skips)
            finally:
                b.close()

        return {
            "samples": samples,
            "rows": rows,
            "tiles": tiles,
            "attempts_median": int(statistics.median(attempts_list)),
            "skips_median": int(statistics.median(skips_list)),
            "skip_share_median": round(statistics.median(shares), 4),
            "skip_shares": [round(v, 4) for v in shares],
        }


def measure_all():
    report = {"meta": _meta()}
    print("Running D1...", flush=True)
    report["D1"] = measure_d1()
    print("Running O1...", flush=True)
    report["O1"] = measure_o1()
    print("Running O2, I1, F1...", flush=True)
    report["O2"], report["I1"], report["F1"] = measure_o2_i1_f1()
    print("Running I2...", flush=True)
    report["I2"] = measure_i2()
    print("Running E1...", flush=True)
    report["E1"] = measure_e1()
    print("Running R1...", flush=True)
    report["R1"] = measure_r1()
    print("Running W1, W2...", flush=True)
    report["W1"], report["W2"] = measure_w1_w2()
    print("Running X1...", flush=True)
    report["X1"] = measure_x1()
    print("Running V1...", flush=True)
    report["V1"] = measure_v1()
    print("Running S1...", flush=True)
    report["S1"] = measure_s1()
    report["meta_end"] = _meta()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--measure",
        default="all",
        choices=("all", "D1", "O1", "O2", "O1O2", "E1", "R1", "W1W2", "I2", "X1", "V1", "S1"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.measure == "all":
        report = measure_all()
    elif args.measure == "D1":
        report = {"meta": _meta(), "D1": measure_d1()}
    elif args.measure == "O1":
        report = {"meta": _meta(), "O1": measure_o1()}
    elif args.measure == "O2":
        o2, i1, f1 = measure_o2_i1_f1()
        report = {"meta": _meta(), "O2": o2, "I1": i1, "F1": f1}
    elif args.measure == "O1O2":
        o2, i1, f1 = measure_o2_i1_f1()
        report = {"meta": _meta(), "O1": measure_o1(), "O2": o2, "I1": i1, "F1": f1}
    elif args.measure == "E1":
        report = {"meta": _meta(), "E1": measure_e1()}
    elif args.measure == "R1":
        report = {"meta": _meta(), "R1": measure_r1()}
    elif args.measure == "W1W2":
        w1, w2 = measure_w1_w2()
        report = {"meta": _meta(), "W1": w1, "W2": w2}
    elif args.measure == "I2":
        report = {"meta": _meta(), "I2": measure_i2()}
    elif args.measure == "X1":
        report = {"meta": _meta(), "X1": measure_x1()}
    elif args.measure == "V1":
        report = {"meta": _meta(), "V1": measure_v1()}
    elif args.measure == "S1":
        report = {"meta": _meta(), "S1": measure_s1()}
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
