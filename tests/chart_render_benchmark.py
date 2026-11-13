# SPDX-License-Identifier: GPL-3.0-only
"""Charts stage 1 experiment: run the renderer and the Qt SVG probe, apply the
thresholds from docs/design/charts-stage-1-plan.md and write the evidence.

    python3 tests/chart_render_benchmark.py --probe build/chart_svg_probe \\
        --work build/chart-render --evidence docs/design/evidence [--font Arial]
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

import chart_render_experiment as cr

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "charts"
RUNS = 20
TYPE_FIXTURE = {"column": "column_clustered", "bar": "bar_clustered", "line": "line",
                "area": "area_stacked", "pie": "pie", "scatter": "scatter"}
STRESS = ["WWWWWWWWWW", "iiiiiiiiii", "1,234,567.89", "Quarterly revenue (EUR)",
          "Ærøskøbing Größe façade"]
LIMITS = {"render_ms_10k": 250.0, "svg_bytes_10k": 4 * 1024 * 1024, "qt_ms_10k": 150.0, "total_ms_12": 50.0}
TEXT_MAX_UNDER = 1.05
TEXT_MEDIAN_OVER = 1.35


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


def _large(kind: str) -> dict:
    count = cr.LIMIT_POINTS
    values = [math.sin(i / 50) * 100 + 100 for i in range(count)]
    return {"type": kind, "grouping": "clustered" if kind in ("column", "bar", "area") else None,
            "title": "10,000 points", "categories": None if kind == "scatter" else [str(i) for i in range(count)],
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
    return [{"text": text, "size_px": size, "estimate": cr.estimate_text_width(text, size)} for text, size in seen]


def _text_stats(rows, table=None) -> dict:
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe", required=True, type=pathlib.Path)
    parser.add_argument("--work", required=True, type=pathlib.Path)
    parser.add_argument("--evidence", required=True, type=pathlib.Path)
    parser.add_argument("--font", help="a second installed font family, standing in for a viewer's substitute")
    args = parser.parse_args(argv)
    if not args.probe.is_file():
        print(f"probe not found: {args.probe} (build the chart_svg_probe target)", file=sys.stderr)
        return 2

    svg_dir, png_dir = args.work / "svg", args.work / "png"
    for folder in (svg_dir, png_dir):
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)

    render_ms_12, fixture_svgs = {}, []
    for path in sorted(FIXTURES.glob("*.json")):
        spec = json.loads(path.read_text())
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
               "--labels", str(labels_path), "--report", str(report_path)]
    _run(command + (["--font", args.font] if args.font else []))
    report = json.loads(report_path.read_text())
    charts = {item["file"]: item for item in report["charts"]}

    def qt_ms(name):
        return charts[f"{name}.svg"]["load_ms"] + charts[f"{name}.svg"]["paint_ms"]

    table_stats = table_bytes = None
    if args.font:
        table_path = args.work / "metrics-table.json"
        _run([str(args.probe), "--emit-metrics", "sans-serif", str(table_path)])
        table_bytes = table_path.stat().st_size
        table_stats = _text_stats(report["labels"], json.loads(table_path.read_text())["advances"])

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
        "charts": report["charts"],
        "result": result,
    }
    for item in evidence["charts"]:
        item.pop("png", None)
    (args.evidence / "chart-render-experiment.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": result, "text": measurements["text"], "text_with_table": table_stats}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
