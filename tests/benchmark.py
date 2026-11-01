"""Reproducible local M1 measurements; not a release qualification suite."""

import json
from contextlib import closing
import os
from pathlib import Path
import platform
import resource
import sqlite3
import statistics
import sys
import tempfile
import time

from datahorizon.client import LocalService


def measure(call, count=100):
    samples = []
    for _ in range(count):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter()-started)*1000)
    return {"samples": count, "median_ms": round(statistics.median(samples), 3),
            "p95_ms": round(sorted(samples)[int(count*.95)-1], 3)}


def main():
    with tempfile.TemporaryDirectory() as temp, LocalService() as client:
        path = Path(temp) / "benchmark.dhwb"
        book = client.open(path, create=True)
        for batch in range(10):
            cells = [{"row": batch*10000+i+1, "col": 1, "source": str(i)} for i in range(10000)]
            book.apply("edit", cells=cells)
        cold = measure(lambda: book.read(r1=50000, r2=50031, c1=1, c2=16), 1)
        viewport = measure(lambda: book.read(r1=1, r2=32, c1=1, c2=16))
        distant = measure(lambda: book.read(r1=99969, r2=100000, c1=1, c2=16))
        counter = iter(range(100))
        acknowledgement = measure(lambda: book.set(1, 2, str(next(counter))))
        book.apply("sheet_add", name="Dense")
        book.apply("edit", sheet="Dense", cells=[{"row":i//100+1,"col":i%100+1,"source":str(i)} for i in range(10000)])
        dense = measure(lambda: book.read(sheet="Dense", r1=1,r2=32,c1=1,c2=16))
        book.apply("sheet_add",name="Disk")
        disk_sheet = next(s["id"] for s in book.refresh()["sheets"] if s["name"]=="Disk")
        # Independent inserts deliberately exercise callback-free access and a
        # disk dataset larger than the declared 256 MiB service memory target.
        with closing(sqlite3.connect(path)) as direct, direct:
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'text')",
                               ((disk_sheet,i+1,"x"*1024) for i in range(280000)))
        book.refresh()
        disk = measure(lambda: book.read(sheet="Disk",r1=279969,r2=280000,c1=1,c2=16))
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            rss *= 1024
        report = {"reference_date": "2026-10-31", "platform": platform.platform(), "architecture": platform.machine(),
                  "python": platform.python_version(), "sqlite":sqlite3.sqlite_version, "cpu_count":os.cpu_count(),
                  "dataset": {"stored_cells": 390001, "shape": "100k sparse cells, 100x100 dense cells, 280k 1-KiB text cells"},
                  "transport": "authenticated loopback REST", "cold_viewport": cold, "warm_viewport": viewport,
                  "distant_viewport": distant, "edit_acknowledgement": acknowledgement,
                  "dense_viewport":dense, "large_disk_viewport":disk,
                  "database_bytes": path.stat().st_size, "driver_peak_rss_bytes": rss,
                  "limitations": ["No Windows/Linux, cold OS cache, scrolling/input-stall, frozen panes or formula-chain qualification."]}
    child_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    report["service_peak_rss_bytes"] = child_rss if sys.platform == "darwin" else child_rss*1024
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
