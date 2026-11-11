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


def _load(source,module_name,qualified='__datahorizon_workbook__'):
    """Execute one stored module in a fresh namespace inside the worker."""
    loaded=types.ModuleType(qualified)
    loaded.__file__='<workbook:'+module_name+'>'
    sys.modules[loaded.__name__]=loaded
    namespace=loaded.__dict__
    linecache.cache[loaded.__file__]=(len(source),None,source.splitlines(True),loaded.__file__)
    exec(compile(source,namespace['__file__'],'exec',dont_inherit=True),namespace)
    return namespace


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
                namespace=_load(source,module_name)
                result=namespace[name](BrokerContext(pipe,operation),**arguments)
            except BaseException:
                _send(pipe,dict(method='failed',code='MACRO_FAILED',error=tail(traceback.format_exc(limit=8))))
            else:
                try:_send(pipe,dict(method='done',result=result))
                except (ValueError,TypeError,OverflowError,HookFailure):
                    _send(pipe,dict(method='failed',code='MACRO_RESULT',error='Macro result is not bounded finite JSON'))
    finally:
        watchdog.cancel();pipe.close()


def _reap(process):
    if process is None:return
    try:
        if process.pid is not None:
            process.join(timeout=.05)
            if process.is_alive():process.terminate();process.join(timeout=.5)
            if process.is_alive():process.kill();process.join()
    finally:process.close()


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
            if method not in ('get','value','values','set','table_schema','table_get','table_set','table_insert','table_delete'):
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
        _reap(process)
        if parent is not None:parent.close()
        if pump is not None:pump.join(timeout=1)
        diagnostics['elapsed_ms']=round((time.monotonic()-started)*1000,3)


CONTEXT_METHODS=('get','value','values','set','table_schema','table_get','table_set','table_insert','table_delete')


def _binding_worker(pipe,calls,operation):
    class Output:
        text=''
        def write(self,text):
            self.text=tail(self.text+text);return len(text)
        def flush(self):pass
    output=Output()
    watchdog=threading.Timer(2.5,lambda:os._exit(124));watchdog.daemon=True;watchdog.start()
    try:
        with redirect_stdout(output),redirect_stderr(output):
            namespaces={};load_errors={}
            def summary(error):return tail(''.join(traceback.format_exception_only(type(error),error)).strip())
            for index,call in enumerate(calls):
                # Name the binding before any of its code runs so a hang or crash is attributable.
                _send(pipe,dict(starting=index))
                name=call['module_name']
                if name not in namespaces and name not in load_errors:
                    try:namespaces[name]=_load(call['source'],name,'__datahorizon_workbook_'+name+'__')
                    except BaseException as error:load_errors[name]=(summary(error),tail(traceback.format_exc(limit=8)))
                if name in load_errors:
                    # Module code that fails at import never received a ctx, so it made no edits.
                    error,trace=load_errors[name]
                    if not call['required']:
                        _send(pipe,dict(skipped=index,error=error));continue
                    _send(pipe,dict(method='failed',code='BINDING_UNAVAILABLE',error=error,traceback=trace,logs=output.text))
                    return
                try:namespaces[name][call['function']](BrokerContext(pipe,operation),call['changes'])
                except BaseException as error:
                    _send(pipe,dict(method='failed',code='BINDING_FAILED',error=summary(error),traceback=tail(traceback.format_exc(limit=8)),logs=output.text))
                    return
            _send(pipe,dict(method='done'))
    finally:
        watchdog.cancel();pipe.close()


