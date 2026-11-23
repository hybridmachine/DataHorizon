# SPDX-License-Identifier: GPL-3.0-only
"""Measure stage 2 of the calculation design: iterative evaluation and streamed ranges, through Workbook only.

S1 a 100,000-cell SUM in one default read; S2 the longest chain one read completes; S3 the cost per stored cell of a
streamed range by kind; S4 conditional aggregates and a lookup; S5 invalidation with 4,096 cached range readers;
S6 peak memory of a SUM over 1,000,000 stored cells. Timings are medians of five with a warm file cache. The exit
status says whether the run completed and its results were correct, not whether the targets were met.
"""
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
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "service"), str(ROOT / "tests")]
from datahorizon import formula
from datahorizon.engine import Workbook
from datahorizon.formula import FormulaError
from calculation_benchmark import peak_rss, rss_added, timed
from calculation_fixture import create_fixture, expected_value

TARGETS = {"S1": 0.25}
INSERT = "INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,?,?,?)"


def build(path, cells, temporal=False):
    """A workbook whose Sheet1 holds the (row, col, source, kind) tuples given; temporal inputs need native format 2."""
    book = Workbook(path, create=True)
    try:
        sheet = book.sheet("Sheet1")
        book.db.execute("BEGIN")
        book.db.executemany(INSERT, ((sheet, *cell) for cell in cells))
        if temporal:
            book.db.execute("UPDATE dh_meta SET format_version=2 WHERE format_version=1")
        book.db.execute("COMMIT")
    finally:
        book.close()


def read_cell(book, sheet, row, col):
    return book.read(sheet, dict(r1=row, r2=row, c1=col, c2=col))["cells"][0]


def chain_cells(rows):
    for row in range(1, rows + 1):
        yield row, 1, str(row % 97), "number"
        yield row, 10, "=A1" if row == 1 else f"=J{row - 1}+A{row}", "formula"


def data_cells(rows):
    """A numbers, B 40-character text, C dates, D formula results, E row numbers, F row % 1000, H probes."""
    for row in range(1, rows + 1):
        yield row, 1, str(row % 97), "number"
        yield row, 2, f"{row:08d}" + "x" * 32, "text"
        yield row, 3, f"2026-{row % 12 + 1:02d}-{row % 28 + 1:02d}", "date"
        yield row, 4, f"=A{row}", "formula"
        yield row, 5, str(row), "number"
        yield row, 6, str(row % 1000), "number"


def run_probe(book, source, row=1, col=20):
    """Evaluate one formula placed at (row, col) with a fresh 60 s plain calculator; returns (seconds, value)."""
    sheet = book.sheet("Sheet1")
    book.db.execute("INSERT OR REPLACE INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,?,?,'formula')", (sheet, row, col, source))
    calc = book.calculator(seconds=60, transactional=True)
    start = time.perf_counter()
    value = calc.cell(sheet, row, col)
    return time.perf_counter() - start, value


def probe(book, source, runs):
    samples, value = [], None
    for _ in range(runs):
        seconds, value = run_probe(book, source)
        samples.append(seconds)
    return {"runs": [round(s, 6) for s in samples], "median": round(statistics.median(samples), 6), "unit": "s"}, value


def s1(directory, rows, runs):
    """The read shows a value, or #LIMIT! when it misses its budget; both are recorded. The 2 s figure says how far off it is."""
    path = directory / "fixture.dhwb"
    create_fixture(path, rows, 0)
    with closing(Workbook(path)) as book:
        want = expected_value(rows, "Summary", 1, 1)
        shown = []

        def once():
            book.dependencies.clear()  # a read must not be answered from the in-memory cache
            cell = read_cell(book, "Summary", 1, 1)
            if cell["calculation"] == "current" and cell["value"] != want or cell["calculation"] != "current" and cell["display"] != "#LIMIT!":
                raise SystemExit(f"S1 gave {cell}")
            shown.append(cell["display"])
        result, _ = timed(once, runs)
        result.update(cells=rows, displayed=shown, completed_runs=sum(1 for d in shown if d != "#LIMIT!"))

        def long_read():
            calc = book.calculator(seconds=2, transactional=True)
            return calc.cell(book.sheet("Summary"), 1, 1)
        long_time, value = timed(long_read, runs)
        if value != want:
            raise SystemExit(f"S1 with a 2 s budget gave {value}")
        long_time.update(cells=rows, microseconds_per_cell=round(long_time["median"] / rows * 1e6, 2))
        result["with_2s_budget"] = long_time
    return result


