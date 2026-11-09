# SPDX-License-Identifier: GPL-3.0-only
"""ODF XML fixtures are independent of the exporter; all Apply uses REST."""
import base64
import io
from pathlib import Path
import tempfile
import time
import unittest
import zipfile
import xml.etree.ElementTree as ET

from datahorizon.client import APIError,LocalService
from datahorizon.engine import EngineError
from datahorizon.ods import O,T,X,Y,F,N,OF,MIME,q,ods_preview,ods_export,ods_to_native,native_to_ods


def fixture(rows,styles=""):
    content=f'<office:document-content xmlns:office="{O}" xmlns:table="{T}" xmlns:text="{X}" xmlns:style="{Y}" xmlns:fo="{F}" xmlns:number="{N}" xmlns:of="{OF}"><office:automatic-styles>{styles}</office:automatic-styles><office:body><office:spreadsheet><table:table table:name="Inputs">{rows}</table:table></office:spreadsheet></office:body></office:document-content>'
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w") as archive:
        archive.writestr("mimetype",MIME); archive.writestr("content.xml",content)
    return output.getvalue()


def preview(data,**kwargs): return ods_preview({"file_base64":base64.b64encode(data).decode(),**kwargs})


class OdsTests(unittest.TestCase):
    def test_repetition_sparse_coordinates_text_whitespace_types_and_local_formulas(self):
        data=fixture('<table:table-row table:number-rows-repeated="2"/>'
            '<table:table-row><table:table-cell table:number-columns-repeated="2"/>'
            '<table:table-cell office:value-type="string"><text:p>00<text:span>123</text:span><text:s text:c="2"/>x<text:tab/>y<text:line-break/>z</text:p></table:table-cell>'
            '<table:table-cell office:value-type="float" office:value="7"/>'
            '<table:table-cell office:value-type="boolean" office:boolean-value="true"/>'
            '<table:table-cell table:formula="of:=SUM([.D3];2)" office:value-type="float" office:value="9"/></table:table-row>')
        result=preview(data,row=2,col=2)
        self.assertTrue(result["can_import"],result)
        self.assertEqual([(c["row"],c["col"],c["kind"],c["source"]) for c in result["cells"]],
            [(4,4,"text","00123  x\ty\nz"),(4,5,"number","7"),(4,6,"boolean","TRUE"),(4,7,"formula","=SUM(E4,2)")])
        literal=ods_to_native('of:=IF(TRUE();"[.A1];";[.$B$2])')
        self.assertEqual(literal,'=IF(TRUE,"[.A1];",$B$2)')
        self.assertEqual(native_to_ods('=SUM(A1:A3,IF(TRUE,2,0))',[1,1,3,3]),'of:=SUM([.A1:.A3];IF(TRUE();2.0;0.0))')

    def test_repeated_nonempty_cells_are_bounded_without_materializing_empty_grid(self):
        result=preview(fixture('<table:table-row table:number-rows-repeated="1048576"><table:table-cell table:number-columns-repeated="16384"/></table:table-row>'))
        self.assertFalse(result["can_import"])
        data=fixture('<table:table-row table:number-rows-repeated="10000"><table:table-cell table:number-columns-repeated="2" office:value-type="float" office:value="7"/></table:table-row>')
        with self.assertRaises(EngineError) as failure: preview(data)
        self.assertEqual(failure.exception.body["code"],"LIMIT")

    def test_repeated_long_text_preflights_bytes_and_unknown_types_never_become_blank(self):
        import tracemalloc
        data=fixture('<table:table-row table:number-rows-repeated="10000"><table:table-cell office:value-type="string" office:string-value="'+"x"*60000+'"/></table:table-row>')
        tracemalloc.start()
        try:
            with self.assertRaises(EngineError) as failure: preview(data)
            self.assertEqual(failure.exception.body["code"],"READ_LIMIT")
            self.assertLess(tracemalloc.get_traced_memory()[1],12*1024*1024)
        finally: tracemalloc.stop()
        result=preview(fixture('<table:table-row><table:table-cell office:value-type="unknown"/></table:table-row>'))
        self.assertFalse(result["can_import"]); self.assertEqual(result["cells"],[])
        self.assertIn("Unsupported ODS value type",result["issues"][0]["message"])

    def test_unsupported_formulas_and_error_or_missing_caches_block_complete_payload(self):
        for source in ('of:=[Other.A1]','of:=2^3^2','of:=MEDIAN([.A1])','oooc:=1+1'):
            data=fixture(f'<table:table-row><table:table-cell table:formula="{source}" office:value-type="float" office:value="8"/></table:table-row>')
            result=preview(data); self.assertFalse(result["can_import"]); self.assertEqual(result["cells"],[])
            self.assertEqual(preview(data,mode="values")["cells"][0]["source"],"8")
        for attrs in ('','office:value-type="string" office:string-value="#DIV/0!"'):
            self.assertFalse(preview(fixture(f'<table:table-row><table:table-cell table:formula="of:=1/0" {attrs}/></table:table-row>'),mode="values")["can_import"])

    def test_dates_use_native_types_and_native_styles_roundtrip(self):
        style={"format":"fixed2","bold":True,"italic":True,"align":"center","wrap":True,"background":"#12ab34","border":"box"}
        cells=[{"row":3,"col":2,"kind":"number","source":"7","value":7,"calculation":"current","style":style},
               {"row":3,"col":3,"kind":"formula","source":"=B3*2","value":14,"calculation":"current","style":{}},
               {"row":4,"col":2,"kind":"text","source":"  00123\tline\nnext\r","value":"  00123\tline\nnext\r","calculation":"current","style":{"format":"text"}}]
        exported=ods_export({"state":"s","cells":cells,"range":[3,2,4,3]}, {})
        parsed=preview(base64.b64decode(exported["file_base64"]))
        self.assertTrue(parsed["can_import"],parsed)
        self.assertEqual(parsed["cells"][0]["style"],style)
        self.assertEqual(parsed["cells"][1]["source"],"=(B3*2.0)")
        self.assertEqual(parsed["cells"][2]["source"],cells[2]["source"])
        dates=preview(fixture('<table:table-row><table:table-cell office:value-type="date" office:date-value="2026-11-06"/><table:table-cell office:value-type="time" office:time-value="PT2H"/></table:table-row>'))
        self.assertEqual([(c["source"],c["kind"]) for c in dates["cells"]],[("2026-11-06","date"),("02:00:00","time")])
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["file_base64"]))) as archive:
            self.assertEqual(archive.infolist()[0].filename,"mimetype"); self.assertEqual(archive.infolist()[0].compress_type,zipfile.ZIP_STORED)
            self.assertIn("META-INF/manifest.xml",archive.namelist())

    def test_paragraph_whitespace_collapses_without_annotations_or_ruby_readings(self):
        data=fixture('<table:table-row><table:table-cell office:value-type="string"><text:p>  A <text:span> \n B</text:span><office:annotation><text:p>comment</text:p></office:annotation><text:ruby><text:ruby-base>字</text:ruby-base><text:ruby-text>reading</text:ruby-text></text:ruby><text:s text:c="2"/> X  </text:p></table:table-cell></table:table-row>')
        self.assertEqual(preview(data)["cells"][0]["source"],"A B字   X")
        repeated='<text:s text:c="65536"/>'*2
        data=fixture(f'<table:table-row><table:table-cell office:value-type="string"><text:p>{repeated}</text:p></table:table-cell></table:table-row>')
        with self.assertRaises(EngineError): preview(data)

    def test_ods_cannot_export_native_xml_controls_or_bad_values(self):
        cell={"row":1,"col":1,"kind":"text","value":"a\0b","calculation":"current","style":{}}
        with self.assertRaises(EngineError): ods_export({"state":"s","cells":[cell],"range":[1,1,1,1]}, {})

    def test_rest_sdk_hook_rollback_and_snapshot_export(self):
        handler=Path(__file__).parent/"fixtures"/"validation_hook.py"
        with tempfile.TemporaryDirectory() as temp,LocalService(handler=handler) as client,client.open(Path(temp)/"ods.dhwb",create=True) as book:
            def prepare(value):
                data=fixture(f'<table:table-row><table:table-cell office:value-type="float" office:value="{value}"/></table:table-row>')
                job=client.prepare_spreadsheet(data,format="ods"); deadline=time.monotonic()+3
                while job["status"]=="running" and time.monotonic()<deadline: time.sleep(.01); job=client.preparation(job["job_id"])
                self.assertEqual(job["status"],"completed",job); client.preparation(job["job_id"],"release")
                return job["result"]
            book.import_prepared(prepare(7))
            exported=book.export_spreadsheet("ods",r2=1,c2=2)
            self.assertEqual(exported["state"],book.state)
            self.assertEqual([c["source"] for c in preview(base64.b64decode(exported["file_base64"]))["cells"]],["7.0","14.0"])
            with self.assertRaises(APIError): book.import_prepared(prepare(-1))
            self.assertEqual([c["value"] for c in book.read(r2=1,c2=2)["cells"]],[7,14])
