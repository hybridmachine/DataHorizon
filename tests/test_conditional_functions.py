# SPDX-License-Identifier: GPL-3.0-only
"""Bounded COUNTIF/SUMIF criteria, aligned snapshots and transactional hooks."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from datahorizon.formula import FormulaError,Parser
from datahorizon.engine import Workbook
from datahorizon.client import LocalService,APIError
from datahorizon.interchange import xlsx_export,xlsx_preview
from datahorizon.ods import ods_export,ods_preview
from test_text_functions import calculate


def cell(value):
    kind='null' if value is None else 'boolean' if type(value) is bool else 'number' if type(value) in (int,float) else 'text'
    return dict(source='NULL' if value is None else str(value),kind=kind,value=value)

class ConditionalTests(unittest.TestCase):
    def test_numeric_text_boolean_blank_and_wildcard_criteria(self):
        values={(r,2):cell(value) for r,value in enumerate((2,'02',3,True,'Pen','pencil','*','',None,'a?b'),1)}
        for criterion,expected in [('2',2),('"2"',2),('">2"',1),('TRUE',1),('"p*"',2),('"PEN"',1),('"~*"',1),('"a~?b"',1),('""',2),('C1',1),('"<>"',8)]:
            with self.subTest(criterion=criterion):self.assertEqual(calculate('=COUNTIF(B1:B10,'+criterion+')',values),expected)
        self.assertEqual(values[2,2]['source'],'02')
        self.assertEqual(calculate('=COUNTIF(B1,"??")',{(1,2):cell('ß')}),1)  # Folded matching is explicit.

    def test_sum_alignment_selected_errors_and_unselected_errors(self):
        values={(1,2):cell('Pen'),(2,2):cell('Book'),(3,2):cell('pen'),(1,3):cell(2),(2,3):dict(source='=1/0',kind='formula',value=None),(3,3):cell(3)}
        self.assertEqual(calculate('=SUMIF(B1:B3,"Pen",C1:C3)',values),5)
        with self.assertRaises(FormulaError) as error:calculate('=SUMIF(B1:B3,"Book",C1:C3)',values)
        self.assertEqual(error.exception.code,'#DIV/0!')
        values[2,2]=dict(source='=1/0',kind='formula',value=None)
        self.assertEqual(calculate('=COUNTIF(B1:B3,"<>missing")',values),2)
        values={(r,2):cell(v) for r,v in enumerate((1,2,'3',True,None),1)}
        self.assertEqual(calculate('=SUMIF(B1:B5,">0")',values),3)
        with self.assertRaises(FormulaError) as error:calculate('=SUMIF(B1:B2,1,C1:D1)',values)
        self.assertEqual(error.exception.code,'#VALUE!')

    def test_invalid_criteria_cycles_overflow_and_bounded_wildcards(self):
        for formula,code in [('=COUNTIF(#REF!,1)','#REF!'),('=SUMIF(B1,1,#REF!)','#REF!'),('=COUNTIF(1,2)','#VALUE!'),('=SUMIF(B1)','#NAME?'),('=COUNTIF(B1,B1:B2)','#VALUE!'),('=COUNTIF(B1,">a*")','#VALUE!'),('=COUNTIF(A1,1)','#CYCLE!'),('=COUNTIF(B1,"1e999")','#NUM!'),('=COUNTIF(B1,"'+'x'*256+'")','#LIMIT!')]:
            with self.subTest(formula=formula),self.assertRaises(FormulaError) as error:calculate(formula)
            self.assertEqual(error.exception.code,code)
        values={(1,2):cell('a'*60000)}
        with self.assertRaises(FormulaError) as error:calculate('=IFERROR(COUNTIF(B1,"*'+'a'*250+'b"),"hidden")',values)
        self.assertEqual(error.exception.code,'#LIMIT!')
        with self.assertRaises(FormulaError) as error:calculate('=SUMIF(B1:B2,">0")',{(1,2):cell(1e308),(2,2):cell(1e308)})
        self.assertEqual(error.exception.code,'#NUM!')

    def test_native_snapshot_and_formula_values_interchange(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'conditional.dhwb',create=True)) as book:
            edits=[{'row':r,'col':2,'source':label,'kind':'text'} for r,label in enumerate(('Pen','Book','Pen'),1)]+[{'row':r,'col':3,'source':str(v)} for r,v in enumerate((2,5,3),1)]+[{'row':1,'col':1,'source':'=SUMIF(B1:B3,"Pen",C1:C3)'},{'row':2,'col':1,'source':'=COUNTIF(C1:C3,">2")'}]
            book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':edits});snapshot=book.read('Sheet1',{'r2':3,'c2':3});self.assertEqual(snapshot['cells'][0]['value'],5)
            for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
                for mode in ('formulas','values'):
                    result=preview({'file_base64':export(snapshot,{'mode':mode})['file_base64'],'mode':mode});self.assertTrue(result['can_import'],result['issues']);first=next(c for c in result['cells'] if c['row']==1 and c['col']==1)
                    if mode=='formulas':self.assertEqual(Parser(first['source'][1:]).parse(),Parser(edits[-2]['source'][1:]).parse())
                    else:self.assertEqual(float(first['source']),5)

    def test_required_hook_uses_same_functions_and_rejects_entire_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            handler=Path(temp)/'handler.py';handler.write_text('def on_change(context, changes):\n for change in changes:\n  if (change["row"],change["col"])==(1,1):\n   value=context.value(change["sheet"],1,1)\n   if value is not None:\n    if value>10: raise ValueError("test total too large")\n    context.set(change["sheet"],1,2,str(value))\n')
            with LocalService(handler=handler) as client,client.open(Path(temp)/'hooks.dhwb',create=True) as book:
                book.apply('edit',cells=[{'row':r,'col':3,'source':'5'} for r in (1,2,3)]+[{'row':1,'col':1,'source':'=COUNTIF(C1:C3,">0")'}])
                before=book.read(r2=3,c2=3)['cells'];self.assertEqual(before[1]['value'],3)
                with self.assertRaises(APIError):book.apply('edit',cells=[{'row':1,'col':1,'source':'=SUMIF(C1:C3,">0")'}])
                self.assertEqual(book.read(r2=3,c2=3)['cells'],before)
