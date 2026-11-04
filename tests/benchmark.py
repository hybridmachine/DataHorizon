"""Reproducible local M1 measurements; not a release qualification suite."""

import json
from contextlib import closing
import os
from pathlib import Path
import platform
import argparse
import math
from datetime import datetime, timezone
import sqlite3
import statistics
import sys
import tempfile
import time
try:
    import resource
except ImportError:
    resource = None

from datahorizon.client import LocalService


def measure(call, count=100):
    samples = []
    for _ in range(count):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter()-started)*1000)
    return {"samples": count, "median_ms": round(statistics.median(samples), 3),
            "p95_ms": round(sorted(samples)[math.ceil(count*.95)-1], 3)}


def peak_rss(children=False):
    if resource is None:
        return None
    result = resource.getrusage(resource.RUSAGE_CHILDREN if children else resource.RUSAGE_SELF).ru_maxrss
    return result if sys.platform == "darwin" else result*1024


def formula_measurements(book, samples):
    book.apply("sheet_add", name="Chains")
    # Independent chains are placed in columns. Read each tail first so the
    # sample must traverse the complete dependency chain on every snapshot.
    results = {}
    for col, depth in enumerate((16, 64, 128, 129), 1):
        letter = chr(64+col)
        book.apply("edit", sheet="Chains", cells=[{"row":row,"col":col,
            "source":"1" if row == 1 else f"={letter}{row-1}+1"} for row in range(1,depth+1)])
        def read_tail():
            cell = book.read(sheet="Chains",r1=depth,r2=depth,c1=col,c2=col)["cells"][0]
            expected = "#LIMIT!" if depth == 129 else None
            if (expected is not None and cell["display"] != expected) or (expected is None and (cell["calculation"] != "current" or cell["value"] != depth)):
                raise AssertionError(f"Unexpected depth-{depth} chain result: {cell}")
        results[str(depth)] = {**measure(read_tail,samples),"expected": "#LIMIT!" if depth == 129 else depth,
                              "p95_target_ms":150}
    counter = iter(range(2,samples+2))
    def edit_and_recalculate():
        seed = next(counter)
        book.set(1,1,str(seed),sheet="Chains")
        result = book.read(sheet="Chains",r1=64,r2=64,c1=2,c2=2)["cells"][0]
        # Column B is independent; verify the changed chain separately too.
        changed = book.read(sheet="Chains",r1=16,r2=16,c1=1,c2=1)["cells"][0]
        if result["value"] != 64 or changed["value"] != seed+15:
            raise AssertionError("Recalculation did not use the committed inputs")
    results["edit_and_fresh_read"] = measure(edit_and_recalculate,samples)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formula-only",action="store_true",help="Skip the large-disk fixture")
    parser.add_argument("--samples",type=int,default=100)
    parser.add_argument("--output",type=Path)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    report = {"recorded_at_utc":datetime.now(timezone.utc).isoformat(), "platform":platform.platform(), "architecture":platform.machine(),
              "python":platform.python_version(),"sqlite":sqlite3.sqlite_version,"cpu_count":os.cpu_count(),
              "transport":"authenticated loopback REST", "formula_chains":None,
              "limitations":["No cold OS-cache, native GUI, frozen-pane or cross-platform qualification from a single run.",
                             "Peak RSS is unavailable on Windows in this stdlib driver; timings remain available."]}
    with tempfile.TemporaryDirectory() as temp, LocalService() as client:
        path = Path(temp) / "benchmark.dhwb"
        book = client.open(path, create=True)
        report["formula_chains"] = formula_measurements(book,args.samples)
        if args.formula_only:
            report["driver_peak_rss_bytes"] = peak_rss()
        else:
            report.update(storage_measurements(book,path,args.samples))
    report["service_peak_rss_bytes"] = peak_rss(children=True)
    output = json.dumps(report, indent=2)+"\n"
    if args.output:
        args.output.write_text(output,encoding="utf-8")
    else:
        print(output,end="")


def storage_measurements(book, path, samples):
    for batch in range(10):
        cells = [{"row": batch*10000+i+1, "col": 1, "source": str(i)} for i in range(10000)]
        book.apply("edit", cells=cells)
    cold = measure(lambda: book.read(r1=50000, r2=50031, c1=1, c2=16), 1)
    viewport = measure(lambda: book.read(r1=1, r2=32, c1=1, c2=16),samples)
    distant = measure(lambda: book.read(r1=99969, r2=100000, c1=1, c2=16),samples)
    counter = iter(range(samples))
    acknowledgement = measure(lambda: book.set(1, 2, str(next(counter))),samples)
    book.apply("sheet_add", name="Dense")
    book.apply("edit", sheet="Dense", cells=[{"row":i//100+1,"col":i%100+1,"source":str(i)} for i in range(10000)])
    dense = measure(lambda: book.read(sheet="Dense", r1=1,r2=32,c1=1,c2=16),samples)
    book.apply("sheet_add",name="Disk")
    disk_sheet = next(s["id"] for s in book.refresh()["sheets"] if s["name"]=="Disk")
    # Independent inserts deliberately exercise callback-free access and a
    # disk dataset larger than the declared 256 MiB service memory target.
    with closing(sqlite3.connect(path)) as direct, direct:
        direct.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'text')",
                           ((disk_sheet,i+1,"x"*1024) for i in range(280000)))
    book.refresh()
    disk = measure(lambda: book.read(sheet="Disk",r1=279969,r2=280000,c1=1,c2=16),samples)
    return {"dataset": {"storage_fixture_cells": 390001, "shape": "100k sparse cells, 100x100 dense cells, 280k 1-KiB text cells; formula chains are additional"},
              "cold_viewport": cold, "warm_viewport": viewport,
              "distant_viewport": distant, "edit_acknowledgement": acknowledgement,
              "dense_viewport":dense, "large_disk_viewport":disk,
              "database_bytes": path.stat().st_size, "driver_peak_rss_bytes": peak_rss()}


if __name__ == "__main__":
    main()
