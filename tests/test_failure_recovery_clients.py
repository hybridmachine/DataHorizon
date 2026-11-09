"""Real filesystem/process failures through managed clients and native checkpoints."""
from contextlib import closing
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from datahorizon.client import APIError,Client,LocalService,UnknownOutcome
from datahorizon.engine import EngineError,Workbook,check_integrity

COUNTER='''def on_change(ctx,changes):
    for change in changes:
        if (change['row'],change['col'])==(1,1):
            sheet=change['sheet']
            ctx.set(sheet,1,2,str((ctx.value(sheet,1,2) or 0)+1))
'''

CRASH_SERVICE='''import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from datahorizon.service import Service
class CrashService(Service):
    def dispatch(self,method,path,body):
        if method=='POST' and path.endswith('/operations') and body.get('operation_id')=='crash-before':
            book=self.workbooks[path.split('/')[3]]
            book.db.execute('PRAGMA cache_size=1');book.db.execute('PRAGMA cache_spill=ON')
            put=book.put
            def crash(*args):put(*args);os._exit(72)
            book.put=crash
        result=super().dispatch(method,path,body)
        if method=='POST' and path.endswith('/operations') and body.get('operation_id')=='crash-after':os._exit(71)
        return result
if __name__=='__main__':
    server=CrashService(handler=Path(sys.argv[2]))
    print(json.dumps(dict(url='http://127.0.0.1:'+str(server.server_port),token=server.token)),flush=True)
    try:server.serve_forever()
    finally:server.server_close()
'''


