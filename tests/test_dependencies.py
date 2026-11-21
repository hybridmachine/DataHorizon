# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from datahorizon.engine import Workbook,EngineError


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'deps.dhwb';self.book=Workbook(self.path,create=True)
    def tearDown(self):self.book.close();self.temp.cleanup()
    def apply(self,action='edit',**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))
    def value(self,row=1,col=1):return self.book.read('Sheet1',dict(r1=row,r2=row,c1=col,c2=col))['cells'][0]['value']

    def test_transitive_ranges_absent_cells_and_unrelated_cache_reuse(self):
        self.apply(cells=[dict(row=1,col=1,source='2'),dict(row=1,col=2,source='=SUM(A1:A3)'),dict(row=1,col=3,source='=B1*2'),dict(row=1,col=4,source='=7+8')])
        self.assertEqual(self.value(1,3),4);self.assertEqual(self.value(1,4),15)
        untouched=(self.book.sheet('Sheet1'),1,4)
        self.apply(cells=[dict(row=3,col=1,source='3')])
        self.assertIn(untouched,self.book.dependencies.values)
        self.assertEqual(self.value(1,3),10)
        hits=self.book.dependencies.hits;self.assertEqual(self.value(1,4),15);self.assertGreater(self.book.dependencies.hits,hits)
        self.apply('undo');self.assertEqual(self.value(1,3),4)

    def test_cross_sheet_and_external_refresh_rebuild_without_replay(self):
        self.apply('sheet_add',name='Other');self.apply(sheet='Other',cells=[dict(row=1,col=1,source='3')])
        self.apply(cells=[dict(row=1,col=1,source="='Other'!A1+2")]);self.assertEqual(self.value(),5)
        self.apply(sheet='Other',cells=[dict(row=1,col=1,source='4')]);self.assertEqual(self.value(),6)
        with closing(sqlite3.connect(self.path)) as direct,direct:direct.execute("UPDATE cells SET source='9' WHERE sheet_id=?",(self.book.sheet('Other'),))
        self.assertEqual(self.value(),11)
        self.book.close();self.book=Workbook(self.path);self.assertEqual(self.book.dependencies.info()['cached'],0)

    def test_volatile_transitive_results_never_reuse_a_previous_clock(self):
        self.apply(cells=[dict(row=1,col=1,source='=NOW()'),dict(row=1,col=2,source='=YEAR(A1)')])
        with patch('datahorizon.formula.utc_clock',side_effect=[datetime(2024,12,31),datetime(2025,1,1)]):
            self.assertEqual(self.value(1,2),2024);self.assertEqual(self.value(1,2),2025)
        self.assertNotIn((self.book.sheet('Sheet1'),1,2),self.book.dependencies.values)

    def test_lazy_missing_references_do_not_change_semantics(self):
        self.apply(cells=[dict(row=1,col=1,source='=IF(FALSE,Missing!A1,7)')]);self.assertEqual(self.value(),7)

    def key(self,row,col):return self.book.sheet('Sheet1'),row,col

    def test_a_large_range_does_not_clear_the_cache(self):
        self.apply(cells=[dict(row=1,col=1,source='2'),dict(row=60000,col=1,source='3'),dict(row=1,col=2,source='=SUM(A1:A60000)')])
        self.assertEqual(self.value(1,2),5);hits=self.book.dependencies.hits
        self.assertEqual(self.value(1,2),5)
        self.assertGreaterEqual(self.book.dependencies.info()['cached'],1);self.assertGreater(self.book.dependencies.hits,hits)
        self.assertEqual(self.book.dependencies.edges[self.key(1,2)],{(self.key(1,1)[0],1,1,60000,1)})

    def test_an_edit_inside_a_range_invalidates_its_reader(self):
        self.apply(cells=[dict(row=2,col=1,source='2'),dict(row=3,col=1,source='=C3*2'),dict(row=3,col=3,source='=D3+1'),
                          dict(row=3,col=4,source='1'),dict(row=1,col=2,source='=SUM(A1:A10)')])
        reader=self.key(1,2)
        for cell,expected in ((None,6),(dict(row=2,col=1,source='5'),9),(dict(row=7,col=1,source='10'),19),(dict(row=3,col=4,source='4'),25)):
            if cell:self.apply(cells=[cell]);self.assertNotIn(reader,self.book.dependencies.values)
            self.assertEqual(self.value(1,2),expected);self.assertIn(reader,self.book.dependencies.values)

    def test_a_volatile_formula_inside_a_range_is_not_cached(self):
        self.apply(cells=[dict(row=1,col=1,source='1'),dict(row=2,col=1,source='=YEAR(NOW())'),dict(row=1,col=2,source='=SUM(A1:A3)')])
        with patch('datahorizon.formula.utc_clock',side_effect=[datetime(2024,12,31),datetime(2025,1,1)]):
            self.assertEqual(self.value(1,2),2025);self.assertEqual(self.value(1,2),2026)
        self.assertNotIn(self.key(1,2),self.book.dependencies.values)

    def test_an_unparseable_formula_inside_a_range_is_not_cached(self):
        self.apply(cells=[dict(row=1,col=1,source='1'),dict(row=2,col=1,source='=SUM('),dict(row=1,col=2,source='=IFERROR(SUM(A1:A3),7)')])
        self.assertEqual(self.value(1,2),7);self.assertIn(self.key(2,1),self.book.dependencies.unsafe)
        self.assertNotIn(self.key(1,2),self.book.dependencies.values)

    def test_invalidation_with_4096_cached_range_readers_stays_bounded(self):
        self.apply(cells=[dict(row=row,col=2,source=f'=SUM(A{row}:A{row+1})') for row in range(1,4097)])
        self.book.read('Sheet1',dict(r1=1,r2=4096,c1=2,c2=2));index=self.book.dependencies
        self.assertEqual((index.info()['cached'],index.info()['edges']),(4096,4096))
        index.checks=0;self.apply(cells=[dict(row=1,col=1,source='1')])
        # The edited cell, then its one reader, are each checked against every rectangle on the sheet once.
        self.assertTrue(0<index.checks<=2*4096,index.checks)
        self.assertNotIn(self.key(1,2),index.values);self.assertEqual(index.info()['cached'],4095)

    def chain(self,head,length,second=None):
        """J1 holds head, J2 second (default =J1+1) and each later J<r> =J<r-1>+1."""
        self.apply(cells=[dict(row=1,col=10,source=head)]+[dict(row=row,col=10,source=second if row==2 and second else f'=J{row-1}+1') for row in range(2,length+1)])
        return [self.key(row,10) for row in range(1,length+1)]

    def cached(self,keys):return [key for key in keys if key in self.book.dependencies.values]

    def test_saving_a_chain_costs_work_linear_in_its_length(self):
        class Counted(dict):
            def get(self,*args):
                Counted.calls+=1;return super().get(*args)
        Counted.calls=0;keys=self.chain('1',2000);index=self.book.dependencies;index.edges=Counted()
        calculator=self.book.calculator(seconds=30);calls=Counted.calls
        self.assertEqual(calculator.cell(*keys[-1]),2000);self.assertEqual(len(self.cached(keys)),2000)
        # Each of the 2,000 formulas is linked once and saved at most twice, each save looking at its own edges only.
        self.assertLessEqual(Counted.calls-calls,4*2000)

    def test_a_chain_headed_by_a_volatile_or_unparseable_formula_is_never_cached(self):
        for head,second in (('=YEAR(NOW())',None),('=SUM(','=IFERROR(J1,0)+1')):
            with self.subTest(head=head):
                self.tearDown();self.setUp();keys=self.chain(head,50,second)
                self.book.calculator(seconds=30).cell(*keys[-1]);self.assertEqual(self.cached(keys),[])
                for key in reversed(keys):self.value(key[1],key[2])
                self.assertEqual(self.cached(keys),[])

    def test_a_chain_whose_head_becomes_volatile_is_not_served_stale(self):
        keys=self.chain('1',50);self.assertEqual(self.value(50,10),50);self.assertEqual(len(self.cached(keys)),50)
        self.apply(cells=[dict(row=1,col=10,source='=YEAR(NOW())')])
        with patch('datahorizon.formula.utc_clock',side_effect=[datetime(2024,12,31),datetime(2025,1,1)]):
            self.assertEqual(self.value(50,10),2024+49);self.assertEqual(self.value(50,10),2025+49)
        self.assertEqual(self.cached(keys),[])

    def test_transactional_calculation_never_publishes_uncommitted_values(self):
        self.apply(cells=[dict(row=1,col=1,source='2'),dict(row=1,col=2,source='=A1*2')]);self.assertEqual(self.value(1,2),4)
        self.book.db.execute('BEGIN IMMEDIATE')
        try:
            self.book.db.execute("UPDATE cells SET source='9' WHERE col=1")
            self.assertEqual(self.book.calculator(transactional=True).cell(self.book.sheet('Sheet1'),1,2),18)
        finally:self.book.db.execute('ROLLBACK')
        self.assertEqual(self.value(1,2),4)
