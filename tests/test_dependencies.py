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

    def test_transactional_calculation_never_publishes_uncommitted_values(self):
        self.apply(cells=[dict(row=1,col=1,source='2'),dict(row=1,col=2,source='=A1*2')]);self.assertEqual(self.value(1,2),4)
        self.book.db.execute('BEGIN IMMEDIATE')
        try:
            self.book.db.execute("UPDATE cells SET source='9' WHERE col=1")
            self.assertEqual(self.book.calculator(transactional=True).cell(self.book.sheet('Sheet1'),1,2),18)
        finally:self.book.db.execute('ROLLBACK')
        self.assertEqual(self.value(1,2),4)
