# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from datahorizon.calculation_results import CollectingCalculator
from datahorizon.engine import Workbook,EngineError,encoded
from datahorizon.client import LocalService
from datahorizon.service import openapi


class CalculationJobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.book=Workbook(Path(self.temp.name)/'calc.dhwb',create=True)
        self.edit('2');self.jobs=self.book.calculation_jobs()
    def tearDown(self):self.book.close();self.temp.cleanup()
    def edit(self,source):return self.book.mutate(dict(action='edit',operation_id=str(uuid.uuid4()),expected_state=self.book.state(),cells=[dict(row=1,col=1,source=source),dict(row=2,col=1,source='=A1*2')]))
    def start(self):return self.jobs.start(dict(job_id=str(uuid.uuid4()),expected_state=self.book.state(),r2=2))
    def terminal(self,job):
        deadline=time.monotonic()+4
        while job['status']=='running' and time.monotonic()<deadline:time.sleep(.01);job=self.jobs.access(job['job_id'])
        self.assertNotEqual(job['status'],'running');return job

    def test_snapshot_generation_cached_pending_and_read_only_outcomes(self):
        state=self.book.state();cells=self.book.read('Sheet1',dict(r2=2,calculation='cached'))['cells'];self.assertEqual(cells[1]['calculation'],'pending')
        first=self.terminal(self.start());self.assertEqual(first['calculation'],'current');self.assertEqual(first['result']['cells'][1]['value'],4)
        duplicate=self.jobs.start(dict(job_id=first['job_id'],expected_state=state,r2=2))
        self.assertEqual(duplicate['generation'],first['generation']);self.assertEqual(len(self.jobs.jobs.entries),1)
        second=self.terminal(self.start());self.assertGreater(second['generation'],first['generation']);self.assertEqual(self.jobs.access(first['job_id'])['status'],'stale')
        self.assertEqual(self.book.state(),state)
        self.assertTrue(self.jobs.access(first['job_id'],'release')['released'])
        cancelled=self.jobs.access(second['job_id'],'cancel');self.assertEqual(cancelled['status'],'cancelled');self.assertNotIn('result',cancelled)

    def test_late_snapshot_after_edit_is_stale_and_withholds_result(self):
        entered=threading.Event();resume=threading.Event();read=Workbook.read
        def delayed(book,*args,**kwargs):
            if book.readonly:entered.set();resume.wait(3)
            return read(book,*args,**kwargs)
        with patch.object(Workbook,'read',delayed):
            job=self.start();self.assertTrue(entered.wait(2));self.edit('9');resume.set();job=self.terminal(job)
        self.assertEqual(job['status'],'stale');self.assertNotIn('result',job)
        self.assertEqual(self.book.read('Sheet1',dict(r1=2,r2=2))['cells'][0]['value'],18)

    def test_cancellation_before_publication(self):
        entered=threading.Event();resume=threading.Event();read=Workbook.read
        def delayed(book,*args,**kwargs):
            if book.readonly:entered.set();resume.wait(3)
            return read(book,*args,**kwargs)
        with patch.object(Workbook,'read',delayed):
            job=self.start();self.assertTrue(entered.wait(2));self.jobs.access(job['job_id'],'cancel');resume.set();job=self.terminal(job)
        self.assertEqual(job['status'],'cancelled');self.assertNotIn('result',job)

    def test_rest_sdk_and_openapi(self):
        with LocalService() as client,client.open(Path(self.temp.name)/'rest.dhwb',create=True) as book:
            book.set(1,1,'=DATE(2024,2,29)');job=book.calculate(r2=1,c2=1)
            deadline=time.monotonic()+4
            while job['status']=='running' and time.monotonic()<deadline:time.sleep(.01);job=book.calculation(job['job_id'])
            self.assertEqual(job['result']['cells'][0]['value_kind'],'date');self.assertTrue(book.calculation(job['job_id'],'release')['released'])
        self.assertIn('/v1/workbooks/{workbook}/calculation/jobs',openapi()['paths'])

    def test_independent_viewport_stream_does_not_supersede_explicit_calculation(self):
        manual=self.terminal(self.start())
        viewport=self.terminal(self.jobs.start(dict(job_id='viewport',stream_id='viewport-one',expected_state=self.book.state(),r2=2,preview=True)))
        self.assertEqual(viewport['stream_id'],'viewport-one')
        self.assertEqual(self.jobs.access(manual['job_id'])['status'],'completed')
        later=self.terminal(self.jobs.start(dict(job_id='viewport-next',stream_id='viewport-one',expected_state=self.book.state(),r2=2,preview=True)))
        self.assertGreater(later['generation'],viewport['generation'])
        self.assertEqual(self.jobs.access(viewport['job_id'])['status'],'stale')
        self.assertEqual(self.jobs.access(manual['job_id'])['status'],'completed')
        for value in ('',None,True,'x'*65,'spaces are invalid'):
            with self.assertRaises(EngineError):self.jobs.start(dict(job_id='invalid-stream',stream_id=value,expected_state=self.book.state()))

    def test_pending_formula_previews_bound_sources_before_response_accounting(self):
        sheet=self.book.sheet('Sheet1');source='="'+'x'*65000+'"'
        self.book.db.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,2,?,'formula')",((sheet,row,source) for row in range(1,101)))
        before=self.book.state()
        result=self.book.read('Sheet1',dict(r2=100,c1=2,c2=2,calculation='cached',preview=True))
        self.assertEqual(len(result['cells']),100)
        self.assertTrue(all(cell['calculation']=='pending' and cell['source_omitted'] and len(cell['source'])==128 for cell in result['cells']))
        self.assertLess(len(encoded(result).encode()),100000)
        with self.assertRaises(EngineError) as error:self.book.read('Sheet1',dict(r2=100,c1=2,c2=2,calculation='cached'))
        self.assertEqual(error.exception.body['code'],'READ_LIMIT');self.assertEqual(self.book.state(),before)

    def test_finished_job_stores_its_results(self):
        self.book.mutate(dict(action='edit',operation_id=str(uuid.uuid4()),expected_state=self.book.state(),
                              cells=[dict(row=1,col=1,source='2'),dict(row=1,col=2,source='=A1*3')]))
        sheet=self.book.sheet('Sheet1')
        b1=(sheet,1,2)
        before=self.book.read('Sheet1',dict(r1=1,r2=1,c1=2,c2=2,calculation='cached'))['cells'][0]
        self.assertEqual(before['calculation'],'pending')

        job=self.terminal(self.jobs.start(dict(job_id=str(uuid.uuid4()),expected_state=self.book.state(),r1=1,c1=1,r2=1,c2=2)))
        self.assertEqual(job['status'],'completed')
        after=self.book.read('Sheet1',dict(r1=1,r2=1,c1=2,c2=2,calculation='cached'))['cells'][0]
        self.assertEqual((after['calculation'],after['value']),('current',6))
        self.assertEqual(self.book.store.result(b1),(True,6))

        self.book.close()
        self.book=Workbook(Path(self.temp.name)/'calc.dhwb')
        self.jobs=self.book.calculation_jobs()
        reopened=self.book.read('Sheet1',dict(r1=1,r2=1,c1=2,c2=2,calculation='cached'))['cells'][0]
        self.assertEqual((reopened['calculation'],reopened['value']),('current',6))

    def test_job_batch_is_dropped_after_a_commit(self):
        self.book.mutate(dict(action='edit',operation_id=str(uuid.uuid4()),expected_state=self.book.state(),
                              cells=[dict(row=1,col=1,source='2'),dict(row=1,col=2,source='=A1*3')]))
        sheet=self.book.sheet('Sheet1')
        b1=(sheet,1,2)
        evaluated=threading.Event();entered_offer=threading.Event();resume=threading.Event()
        real_eval=CollectingCalculator._evaluate_cell
        real_offer=self.jobs.offer

        def watched_eval(calc_self,key):
            if key==b1:
                evaluated.set()
            return real_eval(calc_self,key)

        def held_offer(state,batch):
            entered_offer.set()
            resume.wait(3)
            return real_offer(state,batch)

        with patch.object(CollectingCalculator,'_evaluate_cell',watched_eval), patch.object(self.jobs,'offer',held_offer):
            job=self.jobs.start(dict(job_id=str(uuid.uuid4()),expected_state=self.book.state(),r1=1,c1=1,r2=1,c2=2))
            self.assertTrue(evaluated.wait(2))
            self.assertTrue(entered_offer.wait(2))
            self.book.mutate(dict(action='edit',operation_id=str(uuid.uuid4()),expected_state=self.book.state(),
                                  cells=[dict(row=1,col=1,source='9')]))
            resume.set()
            job=self.terminal(job)

        self.assertEqual(job['status'],'stale')
        self.assertEqual(self.book.store.result(b1),(False,None))
        self.assertEqual(self.book.read('Sheet1',dict(r1=1,r2=1,c1=2,c2=2))['cells'][0]['value'],27)

    def test_job_batch_is_dropped_after_a_direct_edit(self):
        self.book.mutate(dict(action='edit',operation_id=str(uuid.uuid4()),expected_state=self.book.state(),
                              cells=[dict(row=1,col=1,source='2'),dict(row=1,col=2,source='=A1*3')]))
        sheet=self.book.sheet('Sheet1')
        b1=(sheet,1,2)
        evaluated=threading.Event();entered_offer=threading.Event();resume=threading.Event()
        real_eval=CollectingCalculator._evaluate_cell
        real_offer=self.jobs.offer

        def watched_eval(calc_self,key):
            if key==b1:
                evaluated.set()
            return real_eval(calc_self,key)

        def held_offer(state,batch):
            entered_offer.set()
            resume.wait(3)
            return real_offer(state,batch)

        with patch.object(CollectingCalculator,'_evaluate_cell',watched_eval), patch.object(self.jobs,'offer',held_offer):
            job=self.jobs.start(dict(job_id=str(uuid.uuid4()),expected_state=self.book.state(),r1=1,c1=1,r2=1,c2=2))
            self.assertTrue(evaluated.wait(2))
            self.assertTrue(entered_offer.wait(2))
            with closing(sqlite3.connect(self.book.path)) as direct, direct:
                direct.execute("UPDATE cells SET source='9' WHERE sheet_id=? AND row=1 AND col=1",(sheet,))
            resume.set()
            job=self.terminal(job)

        self.assertEqual(job['status'],'stale')
        self.assertEqual(self.book.store.result(b1),(False,None))
        self.assertEqual(self.book.read('Sheet1',dict(r1=1,r2=1,c1=2,c2=2))['cells'][0]['value'],27)

    def test_job_response_is_unchanged(self):
        job=self.terminal(self.start())
        self.assertEqual(job['status'],'completed')
        self.assertEqual(set(job.keys()),{'calculation','clock','completed','generation','job_id','phase','result','state','status','stream_id'})
        self.assertEqual(set(job['result'].keys()),{'calculation','cells','clock','generation','range','sheet','state'})

    def test_volatile_tile_stores_nothing(self):
        self.book.mutate(dict(action='edit',operation_id=str(uuid.uuid4()),expected_state=self.book.state(),
                              cells=[dict(row=1,col=1,source='=NOW()'),dict(row=2,col=1,source='=A1')]))
        job=self.terminal(self.start())
        self.assertEqual(job['status'],'completed')
        self.assertEqual(self.book.store.formula_count(),0)
