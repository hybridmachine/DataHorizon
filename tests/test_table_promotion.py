# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
import uuid
import os
import json
import subprocess
import sys
from datahorizon.engine import Workbook,EngineError
from datahorizon.table_promotion import preview
from datahorizon.relational_rows import read
from datahorizon.client import LocalService


class TablePromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'source.dhwb';self.book=Workbook(self.path,create=True);self.addCleanup(lambda:self.book.close())
        self.apply('edit',cells=[dict(row=1,col=1,source='ID'),dict(row=1,col=2,source='Amount'),
            dict(row=2,col=1,source='00123',kind='text'),dict(row=2,col=2,source='=1+2',kind='formula',style=dict(bold=True)),
            dict(row=3,col=1,source='=literal',kind='text'),dict(row=3,col=2,source='2.5')])

    def apply(self,action,**args):
        return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),**{'expected_state':self.book.state(),**args}))

    def preview(self,**args):
        return preview(self.book,dict(name='Snapshot',r1=1,c1=1,r2=3,c2=2,expected_state=self.book.state(),**args))

    def promote(self,review):
        return self.apply('table_promote',**review['source_request'],columns=review['columns'],preview_clock=review['preview_clock'],review_digest=review['review_digest'])

    def records(self):return read(self.book,dict(table='Snapshot',expected_state=self.book.state()))

    def test_reviewed_formula_values_and_text_identity_preserve_source_copy_reopen(self):
        source=self.book.db.execute('SELECT * FROM cells ORDER BY row,col').fetchall()
        review=self.preview();self.assertTrue(review['can_create']);receipt=self.promote(review)
        self.assertEqual(receipt['created_records'],2);self.assertEqual(receipt['changed_records'],2)
        rows=self.records();ids={c['name']:c['id'] for c in rows['columns']}
        self.assertEqual(rows['records'][0]['values'][ids['ID']],'00123')
        self.assertEqual(rows['records'][0]['values'][ids['Amount']],3)
        self.assertEqual(rows['records'][1]['values'][ids['ID']],'=literal')
        self.assertEqual(self.book.db.execute('SELECT * FROM cells ORDER BY row,col').fetchall(),source)
        self.assertFalse(self.book.undo)
        target=Path(self.temp.name)/'copy.dhwb';self.book.snapshot(target)
        with closing(Workbook(target)) as reopened:
            self.assertEqual(len(read(reopened,dict(table='Snapshot',expected_state=reopened.state()))['records']),2)

    def test_stale_and_tampered_review_do_not_create_a_partial_table(self):
        review=self.preview();review['review_digest']='0'*64
        with self.assertRaises(EngineError) as error:self.promote(review)
        self.assertEqual(error.exception.body['code'],'STALE_REVIEW');self.assertEqual(self.book.info()['format_version'],1)
        review=self.preview();old=review['state'];self.apply('edit',cells=[dict(row=3,col=2,source='9')])
        with self.assertRaises(EngineError) as error:
            self.apply('table_promote',**review['source_request'],columns=review['columns'],preview_clock=review['preview_clock'],review_digest=review['review_digest'],expected_state=old)
        self.assertEqual(error.exception.body['code'],'STALE_STATE');self.assertFalse(self.book.info()['tables'])

    def test_conversion_errors_and_invalid_headers_withhold_apply(self):
        review=self.preview(columns=[dict(name='ID'),dict(name='Amount',type='integer')])
        self.assertFalse(review['can_create']);self.assertTrue(review['issues'])
        self.apply('edit',cells=[dict(row=1,col=2,source='ID')])
        self.assertFalse(self.preview()['can_create'])
        review=self.preview(columns=[dict(name='Identifier'),dict(name='Value',type='number')])
        self.assertTrue(review['can_create']);self.promote(review)

    def test_pending_or_error_formula_blocks_values_snapshot(self):
        self.apply('edit',cells=[dict(row=2,col=2,source='=UNKNOWN(1)',kind='formula')])
        self.assertFalse(self.preview()['can_create']);self.assertFalse(self.book.info()['tables'])

    def test_volatile_value_uses_one_reviewed_clock(self):
        self.apply('edit',cells=[dict(row=2,col=2,source='=NOW()',kind='formula'),dict(row=3,col=2,source='2024-02-29T00:00:00',kind='datetime')])
        review=self.preview();self.assertTrue(review['can_create'])
        handler=Path(self.temp.name)/'clock.py'
        handler.write_text("def on_table_structure(ctx,event): pass\ndef on_table_change(ctx,changes):\n for change in changes:\n  column=next(c['id'] for c in ctx.table_schema(change['table'])['columns'] if c['name']=='Amount')\n  assert ctx.value('Sheet1',int(change['row_id'])+1,2)==change['after'][column]\n")
        self.book.handler=str(handler);self.promote(review)
        records=self.records();column=next(col for col in records['columns'] if col['name']=='Amount')
        self.assertEqual(column['type'],'datetime')
        self.assertEqual(records['records'][0]['values'][column['id']],review['records_preview'][0][column['id']])

    def test_table_hook_broker_reads_and_generated_edits_share_parent(self):
        handler=Path(self.temp.name)/'tables.py'
        handler.write_text("def on_table_structure(ctx,event): pass\ndef on_change(ctx,changes): pass\ndef on_table_change(ctx,changes):\n for change in changes:\n  schema=ctx.table_schema(change['table'])\n  amount=next(c['id'] for c in schema['columns'] if c['name']=='Amount')\n  row=ctx.table_get(change['table'],change['row_id'])\n  if row['values'][amount]==3: ctx.table_set(change['table'],change['row_id'],{amount:4})\n ctx.set('Sheet1',10,1,'generated','text')\n")
        self.book.handler=str(handler);self.promote(self.preview())
        self.assertEqual(self.book.db.execute('SELECT Amount FROM Snapshot ORDER BY _dh_row_id').fetchone()[0],4)
        self.assertEqual(self.book.raw(self.book.sheet('Sheet1'),10,1)['source'],'generated')

    def test_required_table_hook_rejection_rolls_back_data_catalog_and_marker(self):
        handler=Path(self.temp.name)/'reject.py'
        handler.write_text("def on_table_structure(ctx,event): pass\ndef on_change(ctx,changes): pass\ndef on_table_change(ctx,changes):\n ctx.set('Sheet1',10,1,'generated')\n raise ValueError('reject record')\n")
        self.book.handler=str(handler)
        with self.assertRaises(EngineError) as error:self.promote(self.preview())
        self.assertEqual(error.exception.body['code'],'HOOK_FAILED')
        self.assertFalse(self.book.info()['tables']);self.assertEqual(self.book.info()['format_version'],1)
        self.assertIsNone(self.book.raw(self.book.sheet('Sheet1'),10,1))

    def test_legacy_structure_only_handler_cannot_bypass_table_changes(self):
        handler=Path(self.temp.name)/'legacy.py';handler.write_text('def on_table_structure(ctx,event): pass\n')
        self.book.handler=str(handler)
        with self.assertRaises(EngineError) as error:self.promote(self.preview())
        self.assertEqual(error.exception.body['code'],'UNSUPPORTED_HOOK_SCOPE');self.assertFalse(self.book.info()['tables'])

    def test_read_pages_bind_state_columns_and_record_ids(self):
        self.promote(self.preview());page=read(self.book,dict(table='Snapshot',expected_state=self.book.state(),limit=1))
        self.assertEqual(page['next_after'],'1')
        second=read(self.book,dict(table='Snapshot',expected_state=self.book.state(),after='1',limit=1))
        self.assertEqual(second['records'][0]['row_id'],'2');self.assertIsNone(second['next_after'])
        with self.assertRaises(EngineError):read(self.book,dict(table='Snapshot',expected_state='stale'))

    def test_generated_record_and_cell_share_undo_redo_history(self):
        self.promote(self.preview());handler=Path(self.temp.name)/'history.py'
        handler.write_text("def on_table_change(ctx,changes): pass\ndef on_change(ctx,changes):\n for change in changes:\n  if change['row']==10 and change['after'] and change['after']['source']=='add' and ctx.table_get('Snapshot','3')['values'] is None:\n   ctx.table_insert('Snapshot',{'ID':'new','Amount':7})\n")
        self.book.handler=str(handler)
        self.apply('edit',cells=[dict(row=10,col=1,source='add')]);self.assertEqual(len(self.records()['records']),3)
        self.apply('undo');self.assertEqual(len(self.records()['records']),2);self.assertIsNone(self.book.raw(self.book.sheet('Sheet1'),10,1))
        self.apply('redo');self.assertEqual(len(self.records()['records']),3);self.assertEqual(self.book.raw(self.book.sheet('Sheet1'),10,1)['source'],'add')

    def test_table_hook_recursion_rolls_back_the_entire_creation(self):
        handler=Path(self.temp.name)/'recursive.py'
        handler.write_text("def on_table_structure(ctx,event): pass\ndef on_table_change(ctx,changes):\n ctx.table_insert(changes[0]['table'],{'ID':'again','Amount':1})\n")
        self.book.handler=str(handler)
        with self.assertRaises(EngineError) as error:self.promote(self.preview())
        self.assertEqual(error.exception.body['code'],'HOOK_RECURSION');self.assertFalse(self.book.info()['tables'])


