# SPDX-License-Identifier: GPL-3.0-only
"""The large interchange limits the service declares are the measured ones, and the measurement script still runs."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from datahorizon.client import Client
from datahorizon.service import MAX_DOWNLOADS, MAX_UPLOADS, Service, openapi
from datahorizon.xlsx_stream import LARGE_CELLS, LARGE_EXPANDED, LARGE_PACKAGE

try:
    import resource
except ImportError:
    resource = None

EVIDENCE = Path(__file__).resolve().parents[1]/"docs"/"design"/"evidence"/"excel-fidelity-1-capacity.json"


class DeclaredLimitTests(unittest.TestCase):
    def test_declared_limits_match_capabilities(self):
        with tempfile.TemporaryDirectory() as temp, Service(staging_root=Path(temp)/"staging") as server:
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            worker.start()
            try:
                capabilities = Client(f"http://127.0.0.1:{server.server_port}", server.token).capabilities()
            finally:
                server.shutdown(); worker.join(timeout=2)
        self.assertEqual(capabilities["max_upload_bytes"], LARGE_PACKAGE)
        self.assertEqual(capabilities["max_import_cells"], LARGE_CELLS)
        self.assertEqual(capabilities["max_export_cells"], LARGE_CELLS)
        self.assertEqual(capabilities["max_concurrent_uploads"], MAX_UPLOADS)
        self.assertEqual(capabilities["max_concurrent_downloads"], MAX_DOWNLOADS)
        upload = openapi()["paths"]["/v1/uploads/{upload}"]["put"]["requestBody"]["content"]["application/octet-stream"]["schema"]
        self.assertEqual(upload["maxLength"], LARGE_PACKAGE)

    def test_declared_limits_are_within_the_recorded_measurement(self):
        decision = json.loads(EVIDENCE.read_text(encoding="utf-8"))["decision"]
        self.assertLessEqual(LARGE_CELLS, decision["largest_qualifying_cells_all_formats"])
        measured = decision["measured_at_that_size"]
        self.assertGreater(LARGE_PACKAGE, measured["max_package_bytes"])
        self.assertLessEqual(LARGE_PACKAGE, 64*1024*1024)
        self.assertLessEqual(LARGE_EXPANDED, 256*1024*1024)


@unittest.skipIf(resource is None or not (sys.platform == "darwin" or sys.platform.startswith("linux")),
                 "Service RSS sampling needs resource and macOS or Linux")
class QualificationScriptSmokeTests(unittest.TestCase):
    def test_a_small_workbook_runs_every_phase_of_both_formats(self):
        import qualify_large_interchange as qualification
        args = argparse.Namespace(sheets=2, formula_share=.05, long_text_share=.05, strings="shared")
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temp:
            report = qualification.qualify([5000], ["xlsx", "ods"], Path(temp).resolve(), args, log=lambda text: None)
            self.assertEqual([path.name for path in Path(temp).iterdir()], [])
        self.assertLess(time.monotonic()-started, 20)
        expected = ["upload", "prepare", "create_workbook", "apply", "close", "open", "first_viewport", "export", "download",
                    "reimport_upload", "reimport_prepare", "reimport_create_workbook", "reimport_apply"]
        for case in report["cases"]:
            with self.subTest(case["format"]):
                self.assertNotIn("error", case)
                self.assertEqual([phase["name"] for phase in case["phases"]], expected)
                self.assertTrue(all(phase["peak_rss_bytes"] > 0 and phase["samples"] >= 1 for phase in case["phases"]))
                self.assertTrue(all(service["ru_maxrss_at_exit_bytes"] > 0 for service in case["services"]))
                self.assertEqual(case["counts"]["imported"], 5000)
                self.assertTrue(case["counts"]["reimport_matches"])
                self.assertGreater(case["files"]["native_bytes"], 0)
        self.assertTrue(all(verdict["qualifies"] for verdict in report["decision"]["cases"]))
        self.assertEqual(report["decision"]["largest_qualifying_cells_all_formats"], 5000)
        self.assertEqual(report["limits_in_force"]["LARGE_CELLS"], LARGE_CELLS)


if __name__ == "__main__":
    unittest.main()
