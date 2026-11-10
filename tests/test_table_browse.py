# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from datahorizon.engine import Workbook,EngineError
from datahorizon.relational import table
from datahorizon.relational_rows import read
from datahorizon.table_schema import preview
from datahorizon.client import LocalService

class TableBrowseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'browse.dhwb';self.book=Workbook(self.path,create=True);self.addCleanup(self.book.close)
        self.apply('table_create',name='Data',columns=[dict(name='Name'),dict(name='Qty',type='integer'),dict(name='Note')]);self.item=table(self.book.db,'Data');self.cols=[col['id'] for col in self.item['columns']]
        with closing(sqlite3.connect(self.path,isolation_level=None)) as db:db.executemany('INSERT INTO Data(Name,Qty,Note) VALUES(?,?,?)',[(None if i%7==0 else 'G'+str(i%4),None if i%9==0 else i%11,'x'*400 if i%10==0 else '') for i in range(603)])
        self.book.info()
    def apply(self,action,**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))
    def read(self,**scope):return read(self.book,dict(table='Data',expected_state=self.book.state(),**scope))
    def test_multikey_null_order_full_scope_and_keyset_pages_match_sqlite(self):
        for first,second in [('asc','asc'),('asc','desc'),('desc','asc'),('desc','desc')]:
            sort=[dict(column=self.cols[0],direction=first),dict(column=self.cols[1],direction=second)];filters=[dict(column=self.cols[1],op='ge',value=2)];ids=[];cursor=None
            while True:
                page=self.read(sort=sort,filters=filters,limit=37,**(dict(cursor=cursor) if cursor else {}));ids.extend(int(r['row_id']) for r in page['records']);cursor=page['next_cursor']
                if not cursor:break
            expected=[r[0] for r in self.book.db.execute(f'SELECT _dh_row_id FROM Data WHERE Qty>=2 ORDER BY Name {first},Qty {second},_dh_row_id')]
            self.assertEqual(ids,expected);self.assertEqual(len(ids),len(set(ids)))
    def test_null_only_no_match_preview_and_full_point_read(self):
        page=self.read(filters=[dict(column=self.cols[1],op='is_null')],count=True,preview=True)
        self.assertEqual(page['count'],67);self.assertEqual(page['count_status'],'exact');self.assertTrue(page['records'][0]['omitted_columns'])
        row=self.read(record_ids=['1']);self.assertEqual(len(row['records'][0]['values'][self.cols[2]]),400)
        empty=self.read(filters=[dict(column=self.cols[1],op='gt',value=99)],count=True);self.assertEqual(empty['records'],[]);self.assertEqual(empty['count'],0)
    def test_forged_stale_or_different_scope_cursors_reject(self):
        page=self.read(limit=2);cursor=page['next_cursor']
        for scope in (dict(cursor=cursor[:-3]+'abc'),dict(cursor=cursor,sort=[dict(column=self.cols[0],direction='asc')]),dict(cursor=cursor,preview=True)):
            with self.assertRaises(EngineError):self.read(**scope)
        self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Qty=5))])
        with self.assertRaises(EngineError) as failure:self.read(cursor=cursor)
        self.assertEqual(failure.exception.body['code'],'STALE_CURSOR')
    def test_long_sort_keys_use_small_authenticated_identity_cursors(self):
        self.apply('table_update',table='Data',edits=[dict(row_id=str(i),values=dict(Name='z'*65536)) for i in range(1,4)])
        sort=[dict(column=self.cols[0],direction='desc')];page=self.read(sort=sort,limit=1,preview=True);self.assertLess(len(page['next_cursor']),1024)
        second=self.read(sort=sort,limit=1,preview=True,cursor=page['next_cursor']);self.assertNotEqual(second['records'][0]['row_id'],page['records'][0]['row_id'])
    def test_native_indexes_preserved_by_identity_rebuild_and_protect_removed_columns(self):
        result=self.apply('index_create',table='Data',name='ByQtyName',columns=[dict(column=self.cols[1],direction='desc'),dict(column=self.cols[0],direction='asc')]);index=result['index']
        columns=[dict(col) for col in self.item['columns']];columns[1]['name']='Amount';review=preview(self.book,dict(table='Data',columns=columns,expected_state=self.book.state()))
        self.apply('table_schema',**review['source_request'],review_digest=review['review_digest']);self.book.validate()
        self.assertIn('"Amount" DESC',self.book.db.execute("SELECT sql FROM sqlite_schema WHERE name='ByQtyName'").fetchone()[0])
        review=preview(self.book,dict(table='Data',columns=columns[:1]+columns[2:],drop_columns=[self.cols[1]],expected_state=self.book.state()))
        with self.assertRaises(EngineError) as failure:self.apply('table_schema',**review['source_request'],review_digest=review['review_digest'])
        self.assertEqual(failure.exception.body['code'],'DEPENDENCY_BLOCKED');self.apply('index_drop',index=index['id']);self.book.validate()
    def test_unique_index_failure_and_cancellation_leave_prior_schema_and_history(self):
        self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Qty=5))]);before=list(self.book.undo)
        with self.assertRaises(EngineError):self.apply('index_create',table='Data',name='BadUnique',columns=[dict(column=self.cols[1],direction='asc')],unique=True)
        self.assertEqual(self.book.undo,before);self.assertEqual(self.book.db.execute("SELECT name FROM sqlite_schema WHERE name='BadUnique'").fetchall(),[])
        event=threading.Event();event.set()
        with self.assertRaises(EngineError):self.book.mutate(dict(action='index_create',table='Data',name='Cancelled',columns=[dict(column=self.cols[0],direction='asc')],operation_id='cancelled',expected_state=self.book.state()),cancel_event=event)
        self.book.validate()
    def test_bad_predicates_and_unloaded_targets_do_not_fall_back(self):
        for scope in (dict(filters=[dict(column=self.cols[1],op='contains',value=3)]),dict(sort=[dict(column='missing',direction='asc')]),dict(record_ids=['99999']),dict(filters=[dict(column=self.cols[1],op='eq',value='3')])):
            with self.assertRaises(EngineError):self.read(**scope)
    def test_count_timeout_is_explicit_and_does_not_discard_the_page(self):
        from datahorizon.engine import sql_deadline
        @contextmanager
        def bounded(db,deadline,**options):
            if deadline-time.monotonic()<.3:raise EngineError('TIMEOUT','Injected count deadline')
            with sql_deadline(db,deadline,**options):yield
        with patch('datahorizon.table_browse.sql_deadline',bounded):result=self.read(count=True)
        self.assertEqual(result['count_status'],'unavailable');self.assertTrue(result['records']);self.assertNotIn('count',result)
    def test_cancel_during_native_index_sql_rolls_back_ddl(self):
        from datahorizon.table_indexes import sql
        with closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) INSERT INTO Data(Name,Qty,Note) SELECT printf('%012d',100000-x),x%13,'' FROM n")
        self.book.info();event=threading.Event();worker=None
        def building(index,target):
            nonlocal worker
            worker=threading.Timer(.001,event.set);worker.start();return sql(index,target)
        with patch('datahorizon.table_indexes.sql',building):
            with self.assertRaises(EngineError) as failure:self.book.mutate(dict(action='index_create',table='Data',name='CancelledDuringSql',columns=[dict(column=self.cols[0],direction='asc')],operation_id='cancel-sql',expected_state=self.book.state()),cancel_event=event)
        if worker:worker.join()
        self.assertEqual(failure.exception.body['code'],'CANCELLED');self.assertEqual(failure.exception.body['outcome'],'rolled_back');self.assertIsNone(self.book.db.execute("SELECT name FROM sqlite_schema WHERE name='CancelledDuringSql'").fetchone());self.book.validate()

class TableBrowseClientTests(unittest.TestCase):
    def test_rest_sdk_indexes_and_predicates_and_cancellation_resource(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            with client.open(Path(temp)/'clients.dhwb',create=True) as book:
                book.create_table('Data',[dict(name='Value',type='integer')]);book.insert_records('Data',[dict(Value=i) for i in range(300)]);col=book.tables()['tables'][0]['columns'][0]['id']
                index=book.create_index('Data','ByValue',[dict(column=col,direction='desc')]);page=book.read_table('Data',sort=[dict(column=col,direction='desc')],count=True,limit=10)
                self.assertEqual(page['count'],300);self.assertEqual(page['records'][0]['values'][col],299);self.assertFalse(book.cancel_operation('nonexistent')['cancel_requested']);book.drop_index(index['index']['id'])
