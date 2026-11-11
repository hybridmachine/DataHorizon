# SPDX-License-Identifier: GPL-3.0-only
import base64
import io
import unittest
import zipfile

from datahorizon.engine import EngineError
from datahorizon.interchange import xlsx_preview, namespaces, TRANSITIONAL, STRICT
from xlsx_builder import workbook_bytes
import xml.etree.ElementTree as ET


def b64(data): return base64.b64encode(data).decode()


def rewrite(data, part, old, new):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for name in source.namelist():
            text = source.read(name)
            if name == part: text = text.decode().replace(old, new).encode()
            target.writestr(name, text)
    return out.getvalue()


SHEET = ('<row r="1"><c r="A1" t="d"><v>2026-11-11</v></c><c r="B1"><f>SUM(A1:A1)</f><v>0</v></c>'
         '<c r="C1" t="s"><v>0</v></c><c r="D1" s="1"><v>5</v></c></row>')
STYLES = {"main": "{ns}", "xml": ('<styleSheet xmlns="{ns}"><fonts count="2"><font/><font><b/></font></fonts>'
          '<fills count="1"><fill/></fills><borders count="1"><border/></borders>'
          '<cellXfs count="2"><xf fontId="0"/><xf fontId="1"/></cellXfs></styleSheet>')}


def package(strict):
    ns = STRICT if strict else TRANSITIONAL
    return workbook_bytes({"S": SHEET, "T": SHEET}, strict=strict, shared_strings=["hello"],
                          styles_xml=STYLES["xml"].format(ns=ns.main))


class StrictNamespaceTests(unittest.TestCase):
    def test_strict_and_transitional_packages_import_identically(self):
        sheet = '<row r="1"><c r="A1" t="d"><v>2026-11-11</v></c><c r="B1"><f>SUM(A1:A1)</f><v>0</v></c></row>'
        a = xlsx_preview({"file_base64": b64(workbook_bytes({"S": sheet}))})
        b = xlsx_preview({"file_base64": b64(workbook_bytes({"S": sheet}, strict=True))})
        self.assertEqual(a["cells"], b["cells"]); self.assertEqual(a["report_entries"], b["report_entries"])

    def test_styles_and_shared_strings_match_at_both_scopes(self):
        for extra in ({}, {"scope": "workbook"}):
            a = xlsx_preview({"file_base64": b64(package(False)), **extra})
            b = xlsx_preview({"file_base64": b64(package(True)), **extra})
            self.assertEqual(a, b)
        single = xlsx_preview({"file_base64": b64(package(True))})
        self.assertIn("hello", str(single["cells"])); self.assertIn("bold", str(single["cells"]))

    def test_builder_strict_output_uses_strict_namespaces_everywhere(self):
        with zipfile.ZipFile(io.BytesIO(package(True))) as archive:
            for name in archive.namelist():
                text = archive.read(name).decode()
                self.assertNotIn("schemas.openxmlformats.org/spreadsheetml", text, name)
                self.assertNotIn("schemas.openxmlformats.org/officeDocument", text, name)

    def test_unknown_root_namespace_is_rejected(self):
        with self.assertRaises(EngineError) as raised:
            namespaces(ET.fromstring('<workbook xmlns="urn:example:other"/>'))
        self.assertEqual(raised.exception.body["code"], "INVALID_SPREADSHEET")
        data = rewrite(package(False), "xl/workbook.xml", TRANSITIONAL.main, "urn:example:other")
        with self.assertRaises(EngineError) as raised: xlsx_preview({"file_base64": b64(data)})
        self.assertEqual(raised.exception.body["code"], "INVALID_SPREADSHEET")

    def test_mixed_variants_are_rejected(self):
        cases = [rewrite(package(True), "xl/worksheets/sheet1.xml", STRICT.main, TRANSITIONAL.main),
                 rewrite(package(True), "xl/styles.xml", STRICT.main, TRANSITIONAL.main),
                 rewrite(package(True), "xl/sharedStrings.xml", STRICT.main, TRANSITIONAL.main),
                 rewrite(package(True), "xl/_rels/workbook.xml.rels", STRICT.rel, TRANSITIONAL.rel),
                 rewrite(package(False), "xl/_rels/workbook.xml.rels", TRANSITIONAL.rel, STRICT.rel)]
        for scope in ({}, {"scope": "workbook"}):
            for data in cases:
                with self.assertRaises(EngineError) as raised: xlsx_preview({"file_base64": b64(data), **scope})
                self.assertEqual(raised.exception.body["code"], "INVALID_SPREADSHEET")


if __name__ == "__main__":
    unittest.main()
