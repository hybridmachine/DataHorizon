# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
from datahorizon.engine import Workbook,EngineError
from datahorizon.relational import table
from datahorizon.relational_rows import read
from datahorizon.table_edits import preview
from datahorizon.client import LocalService,UnknownOutcome


class TableEditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'records.dhwb';self.book=Workbook(self.path,create=True);self.addCleanup(lambda:self.book.close())
        self.apply('table_create',name='Data',columns=[dict(name='ID',required=True,unique=True),dict(name='Qty',type='integer',required=True),
            dict(name='Value',type='number'),dict(name='Flag',type='boolean'),dict(name='Day',type='date'),dict(name='Note')])
        self.apply('table_insert',table='Data',rows=[dict(ID='A001',Qty=2,Value=2.5,Flag=True,Day='2024-02-29',Note=''),dict(ID='B001',Qty=3,Value=4,Flag=False)])

    def apply(self,action,**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),**{'expected_state':self.book.state(),**args}))

    def rows(self):return self.book.db.execute('SELECT _dh_row_id,ID,Qty,Value,Flag,Day,Note FROM Data ORDER BY _dh_row_id').fetchall()

    def test_insert_update_delete_and_history_preserve_typed_values_and_identity(self):
        self.assertEqual(self.rows()[0],(1,'A001',2,2.5,1,'2024-02-29',''))
        receipt=self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Qty=8,Note='=literal'))])
        self.assertEqual(receipt['changed_records'],1);self.assertEqual(self.rows()[0][2],8)
        self.apply('undo');self.assertEqual(self.rows()[0][2],2);self.apply('redo');self.assertEqual(self.rows()[0][2],8)
        self.apply('table_delete',table='Data',record_ids=['1','2']);self.assertEqual(self.rows(),[])
        self.apply('undo');self.assertEqual([row[0] for row in self.rows()],[1,2]);self.apply('redo')
        inserted=self.apply('table_insert',table='Data',rows=[dict(ID='C001',Qty=1)])
        self.assertEqual(inserted['record_ids'],['3']);self.assertEqual(self.rows()[0][6],None)

    def test_invalid_type_and_unique_bulk_edits_roll_back_all_targets(self):
        before=self.rows();history=list(self.book.undo)
        for edits in ([dict(row_id='1',values=dict(Qty=8)),dict(row_id='2',values=dict(Qty='bad'))],
                      [dict(row_id='1',values=dict(ID='same')),dict(row_id='2',values=dict(ID='same'))]):
            with self.assertRaises(EngineError) as failure:self.apply('table_update',table='Data',edits=edits)
            self.assertEqual(failure.exception.body['outcome'],'rolled_back');self.assertEqual(self.rows(),before);self.assertEqual(self.book.undo,history)

    def test_null_empty_text_and_strict_boolean_number_integer_boundaries(self):
        for values in (dict(Qty=None),dict(Qty=True),dict(Qty=2**53),dict(Flag=1),dict(Value='2.5'),dict(Value=float('inf'))):
            with self.assertRaises((EngineError,ValueError)):self.apply('table_update',table='Data',edits=[dict(row_id='1',values=values)])
        self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Note=None,Value=0))])
        self.assertIsNone(self.rows()[0][6]);self.assertEqual(self.rows()[0][3],0)
        self.apply('undo');self.assertEqual(self.rows()[0][6],'')

    def test_noop_preserves_redo_without_dispatching_change_hooks(self):
        self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Qty=8))]);self.apply('undo');redo=list(self.book.redo)
        handler=Path(self.temp.name)/'reject.py';handler.write_text("def on_table_change(ctx,changes):raise ValueError('no event expected')\n")
        self.book.handler=str(handler);receipt=self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Qty=2))])
        self.assertEqual(receipt['changed_records'],0);self.assertEqual(self.book.redo,redo)

    def test_table_and_cell_generated_edits_share_undo_and_rejected_undo_is_atomic(self):
        handler=Path(self.temp.name)/'generated.py'
        handler.write_text("def on_change(ctx,changes):pass\ndef on_table_change(ctx,changes):\n for change in changes:\n  qty=next(c['id'] for c in ctx.table_schema(change['table'])['columns'] if c['name']=='Qty')\n  if change['row_id']=='1' and change['after'] and change['after'][qty]==8:\n   ctx.table_set('Data','2',{'Qty':9})\n   ctx.set('Sheet1',10,1,'generated','text')\n")
        self.book.handler=str(handler);receipt=self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Qty=8))])
        self.assertEqual((receipt['changed_records'],receipt['changed_cells']),(2,1))
        self.apply('undo');self.assertEqual([row[2] for row in self.rows()],[2,3]);self.assertIsNone(self.book.raw(self.book.sheet('Sheet1'),10,1))
        self.apply('redo');self.assertEqual([row[2] for row in self.rows()],[8,9]);before=self.rows();history=list(self.book.undo)
        handler.write_text("def on_change(ctx,changes):pass\ndef on_table_change(ctx,changes):raise ValueError('reject undo')\n")
        with self.assertRaises(EngineError):self.apply('undo')
        self.assertEqual(self.rows(),before);self.assertEqual(self.book.undo,history)

    def test_required_hook_failure_rolls_back_bulk_records_and_generated_marker(self):
        handler=Path(self.temp.name)/'required.py'
        handler.write_text("def on_change(ctx,changes):pass\ndef on_table_change(ctx,changes):\n ctx.set('Sheet1',10,1,'marker')\n raise ValueError('reject batch')\n")
        self.book.handler=str(handler);before=self.rows()
        with self.assertRaises(EngineError) as failure:self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Qty=8)),dict(row_id='2',values=dict(Qty=9))])
        self.assertEqual(failure.exception.body['code'],'HOOK_FAILED');self.assertEqual(self.rows(),before);self.assertIsNone(self.book.raw(self.book.sheet('Sheet1'),10,1))

    def test_conversion_preview_digest_and_canonical_number_survive_json_roundtrip(self):
        review=preview(self.book,dict(action='table_update',table='Data',expected_state=self.book.state(),edits=[dict(row_id='1',values=dict(Qty='7',Value='3.0',Note='00123'))]))
        self.assertTrue(review['can_apply']);operation=review['operation']
        operation['edits'][0]['values'][table(self.book.db,'Data')['columns'][2]['id']]=3
        self.apply(**operation,review_digest=review['review_digest']);self.assertEqual(self.rows()[0][2:4],(7,3.0))
        bad=preview(self.book,dict(action='table_update',table='Data',expected_state=self.book.state(),edits=[dict(row_id='1',values=dict(Qty='bad'))]))
        self.assertFalse(bad['can_apply']);self.assertEqual(self.rows()[0][2],7)

    def test_stale_external_edit_missing_duplicate_and_reserved_targets(self):
        old=self.book.state()
        with closing(sqlite3.connect(self.path)) as direct:direct.execute('UPDATE Data SET Qty=99 WHERE _dh_row_id=1');direct.commit()
        with self.assertRaises(EngineError) as failure:self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Qty=8))],expected_state=old)
        self.assertEqual(failure.exception.body['code'],'STALE_STATE');self.assertEqual(self.rows()[0][2],99)
        for action,options in (
            ('table_update',dict(edits=[dict(row_id='999',values=dict(Qty=1))])),
            ('table_update',dict(edits=[dict(row_id='1',values=dict(Qty=1)),dict(row_id='1',values=dict(Qty=2))])),
            ('table_delete',dict(record_ids=['1','1'])),
            ('table_insert',dict(rows=[dict(_dh_row_id='3',ID='C',Qty=1)]))):
            with self.assertRaises(EngineError):self.apply(action,table='Data',**options)

    def test_point_reads_preserve_requested_order_and_reject_missing_records(self):
        cols=table(self.book.db,'Data')['columns'];qty=cols[1]['id']
        result=read(self.book,dict(table='Data',record_ids=['2','1'],columns=[qty],expected_state=self.book.state()))
        self.assertEqual([row['row_id'] for row in result['records']],['2','1'])
        with self.assertRaises(EngineError):read(self.book,dict(table='Data',record_ids=['999'],columns=[qty],expected_state=self.book.state()))

    def test_identity_exhaustion_has_durable_rollback_without_partial_insert(self):
        with closing(sqlite3.connect(self.path)) as direct:
            direct.execute('INSERT INTO Data(_dh_row_id,ID,Qty) VALUES(?,?,?)',(2**63-1,'maximum',1));direct.commit()
        self.book.refresh()
        with self.assertRaises(EngineError) as failure:self.apply('table_insert',table='Data',rows=[dict(ID='overflow',Qty=1)])
        self.assertEqual(failure.exception.body['code'],'ROW_ID_LIMIT');self.assertEqual(self.book.operation(failure.exception.body['operation_id'])['outcome'],'rolled_back')
        self.assertEqual(len(self.rows()),3)

    def test_ordinary_field_limits_reject_before_partial_insertion(self):
        with self.assertRaises(EngineError) as failure:self.apply('table_insert',table='Data',rows=[dict(ID=str(i),Qty=1) for i in range(2000)])
        self.assertEqual(failure.exception.body['code'],'LIMIT');self.assertEqual(len(self.rows()),2)


