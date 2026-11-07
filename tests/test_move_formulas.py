"""Bounded same-sheet moves preserve referents, types, history and atomic hooks."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook,EngineError
from datahorizon.formula import move_references,FormulaError
from datahorizon.client import LocalService,APIError


class MoveTests(unittest.TestCase):
    def test_refs_absolute_qualified_inherited_ranges_strings_and_external_targets(self):
        box=(1,1,2,2)
        cases=[('=$A$1+A1+Other!A1+"A1"','=\u0024A$4+A4+Other!A1+"A1"'),
               ("=SUM(('sHeEt1'!$B$2):(A1))","=SUM(('sHeEt1'!$B$5):(A4))"),
               ("='id'!A1+'id'!B2","='id'!A4+'id'!B5"),('=SUM(D1:D2)','=SUM(D1:D2)')]
        for source,expected in cases:self.assertEqual(move_references(source,'Sheet1','Sheet1',box,3,0,target_id='id'),expected)
        for source in ('=SUM(A1:C2)','=SUM(Sheet1!A1:Other!B2)','=SUM(A1:(A2+1))','=A1&"text"'):
            with self.assertRaises(FormulaError):move_references(source,'Sheet1','Sheet1',box,3,0)

    def test_move_preserves_outside_targets_and_rewrites_dependents_with_undo_redo_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'move.dhwb'
            with closing(Workbook(path,create=True)) as book:
                def apply(action='edit',**args):return book.mutate({'action':action,'operation_id':str(uuid.uuid4()),'expected_state':book.state(),**args})
                apply('sheet_add',name='Other');identity=book.sheet('Sheet1')
                apply(cells=[{'row':1,'col':1,'source':'2','kind':'number','style':{'format':'text'}},
                             {'row':1,'col':2,'source':'3'},{'row':1,'col':3,'source':'=$A$1+B1+E1','style':{'bold':True}},
                             {'row':1,'col':5,'source':'10'},{'row':1,'col':6,'source':'=SUM(A1:B1)'},
                             {'row':3,'col':1,'source':'=unsupported&"overwritten"'},
                             {'row':2,'col':1,'source':'','kind':'text'}, {'row':2,'col':2,'source':'NULL source','kind':'null'},
                             {'row':2,'col':3,'source':'=A1','kind':'text'}])
                apply(sheet='Other',cells=[{'row':1,'col':1,'source':f"='sHeEt1'!$A$1+Sheet1!B1+'{identity}'!A1"}])
                before={name:book.read(name,{'r2':6,'c2':6})['cells'] for name in ('Sheet1','Other')}
                request={'action':'move','operation_id':'move','expected_state':book.state(),'cut_state':book.state(),'r1':1,'c1':1,'r2':2,'c2':3,'row':3,'col':1}
                result=book.mutate(request);self.assertEqual(book.mutate(request),result)
                moved={(c['row'],c['col']):c for c in book.read('Sheet1',{'r2':6,'c2':6})['cells']}
                self.assertNotIn((1,1),moved);self.assertEqual(moved[3,3]['source'],'=$A$3+B3+E1');self.assertEqual(moved[3,3]['value'],15)
                self.assertEqual(moved[1,6]['source'],'=SUM(A3:B3)');self.assertEqual(moved[4,1]['input_kind'],'text');self.assertEqual(moved[4,2]['input_kind'],'null')
                self.assertEqual(moved[4,3]['source'],'=A1');self.assertEqual(moved[3,1]['input_kind'],'number')
                self.assertEqual(book.read('Other',{})['cells'][0]['value'],7)
                after={name:book.read(name,{'r2':6,'c2':6})['cells'] for name in before}
                apply('undo');self.assertEqual({name:book.read(name,{'r2':6,'c2':6})['cells'] for name in before},before)
                apply('redo');self.assertEqual({name:book.read(name,{'r2':6,'c2':6})['cells'] for name in before},after)
            with closing(Workbook(path)) as reopened:self.assertEqual(reopened.read('Other',{})['cells'][0]['value'],7)

    def test_overlap_absent_targets_noop_and_partial_range_reject(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'overlap.dhwb',create=True)) as book:
            def apply(action='edit',**args):return book.mutate({'action':action,'operation_id':str(uuid.uuid4()),'expected_state':book.state(),**args})
            apply(cells=[{'row':1,'col':1,'source':'2'},{'row':1,'col':2,'source':'=A1+1'},{'row':2,'col':1,'source':'=$C$1'}])
            apply('move',r2=1,c2=3,row=1,col=2,cut_state=book.state())
            cells={(c['row'],c['col']):c for c in book.read('Sheet1',{'r2':2,'c2':4})['cells']}
            self.assertEqual(cells[1,3]['source'],'=B1+1');self.assertEqual(cells[2,1]['source'],'=$D$1');self.assertNotIn((1,4),cells)
            apply(cells=[{'row':3,'col':1,'source':'=SUM(B1:C1)'}]);before=book.read('Sheet1',{'r2':3,'c2':4})['cells']
            with self.assertRaises(EngineError) as failure:apply('move',r1=1,c1=2,row=4,col=2,cut_state=book.state())
            self.assertEqual(failure.exception.body['code'],'REFERENCE_TRANSFORM_UNAVAILABLE');self.assertEqual(book.read('Sheet1',{'r2':3,'c2':4})['cells'],before)
            apply(cells=[{'row':5,'col':1,'source':'=unsupported&syntax'}])
            result=apply('move',r1=1,c1=2,row=1,col=2,cut_state=book.state());self.assertEqual(result['changed_cells'],0)
            with self.assertRaises(EngineError):apply('move',r1=1,c1=2,row=7,col=2,cut_state=book.state())

    def test_required_handler_rejects_entire_move_and_dependent_rewrites(self):
        with tempfile.TemporaryDirectory() as temp,LocalService(handler=Path(__file__).parent/'fixtures'/'validation_hook.py') as client,client.open(Path(temp)/'hook.dhwb',create=True) as book:
            book.apply('edit',cells=[{'row':1,'col':1,'source':'7'},{'row':2,'col':3,'source':'=A1'}])
            before=book.read(r2=4,c2=4)['cells']
            with self.assertRaises(APIError) as failure:book.apply('move',r1=1,c1=1,row=3,col=1,cut_state=book.state)
            self.assertEqual(failure.exception.body['outcome'],'rolled_back');self.assertEqual(book.read(r2=4,c2=4)['cells'],before)
