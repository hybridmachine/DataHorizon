# SPDX-License-Identifier: GPL-3.0-only
"""Whole-workbook export writes every stored formula: XLSX verbatim (counted when DataHorizon cannot evaluate it),
ODS as OpenFormula when it translates and otherwise as the cell's current value or an empty cell (reported)."""
import base64
from pathlib import Path
import tempfile
import threading
import unittest

from datahorizon.client import APIError, Client
from datahorizon.interchange import EXPORT_UNEVALUABLE_DETAIL
from datahorizon.ods import UNTRANSLATED_FORMULA, UNTRANSLATED_DETAIL_EMPTY, UNTRANSLATED_DETAIL_VALUE
from datahorizon.service import Service

TESTS = Path(__file__).resolve().parent
CORPUS = TESTS/"fixtures"/"corpus"
FIXTURES = sorted(CORPUS.glob("*.xlsx"))+[TESTS/"fixtures"/name for name in ("fidelity.xlsx", "inputs.xlsx", "markup.xlsx", "unsupported.xlsx")]
NAMED = ("defined-names.xlsx", "excel-table.xlsx", "external-3d-references.xlsx")


def formula_sources(engine):
    """{(sheet, row, col): source} of every formula cell, with sheet identities named as an export names them."""
    snapshot = engine.read_workbook()
    return {(sheet["name"], cell["row"], cell["col"]): cell["source"]
            for sheet in snapshot["sheets"] for cell in sheet["cells"] if cell["kind"] in ("formula", "unmaintained")}


class ServiceCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.service = Service(staging_root=self.temp/"staging")
        worker = threading.Thread(target=self.service.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        def stop():
            self.service.shutdown(); worker.join(timeout=2); self.service.server_close()
        self.addCleanup(stop)
        self.client = Client(f"http://127.0.0.1:{self.service.server_port}", self.service.token)

    def imported(self, path, name):
        summary = self.client.prepare_spreadsheet_file(path, mode="formulas", local=True)
        book = self.client.open(self.temp/(name+".dhwb"), create=True)
        if summary["can_import"]: book.import_staged(summary)
        return book

    def engine(self, book):
        return self.service.workbooks[book.info["id"]]

    def reimported(self, path, name):
        """The formula sources of `path` imported in formulas mode into a new workbook, which is then closed."""
        book = self.imported(path, name)
        try: return formula_sources(self.engine(book))
        finally: book.close()


class WholeWorkbookFormulaExportTests(ServiceCase):
    def test_every_fixture_imported_with_formulas_exports_by_every_route_and_keeps_its_formulas(self):
        for path in FIXTURES:
            with self.subTest(file=path.name):
                book = self.imported(path, path.stem)
                try: self.round_trip(path, book)
                finally: book.close()

    def round_trip(self, path, book):
        """Bounded and large XLSX exports re-import with the same formula sources; both ODS routes succeed."""
        original = formula_sources(self.engine(book))
        bounded = book.export_spreadsheet("xlsx", scope="workbook", mode="formulas")
        exported = self.temp/(path.stem+"-bounded.xlsx")
        exported.write_bytes(base64.b64decode(bounded["file_base64"]))
        self.assertEqual(self.reimported(exported, path.stem+"-again"), original)
        ods = book.export_spreadsheet("ods", scope="workbook", mode="formulas")
        self.assertTrue(ods["file_base64"])
        large = book.export_spreadsheet_file(self.temp/(path.stem+"-large.xlsx"))
        self.assertEqual(self.reimported(Path(large["path"]), path.stem+"-large-again"), original)
        self.assertTrue(Path(book.export_spreadsheet_file(self.temp/(path.stem+"-large.ods"))["path"]).is_file())
        # The bounded and the large XLSX export report the same unevaluable formulas.
        unevaluable = lambda entries: sorted((e["category"], e["count"]) for e in entries if e["severity"] == "unevaluable")
        self.assertEqual(unevaluable(bounded["report_entries"]), unevaluable(large["report_entries"]))

    def test_the_xlsx_export_report_counts_formulas_datahorizon_cannot_evaluate(self):
        expected = {"defined-names.xlsx": "formula.name", "excel-table.xlsx": "formula.syntax",
                    "external-3d-references.xlsx": "formula.external_reference"}
        for name in NAMED:
            with self.subTest(file=name):
                book = self.imported(CORPUS/name, name)
                try: result = book.export_spreadsheet("xlsx", scope="workbook", mode="formulas")
                finally: book.close()
                entries = [e for e in result["report_entries"] if e["severity"] == "unevaluable"]
                self.assertTrue(entries, result["report_entries"])
                self.assertEqual({e["detail"] for e in entries}, {EXPORT_UNEVALUABLE_DETAIL})
                self.assertIn(expected[name], {e["category"] for e in entries})
                # Each category is one entry, counting every formula of that reason, with its locations.
                categories = [e["category"] for e in entries]
                self.assertEqual(len(categories), len(set(categories)))
                self.assertTrue(all(e["samples"] and e["samples"][0]["cell"] for e in entries))

    def test_an_untokenizable_formula_is_written_verbatim_as_approximated(self):
        book = self.client.open(self.temp/"untokenizable.dhwb", create=True)
        book.set(1, 1, "=A5 ` 1")
        result = book.export_spreadsheet("xlsx", scope="workbook", mode="formulas")
        (entry,) = [e for e in result["report_entries"] if e["category"] == "formula.syntax"]
        self.assertEqual((entry["severity"], entry["count"], entry["samples"][0]["cell"]), ("approximated", 1, "A1"))
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(result["file_base64"]))) as archive:
            self.assertIn(b"<f>A5 ` 1</f>", archive.read("xl/worksheets/sheet1.xml"))

    def test_ods_writes_an_untranslatable_formula_as_its_value_or_empty(self):
        book = self.client.open(self.temp/"ods.dhwb", create=True)
        book.apply("edit", cells=[{"row": 1, "col": 1, "source": "=A2:A3 2:2"}, {"row": 2, "col": 1, "source": "=1+1"},
                                  {"row": 3, "col": 1, "source": "=Missing!A1"}])
        bounded = book.export_spreadsheet("ods", scope="workbook", mode="formulas")
        large = book.export_spreadsheet_file(self.temp/"ods.ods")
        for result in (bounded, large):
            with self.subTest(route="bounded" if result is bounded else "large"):
                found = {(e["severity"], e["detail"]): e["count"] for e in result["report_entries"] if e["category"] == UNTRANSLATED_FORMULA}
                # Neither formula has an error-free current value (the large export evaluates nothing), so both are empty.
                self.assertEqual(found, {("omitted", UNTRANSLATED_DETAIL_EMPTY): 2}, result["report_entries"])

    def test_ods_bounded_export_keeps_a_current_value_for_an_untranslatable_formula(self):
        book = self.client.open(self.temp/"ods-value.dhwb", create=True)
        book.apply("edit", cells=[{"row": 1, "col": 1, "source": "5"}, {"row": 1, "col": 2, "source": "=A1^2"}])
        result = book.export_spreadsheet("ods", scope="workbook", mode="formulas")
        (entry,) = [e for e in result["report_entries"] if e["category"] == UNTRANSLATED_FORMULA]
        self.assertEqual((entry["severity"], entry["detail"], entry["samples"][0]["cell"]), ("converted", UNTRANSLATED_DETAIL_VALUE, "B1"))
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(result["file_base64"]))) as archive:
            content = archive.read("content.xml").decode()
        self.assertIn('office:value="25.0"', content)
        self.assertNotIn("A1^2", content)

    def test_selection_export_stays_strict(self):
        book = self.imported(CORPUS/"defined-names.xlsx", "selection")
        sheet = self.engine(book).read_workbook()["sheets"][0]["name"]
        with self.assertRaises(APIError) as raised:
            book.export_spreadsheet("xlsx", sheet=sheet, r1=1, c1=1, r2=20, c2=10, mode="formulas")
        self.assertEqual(raised.exception.body["code"], "UNSUPPORTED_FORMULA")

    def test_values_export_names_the_cells_that_block_it(self):
        book = self.client.open(self.temp/"values.dhwb", create=True)
        book.apply("edit", cells=[{"row": row, "col": 1, "source": "=1/0"} for row in range(1, 151)]+[{"row": 1, "col": 2, "source": "7"}])
        for fmt in ("xlsx", "ods"):
            with self.subTest(format=fmt):
                with self.assertRaises(APIError) as raised:
                    book.export_spreadsheet(fmt, scope="workbook", mode="values")
                body = raised.exception.body
                self.assertEqual(body["code"], "CALCULATION_ERROR")
                self.assertEqual(body["blocked_count"], 150)
                self.assertEqual(len(body["cells"]), 100)
                self.assertEqual(body["cells"][0], "Sheet1!A1")
                self.assertIn("Sheet1!A1", body["message"])


if __name__ == "__main__":
    unittest.main()