def invoke_bindings(calls,operation,dispatch,deadline):
    """Run one round of admitted binding calls, in order, in one spawned worker inside the parent transaction.

    Returns [(call, 'BINDING_UNAVAILABLE')] for optional calls skipped because their module failed to load."""
    from .engine import EngineError
    started=time.monotonic();deadline=min(deadline,started+2)
    parent=child=process=pump=None;stop=threading.Event();running=[0];skipped=[]
    incoming=queue.Queue(maxsize=1);replies=queue.Queue(maxsize=1)
    def failure(code,text,status=409,**details):
        call=calls[running[0]]
        return EngineError(code,tail('Binding '+call['module_name']+'.'+call['function']+': '+text),status,
                           binding=dict(binding_id=call['binding']['id'],module=call['module_name'],function=call['function'],**details))
    def timeout():return failure('BINDING_TIMEOUT','exceeded its two-second budget',408)
    def offer(message):
        while not stop.is_set():
            try:incoming.put(message,timeout=.02);return
            except queue.Full:pass
    def broker():
        try:
            while not stop.is_set():
                message=_receive(parent);offer(message)
                if message.get('method') in ('done','failed'):return
                if set(message) in ({'starting'},{'skipped','error'}):continue
                while not stop.is_set():
                    try:reply=replies.get(timeout=.02);break
                    except queue.Empty:pass
                else:return
                _send(parent,reply)
        except Exception as error:offer(error)
    try:
        context=multiprocessing.get_context('spawn');parent,child=context.Pipe()
        payload=[dict(required=call['binding']['required'],**{key:call[key] for key in ('module_name','source','function','changes')}) for call in calls]
        process=context.Process(target=_binding_worker,args=(child,payload,operation),daemon=True)
        process.start();child.close();pump=threading.Thread(target=broker,daemon=True,name='DataHorizonBindingBroker');pump.start()
        while True:
            if time.monotonic()>=deadline:raise timeout()
            try:message=incoming.get(timeout=min(.02,max(0,deadline-time.monotonic())))
            except queue.Empty:continue
            if time.monotonic()>=deadline:raise timeout()
            if isinstance(message,Exception):raise failure('BINDING_FAILED','worker exited or broke its broker protocol: '+str(message))
            if 'starting' in message:
                index=message['starting']
                if set(message)!={'starting'} or type(index) is not int or not running[0]<=index<len(calls):raise failure('BINDING_FAILED','invalid binding worker message')
                running[0]=index;continue
            if 'skipped' in message:
                if set(message)!={'skipped','error'} or message['skipped']!=running[0] or calls[running[0]]['binding']['required'] or not isinstance(message['error'],str):raise failure('BINDING_FAILED','invalid binding worker message')
                skipped.append((calls[running[0]],'BINDING_UNAVAILABLE'));continue
            method=message.pop('method',None)
            if method=='done':
                if message or running[0]!=len(calls)-1:raise failure('BINDING_FAILED','invalid binding worker completion')
                return skipped
            if method=='failed':
                if set(message)!={'code','error','traceback','logs'} or message['code'] not in ('BINDING_FAILED','BINDING_UNAVAILABLE') or not all(isinstance(value,str) for value in message.values()):raise failure('BINDING_FAILED','invalid binding worker failure')
                if message['code']=='BINDING_UNAVAILABLE':
                    raise failure('BINDING_UNAVAILABLE','the module failed to load: '+message['error'],traceback=message['traceback'],logs=message['logs'])
                raise failure('BINDING_FAILED',message['error'],traceback=message['traceback'],logs=message['logs'])
            if method not in CONTEXT_METHODS:raise failure('BINDING_FAILED','unknown binding context method')
            try:result=dispatch(method,message)
            except Exception as error:
                code=getattr(error,'body',{}).get('code')
                if code=='CANCELLED':raise
                if code=='TIMEOUT':raise failure('BINDING_TIMEOUT','exceeded the operation time budget',408) from error
                # A context error always fails the operation, even if the binding catches it.
                raise failure('BINDING_FAILED',str(error),context_code=code or getattr(error,'sqlite_errorname',type(error).__name__)) from error
            replies.put(dict(result=result))
    except EngineError:raise
    except (OSError,ValueError,EOFError,RuntimeError,HookFailure) as error:raise failure('BINDING_FAILED','worker could not start or communicate: '+str(error)) from error
    finally:
        stop.set()
        if child is not None:child.close()
        _reap(process)
        if parent is not None:parent.close()
        if pump is not None and pump.ident is not None:pump.join(timeout=1)
