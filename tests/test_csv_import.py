"""CSV byte decoding, dialect review and managed import contracts."""
import base64
from pathlib import Path
import tempfile
import unittest

from datahorizon.client import APIError, LocalService
from datahorizon.csv_import import preview_csv
from datahorizon.engine import EngineError


class CsvImportTests(unittest.TestCase):
    def byte_request(self, data, encoding="utf-8-sig", delimiter=","):
        return {"csv_base64":base64.b64encode(data).decode("ascii"),"encoding":encoding,"delimiter":delimiter}

    def test_encoding_dialects_bom_and_quotes_preserve_exact_text(self):
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
