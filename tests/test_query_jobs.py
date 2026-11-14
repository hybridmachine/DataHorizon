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
from datahorizon.query_sql import bindings,authorize
from datahorizon.query_storage import create
from datahorizon.query_views import preview

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
        # Exercise our 64-KiB scalar bound without relying on SQLite's
        # version-dependent NULL/error behavior for an excessive printf width.
        for sql in ("SELECT x'0102' AS blob",'SELECT 1e999 AS inf','SELECT missing FROM Customers',"SELECT printf('%070000d',1)"):
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
    def save(self,sql,**options):
        review=preview(self.book,dict(name=options.pop('name','Saved '+str(uuid.uuid4())),sql=sql,expected_state=self.book.state(),**options));return self.apply('query_save',**review['source_request'],review_digest=review['review_digest'])['query']
    def once(self,query_id,**options):
        arguments=dict(state=self.book.state(),cancel_event=threading.Event(),seconds=5,max_rows=100);arguments.update(options);return self.jobs.run_once(query_id,**arguments)
    def code(self,call):
        with self.assertRaises(EngineError) as failure:call()
        return failure.exception.body['code'],failure.exception.status
    def test_run_once_returns_columns_and_rows_of_a_saved_query(self):
        item=self.save('SELECT ID,Name,9007199254740993 AS big,NULL AS empty FROM Customers ORDER BY ID')
        self.assertEqual(self.once(item['id']),dict(columns=['ID','Name','big','empty'],rows=[['C001','Ada',dict(type='integer',value='9007199254740993'),None],['C002','Grace',dict(type='integer',value='9007199254740993'),None]]))
    def test_run_once_binds_saved_defaults(self):
        item=self.save('SELECT Customer,Qty FROM Orders WHERE Qty>=:minimum ORDER BY Qty',parameters=[dict(name='minimum',type='integer')],defaults=dict(minimum=2))
        self.assertEqual(self.once(item['id'])['rows'],[['C001',2],['C002',3]])
    def test_run_once_without_a_default_is_invalid_parameters(self):
        item=self.save('SELECT Customer FROM Orders WHERE Qty>=:minimum',parameters=[dict(name='minimum',type='integer')])
        self.assertEqual(self.code(lambda:self.once(item['id'])),('INVALID_PARAMETERS',400))
    def test_run_once_over_max_rows_is_query_limit(self):
        item=self.save('SELECT Customer,Qty FROM Orders');self.assertEqual(len(self.once(item['id'],max_rows=3)['rows']),3)
        with self.assertRaises(EngineError) as failure:self.once(item['id'],max_rows=2)
        self.assertEqual((failure.exception.body['code'],failure.exception.status),('QUERY_LIMIT',409));self.assertIn('2',failure.exception.body['message'])
    def test_run_once_bounds_the_size_held_in_memory(self):
        # An endless result of 1,000-character rows: only a limit can end it, and the memory cap must be the one that does.
        item=self.save("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT x,printf('%01000d',x) AS wide FROM n");made=[]
        def tracked():directory=create();made.append(Path(directory.name));return directory
        self.jobs.run_once_bytes=64*1024
        with patch('datahorizon.query_jobs.create',side_effect=tracked):
            with self.assertRaises(EngineError) as failure:self.once(item['id'],max_rows=1000)
        self.assertEqual((failure.exception.body['code'],failure.exception.status),('QUERY_LIMIT',409));self.assertEqual(failure.exception.body['message'],'Query result exceeds 16 MiB in memory')
        self.assertEqual(len(made),1);self.assertFalse(made[0].exists())
    def test_run_once_max_rows_is_clamped_to_the_query_job_limit(self):
        item=self.save('SELECT Customer,Qty FROM Orders');self.jobs.max_rows=2
        self.assertEqual(self.code(lambda:self.once(item['id'],max_rows=1000)),('QUERY_LIMIT',409));self.jobs.max_rows=3;self.assertEqual(len(self.once(item['id'],max_rows=1000)['rows']),3)
    def test_run_once_requires_the_given_state(self):
        item=self.save('SELECT Name FROM Customers');state=self.book.state();self.apply('table_update',table='Customers',edits=[dict(row_id='1',values=dict(Name='Changed'))])
        self.assertEqual(self.code(lambda:self.once(item['id'],state=state)),('STALE_STATE',409));self.assertEqual(self.once(item['id'])['rows'][0],['Changed'])
        # The snapshot itself also refuses a workbook that moved after preparation.
        original=self.jobs._snapshot
        def moved(entry):
            with closing(sqlite3.connect(self.path,isolation_level=None)) as db:db.execute("UPDATE Customers SET Name='External' WHERE ID='C001'")
            return original(entry)
        with patch.object(self.jobs,'_snapshot',side_effect=moved):self.assertEqual(self.code(lambda:self.once(item['id'])),('STALE_STATE',409))
    def test_run_once_detects_a_changed_dependency(self):
        item=self.save('SELECT Name FROM Customers');from datahorizon import query_views;original=query_views.prepare
        def changed(*args):declarations,deps,shape=original(*args);return declarations,deps,[dict(id='0',name='Renamed')]
        with patch('datahorizon.query_views.prepare',side_effect=changed):self.assertEqual(self.code(lambda:self.once(item['id'])),('QUERY_DEPENDENCY_CHANGED',409))
    def test_run_once_is_cancellable_and_bounded(self):
        item=self.save('WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT max(i) AS top FROM n')
        cancel=threading.Event();timer=threading.Timer(.2,cancel.set);timer.start();self.addCleanup(timer.cancel);started=time.monotonic()
        self.assertEqual(self.code(lambda:self.once(item['id'],cancel_event=cancel,seconds=5,max_rows=1000000)),('CANCELLED',409));self.assertLess(time.monotonic()-started,3)
        started=time.monotonic();self.assertEqual(self.code(lambda:self.once(item['id'],seconds=.2,max_rows=1000000)),('TIMEOUT',409));self.assertLess(time.monotonic()-started,3)
        # A stopping manager cancels a run in progress and refuses new ones.
        outcome=[];runner=threading.Thread(target=lambda:outcome.append(self.code(lambda:self.once(item['id'],seconds=30))[0]));runner.start();deadline=time.monotonic()+5
        while not self.jobs.runs and time.monotonic()<deadline:time.sleep(.005)
        self.assertEqual(len(self.jobs.runs),1);self.jobs.close();runner.join(5)
        self.assertFalse(runner.is_alive());self.assertEqual(outcome,['CANCELLED']);self.assertEqual(self.code(lambda:self.once(item['id'])),('SERVICE_STOPPING',503))
    def test_run_once_stops_at_a_caller_deadline(self):
        item=self.save('WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT max(i) AS top FROM n');started=time.monotonic()
        self.assertEqual(self.code(lambda:self.once(item['id'],seconds=20,deadline=time.monotonic()+.3,max_rows=1000000)),('TIMEOUT',409));self.assertLess(time.monotonic()-started,3)
        with patch.object(self.jobs,'_snapshot',wraps=self.jobs._snapshot) as snapshot:
            self.assertEqual(self.code(lambda:self.once(item['id'],seconds=20,deadline=time.monotonic()-1)),('TIMEOUT',409));snapshot.assert_not_called()
        self.assertEqual(self.jobs.runs,set())
    def test_run_once_honours_cancellation_and_its_deadline_while_waiting_for_the_workbook(self):
        item=self.save('SELECT Name FROM Customers ORDER BY ID');held=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def hold():
            with self.book.lock:held.set();release.wait(4)
        holder=threading.Thread(target=hold);holder.start();self.addCleanup(holder.join);self.assertTrue(held.wait(2))
        cancel=threading.Event();timer=threading.Timer(.2,cancel.set);timer.start();self.addCleanup(timer.cancel);started=time.monotonic()
        self.assertEqual(self.code(lambda:self.once(item['id'],cancel_event=cancel,seconds=5))[0],'CANCELLED');self.assertLess(time.monotonic()-started,2)
        started=time.monotonic();self.assertEqual(self.code(lambda:self.once(item['id'],seconds=5,deadline=time.monotonic()+.2))[0],'TIMEOUT');self.assertLess(time.monotonic()-started,2)
        self.assertEqual(self.jobs.runs,set());release.set();holder.join(5);self.assertEqual(self.once(item['id'])['rows'],[['Ada'],['Grace']])
    def test_run_once_refuses_blob_results(self):
        item=self.save("SELECT x'0102' AS b");self.assertEqual(self.code(lambda:self.once(item['id'])),('QUERY_VALUE',409))
    def test_run_once_uses_no_job_slot_and_leaves_no_files(self):
        item=self.save('SELECT Name FROM Customers ORDER BY ID');made=[]
        def tracked():directory=create();made.append(Path(directory.name));return directory
        for i in range(3):self.run_query('SELECT '+str(i))
        self.jobs.start(dict(job_id='slow',sql='WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT max(i) FROM n',expected_state=self.book.state()))
        with self.assertRaises(EngineError):self.run_query('SELECT 4')
        before={key:entry['status'] for key,entry in self.jobs.entries.items()};self.assertEqual(list(before.values()).count('running'),1)
        with patch('datahorizon.query_jobs.create',side_effect=tracked):
            self.assertEqual(self.once(item['id'])['rows'],[['Ada'],['Grace']]);self.assertEqual(self.code(lambda:self.once(item['id'],max_rows=1)),('QUERY_LIMIT',409))
        self.assertEqual({key:entry['status'] for key,entry in self.jobs.entries.items()},before);self.assertEqual(len(made),2);self.assertFalse(any(path.exists() for path in made))
        self.jobs.access('slow','cancel');self.wait('slow')
    def test_run_once_denies_what_a_query_job_denies(self):
        for sql in ('SELECT * FROM dh_meta','SELECT load_extension(\'anything\')','SELECT * FROM sqlite_schema'):
            with self.subTest(sql=sql):self.assertEqual(self.code(lambda:self.save(sql)),('SQL_DENIED',409))
        item=self.save('SELECT Name FROM Customers');calls=[]
        def nothing_registered(db,registered):calls.append(registered);return authorize(db,[])
        with patch('datahorizon.query_jobs.authorize',side_effect=nothing_registered):self.assertEqual(self.code(lambda:self.once(item['id'])),('SQL_DENIED',403))
        self.assertEqual(len(calls),1);self.assertIn('Customers',[table['name'] for table in calls[0]])
    def test_existing_query_job_results_are_unchanged(self):
        item=self.save('SELECT o.Customer,c.Name,o.Qty*o.Price AS total FROM Orders o JOIN Customers c ON c.ID=o.Customer ORDER BY o.Qty')
        job=self.wait(self.jobs.start(dict(query=item['id'],job_id='saved',expected_state=self.book.state()))['job_id']);self.assertEqual(job['status'],'completed',job);page=self.page(job)
        once=self.once(item['id']);self.assertEqual([col['name'] for col in job['result']['columns']],once['columns']);self.assertEqual(job['result']['row_count'],len(once['rows']));self.assertEqual([row['values'] for row in page['rows']],once['rows'])
        self.assertEqual(once['rows'],[['C001','Ada',10.0],['C001','Ada',25.0],['C002','Grace',21.0]])

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
