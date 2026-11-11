# SPDX-License-Identifier: GPL-3.0-only
"""Whole-workbook contracts across real REST, SQLite snapshots and package consumers."""
import base64
from contextlib import closing
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from datahorizon.client import APIError, LocalService
from datahorizon.engine import Workbook, EngineError
from datahorizon.interchange import xlsx_export, xlsx_preview
from xlsx_builder import workbook_bytes


def b64(data):
    return base64.b64encode(data).decode()


def sheets():
    return [{"name":"O'Brien Sales","cells":[{"row":1,"col":1,"source":"7","kind":"number","style":{"bold":True}}]},
            {"name":"Summary","cells":[{"row":3,"col":2,"source":"='O''Brien Sales'!$A$1*2","kind":"formula"}]},
            {"name":"Empty","cells":[]}]


def prepare(client,data,format="xlsx"):
    job=client.prepare_spreadsheet(data,format=format,scope="workbook")
    deadline=time.monotonic()+5
    while job["status"]=="running" and time.monotonic()<deadline:
        time.sleep(.01); job=client.preparation(job["job_id"])
    client.preparation(job["job_id"],"release")
    if job["status"]!="completed": raise AssertionError(job)
    return job["result"]


class WorkbookInterchangeTests(unittest.TestCase):
    def test_xlsx_rest_roundtrip_order_empty_sheets_formulas_styles_and_reopen(self):
        with tempfile.TemporaryDirectory() as temp, LocalService() as client:
            path=Path(temp)/"source.dhwb"
            with client.open(path,create=True) as book:
                book.apply("workbook_import",sheets=sheets())
                book.refresh()
                # Stable UUID references must become portable names in an external file.
                identity=book.info["sheets"][0]["id"]
                book.apply("edit","Summary",cells=[{"row":4,"col":1,"source":f"='{identity}'!A1","kind":"formula"}])
                out=book.export_spreadsheet(scope="workbook")
                self.assertEqual(out["state"],book.state)
            data=base64.b64decode(out["file_base64"])
            preview=prepare(client,data)
            self.assertTrue(preview["can_import"],preview)
            self.assertEqual([s["name"] for s in preview["workbook_sheets"]],[s["name"] for s in sheets()])
            self.assertEqual(preview["workbook_sheets"][0]["cells"][0]["style"],{"bold":True})
            with client.open(Path(temp)/"destination.dhwb",create=True) as target:
                receipt=target.import_prepared(preview)
                self.assertEqual(receipt["outcome"],"committed")
                self.assertEqual([c["value"] for c in target.read("Summary",r2=4,c2=2)["cells"]],[14,7])
                target.refresh()
                self.assertFalse(target.info["undo"])
                with self.assertRaises(APIError) as rejected: target.import_prepared(preview)
                self.assertEqual(rejected.exception.body["code"],"DESTINATION_NOT_EMPTY")
            with client.open(path) as reopened:
                self.assertEqual(reopened.read("Summary",r1=3,c1=2,r2=3,c2=2)["cells"][0]["value"],14)
            try: import openpyxl
            except ImportError: return  # Separate optional producer/consumer run records coverage.
            external=openpyxl.load_workbook(io.BytesIO(data))
            self.assertEqual(external.sheetnames,[s["name"] for s in sheets()])
            self.assertEqual(external["Summary"]["B3"].value,"='O''Brien Sales'!$A$1*2")

    def test_workbook_import_rolls_back_catalog_cells_and_receipt_on_late_failure(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/"test.dhwb",create=True)) as book:
            before=book.info(); put=book.put
            def fail(*args):
                put(*args)
                if args[1]==3: raise EngineError("INJECTED","late failure")
            with patch.object(book,"put",side_effect=fail),self.assertRaises(EngineError) as failure:
                book.mutate({"action":"workbook_import","sheets":sheets(),"operation_id":"failed","expected_state":book.state()})
            self.assertEqual(failure.exception.body["outcome"],"rolled_back")
            self.assertEqual(book.info()["sheets"],before["sheets"])
            self.assertEqual(book.read("Sheet1",{})["cells"],[])
            self.assertEqual(book.operation("failed")["outcome"],"rolled_back")

    def test_workbook_import_rejects_required_handlers_duplicate_names_and_aggregate_limit(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/"test.dhwb",create=True)) as book:
            for payload in ([{"name":"A","cells":[]},{"name":"a","cells":[]}],
                            [{"name":"A","cells":[{}]*6000},{"name":"B","cells":[{}]*6000}]):
                with self.assertRaises(EngineError):
                    book.mutate({"action":"workbook_import","sheets":payload,"operation_id":str(len(payload[0]["cells"])),"expected_state":book.state()})
            book.handler="required.py"
            with self.assertRaises(EngineError) as failure:
                book.mutate({"action":"workbook_import","sheets":sheets(),"operation_id":"handler","expected_state":book.state()})
            self.assertEqual(failure.exception.body["code"],"UNSUPPORTED_HOOK_SCOPE")
            self.assertEqual(len(book.info()["sheets"]),1)

    def test_whole_preview_aggregate_limit_cancellation_and_invalid_late_sheet(self):
        from datahorizon.interchange import S
        cell={"kind":"number","source":"1","value":1,"calculation":"current","style":{},"col":1}
        snapshot={"state":"s","sheets":[{"name":name,"cells":[{**cell,"row":r} for r in range(1,6001)]} for name in ("One","Two")]}
        payload=xlsx_export(snapshot,{})
        with self.assertRaises(EngineError) as failure: xlsx_preview({**payload,"scope":"workbook"})
        self.assertEqual(failure.exception.body["code"],"LIMIT")
        snapshot["sheets"]=[{"name":"One","cells":[{**cell,"row":1}]},{"name":"Two","cells":[{**cell,"row":1}]}]
        data=base64.b64decode(xlsx_export(snapshot,{})["file_base64"])
        def cancel(count,phase): raise EngineError("CANCELLED","test")
        with self.assertRaises(EngineError) as failure: xlsx_preview({"file_base64":base64.b64encode(data).decode(),"scope":"workbook"},cancel)
        self.assertEqual(failure.exception.body["code"],"CANCELLED")
        output=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source,zipfile.ZipFile(output,"w") as target:
            for name in source.namelist():
                value=source.read(name)
                if name=="xl/worksheets/sheet2.xml": value=value.replace(b"<v>1</v>",b'<f t="shared" si="9"/>')
                target.writestr(name,value)
        with self.assertRaises(EngineError) as failure: xlsx_preview({"file_base64":base64.b64encode(output.getvalue()).decode(),"scope":"workbook"})
        self.assertEqual(failure.exception.body["code"],"INVALID_SPREADSHEET")
        # A reference to a sheet the workbook lacks is kept as an unevaluable formula, not a blocking issue.
        output=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source,zipfile.ZipFile(output,"w") as target:
            for name in source.namelist():
                value=source.read(name)
                if name=="xl/worksheets/sheet2.xml": value=value.replace(b"<v>1</v>",b"<f>Missing!A1</f>")
                target.writestr(name,value)
        result=xlsx_preview({"file_base64":base64.b64encode(output.getvalue()).decode(),"scope":"workbook"})
        self.assertTrue(result["can_import"],result)
        self.assertIn("1 cell: Missing (unevaluable); first at Two!A1"," ".join(result["report"]))

    def test_missing_sheet_reference_is_itemized_and_values_mode_is_explicit(self):
        snapshot={"state":"s","sheets":[{"name":"One","cells":[{"row":1,"col":1,"kind":"formula","source":"=Missing!A1","value":7,"calculation":"current","style":{}}]}]}
        with self.assertRaises(EngineError): xlsx_export(snapshot,{})
        data=xlsx_export(snapshot,{"mode":"values"})
        result=xlsx_preview({"file_base64":data["file_base64"],"scope":"workbook"})
        self.assertEqual(result["workbook_sheets"][0]["cells"][0]["kind"],"number")
        # A package whose formula names a missing sheet keeps the formula and itemizes it.
        data=workbook_bytes({"One":'<row r="1"><c r="A1"><f>Missing!A1</f><v>7</v></c></row>'})
        result=xlsx_preview({"file_base64":base64.b64encode(data).decode(),"scope":"workbook"})
        self.assertTrue(result["can_import"])
        self.assertEqual(result["workbook_sheets"][0]["cells"][0]["source"],"=Missing!A1")
        found=[e for e in result["report_entries"] if e["category"]=="formula.missing_sheet"]
        self.assertEqual(len(found),1); self.assertEqual(found[0]["samples"],[{"sheet":"One","cell":"A1"}])

    def test_sheet_where_every_cell_degrades_is_still_created(self):
        data=workbook_bytes({"Bad":'<row r="1"><c r="A1" t="zz"><v>1</v></c></row>',"Empty":""})
        result=xlsx_preview({"file_base64":b64(data),"scope":"workbook","mode":"mixed"})
        self.assertTrue(result["can_import"])
        self.assertEqual([(s["name"],s["cells"]) for s in result["workbook_sheets"]],[("Bad",[]),("Empty",[])])
        self.assertEqual(result["totals"]["omitted"],1)
        self.assertEqual(result["report_entries"][0]["samples"],[{"sheet":"Bad","cell":"A1"}])
        self.assertEqual(sum(1 for line in result["report"] if "omitted" in line and "Bad!A1" in line),1)
        with tempfile.TemporaryDirectory() as temp, LocalService() as client:
            preview=prepare(client,data)
            self.assertTrue(preview["can_import"])
            with client.open(Path(temp)/"d.dhwb",create=True) as book:
                book.import_prepared(preview)
            with client.open(Path(temp)/"d.dhwb") as reopened:
                self.assertEqual([s["name"] for s in reopened.info["sheets"]],["Bad","Empty"])

    def test_reports_merge_across_sheets_with_sheet_in_samples(self):
        bad='<row r="1"><c r="A1" t="zz"><v>1</v></c></row>'
        result=xlsx_preview({"file_base64":b64(workbook_bytes({"One":bad,"Two":bad})),"scope":"workbook"})
        (found,)=result["report_entries"]
        self.assertEqual(found["count"],2)
        self.assertEqual([x["sheet"] for x in found["samples"]],["One","Two"])
        self.assertEqual(result["totals"]["omitted"],2)

    def test_ods_sheet_where_every_cell_degrades_is_still_created(self):
        from ods_builder import workbook_bytes as ods_bytes
        from datahorizon.ods import ods_preview
        data=ods_bytes({"Bad":'<table:table-row><table:table-cell office:value-type="unknown"/></table:table-row>',"Empty":""})
        result=ods_preview({"file_base64":b64(data),"scope":"workbook","mode":"mixed"})
        self.assertTrue(result["can_import"])
        self.assertEqual([(s["name"],s["cells"]) for s in result["workbook_sheets"]],[("Bad",[]),("Empty",[])])
        self.assertEqual(result["totals"]["omitted"],1)
        with tempfile.TemporaryDirectory() as temp, LocalService() as client:
            preview=prepare(client,data,"ods")
            with client.open(Path(temp)/"d.dhwb",create=True) as book:
                book.import_prepared(preview)
            with client.open(Path(temp)/"d.dhwb") as reopened:
                self.assertEqual([s["name"] for s in reopened.info["sheets"]],["Bad","Empty"])


