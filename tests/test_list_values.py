"""Filtered values are one bounded, state-checked snapshot in display order."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datahorizon.engine import Workbook,EngineError
from datahorizon.client import LocalService,APIError
from datahorizon.service import openapi

class ListValuesTests(unittest.TestCase):
    def seed(self,book):
        book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[
            {'row':1,'col':1,'source':'ID'},{'row':1,'col':2,'source':'Value'},
            {'row':2,'col':1,'source':'00123','kind':'text'},{'row':2,'col':2,'source':'=1/4','style':{'format':'percent2'}},
            {'row':3,'col':1,'source':'=literal','kind':'text'},{'row':3,'col':2,'source':'TRUE'},
            {'row':4,'col':1,'source':'','kind':'text'},{'row':4,'col':2,'source':'NULL','kind':'null'}]})
        return {'r1':1,'r2':4,'c1':1,'c2':2,'header':True,'sort':[{'col':1,'descending':True}],'expected_state':book.state()}

    def test_types_order_selection_hiding_and_no_history_changes(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'values.dhwb',create=True)) as book:
            args=self.seed(book);state=book.state();result=book.list_values('Sheet1',args)
            self.assertEqual(result['source_rows'],[1,3,2,4]);self.assertEqual(result['source_columns'],[1,2]);self.assertEqual(book.state(),state)
            self.assertEqual(result['rows'][1][0]['input_kind'],'text');self.assertEqual(result['rows'][1][0]['source'],'=literal')
            self.assertIs(result['rows'][1][1]['value'],True);self.assertEqual(result['rows'][2][0]['source'],'00123')
            self.assertEqual(result['rows'][2][1]['value'],.25);self.assertEqual(result['rows'][2][1]['input_kind'],'number');self.assertEqual(result['formulas_as_values'],1)
            self.assertEqual(result['rows'][3][0]['source'],'');self.assertEqual(result['rows'][3][0]['input_kind'],'text');self.assertEqual(result['rows'][3][1]['input_kind'],'null')
            selected=book.list_values('Sheet1',{**args,'view_r1':2,'view_r2':4,'exclude_rows':[2],'exclude_columns':[2]})
            self.assertEqual(selected['source_rows'],[3,4]);self.assertEqual(selected['source_columns'],[1])
            empty=book.list_values('Sheet1',{**args,'header':False,'filters':[{'col':1,'op':'=','value':'missing'}]})
            self.assertEqual(empty['rows'],[])
            book.mutate({'action':'undo','operation_id':'undo','expected_state':state});self.assertEqual(book.read('Sheet1',{'r2':4,'c2':2})['cells'],[])

    def test_snapshot_external_writer_and_errors_withhold_results(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'values.dhwb',create=True)) as book:
            args=self.seed(book);book.db.execute('PRAGMA journal_mode=WAL');book.refresh();args['expected_state']=book.state();original=book._list_rows
            def rows(*arguments):
                result=original(*arguments)
                with closing(sqlite3.connect(book.path)) as writer:
                    writer.execute("UPDATE cells SET source='changed' WHERE row=2 AND col=1");writer.commit()
                return result
            with patch.object(book,'_list_rows',rows):result=book.list_values('Sheet1',args)
            self.assertEqual(result['rows'][2][0]['source'],'00123');self.assertEqual(result['state'],args['expected_state'])
            with self.assertRaises(EngineError) as stale:book.list_values('Sheet1',args)
            self.assertEqual(stale.exception.body['code'],'STALE_STATE');self.assertFalse(book.db.in_transaction)
            book.mutate({'action':'edit','operation_id':'error','expected_state':book.state(),'cells':[{'row':2,'col':2,'source':'=1/0'}]})
            with self.assertRaises(EngineError) as error:book.list_values('Sheet1',{**args,'expected_state':book.state()})
            self.assertEqual(error.exception.body['code'],'CALCULATION_ERROR');self.assertFalse(book.db.in_transaction)
            with self.assertRaises(EngineError):book.list_values('Sheet1',{**args,'expected_state':book.state(),'r2':10000,'c2':2})
            with self.assertRaises(EngineError):book.list_values('Sheet1',{**args,'expected_state':book.state(),'exclude_rows':[1]*1025})

    def test_response_and_criteria_cache_byte_budgets(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'bytes.dhwb',create=True)) as book:
            sheet=book.sheet('Sheet1');book.db.execute('BEGIN')
            book.db.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'text')",((sheet,row,'x'*64000) for row in range(1,70)));book.db.execute('COMMIT')
            for sort in ([],[{'col':1}]):
                with self.assertRaises(EngineError) as error:book.list_values('Sheet1',{'r2':69,'c2':1,'sort':sort,'expected_state':book.state()})
                self.assertEqual(error.exception.body['code'],'READ_LIMIT');self.assertFalse(book.db.in_transaction)

    def test_sdk_readonly_and_documented_route(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'values.dhwb'
            with client.open(path,create=True) as book:book.apply('edit',cells=[{'row':1,'col':1,'source':'=2+3'}])
            with client.open(path,readonly=True) as book:
                state=book.state;result=book.list_values(r2=1,c2=1);self.assertEqual(result['rows'][0][0]['value'],5);self.assertEqual(book.refresh()['state'],state);self.assertIsNone(book.pending_operation)
                with self.assertRaises(APIError):book.list_values(expected_state='stale')
        self.assertIn('/v1/workbooks/{workbook}/list/values',openapi()['paths'])
