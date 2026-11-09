# SPDX-License-Identifier: GPL-3.0-only
"""Style-only snapshots support painter patterns without touching formula/input data."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datahorizon.engine import Workbook,EngineError,encoded
from datahorizon.client import LocalService,APIError
from datahorizon.service import openapi

class StyleSnapshotTests(unittest.TestCase):
    def test_dense_snapshot_skips_input_reads_evaluation_hooks_and_history(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'s.dhwb',create=True)) as b:
            b.mutate(dict(action='edit',operation_id='seed',expected_state=b.state(),cells=[dict(row=1,col=1,source='=1/0',style={'font_size':16}),dict(row=2,col=2,source='x'*65536,kind='text',style={'border_bottom':'dashed'})]))
            state=b.state();history=list(b.undo);b.handler='must-not-run.py'
            with patch.object(b,'calculator',side_effect=AssertionError('no calculation')),patch.object(b,'raw',side_effect=AssertionError('no inputs')):
                result=b.read_styles('Sheet1',dict(r2=2,c2=2,expected_state=state))
            self.assertEqual(result,dict(state=state,sheet=b.sheet('Sheet1'),range=[1,1,2,2],rows=[[{'font_size':16},{}],[{},{'border_bottom':'dashed'}]]));self.assertEqual(b.state(),state);self.assertEqual(b.undo,history);self.assertFalse(b.db.in_transaction)

    def test_limits_state_timeout_and_wal_snapshot_fail_without_partial_results(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'s.dhwb',create=True)) as b:
            b.db.execute('PRAGMA journal_mode=WAL');b.refresh();sid=b.sheet('Sheet1');b.mutate(dict(action='edit',operation_id='seed',expected_state=b.state(),cells=[dict(row=1,col=1,source='7',style={'bold':True})]));state=b.state();original=b.sheet
            def change(sheet):
                target=original(sheet)
                with closing(sqlite3.connect(b.path)) as writer:writer.execute("UPDATE cells SET style='{}' WHERE sheet_id=?",(sid,));writer.commit()
                return target
            with patch.object(b,'sheet',side_effect=change):result=b.read_styles(sid,dict(expected_state=state))
            self.assertEqual(result['rows'],[[{'bold':True}]]);self.assertEqual(result['state'],state)
            with self.assertRaises(EngineError) as error:b.read_styles(sid,dict(expected_state=state))
            self.assertEqual(error.exception.body['code'],'STALE_STATE')
            for args in ({},{'expected_state':True},{'expected_state':''},{'expected_state':b.state(),'r2':10001},{'expected_state':b.state(),'c1':True}):
                with self.assertRaises(EngineError):b.read_styles(sid,args)
            expected=b.state();calls=[0]
            def clock():calls[0]+=1;return 0 if calls[0]<2 else 3
            with patch('datahorizon.engine.time.monotonic',side_effect=clock):
                with self.assertRaises(EngineError) as error:b.read_styles(sid,dict(expected_state=expected))
            self.assertEqual(error.exception.body['code'],'TIMEOUT');self.assertFalse(b.db.in_transaction)

    def test_response_budget_rejects_large_valid_styles(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'s.dhwb',create=True)) as b:
            sid=b.sheet('Sheet1');style=encoded({'font_family':'界'*64,'font_size':72,'text_color':'#ffffff','background':'#123abc','border_left':'dashed','border_right':'dashed','border_top':'medium','border_bottom':'thick','format':'currency','currency':'USD','decimals':15,'grouping':True,'align':'center','wrap':True,'bold':True,'italic':True})
            b.db.executemany('INSERT INTO cells(sheet_id,row,col,style) VALUES(?,?,1,?)',[(sid,r,style) for r in range(1,10001)])
            with self.assertRaises(EngineError) as error:b.read_styles(sid,dict(r2=10000,expected_state=b.state()))
            self.assertEqual(error.exception.body['code'],'READ_LIMIT');self.assertFalse(b.db.in_transaction)

    def test_sdk_readonly_schema_and_atomic_exact_style_replacement(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'sdk.dhwb'
            with client.open(path,create=True) as book:
                book.apply('edit',cells=[dict(row=1,col=1,source='=2+3',kind='formula',style={'font_size':16}),dict(row=1,col=2,source='00123',kind='text',style={'bold':True}),dict(row=2,col=1,source='=7+8',kind='formula',style={'italic':True}),dict(row=2,col=2,source='00123',kind='text',style={'background':'#ffffff'})])
                pattern=book.read_styles(r2=1,c2=2)['rows'];book.apply('edit',cells=[dict(row=2,col=c,style=pattern[0][c-1],replace_style=True) for c in (1,2)])
                cells=book.read(r1=2,r2=2,c2=2)['cells'];self.assertEqual([c['source'] for c in cells],['=7+8','00123']);self.assertEqual([c['style'] for c in cells],pattern[0]);book.apply('undo');self.assertEqual(book.read(r1=2,r2=2,c2=1)['cells'][0]['style'],{'italic':True})
            with client.open(path,readonly=True) as book:
                state=book.state;self.assertEqual(book.read_styles(r2=1,c2=2)['rows'],pattern);self.assertEqual(book.state,state)
                with self.assertRaises(APIError):book.apply('edit',cells=[dict(row=2,col=1,style={},replace_style=True)])
        api=openapi();self.assertIn('/v1/workbooks/{workbook}/styles',api['paths']);self.assertIn('expected_state',api['components']['schemas']['StylesRequest']['required'])
