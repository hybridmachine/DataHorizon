# SPDX-License-Identifier: GPL-3.0-only
"""Spawned trusted Python with a bounded transaction broker, never a sandbox."""
from contextlib import redirect_stdout,redirect_stderr
import multiprocessing
import os
import linecache
import queue
import sys
import threading
import time
import traceback
import types
from .hooks import BrokerContext,HookFailure,_receive,_send


def tail(text,maximum=8192):
    return text[-maximum:].encode('utf-8',errors='replace')[-maximum:].decode('utf-8',errors='ignore')


class MacroFailure(Exception):
    def __init__(self,code,message,logs=''):
        super().__init__(tail(message));self.code,self.logs=code,tail(logs)


def _worker(pipe,source,module_name,name,arguments,operation):
    class Output:
        def write(self,text):
            chunk=tail(text)
            if chunk:_send(pipe,dict(method='log',text=chunk))
            return len(text)
        def flush(self):pass
    watchdog=threading.Timer(2.5,lambda:os._exit(124));watchdog.daemon=True;watchdog.start()
    try:
        with redirect_stdout(Output()),redirect_stderr(Output()):
            try:
                loaded=types.ModuleType('__datahorizon_workbook__')
                loaded.__file__='<workbook:'+module_name+'>'
                sys.modules[loaded.__name__]=loaded
                namespace=loaded.__dict__
                linecache.cache[loaded.__file__]=(len(source),None,source.splitlines(True),loaded.__file__)
                exec(compile(source,namespace['__file__'],'exec',dont_inherit=True),namespace)
                result=namespace[name](BrokerContext(pipe,operation),**arguments)
            except BaseException:
                _send(pipe,dict(method='failed',code='MACRO_FAILED',error=tail(traceback.format_exc(limit=8))))
            else:
                try:_send(pipe,dict(method='done',result=result))
                except (ValueError,TypeError,OverflowError,HookFailure):
                    _send(pipe,dict(method='failed',code='MACRO_RESULT',error='Macro result is not bounded finite JSON'))
    finally:
        watchdog.cancel();pipe.close()


def invoke(module,name,arguments,operation,dispatch,deadline,check,diagnostics):
    started=time.monotonic();deadline=min(deadline,started+2)
    parent=child=process=pump=None;stop=threading.Event()
    incoming=queue.Queue(maxsize=1);replies=queue.Queue(maxsize=1)
    diagnostics.update(logs='')
    def offer(message):
        while not stop.is_set():
            try:incoming.put(message,timeout=.02);return
            except queue.Full:pass
    def broker():
        try:
            while not stop.is_set():
                message=_receive(parent);method=message.get('method');offer(message)
                if method in ('done','failed'):return
                if method=='log':continue
                while not stop.is_set():
                    try:reply=replies.get(timeout=.02);break
                    except queue.Empty:pass
                else:return
                _send(parent,reply)
        except Exception as error:offer(error)
    try:
        check()
        context=multiprocessing.get_context('spawn');parent,child=context.Pipe()
        process=context.Process(target=_worker,args=(child,module['source'],module['name'],name,arguments,operation),daemon=True)
        process.start();child.close();pump=threading.Thread(target=broker,daemon=True,name='DataHorizonMacroBroker');pump.start()
        while True:
            check()
            if time.monotonic()>=deadline:raise MacroFailure('MACRO_TIMEOUT','Python macro exceeded its two-second budget',diagnostics['logs'])
            try:message=incoming.get(timeout=min(.02,max(0,deadline-time.monotonic())))
            except queue.Empty:continue
            check()
            if time.monotonic()>=deadline:raise MacroFailure('MACRO_TIMEOUT','Python macro exceeded its two-second budget',diagnostics['logs'])
            if isinstance(message,Exception):raise MacroFailure('MACRO_WORKER_FAILED','Macro exited or broke its broker protocol: '+str(message),diagnostics['logs'])
            method=message.pop('method',None)
            if method=='log':
                if set(message)!={'text'} or not isinstance(message['text'],str):raise MacroFailure('MACRO_PROTOCOL','Invalid macro log message',diagnostics['logs'])
                diagnostics['logs']=tail(diagnostics['logs']+message['text']);continue
            if method=='done':
                if set(message)!={'result'}:raise MacroFailure('MACRO_PROTOCOL','Invalid macro completion',diagnostics['logs'])
                check();return message['result']
            if method=='failed':
                if set(message)!={'code','error'} or message['code'] not in ('MACRO_FAILED','MACRO_RESULT') or not isinstance(message['error'],str):raise MacroFailure('MACRO_PROTOCOL','Invalid macro failure',diagnostics['logs'])
                diagnostics['traceback']=tail(message['error'])
                raise MacroFailure(message['code'],message['error'],diagnostics['logs'])
            if method not in ('get','value','set','table_schema','table_get','table_set','table_insert','table_delete'):
                raise MacroFailure('MACRO_PROTOCOL','Unknown macro context method',diagnostics['logs'])
            try:result=dispatch(method,message)
            except Exception as error:
                if getattr(error,'body',{}).get('code') in ('CANCELLED','TIMEOUT'):raise
                diagnostics['context_code']=getattr(error,'body',{}).get('code',getattr(error,'sqlite_errorname',type(error).__name__))
                raise MacroFailure('MACRO_CONTEXT',str(error),diagnostics['logs']) from error
            replies.put(dict(result=result))
    except MacroFailure:raise
    except (OSError,ValueError,EOFError,RuntimeError) as error:raise MacroFailure('MACRO_WORKER_FAILED','Macro worker could not communicate: '+str(error),diagnostics['logs']) from error
    finally:
        stop.set()
        if child is not None:child.close()
        if process is not None:
            try:
                if process.pid is not None:
                    process.join(timeout=.05)
                    if process.is_alive():process.terminate();process.join(timeout=.5)
                    if process.is_alive():process.kill();process.join()
            finally:process.close()
        if parent is not None:parent.close()
        if pump is not None:pump.join(timeout=1)
        diagnostics['elapsed_ms']=round((time.monotonic()-started)*1000,3)
