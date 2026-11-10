# SPDX-License-Identifier: GPL-3.0-only
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
from datahorizon.client import LocalService,APIError,UnknownOutcome
from datahorizon.service import openapi


SOURCE="def add(ctx, amount):\n    old=ctx.value('Sheet1',1,1) or 0\n    ctx.set('Sheet1',1,1,str(old+amount),'number')\n    print('added',amount)\n    return old+amount\n"
MACROS=[dict(name='add',parameters=[dict(name='amount',type='number',default=2)],result=dict(type='number'))]


class AutomationClientTests(unittest.TestCase):
    def create(self,path):
        with LocalService() as client,client.open(path,create=True) as book:
            book.save_module('Example',SOURCE,MACROS,fingerprint=book.code()['fingerprint'])
            return book.code()

    def test_sdk_cli_and_native_reopen_use_exact_operator_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'macro.dhwb';code=self.create(path)
            cli=[sys.executable,'-m','datahorizon.cli']
            denied=subprocess.run([*cli,str(path),'macro','Example','add','--fingerprint',code['fingerprint']],input='{}',capture_output=True,text=True)
            self.assertEqual(denied.returncode,1);self.assertEqual(json.loads(denied.stderr)['code'],'CODE_NOT_APPROVED')
            run=subprocess.run([*cli,'--approve-code',code['activation_key'],str(path),'macro','Example','add','--fingerprint',code['fingerprint']],input='{"amount":3}',capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(run.stdout)['macro_result'],3)
            with LocalService(approved_code=[code['activation_key']]) as client,client.open(path) as book:
                self.assertEqual(book.run_macro('Example','add',fingerprint=code['fingerprint'])['macro_result'],5)
                self.assertTrue(book.code()['approved']);book.apply('undo');self.assertEqual(book.read()['cells'][0]['value'],3)
            read=subprocess.run([*cli,str(path),'code','read','Example'],capture_output=True,text=True,check=True)
            self.assertFalse(json.loads(read.stdout)['approved'])

    def test_lost_response_reconciles_durable_result_without_second_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'macro.dhwb';code=self.create(path)
            with LocalService(approved_code=[code['activation_key']]) as client,client.open(path) as book:
                original=client.request;operation=str(uuid.uuid4())
                def lost(path,body=None):
                    result=original(path,body)
                    if path.endswith('/operations'):raise OSError('lost acknowledgement')
                    return result
                with patch.object(client,'request',lost):
                    with self.assertRaises(UnknownOutcome):book.run_macro('Example','add',fingerprint=code['fingerprint'],operation_id=operation)
                with self.assertRaises(UnknownOutcome):book.run_macro('Example','add',fingerprint=code['fingerprint'])
                result=book.outcome(operation);self.assertEqual(result['macro_result'],2)
                self.assertEqual(book.read()['cells'][0]['value'],2)

    def test_real_rest_cancellation_during_running_macro_has_durable_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'macro.dhwb'
            with LocalService() as client,client.open(path,create=True) as book:
                book.save_module('Slow',"def run(ctx):\n    ctx.set('Sheet1',1,1,'staged')\n    while True:pass\n",[dict(name='run',result=dict(type='integer'))],fingerprint=book.code()['fingerprint']);code=book.code()
            with LocalService(approved_code=[code['activation_key']]) as client,client.open(path) as book:
                operation=str(uuid.uuid4());errors=[]
                def run():
                    try:book.run_macro('Slow','run',fingerprint=code['fingerprint'],operation_id=operation)
                    except APIError as error:errors.append(error.body)
                thread=threading.Thread(target=run);thread.start()
                try:
                    deadline=time.monotonic()+1.5
                    while time.monotonic()<deadline:
                        cancelled=client.request(book.base+'/operations/'+operation+'/cancel',{})
                        if cancelled['cancel_requested']:break
                        time.sleep(.01)
                    self.assertTrue(cancelled['cancel_requested']);thread.join(timeout=3);self.assertFalse(thread.is_alive())
                    self.assertEqual(errors[0]['code'],'CANCELLED');self.assertEqual(book.outcome(operation)['outcome'],'rolled_back');self.assertEqual(book.read()['cells'],[])
                finally:thread.join(timeout=5)

    def test_openapi_examples_execute_with_reviewed_state_and_digest(self):
        document=openapi();schemas=document['components']['schemas']
        self.assertIn('/v1/workbooks/{workbook}/code',document['paths'])
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'examples.dhwb'
            with LocalService() as client,client.open(path,create=True) as book:
                request=copy.deepcopy(schemas['ModuleSaveOperation']['example']);request.update(operation_id=str(uuid.uuid4()),expected_state=book.state,fingerprint=book.code()['fingerprint']);client.request(book.base+'/operations',request);book.refresh();code=book.code()
            with LocalService(approved_code=[code['activation_key']]) as client,client.open(path) as book:
                request=copy.deepcopy(schemas['MacroRunOperation']['example']);request.update(operation_id=str(uuid.uuid4()),expected_state=book.state,fingerprint=code['fingerprint']);result=client.request(book.base+'/operations',request);self.assertEqual(result['macro_result'],6)
                book.refresh();request=copy.deepcopy(schemas['ModuleDeleteOperation']['example']);request.update(operation_id=str(uuid.uuid4()),expected_state=book.state,fingerprint=code['fingerprint']);client.request(book.base+'/operations',request);book.refresh();self.assertEqual(book.code()['modules'],[])

    def test_remote_client_cannot_claim_local_operator_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'macro.dhwb';code=self.create(path)
            run=subprocess.run([sys.executable,'-m','datahorizon.cli','--url','http://127.0.0.1:1','--approve-code',code['activation_key'],str(path),'code','read'],capture_output=True,text=True)
            self.assertEqual(run.returncode,1);self.assertIn('owned managed service',json.loads(run.stderr)['message'])


if __name__=='__main__':unittest.main()
