"""Typed exact first-match lookup, lazy return/fallback and package aliases."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from datahorizon.formula import FormulaError
from datahorizon.engine import Workbook
from datahorizon.interchange import xlsx_export,xlsx_preview
from datahorizon.ods import ods_export,ods_preview
from test_text_functions import calculate
from test_conditional_functions import cell

class XLookupTests(unittest.TestCase):
    def test_typed_exact_first_match_and_lazy_fallback_returns(self):
        values={(r,2):cell(value) for r,value in enumerate(('00123',123,True,'pEn','PEN','*',''),1)}
        values.update({(r,3):cell(value) for r,value in enumerate(('id','number','bool','first','second','star','empty'),1)})
        for key,result in [('"00123"','id'),('123','number'),('TRUE','bool'),('"PEN"','first'),('"*"','star'),('""','empty')]:
            self.assertEqual(calculate('=XLOOKUP('+key+',B1:B7,C1:C7,1/0)',values),result)
        self.assertEqual(calculate('=XLOOKUP("none",B1:B7,C1:C7,"fallback",0,1)',values),'fallback')
        self.assertEqual(calculate('=IFERROR(XLOOKUP("none",B1:B7,C1:C7),"missing")',values),'missing')
        with self.assertRaises(FormulaError) as error:calculate('=XLOOKUP("none",B1:B7,C1:C7)',values)
        self.assertEqual(error.exception.code,'#N/A')
        values[2,3]=dict(source='=1/0',kind='formula',value=None)
        self.assertEqual(calculate('=XLOOKUP("00123",B1:B7,C1:C7)',values),'id')
        with self.assertRaises(FormulaError):calculate('=XLOOKUP(123,B1:B7,C1:C7)',values)

    def test_horizontal_blank_results_shapes_modes_cycles_and_limits(self):
        values={(2,2):cell('a'),(2,3):cell('b'),(3,2):cell('x'),(3,3):cell('')}
        self.assertEqual(calculate('=XLOOKUP("b",B2:C2,B3:C3)',values),'')
        self.assertEqual(calculate('=XLOOKUP("a",B2:C2,B4:C4)',values),0)
        self.assertEqual(calculate('=XLOOKUP(D1,B1:B2,C1:C2)',{}),0)
        for formula,code in [('=XLOOKUP(1)','#NAME?'),('=XLOOKUP(1,B1:C2,D1:E2)','#VALUE!'),('=XLOOKUP(1,B1:B2,C1:D1)','#VALUE!'),('=XLOOKUP(1,B1:B2,C1:C2,0,1)','#VALUE!'),('=XLOOKUP(1,B1:B2,C1:C2,0,0,-1)','#VALUE!'),('=XLOOKUP(B1:B2,C1:C2,D1:D2)','#VALUE!'),('=XLOOKUP(1,#REF!,C1:C2)','#REF!'),('=XLOOKUP(1,A1:A2,B1:B2)','#CYCLE!'),('=XLOOKUP(1,B1:B10001,C1:C10001)','#LIMIT!')]:
            with self.subTest(formula=formula),self.assertRaises(FormulaError) as error:calculate(formula)
            self.assertEqual(error.exception.code,code)
        with self.assertRaises(FormulaError) as error:calculate('=IFERROR(XLOOKUP("a",B1:B2,C1:C2),"hidden")',{(1,2):cell('a'),(1,3):cell('x'*40000)})
        self.assertEqual(error.exception.code,'#LIMIT!')

    def test_cross_sheet_persistence_and_package_formula_value_roundtrips(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'lookup.dhwb'
            with closing(Workbook(path,create=True)) as book:
                def apply(action='edit',**args):return book.mutate({'action':action,'operation_id':str(book.state())+action,'expected_state':book.state(),**args})
                apply('sheet_add',name='Other');apply(sheet='Other',cells=[{'row':1,'col':1,'source':'00123','kind':'text'},{'row':2,'col':1,'source':'00456','kind':'text'},{'row':1,'col':2,'source':'Pen','kind':'text'},{'row':2,'col':2,'source':'Book','kind':'text'}]);apply(cells=[{'row':1,'col':1,'source':'=XLOOKUP("00123",Other!A1:A2,Other!B1:B2)'}])
                self.assertEqual(book.read('Sheet1',{})['cells'][0]['value'],'Pen')
                snapshot=book.read_workbook()
                for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
                    for mode in ('formulas','values'):
                        package=export(snapshot,{'mode':mode,'scope':'workbook'});result=preview({'file_base64':package['file_base64'],'mode':mode,'scope':'workbook'});self.assertTrue(result['can_import'],result['issues']);first=result['workbook_sheets'][0]['cells'][0]
                        if mode=='formulas':
                            with closing(Workbook(Path(temp)/('roundtrip-'+str(export.__name__)+'.dhwb'),create=True)) as imported:
                                imported.mutate({'action':'workbook_import','operation_id':'import','expected_state':imported.state(),'sheets':result['workbook_sheets']})
                                self.assertEqual(imported.read('Sheet1',{})['cells'][0]['value'],'Pen')
                        else:self.assertEqual(first['source'],'Pen');self.assertEqual(first['kind'],'text')
            with closing(Workbook(path)) as book:self.assertEqual(book.read('Sheet1',{})['cells'][0]['value'],'Pen')
