"""Native-style fidelity, explicit display loss and style replacement semantics."""
import base64
import io
import tempfile
from pathlib import Path
import unittest
import zipfile

from datahorizon.client import LocalService
from datahorizon.interchange import S,xlsx_export,xlsx_preview
from test_interchange import fixture


def styled_fixture(cells,styles):
    original=fixture(cells)
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source,zipfile.ZipFile(output,"w") as target:
        for name in source.namelist(): target.writestr(name,source.read(name))
        target.writestr("xl/styles.xml",styles)
    return output.getvalue()


def preview(data): return xlsx_preview({"file_base64":base64.b64encode(data).decode()})


class XlsxStyleTests(unittest.TestCase):
    def test_native_style_roundtrip_including_styled_blanks_and_explicit_number_with_text_format(self):
        style={"format":"fixed2","bold":True,"italic":True,"wrap":True,"align":"center","background":"#12ab34","border":"box"}
        cells=[{"row":1,"col":1,"kind":"number","source":"7","value":7,"calculation":"current","style":style},
               {"row":1,"col":2,"kind":"number","source":"8","value":8,"calculation":"current","style":{"format":"text"}},
               {"row":2,"col":1,"kind":"blank","source":"","value":None,"calculation":"current","style":{"background":"#abcdef"}}]
        result=xlsx_export({"cells":cells,"state":"s","range":[1,1,2,2]}, {})
        reviewed=preview(base64.b64decode(result["file_base64"]))
        self.assertTrue(reviewed["can_import"],reviewed)
        self.assertEqual([cell["style"] for cell in reviewed["cells"]],[style,{"format":"text"},{"background":"#abcdef"}])
        self.assertEqual(reviewed["cells"][1]["kind"],"number")
        self.assertEqual(reviewed["cells"][2]["source"],"")
        self.assertTrue(all(cell["replace_style"] for cell in reviewed["cells"]))

    def test_unsupported_format_theme_fill_and_partial_border_are_reported_and_invalid_indices_block(self):
        styles=f'''<styleSheet xmlns="{S}"><fonts><font/></fonts><fills><fill><patternFill patternType="solid"><fgColor theme="5"/></patternFill></fill></fills>
        <borders><border><bottom style="double"/></border></borders><cellXfs><xf numFmtId="14" fontId="0" fillId="0" borderId="0"><alignment horizontal="justify" textRotation="90"/></xf></cellXfs></styleSheet>'''
        result=preview(styled_fixture('<c r="A1"><v>45200</v></c>',styles))
        self.assertTrue(result["can_import"])
        self.assertEqual(result["cells"][0]["style"],{})
        report=" ".join(result["report"])
        for word in ("serials","theme","Partial","alignment","rotation"): self.assertIn(word,report)
        invalid=preview(styled_fixture('<c r="A1" s="99"><v>7</v></c>',styles))
        self.assertFalse(invalid["can_import"]); self.assertEqual(invalid["cells"],[])

    def test_import_replaces_default_style_preserves_input_and_undo_restores_prior_style(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/"style.dhwb",create=True) as book:
            book.apply("edit",cells=[{"row":1,"col":1,"source":"old","style":{"bold":True,"background":"#ff0000"}}])
            reviewed=preview(fixture('<c r="A1" t="inlineStr"><is><t>00123</t></is></c>'))
            book.import_prepared(reviewed)
            cell=book.read(r2=1,c2=1)["cells"][0]
            self.assertEqual(cell["style"],{}); self.assertEqual(cell["value"],"00123")
            book.apply("undo")
            self.assertEqual(book.read(r2=1,c2=1)["cells"][0]["style"],{"bold":True,"background":"#ff0000"})
