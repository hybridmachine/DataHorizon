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
from datahorizon.interchange import S, xlsx_export, xlsx_preview
from xlsx_builder import workbook_bytes


def b64(data):
    return base64.b64encode(data).decode()


def preview(sheet_xml, mode="formulas", **request):
    data = workbook_bytes({"Sheet1": sheet_xml})
    return xlsx_preview({"file_base64": b64(data), "mode": mode, **request})


def entry(category, severity, detail="", *cells, count=None):
    return {"category": category, "severity": severity, "detail": detail, "count": count or len(cells),
            "samples": [{"sheet": "Sheet1", "cell": c} for c in cells]}


def reasons(result):
    return {e["category"]: e for e in result["report_entries"]}


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

    def test_an_empty_cache_element_is_not_a_cached_value(self):
        # A writer that never calculated (openpyxl) leaves <v/> or <v></v> on a formula cell.
        for cache in ("<v/>", "<v></v>", "<v> </v>"):
            with self.subTest(cache=cache):
                sheet = f'<row r="1"><c r="B1"><f>IFS(1,2)</f>{cache}</c><c r="C1"><f>1+1</f>{cache}</c></row>'
                result = preview(sheet, mode="mixed")
                self.assertEqual(cell(result, "B1"), {"source": "=IFS(1,2)", "kind": "formula"})
                self.assertEqual(cell(result, "C1")["source"], "=1+1")
                self.assertEqual((result["totals"]["unevaluable"], result["totals"]["converted"]), (1, 0))
                result = preview(sheet, mode="values")
                self.assertEqual(result["cells"], [])
                self.assertIn(entry("cell.missing_cache", "omitted", "", "B1", "C1"), result["report_entries"])
        # An empty string result is a real cache.
        result = preview('<row r="1"><c r="B1" t="str"><f>IFS(1,"")</f><v></v></c></row>', mode="mixed")
        self.assertEqual(cell(result, "B1"), {"source": "", "kind": "text"})

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

    def test_shared_member_translates_through_an_unevaluable_master(self):
        sheet=('<row r="1"><c r="C1"><f t="shared" si="0" ref="C1:C2">_xlfn.IFS(A1&gt;1,B:B)</f></c></row>'
               '<row r="2"><c r="C2"><f t="shared" si="0"/></c></row>')
        result=preview(sheet)
        self.assertEqual(cell(result,"C2"),{"source":"=IFS(A2>1,B:B)","kind":"formula"})
        self.assertNotIn("formula.untranslatable",reasons(result))

    def test_shared_member_shifts_relative_external_references(self):
        sheet=('<row r="1"><c r="C1"><f t="shared" si="0" ref="C1:C2">[1]Data!A1*2+[1]Data!$A$1</f><v>1</v></c></row>'
               '<row r="2"><c r="C2"><f t="shared" si="0"/><v>1</v></c></row>')
        result=preview(sheet)
        self.assertEqual(cell(result,"C2"),{"source":"=[1]Data!A2*2+[1]Data!$A$1","kind":"formula"})

    def test_untranslatable_formula_is_omitted_and_reported_at_another_destination(self):
        sheet=('<row r="1"><c r="A1"><v>5</v></c><c r="B1"><f>{1,2}</f></c><c r="C1"><f>STDEV.S(A1:A1)</f></c>'
               '<c r="D1"><f>VLOOKUP(A1,B:C,2,0)</f></c><c r="E1"><f>A1 ` 2</f></c></row>')
        result=preview(sheet,row=2)
        self.assertTrue(result["can_import"],result)
        self.assertEqual([(c["row"],c["col"]) for c in result["cells"]],[(2,1),(2,2),(2,3),(2,4)])
        # Formulas the calculator cannot parse still translate when the scanner can read their references.
        self.assertEqual([c["source"] for c in result["cells"]],["5","={1,2}","=STDEV.S(A2:A2)","=VLOOKUP(A2,B:C,2,0)"])
        entry={"category":"formula.untranslatable","severity":"omitted","detail":"","count":1,
               "samples":[{"sheet":"Sheet1","cell":"E1"}]}
        self.assertIn(entry,result["report_entries"])
        self.assertEqual(result["totals"]["omitted"],1)
        self.assertTrue(any("formula.untranslatable" in line and "Sheet1!E1" in line for line in result["report"]))
        kept=preview(sheet)
        self.assertEqual([c["source"] for c in kept["cells"]],["5","={1,2}","=STDEV.S(A1:A1)","=VLOOKUP(A1,B:C,2,0)","=A1 ` 2"])

    def test_untranslatable_formula_with_cache_becomes_a_value_in_mixed_mode_at_any_destination(self):
        result=preview('<row r="1"><c r="B1"><f>{1,2}</f><v>1</v></c></row>',mode="mixed",row=2)
        self.assertEqual(cell(result,"B2"),{"source":"1","kind":"number"})

    def test_selection_export_keeps_bounds_and_sheet_checks_for_unparsable_formulas(self):
        for source in ("=STDEV.S(A1:Z99)","=STDEV.S(Other!A1)"):
            snapshot={"state":"s","range":[1,1,2,2],"cells":[{"row":1,"col":1,"kind":"formula","source":source,"value":None,"calculation":"error","style":{}}]}
            with self.subTest(source), self.assertRaises(EngineError):
                xlsx_export(snapshot,{})
        whole={"state":"s","sheets":[{"name":"One","cells":[{"row":1,"col":1,"kind":"formula","source":"=STDEV.S(A1:Z99)","value":None,"calculation":"error","style":{}}]}]}
        data=base64.b64decode(xlsx_export(whole,{})["file_base64"])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIn("_xlfn.STDEV.S(A1:Z99)",archive.read("xl/worksheets/sheet1.xml").decode())

    def test_unknown_cell_type_is_skipped_and_reported(self):
        result = preview('<row r="1"><c r="A1" t="q"><v>1</v></c><c r="B1"><v>2</v></c></row>')
        self.assertEqual(result["issues"], [])
        self.assertEqual([c["source"] for c in result["cells"]], ["2"])
        self.assertIn(entry("cell.unsupported_type", "omitted", "q", "A1"), result["report_entries"])

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


