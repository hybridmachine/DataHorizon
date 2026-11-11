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
from datahorizon.ods import convert_ods_cell, ods_export, ods_preview
from ods_builder import workbook_bytes


def b64(data):
    return base64.b64encode(data).decode()


def preview(rows, mode="formulas", styles="", **request):
    data = workbook_bytes({"Sheet1": rows}, styles=styles)
    return ods_preview({"file_base64": b64(data), "mode": mode, **request})


def row(*cells):
    return "<table:table-row>" + "".join(cells) + "</table:table-row>"


def number(value, **extra):
    return f'<table:table-cell office:value-type="float" office:value="{value}"{"".join(f" {k}={v!r}" for k, v in extra.items())}/>'


def formula(source, cache=None, kind="float", extra=""):
    source = source.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    if cache is None:
        return f'<table:table-cell table:formula="{source}"{extra}/>'
    attribute = {"float": "office:value", "string": "office:string-value", "boolean": "office:boolean-value"}[kind]
    return f'<table:table-cell table:formula="{source}" office:value-type="{kind}" {attribute}="{cache}"{extra}/>'


def entry(category, severity, detail="", *cells, count=None):
    return {"category": category, "severity": severity, "detail": detail, "count": count or len(cells),
            "samples": [{"sheet": "Sheet1", "cell": c} for c in cells]}


def cell(result, location):
    r, c = coordinates(location)
    (found,) = [x for x in result["cells"] if (x["row"], x["col"]) == (r, c)]
    return {"source": found["source"], "kind": found["kind"]}


SHEET = row(number(2),
            formula('of:=IFS([.A1]>1;"big")', "big", "string"),
            formula("of:=SUM([.A1:.A1])", "2"))


