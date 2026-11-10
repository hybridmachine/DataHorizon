# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook,EngineError
from datahorizon.workbook_code import activation,fingerprint


class MacroTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.book=Workbook(Path(self.temp.name)/'macros.dhwb',create=True);self.addCleanup(self.book.close)
        self.apply('table_create',name='Data',columns=[dict(name='Value',type='integer',required=True,unique=True)])
        self.apply('table_insert',table='Data',rows=[dict(Value=1),dict(Value=2)])

    def apply(self,action,**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))
    def install(self,source,*,parameters=None,result=None):
        self.apply('module_save',name='Example',source=source,macros=[dict(name='run',parameters=parameters or [],result=result or dict(type='integer'))],fingerprint=fingerprint(self.book.db))
        self.book.approved_code=frozenset([activation(self.book)['activation_key']])
    def request(self,**args):return dict(action='macro_run',operation_id=str(uuid.uuid4()),expected_state=self.book.state(),module='Example',macro='run',fingerprint=fingerprint(self.book.db),**args)
    def run_macro(self,**args):return self.book.mutate(self.request(**args))
    def cell(self,row=1,col=1):return self.book.raw(self.book.sheet('Sheet1'),row,col)

    def test_mixed_data_context_reads_required_hooks_and_one_history_entry(self):
        self.install("def run(ctx, amount):\n    ctx.set('Sheet1',1,1,str(amount),'number')\n    assert ctx.value('Sheet1',1,1)==amount\n    ctx.table_set('Data','1',{'Value':amount})\n    print('applied')\n    return amount*2\n",parameters=[dict(name='amount',type='integer',default=3)])
        handler=Path(self.temp.name)/'rules.py';handler.write_text("def on_change(ctx,changes):\n    for change in changes:\n        if change['row']==1 and change['col']==1 and change['after'] is not None:ctx.set('Sheet1',1,2,'hook')\ndef on_table_change(ctx,changes):\n    pass\n")
        self.book.handler=handler;result=self.run_macro();self.assertEqual(result['macro_result'],6);self.assertIn('applied',result['macro']['logs']);self.assertEqual(result['changed_cells'],2);self.assertEqual(result['changed_records'],1)
        self.assertEqual(len(self.book.undo),1);self.apply('undo');self.assertIsNone(self.cell());self.assertIsNone(self.cell(col=2));self.assertEqual(self.book.db.execute('SELECT Value FROM Data WHERE _dh_row_id=1').fetchone()[0],1)
        self.apply('redo');self.assertEqual(self.cell()['source'],'3');self.assertEqual(self.cell(col=2)['source'],'hook')

    def test_exception_after_writes_rolls_back_all_changes_and_retains_diagnostics(self):
        self.install("def run(ctx):\n    ctx.set('Sheet1',1,1,'staged')\n    ctx.table_set('Data','1',{'Value':3})\n    print('before exception')\n    raise ValueError('intentional')\n")
        request=self.request()
        with self.assertRaises(EngineError) as error:self.book.mutate(request)
        self.assertEqual(error.exception.body['code'],'MACRO_FAILED');self.assertIn('before exception',error.exception.body['macro']['logs']);self.assertIn('ValueError: intentional',str(error.exception))
        self.assertIsNone(self.cell());self.assertEqual(self.book.db.execute('SELECT Value FROM Data WHERE _dh_row_id=1').fetchone()[0],1);self.assertNotIn('macro_result',error.exception.body)
        self.assertEqual(self.book.operation(request['operation_id']),error.exception.body)

    def test_constraints_and_caught_broker_errors_cannot_commit_partial_data(self):
        self.install("def run(ctx):\n    ctx.set('Sheet1',1,1,'staged')\n    try:ctx.table_set('Data','1',{'Value':2})\n    except Exception:pass\n    return 1\n")
        with self.assertRaises(EngineError) as error:self.run_macro()
        self.assertEqual(error.exception.body['code'],'MACRO_CONTEXT');self.assertIsNone(self.cell())

    def test_argument_and_result_types_reject_without_a_successful_result(self):
        self.install("def run(ctx, amount):\n    ctx.set('Sheet1',1,1,'staged')\n    return 'wrong type'\n",parameters=[dict(name='amount',type='integer')])
        for arguments in ({},{'amount':True},{'amount':'3'},{'amount':3,'extra':1}):
            with self.assertRaises(EngineError) as error:self.run_macro(arguments=arguments)
            self.assertEqual(error.exception.body['code'],'MACRO_ARGUMENT');self.assertIsNone(self.cell())
        with self.assertRaises(EngineError) as error:self.run_macro(arguments=dict(amount=3))
        self.assertEqual(error.exception.body['code'],'MACRO_RESULT');self.assertIsNone(self.cell())

    def test_repeated_operation_returns_durable_result_without_executing_again(self):
        self.install("def run(ctx):\n    previous=ctx.value('Sheet1',1,1) or 0\n    ctx.set('Sheet1',1,1,str(previous+1),'number')\n    return 7\n")
        request=self.request();result=self.book.mutate(request);self.assertEqual(self.book.mutate(request),result);self.assertEqual(self.cell()['source'],'1')
        with self.assertRaises(EngineError) as error:self.book.mutate({**request,'arguments':{'new':1}})
        self.assertEqual(error.exception.body['code'],'OPERATION_ID_REUSED')
        with closing(Workbook(self.book.path)) as reopened:self.assertEqual(reopened.mutate(request),result)

    def test_missing_approval_readonly_and_repair_handles_refuse(self):
        self.install('def run(ctx):\n    return 1\n');self.book.approved_code=frozenset()
        with self.assertRaises(EngineError) as error:self.run_macro()
        self.assertEqual(error.exception.body['code'],'CODE_NOT_APPROVED')
        for options in (dict(readonly=True),dict(repair=True)):
            with closing(Workbook(self.book.path,**options)) as other:
                with self.assertRaises(EngineError):other.mutate(self.request())

    def test_required_data_hook_failure_rolls_back_macro_result_and_writes(self):
        self.install("def run(ctx):\n    ctx.set('Sheet1',1,1,'staged')\n    return 1\n")
        handler=Path(self.temp.name)/'reject.py';handler.write_text("def on_change(ctx,changes):raise ValueError('required rejection')\n");self.book.handler=handler
        with self.assertRaises(EngineError) as error:self.run_macro()
        self.assertEqual(error.exception.body['code'],'HOOK_FAILED');self.assertIsNone(self.cell());self.assertNotIn('macro_result',error.exception.body)


if __name__=='__main__':unittest.main()
