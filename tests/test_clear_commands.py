# SPDX-License-Identifier: GPL-3.0-only
"""Managed clear contracts: preserve input identities or roll back the whole range."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook
from datahorizon.client import APIError,LocalService


class ClearTests(unittest.TestCase):
    def test_clear_formats_preserves_numeric_text_empty_text_null_and_formulas(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'clear.dhwb',create=True)) as book:
            def apply(**args): return book.mutate({'action':'edit','operation_id':str(uuid.uuid4()),'expected_state':book.state(),**args})
            original=[{'row':1,'col':1,'source':'0.125','kind':'number','style':{'format':'text'}},
                      {'row':2,'col':1,'source':'=1/8','kind':'formula','style':{'format':'percent2'}},
                      {'row':3,'col':1,'source':'','kind':'text','style':{'bold':True}},
                      {'row':4,'col':1,'source':'null source','kind':'null','style':{'italic':True}},
                      {'row':5,'col':1,'source':'','kind':'auto','style':{'wrap':True}}]
            apply(cells=original)
            apply(cells=[{'row':r,'col':1,'style':{},'replace_style':True} for r in range(1,6)],label='Clear formatting')
            cells=book.read('Sheet1',{'r2':5})['cells']
            self.assertEqual([(c['row'],c['source'],c['input_kind']) for c in cells],[(1,'0.125','number'),(2,'=1/8','formula'),(3,'','text'),(4,'null source','null')])
            self.assertTrue(all(not c['style'] for c in cells));self.assertEqual(cells[0]['value'],0.125)
            book.mutate({'action':'undo','operation_id':'undo','expected_state':book.state()})
            self.assertEqual(len(book.read('Sheet1',{'r2':5})['cells']),5)

    def test_clear_all_required_handler_rejection_leaves_both_inputs_and_styles(self):
        with tempfile.TemporaryDirectory() as temp,LocalService(handler=Path(__file__).parent/'fixtures'/'validation_hook.py') as client,client.open(Path(temp)/'clear.dhwb',create=True) as book:
            book.apply('edit',cells=[{'row':1,'col':1,'source':'7','style':{'bold':True}},{'row':1,'col':2,'style':{'format':'fixed2'}}])
            before=book.read(r2=1,c2=2)['cells']
            with self.assertRaises(APIError) as failure: book.apply('edit',cells=[{'row':1,'col':1,'clear':True},{'row':1,'col':2,'clear':True}],label='Clear all')
            self.assertEqual(failure.exception.body['outcome'],'rolled_back')
            self.assertEqual(book.read(r2=1,c2=2)['cells'],before)
            book.apply('edit',cells=[{'row':1,'col':1,'style':{},'replace_style':True},{'row':1,'col':2,'style':{},'replace_style':True}],label='Clear formatting')
            self.assertEqual([c['value'] for c in book.read(r2=1,c2=2)['cells']],[7,14])
