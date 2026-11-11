# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook,EngineError
from datahorizon.workbook_code import activation,fingerprint


def macro(body,name='run'):return "def %s(ctx):\n"%name+"".join("    "+line+"\n" for line in body.splitlines())


class ContextReferences(unittest.TestCase):
    count=0
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.book=Workbook(Path(self.temp.name)/'refs.dhwb',create=True);self.addCleanup(self.book.close)
        self.apply('sheet_add',name='Supplies')
        cells=[dict(row=1,col=1,source='Item'),dict(row=1,col=2,source='Quantity'),dict(row=1,col=3,source='Unit price'),dict(row=1,col=4,source='Amount')]
        for row,(item,quantity,price) in enumerate([('Notebook',2,'3.50'),('Pen',4,'1.25'),('Folder',3,'2.00')],start=2):
            cells+=[dict(row=row,col=1,source=item),dict(row=row,col=2,source=str(quantity)),dict(row=row,col=3,source=price),dict(row=row,col=4,source='=B%d*C%d'%(row,row))]
        cells.append(dict(row=6,col=4,source='=SUM(D2:D4)'))
        self.apply('edit',sheet='Supplies',cells=cells)
        self.apply('name_save',name='PenQuantity',reference="Supplies!B3");self.apply('name_save',name='Quantities',reference='Supplies!B2:B4');self.apply('name_save',name='Total',reference='Supplies!D6')

    def apply(self,action,**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))
    def install(self,source,result='number'):
        self.count+=1
        self.apply('module_save',name='Example%d'%self.count,source=source,macros=[dict(name='run',parameters=[],result=dict(type=result))],fingerprint=fingerprint(self.book.db))
        self.book.approved_code=frozenset([activation(self.book)['activation_key']])
    def run_macro(self,name='run'):return self.book.mutate(dict(action='macro_run',operation_id=str(uuid.uuid4()),expected_state=self.book.state(),module='Example%d'%self.count,macro=name,fingerprint=fingerprint(self.book.db)))
    def failure(self):
        with self.assertRaises(EngineError) as caught:self.run_macro()
        return caught.exception.body

    def test_address_and_name_forms_read_and_write(self):
        self.install(macro('q = ctx.value("Supplies!B3")\nctx.set("PenQuantity", q + 2)\nreturn ctx.value("Total")'))
        self.assertEqual(self.run_macro()['macro_result'],20.5)
        self.assertEqual(self.book.raw(self.book.sheet('Supplies'),3,2)['source'],'6')

    def test_legacy_three_and_four_argument_forms_still_work(self):
        self.install(macro('ctx.set("Supplies",3,2,"9")\nassert ctx.get("Supplies",3,2)["source"]=="9"\nreturn ctx.value("Supplies",3,2)'))
        self.assertEqual(self.run_macro()['macro_result'],9)

    def test_values_returns_rows(self):
        self.install(macro('import json\nreturn json.dumps([ctx.values("Quantities"), ctx.values("Supplies!A1:B2"), ctx.values("Supplies!E1:F1")])'),result='text')
        self.assertEqual(json.loads(self.run_macro()['macro_result']),[[[2],[4],[3]],[['Item','Quantity'],['Notebook',2]],[[None,None]]])

    def test_quoted_sheet_names_resolve(self):
        for sheet in ["Bob's Sheet",'Two Words','Übersicht']:
            self.apply('sheet_add',name=sheet);self.apply('edit',sheet=sheet,cells=[dict(row=3,col=2,source='42')])
            reference="'"+sheet.replace("'","''")+"'!B3"
            self.install(macro('return ctx.value(%r)'%reference))
            self.assertEqual(self.run_macro()['macro_result'],42)

    def test_shape_and_lookup_errors_abort_even_when_caught(self):
        for reference,code in [("Supplies!B2:B4",'REFERENCE_SHAPE'),("Quantities",'REFERENCE_SHAPE'),("Nowhere!A1",'REFERENCE_NOT_FOUND'),("NoSuchName",'REFERENCE_NOT_FOUND'),("Supplies!ZZZ",'REFERENCE_INVALID')]:
            self.install(macro('ctx.set("Supplies!A8","kept?")\ntry:\n    ctx.value(%r)\nexcept Exception:\n    pass\nreturn 1'%reference))
            outcome=self.failure()
            self.assertEqual(outcome['code'],'MACRO_CONTEXT');self.assertEqual(outcome['macro']['context_code'],code);self.assertEqual(outcome['outcome'],'rolled_back')
            self.assertEqual(self.book.operation(outcome['operation_id'])['macro']['context_code'],code)
            self.assertIsNone(self.book.raw(self.book.sheet('Supplies'),8,1))

    def test_values_over_4096_cells_rejects(self):
        self.install(macro('try:\n    ctx.values("Supplies!A1:A4097")\nexcept Exception:\n    pass\nreturn 1'))
        self.assertEqual(self.failure()['macro']['context_code'],'REFERENCE_LIMIT')
        self.install(macro('return len(ctx.values("Supplies!A1:A4096"))'))
        self.assertEqual(self.run_macro()['macro_result'],4096)

    def test_set_rejects_nan_inf_and_objects(self):
        for value in ('float("nan")','float("inf")','object()','[1]','None'):
            self.install(macro('try:\n    ctx.set("Supplies!A8",%s)\nexcept Exception:\n    pass\nreturn 1'%value))
            self.assertEqual(self.failure()['macro']['context_code'],'CONTEXT_TYPE',value)

    def test_set_accepts_numbers_and_booleans(self):
        self.install(macro('ctx.set("Supplies!A8",True)\nctx.set("Supplies!A9",1.5)\nctx.set("Supplies!A10",7)\nctx.set("Supplies!A11",False)\nreturn 1'))
        self.run_macro();sheet=self.book.sheet('Supplies')
        self.assertEqual([self.book.raw(sheet,row,1)['source'] for row in (8,9,10,11)],['TRUE','1.5','7','FALSE'])

    def test_wrong_argument_counts_raise_type_error_in_worker(self):
        self.install(macro('ctx.value("A","B")'));body=self.failure()
        self.assertEqual(body['code'],'MACRO_FAILED');self.assertIn('TypeError',body['error'] if 'error' in body else str(body))

    def test_broken_name_rejects(self):
        self.apply('sheet_delete',sheet='Supplies')
        self.install(macro('try:\n    ctx.value("Total")\nexcept Exception:\n    pass\nreturn 1'))
        self.assertEqual(self.failure()['macro']['context_code'],'NAME_BROKEN')

    def test_external_handler_gets_the_same_forms(self):
        self.apply('sheet_add',name='Seen')
        self.book.handler=Path(__file__).parent/'fixtures'/'reference_hook.py'
        self.apply('edit',sheet='Sheet1',cells=[dict(row=1,col=1,source='go')])
        self.assertEqual(float(self.book.raw(self.book.sheet('Seen'),1,1)['source']),18)


if __name__=='__main__':unittest.main()
