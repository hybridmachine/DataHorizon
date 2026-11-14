# SPDX-License-Identifier: GPL-3.0-only
"""Chart renderer measurements: run the renderer and the Qt SVG probe, apply the
thresholds from docs/design/charts-stage-1-plan.md and write the evidence.

    PYTHONPATH=service python3 tests/chart_render_benchmark.py --probe build/chart_svg_probe \\
        --work build/chart-render --evidence docs/design/evidence/charts-stage-2 [--font Arial]

Two further measurements from the charts stage 2 plan run instead of the
renderer benchmark when asked for:

    --pie-sizes 500,1000,2000   Qt load plus paint of one-series pies; writes pie-limit.json
    --service                   resolve and render through LocalService; writes capacity.json
    --sources                   table and saved-query charts through LocalService, then the query
                                chart on a workbook padded with 200,000 cells; writes sources.json.
                                Its workbook is in a temporary directory removed at the end; --work
                                is not used
"""

import argparse
import json
import math
import pathlib
import platform
import shutil
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from datahorizon import chart_font
from datahorizon import chart_render as cr
from datahorizon.formula import column_name

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "charts"
RUNS = 20
TYPE_FIXTURE = {"column": "column_clustered", "bar": "bar_clustered", "line": "line",
                "area": "area_stacked", "pie": "pie", "scatter": "scatter"}
STRESS = ["WWWWWWWWWW", "iiiiiiiiii", "1,234,567.89", "Quarterly revenue (EUR)",
          "Ærøskøbing Größe façade"]
LIMITS = {"render_ms_10k": 250.0, "svg_bytes_10k": 4 * 1024 * 1024, "qt_ms_10k": 150.0, "total_ms_12": 50.0}
TEXT_MAX_UNDER = 1.05
TEXT_MEDIAN_OVER = 1.35
FONT_TEXT = "Quarterly revenue by region, 2026 (EUR) Wg"
FONT_PX = 24
FONT_RATIO = (0.95, 1.05)
UNKNOWN_FAMILY = "NoSuchFamily Zq"
PIE_BUDGET_MS = LIMITS["qt_ms_10k"] / 2
PIE_FALLBACK = 250
JOB_SECONDS = 30
RESULT_BYTES = 4 * 1024 * 1024
SERVICE_RUNS = 3


def _text_ok(text) -> bool:
    return text["max_under"] <= TEXT_MAX_UNDER and text["median_over"] <= TEXT_MEDIAN_OVER


def evaluate(measurements: dict) -> dict:
    """Apply the plan's thresholds; returns the route and what failed."""
    failed = [f"{key}.{kind}" for key, limit in LIMITS.items()
              for kind, value in measurements[key].items() if value > limit]
    if not measurements["all_valid"]:
        failed.append("all_valid")
    hard = bool(failed)
    table = measurements.get("text_with_table")
    text_ok = _text_ok(measurements["text"])
    table_ok = table is not None and _text_ok(table)
    if not text_ok:
        failed.append("text")
        if table is not None and not table_ok:
            failed.append("text_with_table")
    if hard or not (text_ok or table_ok):
        return {"route": "fallback_qcustomplot", "failed": failed}
    return {"route": "service_svg" if text_ok else "service_svg_with_metrics_table", "failed": failed}


def font_svg(family: str) -> str:
    """One line of text in ``family``, for checking which font Qt SVG draws it in."""
    return ('<svg xmlns="http://www.w3.org/2000/svg" version="1.2" baseProfile="tiny" width="800" height="60" '
            f'viewBox="0 0 800 60"><text x="10" y="40" font-family="{family}" font-size="{FONT_PX}" '
            f'fill="#000000">{FONT_TEXT}</text></svg>')


def font_check(bundled_px: float, unknown_px: float, laid_out_px: float) -> dict:
    """Compare ink widths drawn through Qt SVG with the width laid out from the metrics table."""
    bundled, unknown = bundled_px / laid_out_px, unknown_px / laid_out_px
    return {"laid_out_px": laid_out_px, "bundled_px": bundled_px, "unknown_family_px": unknown_px,
            "bundled_ratio": bundled, "unknown_family_ratio": unknown,
            "ok": FONT_RATIO[0] <= bundled <= FONT_RATIO[1]}


