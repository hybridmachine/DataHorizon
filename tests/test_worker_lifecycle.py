# SPDX-License-Identifier: GPL-3.0-only
"""Worker failure cleanup and broker framing without bypassing required hooks."""
from contextlib import closing, contextmanager
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from datahorizon import hooks
from datahorizon.engine import EngineError, Workbook

FIXTURES=Path(__file__).resolve().parent/'fixtures'


def protocol_worker(pipe, mode, changes, operation):
    """A real spawned peer that deliberately violates or stalls the protocol."""
    try:
        if mode=='oversized': pipe.send_bytes(b'x'*(hooks.MAX_BROKER_BYTES+1))
        elif mode=='malformed': pipe.send_bytes(b'not JSON')
        elif mode=='unknown': hooks._send(pipe,{'method':'invented'})
        elif mode=='bad_done': hooks._send(pipe,{'method':'done','extra':True})
        elif mode=='blocked_response':
            hooks._send(pipe,{'method':'get','sheet':'Sheet1','row':1,'col':1})
            time.sleep(30)  # Parent's response must not stall its transaction.
        elif mode=='hang': time.sleep(30)
        # eof intentionally exits without a result.
    finally:
        pipe.close()


@contextmanager
def tracked_context():
    context=multiprocessing.get_context('spawn')
    make_process,make_pipe=context.Process,context.Pipe
    processes,pipes=[],[]
    def process(*args,**kwargs):
        result=make_process(*args,**kwargs); processes.append(result); return result
    def pipe(*args,**kwargs):
        result=make_pipe(*args,**kwargs); pipes.extend(result); return result
    with patch.object(context,'Process',side_effect=process),patch.object(context,'Pipe',side_effect=pipe):
        yield processes,pipes


