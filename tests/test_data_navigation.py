"""Navigation uses stored inputs in one bounded snapshot, without managed writes."""
from contextlib import closing
from pathlib import Path
import tempfile
import sqlite3
from unittest.mock import patch
import unittest
from datahorizon.engine import Workbook,EngineError
from datahorizon.client import LocalService,APIError
from datahorizon.service import openapi

class NavigationTests(unittest.TestCase):
    def test_runs_gaps_empty_formula_text_null_styles_and_grid_edges(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'nav.dhwb',create=True)) as book:
            book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[
                {'row':1,'col':1,'source':'1'},{'row':2,'col':1,'source':'=1/0'},
                {'row':3,'col':1,'source':'','kind':'text'},{'row':4,'col':1,'source':'NULL','kind':'null'},
                {'row':5,'col':1,'style':{'bold':True}},{'row':10000,'col':1,'source':'last'},
                {'row':1,'col':2,'source':'TRUE'},{'row':1,'col':3,'source':'2'}, {'row':1,'col':100,'source':'far'}]})
            state=book.state()
            def nav(row,col,direction):return book.navigate('Sheet1',{'row':row,'col':col,'direction':direction,'expected_state':state})
            self.assertEqual(nav(1,1,'down')['row'],3);self.assertEqual(nav(3,1,'down')['row'],10000)
            self.assertEqual(nav(4,1,'up')['row'],3);self.assertEqual(nav(3,1,'up')['row'],1)
            self.assertEqual(nav(10000,1,'down')['row'],1048576);self.assertEqual(nav(1048576,1,'down')['row'],1048576)
            self.assertEqual(nav(1,1,'right')['col'],3);self.assertEqual(nav(1,3,'right')['col'],100)
            self.assertEqual(nav(1,100,'left')['col'],3);self.assertEqual(nav(1,3,'left')['col'],1)
            self.assertEqual(nav(8,8,'left')['col'],1);self.assertEqual(nav(8,8,'right')['col'],16384)
            self.assertEqual(book.state(),state)
            for args in ({'row':0,'col':1,'direction':'down'},{'row':1,'col':1,'direction':'diagonal'}):
                with self.assertRaises(EngineError):book.navigate('Sheet1',{**args,'expected_state':state})
            with self.assertRaises(EngineError) as stale:book.navigate('Sheet1',{'row':1,'col':1,'direction':'down','expected_state':'stale'})
            self.assertEqual(stale.exception.body['code'],'STALE_STATE')

    def test_limits_return_no_partial_target_and_release_transaction(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'limit.dhwb',create=True)) as book:
            sheet=book.sheet('Sheet1')
            book.db.execute("BEGIN")
            book.db.executemany("INSERT INTO cells(sheet_id,row,col,source) VALUES(?,?,1,'value')",((sheet,r) for r in range(1,10003)))
            book.db.execute('COMMIT')
            with self.assertRaises(EngineError) as failure:book.navigate('Sheet1',{'row':1,'col':1,'direction':'down','expected_state':book.state()})
            self.assertEqual(failure.exception.body['code'],'NAVIGATION_LIMIT');self.assertFalse(book.db.in_transaction)
            self.assertEqual(book.read('Sheet1',{})['cells'][0]['value'],'value')

    def test_snapshot_and_source_byte_budget(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'snapshot.dhwb',create=True)) as book:
            book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[{'row':r,'col':1,'source':'1'} for r in (1,2,3)]})
            book.db.execute('PRAGMA journal_mode=WAL');book.refresh();state=book.state();read=book.raw
            def raw(*args):
                item=read(*args)
                with closing(sqlite3.connect(book.path)) as writer:
                    writer.execute('DELETE FROM cells WHERE row=3');writer.commit()
                return item
            with patch.object(book,'raw',raw):result=book.navigate('Sheet1',{'row':1,'col':1,'direction':'down','expected_state':state})
            self.assertEqual(result['row'],3);self.assertEqual(result['state'],state)
            state=book.info()['state'];self.assertEqual(book.navigate('Sheet1',{'row':1,'col':1,'direction':'down','expected_state':state})['row'],2)
            for start in (1,41):book.mutate({'action':'edit','operation_id':str(start),'expected_state':book.state(),'cells':[{'row':r,'col':2,'source':'x'*60000,'kind':'null'} for r in range(start,start+40)]})
            with self.assertRaises(EngineError) as failure:book.navigate('Sheet1',{'row':1,'col':2,'direction':'down','expected_state':book.state()})
            self.assertEqual(failure.exception.body['code'],'READ_LIMIT');self.assertFalse(book.db.in_transaction)

    def test_sdk_readonly_preconditions_and_no_hooks_receipts(self):
        with tempfile.TemporaryDirectory() as temp,LocalService(handler=Path(__file__).parent/'fixtures'/'validation_hook.py') as client:
            path=Path(temp)/'sdk.dhwb'
            with client.open(path,create=True) as book:
                book.apply('edit',cells=[{'row':1,'col':1,'source':'7'}]);state=book.state
                self.assertEqual(book.navigate(1,1,'right')['col'],2);self.assertEqual(book.refresh()['state'],state)
            with client.open(path,readonly=True) as book:
                self.assertEqual(book.navigate(1,1,'right')['col'],2)
                with self.assertRaises(APIError):book.navigate(1,1,'right',expected_state='stale')
                self.assertIsNone(book.pending_operation)
            self.assertIn('/v1/workbooks/{workbook}/navigate',openapi()['paths'])
