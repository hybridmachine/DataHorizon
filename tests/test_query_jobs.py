# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from datahorizon.engine import Workbook,EngineError
from datahorizon.client import LocalService,APIError
from datahorizon.query_sql import bindings

class QueryJobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'queries.dhwb';self.book=Workbook(self.path,create=True);self.addCleanup(self.book.close)
        self.apply('table_create',name='Customers',columns=[dict(name='ID',required=True,unique=True),dict(name='Name')]);self.apply('table_create',name='Orders',columns=[dict(name='Customer'),dict(name='Qty',type='integer'),dict(name='Price',type='number')]);self.apply('table_insert',table='Customers',rows=[dict(ID='C001',Name='Ada'),dict(ID='C002',Name='Grace')]);self.apply('table_insert',table='Orders',rows=[dict(Customer='C001',Qty=2,Price=12.5),dict(Customer='C001',Qty=1,Price=10),dict(Customer='C002',Qty=3,Price=7)]);self.jobs=self.book.query_jobs()
    def apply(self,action,**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))
    def run_query(self,sql,**options):
        request=dict(sql=sql,job_id=str(uuid.uuid4()),expected_state=self.book.state(),**options);start=self.jobs.start(request);return self.wait(start['job_id'])
    def wait(self,job_id):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=self.jobs.access(job_id)
            if result['status']!='running':return result
            time.sleep(.005)
        self.fail('Query did not terminate')
    def page(self,result,**options):return self.jobs.page(result['job_id'],dict(expected_state=result['state'],**options))
    def release(self,result):self.jobs.access(result['job_id'],'release')
    def test_joins_aggregates_ctes_named_typed_parameters_and_dependencies(self):
        sql='WITH totals AS (SELECT c.ID,c.Name,SUM(o.Qty*o.Price) AS Total FROM Customers c JOIN Orders o ON o.Customer=c.ID GROUP BY c.ID,c.Name) SELECT * FROM totals WHERE Total>=:minimum ORDER BY ID'
        result=self.run_query(sql,parameters=[dict(name='minimum',type='number')],values=dict(minimum='0'));self.assertEqual(result['status'],'completed',result);self.assertEqual([r['values'] for r in self.page(result)['rows']],[['C001','Ada',35.0],['C002','Grace',21.0]]);self.assertEqual(len(result['result']['dependencies']),2)
        self.release(result);result=self.run_query(sql,parameters=[dict(name='minimum',type='number')],values=dict(minimum=30));self.assertEqual(len(self.page(result)['rows']),1)
    def test_read_only_authorizer_denies_reserved_objects_writes_and_escape_functions(self):
        statements=['SELECT * FROM dh_meta','SELECT * FROM sqlite_schema','SELECT * FROM cells','SELECT * FROM pragma_table_info(\'Customers\')','SELECT load_extension(\'anything\')','SELECT randomblob(10000000)','WITH rows AS (SELECT 1) DELETE FROM Customers','SELECT 1; SELECT 2','SELECT (SELECT revision FROM dh_meta)','SELECT * FROM main.dh_relational']
        before=self.book.state()
        for sql in statements:
            with self.subTest(sql=sql):
                result=self.run_query(sql);self.assertEqual(result['status'],'failed',result);self.assertNotIn('result',result);self.release(result)
        for sql in ('DELETE FROM Customers','PRAGMA journal_mode=WAL','ATTACH DATABASE \'x\' AS x','BEGIN','EXPLAIN SELECT 1'):
            with self.assertRaises(EngineError):self.run_query(sql)
        self.assertEqual(self.book.state(),before);self.assertEqual(self.book.db.execute('SELECT count(*) FROM Customers').fetchone()[0],2)
    def test_bound_values_cannot_become_sql_and_parameter_lexer_skips_literals_comments(self):
        sql="-- :ignored\n SELECT ':also_ignored',Name FROM Customers WHERE ID=:id /* :ignored_too */"
        result=self.run_query(sql,parameters=[dict(name='id',type='text')],values=dict(id="C001' OR 1=1 --"));self.assertEqual(self.page(result)['rows'],[])
        for declarations,values in (([],{}),([dict(name='extra',type='text')],dict(extra='x')),([dict(name='id',type='integer')],dict(id='bad'))):
            with self.assertRaises(EngineError):bindings('SELECT :id',declarations,values)
    def test_count_star_empty_column_authorization_and_lossless_large_integer_null_labels(self):
        result=self.run_query('SELECT count(*) AS n FROM Customers');self.assertEqual(self.page(result)['rows'][0]['values'],[2]);self.release(result)
        result=self.run_query('SELECT 9223372036854775807 AS x, -9223372036854775808 AS x, NULL AS n, TRUE AS flag');page=self.page(result)
        self.assertEqual([col['name'] for col in page['columns']],['x','x','n','flag']);self.assertEqual(page['rows'][0]['values'],[dict(type='integer',value='9223372036854775807'),dict(type='integer',value='-9223372036854775808'),None,1])
    def test_pages_projection_previews_and_unavailable_results_are_explicit(self):
        result=self.run_query("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<603) SELECT x,printf('%0400d',x) AS text FROM n ORDER BY x")
        first=self.page(result,limit=256,preview=True);second=self.page(result,offset=first['next_offset'],columns=['0'],limit=256);third=self.page(result,offset=second['next_offset'],columns=['0'],limit=256)
        self.assertEqual([first['rows'][0]['values'][0],second['rows'][0]['values'][0],third['rows'][-1]['values'][0]],[1,257,603]);self.assertEqual(first['rows'][0]['omitted_columns'],['1']);self.assertEqual(len(self.page(result,limit=1)['rows'][0]['values'][1]),400)
        with self.assertRaises(EngineError):self.page(result,columns=['missing'])
    def test_invalid_values_spool_limits_and_sql_errors_publish_no_partial_result(self):
        for sql in ("SELECT x'0102' AS blob",'SELECT 1e999 AS inf','SELECT missing FROM Customers',"SELECT printf('%1000000000s','x')"):
            result=self.run_query(sql);self.assertEqual(result['status'],'failed',result);self.assertNotIn('result',result);self.release(result)
        self.jobs.max_rows=2;result=self.run_query('SELECT * FROM Orders');self.assertEqual(result['error']['code'],'QUERY_LIMIT');self.assertNotIn('result',result)
        with self.assertRaises(EngineError):self.page(result)
    def test_new_generation_or_managed_external_change_withholds_prior_results(self):
        result=self.run_query('SELECT * FROM Customers');new=self.run_query('SELECT 1');self.assertEqual(self.jobs.access(result['job_id'])['status'],'stale')
        self.apply('table_update',table='Customers',edits=[dict(row_id='1',values=dict(Name='Changed'))]);self.assertEqual(self.jobs.access(new['job_id'])['status'],'stale')
        with self.assertRaises(EngineError):self.page(new)
        self.release(result);self.release(new);result=self.run_query('SELECT * FROM Customers')
        with closing(sqlite3.connect(self.path,isolation_level=None)) as db:db.execute("UPDATE Customers SET Name='External' WHERE ID='C001'")
        self.assertEqual(self.jobs.access(result['job_id'])['status'],'stale')
    def test_private_snapshot_does_not_hold_live_engine_while_query_executes(self):
        entered=threading.Event();resume=threading.Event();original=self.jobs._snapshot
        def snapshot(entry):
            result=original(entry);entered.set();self.assertTrue(resume.wait(3));return result
        with patch.object(self.jobs,'_snapshot',side_effect=snapshot):
            result=self.jobs.start(dict(job_id='snapshot',sql='SELECT Name FROM Customers',expected_state=self.book.state()));self.assertTrue(entered.wait(3));started=time.monotonic();self.apply('table_update',table='Customers',edits=[dict(row_id='1',values=dict(Name='New'))]);self.assertLess(time.monotonic()-started,1);resume.set();result=self.wait(result['job_id'])
        self.assertEqual(result['status'],'stale');self.assertNotIn('result',result)
    def test_cancellation_timeout_release_and_storage_cleanup(self):
        sql='WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n'
        result=self.jobs.start(dict(job_id='slow',sql=sql,expected_state=self.book.state()));time.sleep(.03);self.jobs.access('slow','cancel');result=self.wait('slow');self.assertEqual(result['status'],'cancelled',result);self.assertFalse(Path(self.jobs.entries['slow']['directory'].name).exists());self.release(result)
        self.jobs.seconds=.01;result=self.run_query(sql);self.assertEqual(result['error']['code'],'TIMEOUT');self.release(result)
        self.jobs.snapshot_seconds=-1;result=self.run_query('SELECT 1');self.assertEqual(result['error']['code'],'TIMEOUT')
    def test_capacity_deduplication_expiry_and_thread_start_failure(self):
        request=dict(job_id='same',sql='SELECT 1',expected_state=self.book.state());self.jobs.start(request);self.wait('same');self.assertEqual(self.jobs.start(request)['job_id'],'same')
        with self.assertRaises(EngineError):self.jobs.start({**request,'sql':'SELECT 2'})
        for i in range(3):self.run_query('SELECT '+str(i))
        with self.assertRaises(EngineError):self.run_query('SELECT 4')
        self.jobs.retention=-1
        with self.assertRaises(EngineError):self.jobs.access('same')
        self.jobs.retention=300
        with patch('datahorizon.query_jobs.threading.Thread.start',side_effect=RuntimeError('start failed')):
            with self.assertRaises(RuntimeError):self.run_query('SELECT 4')
        self.assertEqual(self.jobs.entries,{})
    def test_close_cancels_worker_waiting_for_book_ownership_without_deadlock(self):
        with self.book.lock:
            self.jobs.start(dict(job_id='close',sql='SELECT 1',expected_state=self.book.state()));started=time.monotonic();self.jobs.close();self.assertLess(time.monotonic()-started,1)
        self.assertEqual(self.jobs.entries,{})
    def test_wide_native_schema_can_be_projected_with_a_separate_result_width_limit(self):
        self.apply('table_create',name='Wide',columns=[dict(name='C'+str(i)) for i in range(256)])
        result=self.run_query('SELECT C255 FROM Wide');self.assertEqual(result['status'],'completed',result);self.release(result)
        result=self.run_query('SELECT * FROM Wide');self.assertEqual(result['status'],'failed');self.assertEqual(result['error']['code'],'QUERY_LIMIT')
    def test_disk_full_result_write_fails_without_exposing_partial_rows(self):
        connect=sqlite3.connect
        class Full:
            def __init__(self,db):self.db=db
            def execute(self,sql,*args):
                if sql.startswith('INSERT INTO result'):
                    error=sqlite3.OperationalError('database or disk is full');error.sqlite_errorcode=sqlite3.SQLITE_FULL;raise error
                return self.db.execute(sql,*args)
            def close(self):self.db.close()
            def commit(self):self.db.commit()
        def limited(path,*args,**kwargs):
            db=connect(path,*args,**kwargs);return Full(db) if str(path).endswith('result.sqlite') else db
        with patch('datahorizon.query_jobs.sqlite3.connect',side_effect=limited):result=self.run_query('SELECT * FROM Customers')
        self.assertEqual(result['status'],'failed');self.assertEqual(result['error']['code'],'QUERY_LIMIT');self.assertNotIn('result',result)

class QueryClientTests(unittest.TestCase):
    def test_rest_sdk_cli_run_status_page_release_and_openapi(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'clients.dhwb'
            with client.open(path,create=True) as book:
                book.create_table('Data',[dict(name='Value',type='integer')]);book.insert_records('Data',[dict(Value=i) for i in range(300)]);result=book.run_query('SELECT Value FROM Data ORDER BY Value')
                deadline=time.monotonic()+5
                while result['status']=='running' and time.monotonic()<deadline:time.sleep(.01);result=book.query(result['job_id'])
                self.assertEqual(result['result']['row_count'],300);self.assertEqual(book.query(result['job_id'],'page',offset=256)['rows'][0]['values'],[256]);book.query(result['job_id'],'release')
                api=client.request('/openapi.json');self.assertIn('/v1/workbooks/{workbook}/queries/jobs/{job}/page',api['paths'])
            completed=subprocess.run([sys.executable,'-m','datahorizon.cli',str(path),'query','run'],input=json.dumps(dict(sql='SELECT Value FROM Data ORDER BY Value')),text=True,capture_output=True,check=True);result=json.loads(completed.stdout);self.assertEqual(len(result['rows']),300);self.assertEqual(result['rows'][-1]['values'],[299])
