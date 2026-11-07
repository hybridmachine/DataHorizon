"""Declared formula tiers: shared engine, bounds, error handling and interchange."""
import base64
from contextlib import closing
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from datahorizon.engine import Workbook
from datahorizon.formula import Calculator,FormulaError,Parser
from datahorizon.interchange import xlsx_export,xlsx_preview
from datahorizon.ods import ods_export,ods_preview


def evaluate(source,cells=None,seconds=1):
    def read(sheet,row,col): return (cells or {}).get((sheet,row,col))
    return Calculator(read,lambda name:name,seconds).evaluate(Parser(source.removeprefix('=')).parse(),'Sheet1')


def literal(value,kind='number'):
    return {'source':str(value),'kind':kind,'value':value,'style':{}}


class AggregateTests(unittest.TestCase):
    def test_numeric_reference_and_direct_scalar_coercions(self):
        cells={('Sheet1',1,1):literal(2),('Sheet1',2,1):literal(4),('Sheet1',3,1):literal('7','text'),('Sheet1',4,1):literal(True,'boolean'),('Sheet1',5,1):literal('','text')}
        for function,expected in [('AVERAGE',3),('MIN',2),('MAX',4),('COUNT',2),('COUNTA',5)]:
            self.assertEqual(evaluate(f'{function}(A1:A6)',cells),expected)
        self.assertEqual(evaluate('AVERAGE("2",TRUE,3)'),2)
        self.assertEqual(evaluate('MIN("2",TRUE,3)'),1)
        self.assertEqual(evaluate('MAX("2",TRUE,3)'),3)
        self.assertEqual(evaluate('COUNT("2",TRUE,"text",FALSE)'),3)
        self.assertEqual(evaluate('COUNTA("",FALSE,0)'),3)
        self.assertEqual(evaluate('MIN(A6:A8)',cells),0)
        with self.assertRaises(FormulaError) as failure: evaluate('AVERAGE(A6:A8)',cells)
        self.assertEqual(failure.exception.code,'#DIV/0!')

    def test_errors_counting_cycles_and_limits_are_distinct(self):
        cells={('Sheet1',1,1):{'source':'=1/0','kind':'formula','style':{}},('Sheet1',2,1):literal(4)}
        self.assertEqual(evaluate('COUNT(A1:A3)',cells),1)
        self.assertEqual(evaluate('COUNTA(A1:A3)',cells),2)
        self.assertEqual(evaluate('COUNT(1/0,"bad",2)'),1)
        for function in ('AVERAGE','MIN','MAX'):
            with self.assertRaises(FormulaError) as failure: evaluate(f'{function}(A1:A2)',cells)
            self.assertEqual(failure.exception.code,'#DIV/0!')
        cells[('Sheet1',1,1)]={'source':'=A1','kind':'formula','style':{}}
        for function in ('COUNT','COUNTA'):
            with self.assertRaises(FormulaError) as failure: evaluate(f'{function}(A1)',cells)
            self.assertEqual(failure.exception.code,'#CYCLE!')
            with self.assertRaises(FormulaError) as failure: evaluate(f'{function}(A1:A1048576)',{})
            self.assertEqual(failure.exception.code,'#LIMIT!')
            with self.assertRaises(FormulaError) as failure: evaluate(f'{function}(1)',seconds=-1)
            self.assertEqual(failure.exception.code,'#LIMIT!')

    def test_arity_invalid_text_overflow_and_cross_sheet_ranges(self):
        for source,code in [('MIN()','#NAME?'),('COUNT()','#NAME?'),('AVERAGE("bad")','#VALUE!'),('AVERAGE(1e308,1e308)','#NUM!')]:
            with self.assertRaises(FormulaError) as failure: evaluate(source)
            self.assertEqual(failure.exception.code,code)
        cells={('Other',1,1):literal(2),('Other',2,1):literal(4)}
        self.assertEqual(evaluate('AVERAGE(Other!$A$1:A2)',cells),3)

    def test_managed_reopen_and_both_interchange_formats(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'aggregates.dhwb'
            with closing(Workbook(path,create=True)) as book:
                book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[{'row':1,'col':1,'source':'2'},{'row':2,'col':1,'source':'4'},*({'row':1,'col':c,'source':f'={name}(A1:A2)','kind':'formula'} for c,name in enumerate(('AVERAGE','MIN','MAX','COUNT','COUNTA'),2))]})
            with closing(Workbook(path)) as book:
                snapshot=book.read('Sheet1',{'r2':2,'c2':6})
                self.assertEqual([c['value'] for c in snapshot['cells'] if c['row']==1],[2,3,2,4,2,2])
                for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
                    reviewed=preview(export(snapshot,{}));self.assertTrue(reviewed['can_import'],reviewed)
                    self.assertEqual(len([c for c in reviewed['cells'] if c['kind']=='formula']),5)

    def test_sdk_hook_validation_uses_new_functions_transactionally(self):
        from datahorizon.client import APIError,LocalService
        handler=Path(__file__).parent/'fixtures'/'validation_hook.py'
        with tempfile.TemporaryDirectory() as temp,LocalService(handler=handler) as client,client.open(Path(temp)/'managed.dhwb',create=True) as book:
            self.assertTrue({'AVERAGE','MIN','MAX','COUNT','COUNTA'}<=set(client.request('/v1/capabilities')['formulas']))
            book.set(1,1,'=AVERAGE(2,4)')
            self.assertEqual([c['value'] for c in book.read(r2=1,c2=2)['cells']],[3,6])
            with self.assertRaises(APIError): book.set(1,1,'=MIN(-1,4)')
            self.assertEqual([c['value'] for c in book.read(r2=1,c2=2)['cells']],[3,6])
