# SPDX-License-Identifier: GPL-3.0-only
import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from datahorizon.client import LocalService
from datahorizon.engine import EngineError
from datahorizon.formula import coordinates
from datahorizon.interchange import xlsx_export, xlsx_preview
from xlsx_builder import workbook_bytes


def b64(data):
    return base64.b64encode(data).decode()


def preview(sheet_xml, mode="formulas", **request):
    data = workbook_bytes({"Sheet1": sheet_xml})
    return xlsx_preview({"file_base64": b64(data), "mode": mode, **request})


def cell(result, location):
    row, col = coordinates(location)
    (found,) = [c for c in result["cells"] if (c["row"], c["col"]) == (row, col)]
    return {"source": found["source"], "kind": found["kind"]}


SHEET = ('<row r="1"><c r="A1"><v>2</v></c>'
         '<c r="B1" t="str"><f>_xlfn.IFS(A1&gt;1,"big")</f><v>big</v></c>'
         '<c r="C1"><f>SUM(A1:A1)</f><v>2</v></c></row>')


class XlsxDegradeTests(unittest.TestCase):
    def test_formulas_mode_keeps_source_and_reports(self):
        result = preview(SHEET, mode="formulas")
        self.assertTrue(result["can_import"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(cell(result, "B1"), {"source": '=IFS(A1>1,"big")', "kind": "formula"})
        self.assertIn({"category": "formula.unsupported_function", "severity": "unevaluable", "detail": "IFS",
                       "count": 1, "samples": [{"sheet": "Sheet1", "cell": "B1"}]}, result["report_entries"])
        self.assertEqual(result["totals"]["unevaluable"], 1)
        self.assertTrue(any("IFS" in line for line in result["report"]))

    def test_mixed_mode_uses_cache_for_unevaluable_only(self):
        result = preview(SHEET, mode="mixed")
        self.assertEqual(cell(result, "B1"), {"source": "big", "kind": "text"})
        self.assertEqual(cell(result, "C1")["kind"], "formula")
        self.assertIn({"category": "formula.unsupported_function", "severity": "converted", "detail": "IFS",
                       "count": 1, "samples": [{"sheet": "Sheet1", "cell": "B1"}]}, result["report_entries"])

    def test_mixed_mode_keeps_formula_when_cache_is_missing_or_an_error(self):
        for cache in ("", '<v>#N/A</v>'):
            with self.subTest(cache=cache):
                kind = ' t="e"' if cache else ""
                result = preview(f'<row r="1"><c r="B1"{kind}><f>IFS(1,2)</f>{cache}</c></row>', mode="mixed")
                self.assertTrue(result["can_import"], result)
                self.assertEqual(cell(result, "B1"), {"source": "=IFS(1,2)", "kind": "formula"})
                self.assertEqual(result["totals"]["unevaluable"], 1)

    def test_values_mode_uses_every_cache(self):
        result = preview(SHEET, mode="values")
        self.assertEqual(cell(result, "B1"), {"source": "big", "kind": "text"})
        self.assertEqual(cell(result, "C1"), {"source": "2", "kind": "number"})

    def test_each_unevaluable_category_is_reported_and_kept(self):
        formulas = {"VLOOKUP(A1,A1:A2,1,0)": "formula.unsupported_function", "Other!A1": "formula.missing_sheet",
                    "Rate*2": "formula.name", "SUM(A1": "formula.syntax", "{1,2}": "formula.array",
                    "[1]Sheet1!A1": "formula.external_reference"}
        for body, category in formulas.items():
            with self.subTest(body):
                result = preview(f'<row r="1"><c r="A1"><f>{body}</f><v>1</v></c></row>')
                self.assertTrue(result["can_import"], result)
                self.assertEqual(cell(result, "A1"), {"source": "=" + body, "kind": "formula"})
                self.assertEqual([e["category"] for e in result["report_entries"] if e["severity"] == "unevaluable"], [category])

    def test_unevaluable_formula_survives_a_destination_offset_when_translatable(self):
        result = preview('<row r="1"><c r="A1"><f>IFS(B1,1)</f></c></row>', row=2, col=2)
        self.assertEqual(cell(result, "B2")["source"], "=IFS(C2,1)")

    def test_untranslatable_formula_cannot_move_to_another_destination(self):
        with self.assertRaises(EngineError):
            preview('<row r="1"><c r="A1"><f>{1,2}</f></c></row>', row=2)
        self.assertTrue(preview('<row r="1"><c r="A1"><f>{1,2}</f></c></row>')["can_import"])

    def test_other_cell_problems_still_block(self):
        result = preview('<row r="1"><c r="A1" t="q"><v>1</v></c></row>')
        self.assertFalse(result["can_import"])
        self.assertTrue(result["issues"])

    def test_malformed_package_still_rejects_the_file(self):
        with self.assertRaises(EngineError):
            xlsx_preview({"file_base64": b64(b"not a zip")})

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(EngineError):
            preview(SHEET, mode="bogus")

    def test_export_restores_future_prefix_and_rejects_mixed(self):
        snapshot = {"state": "s", "range": [1, 1, 1, 2], "cells": [
            {"row": 1, "col": 1, "kind": "formula", "source": '=IFS(B1>1,"big")', "value": None, "calculation": "error", "style": {}},
            {"row": 1, "col": 2, "kind": "formula", "source": "=SORT(C1)+TEXTJOIN(\",\",TRUE,C1)", "value": None, "calculation": "error", "style": {}}]}
        snapshot["range"] = [1, 1, 1, 3]
        data = base64.b64decode(xlsx_export(snapshot, {})["file_base64"])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("_xlfn.IFS(B1&gt;1", sheet)
        self.assertIn("_xlfn._xlws.SORT(", sheet)
        self.assertIn("_xlfn.TEXTJOIN(", sheet)
        self.assertEqual(cell(xlsx_preview({"file_base64": b64(data)}), "A1")["source"], '=IFS(B1>1,"big")')
        with self.assertRaises(EngineError):
            xlsx_export(snapshot, {"mode": "mixed"})

    def test_applied_unevaluable_formula_reads_back_with_name_error_display(self):
        result = preview(SHEET)
        with tempfile.TemporaryDirectory() as temp, LocalService() as client:
            with client.open(Path(temp) / "book.dhwb", create=True) as book:
                book.apply("edit", "Sheet1", cells=result["cells"])
                found = {(c["row"], c["col"]): c for c in book.read("Sheet1", r2=1, c2=3)["cells"]}
        self.assertEqual(found[(1, 2)]["source"], '=IFS(A1>1,"big")')
        self.assertEqual(found[(1, 2)]["display"], "#NAME?")
        self.assertEqual(found[(1, 3)]["display"], "2")


if __name__ == "__main__":
    unittest.main()
