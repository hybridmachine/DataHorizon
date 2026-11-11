# SPDX-License-Identifier: GPL-3.0-only
"""Explicit atomic macros participate in Workbook.mutate and its durable receipt."""
import time
from .engine import EngineError,encoded,require
from .workbook_code import authorize,find,scalar
from .macro_runtime import invoke,MacroFailure


def run(book,request):
    require(not set(request)-{'action','sheet','operation_id','expected_state','label','fingerprint','module','macro','arguments'},'INVALID_ARGUMENT','Unsupported macro invocation field')
    current=authorize(book,request);module=find(book.db,request.get('module'))
    declaration=next((item for item in module['macros'] if item['name']==request.get('macro')),None)
    require(declaration is not None,'MACRO_NOT_FOUND','Macro is not declared',404)
    supplied=request.get('arguments',{})
    require(isinstance(supplied,dict) and len(encoded(supplied).encode())<=128*1024,'MACRO_ARGUMENT','Macro arguments require an object of at most 128 KiB')
    require(not set(supplied)-{p['name'] for p in declaration['parameters']},'MACRO_ARGUMENT','Unknown macro argument')
    arguments={}
    for parameter in declaration['parameters']:
        require(parameter['name'] in supplied or 'default' in parameter,'MACRO_ARGUMENT','Missing macro argument: '+parameter['name'])
        try:arguments[parameter['name']]=scalar(parameter,supplied.get(parameter['name'],parameter.get('default')))
        except EngineError as error:raise EngineError('MACRO_ARGUMENT',str(error)) from error
    diagnostics=dict(module=module['id'],name=declaration['name'],fingerprint=current['fingerprint'],logs='',execution='running')
    book.macro_diagnostics=diagnostics
    operation_deadline=book.deadline;book.deadline=min(operation_deadline,time.monotonic()+2)
    try:
        result=invoke(module,declaration['name'],arguments,request['operation_id'],book.hook_dispatch,book.deadline,book.check_operation,diagnostics)
        try:result=scalar(declaration['result'],result)
        except EngineError as error:raise EngineError('MACRO_RESULT',str(error),409) from error
    except MacroFailure as error:
        diagnostics['execution']='failed'
        raise EngineError(error.code,str(error),409) from error
    except EngineError as error:
        diagnostics['execution']='failed'
        if error.body['code']=='TIMEOUT' and time.monotonic()<operation_deadline:
            raise EngineError('MACRO_TIMEOUT','Python macro exceeded its two-second budget',409) from error
        raise
    finally:book.deadline=operation_deadline
    diagnostics['execution']='completed'
    return dict(macro_result=result,macro=diagnostics)
