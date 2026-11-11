# SPDX-License-Identifier: GPL-3.0-only
"""Explicitly activated trusted hooks, isolated for timeout/crash containment."""

import multiprocessing
import json
import os
import queue
from contextlib import redirect_stderr, redirect_stdout
import runpy
import time
import threading
import traceback
import ast
from pathlib import Path


class HookFailure(Exception):
    pass


MAX_BROKER_BYTES=1024*1024


def _send(pipe, message):
    payload=json.dumps(message,allow_nan=False,ensure_ascii=False).encode('utf-8')
    if len(payload)>MAX_BROKER_BYTES:
        raise HookFailure("Required Python handler broker message exceeds 1 MiB")
    pipe.send_bytes(payload)


def _receive(pipe):
    message=json.loads(pipe.recv_bytes(MAX_BROKER_BYTES),
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite broker JSON")))
    if not isinstance(message,dict): raise HookFailure("Invalid Python handler broker message")
    return message


def supports_structure(path):
    return supports_event(path,'on_structure')


def supports_event(path,event):
    """Explicit top-level opt-in without executing the trusted module in service."""
    try:
        with Path(path).open('rb') as source:data=source.read(1024*1024+1)
        if len(data)>1024*1024:return False
        return any(isinstance(node,ast.FunctionDef) and node.name==event for node in ast.parse(data).body)
    except (OSError,SyntaxError,ValueError):return False


def _worker(pipe, path, changes, operation, event='on_change'):
    class LogTail:
        def __init__(self):
            self.text=""

        def write(self, text):
            self.text=(self.text+text[-8192:])[-8192:]
            return len(text)

        def flush(self):
            pass

    class Context:
        operation_id = operation

        def request(self, method, **arguments):
            _send(pipe,{"method": method, **arguments})
            response = _receive(pipe)
            if "error" in response:
                raise RuntimeError(response["error"])
            return response.get("result")

        def get(self, sheet, row, col):
            return self.request("get", sheet=sheet, row=row, col=col)

        def value(self, sheet, row, col):
            return self.request("value", sheet=sheet, row=row, col=col)

        def set(self, sheet, row, col, source, kind="auto"):
            return self.request("set", sheet=sheet, row=row, col=col, source=source, kind=kind)

        def table_schema(self,table):return self.request('table_schema',table=table)

        def table_get(self,table,row_id):return self.request('table_get',table=table,row_id=row_id)

        def table_set(self,table,row_id,values):return self.request('table_set',table=table,row_id=row_id,values=values)

        def table_insert(self,table,values):return self.request('table_insert',table=table,values=values)

        def table_delete(self,table,row_id):return self.request('table_delete',table=table,row_id=row_id)

    logs=LogTail()
    # Also bound ordinary Python execution if the service is terminated before
    # its parent-side timeout can reap this worker. This is not a sandbox.
    watchdog=threading.Timer(2.5,lambda:os._exit(124))
    watchdog.daemon=True
    watchdog.start()
    # Workbook print/logging cannot corrupt the parent's startup protocol or
    # block on its stdout pipe. The diagnostic tail is included on failure.
    with redirect_stdout(logs), redirect_stderr(logs):
        try:
            module = runpy.run_path(path)
            module[event](Context(), changes)
            _send(pipe,{"method": "done"})
        except BaseException:
            try:
                _send(pipe,{"method": "failed", "error": traceback.format_exc(limit=8)+logs.text})
            except (OSError,ValueError,HookFailure):
                pass  # Parent timeout/closed channel still fails the operation.
        finally:
            watchdog.cancel()
            pipe.close()


def invoke(path, changes, operation, dispatch, deadline, *, event='on_change'):
    # Charge startup to the same invocation budget, not a fresh post-spawn one.
    deadline = min(deadline, time.monotonic() + 2)
    parent=child=process=pump=None
    incoming,replies=queue.Queue(),queue.Queue()

    def broker_io():
        # Pipe frame reads and response writes can block after poll() says a
        # frame is ready. Keep both off the transaction-owning thread so its
        # deadline can still terminate/reap a stuck peer.
        try:
            while True:
                message=_receive(parent)
                method=message.get('method')
                incoming.put(message)
                if method in ('done','failed'): return
                reply=replies.get()
                if reply is None: return
                _send(parent,reply)
        except Exception as error:
            incoming.put(error)

    try:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        arguments=(child,str(path),changes,operation)
        if event!='on_change':arguments+=(event,)
        process = context.Process(target=_worker, args=arguments, daemon=True)
        process.start()
        child.close()
        pump=threading.Thread(target=broker_io,name='DataHorizonHookBroker',daemon=True)
        pump.start()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HookFailure("Required Python handler timed out; operation rolled back")
            try:
                message=incoming.get(timeout=remaining)
            except queue.Empty as error:
                raise HookFailure("Required Python handler timed out; operation rolled back") from error
            if time.monotonic()>=deadline:
                raise HookFailure("Required Python handler timed out; operation rolled back")
            if isinstance(message,Exception):
                raise HookFailure("Required Python handler exited or broke its broker protocol: "+str(message)) from message
            method = message.pop("method",None)
            if method == "done":
                if message: raise HookFailure("Invalid Python handler completion message")
                return
            if method == "failed":
                if set(message)!={'error'} or not isinstance(message['error'],str):
                    raise HookFailure("Invalid Python handler failure message")
                raise HookFailure(message["error"])
            if method not in ('get','value','set','table_schema','table_get','table_set','table_insert','table_delete'):
                raise HookFailure("Unknown Python handler broker method")
            try:
                replies.put({"result": dispatch(method, message)})
            except Exception as error:
                # A broker validation error always fails the whole operation, even if
                # workbook code attempts to catch it and continue.
                raise HookFailure(str(error)) from error
    except HookFailure:
        raise
    except Exception as error:
        raise HookFailure("Required Python handler could not start or communicate: "+str(error)) from error
    finally:
        replies.put(None)
        try:
            if child is not None: child.close()
            if process is not None:
                try:
                    if process.pid is not None:
                        process.join(timeout=0.1)
                        if process.is_alive():
                            process.terminate()
                            process.join(timeout=1)
                        if process.is_alive():
                            process.kill()
                            process.join()
                finally:
                    process.close()
        finally:
            if pump is not None and pump.ident is not None: pump.join(timeout=1)
            if parent is not None: parent.close()
