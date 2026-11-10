# SPDX-License-Identifier: GPL-3.0-only
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
from datahorizon.engine import Workbook,EngineError
from datahorizon.workbook_code import activation,fingerprint


class MacroLifecycleTests(unittest.TestCase):
    def execute(self,body,*,result_type='integer',cancel=False,preamble=''):
        with tempfile.TemporaryDirectory() as temp:
            book=Workbook(Path(temp)/'run.dhwb',create=True)
            try:
                def operation(action,**args):return dict(action=action,operation_id=str(uuid.uuid4()),expected_state=book.state(),**args)
                source=preamble+"\ndef run(ctx):\n    ctx.set('Sheet1',1,1,'staged')\n"+''.join('    '+line+'\n' for line in body.splitlines())
                book.mutate(operation('module_save',name='Example',source=source,macros=[dict(name='run',result=dict(type=result_type))],fingerprint=fingerprint(book.db)))
                book.approved_code=frozenset([activation(book)['activation_key']]);request=operation('macro_run',module='Example',macro='run',fingerprint=fingerprint(book.db))
                event=threading.Event();timer=None;dispatch=book.hook_dispatch
                def after_staging(method,args):
                    nonlocal timer
                    result=dispatch(method,args)
                    if cancel and method=='set' and timer is None:
                        timer=threading.Timer(.05,event.set);timer.start()
                    return result
                book.hook_dispatch=after_staging
                start=time.monotonic()
                try:
                    try:result=book.mutate(request,cancel_event=event)
                    except EngineError as error:result=error.body
                    elapsed=time.monotonic()-start
                finally:
                    if timer:timer.cancel();timer.join()
                committed=result['outcome']=='committed'
                self.assertEqual(book.raw(book.sheet('Sheet1'),1,1) is not None,committed)
                self.assertEqual(book.operation(request['operation_id']),result)
                if not committed:self.assertNotIn('macro_result',result)
                self.assertFalse(multiprocessing.active_children())
                return result,elapsed
            finally:book.close()

    def test_success_and_exception_logs_are_bounded_utf8_and_durable(self):
        for end in ('return 1',"raise ValueError('failure')"):
            result,_=self.execute("print('🙂'*100000)\n"+end)
            self.assertLessEqual(len(result['macro']['logs'].encode()),8192)
            self.assertIn('🙂',result['macro']['logs'])
            if result['outcome']!='committed':self.assertLessEqual(len(result['macro']['traceback'].encode()),8192)

    def test_timeout_and_cancellation_terminate_busy_workers_and_rollback(self):
        timed,elapsed=self.execute("print('before timeout')\nwhile True:pass")
        self.assertEqual(timed['code'],'MACRO_TIMEOUT');self.assertLess(elapsed,3);self.assertIn('before timeout',timed['macro']['logs'])
        cancelled,elapsed=self.execute("print('before cancel')\nwhile True:pass",cancel=True)
        self.assertEqual(cancelled['code'],'CANCELLED');self.assertLess(elapsed,1.5);self.assertIn('before cancel',cancelled['macro']['logs'])

    def test_worker_crash_and_forbidden_broker_requests_fail_closed(self):
        crashed,_=self.execute("import os\nprint('before crash')\nos._exit(9)")
        self.assertEqual(crashed['code'],'MACRO_WORKER_FAILED');self.assertIn('before crash',crashed['macro']['logs'])
        forbidden,_=self.execute("ctx.request('macro_run',macro='run')\nreturn 1")
        self.assertEqual(forbidden['code'],'MACRO_PROTOCOL')

    def test_invalid_and_oversized_results_cannot_commit(self):
        for expression in ("b'bytes'","float('inf')","'x'*2000000"):
            result,_=self.execute('return '+expression,result_type='text')
            self.assertEqual(result['code'],'MACRO_RESULT',result)

    def test_noisy_loop_uses_bounded_streaming_and_remains_cancellable(self):
        result,elapsed=self.execute("while True:print('noise'*10000)",cancel=True)
        self.assertEqual(result['code'],'CANCELLED');self.assertLessEqual(len(result['macro']['logs'].encode()),8192);self.assertLess(elapsed,1.5)

    def test_crash_during_import_is_visible_without_any_data_write(self):
        # Top-level import execution shares the same approved invocation budget.
        result,_=self.execute('return 1',preamble='import definitely_missing_datahorizon_fixture_package')
        self.assertEqual(result['code'],'MACRO_FAILED');self.assertIn('ModuleNotFoundError',result['macro']['traceback'])

    def test_process_start_failure_has_a_confirmed_rollback(self):
        with patch('datahorizon.macro_runtime.multiprocessing.get_context',side_effect=RuntimeError('cannot spawn')):
            result,_=self.execute('return 1')
        self.assertEqual(result['code'],'MACRO_WORKER_FAILED');self.assertEqual(result['outcome'],'rolled_back')


if __name__=='__main__':unittest.main()
