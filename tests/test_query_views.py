# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
import subprocess
import sys
from datahorizon.engine import Workbook,EngineError
from datahorizon.query_views import preview,read,queries
from datahorizon.table_schema import preview as schema_preview
from datahorizon.client import LocalService

class QueryViewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'views.dhwb';self.book=Workbook(self.path,create=True);self.addCleanup(lambda:self.book.close())
        self.apply('table_create',name='Data',columns=[dict(name='Key',required=True,unique=True),dict(name='Value',type='integer'),dict(name='Note')]);self.apply('table_insert',table='Data',rows=[dict(Key='A',Value=2),dict(Key='B',Value=4)])
    def apply(self,action,**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))
    def save(self,sql='SELECT Key,Value FROM Data ORDER BY Key',**options):
        review=preview(self.book,dict(name=options.pop('name','Saved view'),sql=sql,expected_state=self.book.state(),**options));return self.apply('query_save',**review['source_request'],review_digest=review['review_digest'])['query']
    def wait(self,result):
        deadline=time.monotonic()+4
        while result['status']=='running' and time.monotonic()<deadline:time.sleep(.005);result=self.book.query_jobs().access(result['job_id'])
        self.assertEqual(result['status'],'completed',result);return result
    def test_native_save_copy_reopen_rename_delete_without_running_definition(self):
        started=time.monotonic();item=self.save('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n; -- end')
        self.assertLess(time.monotonic()-started,1);self.assertIsNone(self.book._query_jobs);self.assertEqual(self.book.info()['queries'][0]['id'],item['id']);self.assertEqual(read(self.book,dict(query=item['id'],expected_state=self.book.state()))['query']['sql'],item['sql'])
        copy=Path(self.temp.name)/'copy.dhwb';self.book.snapshot(copy)
        with closing(Workbook(copy,readonly=True)) as reopened:self.assertEqual(queries(reopened.db)[0]['id'],item['id']);self.assertIsNone(reopened._query_jobs)
        self.apply('query_rename',query=item['id'],name='Renamed view');self.assertEqual(queries(self.book.db)[0]['name'],'Renamed view');self.apply('query_delete',query=item['id']);self.assertEqual(queries(self.book.db),[])
    def test_duplicate_labels_and_existing_limit_parameters_preserve_actual_shape(self):
        sql="SELECT Value AS x, Value AS x, ':limit' AS literal FROM Data WHERE Key<>:key LIMIT :limit OFFSET :skip"
        item=self.save(sql,parameters=[dict(name='key',type='text'),dict(name='limit',type='integer'),dict(name='skip',type='integer')],defaults=dict(key='none',limit=2,skip=0))
        self.assertEqual([c['name'] for c in item['shape']],['x','x','literal']);result=self.wait(self.book.query_jobs().start(dict(query=item['id'],job_id='shape',expected_state=self.book.state())))
        self.assertEqual(result['result']['row_count'],2)
    def test_default_bindings_are_explicit_and_invocations_do_not_persist(self):
        item=self.save('SELECT Key FROM Data WHERE Value>=:minimum ORDER BY Key',parameters=[dict(name='minimum',type='integer')],defaults=dict(minimum=3))
        result=self.wait(self.book.query_jobs().start(dict(query=item['id'],values=dict(minimum=0),job_id='invoke',expected_state=self.book.state())))
        self.assertEqual(result['result']['row_count'],2);self.assertEqual(queries(self.book.db)[0]['defaults'],dict(minimum=3));self.book.query_jobs().access('invoke','release')
        result=self.wait(self.book.query_jobs().start(dict(query=item['id'],job_id='default',expected_state=self.book.state())));self.assertEqual(result['result']['row_count'],1)
        with self.assertRaises(EngineError):self.book.query_jobs().start(dict(query=item['id'],sql='SELECT 1',job_id='mixed',expected_state=self.book.state()))
    def test_dependency_blocking_retained_ids_and_unreferenced_column_change(self):
        item=self.save();table=self.book.db.execute("SELECT payload FROM dh_relational WHERE kind='table'").fetchone();original=json.loads(table[0]);columns=[dict(c) for c in original['columns']]
        for target in ('Renamed','Data'):
            definition=[dict(c) for c in columns]
            if target=='Data':definition[1]['name']='Amount'
            review=schema_preview(self.book,dict(table='Data',name=target,columns=definition,expected_state=self.book.state()))
            with self.assertRaises(EngineError) as failure:self.apply('table_schema',**review['source_request'],review_digest=review['review_digest'])
            self.assertEqual(failure.exception.body['code'],'DEPENDENCY_BLOCKED')
        with self.assertRaises(EngineError):self.apply('table_drop',table='Data',allow_data_loss=True)
        columns[2]['name']='Unreferenced';review=schema_preview(self.book,dict(table='Data',columns=columns,expected_state=self.book.state()));self.apply('table_schema',**review['source_request'],review_digest=review['review_digest']);self.book.validate();self.assertEqual(queries(self.book.db)[0]['id'],item['id'])
    def test_select_star_and_cte_dependencies_block_new_columns_and_removal(self):
        item=self.save('WITH c AS (SELECT * FROM Data) SELECT * FROM c');original=self.book.db.execute("SELECT payload FROM dh_relational WHERE kind='table'").fetchone();columns=json.loads(original[0])['columns'];columns.append(dict(name='Extra'))
        review=schema_preview(self.book,dict(table='Data',columns=columns,expected_state=self.book.state()))
        with self.assertRaises(EngineError) as failure:self.apply('table_schema',**review['source_request'],review_digest=review['review_digest'])
        # SQLite expands * into precise referenced column IDs. Appended columns
        # also alter result shape and are blocked by preparation comparison.
        self.assertIn(failure.exception.body['code'],('DEPENDENCY_BLOCKED','QUERY_DEPENDENCY_CHANGED'))
    def test_stale_and_invalid_reviews_and_required_hook_rollback(self):
        review=preview(self.book,dict(name='View',sql='SELECT Value FROM Data',expected_state=self.book.state()));self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Value=3))])
        with self.assertRaises(EngineError):self.apply('query_save',**review['source_request'],review_digest=review['review_digest'])
        for sql in ('SELECT * FROM dh_meta','SELECT 1; DELETE FROM Data','SELECT load_extension(\'x\')'):
            with self.assertRaises(EngineError):preview(self.book,dict(name='Bad',sql=sql,expected_state=self.book.state()))
        handler=Path(self.temp.name)/'rule.py';handler.write_text('def on_table_structure(ctx,event):\n    ctx.set("Sheet1",1,1,"generated")\n    raise ValueError("reject saved view")\n');self.book.handler=str(handler)
        with self.assertRaises(EngineError):self.save()
        self.assertEqual(queries(self.book.db),[]);self.assertIsNone(self.book.raw(self.book.sheet('Sheet1'),1,1));self.book.handler=None
    def test_external_definition_or_shape_tampering_rejects_reopen(self):
        item=self.save();self.book.close()
        with closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            item['sql']='SELECT * FROM dh_meta';db.execute("UPDATE dh_relational SET payload=? WHERE kind='query'",(json.dumps(item),))
        with self.assertRaises(EngineError):Workbook(self.path,readonly=True)
        self.book=Workbook(Path(self.temp.name)/'replacement.dhwb',create=True)
    def test_run_digest_rejects_an_unreviewed_external_definition_change(self):
        from datahorizon.query_views import definition_digest
        item=self.save();digest=definition_digest(item);item['sql']='SELECT Key,Value FROM Data WHERE Value>99 ORDER BY Key'
        with closing(sqlite3.connect(self.path,isolation_level=None)) as db:db.execute("UPDATE dh_relational SET payload=? WHERE kind='query'",(json.dumps(item),))
        self.book.info()
        with self.assertRaises(EngineError) as failure:self.book.query_jobs().start(dict(query=item['id'],definition_digest=digest,job_id='changed',expected_state=self.book.state()))
        self.assertEqual(failure.exception.body['code'],'STALE_DEFINITION')