class OdsFormulaDegradeTests(unittest.TestCase):
    def test_formulas_mode_keeps_source_and_reports(self):
        result = preview(SHEET)
        self.assertTrue(result["can_import"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(cell(result, "B1"), {"source": '=IFS(A1>1,"big")', "kind": "formula"})
        self.assertIn(entry("formula.unsupported_function", "unevaluable", "IFS", "B1"), result["report_entries"])
        self.assertEqual(result["totals"]["unevaluable"], 1)
        self.assertTrue(any("IFS" in line for line in result["report"]))

    def test_mixed_mode_uses_cache_for_unevaluable_only(self):
        result = preview(SHEET, mode="mixed")
        self.assertEqual(cell(result, "B1"), {"source": "big", "kind": "text"})
        self.assertEqual(cell(result, "C1")["kind"], "formula")
        self.assertIn(entry("formula.unsupported_function", "converted", "IFS", "B1"), result["report_entries"])

    def test_mixed_mode_keeps_formula_when_cache_is_missing_or_an_error(self):
        for cache in (formula("of:=IFS(1;2)"), formula("of:=IFS(1;2)", "#N/A", "string")):
            with self.subTest(cache=cache):
                result = preview(row(cache), mode="mixed")
                self.assertTrue(result["can_import"], result)
                self.assertEqual(cell(result, "A1"), {"source": "=IFS(1,2)", "kind": "formula"})
                self.assertEqual(result["totals"]["unevaluable"], 1)

    def test_values_mode_uses_every_cache(self):
        result = preview(SHEET, mode="values")
        self.assertEqual(cell(result, "B1"), {"source": "big", "kind": "text"})
        self.assertEqual(cell(result, "C1"), {"source": "2", "kind": "number"})

    def test_each_unevaluable_category_is_reported_and_kept(self):
        formulas = {"of:=VLOOKUP([.A1];[.A1:.A2];1;0)": "formula.unsupported_function", "of:=[$Other.A1]": "formula.missing_sheet",
                    "of:=Rate*2": "formula.name", "of:=SUM([.A1]": "formula.syntax"}
        for source, category in formulas.items():
            with self.subTest(source):
                result = preview(row(formula(source, "1")))
                self.assertTrue(result["can_import"], result)
                self.assertEqual(result["cells"][0]["kind"], "formula")
                self.assertEqual([e["category"] for e in result["report_entries"] if e["severity"] == "unevaluable"], [category])

    def test_unevaluable_formula_survives_a_destination_offset_when_translatable(self):
        result = preview(row(formula("of:=IFS([.B1];1)")), row=2, col=2)
        self.assertEqual(cell(result, "B2")["source"], "=IFS(C2,1)")

    def test_untranslatable_formula_is_omitted_and_reported_at_another_destination(self):
        rows = row(number(5), formula("of:=STDEV.S([.A1])", "5"), formula("of:=STDEV.S([.A1])"))
        result = preview(rows, row=2)
        self.assertTrue(result["can_import"], result)
        self.assertEqual([(c["row"], c["col"]) for c in result["cells"]], [(2, 1)])
        self.assertIn(entry("formula.untranslatable", "omitted", "", "B1", "C1"), result["report_entries"])
        self.assertEqual(result["totals"]["omitted"], 2)
        self.assertEqual([c["source"] for c in preview(rows)["cells"]], ["5", "=STDEV.S(A1)", "=STDEV.S(A1)"])

    def test_untranslatable_formula_with_cache_becomes_a_value_in_mixed_mode_at_any_destination(self):
        result = preview(row(formula("of:=STDEV.S([.A1])", "1")), mode="mixed", row=2)
        self.assertEqual(cell(result, "A2"), {"source": "1", "kind": "number"})


class OpenFormulaSyntaxTests(unittest.TestCase):
    POWER = "of:=2^3^2"

    def test_formulas_mode_stores_the_original_source_as_text(self):
        for source in (self.POWER, "oooc:=1+1", "of:=SUM(['file:///x.ods'#$S.A1])"):
            with self.subTest(source):
                result = preview(row(formula(source, "512")))
                self.assertEqual(cell(result, "A1"), {"source": source, "kind": "text"})
                self.assertIn(entry("formula.unsupported_syntax", "converted", "", "A1"), result["report_entries"])
                self.assertEqual(result["issues"], [])

    def test_mixed_and_values_modes_prefer_the_cache(self):
        for mode in ("mixed", "values"):
            with self.subTest(mode):
                result = preview(row(formula(self.POWER, "512")), mode=mode)
                self.assertEqual(cell(result, "A1"), {"source": "512", "kind": "number"})
                self.assertEqual(result["totals"]["converted"], 1 if mode == "mixed" else 0)

    def test_without_a_cache_mixed_falls_back_to_text_and_values_skips(self):
        mixed = preview(row(formula(self.POWER)), mode="mixed")
        self.assertEqual(cell(mixed, "A1"), {"source": self.POWER, "kind": "text"})
        values = preview(row(formula(self.POWER), number(1)), mode="values")
        self.assertEqual([c["source"] for c in values["cells"]], ["1"])
        self.assertIn(entry("cell.missing_cache", "omitted", "", "A1"), values["report_entries"])

    def test_matrix_formula_degrades_like_an_xlsx_array_formula(self):
        matrix = ' table:number-matrix-columns-spanned="1" table:number-matrix-rows-spanned="1"'
        kept = preview(row(formula("of:=SUM([.A1:.A2])", "3", extra=matrix)))
        self.assertEqual(cell(kept, "A1")["kind"], "formula")
        self.assertIn(entry("formula.array", "unevaluable", "", "A1"), kept["report_entries"])
        mixed = preview(row(formula("of:=SUM([.A1:.A2])", "3", extra=matrix)), mode="mixed")
        self.assertEqual(cell(mixed, "A1"), {"source": "3", "kind": "number"})
        self.assertIn(entry("formula.array", "converted", "", "A1"), mixed["report_entries"])
        skipped = preview(row(formula(self.POWER, extra=matrix), number(1)))
        self.assertEqual([c["source"] for c in skipped["cells"]], ["1"])
        self.assertIn(entry("formula.array", "omitted", "", "A1"), skipped["report_entries"])


class OdsCellDegradeTests(unittest.TestCase):
    ERROR = '<table:table-cell office:value-type="string" calcext:value-type="error" office:string-value="#DIV/0!"/>'

    def test_error_cell_becomes_text_in_every_mode(self):
        for mode in ("formulas", "mixed", "values"):
            with self.subTest(mode):
                result = preview(row(self.ERROR), mode=mode)
                self.assertEqual(cell(result, "A1"), {"source": "#DIV/0!", "kind": "text"})
                self.assertIn(entry("cell.error_value", "converted", "", "A1"), result["report_entries"])
                self.assertEqual(result["issues"], [])

    def test_values_mode_cached_error_becomes_text(self):
        result = preview(row(formula("of:=1/0", "#DIV/0!", "string")), mode="values")
        self.assertEqual(cell(result, "A1"), {"source": "#DIV/0!", "kind": "text"})
        self.assertIn(entry("cell.error_value", "converted", "", "A1"), result["report_entries"])

    def test_values_mode_formula_without_cache_is_skipped(self):
        result = preview(row(formula("of:=1+1"), number(3)), mode="values")
        self.assertEqual([c["source"] for c in result["cells"]], ["3"])
        self.assertIn(entry("cell.missing_cache", "omitted", "", "A1"), result["report_entries"])
        self.assertTrue(result["can_import"])

    def test_unknown_value_type_is_skipped_and_reported(self):
        result = preview(row('<table:table-cell office:value-type="unknown"/>', number(2)))
        self.assertEqual(result["issues"], [])
        self.assertEqual([c["source"] for c in result["cells"]], ["2"])
        self.assertIn(entry("cell.unsupported_type", "omitted", "unknown", "A1"), result["report_entries"])

    def test_text_longer_than_the_engine_source_limit_is_skipped(self):
        def text(n): return f'<table:table-cell office:value-type="string" office:string-value="{"x" * n}"/>'
        result = preview(row(text(65_537), text(65_536)))
        self.assertEqual([(c["row"], c["col"]) for c in result["cells"]], [(1, 2)])
        self.assertIn(entry("cell.text_too_long", "omitted", "", "A1"), result["report_entries"])

    def test_paragraph_text_beyond_the_cell_limit_is_skipped(self):
        spaces = '<text:s text:c="65536"/>' * 2
        result = preview(row(f'<table:table-cell office:value-type="string"><text:p>{spaces}</text:p></table:table-cell>'))
        self.assertFalse(result["can_import"])
        self.assertIn(entry("cell.text_too_long", "omitted", "", "A1"), result["report_entries"])

    def test_malformed_values_are_skipped_with_fixed_details(self):
        cells = ['<table:table-cell office:value-type="boolean" office:boolean-value="maybe"/>',
                 '<table:table-cell office:value-type="date"/>',
                 '<table:table-cell office:value-type="time" office:time-value="PT24H"/>',
                 '<table:table-cell office:value-type="float"/>']
        result = preview(row(*cells))
        self.assertEqual(result["cells"], [])
        self.assertEqual({(e["detail"], tuple(s["cell"] for s in e["samples"])) for e in result["report_entries"] if e["category"] == "cell.invalid_value"},
                         {("boolean", ("A1",)), ("missing value", ("B1", "D1")), ("date/time", ("C1",))})

    def test_number_the_engine_rejects_becomes_text_with_original_characters(self):
        result = preview(row(number("NaN"), number("1e999")))
        self.assertEqual([cell(result, a) for a in ("A1", "B1")], [{"source": "NaN", "kind": "text"}, {"source": "1e999", "kind": "text"}])
        self.assertIn(entry("cell.number_out_of_range", "converted", "", "A1", "B1"), result["report_entries"])

    def test_skips_in_repeated_cells_are_counted_per_stored_cell(self):
        rows = ('<table:table-row table:number-rows-repeated="3"><table:table-cell table:number-columns-repeated="2" office:value-type="weird"/>'
                '<table:table-cell office:value-type="float" office:value="1"/></table:table-row>')
        result = preview(rows)
        self.assertEqual(len(result["cells"]), 3)
        (found,) = [e for e in result["report_entries"] if e["category"] == "cell.unsupported_type"]
        self.assertEqual((found["count"], found["samples"]), (6, [{"sheet": "Sheet1", "cell": "A1"}]))
        self.assertEqual(result["totals"]["omitted"], 6)

    def test_style_losses_are_reported_once_per_cell_using_the_style(self):
        styles = ('<style:style style:name="J" style:family="table-cell"><style:paragraph-properties fo:text-align="justify"/></style:style>')
        styled = '<table:table-cell table:style-name="J" office:value-type="float" office:value="1"/>'
        result = preview(row(styled, styled, number(2)), styles=styles)
        (found,) = [e for e in result["report_entries"] if e["category"] == "style.alignment"]
        self.assertEqual((found["severity"], found["count"], [s["cell"] for s in found["samples"]]), ("omitted", 2, ["A1", "B1"]))

    def test_convert_ods_cell_is_usable_on_its_own(self):
        from xml.etree.ElementTree import fromstring
        from datahorizon.conversion_report import ConversionReport
        from datahorizon.ods import O, T, OdsCellContext, OdsStyles
        report = ConversionReport()
        context = OdsCellContext(mode="formulas", styles=OdsStyles([], report), sheet_names=None, sheet="S", report=report, row=4, col=2)
        element = fromstring(f'<table:table-cell xmlns:table="{T}" xmlns:office="{O}" office:value-type="float" office:value="7"/>')
        self.assertEqual(convert_ods_cell(element, context=context)["row"], 4)
        element.set(f"{{{O}}}value-type", "bogus")
        self.assertIsNone(convert_ods_cell(element, context=context))
        self.assertEqual(report.entries()[0]["samples"], [{"sheet": "S", "cell": "B4"}])


class OdsPackageTests(unittest.TestCase):
    def test_malformed_package_still_rejects_the_file(self):
        with self.assertRaises(EngineError):
            ods_preview({"file_base64": b64(b"not a zip")})

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(EngineError):
            preview(SHEET, mode="bogus")

    def test_whole_workbook_import_admits_references_between_sheets(self):
        data = workbook_bytes({"One": row(number(1)), "Two": row(formula("of:=SUM([$One.A1:.A2])", "1"))})
        result = ods_preview({"file_base64": b64(data), "scope": "workbook"})
        self.assertTrue(result["can_import"], result)
        self.assertEqual(result["workbook_sheets"][1]["cells"][0]["source"], "=SUM(One!A1:A2)")

    def test_export_refuses_mixed(self):
        snapshot = {"state": "s", "range": [1, 1, 1, 1], "cells": [{"row": 1, "col": 1, "kind": "number", "source": "1", "value": 1, "calculation": "current", "style": {}}]}
        with self.assertRaises(EngineError):
            ods_export(snapshot, {"mode": "mixed"})
        self.assertIn("file_base64", ods_export(snapshot, {"mode": "values"}))

    def test_applied_degraded_cells_read_back(self):
        result = preview(SHEET + row(formula("oooc:=1+1", "2")))
        with tempfile.TemporaryDirectory() as temp, LocalService() as client:
            with client.open(Path(temp) / "book.dhwb", create=True) as book:
                book.apply("edit", "Sheet1", cells=result["cells"])
                found = {(c["row"], c["col"]): c for c in book.read("Sheet1", r2=2, c2=3)["cells"]}
        self.assertEqual(found[(1, 2)]["display"], "#NAME?")
        self.assertEqual(found[(1, 3)]["display"], "2")
        self.assertEqual(found[(2, 1)]["source"], "oooc:=1+1")


if __name__ == "__main__":
    unittest.main()
