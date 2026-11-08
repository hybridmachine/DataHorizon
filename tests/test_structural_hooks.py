from pathlib import Path
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook,EngineError


class StructuralHooks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.book=Workbook(Path(self.temp.name)/'structure.dhwb',create=True)
        self.apply(cells=[dict(row=2,col=1,source='7'),dict(row=1,col=2,source='=Sheet1!$A$2')])
        self.book.handler=Path(__file__).parent/'fixtures'/'structural_hook.py'
    def tearDown(self):self.book.close();self.temp.cleanup()
    def apply(self,action='edit',**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))

    def test_row_structure_references_and_generated_changes_share_transaction(self):
        self.apply('rows_insert',start=2)
        cells=self.book.read('Sheet1',dict(r2=3,c2=8))['cells'];lookup={(c['row'],c['col']):c for c in cells}
        self.assertEqual(lookup[1,2]['source'],'=Sheet1!$A$3');self.assertEqual(lookup[1,2]['value'],7)
        self.assertEqual(lookup[1,8]['source'],'after:rows_insert')
        self.assertEqual(lookup[3,1]['value'],7)

    def test_after_event_rejection_rolls_back_names_references_and_generated_cells(self):
        before=self.book.read_workbook();state=self.book.state()
        with self.assertRaises(EngineError) as caught:self.apply('sheet_rename',sheet='Sheet1',name='Reject')
        self.assertEqual(caught.exception.body['outcome'],'rolled_back');self.assertEqual(self.book.state(),state)
        self.assertEqual(self.book.read_workbook(),before)
        self.assertEqual(self.book.operation(caught.exception.body['operation_id'])['outcome'],'rolled_back')

    def test_rename_duplicate_delete_and_legacy_fail_closed(self):
        self.apply('sheet_rename',sheet='Sheet1',name='Renamed')
        self.assertEqual(self.book.read('Renamed',dict(c2=2))['cells'][0]['source'],"='Renamed'!$A$2")
        self.apply('sheet_duplicate',sheet='Renamed',name='Copy');self.apply('sheet_delete',sheet='Copy')
        self.book.handler=Path(__file__).parent/'fixtures'/'validation_hook.py'
        with self.assertRaises(EngineError) as caught:self.apply('sheet_add',name='Legacy')
        self.assertEqual(caught.exception.body['code'],'UNSUPPORTED_HOOK_SCOPE')