class QueryViewClientTests(unittest.TestCase):
    def test_sdk_saved_view_creation_reopen_and_explicit_refresh(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'client.dhwb'
            with client.open(path,create=True) as book:
                review=book.preview_query('Constant view','SELECT :value AS value',parameters=[dict(name='value',type='integer')],defaults=dict(value=7));item=book.save_query(review)['query'];self.assertEqual(book.query_definition(item['id'])['query']['defaults'],dict(value=7))
            cli=subprocess.run([sys.executable,'-m','datahorizon.cli',str(path),'view','read','Constant view'],capture_output=True,text=True,check=True);self.assertEqual(json.loads(cli.stdout)['query']['id'],item['id'])
            output=Path(temp)/'query.csv';cli=subprocess.run([sys.executable,'-m','datahorizon.cli',str(path),'query','run','--saved','Constant view','--export','csv','--output',str(output)],input='{}',capture_output=True,text=True,check=True);self.assertEqual(json.loads(cli.stdout)['export']['outcome'],'committed');self.assertEqual(output.read_text(),'value\n7\n')
            with client.open(path) as book:
                self.assertEqual(len(book.tables()['queries']),1);result=book.run_saved_query(item['id']);deadline=time.monotonic()+4
                while result['status']=='running' and time.monotonic()<deadline:time.sleep(.005);result=book.query(result['job_id'])
                self.assertEqual(book.query(result['job_id'],'page')['rows'][0]['values'],[7]);book.query(result['job_id'],'release');book.rename_query(item['id'],'Renamed');book.delete_query(item['id'])
