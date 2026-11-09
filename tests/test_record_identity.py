# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook,EngineError
from datahorizon.records import read
from datahorizon.client import LocalService


class RecordIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'records.dhwb';self.book=Workbook(self.path,create=True)
        self.apply(cells=[dict(row=r,col=1,source=str(r)) for r in range(1,5)])
        self.apply('list_declare',id='Records',spec=dict(r1=1,r2=4,c2=2,header=True))
    def tearDown(self):self.book.close();self.temp.cleanup()
    def apply(self,action='edit',**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))
    def records(self):return read(self.book,dict(id='Records',sheet=self.book.info()['sheets'][0]['id'],expected_state=self.book.state()))

    def test_identity_survives_edit_insert_delete_rename_and_reopen(self):
        initial=self.records()['records'];self.apply(cells=[dict(row=2,col=1,source='99')]);self.assertEqual(self.records()['records'],initial)
        self.apply('rows_insert',start=3);inserted=self.records()['records'];self.assertEqual([inserted[i]['id'] for i in (0,2,3)],[r['id'] for r in initial])
        self.apply('rows_delete',start=3);self.assertEqual(self.records()['records'],initial)
        self.assertEqual(self.book.list_view('Sheet1',dict(r2=4,c2=2,header=True,sort=[dict(col=1,descending=True)]))['rows'][0],1)
        self.apply('sheet_rename',sheet='Sheet1',name='Renamed')
        self.book.close();self.book=Workbook(self.path);self.assertEqual(self.records()['records'],initial);self.assertTrue(self.records()['verified'])

    def test_direct_edit_requires_explicit_reconciliation_without_replay(self):
        initial=self.records()['records']
        with closing(sqlite3.connect(self.path)) as direct,direct:direct.execute("UPDATE cells SET source='external' WHERE row=2")
        self.book.info();self.assertFalse(self.records()['verified'])
        self.apply('list_reconcile',id='Records',mode='retain_positions');self.assertTrue(self.records()['verified']);self.assertEqual(self.records()['records'],initial)
        self.apply('list_reconcile',id='Records',mode='regenerate');self.assertTrue(set(r['id'] for r in initial).isdisjoint(r['id'] for r in self.records()['records']))

    def test_header_deletion_rejects_and_retains_all_inputs_and_identity(self):
        before=self.book.read_workbook();records=self.records()
        with self.assertRaises(EngineError):self.apply('rows_delete',start=1)
        self.assertEqual(self.book.read_workbook(),before);self.assertEqual(self.records(),records)

    def test_rest_sdk_declaration_and_read(self):
        with LocalService() as client,client.open(Path(self.temp.name)/'sdk.dhwb',create=True) as book:
            book.declare_list('List',r2=2,c2=2);snapshot=book.records('List');self.assertTrue(snapshot['verified']);self.assertEqual(len(snapshot['records']),2)
