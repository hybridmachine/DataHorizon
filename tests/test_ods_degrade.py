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
from ods_builder import NAMESPACES, workbook_bytes
from datahorizon.ods import O


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
        rows = row(number(5), formula("of:=STDEV.S([.A1])", "5"), formula("of:=STDEV.S([.A1])"),
                   formula("of:=[.A1] ` 2", "5"), formula("of:=[.A1] ` 2"))
        result = preview(rows, row=2)
        self.assertTrue(result["can_import"], result)
        # Formulas the calculator cannot parse still translate when the scanner can read their references.
        self.assertEqual([(c["row"], c["col"], c["source"]) for c in result["cells"]],
                         [(2, 1, "5"), (2, 2, "=STDEV.S(A2)"), (2, 3, "=STDEV.S(A2)")])
        self.assertIn(entry("formula.untranslatable", "omitted", "", "D1", "E1"), result["report_entries"])
        self.assertEqual(result["totals"]["omitted"], 2)
        self.assertEqual([c["source"] for c in preview(rows)["cells"]],
                         ["5", "=STDEV.S(A1)", "=STDEV.S(A1)", "=A1 ` 2", "=A1 ` 2"])

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


DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
CONFIG = "urn:oasis:names:tc:opendocument:xmlns:config:1.0"
LAYOUT_STYLES = ('<style:style style:name="co1" style:family="table-column"><style:table-column-properties style:column-width="2.258cm"/></style:style>'
                 '<style:style style:name="co2" style:family="table-column"><style:table-column-properties style:column-width="5cm"/></style:style>'
                 '<style:style style:name="ro1" style:family="table-row"><style:table-row-properties style:row-height="0.452cm" style:use-optimal-row-height="true"/></style:style>'
                 '<style:style style:name="ro2" style:family="table-row"><style:table-row-properties style:row-height="1cm" style:use-optimal-row-height="false"/></style:style>'
                 '<style:style style:name="ta1" style:family="table"><style:table-properties table:display="true"/></style:style>'
                 '<style:style style:name="ta2" style:family="table"><style:table-properties table:display="false"/></style:style>'
                 '<style:style style:name="Mapped" style:family="table-cell"><style:map style:condition="cell-content()&gt;1" style:apply-style-name="Default"/></style:style>')
FEATURE_TABLE = (
    '<table:table table:name="Sheet1" table:style-name="ta1" table:protected="true">'
    '<table:table-column table:style-name="co2"/><table:table-column table:style-name="co1" table:visibility="collapse" table:number-columns-repeated="2"/>'
    '<table:table-column table:style-name="co1" table:number-columns-repeated="1021"/>'
    '<table:table-row table:style-name="ro2" table:number-rows-repeated="2">'
    '<table:table-cell table:number-columns-spanned="2" table:number-rows-spanned="1" office:value-type="float" office:value="1"/><table:covered-table-cell/>'
    '<table:table-cell office:value-type="float" office:value="2" table:content-validation-name="val1"/></table:table-row>'
    '<table:table-row-group><table:table-row table:visibility="collapse"><table:table-cell office:value-type="string"><text:p><text:a xlink:href="https://example.com/">site</text:a></text:p></table:table-cell>'
    '<table:table-cell office:value-type="float" office:value="3"><office:annotation><text:p>note</text:p></office:annotation></table:table-cell></table:table-row></table:table-row-group>'
    '<calcext:conditional-formats><calcext:conditional-format calcext:target-range-address="Sheet1.A1:Sheet1.A3"><calcext:condition calcext:apply-style-name="Default" calcext:value="&gt;1"/></calcext:conditional-format>'
    '<calcext:conditional-format calcext:target-range-address="Sheet1.B1"><calcext:color-scale/></calcext:conditional-format></calcext:conditional-formats>'
    '</table:table>')
SPREADSHEET_BEFORE = ('<table:content-validations><table:content-validation table:name="val1" table:condition="of:cell-content-is-whole-number()"/>'
                      '<table:content-validation table:name="unused" table:condition="of:cell-content-is-decimal-number()"/></table:content-validations>')