class CellDegradeTests(unittest.TestCase):
    def test_error_cell_becomes_text_in_every_mode(self):
        for mode in ("formulas", "mixed", "values"):
            with self.subTest(mode):
                result = preview('<row r="1"><c r="A1" t="e"><v>#DIV/0!</v></c></row>', mode=mode)
                self.assertEqual(cell(result, "A1"), {"source": "#DIV/0!", "kind": "text"})
                self.assertIn(entry("cell.error_value", "converted", "", "A1"), result["report_entries"])
                self.assertEqual(result["issues"], [])

    def test_values_mode_cached_error_becomes_text(self):
        result = preview('<row r="1"><c r="A1" t="e"><f>1/0</f><v>#DIV/0!</v></c></row>', mode="values")
        self.assertEqual(cell(result, "A1"), {"source": "#DIV/0!", "kind": "text"})
        self.assertIn(entry("cell.error_value", "converted", "", "A1"), result["report_entries"])

    def test_values_mode_formula_without_cache_is_skipped(self):
        result = preview('<row r="1"><c r="A1"><f>1+1</f></c><c r="B1"><v>3</v></c></row>', mode="values")
        self.assertEqual([c["source"] for c in result["cells"]], ["3"])
        self.assertIn(entry("cell.missing_cache", "omitted", "", "A1"), result["report_entries"])
        self.assertTrue(result["can_import"])

    def test_text_longer_than_the_engine_source_limit_is_skipped(self):
        big = "x" * 65_537
        result = preview(f'<row r="1"><c r="A1" t="inlineStr"><is><t>{big}</t></is></c><c r="B1" t="inlineStr"><is><t>{"x"*65_536}</t></is></c></row>')
        self.assertEqual([(c["row"], c["col"]) for c in result["cells"]], [(1, 2)])
        self.assertIn(entry("cell.text_too_long", "omitted", "", "A1"), result["report_entries"])

    def test_same_oversize_shared_string_used_many_times_is_one_entry(self):
        rows = "".join(f'<row r="{r}"><c r="A{r}" t="s"><v>0</v></c></row>' for r in range(1, 301))
        data = workbook_bytes({"Sheet1": rows}, shared_strings=["y" * 70_000])
        result = xlsx_preview({"file_base64": b64(data)})
        self.assertEqual(result["cells"], [])
        self.assertFalse(result["can_import"])
        (found,) = [e for e in result["report_entries"] if e["category"] == "cell.text_too_long"]
        self.assertEqual((found["count"], found["severity"], len(found["samples"])), (300, "omitted", 100))

    def test_number_the_engine_rejects_becomes_text_with_original_characters(self):
        result = preview('<row r="1"><c r="A1"><v>NaN</v></c><c r="B1"><v>1e999</v></c><c r="C1"><v>12345678901234567890</v></c></row>')
        self.assertEqual([cell(result, a) for a in ("A1", "B1", "C1")],
                         [{"source": "NaN", "kind": "text"}, {"source": "1e999", "kind": "text"},
                          {"source": "12345678901234567890", "kind": "text"}])
        self.assertIn(entry("cell.number_out_of_range", "converted", "", "A1", "B1", "C1"), result["report_entries"])

    def test_serial_60_with_a_date_format_keeps_the_number(self):
        styles = f'<styleSheet xmlns="{S}"><fonts><font/></fonts><fills><fill/></fills><borders><border/></borders><cellXfs><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts></styleSheet>'
        data = workbook_bytes({"Sheet1": '<row r="1"><c r="A1" s="1"><v>60</v></c><c r="B1" s="1"><v>61</v></c></row>'}, styles_xml=styles)
        result = xlsx_preview({"file_base64": b64(data)})
        a1, b1 = [c for c in result["cells"]]
        self.assertEqual((a1["source"], a1["kind"], a1["style"]), ("60", "number", {}))
        self.assertEqual((b1["source"], b1["kind"]), ("1900-03-01", "date"))
        self.assertIn(entry("cell.date_conversion", "approximated", "date format dropped; the number is kept", "A1"), result["report_entries"])

    def test_non_size_engine_rejection_is_not_reported_as_too_long(self):
        from unittest import mock
        with mock.patch("datahorizon.interchange.typed", side_effect=EngineError("INVALID_INPUT", "nope")):
            result = preview('<row r="1"><c r="A1" t="inlineStr"><is><t>hi</t></is></c></row>')
        self.assertEqual(result["cells"], [])
        self.assertEqual([(e["category"], e["detail"]) for e in result["report_entries"]], [("cell.invalid_value", "rejected by the engine")])

    def test_formulas_mode_takes_the_cache_when_the_formula_is_empty_or_untranslatable(self):
        sheet = ('<row r="1"><c r="A1"><f t="shared" si="0" ref="A1:A2">1 ` 2</f><v>1</v></c>'
                 '<c r="B1"><f t="array" ref="B1"/><v>7</v></c></row>'
                 '<row r="2"><c r="A2"><f t="shared" si="0"/><v>2</v></c></row>')
        result = preview(sheet, mode="formulas")
        self.assertEqual(len(result["cells"]), 3)
        self.assertEqual(cell(result, "A2"), {"source": "2", "kind": "number"})
        self.assertEqual(cell(result, "B1"), {"source": "7", "kind": "number"})
        self.assertIn(entry("formula.untranslatable", "converted", "", "A1", "A2"), result["report_entries"])
        self.assertIn(entry("formula.array", "converted", "", "B1"), result["report_entries"])

    def test_invalid_temporal_and_boolean_values_are_skipped(self):
        result = preview('<row r="1"><c r="A1" t="d"><v>2023-02-29</v></c><c r="B1" t="b"><v>2</v></c><c r="C1" t="str"/><c r="D1"><v>1</v></c></row>')
        self.assertEqual([c["source"] for c in result["cells"]], ["1"])
        self.assertEqual(sum(e["count"] for e in result["report_entries"] if e["category"] == "cell.invalid_value"), 3)
        self.assertEqual(result["issues"], [])

    def test_malformed_cells_reject_the_file(self):
        for body in ('<c r="A1"><v>1</v></c><c r="A1"><v>2</v></c>', '<c r="A1" t="s"><v>5</v></c>',
                     '<c r="A1" t="s"><v>x</v></c>', '<c r="1A"><v>1</v></c>', '<c><v>1</v></c>'):
            with self.subTest(body), self.assertRaises(EngineError) as failure:
                preview(f'<row r="1">{body}</row>')
            self.assertEqual(failure.exception.body["code"], "INVALID_SPREADSHEET")

    def test_array_and_data_table_formulas_degrade_by_mode(self):
        sheet = ('<row r="1"><c r="A1"><f t="array" ref="A1:A2">SUM(B1:B2)*2</f><v>6</v></c>'
                 '<c r="B1"><f t="dataTable" ref="B1:B2" dt2D="1" r1="C1" r2="C2"/><v>9</v></c>'
                 '<c r="C1"><f t="dataTable" ref="C1" r1="C2"/></c></row>')
        formulas = preview(sheet)
        self.assertEqual(formulas["issues"], [])
        self.assertEqual(cell(formulas, "A1"), {"source": "=SUM(B1:B2)*2", "kind": "formula"})
        self.assertEqual(cell(formulas, "B1"), {"source": "9", "kind": "number"})
        self.assertEqual([c["source"] for c in formulas["cells"]], ["=SUM(B1:B2)*2", "9"])
        self.assertIn(entry("formula.array", "unevaluable", "", "A1"), formulas["report_entries"])
        self.assertIn(entry("formula.data_table", "converted", "", "B1"), formulas["report_entries"])
        self.assertIn(entry("formula.data_table", "omitted", "", "C1"), formulas["report_entries"])
        mixed = preview(sheet, mode="mixed")
        self.assertEqual(cell(mixed, "A1"), {"source": "6", "kind": "number"})
        self.assertIn(entry("formula.array", "converted", "", "A1"), mixed["report_entries"])
        values = preview(sheet, mode="values")
        self.assertEqual(cell(values, "A1"), {"source": "6", "kind": "number"})

    def test_mixed_array_without_usable_cache_keeps_the_formula(self):
        result = preview('<row r="1"><c r="A1" t="e"><f t="array" ref="A1">B1:B2</f><v>#N/A</v></c><c r="B1"><f t="array" ref="B1">C1</f></c></row>', mode="mixed")
        self.assertEqual([c["source"] for c in result["cells"]], ["=B1:B2", "=C1"])
        self.assertIn(entry("formula.array", "unevaluable", "", "A1", "B1"), result["report_entries"])

    def test_shared_member_of_an_unparsable_master_degrades(self):
        sheet = ('<row r="1"><c r="A1"><f t="shared" si="0" ref="A1:A2">{1,2}</f><v>1</v></c></row>'
                 '<row r="2"><c r="A2"><f t="shared" si="0"/><v>2</v></c></row>')
        formulas = preview(sheet)
        self.assertEqual(formulas["issues"], [])
        self.assertEqual(formulas["can_import"], True)
        mixed = preview(sheet, mode="mixed")
        self.assertEqual([c["source"] for c in mixed["cells"]], ["1", "2"])

    def test_style_losses_are_categorised_per_cell(self):
        styles = (f'<styleSheet xmlns="{S}"><fonts><font/></fonts><fills><fill><patternFill patternType="solid"><fgColor theme="5"/></patternFill></fill></fills>'
                  '<borders><border><bottom style="double"/></border></borders><cellXfs><xf numFmtId="0"/>'
                  '<xf numFmtId="37" fontId="0" fillId="0" borderId="0" xfId="1"><alignment horizontal="justify" textRotation="90"/></xf></cellXfs></styleSheet>')
        data = workbook_bytes({"Sheet1": '<row r="1"><c r="A1" s="1"><v>1</v></c><c r="B1" s="1"><v>2</v></c></row>'}, styles_xml=styles)
        result = xlsx_preview({"file_base64": b64(data)})
        self.assertEqual(result["issues"], [])
        by_category = {}
        for e in result["report_entries"]:
            if e["category"].startswith("style."): by_category.setdefault(e["category"], []).append(e)
        self.assertEqual(set(by_category), {"style.number_format", "style.named_style", "style.fill", "style.border", "style.alignment"})
        for entries in by_category.values():
            for e in entries:
                self.assertIn(e["severity"], ("omitted", "approximated"))
                self.assertEqual([s["cell"] for s in e["samples"]], ["A1", "B1"])
                self.assertEqual(e["count"], 2)
        self.assertTrue(any("Mapped direct cell styles" in line for line in result["report"]))
        self.assertTrue(any("Rich-text runs" in line for line in result["report"]))

    def test_border_colour_and_date_display_losses_are_approximations(self):
        styles = (f'<styleSheet xmlns="{S}"><fonts><font/></fonts><fills><fill/></fills><borders><border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/></border></borders>'
                  '<cellXfs><xf numFmtId="14" borderId="0"/></cellXfs></styleSheet>')
        data = workbook_bytes({"Sheet1": '<row r="1"><c r="A1" s="0"><v>45200</v></c></row>'}, styles_xml=styles)
        result = xlsx_preview({"file_base64": b64(data)})
        severities = {(e["category"], e["severity"]) for e in result["report_entries"]}
        self.assertIn(("style.border", "approximated"), severities)
        self.assertIn(("style.number_format", "approximated"), severities)

    def test_convert_xlsx_cell_returns_the_edit_or_none(self):
        import xml.etree.ElementTree as ET
        from datahorizon.conversion_report import ConversionReport
        from datahorizon.interchange import XlsxCellContext, convert_xlsx_cell
        from datahorizon.xlsx_styles import XlsxStyles
        main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        report = ConversionReport()
        with zipfile.ZipFile(io.BytesIO(workbook_bytes({"Sheet1": ""}))) as archive:
            context = XlsxCellContext(mode="formulas", shared_strings=["hello"], styles=XlsxStyles(archive, "none", report),
                                      date_system="1900", sheet_names=None, sheet="Sheet1", report=report)
        parse = lambda xml: ET.fromstring(f'<c xmlns="{main}" {xml}</c>')
        self.assertEqual(convert_xlsx_cell(parse('r="B2" t="s"><v>0</v>'), context=context),
                         {"row": 2, "col": 2, "source": "hello", "kind": "text", "style": {}, "replace_style": True})
        self.assertIsNone(convert_xlsx_cell(parse('r="C3" t="zz"><v>0</v>'), context=context))
        self.assertEqual(report.entries()[0]["category"], "cell.unsupported_type")
        with self.assertRaises(EngineError):
            convert_xlsx_cell(parse('r="B2"><v>1</v>'), context=context)


REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
FEATURE_ROWS = '<row r="1"><c r="A1"><v>1</v></c></row>'
FEATURE_SHEET_AFTER = (
    '<mergeCells count="2"><mergeCell ref="A1:C1"/><mergeCell ref="A2:A3"/></mergeCells>'
    '<conditionalFormatting sqref="A1:A3"><cfRule type="cellIs" priority="1" operator="greaterThan"><formula>1</formula></cfRule>'
    '<cfRule type="cellIs" priority="2" operator="lessThan"><formula>0</formula></cfRule></conditionalFormatting>'
    '<conditionalFormatting sqref="B1"><cfRule type="colorScale" priority="3"/></conditionalFormatting>'
    '<dataValidations count="2"><dataValidation type="list" sqref="A2"><formula1>"a,b"</formula1></dataValidation>'
    '<dataValidation type="whole" sqref="B2"><formula1>0</formula1></dataValidation></dataValidations>'
    '<hyperlinks><hyperlink ref="A1" location="Sheet1!B2"/></hyperlinks>')
FEATURE_SHEET_BEFORE = '<cols><col min="1" max="1" width="20" customWidth="1"/><col min="3" max="5" width="9" customWidth="1"/></cols>'
# What Excel and openpyxl write in a workbook where nothing was changed from its defaults.
DEFAULT_WORKBOOK = ('<fileVersion appName="xl" lastEdited="7" lowestEdited="7" rupBuild="27328"/><workbookPr defaultThemeVersion="166925"/>'
                    '<workbookProtection/><bookViews><workbookView xWindow="0" yWindow="0" windowWidth="28800" windowHeight="12300"/></bookViews>')