class OdsWorkbookTests(unittest.TestCase):
    def test_named_cross_sheet_references_ranges_and_quoted_literals_translate_safely(self):
        from datahorizon.ods import ods_to_native,native_to_ods
        names=["O'Brien Sales",'Semi;"Quote','Summary']
        source='=SUM(\'O\'\'Brien Sales\'!$A$1:A2,IF(TRUE,\'Semi;"Quote\'!A1,"[.A1];"))'
        external=native_to_ods(source,sheet_names=names)
        native=ods_to_native(external,names)
        self.assertIn("'O''Brien Sales'!$A$1:'O''Brien Sales'!A2",native)
        self.assertIn("'Semi;\"Quote'!A1",native)
        self.assertIn('"[.A1];"',native)
        for bad in ('of:=[Missing.A1]','of:=[One.A1:Two.A2]','of:=[file:///tmp/source#One.A1]'):
            with self.assertRaises(EngineError): ods_to_native(bad,["One","Two"])

    def test_independent_ods_workbook_rest_export_roundtrip_and_odfpy_consumer(self):
        from datahorizon.ods import O,T,X,Y,F,N,OF,MIME,q
        from xml.sax.saxutils import escape
        content=f'''<office:document-content xmlns:office="{O}" xmlns:table="{T}" xmlns:text="{X}" xmlns:style="{Y}" xmlns:fo="{F}" xmlns:number="{N}" xmlns:of="{OF}"><office:body><office:spreadsheet>
<table:table table:name="O'Brien Sales"><table:table-row><table:table-cell office:value-type="float" office:value="7"/></table:table-row></table:table>
<table:table table:name="Summary"><table:table-row><table:table-cell table:formula="of:=SUM([$'O''Brien Sales'.$A$1:$'O''Brien Sales'.$A$2])" office:value-type="float" office:value="7"/></table:table-row></table:table>
<table:table table:name="Empty"/>
</office:spreadsheet></office:body></office:document-content>'''
        output=io.BytesIO()
        with zipfile.ZipFile(output,"w") as archive:
            archive.writestr("mimetype",MIME); archive.writestr("content.xml",content)
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            preview=prepare(client,output.getvalue(),"ods")
            self.assertTrue(preview["can_import"],preview)
            with client.open(Path(temp)/"ods.dhwb",create=True) as book:
                book.import_prepared(preview)
                self.assertEqual(book.read("Summary",r2=1,c2=1)["cells"][0]["value"],7)
                result=book.export_spreadsheet("ods",scope="workbook")
                self.assertEqual(result["state"],book.state)
            data=base64.b64decode(result["file_base64"])
            again=prepare(client,data,"ods")
            self.assertEqual([s["name"] for s in again["workbook_sheets"]],["O'Brien Sales","Summary","Empty"])
            with client.open(Path(temp)/"copy.dhwb",create=True) as book:
                book.import_prepared(again)
                self.assertEqual(book.read("Summary",r2=1,c2=1)["cells"][0]["value"],7)
            try:
                from odf.opendocument import load
                from odf.table import Table,TableCell
            except ImportError: return
            foreign=load(io.BytesIO(data))
            self.assertEqual([s.getAttribute("name") for s in foreign.spreadsheet.getElementsByType(Table)],["O'Brien Sales","Summary","Empty"])
            self.assertIn("O''Brien Sales",foreign.spreadsheet.getElementsByType(TableCell)[1].getAttribute("formula"))