def measure_font_in_svg(probe: pathlib.Path, work: pathlib.Path) -> dict:
    """Draw one line through QSvgRenderer in the bundled family and in an unknown one."""
    widths = {}
    for name, family in (("bundled", chart_font.FAMILY), ("unknown", UNKNOWN_FAMILY)):
        svg_path, report_path = work / f"font-{name}.svg", work / f"font-{name}.json"
        svg_path.write_text(font_svg(family), encoding="utf-8")
        _run([str(probe), "--font-file", str(chart_font.font_path()), "--ink-width", str(svg_path),
              "--report", str(report_path)])
        widths[name] = json.loads(report_path.read_text(encoding="utf-8"))["ink_px"]
    laid_out = cr.estimate_text_width(FONT_TEXT, FONT_PX, chart_font.default_metrics())
    return font_check(widths["bundled"], widths["unknown"], laid_out)


def pie_sizes(text: str) -> list[int]:
    """Parse ``--pie-sizes``: positive integers separated by commas, returned in order."""
    sizes = sorted(int(part) for part in text.split(","))
    if not sizes or sizes[0] < 1:
        raise ValueError("pie sizes are positive integers")
    return sizes


def choose_pie_limit(medians: dict) -> dict:
    """The largest pie size whose Qt load plus paint is within half the 150 ms threshold."""
    passing = [size for size, ms in medians.items() if ms <= PIE_BUDGET_MS]
    if not passing:
        return {"limit": PIE_FALLBACK, "fallback": True}
    return {"limit": max(passing), "fallback": False}


def capacity_failures(runs: dict) -> list[str]:
    """Name each chart whose job erred, overran its budget or exceeded the result limit."""
    failed = []
    for name, run in runs.items():
        if run["error"] is not None:
            failed.append(f"{name}.error")
        if any(seconds > JOB_SECONDS for seconds in run["wall_s"]):
            failed.append(f"{name}.wall_s")
        if any(size > RESULT_BYTES for size in run["result_bytes"]):
            failed.append(f"{name}.result_bytes")
    return sorted(failed)


def _large(kind: str, count: int | None = None) -> dict:
    if count is None:
        count = cr.LIMIT_PIE_POINTS if kind == "pie" else cr.LIMIT_POINTS
    values = [math.sin(i / 50) * 100 + 100 for i in range(count)]
    return {"type": kind, "grouping": "clustered" if kind in ("column", "bar", "area") else None,
            "title": f"{count:,} points", "categories": None if kind == "scatter" else [str(i) for i in range(count)],
            "series": [{"name": "Series", "values": values,
                        "x": [float(i) for i in range(count)] if kind == "scatter" else None}],
            "axes": {"category": {"title": None}, "value": {"title": None, "min": None, "max": None}},
            "legend": {"position": "none"}}


def _timed(spec, width, height):
    samples = []
    for _ in range(RUNS):
        start = time.perf_counter()
        out = cr.render(spec, width, height)
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples), out["svg"]


def _labels(svgs) -> list[dict]:
    seen = {}
    for svg in svgs:
        for element in ET.fromstring(svg).iter():
            if element.tag.endswith("text") and element.text:
                seen[(element.text, float(element.get("font-size")))] = None
    for text in STRESS:
        seen[(text, cr.LABEL_PX)] = None
    table = chart_font.default_metrics()
    return [{"text": text, "size_px": size, "estimate": cr.estimate_text_width(text, size, table)} for text, size in seen]


def _text_stats(rows, table=None) -> dict:
    """Measured over estimated width; ``table`` defaults to the bundled font's."""
    table = chart_font.default_metrics() if table is None else table
    under, over = [], []
    for row in rows:
        estimate = cr.estimate_text_width(row["text"], row["size_px"], table)
        for measured in (row["default_px"], row["substituted_px"]):
            if measured and estimate:
                under.append(measured / estimate)
                over.append(estimate / measured)
    return {"max_under": max(under), "median_over": statistics.median(over)}


def copy_screenshots(charts: dict, names, png_dir: pathlib.Path, shots: pathlib.Path) -> list[str]:
    """Replace ``shots`` with the screenshots of the fixtures Qt rendered.

    The probe writes no PNG for an SVG it rejects, so those are left out and
    the evidence records them as invalid instead.
    """
    shutil.rmtree(shots, ignore_errors=True)
    shots.mkdir(parents=True)
    copied = []
    for name in names:
        source = png_dir / f"{name}.png"
        if charts.get(f"{name}.svg", {}).get("valid") and source.is_file():
            shutil.copyfile(source, shots / f"{name}.png")
            copied.append(name)
    return copied