DEFAULT_SHEET_BEFORE = ('<sheetPr><outlinePr summaryBelow="1" summaryRight="1"/><pageSetUpPr/></sheetPr><dimension ref="A1"/>'
                        '<sheetViews><sheetView tabSelected="1" workbookViewId="0"><selection activeCell="A1" sqref="A1"/></sheetView></sheetViews>'
                        '<sheetFormatPr baseColWidth="8" defaultRowHeight="15"/>')
DEFAULT_SHEET_AFTER = ('<sheetProtection sheet="0"/><printOptions gridLinesSet="1"/>'
                       '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
                       '<pageSetup paperSize="9" orientation="portrait" horizontalDpi="300" verticalDpi="300"/><headerFooter/>')


def features(result, category="sheet.feature"):
    return {e["detail"]: e for e in result["report_entries"] if e["category"] == category}


def sheet_entry(detail, count, *sheets):
    return {"category": "sheet.feature", "severity": "omitted", "detail": detail, "count": count,
            "samples": [{"sheet": sheet, "cell": None} for sheet in sheets or ("Sheet1",)]}


def default_book(sheets, *, workbook_after_sheets="", **options):
    """A package whose workbook part starts with Excel's default elements before <sheets>."""
    data = workbook_bytes(sheets, workbook_after_sheets=workbook_after_sheets + '<calcPr calcId="191029"/>', **options)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "xl/workbook.xml": payload = payload.replace(b"<sheets>", DEFAULT_WORKBOOK.encode() + b"<sheets>", 1)
            target.writestr(name, payload)
    return output.getvalue()


