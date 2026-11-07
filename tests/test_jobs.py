"""Preparation jobs have no workbook mutation phase or durable outcome claims."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from datahorizon.client import LocalService
from datahorizon.csv_import import preview_csv
from datahorizon.engine import EngineError
from datahorizon.jobs import PreparationJobs


class PreparationTests(unittest.TestCase):
    def terminal(self, jobs, job_id):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            result=jobs.access(job_id)
            if result["status"]!="running": return result
            time.sleep(.005)
        self.fail("Job did not terminate")

    def test_progress_idempotence_conflict_admission_cancel_and_release(self):
        jobs=PreparationJobs()
        entered, resume=threading.Event(), threading.Event()
        def blocked(payload, checkpoint):
            checkpoint(3,"Parsing")
            entered.set()
            self.assertTrue(resume.wait(2))
            checkpoint(4,"Parsing")
            return {"can_import":True}
        try:
            request={"job_id":"same","csv":"7"}
            jobs.start(request,blocked)
            self.assertTrue(entered.wait(2))
            self.assertEqual(jobs.start(request,blocked)["completed"],3)
            for payload,code in (({**request,"csv":"8"},"JOB_CONFLICT"),({**request,"job_id":"other"},"JOB_BUSY")):
                with self.assertRaises(EngineError) as failure: jobs.start(payload,blocked)
                self.assertEqual(failure.exception.body["code"],code)
            with self.assertRaises(EngineError): jobs.access("same","release")
            jobs.access("same","cancel"); resume.set()
            terminal=self.terminal(jobs,"same")
            self.assertEqual(terminal["status"],"cancelled"); self.assertNotIn("result",terminal)
            self.assertEqual(jobs.start(request,blocked)["status"],"cancelled")
            jobs.access("same","release")
            with self.assertRaises(EngineError): jobs.access("same")
        finally:
            resume.set(); jobs.close()

    def test_failed_timeout_capacity_expiry_and_discard_completed_result(self):
        jobs=PreparationJobs(capacity=1,seconds=-1)
        try:
            jobs.start({"job_id":"timeout","csv":"7"},preview_csv)
            result=self.terminal(jobs,"timeout")
            self.assertEqual(result["error"]["code"],"TIMEOUT")
            with self.assertRaises(EngineError) as failure: jobs.start({"job_id":"next","csv":"7"},preview_csv)
            self.assertEqual(failure.exception.body["code"],"JOB_LIMIT")
            jobs.retention=0; jobs.seconds=2
            jobs.start({"job_id":"next","csv":"7"},preview_csv)
            self.assertEqual(self.terminal(jobs,"next")["result"]["rows"],[["7"]])
            self.assertNotIn("result",jobs.access("next","cancel"))
            jobs.access("next","release")
            jobs.start({"job_id":"bad","csv":'"unterminated'},preview_csv)
            self.assertEqual(self.terminal(jobs,"bad")["error"]["code"],"INVALID_CSV")
        finally: jobs.close()

    def test_shutdown_cancels_and_joins_active_worker(self):
        jobs=PreparationJobs()
        entered=threading.Event()
        def loop(payload,checkpoint):
            entered.set()
            while True:
                checkpoint(0,"Parsing")
                time.sleep(.005)
        jobs.start({"job_id":"stop"},loop)
        self.assertTrue(entered.wait(2))
        worker=jobs.entries["stop"]["worker"]
        jobs.close()
        self.assertFalse(worker.is_alive())
        with self.assertRaises(EngineError): jobs.start({"job_id":"late"},loop)

    def test_rest_sdk_result_applies_once_through_ordinary_managed_edit(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/"job.dhwb",create=True) as book:
            original=book.state
            job=client.prepare_csv({"csv":"00123,7","column_types":{"2":"number"}},"reviewed")
            deadline=time.monotonic()+3
            while job["status"]=="running" and time.monotonic()<deadline:
                time.sleep(.01); job=client.preparation("reviewed")
            self.assertEqual(job["status"],"completed")
            book.refresh(); self.assertEqual(book.state,original)
            inputs=job["result"]["inputs"]
            book.apply("edit",cells=[{"row":1,"col":c,**item} for c,item in enumerate(inputs[0],1)])
            self.assertEqual([cell["value"] for cell in book.read(r2=1,c2=2)["cells"]],["00123",7])
            client.preparation("reviewed","release")
            self.assertIn("/v1/jobs/{job}/cancel",client.request("/openapi.json")["paths"])