def s2(directory, runs, top):
    """Chains of 256, 512, ... rows read at their last cell, every length up to top, three ways. The doubling stops being
    informative where a longer chain completes after a shorter one has not, so no length is skipped after a failure."""
    path = directory / "chain.dhwb"
    build(path, chain_cells(top))
    out = {}
    with closing(Workbook(path)) as book:
        sheet = book.sheet("Sheet1")

        def default_read(n):
            book.dependencies.clear()
            start = time.perf_counter()
            cell = read_cell(book, "Sheet1", n, 10)
            return time.perf_counter() - start, cell

        def calculated(transactional):
            def attempt(n):
                book.dependencies.clear()
                calc = book.calculator(seconds=2, transactional=transactional)
                start = time.perf_counter()
                try:
                    value = calc.cell(sheet, n, 10)
                    return time.perf_counter() - start, {"calculation": "current", "value": value}
                except FormulaError as error:
                    return time.perf_counter() - start, {"calculation": "error", "display": error.code}
            return attempt

        for name, attempt in (("default_read_0.25s", default_read), ("list_budget_2s_cached", calculated(False)),
                              ("list_budget_2s_plain", calculated(True))):
            steps, first_failed, longest_before, largest = [], None, 0, 0
            n = 256
            while n <= top:
                seconds, cell = attempt(n)
                ok = cell["calculation"] == "current"
                if ok and cell["value"] != expected_value(top, "Sheet1", n, 10):
                    raise SystemExit(f"S2 {name} gave a wrong value at {n}")
                if not ok and cell["display"] != "#LIMIT!":
                    raise SystemExit(f"S2 {name} gave {cell} at {n}")
                steps.append({"length": n, "seconds": round(seconds, 4), "completed": ok})
                if ok:
                    largest = n
                    if first_failed is None:
                        longest_before = n
                elif first_failed is None:
                    first_failed = n
                n *= 2
            entry = {"longest_completed_before_first_failure": longest_before, "first_failure": first_failed,
                     "largest_completed": largest, "steps": steps}
            if longest_before:
                times = [attempt(longest_before)[0] for _ in range(runs)]
                entry["time_at_longest_before_first_failure"] = {"runs": [round(t, 4) for t in times],
                                                                 "median": round(statistics.median(times), 4), "unit": "s"}
            out[name] = entry
    return out


def s3_s4(directory, rows, runs):
    path = directory / "data.dhwb"
    build(path, data_cells(rows), temporal=True)
    m = {}
    with closing(Workbook(path)) as book:
        kinds = {"numbers": "A", "text_40": "B", "dates": "C", "formula_results": "D"}
        for name, letter in kinds.items():
            m[f"S3.{name}"], value = probe(book, f"=SUM({letter}1:{letter}{rows})", runs)
            m[f"S3.{name}"].update(cells=rows, microseconds_per_cell=round(m[f"S3.{name}"]["median"] / rows * 1e6, 2))
            if name == "numbers" and value != sum(r % 97 for r in range(1, rows + 1)):
                raise SystemExit("S3 numbers sum is wrong")
        count = sum(1 for row in range(1, rows + 1) if row % 97 > 50)
        m["S4.countif"], value = probe(book, f'=COUNTIF(A1:A{rows},">50")', runs)
        if value != count:
            raise SystemExit("S4 COUNTIF is wrong")
        wants = sum(row for row in range(1, rows + 1) if row % 97 > 10 and row % 1000 < 500)
        m["S4.sumifs_3_criteria"], value = probe(book, f'=SUMIFS(E1:E{rows},A1:A{rows},">10",F1:F{rows},"<500",B1:B{rows},"<>x")', runs)
        if value != wants:
            raise SystemExit(f"S4 SUMIFS gave {value}, expected {wants}")
        m["S4.xlookup_last_row"], value = probe(book, f"=XLOOKUP({rows},E1:E{rows},A1:A{rows})", runs)
        if value != rows % 97:
            raise SystemExit("S4 XLOOKUP is wrong")
        for key in ("S4.countif", "S4.sumifs_3_criteria", "S4.xlookup_last_row"):
            m[key].update(rows=rows, within_default_budget=m[key]["median"] <= 0.25)
    return m


def s5(directory, runs):
    """invalidate for one edit while 4,096 formulas that read ranges are cached."""
    out = {}
    for name, shape in (("ten_cell_ranges_edit_inside_one", lambda r: f"=SUM(A{r}:A{r + 9})"),
                        ("all_readers_contain_the_edit", lambda r: f"=SUM(Z1:Z{r})")):
        path = directory / f"s5-{name}.dhwb"
        build(path, [(r, 1, "1", "number") for r in range(1, 4106)] + [(r, 20, shape(r), "formula") for r in range(1, 4097)])
        with closing(Workbook(path)) as book:
            sheet, samples, checks, affected = book.sheet("Sheet1"), [], [], []
            for _ in range(runs):
                book.dependencies.clear()
                calc = book.calculator(seconds=30)
                for r in range(1, 4097):
                    calc.cell(sheet, r, 20)
                held, recorded = len(book.dependencies.values), sum(len(v) for v in book.dependencies.ranges.values())
                before = book.dependencies.checks
                start = time.perf_counter()
                hit = book.dependencies.invalidate([(sheet, 2000, 1 if name.startswith("ten") else 26)])
                samples.append(time.perf_counter() - start)
                checks.append(book.dependencies.checks - before)
                affected.append(len(hit))
                after = len(book.dependencies.values)  # 0 when invalidation passed its check budget and forgot everything
            out[name] = {"runs": [round(s, 6) for s in samples], "median": round(statistics.median(samples), 6), "unit": "s",
                         "cached_before": held, "range_edges_recorded": recorded,
                         "checks": checks[-1], "invalidated": affected[-1], "cached_after": after}
    return out