class FeatureReportTests(unittest.TestCase):
    def preview(self, data, **request):
        return xlsx_preview({"file_base64": b64(data), "mode": "formulas", **request})

    def test_each_unimported_worksheet_element_is_reported_by_name(self):
        data = workbook_bytes({"Sheet1": FEATURE_ROWS}, sheet_before={"Sheet1": FEATURE_SHEET_BEFORE},
                              sheet_after={"Sheet1": FEATURE_SHEET_AFTER})
        found = features(self.preview(data))
        self.assertEqual(found["merged_cells"], sheet_entry("merged_cells", 2))
        self.assertEqual(found["conditional_formatting"], sheet_entry("conditional_formatting", 3))
        self.assertEqual(found["data_validation"], sheet_entry("data_validation", 2))
        self.assertEqual(found["hyperlinks"], sheet_entry("hyperlinks", 1))
        # Columns, not <col> ranges: A, and C through E.
        self.assertEqual(found["column_widths"], sheet_entry("column_widths", 4))
        self.assertEqual(set(found), {"merged_cells", "conditional_formatting", "data_validation", "hyperlinks", "column_widths"})

    def test_the_free_text_feature_line_is_replaced_by_entries(self):
        data = workbook_bytes({"Sheet1": FEATURE_ROWS}, sheet_after={"Sheet1": FEATURE_SHEET_AFTER})
        result = self.preview(data)
        self.assertFalse(any(line.startswith("Worksheet features outside cell inputs") for line in result["report"]))
        self.assertFalse(any("advanced package parts" in line for line in result["report"]))
        self.assertIn("2 items: merged_cells (omitted); first at Sheet1", result["report"])
        self.assertEqual(result["totals"]["omitted"], 2+3+2+1)

    def test_a_workbook_with_default_settings_reports_no_features(self):
        data = default_book({"Sheet1": FEATURE_ROWS}, workbook_after_sheets="<definedNames/>",
                            sheet_before={"Sheet1": DEFAULT_SHEET_BEFORE}, sheet_after={"Sheet1": DEFAULT_SHEET_AFTER})
        for scope in ("selection", "workbook"):
            with self.subTest(scope=scope):
                result = self.preview(data, scope=scope)
                self.assertEqual([e for e in result["report_entries"] if e["category"].endswith(".feature")], [])
        for name in ("fidelity.xlsx", "inputs.xlsx"):
            with self.subTest(name=name):
                result = self.preview((Path(__file__).parent / "fixtures" / name).read_bytes(), scope="workbook")
                self.assertEqual([e for e in result["report_entries"] if e["category"].endswith(".feature")], [])

    def test_layout_protection_filter_and_print_settings_are_counted(self):
        before = ('<sheetPr><tabColor rgb="FFFF0000"/></sheetPr>'
                  '<sheetViews><sheetView workbookViewId="0" showGridLines="0"><pane xSplit="1" ySplit="2" topLeftCell="B3" state="frozen"/></sheetView></sheetViews>'
                  '<sheetFormatPr defaultRowHeight="20" customHeight="1" defaultColWidth="12"/>'
                  '<cols><col min="2" max="4" width="9" hidden="1"/><col min="6" max="6" outlineLevel="1" style="3"/></cols>')
        rows = ('<row r="1" ht="30" customHeight="1"><c r="A1"><v>1</v></c></row><row r="2" hidden="1"/>'
                '<row r="3" ht="12"/><row r="4" outlineLevel="1" s="2" customFormat="1"/>')
        after = ('<sheetProtection sheet="1" objects="1"/><autoFilter ref="A1:B4"/>'
                 '<pageMargins left="1" right="1" top="1" bottom="1" header="0.5" footer="0.5"/>'
                 '<pageSetup orientation="landscape"/><headerFooter><oddHeader>&amp;CReport</oddHeader></headerFooter>'
                 '<rowBreaks count="1"><brk id="3" max="16383" man="1"/></rowBreaks>'
                 f'<drawing r:id="rId1"/><tableParts count="2"><tablePart r:id="rId2"/><tablePart r:id="rId3"/></tableParts>')
        data = workbook_bytes({"Sheet1": rows}, sheet_before={"Sheet1": before}, sheet_after={"Sheet1": after})
        found = features(self.preview(data))
        self.assertEqual({detail: entry["count"] for detail, entry in found.items()},
                         {"tab_colour": 1, "frozen_panes": 1, "sheet_view": 1, "sheet_format_defaults": 1, "column_widths": 3,
                          # Row 3's height is not custom (no customHeight), so only row 1 counts.
                          "hidden_rows_columns": 3+1, "outline": 1+1, "row_column_styles": 1+1, "row_heights": 1,
                          "sheet_protection": 1, "auto_filter": 1, "page_setup": 1, "drawings": 1, "excel_tables": 2})

    def test_cell_level_features_are_counted(self):
        rows = ('<row r="1"><c r="A1" vm="1" t="e"><v>#VALUE!</v></c><c r="B1" ph="1" t="inlineStr"><is><t>x</t></is></c>'
                '<c r="C1" t="inlineStr"><is><r><rPr><b/></rPr><t>bold</t></r><r><t> plain</t></r></is></c></row>')
        found = features(self.preview(workbook_bytes({"Sheet1": rows})))
        self.assertEqual({detail: entry["count"] for detail, entry in found.items()}, {"rich_values": 1, "phonetic": 1, "rich_text": 1})

    def test_extension_list_features_are_named_and_others_are_extensions(self):
        x14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
        after = (f'<extLst><ext uri="{{05C60535-1F16-4fd2-B633-F4F36F0B64E0}}" xmlns:x14="{x14}"><x14:sparklineGroups>'
                 '<x14:sparklineGroup><x14:sparklines><x14:sparkline/></x14:sparklines></x14:sparklineGroup>'
                 '<x14:sparklineGroup/></x14:sparklineGroups></ext>'
                 f'<ext uri="{{78C0D931-6437-407d-A8EE-F0AAD7539E65}}" xmlns:x14="{x14}"><x14:conditionalFormattings>'
                 '<x14:conditionalFormatting><x14:cfRule type="dataBar"/></x14:conditionalFormatting></x14:conditionalFormattings></ext>'
                 '<ext uri="{00000000-0000-0000-0000-000000000000}"><unknown/></ext></extLst>')
        found = features(self.preview(workbook_bytes({"Sheet1": FEATURE_ROWS}, sheet_after={"Sheet1": after})))
        self.assertEqual({detail: entry["count"] for detail, entry in found.items()},
                         {"sparklines": 2, "conditional_formatting": 1, "extensions": 1})

    def test_comments_are_found_through_the_sheet_relationships(self):
        rels = (f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{REL}/vmlDrawing" Target="../drawings/vmlDrawing1.vml"/>'
                f'<Relationship Id="rId2" Type="{REL}/comments" Target="../comments1.xml"/></Relationships>')
        data = workbook_bytes({"Sheet1": FEATURE_ROWS, "Plain": FEATURE_ROWS},
                              sheet_after={"Sheet1": '<legacyDrawing r:id="rId1"/>', "Plain": '<legacyDrawing r:id="rId1"/>'},
                              extra_parts={"xl/worksheets/_rels/sheet1.xml.rels": rels})
        found = features(self.preview(data, scope="workbook"))
        self.assertEqual(found["comments"], sheet_entry("comments", 1, "Sheet1"))
        # A legacy drawing without comments holds form controls or shapes.
        self.assertEqual(found["legacy_drawings"], sheet_entry("legacy_drawings", 1, "Plain"))

    def test_workbook_features_and_package_parts_are_structured_entries(self):
        names = ('<definedNames><definedName name="Rate">Sheet1!$A$1</definedName><definedName name="Two">2</definedName>'
                 '<definedName name="_xlnm.Print_Area" localSheetId="0">Sheet1!$A$1:$B$2</definedName></definedNames>')
        after_sheets = f'<externalReferences><externalReference r:id="rIdX"/></externalReferences>{names}<calcPr calcMode="manual" iterate="1"/>'
        data = workbook_bytes({"Sheet1": FEATURE_ROWS, "Hidden": "", "Secret": ""}, workbook_after_sheets=after_sheets,
                              extra_parts={"xl/charts/chart1.xml": "<c/>", "xl/charts/chart2.xml": "<c/>", "xl/vbaProject.bin": b"\0",
                                           "xl/pivotTables/pivotTable1.xml": "<p/>", "customXml/item1.xml": "<x/>",
                                           "customXml/itemProps1.xml": "<x/>", "xl/connections.xml": "<x/>"})
        workbook = zipfile.ZipFile(io.BytesIO(data)).read("xl/workbook.xml").decode()
        workbook = (workbook.replace('<sheet name="Hidden"', '<sheet state="hidden" name="Hidden"')
                    .replace('<sheet name="Secret"', '<sheet state="veryHidden" name="Secret"')
                    .replace("<sheets>", '<workbookProtection lockStructure="1"/><sheets>'))
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
            for name in source.namelist(): target.writestr(name, workbook if name == "xl/workbook.xml" else source.read(name))
        for scope in ("selection", "workbook"):
            with self.subTest(scope=scope):
                found = features(self.preview(output.getvalue(), scope=scope), "workbook.feature")
                self.assertEqual({detail: entry["count"] for detail, entry in found.items()},
                                 {"defined_names": 3, "external_references": 1, "hidden_sheets": 2, "workbook_protection": 1,
                                  "calculation_settings": 1, "charts": 2, "vba": 1, "pivot_tables": 1, "custom_xml": 1, "connections": 1})
                self.assertEqual(found["hidden_sheets"]["samples"], [{"sheet": "Hidden", "cell": None}, {"sheet": "Secret", "cell": None}])
                self.assertEqual(found["defined_names"]["samples"], [])

    def test_workbook_scope_reports_each_sheet_once_and_workbook_features_once(self):
        after = '<mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>'
        data = workbook_bytes({"One": FEATURE_ROWS, "Two": FEATURE_ROWS, "Three": ""}, sheet_after={"One": after, "Two": after},
                              workbook_after_sheets='<definedNames><definedName name="X">1</definedName></definedNames>')
        result = self.preview(data, scope="workbook")
        self.assertEqual(features(result)["merged_cells"], sheet_entry("merged_cells", 2, "One", "Two"))
        self.assertEqual(features(result, "workbook.feature")["defined_names"]["count"], 1)
        # A selection reports only its own sheet's features.
        selection = self.preview(data, sheet_index=2)
        self.assertEqual(features(selection)["merged_cells"], sheet_entry("merged_cells", 1, "Two"))



