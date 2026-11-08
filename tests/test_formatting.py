"""Portable formatting must preserve inputs, transaction/history and typed file data."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from datahorizon.engine import Workbook, EngineError, display, validate_style
from datahorizon.formats import number_format_code, simple_number_style
from datahorizon.interchange import xlsx_export,xlsx_preview
from datahorizon.ods import ods_export,ods_preview


class FormattingTests(unittest.TestCase):
    def test_precision_bounds_types_legacy_and_extreme_display(self):
        for bad in (-1,16,True,2.5,'2',None):
            with self.assertRaises(EngineError): validate_style({'decimals':bad})
        for mode in ('number','percent'):
            for places in range(16):
                style={'format':mode,'decimals':places};validate_style(style)
                self.assertEqual(number_format_code(simple_number_style(number_format_code(style))),number_format_code(style))
                self.assertNotIn('inf',display(1e308,style))
                for value,expected in [('00123','00123'),(True,'TRUE'),(None,'')]: self.assertEqual(display(value,style),expected)
        self.assertEqual(display(1.23456,{'format':'number','decimals':4}),'1.2346')
        self.assertEqual(display(.12345,{'format':'percent','decimals':3}),'12.345%')
        self.assertEqual(display(1.23456,{'format':'fixed2','decimals':8}),'1.23')
        self.assertEqual(display(.125,{'format':'percent2','decimals':8}),'12.50%')
        self.assertEqual(display(1.23456,{'decimals':1}),'1.2')
        self.assertEqual(display(1.23456,{'format':'general','decimals':1}),'1.23456')

    def test_precision_patch_preserves_mixed_families_sources_history_and_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'formats.dhwb'
            with closing(Workbook(path,create=True)) as book:
                def apply(action='edit',**args): return book.mutate({'action':action,'operation_id':book.state()+str(len(book.undo)),'expected_state':book.state(),**args})
                apply(cells=[{'row':1,'col':1,'source':'.125','style':{'format':'percent2','bold':True}},{'row':1,'col':2,'source':'=A1*2'},{'row':1,'col':3,'source':'00123','kind':'text'}])
                apply(cells=[{'row':1,'col':c,'style':{'decimals':3}} for c in (1,2,3)])
                cells=book.read('Sheet1',{'c2':3})['cells'];self.assertEqual([c['display'] for c in cells],['12.500%','0.250','00123'])
                self.assertEqual([c['source'] for c in cells],['.125','=A1*2','00123']);self.assertTrue(cells[0]['style']['bold'])
                self.assertEqual(book.csv_export('Sheet1',{'c2':2})['csv'],'0.125,0.25\r\n')
                apply('undo');self.assertEqual(book.read('Sheet1',{})['cells'][0]['style']['format'],'percent2');apply('redo')
                with self.assertRaises(EngineError): apply(cells=[{'row':1,'col':1,'source':'9'},{'row':1,'col':2,'style':{'decimals':16}}])
                self.assertEqual(book.read('Sheet1',{})['cells'][0]['source'],'.125')
            with closing(Workbook(path)) as book:self.assertEqual(book.read('Sheet1',{})['cells'][0]['display'],'12.500%')

    def test_every_precision_preserves_numeric_values_and_display_codes_in_packages(self):
        styles=[{'format':mode,'decimals':places} for mode in ('number','percent') for places in range(16)]
        styles += [{**s,'grouping':True} for s in styles]
        snapshot={'state':'s','range':[1,1,len(styles),1],'cells':[{'row':r,'col':1,'source':'0.125','kind':'number','value':.125,'calculation':'current','style':style} for r,style in enumerate(styles,1)]}
        for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
            result=preview(export(snapshot,{}));self.assertTrue(result['can_import'],result)
            self.assertEqual([float(c['source']) for c in result['cells']],[.125]*len(styles))
            self.assertEqual([number_format_code(c['style']) for c in result['cells']],[number_format_code(s) for s in styles])

    def test_grouping_is_invariant_keeps_magnitudes_and_rejects_non_boolean(self):
        for bad in (0,1,'true',None):
            with self.assertRaises(EngineError):validate_style({'grouping':bad})
        self.assertEqual(display(-12345.625,{'format':'number','decimals':3,'grouping':True}),'-12,345.625')
        self.assertEqual(display(123.456,{'format':'percent','decimals':1,'grouping':True}),'12,345.6%')
        self.assertEqual(display('0012345',{'grouping':True}),'0012345')
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'g.dhwb',create=True)) as book:
            def apply(cells):return book.mutate({'action':'edit','operation_id':book.state(),'expected_state':book.state(),'cells':cells})
            apply([{'row':1,'col':1,'source':'123.456','style':{'format':'percent2','decimals':8}}])
            apply([{'row':1,'col':1,'style':{'grouping':True}}]);cell=book.read('Sheet1',{})['cells'][0]
            self.assertEqual(cell['display'],'12,345.60%');self.assertEqual(cell['value'],123.456);self.assertEqual(cell['source'],'123.456')
            self.assertEqual(book.csv_export('Sheet1',{})['csv'],'123.456\r\n')

    def test_currency_labels_precision_grouping_and_packages_do_not_convert(self):
        for bad in ('usd','US','USDD','$US','UŚD',123,None):
            with self.assertRaises(EngineError):validate_style({'currency':bad})
        self.assertEqual(display(-1234.5,{'format':'currency','currency':'EUR','grouping':True}),'-1,234.50 EUR')
        self.assertEqual(display(.125,{'format':'currency'}),'0.12 USD')
        styles=[{'format':'currency','currency':code,'decimals':n,'grouping':g} for code in ('USD','EUR','JPY','XYZ') for n in (0,2,4,15) for g in (False,True)]
        snapshot={'state':'s','range':[1,1,len(styles),1],'cells':[{'row':r,'col':1,'source':'-1234.5','kind':'number','value':-1234.5,'calculation':'current','style':s} for r,s in enumerate(styles,1)]}
        for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
            reviewed=preview(export(snapshot,{}));self.assertTrue(reviewed['can_import'],reviewed)
            self.assertEqual([float(c['source']) for c in reviewed['cells']],[-1234.5]*len(styles))
            self.assertEqual([number_format_code(c['style']) for c in reviewed['cells']],[number_format_code(s) for s in styles])
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'currency.dhwb',create=True)) as book:
            def apply(cells):book.mutate({'action':'edit','operation_id':book.state(),'expected_state':book.state(),'cells':cells})
            apply([{'row':1,'col':1,'source':'1234.5','style':{'format':'currency','currency':'EUR'}}]);apply([{'row':1,'col':1,'style':{'decimals':3,'grouping':True}}])
            cell=book.read('Sheet1',{})['cells'][0];self.assertEqual(cell['display'],'1,234.500 EUR');self.assertEqual(cell['value'],1234.5)
