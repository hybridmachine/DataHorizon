# SPDX-License-Identifier: GPL-3.0-only
"""Bounded shared formulas, inherited styles and independent producer fixtures."""
import base64
import hashlib
import json
from contextlib import closing
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from datahorizon.engine import Workbook, EngineError
from datahorizon.interchange import xlsx_preview
from datahorizon.ods import ods_preview
from test_interchange import fixture, preview
from test_ods import fixture as ods_fixture, preview as ods_parse


class FidelityTests(unittest.TestCase):
    def test_shared_formulas_expand_relative_absolute_and_quoted_text(self):
        # The member precedes its master in package order.
        data=fixture('<c r="C3"><f t="shared" si="4"/><v>99</v></c>'
            '<c r="C2"><f t="shared" si="4" ref="C2:C3">IF(TRUE,B2+$A$1,"B2")</f><v>99</v></c>')
        result=preview(data)
        self.assertTrue(result["can_import"],result)
        self.assertEqual([c["source"] for c in result["cells"]],['=IF(TRUE,B3+$A$1,"B2")','=IF(TRUE,B2+$A$1,"B2")'])
        self.assertIn("Shared formulas expand"," ".join(result["report"]))
        self.assertEqual([c["source"] for c in preview(data,mode="values")["cells"]],["99","99"])

    def test_broken_shared_groups_reject_the_file(self):
        samples=[
            '<c r="C3"><f t="shared" si="4"/></c>',
            '<c r="C2"><f t="shared" si="4" ref="C2">A1</f></c><c r="C3"><f t="shared" si="4"/></c>',
            '<c r="C2"><f t="shared" si="4" ref="C2:C3">A1</f></c><c r="C3"><f t="shared" si="4" ref="C2:C3">A2</f></c>',
            '<c r="C2"><f t="shared" si="4" ref="A1:B1048576">A1</f></c>']
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaises(EngineError) as failure: preview(fixture('<c r="A1"><v>7</v></c>'+sample))
                self.assertEqual(failure.exception.body["code"],"INVALID_SPREADSHEET")

    def test_ods_inherits_parent_properties_and_child_resets(self):
        styles='''<style:style style:name="Parent" style:family="table-cell"><style:text-properties fo:font-weight="bold" fo:font-style="italic"/><style:table-cell-properties fo:background-color="#12ab34" fo:wrap-option="wrap" fo:border="0.75pt solid #000000"/></style:style>
<style:style style:name="Child" style:family="table-cell" style:parent-style-name="Parent"><style:text-properties fo:font-weight="normal"/><style:table-cell-properties fo:background-color="transparent" fo:wrap-option="no-wrap"/></style:style>'''
        data=ods_fixture('<table:table-row><table:table-cell table:style-name="Child" office:value-type="float" office:value="7"/></table:table-row>',styles)
        result=ods_parse(data)
        self.assertTrue(result["can_import"],result)
        self.assertEqual(result["cells"][0]["style"],{"italic":True,"border":"box"})
        cyclic=styles.replace('style:parent-style-name="Parent"','style:parent-style-name="Child"')
        with self.assertRaises(EngineError) as failure: ods_parse(ods_fixture('<table:table-row><table:table-cell table:style-name="Child" office:value-type="float" office:value="7"/></table:table-row>',cyclic))
        self.assertEqual(failure.exception.body["code"],"INVALID_SPREADSHEET")
        # A parent the file does not define is ignored and reported, as LibreOffice reads it.
        orphan=styles.replace('style:parent-style-name="Parent"','style:parent-style-name="Missing"')
        result=ods_parse(ods_fixture('<table:table-row><table:table-cell table:style-name="Child" office:value-type="float" office:value="7"/></table:table-row>',orphan))
        self.assertTrue(result["can_import"],result)
        self.assertIn(("style.named_style","omitted"),[(e["category"],e["severity"]) for e in result["report_entries"]])

    def test_checked_independent_producer_workbooks_calculate_and_retain_styles(self):
        provenance=json.loads((Path(__file__).parent/"fixtures"/"fidelity-provenance.json").read_text())
        for format,parse in (("xlsx",xlsx_preview),("ods",ods_preview)):
            data=(Path(__file__).parent/"fixtures"/("fidelity."+format)).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(),provenance["sha256"]["fidelity."+format])
            reviewed=parse({"file_base64":base64.b64encode(data).decode(),"scope":"workbook"})
            self.assertTrue(reviewed["can_import"],reviewed)
            with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/"native.dhwb",create=True)) as book:
                book.mutate({"action":"workbook_import","sheets":reviewed["workbook_sheets"],"expected_state":book.state(),"operation_id":"import"})
                cells=book.read("Inputs",{"r2":16,"c2":3})["cells"]
                self.assertEqual([c["value"] for c in cells if c["col"]==1],[7]*16)
                self.assertEqual([c["value"] for c in cells if c["col"]==2],[14]*16)
                summary=book.read("Summary",{})["cells"]
                self.assertEqual(summary[0]["value"],224)
                self.assertEqual(cells[1]["style"],{"format":"fixed2","bold":True,"italic":True,"background":"#12ab34","align":"center","wrap":True,"border":"box"})

    def test_snapshot_does_not_reparse_formulas_without_native_ids(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/"native.dhwb",create=True)) as book:
            book.mutate({"action":"edit","cells":[{"row":1,"col":1,"source":"=SUM(1,2)","kind":"formula"}],"expected_state":book.state(),"operation_id":"seed"})
            with patch("datahorizon.engine.rename_sheet_references",side_effect=AssertionError("No native ID to translate")):
                self.assertEqual(book.read_workbook()["sheets"][0]["cells"][0]["value"],3)