# A hand-built approximation of what LibreOffice Calc writes for an untouched sheet (from its documented output,
# not a LibreOffice-saved file): explicit but non-custom row heights and column widths, its own default margins,
# page setup and header/footer, and its reference-syntax workbook extension.
LIBREOFFICE_WORKBOOK = ('<fileVersion appName="Calc"/><workbookPr backupFile="false" showObjects="all" date1904="false"/>'
                        '<workbookProtection/><bookViews><workbookView showHorizontalScroll="true" showVerticalScroll="true" '
                        'showSheetTabs="true" xWindow="0" yWindow="0" windowWidth="16384" windowHeight="8192" tabRatio="500" '
                        'firstSheet="0" activeTab="0"/></bookViews>')
LIBREOFFICE_AFTER_SHEETS = ('<calcPr iterateCount="100" refMode="A1" iterate="false" iterateDelta="0.001"/>'
                            '<extLst><ext xmlns:loext="http://schemas.libreoffice.org/" uri="{7626C862-2A13-11E5-B345-FEFF819CDC9F}">'
                            '<loext:extCalcPr stringRefSyntax="CalcA1"/></ext></extLst>')
LIBREOFFICE_BEFORE = ('<sheetPr filterMode="false"><pageSetUpPr fitToPage="false"/></sheetPr><dimension ref="A1:B2"/>'
                      '<sheetViews><sheetView showFormulas="false" showGridLines="true" showRowColHeaders="true" showZeros="true" '
                      'rightToLeft="false" tabSelected="true" showOutlineSymbols="true" defaultGridColor="true" view="normal" '
                      'topLeftCell="A1" colorId="64" zoomScale="100" zoomScaleNormal="100" zoomScalePageLayoutView="100" workbookViewId="0">'
                      '<selection pane="topLeft" activeCell="A1" activeCellId="0" sqref="A1"/></sheetView></sheetViews>'
                      '<sheetFormatPr defaultColWidth="11.53515625" defaultRowHeight="12.8" zeroHeight="false" outlineLevelRow="0" outlineLevelCol="0"/>'
                      '<cols><col collapsed="false" customWidth="false" hidden="false" outlineLevel="0" max="1024" min="1" style="0" width="11.52"/></cols>')
LIBREOFFICE_ROWS = ('<row r="1" customFormat="false" ht="12.8" hidden="false" customHeight="false" outlineLevel="0" collapsed="false">'
                    '<c r="A1"><v>1</v></c><c r="B1"><v>2</v></c></row>'
                    '<row r="2" customFormat="false" ht="12.8" hidden="false" customHeight="false" outlineLevel="0" collapsed="false">'
                    '<c r="A2"><v>3</v></c></row>')
LIBREOFFICE_AFTER = ('<printOptions headings="false" gridLines="false" gridLinesSet="true" horizontalCentered="false" verticalCentered="false"/>'
                     '<pageMargins left="0.7875" right="0.7875" top="1.05277777777778" bottom="1.05277777777778" header="0.7875" footer="0.7875"/>'
                     '<pageSetup paperSize="9" scale="100" fitToWidth="1" fitToHeight="1" pageOrder="downThenOver" orientation="portrait" '
                     'blackAndWhite="false" draft="false" cellComments="none" firstPageNumber="1" useFirstPageNumber="true" '
                     'horizontalDpi="300" verticalDpi="300" copies="1"/>'
                     '<headerFooter differentFirst="false" differentOddEven="false">'
                     '<oddHeader>&amp;C&amp;&quot;Times New Roman,Regular&quot;&amp;12&amp;A</oddHeader>'
                     '<oddFooter>&amp;C&amp;&quot;Times New Roman,Regular&quot;&amp;12Page &amp;P</oddFooter></headerFooter>')


def prefixed_book(prefix, sheets, **options):
    """A package whose workbook part has `prefix` before <sheets>."""
    data = workbook_bytes(sheets, **options)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "xl/workbook.xml": payload = payload.replace(b"<sheets>", prefix.encode() + b"<sheets>", 1)
            target.writestr(name, payload)
    return output.getvalue()


def libreoffice_book(rows=LIBREOFFICE_ROWS, before=LIBREOFFICE_BEFORE, after=LIBREOFFICE_AFTER):
    return prefixed_book(LIBREOFFICE_WORKBOOK, {"Sheet1": rows, "Sheet2": rows}, workbook_after_sheets=LIBREOFFICE_AFTER_SHEETS,
                         sheet_before={"Sheet1": before, "Sheet2": before}, sheet_after={"Sheet1": after, "Sheet2": after})


SHEET_TYPES = {"chart": "chartsheet", "dialog": "dialogsheet",
               "macro": "http://schemas.microsoft.com/office/2006/relationships/xlMacrosheet"}


