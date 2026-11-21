# SPDX-License-Identifier: GPL-3.0-only
"""Measure the calculation experiment's prototypes on a generated workbook and apply the stage 1 thresholds.

Every timing is taken in this process with a warm file cache, except the dependency-data load, which runs
in a fresh process so that its memory can be separated. The exit status says whether the run completed
and its results were correct, not whether the thresholds passed.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
try:
    import resource
except ImportError:  # Windows: memory is reported as unmeasured
    resource = None
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "service"), str(ROOT / "tests")]
from datahorizon.engine import Workbook
from calculation_experiment import (HASHES, MODES, DependencyData, _NEW, _encoded, evaluate, scan_digests,
                                    sql_dependents, stream_sum, updated_digest)
from calculation_fixture import create_fixture, expected_value

BANDS = (64, 256, 1024)
MIB = 1024 * 1024
LIMITS = {"T1": 2.0, "T2": 0.001, "T3": (1.0, 64 * MIB), "T4": 0.5, "T5": 10.0, "T6": 0.25, "T7": 0.25}


def peak_rss():
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def rss_added(before):
    return None if before is None else peak_rss() - before


def median_bytes(values):
    return None if None in values else statistics.median(values)


def timed(action, runs, unit="s"):
    samples, result = [], None
    for _ in range(runs):
        start = time.perf_counter()
        result = action()
        samples.append(time.perf_counter() - start)
    return {"runs": [round(s, 6) for s in samples], "median": round(statistics.median(samples), 6), "unit": unit}, result


def child_load(path):
    """Fresh process: what loading the stored precedents costs in time and resident memory."""
    with closing(sqlite3.connect(path)) as db:
        db.execute("SELECT count(*) FROM x_precedents").fetchone()
        before, start = peak_rss(), time.perf_counter()
        data = DependencyData.load(db)
        seconds = time.perf_counter() - start
        print(json.dumps({"seconds": seconds, "rss_added": rss_added(before), "formulas": len(data.formulas())}))


def cell_key(sheet, row, col):
    return sheet << 36 | row << 15 | col


def child_load_compact(path):
    """Fresh process: the same dependency data with integer keys, as an estimate of a compact in-memory form."""
    with closing(sqlite3.connect(path)) as db:
        db.execute("SELECT count(*) FROM x_precedents_c").fetchone()
        before, start = peak_rss(), time.perf_counter()
        forward, single, columns = {}, {}, {}
        for sheet, row, col, p_sheet, r1, c1, r2, c2 in db.execute("SELECT * FROM x_precedents_c"):
            key = cell_key(sheet, row, col)
            if r1 == r2 and c1 == c2:
                target = cell_key(p_sheet, r1, c1)
                forward[key] = (*forward.get(key, ()), target)
                held = single.get(target)
                single[target] = key if held is None else (*held, key) if isinstance(held, tuple) else (held, key)
            else:
                forward[key] = (*forward.get(key, ()), (p_sheet, r1, c1, r2, c2))
                for column in range(c1, c2 + 1):
                    columns.setdefault((p_sheet, column), []).append((r1, r2, key))
        for ranges in columns.values():
            ranges.sort()
        loaded = time.perf_counter() - start
        rss = rss_added(before)
        sheet, = db.execute("SELECT min(sheet) FROM x_precedents_c WHERE col=10").fetchone()
        start, affected, pending = time.perf_counter(), set(), [(sheet, 1, 1)]
        while pending:
            s, r, c = pending.pop()
            held = single.get(cell_key(s, r, c), ())
            found = list(held if isinstance(held, tuple) else (held,))
            found += [key for r1, r2, key in columns.get((s, c), ()) if r1 <= r <= r2]
            for key in found:
                if key not in affected:
                    affected.add(key)
                    pending.append((key >> 36, key >> 15 & 0x1FFFFF, key & 0x7FFF))
        print(json.dumps({"seconds": loaded, "rss_added": rss, "formulas": len(forward),
                          "affected": len(affected), "affected_seconds": time.perf_counter() - start}))


def table_bytes(db):
    try:
        return dict(db.execute("SELECT name,sum(pgsize) FROM dbstat WHERE name LIKE 'x_%' GROUP BY name"))
    except sqlite3.Error:
        return None


def stream_block_update(db, sheet, row, band, hash_name):
    block = (row - 1) // band
    hasher = _NEW[hash_name]()
    for cell in db.execute("SELECT row,col,source,kind FROM cells WHERE sheet_id=? AND row BETWEEN ? AND ? ORDER BY row,col",
                           (sheet, block * band + 1, (block + 1) * band)):
        hasher.update(_encoded(*cell))
    return hasher.digest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--prefix-rows", type=int, default=2_000)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--child-load", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--child-load-compact", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_load:
        return child_load(args.child_load)
    if args.child_load_compact:
        return child_load_compact(args.child_load_compact)
    if args.output is None:
        parser.error("--output is required")
    rows, runs, m = args.rows, args.runs, {}
    args.prefix_rows = min(args.prefix_rows, rows)  # the fixture has no prefix formula below its last row
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        template = directory / "fixture.dhwb"
        ids = create_fixture(template, rows, args.prefix_rows)
        data_sheet, summary = ids["data"], ids["summary"]
        last, total = (data_sheet, rows, 10), (summary, 1, 1)
        fixture_bytes = template.stat().st_size
        with closing(sqlite3.connect(template)) as db:
            cells, = db.execute("SELECT count(*) FROM cells").fetchone()
            formulas, = db.execute("SELECT count(*) FROM cells WHERE kind='formula'").fetchone()

        def copy(name):
            return Path(shutil.copyfile(template, directory / name))

        def opened():
            with closing(Workbook(template)) as book:
                return book.read("Sheet1", dict(r1=1, r2=1, c1=1, c2=1))["cells"][0]["value"]
        m["open_today"], _ = timed(opened, runs)

        with closing(sqlite3.connect(template)) as db:
            m["digest_scan.fetch_only"], _ = timed(lambda: sum(1 for _ in db.execute(
                "SELECT sheet_id,row,col,source,kind FROM cells ORDER BY sheet_id,row,col")), runs)
            digests = {}
            for hash_name in HASHES:
                for mode in MODES:
                    for band in BANDS:
                        key = f"digest_scan.{hash_name}.{mode}.{band}"
                        m[key], digests[key] = timed(lambda: scan_digests(db, band, hash_name, mode), runs)
                        m[key]["blocks"] = len(digests[key])
            old = db.execute("SELECT row,col,source,kind FROM cells WHERE sheet_id=? AND row=? AND col=1",
                             (data_sheet, rows // 2)).fetchone()
            new = (*old[:2], "12345", old[3])
            for hash_name in HASHES:
                block = digests[f"digest_scan.{hash_name}.sum.256"][(data_sheet, (old[0] - 1) // 256)]
                m[f"digest_update.{hash_name}.sum"], _ = timed(lambda: updated_digest(block, hash_name, old, new), 1001)
                for band in BANDS:
                    m[f"digest_update.{hash_name}.stream.{band}"], _ = timed(
                        lambda: stream_block_update(db, data_sheet, old[0], band, hash_name), 101)

            m["precedents_build"], data = timed(lambda: DependencyData.build(db), runs)
            m["affected_memory"], affected = timed(lambda: data.dependents([(data_sheet, 1, 1)]), runs)
            m["affected_memory"]["cells"] = len(affected)

            m["recalculate_all"], results = timed(lambda: evaluate(db, data, set(data.formulas()), {}), runs)
            wrong = [key for key, want in ((last, expected_value(rows, "Sheet1", rows, 10)),
                                           *(((summary, row, 1), expected_value(rows, "Summary", row, 1)) for row in (1, 2, 3)))
                     if results[key] != want]
            if args.prefix_rows:
                prefix_last = (data_sheet, args.prefix_rows, 11)
                if results[prefix_last] != expected_value(rows, "Sheet1", args.prefix_rows, 11):
                    wrong.append(prefix_last)
            if wrong:
                print(f"Incorrect results: {wrong}", file=sys.stderr)
                return 1

            chain = {(data_sheet, row, 10) for row in range(1, rows + 1)}
            m["recalculate_chain"], _ = timed(lambda: evaluate(db, data, chain, results), runs)
            m["recalculate_chain"].update(formulas=len(chain), microseconds_per_formula=round(
                m["recalculate_chain"]["median"] / len(chain) * 1e6, 1))
            m["range_cell_by_cell"], _ = timed(lambda: evaluate(db, data, {total}, results), runs)
            m["range_streamed"], streamed = timed(lambda: stream_sum(db, results, (data_sheet, 1, 1, rows, 1)), runs)
            if streamed != results[total]:
                print("Streamed sum disagrees", file=sys.stderr)
                return 1
            if args.prefix_rows:
                prefix = {(data_sheet, row, 11) for row in range(1, args.prefix_rows + 1)}
                m["prefix_throughput"], _ = timed(lambda: evaluate(db, data, prefix, results), 1)
                read = args.prefix_rows * (args.prefix_rows + 1) // 2
                seconds = m["prefix_throughput"]["median"]
                m["prefix_throughput"].update(formulas=len(prefix), cells_read=read,
                                              formulas_per_second=round(len(prefix) / seconds),
                                              cells_per_second=round(read / seconds))

        for name, row in (("recalculate_edit_top", 1), ("recalculate_edit_bottom", rows)):
            with closing(sqlite3.connect(copy(name + ".dhwb"))) as db:
                db.execute("UPDATE cells SET source='1000' WHERE sheet_id=? AND row=? AND col=1", (data_sheet, row))
                db.commit()
                changed = data.dependents([(data_sheet, row, 1)])
                m[name], fresh = timed(lambda: evaluate(db, data, changed, results), runs)
                m[name]["formulas"] = len(changed)
                if fresh[last] != expected_value(rows, "Sheet1", rows, 10) - row % 97 + 1000:
                    print(f"Incorrect result after {name}", file=sys.stderr)
                    return 1

        stored = copy("stored.dhwb")
        with closing(sqlite3.connect(stored)) as db:
            m["precedents_save"], _ = timed(lambda: data.save(db), 1)
            m["affected_sql"], by_sql = timed(lambda: sql_dependents(db, [(data_sheet, 1, 1)]), runs)
            if by_sql != affected:
                print("SQL dependents disagree", file=sys.stderr)
                return 1
            db.execute("CREATE TABLE x_results(sheet_id TEXT NOT NULL,row INTEGER NOT NULL,col INTEGER NOT NULL,"
                       "value,error TEXT,PRIMARY KEY(sheet_id,row,col)) WITHOUT ROWID")
            db.commit()
            ordered, batches = sorted(results.items()), []
            start = time.perf_counter()
            for index in range(0, len(ordered), 1000):
                begun = time.perf_counter()
                db.executemany("INSERT INTO x_results VALUES(?,?,?,?,?)",
                               ((*key, None if isinstance(value, Exception) else value,
                                 value.code if isinstance(value, Exception) else None) for key, value in ordered[index:index + 1000]))
                db.commit()
                batches.append(time.perf_counter() - begun)
            m["results_write"] = {"total": round(time.perf_counter() - start, 6), "transactions": len(batches),
                                  "longest_transaction": round(max(batches), 6),
                                  "median_transaction": round(statistics.median(batches), 6), "unit": "s"}
            db.execute("CREATE TABLE x_digests(sheet_id TEXT NOT NULL,block INTEGER NOT NULL,digest BLOB NOT NULL,"
                       "PRIMARY KEY(sheet_id,block)) WITHOUT ROWID")
            db.executemany("INSERT INTO x_digests VALUES(?,?,?)",
                           ((*key, value) for key, value in digests["digest_scan.blake2b16.sum.256"].items()))
            db.commit()
            tables = table_bytes(db)
        grown = stored.stat().st_size
        m["file_growth"] = {"fixture_bytes": fixture_bytes, "with_derived_bytes": grown,
                            "fraction": round(grown / fixture_bytes - 1, 4), "unit": "fraction", "table_bytes": tables}

        # The same derived data with small integer sheet numbers and no secondary indexes.
        compact = copy("compact.dhwb")
        with closing(sqlite3.connect(compact)) as db:
            number = {sheet: index for index, (sheet,) in enumerate(db.execute("SELECT id FROM sheets ORDER BY id"), 1)}
            db.executescript("""
                CREATE TABLE x_precedents_c(sheet INTEGER NOT NULL,row INTEGER NOT NULL,col INTEGER NOT NULL,
                    p_sheet INTEGER NOT NULL,r1 INTEGER NOT NULL,c1 INTEGER NOT NULL,r2 INTEGER NOT NULL,c2 INTEGER NOT NULL,
                    PRIMARY KEY(sheet,row,col,p_sheet,r1,c1,r2,c2)) WITHOUT ROWID;
                CREATE TABLE x_results_c(sheet INTEGER NOT NULL,row INTEGER NOT NULL,col INTEGER NOT NULL,
                    value,error TEXT,PRIMARY KEY(sheet,row,col)) WITHOUT ROWID;""")
            db.executemany("INSERT INTO x_precedents_c VALUES(?,?,?,?,?,?,?,?)",
                           ((number[key[0]], key[1], key[2], number[r[0]], *r[1:])
                            for key, rectangles in sorted(data.precedents.items()) for r in rectangles))
            db.executemany("INSERT INTO x_results_c VALUES(?,?,?,?,?)",
                           ((number[key[0]], key[1], key[2], None if isinstance(value, Exception) else value,
                             value.code if isinstance(value, Exception) else None) for key, value in ordered))
            db.commit()
            tables = table_bytes(db)
        grown = compact.stat().st_size
        m["file_growth_compact"] = {"fixture_bytes": fixture_bytes, "with_derived_bytes": grown,
                                    "fraction": round(grown / fixture_bytes - 1, 4), "unit": "fraction", "table_bytes": tables}
        loads = [json.loads(subprocess.check_output([sys.executable, __file__, "--child-load-compact", str(compact)], text=True))
                 for _ in range(runs)]
        if any(item["affected"] != len(affected) for item in loads):
            print("Compact dependents disagree", file=sys.stderr)
            return 1
        m["precedents_load_compact"] = {"runs": [round(item["seconds"], 6) for item in loads],
                                        "median": round(statistics.median(item["seconds"] for item in loads), 6), "unit": "s"}
        m["precedents_rss_compact"] = {"runs": [item["rss_added"] for item in loads],
                                       "median": median_bytes([item["rss_added"] for item in loads]), "unit": "bytes"}
        m["affected_compact"] = {"median": round(statistics.median(item["affected_seconds"] for item in loads), 6), "unit": "s"}

        loads = [json.loads(subprocess.check_output([sys.executable, __file__, "--child-load", str(stored)], text=True))
                 for _ in range(runs)]
        m["precedents_load"] = {"runs": [round(item["seconds"], 6) for item in loads],
                                "median": round(statistics.median(item["seconds"] for item in loads), 6), "unit": "s"}
        m["precedents_rss"] = {"runs": [item["rss_added"] for item in loads],
                               "median": median_bytes([item["rss_added"] for item in loads]), "unit": "bytes"}
    m["peak_rss"] = {"median": peak_rss(), "unit": "bytes"}

    def best(mode):
        return min((m[f"digest_scan.{h}.{mode}.{b}"]["median"], f"{h}.{mode}.{b}") for h in HASHES for b in BANDS)
    best_sum, best_stream = best("sum"), best("stream")
    measured = {
        "T1": {"best_sum": best_sum, "best_stream": best_stream, "fetch_only": m["digest_scan.fetch_only"]["median"],
               "value": min(best_sum[0], best_stream[0])},
        "T2": {"sum": {h: m[f"digest_update.{h}.sum"]["median"] for h in HASHES},
               "stream": {f"{h}.{b}": m[f"digest_update.{h}.stream.{b}"]["median"] for h in HASHES for b in BANDS},
               "value": min(m[f"digest_update.{h}.sum"]["median"] for h in HASHES)},
        "T3": {"value": [m["precedents_load"]["median"], m["precedents_rss"]["median"]],
               "compact": [m["precedents_load_compact"]["median"], m["precedents_rss_compact"]["median"]]},
        "T4": {"value": m["affected_memory"]["median"], "sql": m["affected_sql"]["median"]},
        "T5": {"value": m["recalculate_chain"]["median"], "edit_top_all_dependents": m["recalculate_edit_top"]["median"]},
        "T6": {"value": m["range_streamed"]["median"], "cell_by_cell": m["range_cell_by_cell"]["median"]},
        "T7": {"value": m["file_growth"]["fraction"], "compact": m["file_growth_compact"]["fraction"]},
    }
    thresholds = {}
    for name, limit in LIMITS.items():
        value = measured[name]["value"]
        if isinstance(limit, tuple) and None in value:
            passed = None  # part of the measure is unavailable on this platform
        else:
            passed = all(v <= l for v, l in zip(value, limit)) if isinstance(limit, tuple) else value <= limit
        thresholds[name] = {"limit": limit, "measured": measured[name], "passed": passed}
    report = {"date_utc": datetime.now(timezone.utc).isoformat(),
              "host": {"platform": platform.platform(), "machine": platform.machine(),
                       "python": platform.python_version(), "sqlite": sqlite3.sqlite_version},
              "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "source_sha256": {name: hashlib.sha256((ROOT / "tests" / name).read_bytes()).hexdigest()
                                for name in ("calculation_benchmark.py", "calculation_experiment.py", "calculation_fixture.py")},
              "fixture": {"rows": rows, "prefix_rows": args.prefix_rows, "cells": cells, "formulas": formulas,
                          "native_bytes": fixture_bytes},
              "runs": runs, "measurements": m, "thresholds": thresholds}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    for name, item in thresholds.items():
        print(f"{name}: {'unmeasured' if item['passed'] is None else 'pass' if item['passed'] else 'FAIL'} measured {item['measured']['value']} limit {item['limit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