SPREADSHEET_AFTER = ('<table:named-expressions><table:named-range table:name="Rate" table:base-cell-address="$Sheet1.$A$1" table:cell-range-address="$Sheet1.$A$1"/>'
                     '<table:named-expression table:name="Two" table:base-cell-address="$Sheet1.$A$1" table:expression="of:=2"/></table:named-expressions>'
                     '<table:database-ranges><table:database-range table:name="__Anonymous_Sheet_DB__0" table:target-range-address="Sheet1.A1:Sheet1.B3" table:display-filter-buttons="true"/>'
                     '<table:database-range table:name="plain" table:target-range-address="Sheet1.A1:Sheet1.A2"/></table:database-ranges>')


def feature_content(tables, *, before="", after="", spreadsheet_attributes="", styles=LAYOUT_STYLES):
    namespaces = NAMESPACES + f' xmlns:draw="{DRAW}" xmlns:xlink="http://www.w3.org/1999/xlink"'
    return (f'<office:document-content {namespaces}><office:automatic-styles>{styles}</office:automatic-styles><office:body>'
            f'<office:spreadsheet{spreadsheet_attributes}>{before}{tables}{after}</office:spreadsheet></office:body></office:document-content>')


def settings(*entries):
    """settings.xml with per-sheet view entries: (sheet name, horizontal split mode, vertical split mode)."""
    items = "".join(f'<config:config-item-map-entry config:name="{name}"><config:config-item config:name="HorizontalSplitMode" config:type="short">{h}</config:config-item>'
                    f'<config:config-item config:name="VerticalSplitMode" config:type="short">{v}</config:config-item></config:config-item-map-entry>'
                    for name, h, v in entries)
    return (f'<office:document-settings xmlns:office="{O}" xmlns:config="{CONFIG}"><office:settings>'
            '<config:config-item-set config:name="ooo:view-settings"><config:config-item-map-indexed config:name="Views"><config:config-item-map-entry>'
            f'<config:config-item-map-named config:name="Tables">{items}</config:config-item-map-named>'
            '</config:config-item-map-entry></config:config-item-map-indexed></config:config-item-set></office:settings></office:document-settings>')


def feature_counts(result, category="sheet.feature"):
    return {e["detail"]: e["count"] for e in result["report_entries"] if e["category"] == category}