def _run(command):
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        sys.exit(f"{command[0]} failed ({done.returncode}): {done.stderr.strip()}")


def _machine(report=None) -> dict:
    machine = {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version()}
    if report is not None:
        machine.update(qt=report["qt"], qt_platform=report["platform"])
    return machine


def _write(path: pathlib.Path, evidence: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def measure_pies(probe: pathlib.Path, work: pathlib.Path, evidence_dir: pathlib.Path, sizes: list[int]) -> dict:
    """Render one-series pies of each size at 800x500 and time them in Qt (medians of 20)."""
    svg_dir, png_dir = work / "pie-svg", work / "pie-png"
    for folder in (svg_dir, png_dir):
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
    limit = cr.LIMIT_PIE_POINTS
    cr.LIMIT_PIE_POINTS = max(limit, *sizes)  # measure sizes above the current limit too
    try:
        svg_bytes = {}
        for size in sizes:
            data = cr.render(_large("pie", size), 800, 500)["svg"].encode("utf-8")
            (svg_dir / f"pie_{size}.svg").write_bytes(data)
            svg_bytes[size] = len(data)
    finally:
        cr.LIMIT_PIE_POINTS = limit
    labels_path, report_path = work / "pie-labels.json", work / "pie-report.json"
    labels_path.write_text("[]", encoding="utf-8")
    _run([str(probe), "--svg-dir", str(svg_dir), "--out", str(png_dir), "--labels", str(labels_path),
          "--report", str(report_path), "--font-file", str(chart_font.font_path())])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    charts = {item["file"]: item for item in report["charts"]}
    medians = {size: charts[f"pie_{size}.svg"]["load_ms"] + charts[f"pie_{size}.svg"]["paint_ms"] for size in sizes}
    evidence = {
        "measurement": "charts stage 2 pie point limit",
        "machine": _machine(report),
        "size_px": [800, 500],
        "runs_per_median": RUNS,
        "budget_ms": PIE_BUDGET_MS,
        "fallback": PIE_FALLBACK,
        "pies": [{"points": size, "svg_bytes": svg_bytes[size], "valid": charts[f"pie_{size}.svg"]["valid"],
                  "load_ms": charts[f"pie_{size}.svg"]["load_ms"], "paint_ms": charts[f"pie_{size}.svg"]["paint_ms"],
                  "qt_ms": medians[size]} for size in sizes],
        "result": choose_pie_limit(medians),
    }
    _write(evidence_dir / "pie-limit.json", evidence)
    return evidence


ROWS = 10_000
VALUE_COLUMNS = 10
ROWS_PER_EDIT = 900  # 9,900 cells: an edit admits at most 10,000


def _capacity_cells(first: int, last: int) -> list[dict]:
    """Category text in column A; columns B-K alternate numbers and formulas on the column to the left."""
    cells = []
    for row in range(first, last + 1):
        if row == 1:
            cells.append({"row": 1, "col": 1, "source": "Item"})
            cells += [{"row": 1, "col": col, "source": f"V{col - 1}"} for col in range(2, VALUE_COLUMNS + 2)]
            continue
        cells.append({"row": row, "col": 1, "source": f"Item {row - 1}"})
        for col in range(2, VALUE_COLUMNS + 2):
            if col % 2 == 0:
                source = str((row * 7 + col) % 1000)
            else:
                source = f"={column_name(col - 1)}{row}*2+1"
            cells.append({"row": row, "col": col, "source": source})
    return cells


def _range(col: int, first: int, last: int) -> dict:
    return {"kind": "range", "sheet": "Sheet1", "ref": f"{column_name(col)}{first}:{column_name(col)}{last}"}


def _name(col: int) -> dict:
    return {"source": {"kind": "range", "sheet": "Sheet1", "ref": f"{column_name(col)}1"}}


def _capacity_specs() -> dict:
    """Charts to measure, each with the jobs to time: name -> (specification, kinds)."""
    last = ROWS + 1
    categories = {"source": _range(1, 2, last)}
    specs = {"line_8_series": ({"spec_version": 1, "type": "line", "categories": categories,
                                "series": [{"name": _name(col), "values": {"source": _range(col, 2, last)}}
                                           for col in range(2, 10)],
                                "legend": {"position": "bottom"}}, ("resolve",))}
    # The largest renders admitted (LIMIT_RENDER_POINTS values), over a formula column; column is also resolved.
    for kind in ("column", "bar", "line", "area", "scatter"):
        series = {"name": _name(3), "values": {"source": _range(3, 2, last)}}
        spec = {"spec_version": 1, "type": kind, "series": [series], "legend": {"position": "none"}}
        if kind == "scatter":
            series["x"] = {"source": _range(2, 2, last)}
        else:
            spec["categories"] = categories
        specs[f"{kind}_10k_points"] = (spec, ("resolve", "render") if kind == "column" else ("render",))
    return specs


def _cells(spec: dict) -> int:
    parts = [spec["categories"]] if spec.get("categories") else []
    for item in spec["series"]:
        parts += [part for part in (item["name"], item["values"], item.get("x")) if part and "source" in part]
    total = 0
    for part in parts:
        ref = part["source"]["ref"]
        first, _, last = ref.partition(":")
        total += 1 if not last else int(last[1:]) - int(first[1:]) + 1
    return total


def measure_service(work: pathlib.Path, evidence_dir: pathlib.Path) -> dict:
    """Time resolve_chart and render_chart through a LocalService over 10,000 rows x 11 columns."""
    from datahorizon.client import APIError, LocalService

    work.mkdir(parents=True, exist_ok=True)
    path = work / "capacity.dhwb"
    for leftover in work.glob("capacity.dhwb*"):
        leftover.unlink()
    runs = {}
    with LocalService() as client, client.open(path, create=True) as book:
        start = time.perf_counter()
        for first in range(1, ROWS + 2, ROWS_PER_EDIT):
            book.apply("edit", "Sheet1", cells=_capacity_cells(first, min(first + ROWS_PER_EDIT - 1, ROWS + 1)))
        fill_s = time.perf_counter() - start
        for name, (spec, kinds) in _capacity_specs().items():
            chart_id = book.create_chart(spec, size={"width": 800, "height": 500})["chart_id"]
            for kind in kinds:
                run = {"wall_s": [], "result_bytes": [], "error": None}
                for _ in range(SERVICE_RUNS):
                    start = time.perf_counter()
                    try:
                        # The SDK waits longer than the job's own 30-second budget, so the service decides.
                        if kind == "resolve":
                            result = book.resolve_chart(chart_id, timeout=JOB_SECONDS + 15)
                        else:
                            result = book.render_chart(chart_id, 800, 500, timeout=JOB_SECONDS + 15)
                    except (APIError, TimeoutError) as error:
                        run["error"] = error.body.get("code") if isinstance(error, APIError) else "SDK_TIMEOUT"
                        break
                    run["wall_s"].append(time.perf_counter() - start)
                    run["result_bytes"].append(len(json.dumps(result).encode("utf-8")))
                    run["counts"] = result["counts"]
                runs[f"{name}.{kind}"] = run
    evidence = {
        "measurement": "charts stage 2 capacity through the service",
        "machine": _machine(),
        "workbook": {"rows": ROWS, "columns": VALUE_COLUMNS + 1, "formula_columns": VALUE_COLUMNS // 2,
                     "fill_seconds": fill_s, "edits": len(range(1, ROWS + 2, ROWS_PER_EDIT))},
        "charts": {name: {"type": spec["type"], "series": len(spec["series"]), "source_cells": _cells(spec)}
                   for name, (spec, _) in _capacity_specs().items()},
        "size_px": [800, 500],
        "runs": SERVICE_RUNS,
        "limits": {"job_seconds": JOB_SECONDS, "result_bytes": RESULT_BYTES},
        "jobs": {name: {**run, "median_s": statistics.median(run["wall_s"]) if run["wall_s"] else None,
                        "peak_result_bytes": max(run["result_bytes"], default=None)} for name, run in runs.items()},
        "failed": capacity_failures(runs),
    }
    _write(evidence_dir / "capacity.json", evidence)
    return evidence


SOURCE_ROWS = 10_000
SOURCE_COLUMNS = [{"name": "Region", "type": "text"}] + [{"name": name, "type": "number"} for name in "ABCD"]
SOURCE_ROWS_PER_INSERT = 1_000  # 5,000 fields: an operation admits at most 10,000
QUERY_GROUPS = 100
QUERY_SQL = 'SELECT Region, SUM(A) AS Total FROM Sales GROUP BY Region ORDER BY Region'
PAD_CELLS = 200_000
PAD_COLUMNS = 20
PAD_CELLS_PER_EDIT = 10_000  # an edit admits at most 10,000 cells


def source_rows(first: int, last: int) -> list[dict]:
    """Rows ``first``..``last`` of the source table: a region text in one of 100 groups and four numbers."""
    return [{"Region": f"Region {row % QUERY_GROUPS:03d}",
             **{name: round(math.sin(row / (50 + index)) * 100 + 100, 3) for index, name in enumerate("ABCD")}}
            for row in range(first, last + 1)]


def pad_edits(count: int) -> list[list[dict]]:
    """``count`` number cells in rows of PAD_COLUMNS, split into edits of at most PAD_CELLS_PER_EDIT cells."""
    cells = [{"row": index // PAD_COLUMNS + 1, "col": index % PAD_COLUMNS + 1, "source": str(index % 997)}
             for index in range(count)]
    return [cells[start:start + PAD_CELLS_PER_EDIT] for start in range(0, count, PAD_CELLS_PER_EDIT)]


def _table(column: str) -> dict:
    return {"source": {"kind": "table", "table": "Sales", "column": column}}


def _query(column: str) -> dict:
    return {"source": {"kind": "query", "query": "Totals", "column": column}}


def source_specs() -> dict:
    """Table and saved-query charts to measure: name -> (specification, kinds)."""
    def line(columns):
        return {"spec_version": 1, "type": "line", "categories": _table("Region"),
                "series": [{"name": {"text": name}, "values": _table(name)} for name in columns],
                "legend": {"position": "bottom"}}

    return {"table_line_1_series": (line("A"), ("resolve", "render")),
            # Four series of 10,000 exceed the render limit, so this one is resolved only.
            "table_line_4_series": (line("ABCD"), ("resolve",)),
            "query_column": ({"spec_version": 1, "type": "column", "categories": _query("Region"),
                              "series": [{"name": {"text": "Total"}, "values": _query("Total")}],
                              "legend": {"position": "none"}}, ("resolve", "render"))}


def _time_jobs(book, chart_id, kinds, APIError) -> dict:
    runs = {}
    for kind in kinds:
        run = {"wall_s": [], "result_bytes": [], "error": None}
        for _ in range(SERVICE_RUNS):
            start = time.perf_counter()
            try:
                if kind == "resolve":
                    result = book.resolve_chart(chart_id, timeout=JOB_SECONDS + 15)
                else:
                    result = book.render_chart(chart_id, 800, 500, timeout=JOB_SECONDS + 15)
            except (APIError, TimeoutError) as error:
                run["error"] = error.body.get("code") if isinstance(error, APIError) else "SDK_TIMEOUT"
                break
            run["wall_s"].append(time.perf_counter() - start)
            run["result_bytes"].append(len(json.dumps(result).encode("utf-8")))
            run["counts"] = result["counts"]
        runs[kind] = run
    return runs


def _query_job_snapshot(book) -> dict:
    """Run the saved query once as an ordinary query job and return its own snapshot figures."""
    job_id = book.run_saved_query("Totals")["job_id"]
    deadline = time.monotonic() + JOB_SECONDS + 15
    while (job := book.query(job_id))["status"] == "running":
        if time.monotonic() > deadline:
            raise TimeoutError("query job did not finish")
        time.sleep(0.05)
    book.query(job_id, "release")
    result = job.get("result") or {}
    return {"status": job["status"], "snapshot_bytes": result.get("snapshot_bytes"),
            "snapshot_seconds": result.get("snapshot_seconds"), "execution_seconds": result.get("execution_seconds")}


def measure_sources(evidence_dir: pathlib.Path) -> dict:
    """Time table and saved-query charts through a LocalService, then the query chart on a padded workbook.

    The workbook lives in a temporary directory that is removed when the run ends.
    """
    import tempfile
    from datahorizon.client import APIError, LocalService

    runs, sizes, query_jobs = {}, {}, {}
    with tempfile.TemporaryDirectory(prefix="chart-sources-") as work:
        path = pathlib.Path(work) / "sources.dhwb"
        with LocalService() as client, client.open(path, create=True) as book:
            start = time.perf_counter()
            book.create_table("Sales", SOURCE_COLUMNS)
            for first in range(1, SOURCE_ROWS + 1, SOURCE_ROWS_PER_INSERT):
                book.insert_records("Sales", source_rows(first, min(first + SOURCE_ROWS_PER_INSERT - 1, SOURCE_ROWS)))
            book.save_query(book.preview_query("Totals", QUERY_SQL))
            fill_s = time.perf_counter() - start
            sizes["filled"] = path.stat().st_size
            specs = source_specs()
            chart_ids = {name: book.create_chart(spec, size={"width": 800, "height": 500})["chart_id"]
                         for name, (spec, _) in specs.items()}
            for name, (_, kinds) in specs.items():
                for kind, run in _time_jobs(book, chart_ids[name], kinds, APIError).items():
                    runs[f"{name}.{kind}"] = run
            query_jobs["unpadded"] = _query_job_snapshot(book)
            start = time.perf_counter()
            edits = pad_edits(PAD_CELLS)
            for cells in edits:
                book.apply("edit", "Sheet1", cells=cells)
            pad_s = time.perf_counter() - start
            sizes["padded"] = path.stat().st_size
            for kind, run in _time_jobs(book, chart_ids["query_column"], ("resolve", "render"), APIError).items():
                runs[f"query_column_padded.{kind}"] = run
            # A table chart runs no query, so it shows what padding costs a chart job without a snapshot copy.
            runs["table_line_1_series_padded.resolve"] = _time_jobs(
                book, chart_ids["table_line_1_series"], ("resolve",), APIError)["resolve"]
            query_jobs["padded"] = _query_job_snapshot(book)
    evidence = {
        "measurement": "charts table and saved-query sources through the service",
        "machine": _machine(),
        "table": {"name": "Sales", "rows": SOURCE_ROWS, "columns": SOURCE_COLUMNS,
                  "inserts": len(range(1, SOURCE_ROWS + 1, SOURCE_ROWS_PER_INSERT)), "fill_seconds": fill_s},
        "query": {"name": "Totals", "sql": QUERY_SQL, "rows": QUERY_GROUPS},
        "padding": {"sheet_cells": PAD_CELLS, "edits": len(edits), "seconds": pad_s},
        "workbook_bytes": {"before_padding": sizes["filled"], "after_padding": sizes["padded"]},
        "charts": {name: {"type": spec["type"], "series": len(spec["series"])} for name, (spec, _) in specs.items()},
        "size_px": [800, 500],
        "runs": SERVICE_RUNS,
        "limits": {"job_seconds": JOB_SECONDS, "result_bytes": RESULT_BYTES},
        "jobs": {name: {**run, "median_s": statistics.median(run["wall_s"]) if run["wall_s"] else None,
                        "peak_result_bytes": max(run["result_bytes"], default=None)} for name, run in runs.items()},
        "query_job_snapshot": query_jobs,
        "failed": capacity_failures(runs),
    }
    _write(evidence_dir / "sources.json", evidence)
    return evidence


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe", type=pathlib.Path)
    parser.add_argument("--work", required=True, type=pathlib.Path)
    parser.add_argument("--evidence", required=True, type=pathlib.Path)
    parser.add_argument("--font", help="a second installed font family, standing in for a viewer's substitute")
    parser.add_argument("--pie-sizes", type=pie_sizes, help="measure pies of these sizes, such as 500,1000,2000")
    parser.add_argument("--service", action="store_true", help="measure resolve and render through LocalService")
    parser.add_argument("--sources", action="store_true", help="measure table and saved-query sources through LocalService")
    args = parser.parse_args(argv)
    if args.service:
        print(json.dumps(measure_service(args.work, args.evidence)["jobs"], indent=2))
    if args.sources:
        evidence = measure_sources(args.evidence)
        print(json.dumps({key: evidence[key] for key in ("workbook_bytes", "jobs", "query_job_snapshot", "failed")}, indent=2))
    if (args.service or args.sources) and not args.pie_sizes:
        return 0
    if args.probe is None or not args.probe.is_file():
        print(f"probe not found: {args.probe} (build the chart_svg_probe target)", file=sys.stderr)
        return 2
    if args.pie_sizes:
        print(json.dumps(measure_pies(args.probe, args.work, args.evidence, args.pie_sizes), indent=2))
        return 0

    svg_dir, png_dir = args.work / "svg", args.work / "png"
    for folder in (svg_dir, png_dir):
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)

    render_ms_12, fixture_svgs = {}, []
    for path in sorted(FIXTURES.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        elapsed, svg = _timed(spec, 800, 500)
        render_ms_12[path.stem] = elapsed
        fixture_svgs.append(svg)
        (svg_dir / f"{path.stem}.svg").write_text(svg, encoding="utf-8")
        small = cr.render(spec, 400, 250)["svg"]
        fixture_svgs.append(small)
        (svg_dir / f"{path.stem}_400.svg").write_text(small, encoding="utf-8")

    render_ms_10k, svg_bytes_10k = {}, {}
    for kind in TYPE_FIXTURE:
        elapsed, svg = _timed(_large(kind), 800, 500)
        data = svg.encode("utf-8")
        render_ms_10k[kind], svg_bytes_10k[kind] = elapsed, len(data)
        (svg_dir / f"{kind}_10k.svg").write_bytes(data)

    labels_path, report_path = args.work / "labels.json", args.work / "report.json"
    labels_path.write_text(json.dumps(_labels(fixture_svgs)), encoding="utf-8")
    command = [str(args.probe), "--svg-dir", str(svg_dir), "--out", str(png_dir),
               "--labels", str(labels_path), "--report", str(report_path),
               "--font-file", str(chart_font.font_path())]
    _run(command + (["--font", args.font] if args.font else []))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    charts = {item["file"]: item for item in report["charts"]}

    def qt_ms(name):
        return charts[f"{name}.svg"]["load_ms"] + charts[f"{name}.svg"]["paint_ms"]

    table_stats = table_bytes = None
    if args.font:
        table_path = args.work / "metrics-table.json"
        _run([str(args.probe), "--emit-metrics", "sans-serif", str(table_path)])
        table_bytes = table_path.stat().st_size
        table_stats = _text_stats(report["labels"], json.loads(table_path.read_text(encoding="utf-8"))["advances"])

    measurements = {
        "render_ms_10k": render_ms_10k,
        "svg_bytes_10k": svg_bytes_10k,
        "qt_ms_10k": {kind: qt_ms(f"{kind}_10k") for kind in TYPE_FIXTURE},
        "total_ms_12": {kind: render_ms_12[name] + qt_ms(name) for kind, name in TYPE_FIXTURE.items()},
        "all_valid": all(item["valid"] for item in report["charts"]),
        "text": _text_stats(report["labels"]),
        "text_with_table": table_stats,
    }
    result = evaluate(measurements)
    font_in_svg = measure_font_in_svg(args.probe, args.work)
    if not font_in_svg["ok"]:
        result["failed"].append("font_in_svg")

    copy_screenshots(charts, sorted(path.stem for path in FIXTURES.glob("*.json")),
                     png_dir, args.evidence / "chart-render")
    evidence = {
        "experiment": "charts stage 1 renderer",
        "machine": {"platform": platform.platform(), "machine": platform.machine()},
        "python": platform.python_version(),
        "qt": report["qt"],
        "qt_platform": report["platform"],
        "default_font": report["default_font"],
        "substituted_font": report["substituted_font"],
        "runs_per_median": RUNS,
        "thresholds": {**LIMITS, "text_max_under": TEXT_MAX_UNDER, "text_median_over": TEXT_MEDIAN_OVER},
        "measurements": measurements,
        "metrics_table_bytes": table_bytes,
        "labels_measured": len(report["labels"]),
        "font_in_svg": font_in_svg,
        "charts": report["charts"],
        "result": result,
    }
    for item in evidence["charts"]:
        item.pop("png", None)
    (args.evidence / "chart-render-experiment.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": result, "text": measurements["text"], "text_with_table": table_stats,
                      "font_in_svg": font_in_svg}, indent=2))
    return 0 if font_in_svg["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
