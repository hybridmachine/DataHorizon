# SPDX-License-Identifier: GPL-3.0-only
"""Every preview, review summary and export result names its fixed scope lines, which its report starts with and
one line per report entry follows, for XLSX and ODS alike; clients read them instead of slicing the report."""
import base64
from pathlib import Path
import unittest

from datahorizon.cli import report_text
from datahorizon.client import report_scope_lines
from datahorizon.interchange import xlsx_preview
from datahorizon.ods import ods_preview
from test_export_unevaluable import ServiceCase

FIXTURES = Path(__file__).resolve().parent/"fixtures"


class ScopeLinesTests(ServiceCase):
    def assert_shape(self, result):
        scope = result["scope_lines"]
        self.assertTrue(scope)
        self.assertEqual(result["report"][:len(scope)], scope)
        self.assertEqual(len(result["report"])-len(scope), len(result["report_entries"]))
        self.assertEqual(report_scope_lines(result), scope)

    def test_inline_previews_in_both_scopes(self):
        for name, preview in (("unsupported.xlsx", xlsx_preview), ("fidelity.ods", ods_preview)):
            for scope in ("selection", "workbook"):
                with self.subTest(file=name, scope=scope):
                    self.assert_shape(preview({"file_base64": base64.b64encode((FIXTURES/name).read_bytes()).decode(), "scope": scope}))

    def test_staged_summaries_and_exports(self):
        for name in ("unsupported.xlsx", "fidelity.ods"):
            with self.subTest(file=name):
                summary = self.client.prepare_spreadsheet_file(FIXTURES/name, mode="formulas", local=True)
                self.assert_shape(summary)
                book = self.client.open(self.temp/(name+".dhwb"), create=True)
                try:
                    book.import_staged(summary)
                    for fmt in ("xlsx", "ods"):
                        self.assert_shape(book.export_spreadsheet(fmt, scope="workbook", mode="formulas"))
                        self.assert_shape(book.export_spreadsheet_file(self.temp/(name+"."+fmt)))
                finally:
                    book.close()

    def test_the_inline_and_staged_ods_reports_have_the_same_order(self):
        data = (FIXTURES/"fidelity.ods").read_bytes()
        inline = ods_preview({"file_base64": base64.b64encode(data).decode(), "scope": "workbook", "mode": "formulas"})
        staged = self.client.prepare_spreadsheet_file(FIXTURES/"fidelity.ods", mode="formulas", local=True)
        self.assertEqual(inline["scope_lines"], staged["scope_lines"])
        self.assertTrue(inline["scope_lines"][-1].startswith("Supported dates/datetimes"))
        self.client.discard_job(staged["stage_id"])


class ClientFallbackTests(unittest.TestCase):
    def test_clients_read_scope_lines_and_fall_back_to_slicing_only_without_them(self):
        entry = {"category": "c", "severity": "omitted", "detail": "d", "count": 1, "samples": []}
        named = {"report": ["entry line", "scope"], "scope_lines": ["scope"], "report_entries": [entry], "totals": {}}
        self.assertEqual(report_scope_lines(named), ["scope"])
        self.assertIn("  scope", report_text(named).splitlines())
        self.assertNotIn("  entry line", report_text(named).splitlines())
        older = {"report": ["scope", "entry line"], "report_entries": [entry]}
        self.assertEqual(report_scope_lines(older), ["scope"])

    def test_a_saturated_count_reads_as_a_lower_bound_in_the_cli(self):
        entry = {"category": "sheet.feature", "severity": "omitted", "detail": "data_validation", "count": 9, "samples": [], "at_least": True}
        self.assertIn(">=9", report_text({"report": [], "scope_lines": [], "report_entries": [entry], "totals": {}}))


if __name__ == "__main__":
    unittest.main()