def book_with_sheet_kinds(kinds, data_rows=FEATURE_ROWS):
    """A worksheet "Data" (unless data_rows is None) followed by one sheet of each kind in `kinds`."""
    extra, rels, parts = "", [], {}
    for index, kind in enumerate(kinds, 1):
        kind_type = SHEET_TYPES[kind]
        relation_type = kind_type if kind_type.startswith("http") else f"{REL}/{kind_type}"
        extra += f'<sheet name="{kind.title()}{index}" sheetId="{10+index}" r:id="rIdK{index}"/>'
        rels.append(f'<Relationship Id="rIdK{index}" Type="{relation_type}" Target="other/sheet{index}.xml"/>')
        parts[f"xl/other/sheet{index}.xml"] = f'<{kind}sheet xmlns="{S}"/>'
    sheets = {"Data": data_rows} if data_rows is not None else {}
    data = workbook_bytes(sheets or {"Placeholder": ""}, extra_sheets=extra, workbook_rels=rels, extra_parts=parts)
    if data_rows is None:
        # Drop the placeholder worksheet so the package has only the other sheets.
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
            for name in source.namelist():
                payload = source.read(name)
                if name == "xl/workbook.xml": payload = payload.replace(b'<sheet name="Placeholder" sheetId="1" r:id="rId1"/>', b"")
                target.writestr(name, payload)
        data = output.getvalue()
    return data


