# SPDX-License-Identifier: GPL-3.0-only
"""Independent fill seeds, exact native types, translation and transactional failures."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook, EngineError

class FillTests(unittest.TestCase):
    def test_down_and_right_snapshot_inputs_formulas_styles_blanks_and_history(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'fill.dhwb',create=True)) as book:
            def apply(action='edit',**args):return book.mutate({'action':action,'operation_id':str(uuid.uuid4()),'expected_state':book.state(),**args})
            apply(cells=[{'row':1,'col':1,'source':'3','kind':'number','style':{'format':'text'}},
                         {'row':1,'col':2,'source':'=$A1+A$1+$A$1','kind':'formula','style':{'bold':True}},
                         {'row':1,'col':3,'source':'','kind':'text'},
                         {'row':1,'col':4,'source':'NULL source','kind':'null'},
                         {'row':3,'col':5,'source':'erase','style':{'italic':True}}])
            apply('fill',mode='down',r2=3,c2=5)
            cells={(c['row'],c['col']):c for c in book.read('Sheet1',{'r2':3,'c2':5})['cells']}
            self.assertEqual(cells[3,1]['input_kind'],'number');self.assertEqual(cells[3,1]['value'],3)
            self.assertEqual(cells[3,2]['source'],'=$A3+A$1+$A$1');self.assertTrue(cells[3,2]['style']['bold'])
            self.assertEqual(cells[3,3]['input_kind'],'text');self.assertEqual(cells[3,4]['input_kind'],'null')
            self.assertNotIn((3,5),cells)
            apply('undo')
            self.assertEqual(book.read('Sheet1',{'r1':3,'c1':5,'r2':3,'c2':5})['cells'][0]['source'],'erase')
            apply('redo')
            apply('fill',mode='right',r2=3,c2=3)
            self.assertTrue(all(c['input_kind']=='number' and c['value']==3 for c in book.read('Sheet1',{'r2':3,'c2':3})['cells']))
            before=book.read('Sheet1',{'r2':3,'c2':5})['cells']
            with self.assertRaises(EngineError):apply('fill',mode='diagonal',r2=3,c2=5)
            self.assertEqual(book.read('Sheet1',{'r2':3,'c2':5})['cells'],before)

    def test_right_uses_each_rows_left_seed_and_absolute_components(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'right.dhwb',create=True)) as book:
            def apply(action='edit',**args):return book.mutate({'action':action,'operation_id':str(uuid.uuid4()),'expected_state':book.state(),**args})
            apply(cells=[{'row':1,'col':1,'source':'0012','kind':'text'},
                         {'row':2,'col':1,'source':'=$D2+D$1+$D$1','kind':'formula'},
                         {'row':3,'col':1,'source':'5','kind':'number','style':{'format':'percent2'}}])
            apply('fill',mode='right',r2=3,c2=3)
            cells={(c['row'],c['col']):c for c in book.read('Sheet1',{'r2':3,'c2':3})['cells']}
            self.assertEqual(cells[1,3]['value'],'0012')
            self.assertEqual(cells[2,3]['source'],'=$D2+F$1+$D$1')
            self.assertEqual(cells[3,3]['value'],5);self.assertEqual(cells[3,3]['display'],'500.00%')

    def test_required_handler_rejects_fill_atomically(self):
        from datahorizon.client import LocalService,APIError
        with tempfile.TemporaryDirectory() as temp:
            handler=Path(temp)/'hook.py'
            handler.write_text("def on_change(ctx,changes):\n    for c in changes:\n        if (c['row'],c['col'])==(2,1):\n            ctx.set(c['sheet'],2,2,'generated')\n            raise ValueError('Reject row 2')\n")
            with LocalService(handler=handler) as client,client.open(Path(temp)/'fill.dhwb',create=True) as book:
                book.apply('edit',cells=[{'row':1,'col':1,'source':'7','style':{'bold':True}}])
                before=book.read(r2=2,c2=2)['cells']
                with self.assertRaises(APIError) as failure:book.apply('fill',mode='down',r2=2,c2=2)
                self.assertEqual(failure.exception.body['outcome'],'rolled_back')
                self.assertEqual(book.read(r2=2,c2=2)['cells'],before)
