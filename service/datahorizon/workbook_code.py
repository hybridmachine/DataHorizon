# SPDX-License-Identifier: GPL-3.0-only
"""Portable Python source; inspection and validation never execute workbook code."""
import ast
import hashlib
import keyword
import re
import uuid
from .engine import EngineError,encoded,require,valid_uuid
from . import relational
from .hooks import invoke,supports_event


def identifier(value):
    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',value) is not None
            and not keyword.iskeyword(value) and not value.startswith('__'),
            'INVALID_CODE','Names require a public ASCII Python identifier of at most 64 characters')
    return value


def spec(value,*,argument=False):
    allowed={'type','nullable'}|({'name','default'} if argument else set())
    require(isinstance(value,dict) and not set(value)-allowed and value.get('type') in relational.TYPES
            and type(value.get('nullable',False)) is bool,'INVALID_CODE','Invalid scalar declaration')
    result=dict(type=value['type'],nullable=value.get('nullable',False))
    if argument:
        result['name']=identifier(value.get('name'));require(result['name']!='ctx','INVALID_CODE','ctx is reserved for the transaction context')
        if 'default' in value:result['default']=scalar(result,value['default'])
    return result


def scalar(declaration,value):
    return relational.value(dict(name='Macro scalar',type=declaration['type'],required=not declaration['nullable']),value)


def definition(value):
    require(isinstance(value,dict) and set(value)=={'id','name','source','macros'},'INVALID_CODE','Module requires id, name, source and macros')
    require(valid_uuid(value['id']) and str(uuid.UUID(value['id']))==value['id'],'INVALID_CODE','Module identity must be a canonical UUID')
    identifier(value['name']);source=value['source']
    require(isinstance(source,str) and len(source.encode('utf-8'))<=128*1024,'CODE_LIMIT','Module source exceeds 128 KiB')
    try:
        tree=ast.parse(source,filename='<workbook:'+value['name']+'>')
        compile(tree,'<workbook:'+value['name']+'>','exec',dont_inherit=True)
    except (SyntaxError,ValueError,RecursionError) as error:
        raise EngineError('INVALID_CODE','Invalid module syntax: '+str(error)[:512]) from error
    functions={node.name for node in tree.body if isinstance(node,ast.FunctionDef)}
    require(isinstance(value['macros'],list) and len(value['macros'])<=32,'CODE_LIMIT','At most 32 macros per module')
    declarations=[];names=set()
    for macro in value['macros']:
        require(isinstance(macro,dict) and not set(macro)-{'name','parameters','result'},'INVALID_CODE','Invalid macro declaration')
        name=identifier(macro.get('name'));require(name in functions and name not in names,'INVALID_CODE','Macro requires a unique top-level synchronous function')
        parameters=macro.get('parameters',[]);require(isinstance(parameters,list) and len(parameters)<=32,'CODE_LIMIT','At most 32 macro parameters')
        parameters=[spec(parameter,argument=True) for parameter in parameters]
        require(len({p['name'] for p in parameters})==len(parameters),'INVALID_CODE','Duplicate parameter name')
        declarations.append(dict(name=name,parameters=parameters,result=spec(macro.get('result'))));names.add(name)
    result=dict(id=value['id'],name=value['name'],source=source,macros=declarations)
    require(len(encoded(result).encode())<=256*1024,'CODE_LIMIT','Module metadata exceeds 256 KiB')
    return result


def modules(db):
    result=[value for _,value in relational.objects(db,'module')]
    require(len(result)<=16 and sum(len(encoded(value).encode()) for value in result)<=2*1024*1024,'CODE_LIMIT','Workbook code exceeds 16 modules/2 MiB')
    names=set()
    for value in result:
        try:validated=definition(value)
        except (ValueError,TypeError,AttributeError,UnicodeError) as error:raise EngineError('INVALID_CODE','Invalid stored module',409) from error
        require(validated==value and value['name'] not in names,'INVALID_CODE','Invalid or duplicate stored module')
        names.add(value['name'])
    return result


def fingerprint(db):
    from .bindings import bindings,fingerprint_view
    attached=bindings(db) if db.execute('SELECT format_version FROM dh_meta').fetchone()[0]>=6 else []
    value=modules(db) if not attached else dict(modules=modules(db),bindings=[fingerprint_view(item) for item in attached])
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def approval_keys(values):
    from .code_approval import validate_approvals
    try:return validate_approvals(values)
    except ValueError as error:raise EngineError('INVALID_CODE_APPROVAL',str(error)) from error


def activation(book):
    digest=fingerprint(book.db);key=book.id+':'+digest
    return dict(fingerprint=digest,activation_key=key,approved=key in book.approved_code)


def authorize(book,request):
    current=activation(book)
    require(request.get('fingerprint')==current['fingerprint'],'CODE_CHANGED','Review the current workbook code before invoking it',409)
    require(current['approved'],'CODE_NOT_APPROVED','The service operator must approve this exact workbook/code activation key',403)
    return current


def find(db,identity):
    require(isinstance(identity,str),'INVALID_CODE','Module identity is required')
    result=next((item for item in modules(db) if identity in (item['id'],item['name'])),None)
    require(result is not None,'MODULE_NOT_FOUND','Workbook module does not exist',404)
    return result


def read(book,args):
    require(not set(args)-{'expected_state','module'},'INVALID_ARGUMENT','Unsupported code inspection field')
    with book.lock:
        book.refresh();book.db.execute('BEGIN')
        try:
            book.refresh();require(args.get('expected_state')==book.state(),'STALE_STATE','Refresh code before inspection',409)
            items=modules(book.db)
            if 'module' in args:items=[find(book.db,args['module'])]
            from .bindings import described
            return dict(state=book.state(),modules=items,bindings=described(book.db) if book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]>=6 else [],**activation(book))
        finally:book.db.execute('ROLLBACK')


def mutate(book,request):
    require(not set(request)-{'action','sheet','operation_id','expected_state','label','fingerprint','module','name','source','macros'},'INVALID_ARGUMENT','Unsupported code operation field')
    require(request.get('fingerprint')==fingerprint(book.db),'CODE_CHANGED','Review current workbook code before changing it',409)
    before=find(book.db,request['module']) if 'module' in request else None
    require(request['action']=='module_save' or before is not None,'MODULE_NOT_FOUND','Choose the module to delete',404)
    after=None
    if request['action']=='module_delete':
        from .bindings import stored
        require(all(item['module_id']!=before['id'] for item in stored(book)),'MODULE_IN_USE','Delete the bindings that use this module first',409)
    if request['action']=='module_save':
        after=definition(dict(id=before['id'] if before else str(uuid.uuid4()),name=request.get('name'),source=request.get('source'),macros=request.get('macros',[])))
        require(all(item['id']==after['id'] or item['name']!=after['name'] for item in modules(book.db)),'DUPLICATE_MODULE','Module name already exists')
    if book.handler:require(supports_event(book.handler,'on_code_structure'),'UNSUPPORTED_HOOK_SCOPE','Required handler must explicitly define on_code_structure(ctx,event)')
    def event(phase):
        if book.handler:invoke(book.handler,dict(phase=phase,action=request['action'],operation_id=request['operation_id'],before=before,after=after),request['operation_id'],book.hook_dispatch,book.deadline,event='on_code_structure')
    event('before')
    relational.ensure_version(book,5)
    if after:relational.store(book,'module',after)
    else:book.db.execute("DELETE FROM dh_relational WHERE kind='module' AND id=?",(before['id'],))
    modules(book.db);event('after')
    return dict(module_id=(after or before)['id'],code_fingerprint=fingerprint(book.db))
