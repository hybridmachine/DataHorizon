"""Explicit scalar Text semantics, errors and interchange stage boundaries."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from datahorizon.formula import Calculator,FormulaError
from datahorizon.engine import Workbook
from datahorizon.client import LocalService
from datahorizon.interchange import xlsx_export,xlsx_preview
from datahorizon.ods import ods_export,ods_preview


def calculate(formula,cells=None):
    inputs={(1,1):{'source':formula,'kind':'formula','value':None},**(cells or {})}
    return Calculator(lambda s,r,c:inputs.get((r,c)),lambda s:s,seconds=1).cell('sheet',1,1)


class TextFunctionTests(unittest.TestCase):
    def test_scalar_conversion_unicode_ascii_trim_and_exact_text(self):
        for formula,result in [('=LEN(" a ")',3),('=LEN("😀é")',3),('=LEN(TRUE)',4),('=LEN(FALSE)',5),('=LEN(12.5)',4),('=LEN(1)',1),('=LEN(-0)',1),('=LEN(B1)',0),('=TRIM("  a   b  ")','a b'),('=TRIM(" \ta  b\u00a0 ")','\ta b\u00a0'),('=UPPER("straße")','STRASSE'),('=LOWER("ÄBC")','äbc'),('=UPPER("00123")','00123'),('=LOWER(TRUE)','true'),('=TRIM(B1)',''),('=UPPER("=sum(1)")','=SUM(1)')]:
            with self.subTest(formula=formula):self.assertEqual(calculate(formula),result)
        for kind,value,source in [('text','',''),('null',None,'NULL'),('text','00123','00123'),('number',.25,'0.25')]:
            self.assertEqual(calculate('=LEN(B1)',{(1,2):dict(kind=kind,value=value,source=source)}),len('' if value is None else str(value)))

    def test_errors_arity_range_cycle_and_result_limits_are_visible(self):
        for formula,code in [('=LEN()','#NAME?'),('=TRIM(1,2)','#NAME?'),('=UPPER(B1:B2)','#VALUE!'),('=LEN(1/0)','#DIV/0!'),('=LOWER(A1)','#CYCLE!'),('=IFERROR(UPPER(B1),"hidden")','#LIMIT!')]:
            with self.subTest(formula=formula),self.assertRaises(FormulaError) as error:
                calculate(formula,{(1,2):dict(source='ß'*20000,kind='text',value='ß'*20000)})
            self.assertEqual(error.exception.code,code)
        self.assertEqual(calculate('=IFERROR(LEN(1/0),7)'),7)
        self.assertEqual(calculate('=LEN(B1)',{(1,2):dict(source='x'*60000,kind='text',value='x'*60000)}),60000)
        self.assertEqual(calculate('=TRIM(B1)',{(1,2):dict(source=' '*60000+'x',kind='text',value=' '*60000+'x')}),'x')

    def test_native_reopen_undo_and_formula_values_interchange(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'text.dhwb'
            with closing(Workbook(path,create=True)) as book:
                book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[{'row':1,'col':1,'source':'  Data   Horizon  ','kind':'text'},{'row':1,'col':2,'source':'=UPPER(TRIM(A1))'},{'row':2,'col':2,'source':'=LEN(A1)'}]})
                snapshot=book.read('Sheet1',{'r2':2,'c2':2});self.assertEqual(snapshot['cells'][1]['value'],'DATA HORIZON');self.assertEqual(snapshot['cells'][0]['source'],'  Data   Horizon  ')
                for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
                    for mode in ('formulas','values'):
                        package=export(snapshot,{'mode':mode});result=preview({'file_base64':package['file_base64'],'mode':mode});self.assertTrue(result['can_import'],result['issues']);inputs={(c['row'],c['col']):c for c in result['cells']}
                        self.assertEqual(inputs[1,2]['source'],'=UPPER(TRIM(A1))' if mode=='formulas' else 'DATA HORIZON')
                book.mutate({'action':'undo','operation_id':'undo','expected_state':book.state()});self.assertEqual(book.read('Sheet1',{'r2':2,'c2':2})['cells'],[])
                book.mutate({'action':'redo','operation_id':'redo','expected_state':book.state()})
            with closing(Workbook(path)) as book:
                self.assertEqual(book.read('Sheet1',{'r2':2,'c2':2})['cells'][1]['value'],'DATA HORIZON')

    def test_readonly_sdk_uses_same_engine_and_reports_catalog(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'sdk.dhwb'
            with client.open(path,create=True) as book:book.apply('edit',cells=[{'row':1,'col':1,'source':'=LOWER(TRIM(" X "))'}])
            with client.open(path,readonly=True) as book:
                self.assertEqual(book.read(r2=1,c2=1)['cells'][0]['value'],'x');state=book.state;self.assertEqual(book.refresh()['state'],state)
            self.assertTrue({'LEN','TRIM','UPPER','LOWER'}<={item['name'] for item in client.functions()['functions']})
