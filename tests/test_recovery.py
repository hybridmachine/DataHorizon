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

    def test_input_inspection_reports_invalid_types_styles_formula_syntax_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"inputs.dhwb"
            with closing(Workbook(path,create=True)) as book:
                edit(book,"retained")
                identity=book.sheet("Sheet1")
            with closing(sqlite3.connect(path)) as db,db:
                db.executemany("INSERT INTO cells VALUES(?,?,?,?,?,?)",[
                    (identity,2,1,"not-a-number","number","{}"),(identity,3,1,"x","text",'{"bold":"yes"}'),
                    (identity,4,1,"=SUM(","formula","{}"),(identity,5,1,b"bytes","text","{}")])
            original=path.read_bytes(); report=inspect_native(path)
            self.assertEqual(path.read_bytes(),original)
            self.assertTrue(report["inputs"]["complete"]); self.assertFalse(report["inputs"]["inputs_valid"])
            self.assertEqual(report["inputs"]["checked_cells"],5)
            self.assertEqual({i["row"] for i in report["inputs"]["issues"]},{2,3,4,5})
            self.assertEqual(report["inputs"]["issue_count"],4)

    def test_input_inspection_limits_never_claim_unchecked_data_is_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"limit.dhwb"
            with closing(Workbook(path,create=True)) as book:
                identity=book.sheet("Sheet1")
            with closing(sqlite3.connect(path)) as db,db:
                db.executemany("INSERT INTO cells VALUES(?,?,1,'7','number','{}')",[(identity,r) for r in range(1,10002)])
            result=inspect_native(path)["inputs"]
            self.assertFalse(result["complete"]); self.assertFalse(result["inputs_valid"])
            self.assertEqual(result["stop_reason"],"cell_limit"); self.assertEqual(result["checked_cells"],10000)
            with closing(sqlite3.connect(path)) as db,db:
                db.execute("DELETE FROM cells"); db.execute("INSERT INTO cells VALUES(?,1,1,?,'text','{}')",(identity,'x'*(1024*1024+1)))
            result=inspect_native(path)["inputs"]
            self.assertFalse(result["complete"]); self.assertEqual(result["issues"][0]["code"],"INPUT_SCAN_FAILED")

    def test_real_sqlite_full_rolls_back_data_and_retains_unknown_operation_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/"full.dhwb",create=True)) as book:
            edit(book,"retained")
            pages=book.db.execute("PRAGMA page_count").fetchone()[0]
            book.db.execute(f"PRAGMA max_page_count={pages}")
            with self.assertRaises(EngineError) as failure: edit(book,"x"*60000,"disk-full")
            self.assertEqual(failure.exception.body["outcome"],"unknown")
            self.assertFalse(book.db.in_transaction)
            self.assertEqual(book.read("Sheet1",{})["cells"][0]["value"],"retained")
            with self.assertRaises(EngineError) as unknown: book.operation("disk-full")
            self.assertEqual(unknown.exception.body["outcome"],"unknown")
            book.db.execute("PRAGMA max_page_count=2147483646")
            edit(book,"recovered","after-full")
            self.assertEqual(book.operation("after-full")["outcome"],"committed")

    def test_real_readonly_snapshot_destination_failure_retains_source_and_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/"source.dhwb",create=True)) as book:
            edit(book,"retained"); target=Path(temp)/"readonly.dhwb"
            with closing(Workbook(target,create=True)) as existing: edit(existing,"destination")
            original=target.read_bytes(); connect=sqlite3.connect
            def readonly(database,*args,**kwargs):
                return connect(str(database).replace("mode=rw","mode=ro"),*args,**kwargs)
            # The fixture reserves an already existing read-only destination to reach backup itself.
            identity=(target.stat().st_dev,target.stat().st_ino)
            with patch("datahorizon.engine._reserve_destination",return_value=identity),patch("datahorizon.engine.sqlite3.connect",side_effect=readonly):
                with self.assertRaises(sqlite3.OperationalError): book.snapshot(target)
            self.assertEqual(target.read_bytes(),original)
            self.assertEqual(book.read("Sheet1",{})["cells"][0]["value"],"retained")

    def test_snapshot_reservation_permission_and_capacity_failures_create_no_success(self):
        import errno
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/"source.dhwb",create=True)) as book:
            edit(book,"retained")
            for number in (errno.ENOSPC,errno.EACCES):
                target=Path(temp)/str(number)
                with patch("datahorizon.engine.os.open",side_effect=OSError(number,os.strerror(number))):
                    with self.assertRaises(OSError): book.snapshot(target)
                self.assertFalse(target.exists())
            self.assertEqual(book.read("Sheet1",{})["cells"][0]["value"],"retained")

    def test_snapshot_api_explains_incomplete_destination_without_overwriting_existing_file(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/"source.dhwb",create=True) as book:
            target=Path(temp)/"existing.dhwb"; target.write_bytes(b"retain existing destination")
            with self.assertRaises(APIError) as failure: client.request(book.base+"/snapshot",{"path":str(target)})
            self.assertEqual(failure.exception.body["copy_outcome"],"incomplete")
            self.assertTrue(failure.exception.body["destination_requires_inspection"])
            self.assertEqual(target.read_bytes(),b"retain existing destination")
            book.set(1,1,"7")
            self.assertEqual(book.read()["cells"][0]["value"],7)

    def test_input_inspection_byte_and_time_limits_restore_connection_state(self):
        from contextlib import contextmanager
        from datahorizon.recovery import inspect_inputs
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/"source.dhwb",create=True)) as book:
            edit(book,"retained")
            old=book.db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
            with patch("datahorizon.recovery.INSPECTION_BYTES",1): result=inspect_inputs(book)
            self.assertFalse(result["complete"]); self.assertEqual(result["stop_reason"],"byte_limit")
            @contextmanager
            def expired(*args):
                raise EngineError("TIMEOUT","injected")
                yield
            with patch("datahorizon.recovery.sql_deadline",expired): result=inspect_inputs(book)
            self.assertFalse(result["complete"]); self.assertEqual(result["stop_reason"],"time_limit")
            self.assertFalse(book.db.in_transaction); self.assertEqual(book.db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH),old)
            edit(book,"still writable","after-inspect")

    def test_external_commit_between_input_scan_and_metadata_requires_new_inspection(self):
        import datahorizon.recovery as recovery
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"source.dhwb"
            with closing(Workbook(path,create=True)) as book: edit(book,"old")
            scan=recovery.inspect_inputs
            def change_after_scan(book):
                result=scan(book)
                with closing(sqlite3.connect(path)) as db,db: db.execute("UPDATE cells SET source='external'")
                return result
            with patch("datahorizon.recovery.inspect_inputs",side_effect=change_after_scan),self.assertRaises(EngineError) as failure:
                inspect_native(path)
            self.assertEqual(failure.exception.body["code"],"STALE_STATE")
            self.assertTrue(inspect_native(path)["inputs"]["inputs_valid"])
