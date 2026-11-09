# SPDX-License-Identifier: GPL-3.0-only
"""State-checked Find pages never skip or duplicate source coordinates."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datahorizon.engine import Workbook,EngineError
from datahorizon.client import LocalService,APIError
from datahorizon.service import openapi

class FindPageTests(unittest.TestCase):
    def seed(self,book):
        book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[{'row':row,'col':col,'source':'hit '+str(row)} for row in range(1,752) for col in (1,2)]})
        return {'r2':751,'c2':2,'needle':'hit','expected_state':book.state()}

    def test_all_pages_have_consistent_count_order_bounds_and_no_history(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'find.dhwb',create=True)) as book:
            args=self.seed(book);state=book.state();matches=[];offset=0
            while True:
                result=book.find('Sheet1',{**args,'offset':offset});self.assertEqual(result['count'],1502);self.assertEqual(result['offset'],offset);self.assertEqual(result['limit'],500);self.assertEqual(result['state'],state);self.assertLessEqual(len(result['matches']),500);matches+=result['matches']
                if not result['has_more']:break
                offset=result['next_offset']
            self.assertEqual([(m['row'],m['col']) for m in matches],[(row,col) for row in range(1,752) for col in (1,2)])
            self.assertEqual(book.find('Sheet1',{**args,'offset':1502})['matches'],[])
            self.assertEqual(len(book.find('Sheet1',{**args,'limit':1})['matches']),1)
            for invalid in ({'offset':-1},{'offset':True},{'limit':501},{'limit':0},{'offset':1,'expected_state':None},{'expected_state':''}):
                with self.assertRaises(EngineError):book.find('Sheet1',{**args,**invalid})
            self.assertFalse(book.db.in_transaction);self.assertEqual(book.state(),state)
            book.mutate({'action':'undo','operation_id':'undo','expected_state':state});self.assertEqual(book.find('Sheet1',{'r2':751,'c2':2,'needle':'hit'})['count'],0)

    def test_external_change_stales_later_page_but_not_its_read_snapshot(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'find.dhwb',create=True)) as book:
            args=self.seed(book);book.db.execute('PRAGMA journal_mode=WAL');book.refresh();args['expected_state']=book.state();read=book.range_cells
            def cells(*arguments):
                for index,cell in enumerate(read(*arguments)):
                    if index==0:
                        with closing(sqlite3.connect(book.path)) as writer:writer.execute("DELETE FROM cells WHERE row=751");writer.commit()
                    yield cell
            with patch.object(book,'range_cells',cells):result=book.find('Sheet1',{**args,'offset':1500})
            self.assertEqual(result['count'],1502);self.assertEqual([m['row'] for m in result['matches']],[751,751])
            with self.assertRaises(EngineError) as error:book.find('Sheet1',{**args,'offset':500})
            self.assertEqual(error.exception.body['code'],'STALE_STATE');self.assertFalse(book.db.in_transaction)
            self.assertEqual(book.find('Sheet1',{**args,'expected_state':book.state()})['count'],1500)

    def test_display_previews_readonly_sdk_and_documented_limits(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'find.dhwb'
            with client.open(path,create=True) as book:book.apply('edit',cells=[{'row':r,'col':1,'source':'=1/8','style':{'format':'percent2'}} for r in range(1,503)])
            with client.open(path,readonly=True) as book:
                state=book.state;result=book.find('12.50%',field='display',r2=502,c2=1,offset=500)
                self.assertEqual([m['row'] for m in result['matches']],[501,502]);self.assertEqual(book.refresh()['state'],state);self.assertIsNone(book.pending_operation)
                with self.assertRaises(APIError):book.find('12.50%',field='display',r2=502,c2=1,offset=500,expected_state='stale')
        schema=openapi()['components']['schemas']['FindRequest'];self.assertEqual(schema['properties']['limit']['maximum'],500)
