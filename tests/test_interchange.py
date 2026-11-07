"""Independent XML fixtures, managed hooks and explicit fidelity failures."""
import base64
import io
from pathlib import Path
import tempfile
import time
import unittest
import zipfile
import xml.etree.ElementTree as ET

from datahorizon.client import APIError, LocalService
from datahorizon.engine import EngineError
from datahorizon.interchange import P,R,S,xlsx_preview,xlsx_export


def fixture(cells, extra="", shared=None):
    parts={"xl/workbook.xml":f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Inputs" sheetId="7" r:id="custom"/></sheets></workbook>',
           "xl/_rels/workbook.xml.rels":f'<Relationships xmlns="{P}"><Relationship Id="custom" Type="{R}/worksheet" Target="/xl/worksheets/custom.xml"/></Relationships>',
           "xl/worksheets/custom.xml":f'<worksheet xmlns="{S}"><sheetData><row r="1">{cells}</row></sheetData>{extra}</worksheet>'}
    if shared is not None: parts["xl/sharedStrings.xml"]=f'<sst xmlns="{S}">{shared}</sst>'
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
        for name,text in parts.items(): archive.writestr(name,text)
    return output.getvalue()


def preview(data,**kwargs):
    return xlsx_preview({"file_base64":base64.b64encode(data).decode(),**kwargs})