class ApplicationDefaultTests(unittest.TestCase):
    def preview(self, data, **request):
        return xlsx_preview({"file_base64": b64(data), "mode": "formulas", **request})

    def test_a_libreoffice_style_default_sheet_reports_no_features(self):
        for scope in ("selection", "workbook"):
            with self.subTest(scope=scope):
                result = self.preview(libreoffice_book(), scope=scope)
                self.assertEqual([e for e in result["report_entries"] if e["category"].endswith(".feature")], [])

    def test_explicit_custom_sizes_still_report(self):
        rows = LIBREOFFICE_ROWS.replace('ht="12.8" hidden="false" customHeight="false"', 'ht="30" hidden="false" customHeight="true"', 1)
        before = LIBREOFFICE_BEFORE.replace(
            '<cols>', '<cols><col customWidth="true" max="1" min="1" width="30"/>').replace('max="1024" min="1"', 'max="1024" min="2"')
        after = LIBREOFFICE_AFTER.replace("12Page &amp;P", "12Confidential")
        found = features(self.preview(libreoffice_book(rows, before, after)))
        self.assertEqual({detail: entry["count"] for detail, entry in found.items()},
                         {"row_heights": 1, "column_widths": 1, "page_setup": 1})

    def test_a_width_that_differs_from_the_default_counts_without_custom_width(self):
        before = ('<sheetFormatPr defaultColWidth="10" defaultRowHeight="15"/>'
                  '<cols><col min="1" max="2" width="10"/><col min="3" max="3" width="25" bestFit="1"/></cols>')
        found = features(self.preview(workbook_bytes({"Sheet1": FEATURE_ROWS}, sheet_before={"Sheet1": before})))
        self.assertEqual(found["column_widths"]["count"], 1)

    def test_an_x14_twin_of_a_data_bar_counts_once(self):
        x14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
        after = ('<conditionalFormatting sqref="A1:A3"><cfRule type="dataBar" priority="1"><dataBar><cfvo type="min"/><cfvo type="max"/>'
                 '<color rgb="FF638EC6"/></dataBar><extLst><ext uri="{B025F937-C7B1-47D3-B67F-A62EFF666E3E}" xmlns:x14="' + x14 + '">'
                 '<x14:id>{1A2B}</x14:id></ext></extLst></cfRule></conditionalFormatting>'
                 '<extLst><ext uri="{78C0D931-6437-407d-A8EE-F0AAD7539E65}" xmlns:x14="' + x14 + '"><x14:conditionalFormattings>'
                 '<x14:conditionalFormatting><x14:cfRule type="dataBar" id="{1A2B}"/></x14:conditionalFormatting>'
                 '<x14:conditionalFormatting><x14:cfRule type="iconSet" id="{3C4D}"/></x14:conditionalFormatting>'
                 '</x14:conditionalFormattings></ext></extLst>')
        found = features(self.preview(workbook_bytes({"Sheet1": FEATURE_ROWS}, sheet_after={"Sheet1": after})))
        # The data bar and its twin are one rule; the icon set lives only in the extension.
        self.assertEqual({detail: entry["count"] for detail, entry in found.items()}, {"conditional_formatting": 2})

    def test_comments_are_counted_in_the_comments_part(self):
        rels = (f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId2" Type="{REL}/comments" Target="../comments1.xml"/></Relationships>')
        comments = (f'<comments xmlns="{S}"><authors><author>A</author></authors><commentList>'
                    + "".join(f'<comment ref="A{i}" authorId="0"><text><t>c{i}</t></text></comment>' for i in range(1, 4))
                    + "</commentList></comments>")
        with_part = workbook_bytes({"Sheet1": FEATURE_ROWS}, extra_parts={"xl/worksheets/_rels/sheet1.xml.rels": rels, "xl/comments1.xml": comments})
        without = workbook_bytes({"Sheet1": FEATURE_ROWS}, extra_parts={"xl/worksheets/_rels/sheet1.xml.rels": rels})
        self.assertEqual(features(self.preview(with_part))["comments"]["count"], 3)
        self.assertEqual(features(self.preview(without))["comments"]["count"], 1)

    def test_an_unusual_sheet_relationship_part_never_rejects_valid_cells(self):
        def with_rels(payload):
            return workbook_bytes({"Sheet1": FEATURE_ROWS}, extra_parts={"xl/worksheets/_rels/sheet1.xml.rels": payload})
        self.assertEqual(features(self.preview(with_rels(b""))), {})
        latin = '<?xml version="1.0" encoding="ISO-8859-1"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
        for payload in (latin.encode("latin-1"), b"<Relationships"):
            with self.subTest(payload=payload[:40]):
                result = self.preview(with_rels(payload))
                self.assertTrue(result["can_import"])
                self.assertEqual(features(result)["relationships_unreadable"]["count"], 1)
        with self.assertRaises(EngineError) as raised:
            self.preview(with_rels(b'<!DOCTYPE x [<!ENTITY a "b">]><Relationships/>'))
        self.assertEqual(raised.exception.body["code"], "INVALID_SPREADSHEET")


class OtherSheetKindTests(unittest.TestCase):
    def preview(self, data, **request):
        return xlsx_preview({"file_base64": b64(data), "mode": "formulas", **request})

    def test_chart_dialog_and_macro_sheets_are_reported_and_not_imported(self):
        rows = '<row r="1"><c r="A1"><v>1</v></c><c r="B1"><f>\'Chart1\'!A1+A1</f><v>1</v></c></row>'
        data = book_with_sheet_kinds(["chart", "dialog", "macro"], rows)
        result = self.preview(data, scope="workbook")
        self.assertEqual([sheet["name"] for sheet in result["workbook_sheets"]], ["Data"])
        self.assertTrue(result["can_import"])
        wide = features(result, "workbook.feature")
        self.assertEqual({detail: (entry["count"], entry["samples"]) for detail, entry in wide.items()},
                         {"chart_sheets": (1, [{"sheet": "Chart1", "cell": None}]),
                          "dialog_sheets": (1, [{"sheet": "Dialog2", "cell": None}]),
                          "macro_sheets": (1, [{"sheet": "Macro3", "cell": None}])})
        # A formula that refers to a sheet that is not imported is a reference to a missing sheet.
        self.assertIn("formula.missing_sheet", {e["category"] for e in result["report_entries"]})

    def test_selection_of_a_worksheet_reports_the_other_kinds_and_choosing_one_is_an_argument_error(self):
        data = book_with_sheet_kinds(["chart"])
        selection = self.preview(data, sheet_index=1)
        self.assertEqual(features(selection, "workbook.feature")["chart_sheets"]["count"], 1)
        with self.assertRaises(EngineError) as raised:
            self.preview(data, sheet_index=2)
        self.assertEqual(raised.exception.body["code"], "INVALID_ARGUMENT")
        self.assertIn("'Chart1' is a chart sheet", raised.exception.body["message"])

    def test_a_workbook_of_only_chart_sheets_prepares_nothing_to_apply(self):
        result = self.preview(book_with_sheet_kinds(["chart", "chart"], None), scope="workbook")
        self.assertEqual((result["workbook_sheets"], result["can_import"]), ([], False))
        self.assertEqual(features(result, "workbook.feature")["chart_sheets"]["count"], 2)
        self.assertTrue(result["issues"])

    def test_the_corpus_chart_sheet_workbook_imports_its_worksheet(self):
        data = (Path(__file__).parent / "fixtures" / "corpus" / "chart-sheet.xlsx").read_bytes()
        result = self.preview(data, scope="workbook")
        self.assertEqual([sheet["name"] for sheet in result["workbook_sheets"]], ["Data"])
        self.assertEqual(features(result, "workbook.feature")["chart_sheets"]["samples"], [{"sheet": "Sales chart", "cell": None}])


class WorksheetCountTests(unittest.TestCase):
    """Only worksheets count toward a reader's sheet limit, and a selection can name any worksheet's position."""

    def book(self, worksheets):
        # A chart sheet first, then the worksheets, so the last worksheet sits at position worksheets+1.
        sheets = {f"W{index}": f'<row r="1"><c r="A1"><v>{index}</v></c></row>' for index in range(1, worksheets+1)}
        chart = '<sheet name="Chart" sheetId="900" r:id="rIdChart"/>'
        data = workbook_bytes(sheets, extra_sheets="", workbook_rels=[f'<Relationship Id="rIdChart" Type="{REL}/chartsheet" Target="other/chart.xml"/>'],
                              extra_parts={"xl/other/chart.xml": f'<chartsheet xmlns="{S}"/>'})
        # Put the chart sheet first in the workbook's sheet list.
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
            for name in source.namelist():
                payload = source.read(name)
                if name == "xl/workbook.xml": payload = payload.replace(b"<sheets>", b"<sheets>"+chart.encode())
                target.writestr(name, payload)
        return output.getvalue()

    def test_sixty_four_worksheets_and_a_chart_sheet_import_whole_and_by_selection(self):
        data = self.book(64)
        whole = xlsx_preview({"file_base64": b64(data), "scope": "workbook"})
        self.assertEqual(len(whole["workbook_sheets"]), 64)
        self.assertEqual(whole["workbook_sheets"][-1]["name"], "W64")
        last = xlsx_preview({"file_base64": b64(data), "sheet_index": 65})
        self.assertEqual((last["sheet"], last["cells"][0]["source"]), ("W64", "64"))
        with self.assertRaises(EngineError) as raised: xlsx_preview({"file_base64": b64(data), "sheet_index": 1})
        self.assertEqual(raised.exception.body["code"], "INVALID_ARGUMENT")
        with self.assertRaises(EngineError) as raised: xlsx_preview({"file_base64": b64(data), "sheet_index": 66})
        self.assertEqual(raised.exception.body["code"], "INVALID_SPREADSHEET")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"book.xlsx"; path.write_bytes(data)
            from datahorizon.xlsx_stream import read_workbook
            names = []
            class Sink:
                class strings:
                    put = staticmethod(lambda index, text: None)
                def sheet(self, name): names.append(name)
                def cells(self, batch): pass
            read_workbook(path, {}, Sink(), lambda completed, phase: None)
            self.assertEqual(len(names), 64)

    def test_sixty_five_worksheets_exceed_the_bounded_whole_workbook_limit(self):
        data = self.book(65)
        with self.assertRaises(EngineError) as raised: xlsx_preview({"file_base64": b64(data), "scope": "workbook"})
        self.assertEqual(raised.exception.body["code"], "LIMIT")
        # A selection still reaches any of them.
        self.assertEqual(xlsx_preview({"file_base64": b64(data), "sheet_index": 66})["sheet"], "W65")

    def test_an_ods_selection_reaches_a_table_after_the_sixty_fourth(self):
        import ods_builder
        from datahorizon.ods import ods_preview
        data = ods_builder.workbook_bytes({f"T{index}": '<table:table-row><table:table-cell office:value-type="float" office:value="1"/></table:table-row>'
                                           for index in range(1, 66)})
        self.assertEqual(ods_preview({"file_base64": b64(data), "sheet_index": 65})["sheet"], "T65")
        with self.assertRaises(EngineError) as raised: ods_preview({"file_base64": b64(data), "scope": "workbook"})
        self.assertEqual(raised.exception.body["code"], "LIMIT")


if __name__ == "__main__":
    unittest.main()
