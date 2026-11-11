# SPDX-License-Identifier: GPL-3.0-only
"""Stored names follow whole-row/column and sheet changes."""
from pathlib import Path
import tempfile
import unittest
import uuid

from datahorizon.engine import Workbook
from datahorizon.formula import transform_structure
from datahorizon.names import read


class NameStructureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'names.dhwb'
        self.book=Workbook(self.path,create=True);self.addCleanup(self.book.close)
        self.apply('sheet_rename',sheet='Sheet1',name='Supplies')

    def apply(self,action,**args):
        return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))

    def save(self,name,reference,**args):
        return self.apply('name_save',name=name,reference=reference,**args)

    def read(self,**args):
        return read(self.book,dict(expected_state=self.book.state(),**args))

    def version(self):
        return self.book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]

    def test_rectangles_match_independent_enumeration(self):
        # Tag every named cell, move the tags the way the grid moves cells, take bounds.
        def expected(rect,axis,start,count,delete):
            r1,c1,r2,c2=rect;tags=[]
            for r in range(r1,r2+1):
                for c in range(c1,c2+1):
                    v=r if axis=='row' else c
                    if delete:
                        if start<=v<start+count:continue
                        v-=count if v>=start+count else 0
                    else:v+=count if v>=start else 0
                    tags.append((v,c) if axis=='row' else (r,v))
            if not tags:return None
            rows=[t[0] for t in tags];cols=[t[1] for t in tags]
            return (min(rows),min(cols),max(rows),max(cols))
        from datahorizon.names import adjust_structure
        cases=0
        for axis in ('row','col'):
            for delete in (False,True):
                for r1 in range(1,7):
                    for r2 in range(r1,7):
                        for c1 in (1,3):
                            for c2 in range(c1,c1+2):
                                for count in (1,2,3):
                                    for start in range(1,8):
                                        rect=(r1,c1,r2,c2)
                                        if axis=='col':rect=(c1,r1,c2,r2)
                                        self.fresh(rect)
                                        action=('rows_' if axis=='row' else 'columns_')+('delete' if delete else 'insert')
                                        self.apply(action,sheet='Supplies',start=start,count=count)
                                        item=self.read()['names'][0];want=expected(rect,axis,start,count,delete)
                                        got=None if item['broken'] else (item['row1'],item['col1'],item['row2'],item['col2'])
                                        self.assertEqual(got,want,(axis,delete,rect,start,count));cases+=1
        self.assertGreater(cases,5000)

    def fresh(self,rect):
        for item in self.read()['names']:self.apply('name_delete',name_id=item['id'])
        from datahorizon.formula import column_name
        r1,c1,r2,c2=rect
        self.save('Box',f"Supplies!{column_name(c1)}{r1}:{column_name(c2)}{r2}")

    def test_insert_inside_expands_and_before_shifts(self):
        self.save('Quantities','Supplies!B2:B4')
        self.apply('rows_insert',sheet='Supplies',start=3,count=1)
        self.assertEqual(self.read()['names'][0]['reference'],"'Supplies'!B2:B5")
        self.apply('rows_insert',sheet='Supplies',start=1,count=1)
        self.assertEqual(self.read()['names'][0]['reference'],"'Supplies'!B3:B6")

    def test_delete_all_cells_breaks_name_and_reports_it(self):
        self.save('Quantities','Supplies!B2:B4');self.save('Other','Supplies!D1')
        receipt=self.apply('rows_delete',sheet='Supplies',start=2,count=3)
        self.assertEqual(receipt['broken_names'],['Quantities'])
        by={n['name']:n for n in self.read()['names']}
        self.assertTrue(by['Quantities']['broken']);self.assertIsNone(by['Quantities']['reference'])
        self.assertFalse(by['Other']['broken'])
        self.assertEqual(self.apply('rows_delete',sheet='Supplies',start=9,count=1).get('broken_names'),None)
        reopened=Workbook(self.path)
        try:self.assertTrue(read(reopened,dict(expected_state=reopened.state()))['names'][0]['broken'])
        finally:reopened.close()

    def test_sheet_delete_breaks_rename_keeps_duplicate_does_not_copy(self):
        self.apply('sheet_add',name='Other')
        self.save('Here','Supplies!A1:B2');self.save('There','Other!A1')
        self.apply('sheet_duplicate',sheet='Supplies',name='Copy')
        self.assertEqual(len(self.read()['names']),2)
        self.apply('sheet_rename',sheet='Other',name='Elsewhere')
        self.assertEqual(self.read(name='There')['names'][0]['reference'],"'Elsewhere'!A1")
        receipt=self.apply('sheet_delete',sheet='Elsewhere')
        self.assertEqual(receipt['broken_names'],['There'])
        by={n['name']:n for n in self.read()['names']}
        self.assertTrue(by['There']['broken']);self.assertFalse(by['Here']['broken'])
        self.assertNotIn('broken_names',self.apply('sheet_delete',sheet='Copy'))

    def test_no_names_leaves_receipts_and_version_unchanged(self):
        self.apply('rows_insert',sheet='Supplies',start=1,count=1)
        self.assertEqual(self.version(),1)
        self.assertNotIn('broken_names',self.apply('rows_delete',sheet='Supplies',start=1,count=1))
        self.assertEqual(self.version(),1)

    def test_existing_formula_transform_results_unchanged(self):
        self.assertEqual(transform_structure('=SUM($A$1:A5)+B3','Main','Main','row',3,2),('=SUM($A$1:A7)+B5',0))
        self.assertEqual(transform_structure('=A3','Main','Main','row',3,2,True),('=#REF!',1))


if __name__=='__main__':unittest.main()
