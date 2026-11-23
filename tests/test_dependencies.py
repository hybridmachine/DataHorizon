# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from datahorizon import dependencies
from datahorizon.engine import Workbook,EngineError
from datahorizon.formula import FormulaError


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

    def test_a_cleared_cell_inside_a_range_invalidates_its_reader(self):
        self.apply(cells=[dict(row=2,col=1,source='2'),dict(row=5,col=1,source='3'),dict(row=1,col=2,source='=SUM(A1:A10)')])
        reader=self.key(1,2);self.assertEqual(self.value(1,2),5);self.assertIn(reader,self.book.dependencies.values)
        self.apply(cells=[dict(row=5,col=1,clear=True)]);self.assertNotIn(reader,self.book.dependencies.values)
        self.assertEqual(self.value(1,2),2);self.assertIn(reader,self.book.dependencies.values)
        self.apply('undo');self.assertNotIn(reader,self.book.dependencies.values);self.assertEqual(self.value(1,2),5)

    def test_an_edit_on_one_sheet_invalidates_a_range_reader_on_another(self):
        self.apply('sheet_add',name='Sheet2');self.apply(cells=[dict(row=3,col=1,source='4')])
        self.apply(sheet='Sheet2',cells=[dict(row=1,col=2,source='=SUM(Sheet1!A1:A10)'),dict(row=1,col=3,source='=SUM(A1:A10)')])
        read=lambda col:self.book.read('Sheet2',dict(r1=1,r2=1,c1=col,c2=col))['cells'][0]['value']
        reader,local=(self.book.sheet('Sheet2'),1,2),(self.book.sheet('Sheet2'),1,3)
        self.assertEqual((read(2),read(3)),(4,0));self.assertIn(reader,self.book.dependencies.values)
        self.apply(cells=[dict(row=7,col=1,source='6')])
        # The same rectangle on the reader's own sheet is another sheet's cells: its reader is untouched.
        self.assertNotIn(reader,self.book.dependencies.values);self.assertIn(local,self.book.dependencies.values)
        self.assertEqual((read(2),read(3)),(10,0));self.assertIn(reader,self.book.dependencies.values)

    def test_a_volatile_formula_written_into_a_cached_range_evicts_its_reader_until_it_is_cleared(self):
        self.apply(cells=[dict(row=1,col=4,source='5'),dict(row=1,col=5,source='=SUM(D1:D9)'),dict(row=2,col=5,source='=E1*2')])
        readers=[self.key(1,5),self.key(2,5)];self.assertEqual(self.value(2,5),10);self.assertEqual(self.cached(readers),readers)
        self.apply(cells=[dict(row=7,col=4,source='=YEAR(NOW())')]);self.assertEqual(self.cached(readers),[])
        with patch('datahorizon.formula.utc_clock',side_effect=[datetime(2024,12,31),datetime(2025,1,1),datetime(2026,1,1)]):
            self.assertEqual(self.value(2,5),2*2029);self.assertEqual(self.value(2,5),2*2030);self.assertEqual(self.value(1,5),2031)
        self.assertEqual(self.cached(readers),[])
        self.apply(cells=[dict(row=7,col=4,clear=True)])
        self.assertEqual(self.value(2,5),10);self.assertEqual(self.cached(readers),readers)

    def evaluate(self,rows,col):
        """Read the cells of a column through the dependency cache with a budget no test machine exhausts."""
        calculator=self.book.calculator(seconds=30)
        for row in rows:
            try:calculator.cell(*self.key(row,col))
            except FormulaError:pass

    def unhurried(self,row,col):
        """One cell's value through the dependency cache, with the same budget."""
        return self.book.calculator(seconds=30).cell(*self.key(row,col))

    def range_readers(self,count,source=lambda row:f'=SUM(A{row}:A{row+5})'):
        """B1 to B<count> each read their own small range of column A and are cached; returns their keys."""
        self.apply(cells=[dict(row=row,col=2,source=source(row)) for row in range(1,count+1)])
        self.evaluate(range(1,count+1),2);index=self.book.dependencies
        self.assertEqual((index.info()['cached'],index.info()['edges']),(count,count))
        return [self.key(row,2) for row in range(1,count+1)]

    def test_invalidation_with_4096_cached_range_readers_stays_bounded(self):
        readers=self.range_readers(4096,lambda row:f'=SUM(A{row}:A{row+1})');index=self.book.dependencies
        index.checks=0;self.apply(cells=[dict(row=1,col=1,source='1')])
        # The edited cell, then its one reader, are each checked against every rectangle on the sheet once.
        self.assertEqual(index.checks,2*4096)
        self.assertEqual(self.cached(readers),readers[1:])

    def test_a_large_edit_elsewhere_checks_each_rectangle_once_and_invalidates_nothing(self):
        readers=self.range_readers(4000);index=self.book.dependencies
        for source in ('1','2'):  # the second edit meets the same cache: nothing was evicted by the first
            index.checks=0;self.apply(cells=[dict(row=row,col=col,source=source) for row in range(1,1001) for col in range(10,20)])
            # 10,000 cells against 4,000 rectangles was 40,008,000 checks; now the edit's bounding box against each.
            self.assertEqual(index.checks,4000)
            self.assertEqual(self.cached(readers),readers)

    def test_a_large_edit_invalidates_every_reader_whose_range_meets_its_bounding_box(self):
        readers=self.range_readers(4000);index=self.book.dependencies
        index.checks=0;self.apply(cells=[dict(row=row,col=1,source='1') for row in range(1001,3001)])
        # Rows 1001 to 3000 of column A meet the ranges of B996 to B3000; each is then looked up as a cell itself.
        self.assertEqual(self.cached(readers),readers[:995]+readers[3000:])
        self.assertLessEqual(index.checks,3*4000)
        self.assertEqual([self.value(row,2) for row in (995,996,1001,2999,3000,3001)],[0,1,6,2,1,0])

    def test_scattered_edits_past_the_budget_invalidate_conservatively_and_never_leave_a_stale_value(self):
        readers=self.range_readers(4000);index=self.book.dependencies;rows=range(100,3100,100)
        self.assertGreater(len(rows)*4000,dependencies.INVALIDATION_CHECKS)
        index.checks=0;self.apply(cells=[dict(row=row,col=1,source='1') for row in rows])
        # Only six readers hold each edited cell, but every reader between the first and the last is given up.
        self.assertLessEqual(index.checks,3*4000);self.assertEqual(self.cached(readers),readers[:94]+readers[3000:])
        self.assertEqual([self.value(row,2) for row in (94,95,100,101,2995,3000,3001)],[0,1,1,0,1,1,0])

    def test_a_long_chain_beside_many_ranges_is_followed_without_checking_each_cell_against_each_range(self):
        readers=self.range_readers(2000);keys=self.chain('1',2000);self.assertEqual(self.unhurried(2000,10),2000)
        index=self.book.dependencies;self.assertEqual(len(self.cached(readers+keys)),4000)
        index.checks=0;self.apply(cells=[dict(row=1,col=10,source='5')])
        # The chain is followed through its single-cell edges; its 2,000 cells against 2,000 rectangles was 4,000,000
        # checks and is now their bounding box (J1:J2000) against each rectangle, which meets none.
        self.assertEqual(index.checks,2000);self.assertEqual(self.cached(readers+keys),readers)
        self.assertEqual((self.unhurried(2000,10),self.value(7,2)),(2004,0))

    def test_formulas_chained_through_ranges_clear_the_cache_once_the_budget_is_spent(self):
        count=600;self.apply(cells=[dict(row=row,col=10,source='1' if row<3 else f'=MAX(J{row-2}:J{row-1})') for row in range(1,count+1)]+[dict(row=1,col=4,source='=7+8')])
        self.evaluate(range(1,count+1),10);self.assertEqual(self.value(1,4),15)
        index=self.book.dependencies;self.assertEqual(index.info()['cached'],count+1)
        index.checks=0;self.apply(cells=[dict(row=1,col=10,source='3')])
        # Each step finds one or two more formulas to check against all 598 rectangles: 358,202 checks to the end.
        self.assertLessEqual(index.checks,dependencies.INVALIDATION_CHECKS+count);self.assertEqual(index.info()['cached'],0)
        self.assertEqual((self.unhurried(count,10),self.value(1,4)),(3,15))

    def test_deciding_a_range_reader_does_not_walk_the_uncacheable_cells(self):
        for name,source in (('beside',lambda row:f'=SUM(D{row}:F{row})'),('tall',lambda row:f'=SUM(D$1:D{row})')):
            with self.subTest(shape=name):
                self.tearDown();self.setUp();index=self.book.dependencies
                self.apply(cells=[dict(row=row,col=1,source='=TODAY()') for row in range(1,2001)]+[dict(row=row,col=3,source=source(row)) for row in range(1,1501)])
                self.evaluate(range(1,2001),1);self.assertEqual(len(index.uncacheable),2000)
                index.checks=0;self.evaluate(range(1,1501),3)
                self.assertEqual(index.info()['cached'],1500)
                # Each reader is decided once, from its one edge: before, twice, each walking all 2,000 cells.
                self.assertLessEqual(index.checks,4*1500,index.checks)
                hits=index.hits;index.checks=0;self.evaluate(range(1,1501),3)
                self.assertEqual((index.hits-hits,index.checks),(1500,0))  # a result served from the cache is not decided again

    def test_a_volatile_cell_beside_or_inside_a_range_decides_its_reader(self):
        self.apply(cells=[dict(row=5,col=1,source='=YEAR(NOW())'),dict(row=5,col=5,source='=YEAR(NOW())'),dict(row=9,col=3,source='=1+'),
                          dict(row=1,col=7,source='=COUNT(B1:D4)'),dict(row=2,col=7,source='=COUNT(B1:D5)'),dict(row=3,col=7,source='=COUNT(A5:A5)+COUNT(B6:D8)'),
                          dict(row=4,col=7,source='=COUNT(E1:E9)'),dict(row=5,col=7,source='=IFERROR(COUNT(B6:D9),0)'),dict(row=6,col=7,source='=COUNT(A1:E4)+COUNT(A6:E8)')])
        with patch('datahorizon.formula.utc_clock',return_value=datetime(2024,1,1)):
            self.book.read('Sheet1',dict(r1=1,r2=9,c1=1,c2=7))
        self.assertEqual(self.cached([self.key(row,7) for row in range(1,7)]),[self.key(row,7) for row in (1,2,6)])

    def test_a_calculator_that_gave_up_caching_leaves_nothing_behind(self):
        self.apply(cells=[dict(row=row,col=1,source='=TODAY()') for row in range(1,8001)]+[dict(row=1,col=3,source='=COUNTA(A1:A3)')])
        index=self.book.dependencies
        for first in (1,4001):self.evaluate(range(first,first+4000),1)
        # The second read passed 4,096 cells, cleared the index and stopped recording: 3,903 marks used to stay.
        self.assertEqual((index.info()['nodes'],len(index.uncacheable),len(index.volatile),len(index.unsafe)),(0,0,0,0))
        for _ in range(2):self.assertEqual(self.value(1,3),3);self.assertNotIn(self.key(1,3),index.values)
        self.assertLessEqual(index.uncacheable,set(index.edges))

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
