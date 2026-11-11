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


if __name__ == "__main__":
    unittest.main()