class TablePromotionClientTests(unittest.TestCase):
    def test_rest_sdk_promotion_uses_reviewed_state_and_types(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/'api.dhwb',create=True) as book:
            book.apply('edit',cells=[dict(row=1,col=1,source='ID'),dict(row=2,col=1,source='00123',kind='text')])
            review=book.preview_table('Items',r1=1,r2=2,c1=1,c2=1)
            self.assertTrue(review['can_create']);receipt=book.promote_table(review)
            self.assertEqual(receipt['created_records'],1);page=book.read_table('Items')
            self.assertEqual(page['records'][0]['values'][page['columns'][0]['id']],'00123')

    def test_managed_cli_selection_creation_matches_sdk(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/'cli.dhwb',create=True) as book:
            book.apply('edit',cells=[dict(row=1,col=1,source='ID'),dict(row=2,col=1,source='00123',kind='text')])
            command=[sys.executable,'-m','datahorizon.cli','--url',client.url,book.info['path'],'table-from-range','Items','--apply']
            result=subprocess.run(command,input=json.dumps(dict(r1=1,r2=2,c1=1,c2=1)),env={**os.environ,'DATAHORIZON_TOKEN':client.token},capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout)['created_records'],1)
            book.refresh();page=book.read_table('Items');self.assertEqual(page['records'][0]['values'][page['columns'][0]['id']],'00123')
