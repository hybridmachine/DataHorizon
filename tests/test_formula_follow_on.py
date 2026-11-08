"""Typed follow-on formula stages, bounded failures and package recalculation."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from datahorizon.formula import FormulaError
from datahorizon.functions import function_catalog
from datahorizon.engine import Workbook
from datahorizon.interchange import xlsx_export,xlsx_preview
from datahorizon.ods import ods_export,ods_preview
from test_text_functions import calculate
from test_conditional_functions import cell

class FollowOnTests(unittest.TestCase):
    def errors(self,cases,values=None):
        for formula,code in cases:
            with self.subTest(formula=formula),self.assertRaises(FormulaError) as error:calculate(formula,values)
            self.assertEqual(error.exception.code,code)

    def test_inspection_exact_types_errors_and_cycles(self):
        for name,value,expected in [('ISBLANK',None,True),('ISBLANK','',False),('ISNUMBER','00123',False),('ISNUMBER',True,False),('ISNUMBER',123,True),('ISTEXT','',True),('ISLOGICAL',True,True),('ISLOGICAL',1,False)]:
            self.assertIs(calculate('='+name+'(B1)',{(1,2):cell(value)}),expected)
        self.assertIs(calculate('=ISERROR(1/0)'),True);self.assertIs(calculate('=ISNA(1/0)'),False)
        self.assertIs(calculate('=ISNA(XLOOKUP(9,B1,C1))'),True)
        self.assertIs(calculate('=ISNUMBER(1/0)'),False)
        self.errors([('=ISERROR(A1)','#CYCLE!'),('=ISNA(SUM(B1:B10001))','#LIMIT!'),('=ISERROR(B1:B2)','#VALUE!'),('=ISBLANK()','#NAME?')])

    def test_ifna_only_missing_and_lazy_fallback(self):
        self.assertEqual(calculate('=IFNA(XLOOKUP(9,B1,C1),"missing")'),'missing')
        self.assertEqual(calculate('=IFNA("00123",1/0)'),'00123');self.assertEqual(calculate('=IFNA(B1,1/0)'),'')
        self.errors([('=IFNA(1/0,7)','#DIV/0!'),('=IFNA(A1,7)','#CYCLE!'),('=IFNA(SUM(B1:B10001),7)','#LIMIT!'),('=IFNA(B1:B2,7)','#VALUE!'),('=IFNA(7)','#NAME?'),('=IFNA(XLOOKUP(9,B1,C1),1/0)','#DIV/0!')])

    def test_exact_case_sensitive_unicode_and_scalar_spelling(self):
        for formula,expected in [('=EXACT("Data","data")',False),('=EXACT("00123",123)',False),('=EXACT(123,"123")',True),('=EXACT(TRUE,"TRUE")',True),('=EXACT(B1,"")',True),('=EXACT("é","é")',False),('=EXACT("x ","x")',False),('=EXACT("😀","😀")',True)]:self.assertIs(calculate(formula),expected)
        self.errors([('=EXACT(B1:B2,1)','#VALUE!'),('=EXACT(1/0,"x")','#DIV/0!'),('=EXACT(A1,"x")','#CYCLE!'),('=EXACT(1)','#NAME?')])
        self.assertTrue(calculate('=EXACT(B1,B1)',{(1,2):cell('x'*60000)}))

    def test_catalog_formulas_recalculate_after_native_package_reimport(self):
        with tempfile.TemporaryDirectory() as temp:
            for index,item in enumerate(function_catalog()['functions']):
                example=item['examples'][0]
                with closing(Workbook(Path(temp)/(str(index)+'.dhwb'),create=True)) as book:
                    book.mutate(dict(action='edit',operation_id='seed',expected_state=book.state(),cells=[*example.get('inputs',[]),dict(row=1,col=1,source=example['formula'])]))
                    snapshot=book.read('Sheet1',dict(r2=4,c2=4))
                    for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
                        data=preview(dict(file_base64=export(snapshot,dict(mode='formulas'))['file_base64'],mode='formulas'));self.assertTrue(data['can_import'],data['issues'])
                        with closing(Workbook(Path(temp)/(str(index)+export.__name__+'.dhwb'),create=True)) as imported:
                            imported.mutate(dict(action='edit',operation_id='import',expected_state=imported.state(),cells=data['cells']))
                            value=imported.read('Sheet1',dict(r2=1,c2=1))['cells'][0];self.assertEqual(value['calculation'],'current',example['formula']);self.assertEqual(value['value'],example['result'],example['formula'])
