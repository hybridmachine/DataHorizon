from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from datahorizon.engine import Workbook,EngineError
from datahorizon.client import LocalService
from datahorizon.service import openapi,Service


class OperationJobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'jobs.dhwb';self.book=Workbook(self.path,create=True);self.jobs=self.book.operation_jobs()
    def tearDown(self):self.book.close();self.temp.cleanup()
    def create(self,**kw):return self.jobs.create(dict(job_id=uuid.uuid4().hex,expected_state=self.book.state(),**kw))
    def stage(self,count=3,**kw):
        job=self.create(**kw);self.jobs.append(job['job_id'],[dict(row=i+1,col=1,source=str(i+1)) for i in range(count)]);return self.jobs.seal(job['job_id'])
    def start(self,job,resume=False):return self.jobs.start(job['job_id'],dict(plan_digest=job['plan_digest'],expected_state=self.book.state()),resume)
    def terminal(self,job,stage=False):
        deadline=time.monotonic()+8
        while job['status'] in (('running','staging') if stage else ('running',)) and time.monotonic()<deadline:
            time.sleep(.01);job=self.jobs.view(job['job_id'])
        self.assertNotIn(job['status'],('running','staging'));return job
    def settled(self,identifier):
        for _ in range(400):
            with self.jobs.lock:active=identifier in self.jobs.active
            if not active:return
            time.sleep(.005)
        self.fail('worker did not stop')

    def test_staging_seal_digest_atomic_and_receipt_reopen(self):
        state=self.book.state();job=self.stage();self.assertEqual(job['status'],'prepared');self.assertEqual(self.book.state(),state)
        with self.assertRaises(EngineError):self.jobs.start(job['job_id'],dict(plan_digest='wrong',expected_state=state))
        job=self.terminal(self.start(job));self.assertEqual(job['status'],'completed');self.assertEqual(job['committed_batches'],1)
        self.settled(job['job_id']);self.book.close();self.book=Workbook(self.path);self.jobs=self.book.operation_jobs()
        recovered=self.jobs.view(job['job_id']);self.assertEqual(recovered['batches'][0]['receipt']['outcome'],'committed');self.assertEqual(self.book.read('Sheet1',dict(r2=3))['cells'][2]['value'],3)
        self.assertTrue(self.jobs.release(job['job_id'])['released'])

    def test_cancel_inside_atomic_rolls_back_and_after_last_commit_is_committed(self):
        job=self.stage();original=self.book.put
        def cancel_inside(*args):
            original(*args)
            if args[1]==1:
                with self.jobs.lock:self.jobs.active[job['job_id']][0].set()
        with patch.object(self.book,'put',cancel_inside):job=self.terminal(self.start(job))
        self.assertEqual(job['status'],'cancelled');self.assertEqual(job['batches'][0]['receipt']['outcome'],'rolled_back');self.assertEqual(self.book.read('Sheet1',dict(r2=3))['cells'],[])
        self.settled(job['job_id']);original_commit=self.jobs.committed
        def cancel_after(db,meta,*args):
            original_commit(db,meta,*args)
            with self.jobs.lock:self.jobs.active[job['job_id']][0].set()
        with patch.object(self.jobs,'committed',cancel_after):job=self.terminal(self.start(job,True))
        self.assertEqual(job['status'],'completed');self.assertEqual(job['committed_batches'],1)

    def test_batched_cancel_resume_and_crash_gap_never_replays_committed_batch(self):
        job=self.stage(mode='batches',batch_size=1);original=self.jobs.committed
        def stop_after_first(db,meta,*args):
            original(db,meta,*args)
            with self.jobs.lock:self.jobs.active[job['job_id']][0].set()
        with patch.object(self.jobs,'committed',stop_after_first):job=self.terminal(self.start(job))
        self.assertEqual((job['status'],job['next'],job['committed_batches']),('cancelled',1,1));self.settled(job['job_id'])
        def crash_gap(*args):raise RuntimeError('crash after native receipt before journal acknowledgement')
        with patch.object(self.jobs,'committed',crash_gap):job=self.terminal(self.start(job,True))
        self.assertEqual(job['status'],'failed');self.assertIsNone(job['batches'][-1]['receipt']);self.settled(job['job_id'])
        job=self.terminal(self.start(job,True));self.assertEqual(job['status'],'completed');self.assertEqual(job['committed_batches'],3);self.assertEqual(len(job['batches']),3)
        self.assertEqual(self.book.db.execute('SELECT revision FROM dh_meta').fetchone()[0],3)

    def test_missing_receipt_remains_unknown_and_cannot_release_or_cancel_away(self):
        job=self.stage();identifier=job['job_id']
        with self.jobs.journal(identifier) as (db,meta),db:
            request=dict(action='edit',operation_id='missing',expected_state=self.book.state(),cells=[])
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,NULL)',('missing',0,3,json.dumps(request)));meta['status']='running';self.jobs.save(db,meta)
        with self.assertRaises(EngineError) as error:self.start(job,True)
        self.assertEqual(error.exception.body['code'],'OUTCOME_UNKNOWN');self.assertEqual(self.jobs.cancel(identifier)['status'],'unknown')
        with self.assertRaises(EngineError):self.jobs.release(identifier)
        self.assertEqual(self.book.db.execute('SELECT revision FROM dh_meta').fetchone()[0],0)

    def test_large_csv_stage_and_global_sorted_read_pages_stale_withholding(self):
        source=Path(self.temp.name)/'large.csv';source.write_text(''.join(str(i)+'\n' for i in range(11000,0,-1)))
        job=self.terminal(self.create(mode='batches',source_path=str(source),column_types={'1':'number'}),True)
        self.assertEqual((job['status'],job['total']),('prepared',11000));self.settled(job['job_id']);job=self.terminal(self.start(job));self.assertEqual(job['status'],'completed');self.settled(job['job_id']);self.jobs.release(job['job_id'])
        job=self.create(action='list',r2=11000,sort=[dict(col=1)]);job=self.terminal(self.start(job));self.assertEqual(job['matches'],11000);self.assertEqual([r['row'] for r in job['results'][:3]],[11000,10999,10998]);self.assertEqual(self.jobs.view(job['job_id'],100,2)['results'][0]['row'],10900)
        self.settled(job['job_id']);self.book.mutate(dict(action='edit',operation_id='changed',expected_state=self.book.state(),cells=[dict(row=1,col=1,source='0')]))
        stale=self.jobs.view(job['job_id']);self.assertEqual(stale['status'],'stale');self.assertNotIn('results',stale);self.jobs.release(job['job_id'])
        job=self.create(action='find',r2=11000,needle='10999');job=self.terminal(self.start(job));self.assertEqual(job['results'],[dict(row=2,col=1,text='10999')])

    def test_rest_cancellation_does_not_wait_for_writer_lock(self):
        with Service() as server:
            info=server.dispatch('POST','/v1/open',dict(path=str(Path(self.temp.name)/'cancel.dhwb'),create=True));engine=server.workbooks[info['id']];jobs=engine.operation_jobs();base='/v1/workbooks/'+info['id']+'/operation-jobs'
            job=jobs.create(dict(job_id='rest-cancel',expected_state=engine.state()));jobs.append(job['job_id'],[dict(row=1,col=1,source='1')]);job=jobs.seal(job['job_id'])
            entered=threading.Event();release=threading.Event();returned=threading.Event();original=engine.put
            def blocked(*args):entered.set();release.wait(2);return original(*args)
            def cancel():server.dispatch('POST',base+'/rest-cancel/cancel',{});returned.set()
            with patch.object(engine,'put',blocked):
                server.dispatch('POST',base+'/rest-cancel/start',dict(plan_digest=job['plan_digest'],expected_state=engine.state()));self.assertTrue(entered.wait(1));thread=threading.Thread(target=cancel);thread.start();timely=returned.wait(.25);release.set();thread.join(2)
            self.assertTrue(timely,'Cancel must signal the worker while its transaction owns the writer lock')
            for _ in range(200):
                result=jobs.view(job['job_id'])
                if result['status']!='running':break
                time.sleep(.005)
            self.assertEqual(result['status'],'cancelled');self.assertEqual(result['batches'][0]['receipt']['outcome'],'rolled_back')

    def test_close_cancels_worker_waiting_for_engine_without_post_close_write(self):
        job=self.stage()
        with self.book.lock:
            self.start(job);self.book.close()
        self.settled(job['job_id']);self.assertEqual(self.jobs.view(job['job_id'])['status'],'cancelled')
        self.book=Workbook(self.path);self.jobs=self.book.operation_jobs();self.assertEqual(self.book.read('Sheet1',dict(r2=3))['cells'],[])

    def test_rest_sdk_cli_disk_staging_and_review(self):
        with LocalService() as client,client.open(Path(self.temp.name)/'rest.dhwb',create=True) as book:
            job=book.create_job();identifier=job['job_id'];book.operation_job(identifier,'append',cells=[dict(row=1,col=1,source='00123',kind='text')]);job=book.operation_job(identifier,'seal')
            command=[sys.executable,'-m','datahorizon.cli','--url',client.url,str(Path(self.temp.name)/'rest.dhwb'),'job','start','--job-id',identifier]
            output=subprocess.run(command,input=json.dumps(dict(plan_digest=job['plan_digest'])),capture_output=True,text=True,env={**os.environ,'DATAHORIZON_TOKEN':client.token},timeout=15)
            self.assertEqual(output.returncode,0,output.stderr);self.assertEqual(json.loads(output.stdout)['status'],'completed');self.assertEqual(book.read(r2=1,c2=1)['cells'][0]['value'],'00123')
        self.assertIn('/v1/workbooks/{workbook}/operation-jobs/{job}/resume',openapi()['paths'])
