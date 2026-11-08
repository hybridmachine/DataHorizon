"""Bounded Unicode substring positions and scalar/error semantics."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from datahorizon.formula import FormulaError,Parser
from datahorizon.engine import Workbook
from datahorizon.interchange import xlsx_export,xlsx_preview
from datahorizon.ods import ods_export,ods_preview
from test_text_functions import calculate

class SubstringTests(unittest.TestCase):
    def test_defaults_counts_unicode_and_unformatted_scalar_text(self):
        for formula,value in [('=LEFT("00123")','0'),('=RIGHT("00123")','3'),('=LEFT("00123",2)','00'),('=RIGHT("00123",0)',''),('=LEFT("x",100000000000000000000)','x'),('=MID("abc",20,2)',''),('=MID("abc",2,20)','bc'),('=LEFT("abc",1.9)','a'),('=RIGHT("abc",1.9)','c'),('=MID("abc",1.9,1.9)','a'),('=MID("😀é",1,1)','😀'),('=MID("😀é",3,1)','́'),('=LEFT(TRUE,2)','TR'),('=LEFT(B1,2)',''),('=LEFT("abc",FALSE)',''),('=RIGHT("abc",B1)','')]:
            with self.subTest(formula=formula):self.assertEqual(calculate(formula),value)
        self.assertEqual(calculate('=RIGHT(12.5,2)'),'.5')

    def test_invalid_positions_arity_errors_ranges_and_limits(self):
        for formula,code in [('=LEFT()','#NAME?'),('=MID("a",1)','#NAME?'),('=LEFT("a",-0.1)','#VALUE!'),('=RIGHT("a",-1)','#VALUE!'),('=MID("a",0,1)','#VALUE!'),('=MID("a",0.9,1)','#VALUE!'),('=MID("a",1,-1)','#VALUE!'),('=LEFT("a","1")','#VALUE!'),('=LEFT(B1:B2)','#VALUE!'),('=RIGHT("a",B1:B2)','#VALUE!'),('=MID(1/0,1,0)','#DIV/0!'),('=RIGHT(A1,1)','#CYCLE!')]:
            with self.subTest(formula=formula),self.assertRaises(FormulaError) as error:calculate(formula)
            self.assertEqual(error.exception.code,code)
        cells={(1,2):dict(kind='text',source='x'*60000,value='x'*60000)}
        self.assertEqual(calculate('=LEFT(B1,2)',cells),'xx')
        with self.assertRaises(FormulaError) as error:calculate('=IFERROR(RIGHT(B1,60000),"hidden")',cells)
        self.assertEqual(error.exception.code,'#LIMIT!')

    def test_native_and_package_roundtrip_preserves_sources_and_identifier_values(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'substring.dhwb',create=True)) as book:
            book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[{'row':1,'col':1,'source':'00123','kind':'text'},{'row':1,'col':2,'source':'=LEFT(A1,2)'},{'row':2,'col':2,'source':'=RIGHT(A1,2)'},{'row':3,'col':2,'source':'=MID(A1,2,3)'}]})
            snapshot=book.read('Sheet1',{'r2':3,'c2':2});self.assertEqual([c['value'] for c in snapshot['cells']],["00123","00","23","012"])
            for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
                for mode in ('formulas','values'):
                    package=export(snapshot,{'mode':mode});result=preview({'file_base64':package['file_base64'],'mode':mode});self.assertTrue(result['can_import'],result['issues'])
                    cell=next(c for c in result['cells'] if c['row']==3 and c['col']==2)
                    if mode=='formulas':self.assertEqual(Parser(cell['source'][1:]).parse(),Parser('MID(A1,2,3)').parse())
                    else:self.assertEqual(cell['source'],'012');self.assertEqual(cell['kind'],'text')
            book.mutate({'action':'undo','operation_id':'undo','expected_state':book.state()});self.assertEqual(book.read('Sheet1',{'r2':3,'c2':2})['cells'],[])
