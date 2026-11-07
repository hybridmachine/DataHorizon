"""Declared formula tiers: shared engine, bounds, error handling and interchange."""
import base64
from contextlib import closing
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from datahorizon.engine import Workbook,EngineError
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


class LogicalTests(unittest.TestCase):
    def test_boolean_numeric_text_reference_and_blank_contracts(self):
        cells={('Sheet1',1,1):literal('TRUE','text'),('Sheet1',2,1):literal(True,'boolean'),('Sheet1',3,1):literal(0)}
        for source,expected in [('AND(TRUE,2,-1)',True),('OR(FALSE,0)',False),('NOT(FALSE)',True),('NOT(A4)',True),('AND("true",1)',True),('OR(A1:A4)',True),('AND(A1:A4)',False)]:
            self.assertEqual(evaluate(source,cells),expected)
        for source in ('AND(A1)','OR(A4)','AND("bad")','NOT(A1:A2)','NOT("1")'):
            with self.assertRaises(FormulaError) as failure: evaluate(source,cells)
            self.assertEqual(failure.exception.code,'#VALUE!')

    def test_eager_errors_arity_and_resource_guards(self):
        for source in ('AND(FALSE,1/0)','OR(TRUE,1/0)','NOT(1/0)'):
            with self.assertRaises(FormulaError) as failure: evaluate(source)
            self.assertEqual(failure.exception.code,'#DIV/0!')
        for source in ('AND()','OR()','NOT()','NOT(1,2)'):
            with self.assertRaises(FormulaError) as failure: evaluate(source)
            self.assertEqual(failure.exception.code,'#NAME?')
        for source in ('AND(FALSE,A1:A1048576)','OR(TRUE,A1:A1048576)'):
            with self.assertRaises(FormulaError) as failure: evaluate(source)
            self.assertEqual(failure.exception.code,'#LIMIT!')
        self.assertEqual(evaluate('IF(FALSE,AND(TRUE,1/0),7)'),7)

    def test_both_interchange_formats_preserve_boolean_function_sources(self):
        cells=[{'row':1,'col':i,'kind':'formula','source':source,'value':value,'calculation':'current','style':{}} for i,(source,value) in enumerate([('=AND(TRUE,1)',True),('=OR(FALSE,0)',False),('=NOT(TRUE)',False)],1)]
        snapshot={'cells':cells,'range':[1,1,1,3],'state':'fixture'}
        for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
            parsed=preview(export(snapshot,{}));self.assertTrue(parsed['can_import'],parsed)
            self.assertEqual([evaluate(c['source']) for c in parsed['cells']],[True,False,False])

    def test_logical_conditions_participate_in_required_transactional_validation(self):
        from datahorizon.client import APIError,LocalService
        with tempfile.TemporaryDirectory() as temp,LocalService(handler=Path(__file__).parent/'fixtures'/'validation_hook.py') as client,client.open(Path(temp)/'logical.dhwb',create=True) as book:
            book.set(1,1,'=IF(AND(TRUE,2>1),7,-1)')
            self.assertEqual([c['value'] for c in book.read(r2=1,c2=2)['cells']],[7,14])
            with self.assertRaises(APIError): book.set(1,1,'=IF(OR(FALSE,0),1,-1)')
            self.assertEqual([c['value'] for c in book.read(r2=1,c2=2)['cells']],[7,14])


class IfErrorTests(unittest.TestCase):
    def test_lazy_fallback_ordinary_errors_and_blank_conversion(self):
        for source,expected in [('IFERROR(7,1/0)',7),('IFERROR(1/0,42)',42),('IFERROR(#REF!,"missing")','missing'),('IFERROR(UnknownFunction(),"unknown")','unknown'),('IFERROR(1e9999,9)',9),('IFERROR(A1,9)',''),('IFERROR(1/0,A1)','')]:
            self.assertEqual(evaluate(source),expected)
        with self.assertRaises(FormulaError) as failure: evaluate('IFERROR(1/0,2/0)')
        self.assertEqual(failure.exception.code,'#DIV/0!')
        self.assertEqual(evaluate('COUNT(IFERROR(1/0,""))'),0)
        self.assertEqual(evaluate('COUNTA(IFERROR(1/0,""))'),1)

    def test_arity_arrays_cycles_and_limits_remain_visible(self):
        for source,code in [('IFERROR()','#NAME?'),('IFERROR(1)','#NAME?'),('IFERROR(1,2,3)','#NAME?'),('IFERROR(A1:A2,0)','#VALUE!'),('IFERROR(1/0,A1:A2)','#VALUE!'),('IFERROR(SUM(A1:A1048576),0)','#LIMIT!')]:
            with self.assertRaises(FormulaError) as failure: evaluate(source)
            self.assertEqual(failure.exception.code,code)
        cells={('Sheet1',1,1):{'source':'=IFERROR(A1,0)','kind':'formula','style':{}}}
        with self.assertRaises(FormulaError) as failure: evaluate('IFERROR(A1,0)',cells)
        self.assertEqual(failure.exception.code,'#CYCLE!')
        with self.assertRaises(FormulaError) as failure: evaluate('IFERROR(1,0)',seconds=-1)
        self.assertEqual(failure.exception.code,'#LIMIT!')
        self.assertEqual(evaluate('IFERROR(7,A1:A2)'),7)

    def test_fallback_uses_same_required_hook_transaction_and_file_tier(self):
        from datahorizon.client import APIError,LocalService
        with tempfile.TemporaryDirectory() as temp,LocalService(handler=Path(__file__).parent/'fixtures'/'validation_hook.py') as client,client.open(Path(temp)/'iferror.dhwb',create=True) as book:
            book.set(1,1,'=IFERROR(1/0,7)')
            self.assertEqual([c['value'] for c in book.read(r2=1,c2=2)['cells']],[7,14])
            with self.assertRaises(APIError): book.set(1,1,'=IFERROR(1/0,-1)')
            self.assertEqual([c['value'] for c in book.read(r2=1,c2=2)['cells']],[7,14])
        snapshot={'state':'s','range':[1,1,1,1],'cells':[{'row':1,'col':1,'source':'=IFERROR(1/0,7)','kind':'formula','value':7,'calculation':'current','style':{}}]}
        for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
            reviewed=preview(export(snapshot,{}));self.assertTrue(reviewed['can_import'],reviewed)
            self.assertEqual(evaluate(reviewed['cells'][0]['source']),7)

    def test_storage_failures_are_never_formula_fallbacks(self):
        def unavailable(*args): raise EngineError("STORAGE_ERROR","injected read failure")
        calc=Calculator(unavailable,lambda name:name)
        with self.assertRaises(EngineError): calc.evaluate(Parser('IFERROR(A1,0)').parse(),'Sheet1')
