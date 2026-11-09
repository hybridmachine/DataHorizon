# SPDX-License-Identifier: GPL-3.0-only
"""CSV byte decoding, dialect review and managed import contracts."""
import base64
from pathlib import Path
import tempfile
import unittest

from datahorizon.client import APIError, LocalService
from datahorizon.csv_import import preview_csv
from datahorizon.engine import EngineError


class CsvImportTests(unittest.TestCase):
    def edits(self,preview):
        self.assertTrue(preview["can_import"])
        return [{"row":r,"col":c,**item} for r,row in enumerate(preview["inputs"],1) for c,item in enumerate(row,1)]

    def byte_request(self, data, encoding="utf-8-sig", delimiter=","):
        return {"csv_base64":base64.b64encode(data).decode("ascii"),"encoding":encoding,"delimiter":delimiter}

    def test_encoding_dialects_bom_and_quotes_preserve_exact_text(self):
        big_endian=preview_csv(self.byte_request(b"\xfe\xff"+'café;00123'.encode("utf-16-be"),"utf-16",";"))
        self.assertEqual(big_endian["rows"],[["café","00123"]])
        for encoding in ("utf-8-sig","utf-8","utf-16","cp1252","latin-1"):
            for delimiter in (",",";","\t","|"):
                with self.subTest(encoding=encoding,delimiter=delimiter):
                    text=f'ID{delimiter}Name\r\n00123{delimiter}"café{delimiter}two\nlines"\r\n=1+1{delimiter}TRUE'
                    result=preview_csv(self.byte_request(text.encode(encoding),encoding,delimiter))
                    self.assertEqual(result["rows"],[["ID","Name"],["00123",f"café{delimiter}two\nlines"],["=1+1","TRUE"]])
                    self.assertEqual(result["default_kind"],"text")
                    self.assertEqual(result["encoding"],encoding)

    def test_invalid_bytes_never_get_replacement_characters_or_a_preview(self):
        for data,encoding in ((b"\xff","utf-8"),(b"\x81","cp1252"),(b"\xff\xfeA","utf-16"),
                              (b"A\0","utf-16"),(b"\0A","utf-16")):
            with self.subTest(encoding=encoding),self.assertRaises(EngineError) as failure:
                preview_csv(self.byte_request(data,encoding))
            self.assertEqual(failure.exception.body["code"],"INVALID_CSV_ENCODING")

    def test_invalid_options_and_malformed_csv_are_structured_errors(self):
        invalid=({},{"csv":"a","csv_base64":"YQ=="},{"csv":"a","encoding":"utf-8"},
                 {"csv_base64":"@@@"},{"csv_base64":"é"},self.byte_request(b"a","unknown"),
                 {"csv":7},{"csv":"a","delimiter":""},{"csv":"a","delimiter":"\n"},
                 {"csv":"a","delimiter":"\0"},{"csv":"a","delimiter":'"'},
                 {"csv":"a","delimiter":True},{"csv":'"unterminated'})
        for request in invalid:
            with self.subTest(request=request),self.assertRaises(EngineError) as failure:
                preview_csv(request)
            self.assertEqual(failure.exception.body["code"],"INVALID_CSV")

    def test_source_cell_record_and_complete_response_bounds(self):
        for request,code in (({"csv":"x"*(3*1024*1024+1)},"LIMIT"),
                             ({"csv":"é"*32769},"INVALID_INPUT"),
                             ({"csv":"a,"*10000},"LIMIT"),
                             ({"csv":"\n"*10001},"LIMIT"),
                             ({"csv":",".join(["\0"*300]*8000)},"READ_LIMIT")):
            with self.subTest(code=code),self.assertRaises(EngineError) as failure:
                preview_csv(request)
            self.assertEqual(failure.exception.body["code"],code)

    def test_rest_preview_import_preserves_ids_and_formula_like_text_and_rejects_bad_bytes(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/"csv.dhwb",create=True) as book:
            preview=client.request("/v1/import/csv/preview",self.byte_request('00123;café;=1+1'.encode("cp1252"),"cp1252",";"))
            book.apply("edit",cells=[{"row":1,"col":col,"source":value,"kind":"text"}
                                     for col,value in enumerate(preview["rows"][0],1)])
            cells=book.read(r2=1,c2=3)["cells"]
            self.assertEqual([cell["value"] for cell in cells],["00123","café","=1+1"])
            old_state=book.state
            with self.assertRaises(APIError) as failure:
                client.request("/v1/import/csv/preview",self.byte_request(b"\xff"))
            self.assertEqual(failure.exception.body["code"],"INVALID_CSV_ENCODING")
            book.refresh(); self.assertEqual(book.state,old_state)
            schema=client.request("/openapi.json")["components"]["schemas"]["CsvPreviewRequest"]
            self.assertIn("cp1252",schema["properties"]["encoding"]["enum"])

    def test_explicit_types_headers_styles_history_and_reopen_use_one_managed_edit(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/"typed.dhwb"
            with client.open(path,create=True) as book:
                book.apply("edit",cells=[{"row":2,"col":2,"source":"99","kind":"number","style":{"format":"text","bold":True}}])
                preview=client.request("/v1/import/csv/preview",{
                    "csv":"ID,Qty,Active,When,Formula\n00123,2,TrUe,2026-11-05,=1+1",
                    "column_types":{"2":"number","3":"boolean","4":"date_iso"},"header":True})
                self.assertEqual(preview["conversion_errors"],0)
                self.assertEqual(preview["converted_cells"],3)
                self.assertEqual([item["kind"] for item in preview["inputs"][0]],["text"]*5)
                self.assertEqual([item["kind"] for item in preview["inputs"][1]],["text","number","boolean","date","text"])
                book.apply("edit",label="Import CSV",cells=self.edits(preview))
                cells=book.read(r1=2,r2=2,c2=5)["cells"]
                self.assertEqual([cell["value"] for cell in cells],["00123",2,True,"2026-11-05","=1+1"])
                self.assertEqual(cells[1]["style"],{"format":"text","bold":True})
                book.apply("undo")
                self.assertEqual(book.read(r1=2,r2=2,c2=2)["cells"][0]["value"],99)
                book.apply("redo")
            with client.open(path) as reopened:
                self.assertEqual([cell["value"] for cell in reopened.read(r1=2,r2=2,c2=5)["cells"]],
                                 ["00123",2,True,"2026-11-05","=1+1"])

    def test_slash_date_order_is_explicit_and_ambiguity_is_reported_as_native_date(self):
        for kind,expected in (("date_mdy","2026-03-04"),("date_dmy","2026-04-03")):
            with self.subTest(kind=kind):
                preview=preview_csv({"csv":"03/04/2026","column_types":{"1":kind}})
                self.assertEqual(preview["inputs"],[[{"source":expected,"kind":"date"}]])
                self.assertEqual(preview["ambiguous_dates"],1)
                self.assertEqual(preview["date_examples"][0]["result"],expected)
        raw=preview_csv({"csv":"03/04/2026"})
        self.assertEqual(raw["rows"],[["03/04/2026"]]); self.assertNotIn("inputs",raw)

    def test_invalid_conversions_block_the_whole_payload_and_report_locations(self):
        cases=(("number","1e9999","INVALID_INPUT"),("number","9007199254740992","PRECISION"),
               ("number","1,234","INVALID_INPUT"),("number"," 2","INVALID_INPUT"),
               ("number","=1+1","INVALID_INPUT"),("boolean","1","INVALID_INPUT"),
               ("boolean","yes","INVALID_INPUT"),("date_iso","2026-02-29","INVALID_DATE"),
               ("date_iso","20261105","INVALID_DATE"),("date_mdy","13/4/2026","INVALID_DATE"),
               ("date_dmy","4/13/2026","INVALID_DATE"))
        for kind,value,code in cases:
            with self.subTest(kind=kind,value=value):
                result=preview_csv({"csv":f'Header\n"{value}"',"column_types":{"1":kind},"header":True})
                self.assertFalse(result["can_import"]); self.assertNotIn("inputs",result)
                self.assertEqual(result["conversion_errors"],1)
                self.assertEqual((result["conversion_issues"][0]["row"],result["conversion_issues"][0]["col"]),(2,1))
                self.assertEqual(result["conversion_issues"][0]["code"],code)

    def test_empty_converted_fields_are_blank_and_ragged_records_are_not_padded(self):
        result=preview_csv({"csv":"ID,Qty,Enabled\n001,,\n002,3\n\n003,,FALSE",
                            "column_types":{"2":"number","3":"boolean"},"header":True})
        self.assertTrue(result["can_import"])
        self.assertEqual(result["inputs"][1],[{"source":"001","kind":"text"},{"source":"","kind":"auto"},{"source":"","kind":"auto"}])
        self.assertEqual(len(result["inputs"][2]),2)
        self.assertEqual(result["inputs"][3],[])
        self.assertEqual(result["converted_cells"],2)
        self.assertFalse(preview_csv({"csv":"\n\n"})["can_import"])

    def test_type_options_are_strict_and_conversion_reports_and_payload_are_bounded(self):
        for types in (None,[],{"0":"number"},{"01":"number"},{"2":"number"},{"1":"auto"},{"1":True}):
            with self.subTest(types=types),self.assertRaises(EngineError) as failure:
                preview_csv({"csv":"7","column_types":types})
            self.assertEqual(failure.exception.body["code"],"INVALID_CSV_TYPES")
        with self.assertRaises(EngineError): preview_csv({"csv":"7","header":"true"})
        errors=preview_csv({"csv":"bad\n"*101,"column_types":{"1":"number"}})
        self.assertEqual(errors["conversion_errors"],101); self.assertEqual(len(errors["conversion_issues"]),100)
        dates=preview_csv({"csv":"3/4/2026\n"*101,"column_types":{"1":"date_mdy"}})
        self.assertEqual(dates["ambiguous_dates"],101); self.assertEqual(len(dates["date_examples"]),20)
        with self.assertRaises(EngineError) as failure:
            preview_csv({"csv":("x"*60000+"\n")*40,"column_types":{"1":"text"}})
        self.assertEqual(failure.exception.body["code"],"READ_LIMIT")

    def test_typed_import_runs_required_hooks_and_rolls_back_rejected_batch(self):
        handler=Path(__file__).parent/"fixtures"/"validation_hook.py"
        with tempfile.TemporaryDirectory() as temp,LocalService(handler=handler) as client,client.open(Path(temp)/"hook-csv.dhwb",create=True) as book:
            preview=client.request("/v1/import/csv/preview",{"csv":"7","column_types":{"1":"number"}})
            book.apply("edit",cells=self.edits(preview))
            self.assertEqual([cell["value"] for cell in book.read(r2=1,c2=2)["cells"]],[7,14])
            rejected=client.request("/v1/import/csv/preview",{"csv":"-1","column_types":{"1":"number"}})
            with self.assertRaises(APIError) as failure:
                book.apply("edit",cells=self.edits(rejected))
            self.assertEqual(failure.exception.body["outcome"],"rolled_back")
            self.assertEqual([cell["value"] for cell in book.read(r2=1,c2=2)["cells"]],[7,14])
