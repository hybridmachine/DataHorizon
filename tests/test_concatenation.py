# SPDX-License-Identifier: GPL-3.0-only
"""Concatenation precedence, scalar/range values, bounds and package spelling."""
import base64
from contextlib import closing
import io
from pathlib import Path
import tempfile
import unittest
import zipfile
from datahorizon.formula import FormulaError,Parser
from datahorizon.engine import Workbook,EngineError
from datahorizon.interchange import xlsx_export,xlsx_preview,native_formula_to_xlsx,xlsx_formula_to_native
from datahorizon.ods import ods_export,ods_preview
from test_text_functions import calculate

class ConcatenationTests(unittest.TestCase):
    def test_ampersand_precedence_coercion_and_exact_text(self):
        for formula,value in [('=1+2&3*4','312'),('=1&2&3','123'),('="a"&"b"="ab"',True),('="00123"&FALSE&B1','00123FALSE'),('=CONCAT("A",TRUE,12.5)','ATRUE12.5'),('=CONCAT(B1:B2)',''),('=CONCAT("=SUM(1)")','=SUM(1)'),('=IFERROR("x"&(1/0),"safe")','safe')]:
            with self.subTest(formula=formula):self.assertEqual(calculate(formula),value)
        values={(1,2):dict(source='00123',kind='text',value='00123'),(1,3):dict(source='TRUE',kind='boolean',value=True),(2,2):dict(source='2',kind='number',value=2),(2,3):dict(source='',kind='text',value='')}
        self.assertEqual(calculate('=CONCAT(B1:C2)',values),'00123TRUE2')
        for formula,code in [('=CONCAT()','#NAME?'),('=CONCAT('+','.join('1' for _ in range(254))+')','#NAME?'),('=B1:B2&"x"','#VALUE!'),('=CONCAT(A1)','#CYCLE!'),('=CONCAT(1/0)','#DIV/0!')]:
            with self.subTest(formula=formula),self.assertRaises(FormulaError) as error:calculate(formula)
            self.assertEqual(error.exception.code,code)

    def test_incremental_limits_and_error_propagation(self):
        text='x'*20000;cells={(1,2):dict(source=text,kind='text',value=text)}
        for formula in ('=B1&B1','=CONCAT(B1,B1)','=IFERROR(CONCAT(B1,B1),"hidden")'):
            with self.assertRaises(FormulaError) as error:calculate(formula,cells)
            self.assertEqual(error.exception.code,'#LIMIT!')
        self.assertEqual(calculate('=CONCAT(B1,"x")',cells),text+'x')

    def test_namespace_mapping_preserves_strings_and_unknown_functions(self):
        source='=CONCAT("CONCAT(",\'CONCAT(name)\'!A1)'
        encoded=native_formula_to_xlsx(source,sheet_names=['CONCAT(name)'])
        self.assertEqual(encoded,'=_xlfn.CONCAT("CONCAT(",\'CONCAT(name)\'!A1)');self.assertEqual(xlsx_formula_to_native(encoded),source)
        self.assertEqual(xlsx_formula_to_native('=_xlfn.UNSUPPORTED(1)'),'=UNSUPPORTED(1)')  # admission, not import, decides evaluability

    def test_native_packages_and_transforms_preserve_semantics_and_types(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'concat.dhwb',create=True)) as book:
            book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[{'row':1,'col':1,'source':'00123','kind':'text'},{'row':1,'col':2,'source':'=A1&"X"'},{'row':2,'col':2,'source':'=CONCAT(A1,TRUE,0)'}]})
            snapshot=book.read('Sheet1',{'r2':2,'c2':2});self.assertEqual(snapshot['cells'][-1]['value'],'00123TRUE0')
            for format,export,preview in (('xlsx',xlsx_export,xlsx_preview),('ods',ods_export,ods_preview)):
                for mode in ('formulas','values'):
                    package=export(snapshot,{'mode':mode});result=preview({'file_base64':package['file_base64'],'mode':mode});self.assertTrue(result['can_import'],result['issues']);cell=next(c for c in result['cells'] if c['row']==2 and c['col']==2)
                    if mode=='formulas':
                        self.assertEqual(Parser(cell['source'][1:]).parse(),Parser('CONCAT(A1,TRUE,0)').parse());archive=zipfile.ZipFile(io.BytesIO(base64.b64decode(package['file_base64'])));content=archive.read('xl/worksheets/sheet1.xml' if format=='xlsx' else 'content.xml').decode();self.assertIn('_xlfn.CONCAT' if format=='xlsx' else 'COM.MICROSOFT.CONCAT',content)
                    else:self.assertEqual(cell['source'],'00123TRUE0');self.assertEqual(cell['kind'],'text')
            book.mutate({'action':'fill','operation_id':'fill','expected_state':book.state(),'r1':1,'r2':2,'c1':2,'c2':2,'mode':'copy'})
            self.assertEqual(book.read('Sheet1',{'r2':2,'c2':2})['cells'][-1]['source'],'=A2&"X"')
