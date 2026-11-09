# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook,EngineError
from datahorizon.record_operations import review,copy,export,projected_formula
from datahorizon.interchange import xlsx_preview


class RecordOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.book=Workbook(Path(self.temp.name)/'records.dhwb',create=True)
        self.apply(cells=[dict(row=r,col=c,source=source) for r,c,source in ((1,1,'Label'),(1,2,'Value'),(2,1,'A'),(2,2,'2'),(3,1,'Hidden'),(3,2,'1'),(4,1,'B'),(4,2,'4'),(2,3,'=B2*2'),(4,3,'=$B$4*2'))])
        self.apply('list_declare',id='Records',spec=dict(r2=4,c2=3,header=True))
    def tearDown(self):self.book.close();self.temp.cleanup()
    def apply(self,action='edit',**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))
    def request(self,**args):return dict(id='Records',expected_state=self.book.state(),filters=[dict(col=2,op='>',value=1)],**args)

    def test_filtered_bulk_clear_retains_hidden_records_and_undoes_once(self):
        targets=review(self.book,self.request())['records'];ids=[item['id'] for item in targets]
        self.assertEqual([item['row'] for item in targets],[2,4])
        self.apply('list_edit',id='Records',record_ids=ids,columns=[2],mode='clear',clear_scope='contents')
        cells=self.book.read('Sheet1',dict(r1=2,r2=4,c1=2,c2=2))['cells'];self.assertEqual([(c['row'],c['value']) for c in cells],[(3,1)])
        self.apply('undo');self.assertEqual([c['value'] for c in self.book.read('Sheet1',dict(r1=2,r2=4,c1=2,c2=2))['cells']],[2,1,4])

    def test_formula_copy_paste_uses_each_original_source_coordinate(self):
        copied=copy(self.book,self.request(columns=[3]));self.assertEqual(copied['source_rows'],[2,4])
        self.apply('paste',row=6,col=3,rows=copied['rows'],record_projection=True)
        cells=self.book.read('Sheet1',dict(r1=6,r2=7,c1=3,c2=3))['cells'];self.assertEqual([c['source'] for c in cells],['=B6*2','=$B$4*2'])

    def test_projection_formula_export_remaps_referents_and_rejects_excluded_targets(self):
        result=export(self.book,'xlsx',self.request(mode='formulas'));preview=xlsx_preview(result);self.assertTrue(preview['can_import'],preview)
        formulas=[c['source'] for c in preview['cells'] if c['kind']=='formula'];self.assertEqual(formulas,['=B2*2','=$B$3*2'])
        self.apply(cells=[dict(row=2,col=3,source='=B3')])
        with self.assertRaises(EngineError):export(self.book,'xlsx',self.request(mode='formulas'))
        self.assertTrue(xlsx_preview(export(self.book,'xlsx',self.request(mode='values')))['can_import'])
        with self.assertRaises(EngineError):projected_formula('=SUM(A2:A4)','Sheet1',{2:1,4:2},{1:1})

    def test_reviewed_ids_and_input_validation_reject_atomically(self):
        before=self.book.read_workbook()
        with self.assertRaises(EngineError):self.apply('list_edit',id='Records',record_ids=['missing'],columns=[2],mode='clear')
        self.assertEqual(self.book.read_workbook(),before)
        ids=[item['id'] for item in review(self.book,self.request())['records']]
        self.apply('list_edit',id='Records',record_ids=ids,columns=[2],mode='all',repeat=True,rows=[[dict(source='9',input_kind='number')]])
        self.assertEqual([c['value'] for c in self.book.read('Sheet1',dict(r1=2,r2=4,c1=2,c2=2))['cells']],[9,1,9])
