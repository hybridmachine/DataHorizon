"""Percentage formatting preserves numeric fractions, types, history and file values."""
from contextlib import closing
import io
from pathlib import Path
import tempfile
import unittest

from datahorizon.engine import Workbook,display,EngineError
from datahorizon.interchange import xlsx_export,xlsx_preview
from datahorizon.ods import ods_export,ods_preview


class PercentageTests(unittest.TestCase):
    def test_finite_display_text_boolean_null_and_extremes(self):
        for value,expected in [(0.125,'12.50%'),(-0.125,'-12.50%'),(0,'0.00%'),('00123','00123'),(True,'TRUE'),(None,'')]:
            self.assertEqual(display(value,{'format':'percent2'}),expected)
        for value in (1e308,-1e308):
            shown=display(value,{'format':'percent2'})
            self.assertTrue(shown.endswith('.00%'));self.assertNotIn('inf',shown)
        from decimal import localcontext,Rounded
        with localcontext() as outer:
            outer.traps[Rounded]=True;outer.prec=2
            self.assertEqual(display(0.125,{'format':'percent2'}),'12.50%')

    def test_style_preserves_inputs_copy_history_reopen_and_csv_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'percentage.dhwb'
            with closing(Workbook(path,create=True)) as book:
                def apply(action='edit',**args): return book.mutate({'action':action,'operation_id':str(len(book.undo))+str(book.state()),'expected_state':book.state(),**args})
                apply(cells=[{'row':1,'col':1,'source':'0.125'},{'row':1,'col':2,'source':'=A1*2','kind':'formula'},{'row':1,'col':3,'source':'00123','kind':'text'}])
                apply(cells=[{'row':1,'col':c,'style':{'format':'percent2'}} for c in (1,2,3)])
                cells=book.read('Sheet1',{'r2':1,'c2':3})['cells']
                self.assertEqual([c['display'] for c in cells],['12.50%','25.00%','00123'])
                self.assertEqual([c['source'] for c in cells],['0.125','=A1*2','00123'])
                self.assertEqual(book.csv_export('Sheet1',{'r2':1,'c2':2})['csv'],'0.125,0.25\r\n')
                apply('undo');self.assertEqual(book.read('Sheet1',{})['cells'][0]['style'],{})
                apply('redo'); apply('paste',row=2,col=1,mode='formats',rows=[[cells[0]]])
                self.assertEqual(book.read('Sheet1',{'r1':2,'r2':2})['cells'][0]['kind'],'blank')
                with self.assertRaises(EngineError): apply(cells=[{'row':1,'col':1,'source':'9'},{'row':1,'col':2,'style':{'format':'percent0'}}])
                self.assertEqual(book.read('Sheet1',{})['cells'][0]['value'],0.125)
            with closing(Workbook(path)) as book:
                self.assertEqual(book.read('Sheet1',{})['cells'][0]['display'],'12.50%')

    def test_both_file_formats_preserve_style_and_unscaled_numeric_values(self):
        snapshot={'state':'s','range':[1,1,1,2],'cells':[{'row':1,'col':1,'source':'0.125','kind':'number','value':0.125,'calculation':'current','style':{'format':'percent2'}},{'row':1,'col':2,'source':'=A1*2','kind':'formula','value':0.25,'calculation':'current','style':{'format':'percent2'}}]}
        for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
            reviewed=preview(export(snapshot,{}));self.assertTrue(reviewed['can_import'],reviewed)
            self.assertEqual([c['style']['format'] for c in reviewed['cells']],['percent2','percent2'])
            self.assertEqual(float(reviewed['cells'][0]['source']),0.125)
            literal=preview(export(snapshot,{'mode':'values'}))
            self.assertEqual([float(c['source']) for c in literal['cells']],[0.125,0.25])