class XlsxTests(unittest.TestCase):
    def terminal(self,client,job):
        deadline=time.monotonic()+3
        while job["status"]=="running" and time.monotonic()<deadline:
            time.sleep(.01); job=client.preparation(job["job_id"])
        self.assertEqual(job["status"],"completed",job)
        client.preparation(job["job_id"],"release")
        return job["result"]

    def test_independent_shared_inline_typed_formulas_and_sparse_offsets(self):
        data=fixture('<c r="A1" t="s"><v>0</v></c><c r="C3" t="inlineStr"><is><t>=1+1</t></is></c>'
                     '<c r="D3" t="b"><v>1</v></c><c r="E3"><v>7</v></c><c r="F3"><f>SUM(E3,$E$3)</f><v>14</v></c>',
                     '<mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>',
                     '<si><r><t>00</t></r><r><t>123</t></r></si>')
        result=preview(data,row=2,col=2)
        self.assertTrue(result["can_import"])
        self.assertEqual([(c["row"],c["col"],c["kind"],c["source"]) for c in result["cells"]],
            [(2,2,"text","00123"),(4,4,"text","=1+1"),(4,5,"boolean","TRUE"),(4,6,"number","7"),(4,7,"formula","=SUM(F4,$E$3)")])
        self.assertIn("mergeCells"," ".join(result["report"]))

    def test_unsupported_or_invalid_cell_blocks_all_inputs_and_cached_values_is_explicit(self):
        bad=('<c r="A1"><f>Other!A1</f><v>8</v></c>', '<c r="A1"><f t="shared" si="1">A2</f><v>8</v></c>',
             '<c r="A1"><f>AVERAGE(A2:A3)</f><v>8</v></c>', '<c r="A1" t="e"><v>#N/A</v></c>',
             '<c r="XFE1"><v>8</v></c>','<c r="A1"><v>9007199254740992</v></c>',
             '<c r="A1" t="s"><v>900</v></c>','<c r="A1"><v>NaN</v></c>')
        for cell in bad:
            with self.subTest(cell=cell):
                result=preview(fixture('<c r="B1"><v>7</v></c>'+cell))
                self.assertFalse(result["can_import"]); self.assertEqual(result["cells"],[]); self.assertTrue(result["issues"])
        result=preview(fixture(bad[0]),mode="values")
        self.assertEqual(result["cells"][0]["source"],"8"); self.assertIn("freshness"," ".join(result["report"]))
        self.assertFalse(preview(fixture('<c r="A1"><f>A2</f></c>'),mode="values")["can_import"])

    def test_roundtrip_preserves_literal_controls_empty_text_numbers_and_formula_sources(self):
        texts=["00123","=1+1","","line\nnext\r\t\0","_x0041_","é & < >"]
        cells=[{"row":1,"col":c,"kind":"text","value":text,"source":text,"calculation":"current","style":{}} for c,text in enumerate(texts,1)]
        cells.extend([{"row":2,"col":1,"kind":"number","value":7,"source":"7","calculation":"current","style":{}},
                      {"row":2,"col":2,"kind":"formula","source":"=A2*2","calculation":"current","value":14,"style":{}}])
        result=xlsx_export({"cells":cells,"state":"snapshot","range":[1,1,2,6]}, {})
        imported=preview(base64.b64decode(result["file_base64"]))
        self.assertTrue(imported["can_import"],imported)
        self.assertEqual([c["source"] for c in imported["cells"][:6]],texts)
        self.assertEqual(imported["cells"][-1]["source"],"=A2*2")
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(result["file_base64"]))) as archive:
            root=ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            self.assertEqual(root.find(f".//{{{S}}}f").text,"A2*2")
            self.assertIn("fullCalcOnLoad",archive.read("xl/workbook.xml").decode())

    def test_export_blocks_dangling_references_and_error_values(self):
        snapshot={"state":"snapshot","range":[1,1,1,1],"cells":[{"row":1,"col":1,"kind":"formula","source":"=A2","value":7,"calculation":"current","style":{}}]}
        with self.assertRaises(EngineError): xlsx_export(snapshot,{})
        result=xlsx_export(snapshot,{"mode":"values"})
        self.assertEqual(preview(base64.b64decode(result["file_base64"]))["cells"][0]["source"],"7")
        snapshot["cells"][0]["calculation"]="error"
        with self.assertRaises(EngineError): xlsx_export(snapshot,{"mode":"values"})

    def test_invalid_packages_entities_duplicates_expansion_and_coordinates_are_bounded(self):
        for data in (b"not zip",):
            with self.assertRaises(EngineError): preview(data)
        for cells in ('<c r="A1"><v>7</v></c><c r="A1"><v>8</v></c>',):
            self.assertFalse(preview(fixture(cells))["can_import"])
        data=fixture('<c r="XFD1048576"><v>7</v></c>')
        with self.assertRaises(EngineError): preview(data,row=2)
        output=io.BytesIO()
        with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive: archive.writestr("bomb",b"x"*(16*1024*1024+1))
        with self.assertRaises(EngineError) as failure: preview(output.getvalue())
        self.assertEqual(failure.exception.body["code"],"LIMIT")
        output=io.BytesIO()
        with zipfile.ZipFile(output,"w") as archive: archive.writestr("xl/workbook.xml",'<!DOCTYPE x [<!ENTITY y "secret">]><x>&y;</x>')
        with self.assertRaises(EngineError): preview(output.getvalue())

    def test_rest_export_import_history_hook_rollback_and_reopen(self):
        handler=Path(__file__).parent/"fixtures"/"validation_hook.py"
        with tempfile.TemporaryDirectory() as temp,LocalService(handler=handler) as client:
            path=Path(temp)/"xlsx.dhwb"
            with client.open(path,create=True) as book:
                book.set(1,1,"0")
                reviewed=self.terminal(client,client.prepare_spreadsheet(fixture('<c r="A1"><v>7</v></c>')))
                book.apply("edit",cells=reviewed["cells"])
                self.assertEqual([c["value"] for c in book.read(r2=1,c2=2)["cells"]],[7,14])
                snapshot=client.request(book.base+"/export/xlsx",{"r2":1,"c2":2})
                self.assertEqual(snapshot["state"],book.state)
                book.apply("undo"); self.assertEqual([c["value"] for c in book.read(r2=1,c2=2)["cells"]],[0,0])
                book.apply("redo")
                bad=self.terminal(client,client.prepare_spreadsheet(fixture('<c r="A1"><v>-1</v></c>')))
                with self.assertRaises(APIError): book.apply("edit",cells=bad["cells"])
                self.assertEqual([c["value"] for c in book.read(r2=1,c2=2)["cells"]],[7,14])
            with client.open(path) as reopened:
                self.assertEqual([c["value"] for c in reopened.read(r2=1,c2=2)["cells"]],[7,14])