def child_sum(path):
    """Fresh process: peak resident memory added by one SUM over every stored cell of column A."""
    with closing(Workbook(path)) as book:
        sheet = book.sheet("Sheet1")
        calc = book.calculator(seconds=120, transactional=True)
        before, start = peak_rss(), time.perf_counter()
        value = calc.cell(sheet, 1, 3)
        print(json.dumps({"seconds": time.perf_counter() - start, "rss_added": rss_added(before), "peak": peak_rss(),
                          "value": value, "cache": len(calc.cache)}))


def s6(directory, runs, cells=1_000_000):
    path = directory / "million.dhwb"
    build(path, [*((r, 1, str(r % 97), "number") for r in range(1, cells + 1)), (1, 3, f"=SUM(A1:A{cells})", "formula")])
    loads = [json.loads(subprocess.check_output([sys.executable, __file__, "--child-sum", str(path)], text=True)) for _ in range(runs)]
    want = sum(r % 97 for r in range(1, cells + 1))
    if any(item["value"] != want for item in loads):
        raise SystemExit("S6 sum is wrong")
    rss = [item["rss_added"] for item in loads]
    return {"cells": cells, "seconds": {"runs": [round(i["seconds"], 3) for i in loads],
                                        "median": round(statistics.median(i["seconds"] for i in loads), 3), "unit": "s"},
            "rss_added": {"runs": rss, "median": None if None in rss else statistics.median(rss), "unit": "bytes"},
            "peak_rss": {"runs": [i["peak"] for i in loads], "unit": "bytes"}, "calculator_cache_entries": loads[0]["cache"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--chain-rows", type=int, default=262_144, help="largest chain tried by S2")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--child-sum", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_sum:
        return child_sum(args.child_sum)
    if args.output is None:
        parser.error("--output is required")
    m = {}
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        m["S1"] = s1(directory, args.rows, args.runs)
        m["S2"] = s2(directory, args.runs, args.chain_rows)
        m.update(s3_s4(directory, args.rows, args.runs))
        m["S5"] = s5(directory, args.runs)
        m["S6"] = s6(directory, args.runs)
    m["S1"]["target"] = TARGETS["S1"]
    m["S1"]["met"] = m["S1"]["completed_runs"] == args.runs and m["S1"]["median"] <= TARGETS["S1"]
    report = {"date_utc": datetime.now(timezone.utc).isoformat(),
              "host": {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
                       "sqlite": sqlite3.sqlite_version, "cpu_count": os.cpu_count(),
                       "load_average_at_end": [round(x, 2) for x in os.getloadavg()]},
              "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_sha256": {name: hashlib.sha256((ROOT / "tests" / name).read_bytes()).hexdigest()
                                for name in ("calculation_stage2_benchmark.py", "calculation_benchmark.py", "calculation_fixture.py")},
              "limits": {"FORMULA_CELL_LIMIT": formula.FORMULA_CELL_LIMIT, "SCAN_ROWS": formula.SCAN_ROWS},
              "rows": args.rows, "runs": args.runs, "measurements": m}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"S1: {m['S1']['median']} s, shown {m['S1']['displayed']} (target {TARGETS['S1']}, {'met' if m['S1']['met'] else 'MISSED'}); "
          f"2 s budget {m['S1']['with_2s_budget']['median']} s")
    for name, item in m["S2"].items():
        print(f"S2 {name}: longest before first failure {item['longest_completed_before_first_failure']}, "
              f"first failure {item['first_failure']}, largest completed {item['largest_completed']}")
    for key, item in m.items():
        if key.startswith(("S3", "S4")):
            print(f"{key}: {item['median']} s" + (f", {item['microseconds_per_cell']} us/cell" if "microseconds_per_cell" in item else ""))
    for key, item in m["S5"].items():
        print(f"S5 {key}: {item['median']} s, {item['checks']} checks, {item['invalidated']} invalidated, {item['cached_after']} cached after")
    print(f"S6: {m['S6']['seconds']['median']} s, rss added {m['S6']['rss_added']['median']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
