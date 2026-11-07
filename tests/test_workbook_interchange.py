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
                if name=="xl/worksheets/sheet2.xml": value=value.replace(b"<v>1</v>",b"<f>Missing!A1</f>")
                target.writestr(name,value)
        result=xlsx_preview({"file_base64":base64.b64encode(output.getvalue()).decode(),"scope":"workbook"})
        self.assertFalse(result["can_import"]); self.assertEqual(result["workbook_sheets"],[])
        self.assertEqual(result["issues"][0]["sheet"],"Two")

    def test_missing_sheet_reference_blocks_whole_preview_and_values_mode_is_explicit(self):
        snapshot={"state":"s","sheets":[{"name":"One","cells":[{"row":1,"col":1,"kind":"formula","source":"=Missing!A1","value":7,"calculation":"current","style":{}}]}]}
        with self.assertRaises(EngineError): xlsx_export(snapshot,{})
        data=xlsx_export(snapshot,{"mode":"values"})
        result=xlsx_preview({"file_base64":data["file_base64"],"scope":"workbook"})
        self.assertEqual(result["workbook_sheets"][0]["cells"][0]["kind"],"number")
