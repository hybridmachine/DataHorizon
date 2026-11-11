# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
import json
import os
import subprocess
import sys

from datahorizon.engine import Workbook,EngineError
from datahorizon.relational import table
from datahorizon.table_schema import preview
from datahorizon.client import LocalService,APIError


class TableSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'schema.dhwb';self.book=Workbook(self.path,create=True);self.addCleanup(lambda:self.book.close())
        self.apply('table_create',name='Data',columns=[dict(name='ID',required=True,unique=True),dict(name='Value',type='number'),dict(name='Optional')])
        self.direct("INSERT INTO Data(ID,Value,Optional) VALUES('C001',1,'keep'),('C002',2,NULL)")

    def apply(self,action,**args):
        return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),**{'expected_state':self.book.state(),**args}))

    def direct(self,sql):
        with closing(sqlite3.connect(self.path)) as db:db.execute(sql);db.commit()
        self.book.refresh()

    def columns(self):return table(self.book.db,'Data')['columns']

    def review(self,columns=None,**args):
        return preview(self.book,dict(table='Data',columns=self.columns() if columns is None else columns,expected_state=self.book.state(),**args))

    def change(self,review):return self.apply('table_schema',**review['source_request'],review_digest=review['review_digest'],expected_state=review['state'])

    def test_rebuild_rename_type_and_identity_sequence_survive_native_reopen(self):
        original=table(self.book.db,'Data');cols=self.columns();cols[1].update(name='Quantity',type='integer')
        self.direct("UPDATE sqlite_sequence SET seq=30 WHERE name='Data'")
        review=self.review(cols,name='Renamed');self.assertTrue(review['can_apply']);self.change(review)
        current=table(self.book.db,'Renamed');self.assertEqual(current['id'],original['id']);self.assertEqual(current['columns'][1]['id'],cols[1]['id'])
        self.assertEqual(self.book.db.execute('SELECT _dh_row_id,Quantity,Optional FROM Renamed ORDER BY _dh_row_id').fetchall(),[(1,1,'keep'),(2,2,None)])
        self.book.validate()
        with closing(sqlite3.connect(self.path)) as direct:
            self.assertEqual(direct.execute("INSERT INTO Renamed(ID,Quantity) VALUES('C003',3)").lastrowid,31);direct.commit()
            with self.assertRaises(sqlite3.IntegrityError):direct.execute("INSERT INTO Renamed(ID,Quantity) VALUES('bad',3.5)")
        self.book.refresh();target=Path(self.temp.name)/'copy.dhwb';self.book.snapshot(target)
        with closing(Workbook(target)) as copy:self.assertEqual(table(copy.db,'Renamed')['id'],original['id'])

    def test_lossy_conversion_reports_row_and_preserves_data_schema_history(self):
        self.direct('UPDATE Data SET Value=1.5 WHERE _dh_row_id=1');cols=self.columns();cols[1]['type']='integer'
        self.apply('edit',cells=[dict(row=1,col=1,source='saved')]);history=list(self.book.undo)
        review=self.review(cols);self.assertFalse(review['can_apply']);self.assertEqual(review['issues'][0]['row_id'],'1')
        with self.assertRaises(EngineError):self.change(review)
        self.assertEqual(table(self.book.db,'Data')['columns'][1]['type'],'number');self.assertEqual(self.book.undo,history)
        self.assertEqual(self.book.db.execute('SELECT Value FROM Data WHERE _dh_row_id=1').fetchone()[0],1.5)

    def test_required_unique_and_explicit_column_identity_checks(self):
        self.direct('UPDATE Data SET Value=NULL WHERE _dh_row_id=1');cols=self.columns();cols[1]['required']=True
        self.assertFalse(self.review(cols)['can_apply'])
        self.direct('UPDATE Data SET Value=1');cols=self.columns();cols[1]['unique']=True
        self.assertFalse(self.review(cols)['can_apply'])
        with self.assertRaises(EngineError) as failure:self.review([dict(name='ID'),dict(name='Value'),dict(name='Optional')])
        self.assertEqual(failure.exception.body['code'],'COLUMN_ID_REQUIRED')

    def test_add_nullable_column_and_explicit_drop_preserve_other_fields(self):
        cols=self.columns();cols.append(dict(name='Added',type='text'))
        review=self.review(cols);self.change(review);self.book.validate()
        self.assertEqual(self.book.db.execute('SELECT Added FROM Data').fetchall(),[(None,),(None,)])
        cols=self.columns();removed=cols.pop(2)
        with self.assertRaises(EngineError) as failure:self.review(cols)
        self.assertEqual(failure.exception.body['code'],'DROP_REVIEW_REQUIRED')
        self.change(self.review(cols,drop_columns=[removed['id']]));self.book.validate()
        self.assertEqual(self.book.db.execute('SELECT ID,Value FROM Data ORDER BY _dh_row_id').fetchall(),[('C001',1.0),('C002',2.0)])

    def test_failed_structure_hook_reverts_rebuild_and_generated_edit(self):
        handler=Path(self.temp.name)/'reject.py'
        handler.write_text("def on_table_structure(ctx,event):\n ctx.set('Sheet1',10,1,'generated')\n if event['phase']=='after':raise ValueError('reject schema')\ndef on_table_change(ctx,changes):pass\ndef on_change(ctx,changes):pass\n")
        review=self.review(name='Renamed');self.book.handler=str(handler)
        with self.assertRaises(EngineError) as failure:self.change(review)
        self.assertEqual(failure.exception.body['code'],'HOOK_FAILED');self.assertEqual(table(self.book.db,'Data')['name'],'Data')
        self.assertIsNone(self.book.raw(self.book.sheet('Sheet1'),10,1));self.book.validate()

    def test_type_changes_emit_old_new_semantics_with_before_schema_access(self):
        handler=Path(self.temp.name)/'types.py'
        handler.write_text("def on_table_structure(ctx,event):pass\ndef on_change(ctx,changes):pass\ndef on_table_change(ctx,changes):\n for change in changes:\n  assert change['schema_changed']\n  old=ctx.table_schema(change['table'],phase='before')\n  new=ctx.table_schema(change['table'])\n  assert old['columns'][1]['type']=='number' and new['columns'][1]['type']=='integer'\n  ctx.set('Sheet1',10,1,'types checked','text')\n")
        cols=self.columns();cols[1]['type']='integer';review=self.review(cols);self.book.handler=str(handler);self.change(review)
        self.assertEqual(self.book.raw(self.book.sheet('Sheet1'),10,1)['source'],'types checked')

    def test_preparatory_hook_cannot_have_its_source_edits_overwritten(self):
        handler=Path(self.temp.name)/'prepare.py'
        handler.write_text("def on_table_structure(ctx,event):\n if event['phase']=='before':ctx.table_set('Data','1',{'Value':99})\ndef on_table_change(ctx,changes):pass\n")
        review=self.review(name='Renamed');self.book.handler=str(handler)
        with self.assertRaises(EngineError) as failure:self.change(review)
        self.assertEqual(failure.exception.body['code'],'HOOK_CHANGED_SCHEMA_SOURCE')
        self.assertEqual(self.book.db.execute('SELECT Value FROM Data WHERE _dh_row_id=1').fetchone()[0],1.0)

    def test_tampered_and_stale_schema_reviews_fail_before_mutation(self):
        review=self.review(name='Renamed');review['review_digest']='0'*64
        with self.assertRaises(EngineError) as failure:self.change(review)
        self.assertEqual(failure.exception.body['code'],'STALE_REVIEW')
        review=self.review(name='Renamed');self.direct("UPDATE Data SET Optional='external' WHERE _dh_row_id=1")
        with self.assertRaises(EngineError) as failure:self.change(review)
        self.assertEqual(failure.exception.body['code'],'STALE_STATE')

    def test_populated_drop_requires_explicit_loss_review_and_runs_change_hooks(self):
        with self.assertRaises(EngineError) as failure:self.apply('table_drop',table='Data')
        self.assertEqual(failure.exception.body['code'],'TABLE_NOT_EMPTY')
        handler=Path(self.temp.name)/'drop.py'
        handler.write_text("def on_table_structure(ctx,event):pass\ndef on_table_change(ctx,changes):\n for change in changes:\n  assert ctx.table_schema(change['table'],phase='before')['name']=='Data'\n  assert change['after'] is None\n raise ValueError('reject deletion')\n")
        self.book.handler=str(handler)
        with self.assertRaises(EngineError) as failure:self.apply('table_drop',table='Data',allow_data_loss=True)
        self.assertEqual(failure.exception.body['code'],'HOOK_FAILED');self.assertEqual(self.book.db.execute('SELECT count(*) FROM Data').fetchone()[0],2)
        self.book.handler=None;self.apply('table_drop',table='Data',allow_data_loss=True);self.assertFalse(self.book.info()['tables']);self.book.validate()

    def test_large_rebuild_rejects_without_intermediate_schema(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.executemany('INSERT INTO Data(ID,Value) VALUES(?,?)',[(str(i),i) for i in range(4000)]);db.commit()
        self.book.refresh()
        with self.assertRaises(EngineError) as failure:self.review(name='Renamed')
        self.assertEqual(failure.exception.body['code'],'LIMIT');self.book.validate()
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM Data').fetchone()[0],4002)


class TableSchemaClientTests(unittest.TestCase):
    def test_schema_preview_sdk_cli_and_readonly_share_managed_contract(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'clients.dhwb'
            with client.open(path,create=True) as book:
                book.apply('edit',cells=[dict(row=1,col=1,source='ID'),dict(row=1,col=2,source='Value'),dict(row=2,col=1,source='00123',kind='text'),dict(row=2,col=2,source='3')])
                book.promote_table(book.preview_table('Data',r1=1,c1=1,r2=2,c2=2));columns=book.tables()['tables'][0]['columns']
                identities=[col['id'] for col in columns];columns[1]['type']='integer'
                book.alter_table(book.preview_table_schema('Data',columns))
                command=[sys.executable,'-m','datahorizon.cli','--url',client.url,str(path),'table-schema','Data','--apply']
                result=subprocess.run(command,input=json.dumps(dict(columns=columns,name='Renamed')),env={**os.environ,'DATAHORIZON_TOKEN':client.token},capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr);book.refresh();page=book.read_table('Renamed')
                self.assertEqual([col['id'] for col in page['columns']],identities);self.assertEqual(page['records'][0]['row_id'],'1')
            with client.open(path,readonly=True) as book:
                review=book.preview_table_schema('Renamed',columns)
                with self.assertRaises(APIError) as failure:book.alter_table(review)
                self.assertEqual(failure.exception.body['code'],'READ_ONLY')