class OdsFeatureReportTests(unittest.TestCase):
    def preview(self, data, **request):
        return ods_preview({"file_base64": b64(data), "mode": "formulas", **request})

    def book(self, **options):
        content = feature_content(FEATURE_TABLE + '<table:table table:name="Hidden" table:style-name="ta2"/>',
                                  before=SPREADSHEET_BEFORE, after=SPREADSHEET_AFTER, spreadsheet_attributes=' table:structure-protected="true"')
        return workbook_bytes({}, content=content, **options)

    def test_each_unimported_sheet_feature_is_reported_by_name(self):
        result = self.preview(self.book())
        self.assertEqual(feature_counts(result),
                         {"merged_cells": 1, "column_widths": 1, "row_heights": 2, "hidden_rows_columns": 1+2, "outline": 1,
                          "conditional_formatting": 2, "data_validation": 1, "hyperlinks": 1, "comments": 1,
                          "auto_filter": 1, "sheet_protection": 1})
        (merged,) = [e for e in result["report_entries"] if e["detail"] == "merged_cells"]
        self.assertEqual(merged, {"category": "sheet.feature", "severity": "omitted", "detail": "merged_cells", "count": 1,
                                  "samples": [{"sheet": "Sheet1", "cell": None}]})

    def test_workbook_features_are_reported_once(self):
        for scope in ("selection", "workbook"):
            with self.subTest(scope=scope):
                result = self.preview(self.book(), scope=scope)
                self.assertEqual(feature_counts(result, "workbook.feature"),
                                 {"defined_names": 2, "hidden_sheets": 1, "workbook_protection": 1})
                (hidden,) = [e for e in result["report_entries"] if e["detail"] == "hidden_sheets"]
                self.assertEqual(hidden["samples"], [{"sheet": "Hidden", "cell": None}])

    def test_mapped_cell_styles_count_as_conditional_formatting(self):
        rows = row(number(1, **{"table:style-name": "Mapped"}), number(2, **{"table:style-name": "Mapped"}))
        content = feature_content(f'<table:table table:name="Sheet1">{rows}</table:table>')
        self.assertEqual(feature_counts(self.preview(workbook_bytes({}, content=content))), {"conditional_formatting": 1})

    def test_frozen_and_split_panes_come_from_the_settings(self):
        content = feature_content('<table:table table:name="Sheet1"/><table:table table:name="Two"/><table:table table:name="Three"/>')
        data = workbook_bytes({}, content=content, extra_parts={"settings.xml": settings(("Sheet1", 2, 0), ("Two", 1, 1), ("Three", 0, 0))})
        result = self.preview(data, scope="workbook")
        entries = {e["detail"]: e for e in result["report_entries"]}
        self.assertEqual(entries["frozen_panes"]["samples"], [{"sheet": "Sheet1", "cell": None}])
        self.assertEqual(entries["split_panes"]["samples"], [{"sheet": "Two", "cell": None}])
        self.assertEqual(set(entries), {"frozen_panes", "split_panes"})

    def test_a_spreadsheet_with_default_settings_reports_no_features(self):
        table = ('<table:table table:name="Sheet1" table:style-name="ta1"><table:table-column table:style-name="co1" table:number-columns-repeated="1024" table:default-cell-style-name="Default"/>'
                 f'<table:table-row table:style-name="ro1">{number(1)}</table:table-row>'
                 '<table:table-row table:style-name="ro1" table:number-rows-repeated="1048575"><table:table-cell table:number-columns-repeated="1024"/></table:table-row>'
                 '<office:forms form:automatic-focus="false" form:apply-design-mode="false" xmlns:form="urn:oasis:names:tc:opendocument:xmlns:form:1.0"/></table:table>')
        content = feature_content(table, before='<table:calculation-settings table:automatic-find-labels="false"/>', after="<table:named-expressions/>")
        data = workbook_bytes({}, content=content, extra_parts={"settings.xml": settings(("Sheet1", 0, 0))})
        for scope in ("selection", "workbook"):
            with self.subTest(scope=scope):
                result = self.preview(data, scope=scope)
                self.assertEqual([e for e in result["report_entries"] if e["category"].endswith(".feature")], [])
        for name in ("fidelity.ods", "inputs.ods"):
            with self.subTest(name=name):
                result = self.preview((Path(__file__).parent / "fixtures" / name).read_bytes(), scope="workbook")
                self.assertEqual([e for e in result["report_entries"] if e["category"].endswith(".feature")], [])

    def test_drawings_scenarios_and_package_parts(self):
        table = ('<table:table table:name="Sheet1"><table:scenario table:scenario-ranges="Sheet1.A1" table:is-active="true"/>'
                 '<table:shapes><draw:frame draw:name="Chart 1"><draw:object xlink:href="./Object 1"/></draw:frame></table:shapes>'
                 f'<table:table-row>{number(1)}</table:table-row></table:table>')
        content = feature_content(table, after='<table:data-pilot-tables><table:data-pilot-table table:name="P" table:target-range-address="Sheet1.D1:Sheet1.E3"/></table:data-pilot-tables>')
        data = workbook_bytes({}, content=content, extra_parts={"Object 1/content.xml": "<x/>", "Basic/Standard/Module1.xml": "<x/>"})
        result = self.preview(data)
        self.assertEqual(feature_counts(result), {"scenarios": 1, "drawings": 1})
        self.assertEqual(feature_counts(result, "workbook.feature"), {"pivot_tables": 1, "embedded_objects": 1, "macros": 1})


    def test_a_repeated_spanning_cell_counts_each_repeat(self):
        rows = row('<table:table-cell table:number-columns-spanned="2" table:number-columns-repeated="3" office:value-type="float" office:value="1"/>')
        content = feature_content(f'<table:table table:name="Sheet1">{rows}</table:table>')
        self.assertEqual(feature_counts(self.preview(workbook_bytes({}, content=content)))["merged_cells"], 3)

    def test_unreadable_view_settings_skip_pane_detection_and_never_reject(self):
        content = feature_content('<table:table table:name="Sheet1"><table:table-row><table:table-cell office:value-type="float" office:value="1"/></table:table-row></table:table>')
        for payload in (b"", b'<!DOCTYPE x><office:document-settings/>', '<?xml version="1.0" encoding="ISO-8859-1"?><a/>'.encode("latin-1"), b"<broken"):
            for scope in ("selection", "workbook"):
                with self.subTest(payload=payload[:20], scope=scope):
                    result = self.preview(workbook_bytes({}, content=content, extra_parts={"settings.xml": payload}), scope=scope)
                    self.assertTrue(result["can_import"])
                    self.assertEqual(feature_counts(result, "workbook.feature"), {"view_settings_unreadable": 1})


if __name__ == "__main__":
    unittest.main()
