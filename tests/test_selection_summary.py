# SPDX-License-Identifier: GPL-3.0-only
"""Snapshot summaries count stored data without writes, hooks or partial aggregates."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch
from datahorizon.engine import Workbook,EngineError
from datahorizon.client import LocalService,APIError
from datahorizon.service import openapi


class SummaryTests(unittest.TestCase):
    def test_scalar_types_errors_overflow_empty_ranges_and_limits(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'summary.dhwb',create=True)) as book:
            def apply(cells):return book.mutate({'action':'edit','operation_id':str(uuid.uuid4()),'expected_state':book.state(),'cells':cells})
            apply([{'row':1,'col':1,'source':'0.125','style':{'format':'percent2'}},
                   {'row':2,'col':1,'source':'=3+1'}, {'row':3,'col':1,'source':'12','kind':'text'},
                   {'row':4,'col':1,'source':'TRUE'}, {'row':5,'col':1,'source':'','kind':'text'},
                   {'row':6,'col':1,'source':'NULL','kind':'null'}, {'row':7,'col':1,'style':{'bold':True}}])
            state=book.state();result=book.summary('Sheet1',{'r2':8})
            self.assertEqual((result['count'],result['numeric_count'],result['selected_cells']),(5,2,8))
            self.assertEqual(result['sum'],4.125);self.assertEqual(result['average'],2.0625)
            self.assertEqual(result['state'],state);self.assertEqual(book.state(),state)
            empty=book.summary('Sheet1',{'r1':100,'r2':100,'c1':1,'c2':100})
            self.assertEqual((empty['count'],empty['sum'],empty['average']),(0,0,None))
            apply([{'row':8,'col':1,'source':'=1/0'},{'row':9,'col':1,'source':'=A9'}])
            bad=book.summary('Sheet1',{'r2':9});self.assertEqual(bad['count'],7)
            self.assertEqual(bad['error_count'],2);self.assertEqual({e['code'] for e in bad['errors']},{'#DIV/0!','#CYCLE!'})
            self.assertIsNone(bad['sum']);self.assertIsNone(bad['average'])
            apply([{'row':10,'col':1,'source':'1e308'},{'row':11,'col':1,'source':'1e308'}])
            huge=book.summary('Sheet1',{'r1':10,'r2':11});self.assertIsNone(huge['sum']);self.assertEqual(huge['numeric_error'],'#NUM!')
            json.dumps(huge,allow_nan=False)
            apply([{'row':12,'col':1,'source':'=SUM(A1:A1048576)'}])
            limited=book.summary('Sheet1',{'r1':12});self.assertEqual(limited['errors'][0]['code'],'#LIMIT!');self.assertIsNone(limited['sum'])
            for args in ({'r2':10001},{'rows':[1,2]},{'r1':0},{'c2':16385}):
                with self.assertRaises(EngineError):book.summary('Sheet1',args)
            apply([{'row':r,'col':2,'source':'=1/0'} for r in range(1,102)])
            errors=book.summary('Sheet1',{'r2':101,'c1':2,'c2':2});self.assertEqual(errors['error_count'],101)
            self.assertEqual(len(errors['errors']),100);self.assertTrue(errors['errors_truncated'])

    def test_snapshot_survives_external_writer_and_next_summary_refreshes(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'snapshot.dhwb'
            with closing(Workbook(path,create=True)) as book:
                book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[{'row':1,'col':1,'source':'1'},{'row':2,'col':1,'source':'2'}]})
                book.db.execute('PRAGMA journal_mode=WAL');book.refresh()
                old=book.state();read=book.range_cells;sheet=book.sheet('Sheet1')
                def rows(*args):
                    for index,item in enumerate(read(*args)):
                        yield item
                        if index==0:
                            with closing(sqlite3.connect(path)) as writer:
                                writer.execute('UPDATE cells SET source=? WHERE sheet_id=? AND row=2 AND col=1',('20',sheet));writer.commit()
                with patch.object(book,'range_cells',rows):result=book.summary('Sheet1',{'r2':2})
                self.assertEqual(result['sum'],3);self.assertEqual(result['state'],old)
                fresh=book.summary('Sheet1',{'r2':2});self.assertEqual(fresh['sum'],21);self.assertNotEqual(fresh['state'],old)

    def test_sdk_readonly_no_handler_history_or_operation_receipt(self):
        with tempfile.TemporaryDirectory() as temp,LocalService(handler=Path(__file__).parent/'fixtures'/'validation_hook.py') as client:
            path=Path(temp)/'sdk.dhwb'
            with client.open(path,create=True) as book:
                book.apply('edit',cells=[{'row':1,'col':1,'source':'7'}]);before=book.refresh()['state']
                with closing(sqlite3.connect(path)) as db:counts=db.execute('SELECT count(*) FROM operations').fetchone()
                result=book.summary(r2=1,c2=2);self.assertEqual(result['sum'],21)
                self.assertEqual(book.refresh()['state'],before)
                with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM operations').fetchone(),counts)
            with client.open(path,readonly=True) as reader:
                self.assertEqual(reader.summary(r2=1,c2=2)['sum'],21)
                with self.assertRaises(APIError):reader.summary(r2=10001)
            api=openapi();route=api['paths']['/v1/workbooks/{workbook}/summary']['post']
            self.assertEqual(route['responses']['200']['content']['application/json']['schema']['$ref'],'#/components/schemas/Summary')

    def test_input_budget_reports_failure_instead_of_partial_results(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'bytes.dhwb',create=True)) as book:
            # Separate bounded transactions stay under ordinary before/after history limits.
            for start in (1,41):
                book.mutate({'action':'edit','operation_id':str(start),'expected_state':book.state(),'cells':[{'row':r,'col':1,'source':'x'*60000,'kind':'text'} for r in range(start,start+40)]})
            with self.assertRaises(EngineError) as failure:book.summary('Sheet1',{'r2':80})
            self.assertEqual(failure.exception.body['code'],'READ_LIMIT')
