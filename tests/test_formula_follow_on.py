"""Typed follow-on formula stages, bounded failures and package recalculation."""
from contextlib import closing
from pathlib import Path
import tempfile
import itertools
import re
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

    def test_substitute_literals_occurrences_and_preflight_limits(self):
        for formula,expected in [('=SUBSTITUTE("aaaa","aa","x")','xx'),('=SUBSTITUTE("aAa","a","x",2)','aAx'),('=SUBSTITUTE("00123","0","")','123'),('=SUBSTITUTE("abc","","x")','abc'),('=SUBSTITUTE("abc","b","x",99)','abc'),('=SUBSTITUTE("😀-😀","😀","X",2.9)','😀-X')]:self.assertEqual(calculate(formula),expected)
        self.errors([('=SUBSTITUTE("a","a","x",0)','#VALUE!'),('=SUBSTITUTE("a","a","x","1")','#VALUE!'),('=SUBSTITUTE(B1:B2,"a","x")','#VALUE!'),('=SUBSTITUTE(A1,"a","x")','#CYCLE!'),('=SUBSTITUTE("a","b")','#NAME?')])
        self.errors([('=IFERROR(SUBSTITUTE(B1,"x",C1),"hidden")','#LIMIT!')],{(1,2):cell('x'*3000),(1,3):cell('😀'*1000)})
        self.errors([('=SUBSTITUTE(B1,"missing","")','#LIMIT!')],{(1,2):cell('x'*40000)})

    def test_replace_codepoints_insert_append_counts_and_limits(self):
        for formula,expected in [('=REPLACE("00123",3,2,"AB")','00AB3'),('=REPLACE("abc",2,0,"X")','aXbc'),('=REPLACE("abc",99,999,"X")','abcX'),('=REPLACE("😀áz",2,2,"X")','😀Xz'),('=REPLACE("abc",2.9,1.9,TRUE)','aTRUEc'),('=REPLACE("abc",1,1e300,"")','')]:self.assertEqual(calculate(formula),expected)
        self.errors([('=REPLACE("a",0,1,"x")','#VALUE!'),('=REPLACE("a",1,-0.1,"x")','#VALUE!'),('=REPLACE("a","1",1,"x")','#VALUE!'),('=REPLACE(B1:B2,1,1,"x")','#VALUE!'),('=REPLACE(A1,1,1,"x")','#CYCLE!'),('=REPLACE("a",1,1)','#NAME?')])
        self.errors([('=IFERROR(REPLACE(B1,1,0,C1),"hidden")','#LIMIT!')],{(1,2):cell('x'*20000),(1,3):cell('x'*20000)})
        self.assertEqual(calculate('=REPLACE(B1,1,40000,"ok")',{(1,2):cell('x'*40000)}),'ok')

    def test_find_literal_case_unicode_empty_and_start_errors(self):
        for formula,expected in [('=FIND("a","Data")',2),('=FIND("a","Data",3)',4),('=FIND("*","a*b")',2),('=FIND("a","😀á")',2),('=FIND("","abc",4)',4),('=FIND("",B1)',1),('=FIND(23,"00123")',4),('=FIND("b","abc",2.9)',2)]:self.assertEqual(calculate(formula),expected)
        self.errors([('=FIND("A","Data")','#VALUE!'),('=FIND("x","abc",0)','#VALUE!'),('=FIND("","abc",5)','#VALUE!'),('=FIND("a","abc","1")','#VALUE!'),('=FIND(B1:B2,"x")','#VALUE!'),('=FIND(A1,"x")','#CYCLE!'),('=FIND("a")','#NAME?'),('=FIND("é","é")','#VALUE!')])
        self.assertEqual(calculate('=FIND("z",B1)',{(1,2):cell('x'*50000+'z')}),50001)

    def test_search_wildcards_unicode_mapping_and_budgets(self):
        for formula,expected in [('=SEARCH("A","Data")',2),('=SEARCH("H*","Data Horizon")',6),('=SEARCH("~*","a*b")',2),('=SEARCH("~?","a?b")',2),('=SEARCH("~","a~b")',2),('=SEARCH("sa","ßa")',1),('=SEARCH("a","😀A")',2),('=SEARCH("s","xß",2)',2),('=SEARCH("??","ß")',1),('=SEARCH("","abc",4)',4)]:self.assertEqual(calculate(formula),expected)
        self.errors([('=SEARCH("z","abc")','#VALUE!'),('=SEARCH("a","abc",0)','#VALUE!'),('=SEARCH(B1:B2,"x")','#VALUE!'),('=SEARCH(A1,"x")','#CYCLE!'),('=SEARCH("a")','#NAME?'),('=SEARCH("a","abc","1")','#VALUE!'),('=SEARCH("'+'a'*256+'","x")','#LIMIT!')])
        self.errors([('=IFERROR(SEARCH("*'+'a'*200+'b",B1),"hidden")','#LIMIT!')],{(1,2):cell('a'*10000)})
        self.assertEqual(calculate('=SEARCH("Z",B1)',{(1,2):cell('x'*50000+'z')}),50001)

    def test_search_small_wildcard_language_against_independent_regex(self):
        for length in range(5):
            for chars in itertools.product('ab',repeat=length):
                text=''.join(chars)
                for pattern in ('','*','?','*a','a*','a?','a*b','*b*a','**b','?a?'):
                    regex=''.join('.*' if ch=='*' else '.' if ch=='?' else re.escape(ch) for ch in pattern)
                    match=re.search(regex,text)
                    formula='=SEARCH("'+pattern+'","'+text+'")'
                    if match:self.assertEqual(calculate(formula),match.start()+1,(pattern,text))
                    else:self.errors([(formula,'#VALUE!')])

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