class WorkerLifecycleTests(unittest.TestCase):
    def assert_released(self, processes, pipes, previous):
        self.assertTrue(all(pipe.closed for pipe in pipes))
        for process in processes:
            with self.assertRaises(ValueError): process.is_alive()  # Process.close contract.
        self.assertEqual({p.pid for p in multiprocessing.active_children()},previous)
        self.assertFalse(any(t.name=='DataHorizonHookBroker' for t in threading.enumerate()))

    def test_start_and_construction_failure_close_resources(self):
        context=multiprocessing.get_context('spawn')
        for stage in ('construct','start'):
            with self.subTest(stage=stage):
                parent,child=context.Pipe(); process=Mock(pid=None)
                if stage=='start': process.start.side_effect=OSError('Cannot spawn')
                fake=Mock()
                fake.Pipe.return_value=(parent,child)
                fake.Process.side_effect=OSError('Cannot construct') if stage=='construct' else None
                fake.Process.return_value=process
                with patch.object(hooks.multiprocessing,'get_context',return_value=fake):
                    with self.assertRaises(hooks.HookFailure):
                        hooks.invoke('unused',[],'start-failed',Mock(),time.monotonic()+5)
                self.assertTrue(parent.closed); self.assertTrue(child.closed)
                if stage=='start': process.close.assert_called_once()

    def test_start_failure_is_a_durable_rollback_then_required_hook_recovers(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'worker.dhwb',create=True,handler=FIXTURES/'validation_hook.py')) as book:
            operation=str(uuid.uuid4())
            request={'operation_id':operation,'expected_state':book.info()['state'],'action':'edit',
                     'cells':[{'row':1,'col':1,'source':'7'}]}
            with patch('multiprocessing.process.BaseProcess.start',side_effect=OSError('Cannot spawn')):
                with self.assertRaises(EngineError) as failed: book.mutate(request)
            self.assertEqual(failed.exception.body['code'],'HOOK_FAILED')
            self.assertEqual(failed.exception.body['outcome'],'rolled_back')
            self.assertEqual(book.operation(operation),failed.exception.body)
            self.assertEqual(book.read('Sheet1',{'r2':1,'c2':2})['cells'],[])
            receipt=book.mutate({**request,'operation_id':str(uuid.uuid4()),'expected_state':book.info()['state']})
            self.assertEqual(receipt['outcome'],'committed')
            self.assertEqual([c['value'] for c in book.read('Sheet1',{'r2':1,'c2':2})['cells']],[7,14])

    def test_protocol_failure_reaps_worker_pipes_and_broker(self):
        previous={p.pid for p in multiprocessing.active_children()}
        for mode in ('malformed','oversized','unknown','bad_done','eof'):
            with self.subTest(mode=mode),tracked_context() as (processes,pipes),patch.object(hooks,'_worker',protocol_worker):
                dispatch=Mock()
                with self.assertRaises(hooks.HookFailure):
                    hooks.invoke(mode,[],'protocol-failed',dispatch,time.monotonic()+5)
                dispatch.assert_not_called()
                self.assert_released(processes,pipes,previous)

    def test_blocked_pipe_response_still_times_out_and_reaps(self):
        previous={p.pid for p in multiprocessing.active_children()}
        with tracked_context() as (processes,pipes),patch.object(hooks,'_worker',protocol_worker):
            dispatch=Mock(return_value='x'*(hooks.MAX_BROKER_BYTES-100))
            started=time.monotonic()
            with self.assertRaisesRegex(hooks.HookFailure,'timed out'):
                hooks.invoke('blocked_response',[],'blocked',dispatch,started+5)
            self.assertLess(time.monotonic()-started,3.5)
            dispatch.assert_called_once()
            self.assert_released(processes,pipes,previous)

    def test_startup_uses_parent_deadline(self):
        start=multiprocessing.process.BaseProcess.start
        def delayed_start(process):
            time.sleep(.05); start(process)
        with patch('multiprocessing.process.BaseProcess.start',new=delayed_start),patch.object(hooks,'_worker',protocol_worker):
            with self.assertRaisesRegex(hooks.HookFailure,'timed out'):
                hooks.invoke('hang',[],'startup',Mock(),time.monotonic()+.01)

    def test_broker_thread_start_failure_reaps_started_worker(self):
        previous={p.pid for p in multiprocessing.active_children()}
        start=threading.Thread.start
        def fail_broker(thread):
            if thread.name=='DataHorizonHookBroker': raise RuntimeError('Cannot start broker')
            return start(thread)
        with tracked_context() as (processes,pipes),patch.object(hooks,'_worker',protocol_worker),patch.object(threading.Thread,'start',new=fail_broker):
            with self.assertRaises(hooks.HookFailure):
                hooks.invoke('hang',[],'broker-startup',Mock(),time.monotonic()+5)
            self.assert_released(processes,pipes,previous)

    def test_maximum_cell_source_round_trips_through_json_broker(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'copy.py'
            path.write_text("def on_change(context, changes):\n    for change in changes:\n        if change['row'] == 1 and change['col'] == 1:\n            cell=context.get('Sheet1',1,1)\n            context.set('Sheet1',1,2,cell['source'],kind='text')\n")
            source='\x01'*65536  # JSON escaping is larger than the stored UTF-8.
            with closing(Workbook(Path(temp)/'large.dhwb',create=True,handler=path)) as book:
                receipt=book.mutate({'operation_id':str(uuid.uuid4()),'expected_state':book.info()['state'],'action':'edit',
                                     'cells':[{'row':1,'col':1,'source':source,'kind':'text'}]})
                self.assertEqual(receipt['outcome'],'committed')
                self.assertEqual(book.read('Sheet1',{'r2':1,'c2':2})['cells'][1]['source'],source)

    def test_repeated_failures_and_successes_release_resources(self):
        previous={p.pid for p in multiprocessing.active_children()}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'repeated.py'
            path.write_text("import os\ndef on_change(context, changes):\n    if context.value('Sheet1',1,1) == 'crash': os._exit(13)\n")
            with closing(Workbook(Path(temp)/'repeated.dhwb',create=True,handler=path)) as book,tracked_context() as (processes,pipes):
                for source in ('crash','1','crash','2','crash','3'):
                    operation=str(uuid.uuid4())
                    request={'operation_id':operation,'expected_state':book.info()['state'],'action':'edit',
                             'cells':[{'row':1,'col':1,'source':source}]}
                    if source=='crash':
                        with self.assertRaises(EngineError) as failed: book.mutate(request)
                        self.assertEqual(failed.exception.body['code'],'HOOK_FAILED')
                        self.assertEqual(book.operation(operation),failed.exception.body)
                    else:
                        self.assertEqual(book.mutate(request)['outcome'],'committed')
                        self.assertEqual(book.read('Sheet1',{'r2':1,'c2':1})['cells'][0]['value'],int(source))
                    self.assert_released(processes,pipes,previous)
