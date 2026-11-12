# SPDX-License-Identifier: GPL-3.0-only
"""The staging directory goes away when the service stops, and a dead service's directory at the next start.

Only a directory named like one the service creates, holding the project's owner marker for a process that is no
longer running, is ever swept; a directory without the marker or whose process is alive is left alone."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from datahorizon.client import LocalService
from datahorizon.engine import EngineError
from datahorizon.service import Service
from datahorizon.staging import MAGIC, MARKER, Staging, sweep_terminated

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import xlsx_builder  # noqa: E402


def dead_pid():
    """The pid of a process that has exited (and been reaped)."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def owned_directories(pid):
    return sorted(p for p in Path(tempfile.gettempdir()).glob(f"datahorizon-{pid}-*") if p.is_dir())


def make_leftover(root, pid, *, marker=True, record_pid=None):
    directory = Path(tempfile.mkdtemp(prefix=f"datahorizon-{pid}-", dir=root))
    (directory/"stage-x.stage").write_bytes(b"left over")
    if marker:
        (directory/MARKER).write_text(json.dumps({"magic": MAGIC, "pid": pid if record_pid is None else record_pid, "created": time.time()}))
    return directory


class StagingLifecycleTests(unittest.TestCase):
    def test_an_owned_directory_holds_the_marker_and_close_removes_it(self):
        staging = Staging()
        owner = json.loads((staging.root/MARKER).read_text())
        self.assertEqual((owner["magic"], owner["pid"]), (MAGIC, os.getpid()))
        self.assertIsInstance(owner["created"], float)
        staging.close()
        self.assertFalse(staging.root.exists())

    def test_new_after_close_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            staging = Staging(Path(temp)/"root")
            staging.close()
            with self.assertRaises(EngineError) as raised:
                staging.new("upload", "late")
            self.assertEqual((raised.exception.body["code"], raised.exception.status), ("SERVICE_STOPPING", 503))
            self.assertFalse((Path(temp)/"root"/"upload-late.upload").exists())

    def test_a_supplied_root_gets_no_marker_and_is_never_swept(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/f"datahorizon-{dead_pid()}-supplied"
            staging = Staging(root)
            try:
                self.assertFalse((root/MARKER).exists())
                self.assertEqual(sweep_terminated(temp), 0)
                self.assertTrue(root.is_dir())
            finally:
                staging.close()


class SweepTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_only_a_marked_directory_of_a_dead_process_is_swept(self):
        gone = dead_pid()
        dead = make_leftover(self.root, gone)
        unmarked = make_leftover(self.root, gone, marker=False)
        live = make_leftover(self.root, os.getppid())
        other = make_leftover(self.root, gone, record_pid=gone+1)  # the marker names another process
        foreign = self.root/f"datahorizon-{gone}-UPPER"; foreign.mkdir()  # not a name Staging creates
        (foreign/MARKER).write_text(json.dumps({"magic": MAGIC, "pid": gone, "created": 1.0}))
        self.assertEqual(sweep_terminated(self.root), 1)
        self.assertFalse(dead.exists())
        for kept in (unmarked, live, other, foreign):
            self.assertTrue(kept.exists(), kept)

    def test_a_marker_that_is_not_a_regular_small_file_is_not_trusted(self):
        gone = dead_pid()
        linked = make_leftover(self.root, gone, marker=False)
        target = self.root/"elsewhere.json"
        target.write_text(json.dumps({"magic": MAGIC, "pid": gone, "created": 1.0}))
        (linked/MARKER).symlink_to(target)
        large = make_leftover(self.root, gone, marker=False)
        (large/MARKER).write_text(json.dumps({"magic": MAGIC, "pid": gone, "created": 1.0, "pad": "x"*5000}))
        self.assertEqual(sweep_terminated(self.root), 0)
        self.assertTrue(linked.exists() and large.exists())

    def test_service_start_sweeps_the_system_temporary_directory(self):
        gone = dead_pid()
        dead = make_leftover(tempfile.gettempdir(), gone)
        unmarked = make_leftover(tempfile.gettempdir(), gone, marker=False)
        self.addCleanup(lambda: __import__("shutil").rmtree(unmarked, ignore_errors=True))
        with tempfile.TemporaryDirectory() as temp:
            service = Service(staging_root=Path(temp)/"staging")
            service.server_close()
        self.assertFalse(dead.exists())
        self.assertTrue(unmarked.exists())


class ShutdownTests(unittest.TestCase):
    def test_closing_a_local_service_removes_its_staging_with_an_upload_and_a_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp)/"book.xlsx"
            package.write_bytes(xlsx_builder.workbook_bytes({"Sheet1": '<row r="1"><c r="A1"><v>1</v></c></row>'}))
            service = LocalService()
            try:
                client = service.client
                upload = client.upload(package, "kept-upload")
                client.start_job("/v1/import/xlsx/jobs", {"job_id": "kept-stage", "upload_id": upload["upload_id"], "scope": "workbook"})
                for _ in range(500):
                    if client.preparation("kept-stage")["status"] != "running": break
                    time.sleep(.01)
                (directory,) = owned_directories(service.process.pid)
                self.assertTrue(any(p.name.startswith("stage-") for p in directory.iterdir()))
                self.assertTrue(any(p.name.startswith("upload-") for p in directory.iterdir()))
            finally:
                started = time.monotonic()
                service.close()
            # The polite stop within LocalService's wait: an orderly exit, not the kill that follows a timeout.
            self.assertEqual(service.process.returncode, 0)
            self.assertLess(time.monotonic()-started, 3)
            self.assertFalse(directory.exists())

    @unittest.skipIf(os.name == "nt", "Windows terminates the process without a signal; the next start sweeps instead")
    def test_sigint_also_stops_in_order(self):
        service = LocalService()
        try:
            (directory,) = owned_directories(service.process.pid)
            service.process.send_signal(signal.SIGINT)
            self.assertEqual(service.process.wait(timeout=5), 0)
            self.assertFalse(directory.exists())
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
