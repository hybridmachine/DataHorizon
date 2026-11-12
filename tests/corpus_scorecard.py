# SPDX-License-Identifier: GPL-3.0-only
"""Imports every interchange-corpus file through the staged import path in each mode and records how it imports.

Usage: PYTHONPATH=service python3 tests/corpus_scorecard.py [--out PATH] [--check]
The output holds no timestamps, absolute paths or host details, so a fresh run is byte-identical to a checked-in
one unless behaviour changed. --check compares instead of writing and exits non-zero when the file differs."""
import argparse
import json
import sys
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
from datahorizon.client import APIError, LocalService  # noqa: E402

PROVENANCE = ROOT / "tests" / "fixtures" / "corpus" / "provenance.json"
DEFAULT_OUT = ROOT / "docs" / "design" / "evidence" / "excel-fidelity-scorecard.json"
MODES = ("formulas", "mixed", "values")
REQUIRED_PRODUCERS = (("Microsoft Excel", "no Excel-saved files"), ("LibreOffice Calc", "no LibreOffice-saved files"))


def load_provenance():
    return json.loads(PROVENANCE.read_text(encoding="utf-8"))


def reduce_entries(entries):
    kept = [{"category": e["category"], "severity": e["severity"], "detail": e["detail"], "count": e["count"]} for e in entries]
    return sorted(kept, key=lambda e: (e["category"], e["severity"], e["detail"], e["count"]))


def score_file(client, path, mode):
    job = uuid.uuid4().hex
    try:
        try:
            summary = client.prepare_spreadsheet_file(path, mode=mode, local=True, job_id=job)
        except APIError as error:
            body = error.body if isinstance(error.body, dict) else {}
            return {"status": "rejected", "error": {"code": body.get("code"), "message": body.get("message", str(error))}}
        entries = reduce_entries(summary.get("report_entries", []))
        return {"status": "degraded" if entries else "clean", "stored_cells": summary.get("stored_cells"),
                "totals": summary.get("totals"), "entries": entries}
    finally:
        client.discard_job(job)


STATUS_MEANING = ("clean means the importer produced no report entry for the file in that mode; it does not mean the file's "
                  "features were preserved, only that none of them was reported as changed or left out. degraded means at least "
                  "one report entry; rejected means the import failed. known_unreported lists, per mode and in every status, each "
                  "listed unsupported feature of a file whose required report entry is missing: features dropped without a report.")

# The report entry that shows each unsupported_features id was reported: any one of the (category, detail) pairs,
# where a detail of None accepts any detail and a callable detail tests it. FEATURE_REPORTS_VALUES_MODE overrides an
# id in values mode, where every formula is replaced by its cached value by request; None means no entry is needed.
FEATURE_REPORTS = {
    "merged_cells": (("sheet.feature", "merged_cells"),),
    "column_widths": (("sheet.feature", "column_widths"),),
    "row_heights": (("sheet.feature", "row_heights"),),
    "hidden_rows_columns": (("sheet.feature", "hidden_rows_columns"),),
    "frozen_panes": (("sheet.feature", "frozen_panes"),),
    "conditional_formatting": (("sheet.feature", "conditional_formatting"),),
    "data_validation": (("sheet.feature", "data_validation"),),
    "hyperlinks": (("sheet.feature", "hyperlinks"),),
    "comments": (("sheet.feature", "comments"),),
    "excel_tables": (("sheet.feature", "excel_tables"),),
    "defined_names": (("workbook.feature", "defined_names"),),
    "external_references": (("workbook.feature", "external_references"),),
    "chart_sheets": (("workbook.feature", "chart_sheets"),),
    "custom_number_formats": (("style.number_format", None),),
    "named_styles": (("style.named_style", None),),
    "theme_colours": (("style.font", lambda detail: "theme" in detail), ("style.fill", lambda detail: "theme" in detail)),
    "error_values": (("cell.error_value", None),),
    "array_formulas": (("formula.array", None),),
    "data_tables": (("formula.data_table", None),),
    "3d_references": (("formula.unsupported_reference", None),),
}
FEATURE_REPORTS_VALUES_MODE = {"array_formulas": None, "data_tables": None, "3d_references": None}


def required_reports(feature, mode):
    if mode == "values" and feature in FEATURE_REPORTS_VALUES_MODE: return FEATURE_REPORTS_VALUES_MODE[feature]
    return FEATURE_REPORTS[feature]


def reported(feature, mode, entries):
    required = required_reports(feature, mode)
    if required is None: return True
    for category, detail in required:
        for entry in entries:
            if entry["category"] != category: continue
            if detail is None or (callable(detail) and detail(entry["detail"])) or entry["detail"] == detail: return True
    return False


def known_unreported(files, provenance):
    """Per mode, each (file, feature) whose provenance lists a feature DataHorizon does not import and whose required
    report entry (FEATURE_REPORTS) is absent, whatever the file's status: the measure of silent loss. A rejected file
    reports nothing, so all of its listed features count."""
    listed = {entry["file"]: sorted(entry.get("unsupported_features", [])) for entry in provenance}
    result = {}
    for mode in MODES:
        missing = []
        for name in sorted(files):
            entries = files[name][mode].get("entries", [])
            features = [feature for feature in listed[name] if not reported(feature, mode, entries)]
            if features: missing.append({"file": name, "features": features})
        result[mode] = missing
    return result


def summarize(files, provenance):
    by_status = {mode: dict(sorted(Counter(r[mode]["status"] for r in files.values()).items())) for mode in MODES}
    categories = {mode: dict(sorted(Counter(e["category"] for r in files.values() for e in r[mode].get("entries", [])).items())) for mode in MODES}
    producers = sorted({entry["producer"] for entry in provenance})
    gaps = [gap for producer, gap in REQUIRED_PRODUCERS if producer not in producers]
    return {"files": len(files), "status_by_mode": by_status, "entries_by_category": categories, "producers": producers, "gaps": gaps,
            "status_meaning": STATUS_MEANING, "known_unreported": known_unreported(files, provenance)}


def build():
    provenance = load_provenance()
    files = {}
    with LocalService() as client:
        for entry in sorted(provenance, key=lambda e: e["file"]):
            path = ROOT / entry["file"]
            files[entry["file"]] = {mode: score_file(client, path, mode) for mode in MODES}
    return {"files": files, "summary": summarize(files, provenance)}


def render(scorecard):
    return json.dumps(scorecard, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="scorecard path (default: %(default)s)")
    parser.add_argument("--check", action="store_true", help="exit non-zero if the file on disk differs from a fresh run")
    arguments = parser.parse_args(argv)
    text, target = render(build()), Path(arguments.out)
    if arguments.check:
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            print(f"{target} differs from a fresh run; regenerate it with corpus_scorecard.py", file=sys.stderr)
            return 1
        return 0
    target.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
