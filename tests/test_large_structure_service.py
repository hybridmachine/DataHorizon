# SPDX-License-Identifier: GPL-3.0-only
"""Large structural operation admission, progress, cancellation and receipt recovery."""
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from datahorizon.client import APIError, Client, UnknownOutcome
from datahorizon.engine import EngineError, Workbook
from datahorizon.service import Service, openapi


class LargeStructureServiceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.server = self.enterContext(Service())
        serving = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval":.01})
        serving.start()
        self.addCleanup(serving.join)
        self.addCleanup(self.server.shutdown)
        self.path = Path(directory) / "structure.dhwb"
        self.client = Client(f"http://127.0.0.1:{self.server.server_port}", self.server.token)
        self.book = self.client.open(self.path, create=True)
        self.operations = self.book.base + "/operations"
        self.book.apply("edit", label="Seed rows", cells=[
            {"row":row, "col":1, "source":str(row)} for row in range(1, 5002)
        ])

    def command(self, operation="move-once", **options):
        return dict(action="rows_insert", sheet="Sheet1", start=1, count=6000,
                    expected_state=self.book.state, operation_id=operation, **options)

    def terminal(self, operation):
        deadline = time.monotonic() + 10
        while True:
            view = self.book.operation_status(operation)
            if view["status"] != "running":
                return view
            self.assertLess(time.monotonic(), deadline, view)
            time.sleep(.02)

    def cell(self, row):
        result = self.book.read(r1=row, r2=row, c1=1, c2=1)["cells"]
        return result[0]["value"] if result else None

    def test_review_preserves_history_and_confirmed_execution_exposes_a_receipt(self):
        self.book.apply("edit", label="Second edit", cells=[{"row":1,"col":2,"source":"extra"}])
        self.book.apply("undo")
        before = self.book.refresh()
        command = self.command()
        with self.assertRaises(APIError) as raised:
            self.client.request(self.operations, command)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.body["code"], "LARGE_STRUCTURE_CONFIRMATION_REQUIRED")
        self.assertEqual(raised.exception.body["outcome"], "rejected")
        self.assertIs(raised.exception.body["clears_history"], True)
        self.assertEqual(raised.exception.body["expected_state"], before["state"])
        self.assertEqual(self.book.refresh(), before)
        with self.assertRaises(UnknownOutcome):
            self.book.outcome(command["operation_id"])

        progress = []
        receipt = self.book.apply(**command, confirm_large_structure=True, progress=progress.append)
        self.assertEqual(receipt["changed_cells"], 10002)
        self.assertEqual(progress[0]["operation_id"], command["operation_id"])
        self.assertEqual(progress[0]["status"], "running")
        self.assertIsNone(self.book.pending_operation)
        self.assertEqual(self.book.state, receipt["state"])
        self.assertEqual(self.book.operation_status(command["operation_id"])["receipt"], receipt)
        self.assertEqual(self.client.request(self.operations, command), receipt)
        self.assertIsNone(self.cell(1))
        self.assertEqual([self.cell(6001), self.cell(11001)], [1, 5001])
        after = self.book.refresh()
        self.assertEqual((after["undo"], after["redo"]), (None, None))

        # Closing the final handle discards live progress. Reopening can still report the original receipt.
        self.book.close()
        with self.client.open(self.path) as reopened:
            self.assertEqual(reopened.operation_status(command["operation_id"])["receipt"], receipt)

    def test_running_status_replay_and_cancel_do_not_wait_for_writer(self):
        observer = self.client.open(self.path)
        entered, release = threading.Event(), threading.Event()
        original = Workbook.mutate

        def pause(workbook, request, *, large_structure_progress=None, **options):
            def progress(phase):
                large_structure_progress(phase)
                if phase == "committing":
                    entered.set()
                    self.assertTrue(release.wait(8))
            return original(workbook, request, large_structure_progress=progress if large_structure_progress else None, **options)

        command = self.command(confirm_large_structure=True)
        operation = command["operation_id"]
        with patch.object(Workbook, "mutate", pause):
            try:
                self.assertEqual(self.client.request(self.operations, command)["status"], "running")
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.book.operation_status(operation)["phase"], "committing")
                self.assertEqual(self.client.request(self.operations, dict(command, confirm_large_structure=False))["status"], "running")
                with self.assertRaises(APIError) as reused:
                    self.client.request(self.operations, dict(command, count=6001))
                self.assertEqual(reused.exception.body["code"], "OPERATION_ID_REUSED")
                self.assertEqual(reused.exception.body["outcome"], "unknown")
                with self.assertRaises(APIError) as running:
                    self.client.request(self.book.base + "/close", {})
                self.assertEqual(running.exception.body["code"], "OPERATION_RUNNING")
                self.assertEqual(observer.close(), {"closed":True})
                with self.assertRaises(UnknownOutcome):
                    self.book.outcome(operation)
                self.assertTrue(self.book.cancel_operation(operation)["cancel_requested"])
            finally:
                release.set()
            view = self.terminal(operation)
        self.assertEqual(view["status"], "failed")
        self.assertEqual(view["error"]["code"], "CANCELLED")
        self.assertEqual(view["error"], self.book.outcome(operation))
        with self.assertRaises(APIError) as replay:
            self.client.request(self.operations, command)
        self.assertEqual(replay.exception.body, view["error"])
        self.assertEqual(self.cell(1), 1)
        self.assertIsNone(self.cell(6001))
        self.assertEqual(self.book.refresh()["undo"], "Seed rows")

    def test_worker_rechecks_state_under_sqlite_writer_lock(self):
        command = self.command()
        with self.assertRaises(APIError) as offer:
            self.client.request(self.operations, command)
        self.assertEqual(offer.exception.body["code"], "LARGE_STRUCTURE_CONFIRMATION_REQUIRED")
        entered, release = threading.Event(), threading.Event()
        original = Workbook.mutate

        def pause(workbook, request, **options):
            if options.get("large_structure_progress"):
                entered.set()
                self.assertTrue(release.wait(8))
            return original(workbook, request, **options)

        with patch.object(Workbook, "mutate", pause):
            try:
                self.client.request(self.operations, dict(command, confirm_large_structure=True))
                self.assertTrue(entered.wait(5))
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("UPDATE cells SET source='99' WHERE row=1 AND col=1")
            finally:
                release.set()
            view = self.terminal(command["operation_id"])
        self.assertEqual(view["error"]["code"], "STALE_STATE")
        self.assertEqual(view["error"]["outcome"], "rolled_back")
        self.assertEqual(self.cell(1), 99)
        self.assertIsNone(self.cell(6001))

    def test_lost_running_acknowledgement_reconciles_without_a_second_shift(self):
        original = self.client.request
        sent = []
        command = self.command(confirm_large_structure=True)

        def lose(path, body=None):
            value = original(path, body)
            if path == self.operations and body:
                sent.append(value)
                raise ConnectionError("Dropped acknowledgement")
            return value

        with patch.object(self.client, "request", lose):
            with self.assertRaises(UnknownOutcome) as lost:
                self.book.apply(**command)
        operation = lost.exception.operation_id
        self.assertEqual(sent[0]["status"], "running")
        self.assertEqual(self.book.pending_operation, operation)
        self.assertEqual(self.terminal(operation)["status"], "completed")
        receipt = self.book.outcome(operation)
        self.assertEqual(self.client.request(self.operations, command), receipt)
        self.assertIsNone(self.book.pending_operation)
        self.assertEqual(self.cell(6001), 1)
        self.assertIsNone(self.cell(12001))

    def test_interrupt_during_confirmation_post_cancels_admitted_work(self):
        original_mutate = Workbook.mutate
        original_request = self.client.request
        sent = []
        command = self.command(confirm_large_structure=True)

        def pause(workbook, request, *, cancel_event=None, **options):
            if options.get("large_structure_progress"):
                self.assertTrue(cancel_event.wait(5))
            return original_mutate(workbook, request, cancel_event=cancel_event, **options)

        def interrupt(path, body=None, **options):
            value = original_request(path, body, **options)
            if path == self.operations and body:
                sent.append(value)
                raise KeyboardInterrupt
            return value

        with patch.object(Workbook, "mutate", pause), patch.object(self.client, "request", interrupt):
            with self.assertRaises(APIError) as cancelled:
                self.book.apply(**command, cancel_on_interrupt=True)
        self.assertEqual(len(sent), 1)
        self.assertEqual(cancelled.exception.body["code"], "CANCELLED")
        self.assertEqual(cancelled.exception.body, self.book.outcome(command["operation_id"]))
        self.assertIsNone(self.book.pending_operation)

    def test_interrupt_with_unconfirmed_admission_retains_operation_id(self):
        original = self.client.request

        def interrupt(path, body=None, **options):
            if path == self.operations and body:
                raise KeyboardInterrupt
            return original(path, body, **options)

        command = self.command(confirm_large_structure=True)
        with patch.object(self.client, "request", interrupt):
            with self.assertRaises(UnknownOutcome) as unknown:
                self.book.apply(**command, cancel_on_interrupt=True)
        self.assertEqual(unknown.exception.body["operation_id"], command["operation_id"])
        self.assertEqual(self.book.pending_operation, command["operation_id"])
        self.assertEqual(self.cell(1), 1)

    def test_review_timeout_offers_background_execution(self):
        command = self.command()
        timeout = EngineError("TIMEOUT", "Structural edit review exceeded its time budget")
        with patch("datahorizon.service.needs_large", side_effect=timeout):
            with self.assertRaises(APIError) as offer:
                self.client.request(self.operations, command)
            self.assertEqual(offer.exception.body["code"], "LARGE_STRUCTURE_CONFIRMATION_REQUIRED")
            receipt = self.book.apply(**command, confirm_large_structure=True)
        self.assertEqual(receipt["changed_cells"], 10002)
        self.assertEqual(self.cell(6001), 1)

    def test_exact_coordinate_limit_keeps_the_ordinary_response(self):
        self.book.apply("edit", cells=[{"row":5001,"col":1,"clear":True}])
        receipt = self.client.request(self.operations, self.command())
        self.assertEqual(receipt["outcome"], "committed")
        self.assertEqual(receipt["changed_cells"], 10000)
        self.assertNotIn("status", receipt)

    def test_orderly_shutdown_cancels_and_joins_an_active_writer(self):
        entered = threading.Event()
        original = Workbook.mutate

        def pause(workbook, request, *, large_structure_progress=None, cancel_event=None, **options):
            def progress(phase):
                large_structure_progress(phase)
                if phase == "committing":
                    entered.set()
                    self.assertTrue(cancel_event.wait(8))
            return original(workbook, request, cancel_event=cancel_event,
                            large_structure_progress=progress if large_structure_progress else None, **options)

        command = self.command(confirm_large_structure=True)
        with patch.object(Workbook, "mutate", pause):
            self.client.request(self.operations, command)
            self.assertTrue(entered.wait(5))
            self.server.shutdown()
            self.server.server_close()
        self.assertFalse(self.server.active_operations)
        with closing(Workbook(self.path)) as reopened:
            receipt = reopened.operation(command["operation_id"])
        self.assertEqual((receipt["outcome"], receipt["code"]), ("rolled_back", "CANCELLED"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT row,source FROM cells WHERE row IN (1,6001)").fetchall(), [(1,"1")])

    def test_cli_apply_declines_without_approval_then_reports_the_confirmed_receipt(self):
        def cli(*arguments, request=None):
            return subprocess.run([sys.executable,"-m","datahorizon.cli","--url",self.client.url,str(self.path),*arguments],
                                  env=dict(os.environ, DATAHORIZON_TOKEN=self.server.token),
                                  input=None if request is None else json.dumps(request),
                                  capture_output=True,text=True,timeout=15)

        command = self.command()
        refused = cli("apply", request=command)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(json.loads(refused.stderr)["code"], "LARGE_STRUCTURE_CONFIRMATION_REQUIRED")
        self.assertEqual(self.cell(1), 1)
        approved = cli("apply", "--confirm-large-structure", request=command)
        self.assertEqual(approved.returncode, 0, approved.stderr)
        receipt = json.loads(approved.stdout)
        self.assertEqual(receipt["outcome"], "committed")
        status = cli("operation-status", command["operation_id"], "--handle", self.book.info["id"])
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["receipt"], receipt)

    @unittest.skipUnless(os.name == "posix", "Requires POSIX terminal signal groups")
    def test_owned_cli_group_interrupt_leaves_its_service_available_for_cancel(self):
        marker = self.path.with_name("hook-ready")
        cancelled = self.path.with_name("cancel-requested")
        handler = self.path.with_name("pause_structure.py")
        handler.write_text(
            "from pathlib import Path\nimport time\n"
            "def on_structure(context, event):\n"
            "    if event['phase'] == 'before':\n"
            f"        Path({str(marker)!r}).touch()\n"
            f"        while not Path({str(cancelled)!r}).exists():\n"
            "            time.sleep(.01)\n", encoding="utf-8")
        owned_cli = (
            "from pathlib import Path\nfrom datahorizon import cli\n"
            "from datahorizon.client import WorkbookClient\n"
            "original = WorkbookClient.cancel_operation\n"
            "def cancel(self, operation, **options):\n"
            "    status = original(self, operation, **options)\n"
            "    if status['cancel_requested']:\n"
            f"        Path({str(cancelled)!r}).touch()\n"
            "    return status\n"
            "WorkbookClient.cancel_operation = cancel\ncli.main()\n")
        command = self.command()
        command.pop("expected_state")  # Owned CLI opens its own workbook state.
        process = subprocess.Popen([sys.executable,"-c",owned_cli,"--handler",str(handler),str(self.path),
                                    "apply","--confirm-large-structure"],
                                   start_new_session=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            process.stdin.write(json.dumps(command))
            process.stdin.close()
            process.stdin = None
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists(), "The structural hook was not reached")
            os.killpg(process.pid, signal.SIGINT)
            _, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertTrue(cancelled.exists(), stderr)
        self.assertEqual(process.returncode, 1)
        error = json.loads(stderr)
        self.assertEqual(error["code"], "CANCELLED")
        self.assertEqual(error, self.book.outcome(command["operation_id"]))

    def test_openapi_exposes_review_fields_and_status_variants(self):
        api = openapi()
        schemas = api["components"]["schemas"]
        for action in ("RowsInsert", "RowsDelete", "ColumnsInsert", "ColumnsDelete", "SheetDuplicate", "SheetDelete"):
            self.assertEqual(schemas[action+"Operation"]["properties"]["confirm_large_structure"]["type"], "boolean")
        operations = "/v1/workbooks/{workbook}/operations"
        self.assertEqual(api["paths"][operations+"/{operation}/status"]["get"]["responses"]["200"]["content"]["application/json"]["schema"],
                         {"$ref":"#/components/schemas/LargeStructureStatus"})
        self.assertEqual(len(schemas["OperationSubmission"]["oneOf"]), 2)
        self.assertEqual(len(schemas["LargeStructureStatus"]["oneOf"]), 3)


if __name__ == "__main__":
    unittest.main()
