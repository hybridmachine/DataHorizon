"""Real process exits, bounded validation, path replacement and read-only inspection."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from datahorizon.client import APIError,LocalService
from datahorizon.engine import EngineError,Workbook,check_integrity
from datahorizon.recovery import inspect_native


def edit(book,source,operation="seed"):
    return book.mutate({"operation_id":operation,"expected_state":book.state(),"action":"edit","cells":[{"row":1,"col":1,"source":source,"kind":"text"}]})


class RecoveryTests(unittest.TestCase):
    def test_open_integrity_has_a_shared_deadline_and_does_not_modify_rejected_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"native.dhwb"
            with closing(Workbook(path,create=True)) as book: edit(book,"retained")
            original=path.read_bytes(); validate=Workbook.validate
            def expire(book,*args,**kwargs):
                validate(book,*args,**kwargs)
                book._opening_deadline=time.monotonic()-1
            with patch.object(Workbook,"validate",new=expire),self.assertRaises(EngineError) as failure:
                Workbook(path,readonly=True)
            self.assertEqual(failure.exception.body["code"],"TIMEOUT")
            self.assertEqual(path.read_bytes(),original)
            with closing(Workbook(path)) as reopened:
                self.assertEqual(reopened.read("Sheet1",{})["cells"][0]["value"],"retained")
                with self.assertRaises(EngineError): check_integrity(reopened.db,time.monotonic()-1)

    def test_observed_source_replacement_blocks_reads_writes_receipts_and_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"source.dhwb"; replacement=Path(temp)/"other.dhwb"
            with closing(Workbook(path,create=True)) as old,closing(Workbook(replacement,create=True)) as newer:
                edit(old,"old"); edit(newer,"foreign"); newer.close()
                try: os.replace(replacement,path)
                except PermissionError: self.skipTest("Platform prevents replacement of an open SQLite file")
                foreign=path.read_bytes()
                for operation in (lambda:old.info(),lambda:old.read("Sheet1",{}),lambda:edit(old,"bad","blocked"),
                                  lambda:old.operation("seed"),lambda:old.snapshot(Path(temp)/"copy.dhwb")):
                    with self.assertRaises(EngineError) as failure: operation()
                    self.assertEqual(failure.exception.body["code"],"FILE_CHANGED")
                self.assertEqual(path.read_bytes(),foreign)
                self.assertFalse((Path(temp)/"copy.dhwb").exists())

    def test_late_identity_io_failure_rolls_back_without_detached_receipt_or_writer_lock(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/"source.dhwb",create=True)) as book:
            edit(book,"old")
            unavailable=False; verify=book.verify_file
            def check():
                if unavailable: raise OSError("Injected identity I/O failure")
                verify()
            def hook(_):
                nonlocal unavailable
                unavailable=True
            with patch.object(book,"verify_file",side_effect=check),patch.object(book,"run_hooks",side_effect=hook):
                with self.assertRaises(EngineError) as failure: edit(book,"new","late")
            self.assertEqual(failure.exception.body["outcome"],"unknown")
            self.assertFalse(book.db.in_transaction)
            self.assertEqual(book.read("Sheet1",{})["cells"][0]["value"],"old")
            with self.assertRaises(EngineError): book.operation("late")
            edit(book,"next","recovered")

    def test_snapshot_late_integrity_failure_keeps_destination_and_never_claims_success(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/"source.dhwb",create=True)) as book:
            edit(book,"retained"); target=Path(temp)/"failed-copy.dhwb"
            with patch("datahorizon.engine.check_integrity",side_effect=EngineError("TIMEOUT","Injected integrity deadline")):
                with self.assertRaises(EngineError) as failure: book.snapshot(target)
            self.assertEqual(failure.exception.body["code"],"TIMEOUT"); self.assertTrue(target.exists())
            self.assertEqual(book.read("Sheet1",{})["cells"][0]["value"],"retained")

    def test_abrupt_exit_recovers_spilled_managed_edits_without_replaying_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"crash.dhwb"; original="old"*20000
            with closing(Workbook(path,create=True)) as book: edit(book,original)
            script=Path(temp)/"crash.py"
            script.write_text('''import os,sys
from datahorizon.engine import Workbook
book=Workbook(sys.argv[1])
book.db.execute("PRAGMA cache_size=1")
book.db.execute("PRAGMA cache_spill=ON")
put=book.put
def crash(*args):
    put(*args)
    os._exit(73)
book.put=crash
book.mutate({"action":"edit","operation_id":"interrupted","expected_state":book.state(),"cells":[{"row":1,"col":1,"source":"new"*20000,"kind":"text"}]})
''')
            result=subprocess.run([sys.executable,str(script),str(path)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,73,result.stderr)
            journal=Path(str(path)+"-journal")
            self.assertTrue(journal.exists()); self.assertGreater(journal.stat().st_size,512)
            handler=Path(temp)/"never.py"; handler.write_text('def on_change(ctx,changes):\n    raise RuntimeError("Historical handlers must not run")\n')
            with closing(Workbook(path,handler=handler)) as recovered:
                self.assertEqual(recovered.read("Sheet1",{})["cells"][0]["value"],original)
                with self.assertRaises(EngineError) as failure: recovered.operation("interrupted")
                self.assertEqual(failure.exception.body["outcome"],"unknown")
                self.assertEqual(recovered.operation("seed")["outcome"],"committed")

    def test_abrupt_exit_after_commit_retains_receipt_and_stored_input(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"commit.dhwb"
            with closing(Workbook(path,create=True)): pass
            script=Path(temp)/"commit.py"
            script.write_text('''import os,sys
from datahorizon.engine import Workbook
book=Workbook(sys.argv[1])
book.mutate({"action":"edit","operation_id":"ack-lost","expected_state":book.state(),"cells":[{"row":1,"col":1,"source":"7"}]})
os._exit(74)
''')
            result=subprocess.run([sys.executable,str(script),str(path)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,74,result.stderr)
            with closing(Workbook(path)) as recovered:
                self.assertEqual(recovered.operation("ack-lost")["outcome"],"committed")
                self.assertEqual(recovered.read("Sheet1",{})["cells"][0]["value"],7)

    def test_rest_sdk_inspection_is_readonly_has_no_handle_and_activates_no_required_hook(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"inspect.dhwb"
            with closing(Workbook(path,create=True)) as book: edit(book,"00123")
            digest=hashlib.sha256(path.read_bytes()).digest()
            with LocalService(handler=Path(temp)/"missing-required-hook.py") as client:
                result=client.inspect_native(path)
                self.assertTrue(result["readonly"]); self.assertEqual(result["sqlite_integrity"],"ok"); self.assertNotIn("id",result)
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),digest)
                invalid=Path(temp)/"invalid.dhwb"; invalid.write_bytes(b"invalid database")
                with self.assertRaises(APIError): client.inspect_native(invalid)
                self.assertEqual(invalid.read_bytes(),b"invalid database")
                self.assertIn("/v1/recovery/inspect",client.request("/openapi.json")["paths"])
            result=subprocess.run([sys.executable,"-m","datahorizon.cli",str(path),"inspect"],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            import json
            self.assertTrue(json.loads(result.stdout)["readonly"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),digest)