class FailureRecoveryClientTests(unittest.TestCase):
    def test_native_open_and_snapshot_validation_observe_cancellation_without_publishing_success(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'source.dhwb'
            with closing(Workbook(path,create=True)) as book:
                book.mutate(dict(action='edit',operation_id='seed',expected_state=book.state(),cells=[dict(row=1,col=1,source='7')]))
            original=path.read_bytes();cancel=threading.Event();cancel.set()
            with self.assertRaises(EngineError) as failure:Workbook(path,readonly=True,cancel_event=cancel)
            self.assertEqual(failure.exception.body['code'],'CANCELLED');self.assertEqual(path.read_bytes(),original)
            cancel.clear();connect=sqlite3.connect;connections=[]
            def cancelling(*args,**kwargs):
                db=connect(*args,**kwargs);connections.append(db)
                db.set_trace_callback(lambda sql:cancel.set() if sql.startswith('PRAGMA quick_check') else None)
                return db
            with patch('datahorizon.engine.sqlite3.connect',side_effect=cancelling):
                with self.assertRaises(EngineError) as failure:Workbook(path,readonly=True,cancel_event=cancel)
            self.assertEqual(failure.exception.body['code'],'CANCELLED')
            with self.assertRaises(sqlite3.ProgrammingError):connections[0].execute('SELECT 1')
            cancel.clear()
            with closing(Workbook(path,readonly=True,cancel_event=cancel)) as book:
                cancel.set()
                with self.assertRaises(EngineError) as failure:book.read('Sheet1',{})
                self.assertEqual(failure.exception.body['code'],'CANCELLED');self.assertFalse(book.db.in_transaction)
                cancel.clear();self.assertEqual(book.read('Sheet1',{})['cells'][0]['value'],7)
            with closing(Workbook(path)) as book:
                validate=book.validate
                def cancelled(*args,**kwargs):validate(*args,**kwargs);cancel.set()
                target=Path(temp)/'copy.dhwb'
                with patch.object(book,'validate',side_effect=cancelled):
                    with self.assertRaises(EngineError) as failure:book.snapshot(target,cancel_event=cancel)
                self.assertEqual(failure.exception.body['code'],'CANCELLED');self.assertTrue(target.exists())
            self.assertEqual(path.read_bytes(),original)
            with closing(Workbook(target,readonly=True)) as copied:self.assertEqual(copied.read('Sheet1',{})['cells'][0]['value'],7)

    def test_calculation_cancel_during_open_skips_remaining_integrity_work(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'source.dhwb',create=True)) as book:
            entered,resume=threading.Event(),threading.Event();validate=Workbook.validate
            def blocked(target,*args,**kwargs):
                if target.readonly:entered.set();resume.wait(3)
                return validate(target,*args,**kwargs)
            jobs=book.calculation_jobs()
            with patch.object(Workbook,'validate',blocked),patch('datahorizon.engine.check_integrity',wraps=check_integrity) as integrity:
                job=jobs.start(dict(job_id='cancel-opening',expected_state=book.state()))
                self.assertTrue(entered.wait(2));jobs.access(job['job_id'],'cancel');resume.set()
                deadline=time.monotonic()+4
                while job['status']=='running' and time.monotonic()<deadline:time.sleep(.01);job=jobs.access(job['job_id'])
                self.assertEqual(job['status'],'cancelled');self.assertNotIn('result',job);self.assertEqual(integrity.call_count,0)

    @unittest.skipUnless(os.name=='posix' and os.geteuid()!=0,'Requires real unprivileged POSIX permission enforcement')
    def test_actual_directory_permission_loss_keeps_unknown_id_and_transactional_hook_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);directory=root/'native';directory.mkdir();path=directory/'book.dhwb';hook=root/'counter.py';hook.write_text(COUNTER)
            owner=LocalService(hook)
            try:
                book=owner.client.open(path,create=True);book.set(1,1,'7');original=path.read_bytes();directory.chmod(0o500)
                try:
                    with self.assertRaises(UnknownOutcome):book.apply('edit',operation_id='permission-loss',cells=[dict(row=1,col=1,source='9')])
                finally:directory.chmod(0o700)
                self.assertEqual(book.pending_operation,'permission-loss');self.assertEqual(path.read_bytes(),original)
                self.assertEqual([cell['value'] for cell in book.read(c2=2)['cells']],[7,1])
                with self.assertRaises(UnknownOutcome):book.outcome('permission-loss')
                with self.assertRaises(UnknownOutcome):book.set(1,1,'unreviewed retry')
                self.assertEqual(book.pending_operation,'permission-loss')
            finally:directory.chmod(0o700);owner.close()

    @unittest.skipUnless(os.name=='posix' and os.geteuid()!=0,'Requires real unprivileged POSIX permission enforcement')
    def test_actual_sidecar_permission_loss_preserves_stage_and_allows_explicit_reviewed_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'book.dhwb';hook=root/'counter.py';hook.write_text(COUNTER)
            with LocalService(hook) as client,client.open(path,create=True) as book:
                book.set(1,1,'7');state=book.state;original=path.read_bytes();job=book.create_job(job_id='permission-stage')
                directory=root/('.'+path.name+'.dhjobs');directory.chmod(0o500)
                try:
                    with self.assertRaises(APIError) as failure:book.operation_job(job['job_id'],'append',cells=[dict(row=1,col=1,source='9')])
                    self.assertEqual(failure.exception.body['code'],'STORAGE_ERROR')
                finally:directory.chmod(0o700)
                current=book.operation_job(job['job_id']);self.assertEqual(current['status'],'staging');self.assertEqual(current['total'],0)
                self.assertEqual(book.state,state);self.assertEqual(path.read_bytes(),original)
                book.operation_job(job['job_id'],'append',cells=[dict(row=1,col=1,source='9')]);job=book.operation_job(job['job_id'],'seal')
                job=book.operation_job(job['job_id'],'start',plan_digest=job['plan_digest'],expected_state=book.state)
                deadline=time.monotonic()+4
                while job['status']=='running' and time.monotonic()<deadline:time.sleep(.01);job=book.operation_job(job['job_id'])
                self.assertEqual(job['status'],'completed');self.assertEqual([cell['value'] for cell in book.read(c2=2)['cells']],[9,2])
                book.operation_job(job['job_id'],'release')

    def test_service_process_death_before_and_after_commit_retains_truthful_client_outcomes(self):
        for moment in ('before','after'):
            with self.subTest(moment=moment),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);path=root/'book.dhwb';hook=root/'counter.py';hook.write_text(COUNTER);script=root/'crash-service.py';script.write_text(CRASH_SERVICE)
                service=Path(__file__).resolve().parents[1]/'service'
                with tempfile.TemporaryFile(mode='w+t') as diagnostics:
                    child=subprocess.Popen([sys.executable,'-I','-B',str(script),str(service),str(hook)],stdout=subprocess.PIPE,stderr=diagnostics,text=True)
                    startup=queue.Queue();reader=threading.Thread(target=lambda:startup.put(child.stdout.readline()),daemon=True);reader.start()
                    try:
                        info=json.loads(startup.get(timeout=10));client=Client(info['url'],info['token']);book=client.open(path,create=True);seed=book.set(1,1,'7')
                        identifier='crash-'+moment
                        with self.assertRaises(UnknownOutcome):book.apply('edit',operation_id=identifier,cells=[dict(row=1,col=1,source='x'*60000 if moment=='before' else '9')])
                        self.assertEqual(book.pending_operation,identifier);self.assertEqual(child.wait(timeout=5),72 if moment=='before' else 71)
                        with self.assertRaises(UnknownOutcome):book.set(1,1,'must not retry')
                        if moment=='before':self.assertGreater(Path(str(path)+'-journal').stat().st_size,512)
                        with LocalService(hook) as recovered,recovered.open(path) as reopened:
                            self.assertEqual([cell['value'] for cell in reopened.read(c2=2)['cells']],[7,1] if moment=='before' else [9,2])
                            if moment=='before':
                                with self.assertRaises(UnknownOutcome):reopened.outcome(identifier)
                            else:self.assertEqual(reopened.outcome(identifier)['outcome'],'committed')
                            self.assertEqual(reopened.outcome(seed['operation_id'])['outcome'],'committed')
                    finally:
                        if child.poll() is None:
                            child.terminate()
                            try:child.wait(timeout=3)
                            except subprocess.TimeoutExpired:child.kill();child.wait()
                        reader.join(timeout=1);child.stdout.close()
