"""Trace stored syntax without evaluation, preserving snapshot/access contracts."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datahorizon.engine import Workbook,EngineError
from datahorizon.client import LocalService,APIError
from datahorizon.service import openapi

class TraceTests(unittest.TestCase):
    def test_cross_sheet_compressed_ranges_lazy_branches_broken_and_no_evaluation(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'t.dhwb',create=True)) as b:
            def apply(action='edit',**args):return b.mutate(dict(action=action,operation_id=str(b.state()),expected_state=b.state(),**args))
            apply('sheet_add',name='Other');apply(cells=[dict(row=1,col=1,source='=IF(FALSE,SUM(B1:B1048576),Other!C2)+#REF!'),dict(row=2,col=1,source='=A1+A1'),dict(row=3,col=1,source='=A1',kind='text')])
            state=b.state();history=list(b.undo);b.handler='must-not-run.py'
            with patch.object(b,'calculator',side_effect=AssertionError('must not evaluate')):
                result=b.trace('Sheet1',dict(row=1,col=1,direction='precedents',expected_state=state))
                self.assertEqual([t['address'] for t in result['targets']],['B1:B1048576','C2']);self.assertEqual(len(result['issues']),1);self.assertEqual(result['issues'][0]['sheet_name'],'Sheet1')
                result=b.trace('Sheet1',dict(row=1,col=1,direction='dependents',expected_state=state))
                self.assertEqual([t['address'] for t in result['targets']],['A2'])
                result=b.trace('Sheet1',dict(row=1048576,col=2,direction='dependents',expected_state=state))
                self.assertEqual([t['address'] for t in result['targets']],['A1'])
            self.assertEqual(b.state(),state);self.assertEqual(b.undo,history)
            b.handler=None;apply(cells=[dict(row=4,col=1,source='=Missing!A1')]);self.assertTrue(b.trace('Sheet1',dict(row=4,col=1,direction='precedents',expected_state=b.state()))['issues'])

    def test_fail_closed_invalid_syntax_limits_state_and_wal_snapshot(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'t.dhwb',create=True)) as b:
            b.db.execute('PRAGMA journal_mode=WAL');b.refresh();sid=b.sheet('Sheet1')
            b.mutate(dict(action='edit',operation_id='seed',expected_state=b.state(),cells=[dict(row=1,col=1,source='=B1')]))
            expected=b.state();raw=b.raw
            def external(*args):
                value=raw(*args)
                with closing(sqlite3.connect(b.path)) as writer:
                    writer.execute("UPDATE cells SET source='=C1' WHERE sheet_id=?",(sid,));writer.commit()
                return value
            with patch.object(b,'raw',side_effect=external):
                result=b.trace(sid,dict(row=1,col=1,direction='precedents',expected_state=expected));self.assertEqual(result['targets'][0]['address'],'B1');self.assertEqual(result['state'],expected)
            with self.assertRaises(EngineError) as error:b.trace(sid,dict(row=1,col=1,direction='precedents',expected_state=expected))
            self.assertEqual(error.exception.body['code'],'STALE_STATE')
            b.db.execute("UPDATE cells SET source='=B1%'")
            with self.assertRaises(EngineError) as error:b.trace(sid,dict(row=1,col=1,direction='dependents',expected_state=b.state()))
            self.assertEqual(error.exception.body['code'],'UNSUPPORTED_FORMULA')
            b.db.execute("UPDATE cells SET source='=B1'")
            b.db.executemany("INSERT INTO cells(sheet_id,row,col,source) VALUES(?,?,2,'=A1')",[(sid,r) for r in range(2,1003)])
            with self.assertRaises(EngineError) as error:b.trace(sid,dict(row=1,col=1,direction='dependents',expected_state=b.state()))
            self.assertEqual(error.exception.body['code'],'LIMIT');self.assertFalse(b.db.in_transaction)
            with self.assertRaises(EngineError):b.trace(sid,dict(row=True,col=1,direction='precedents',expected_state=b.state()))

    def test_rest_sdk_readonly_and_schema(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'sdk.dhwb'
            with client.open(path,create=True) as book:book.apply('edit',cells=[dict(row=1,col=1,source='=B1')])
            with client.open(path,readonly=True) as book:
                state=book.state;result=book.trace(1,1);self.assertEqual(result['targets'][0]['address'],'B1');self.assertEqual(book.state,state)
                with self.assertRaises(APIError):book.trace(0,1)
        self.assertIn('expected_state',openapi()['components']['schemas']['TraceRequest']['required'])