class TableEditClientTests(unittest.TestCase):
    def test_sdk_cli_bulk_records_and_unknown_acknowledgement_reconcile(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/'api.dhwb',create=True) as book:
            book.create_table('Data',[dict(name='ID',required=True,unique=True),dict(name='Qty',type='integer')])
            result=book.insert_records('Data',[dict(ID='00123',Qty=2)]);self.assertEqual(result['record_ids'],['1'])
            command=[sys.executable,'-m','datahorizon.cli','--url',client.url,book.info['path'],'table-records','Data','update','--convert-inputs']
            result=subprocess.run(command,input=json.dumps([dict(row_id='1',values=dict(Qty='7'))]),env={**os.environ,'DATAHORIZON_TOKEN':client.token},capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr);book.refresh()
            original=client.request
            def lose(path,body=None):
                result=original(path,body)
                if path.endswith('/operations'):raise OSError('lost acknowledgement')
                return result
            with patch.object(client,'request',side_effect=lose):
                with self.assertRaises(UnknownOutcome) as failure:book.insert_records('Data',[dict(ID='next',Qty=3)])
            operation=failure.exception.operation_id
            with self.assertRaises(UnknownOutcome):book.delete_records('Data',['1'])
            self.assertEqual(book.outcome(operation)['outcome'],'committed');self.assertEqual(len(book.read_table('Data')['records']),2)
            book.delete_records('Data',['1']);book.apply('undo');self.assertEqual(len(book.read_table('Data')['records']),2)
