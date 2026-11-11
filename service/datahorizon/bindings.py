# SPDX-License-Identifier: GPL-3.0-only
"""Event bindings: inert native v6 catalog objects that attach a workbook function to edits in a scope."""
import ast
import uuid
from .engine import EngineError,require,valid_uuid
from . import relational
from .formula import MAX_ROWS,MAX_COLS,adjust_interval
from .hooks import invoke,supports_event
from .names import find as find_name,parse_reference,quote_sheet,describe as describe_name

MAX_BINDINGS=16
FIELDS={'id','event','module_id','function','required','scope'}
SPAN=('row1','col1','row2','col2')
EVENTS={'cell_change':('sheet','range','name'),'table_change':('table',)}
SCOPE_FIELDS={'sheet':{'kind','sheet_id'},'range':{'kind','range_id','sheet_id',*SPAN},'name':{'kind','name_id'},'table':{'kind','table_id'}}


def canonical(value):
    return isinstance(value,str) and valid_uuid(value) and str(uuid.UUID(value))==value


def definition(value):
    invalid=lambda message:EngineError('INVALID_FORMAT',message,409)
    if not isinstance(value,dict) or set(value)!=FIELDS:raise invalid('Invalid stored binding fields')
    if not (canonical(value['id']) and canonical(value['module_id'])):raise invalid('Stored binding identities must be canonical UUIDs')
    scope=value['scope']
    if value['event'] not in EVENTS or type(value['required']) is not bool or not isinstance(value['function'],str) or not value['function']:raise invalid('Invalid stored binding')
    if not isinstance(scope,dict) or scope.get('kind') not in EVENTS[value['event']] or set(scope)!=SCOPE_FIELDS[scope['kind']]:raise invalid('Invalid stored binding scope')
    kind=scope['kind']
    if kind=='sheet':
        if not (scope['sheet_id'] is None or canonical(scope['sheet_id'])):raise invalid('Invalid stored binding scope')
    elif kind=='range':
        if not canonical(scope['range_id']) or not (scope['sheet_id'] is None or canonical(scope['sheet_id'])):raise invalid('Invalid stored binding scope')
        if scope['sheet_id'] is None or scope['row1'] is None:
            if not all(scope[key] is None for key in SPAN):raise invalid('A broken binding scope has no coordinates')
        else:
            if not all(type(scope[key]) is int for key in SPAN) or not (1<=scope['row1']<=scope['row2']<=MAX_ROWS and 1<=scope['col1']<=scope['col2']<=MAX_COLS):raise invalid('Stored binding scope is outside the grid')
    elif not canonical(scope[kind+'_id']):raise invalid('Invalid stored binding scope')
    return value


def bindings(db):
    result=[value for _,value in relational.objects(db,'binding')]
    require(len(result)<=MAX_BINDINGS,'BINDING_LIMIT','Workbook bindings exceed 16',409)
    for value in result:definition(value)
    return result


def validate(db):
    """Open-time referential check: every binding refers to a stored module."""
    from .workbook_code import modules
    items=bindings(db)
    if items:
        known={item['id'] for item in modules(db)}
        require(all(item['module_id'] in known for item in items),'INVALID_FORMAT','Binding refers to a module that does not exist',409)


def fingerprint_view(binding):
    scope={key:value for key,value in binding['scope'].items() if key not in SPAN}
    return dict(binding,scope=scope)


def describe(db,item,module_names):
    scope=item['scope'];kind=scope['kind'];reference=None;broken=False
    if kind=='sheet':
        broken=scope['sheet_id'] is None or db.execute('SELECT 1 FROM sheets WHERE id=?',(scope['sheet_id'],)).fetchone() is None
    elif kind=='range':
        row=db.execute('SELECT name FROM sheets WHERE id=?',(scope['sheet_id'],)).fetchone() if scope['sheet_id'] else None
        broken=row is None or scope['row1'] is None
        if not broken:
            from .formula import column_name
            first=column_name(scope['col1'])+str(scope['row1']);last=column_name(scope['col2'])+str(scope['row2'])
            reference=quote_sheet(row[0])+'!'+(first if first==last else first+':'+last)
    elif kind=='name':
        found=find_name(db,scope['name_id']);described=describe_name(db,found) if found else None
        broken=described is None or described['broken']
        reference=None if broken else described['reference']
    else:
        broken=next((t for t in relational.tables(db) if t['id']==scope['table_id']),None) is None
    return dict(item,module=module_names.get(item['module_id']),reference=reference,broken=broken)


def described(db):
    from .workbook_code import modules
    names={item['id']:item['name'] for item in modules(db)}
    return [describe(db,item,names) for item in bindings(db)]


def label(db,item):
    from .workbook_code import modules
    name=next((m['name'] for m in modules(db) if m['id']==item['module_id']),'?')
    return name+'.'+item['function']


def scope_from(book,request,event):
    scope=request.get('scope')
    require(isinstance(scope,dict) and scope.get('kind') in EVENTS[event],'BINDING_INVALID','This event does not allow that scope kind')
    kind=scope['kind'];key={'sheet':'sheet','range':'reference','name':'name','table':'table'}[kind]
    require(set(scope)=={'kind',key} and isinstance(scope[key],str),'BINDING_INVALID','Invalid binding scope fields')
    if kind=='sheet':
        row=book.db.execute('SELECT id FROM sheets WHERE id=? OR name=? COLLATE NOCASE',(scope[key],scope[key])).fetchone()
        require(row is not None,'SHEET_NOT_FOUND','Binding scope names a sheet that does not exist',404)
        return dict(kind='sheet',sheet_id=row[0])
    if kind=='range':
        target=parse_reference(book,scope[key]);return dict(kind='range',range_id=None,**target)
    if kind=='name':
        found=find_name(book.db,scope[key]);require(found is not None,'NAME_NOT_FOUND','Name does not exist',404)
        require(found['row1'] is not None,'BINDING_INVALID','A broken name cannot be a binding scope')
        return dict(kind='name',name_id=found['id'])
    found=next((t for t in relational.tables(book.db) if scope[key] in (t['id'],) or t['name'].lower()==scope[key].lower()),None)
    require(found is not None,'TABLE_NOT_FOUND','Binding scope names a table that does not exist',404)
    return dict(kind='table',table_id=found['id'])


def mutate(book,request):
    from .workbook_code import fingerprint,find,modules
    require(not set(request)-{'action','sheet','operation_id','expected_state','label','fingerprint','binding_id','event','scope','module','function','required'},'INVALID_ARGUMENT','Unsupported binding operation field')
    require(request.get('fingerprint')==fingerprint(book.db),'CODE_CHANGED','Review current workbook code before changing bindings',409)
    current=bindings(book.db);before=None
    if 'binding_id' in request:
        before=next((item for item in current if item['id']==request['binding_id']),None) if isinstance(request['binding_id'],str) else None
        require(before is not None,'BINDING_NOT_FOUND','Binding does not exist',404)
    action=request['action'];after=None
    if action=='binding_save':
        event=request.get('event');require(event in EVENTS,'BINDING_INVALID','Event must be cell_change or table_change')
        require(type(request.get('required',True)) is bool,'BINDING_INVALID','required must be true or false')
        module=find(book.db,request.get('module'))
        function=request.get('function')
        names=set()
        if isinstance(function,str):
            tree=ast.parse(module['source']);names={node.name for node in tree.body if isinstance(node,ast.FunctionDef)}
        require(function in names,'BINDING_INVALID','Function must be a top-level def in the module')
        scope=scope_from(book,request,event)
        if scope['kind']=='range':
            old=(before or {}).get('scope',{})
            same=old.get('kind')=='range' and old.get('row1') is not None and all(old[k]==scope[k] for k in ('sheet_id',*SPAN))
            scope['range_id']=old['range_id'] if same else str(uuid.uuid4())
        require(before is not None or len(current)<MAX_BINDINGS,'BINDING_LIMIT','A workbook holds at most 16 bindings',409)
        after=definition(dict(id=before['id'] if before else str(uuid.uuid4()),event=event,module_id=module['id'],function=function,required=request.get('required',True),scope=scope))
    else:
        require(before is not None,'BINDING_NOT_FOUND','Choose the binding to delete',404)
    if book.handler:require(supports_event(book.handler,'on_code_structure'),'UNSUPPORTED_HOOK_SCOPE','Required handler must explicitly define on_code_structure(ctx,event)')
    def notify(phase):
        if book.handler:invoke(book.handler,dict(phase=phase,action=action,operation_id=request['operation_id'],before=before,after=after),request['operation_id'],book.hook_dispatch,book.deadline,event='on_code_structure')
    notify('before')
    relational.ensure_version(book,6)
    if after:relational.store(book,'binding',after)
    else:book.db.execute("DELETE FROM dh_relational WHERE kind='binding' AND id=?",(before['id'],))
    bindings(book.db);notify('after')
    return dict(binding_id=(after or before)['id'],code_fingerprint=fingerprint(book.db))


def stored(book):
    if book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]<6:return []
    return bindings(book.db)


def _inside(change,sheet_id,area):
    return change['sheet']==sheet_id and area['row1'] is not None and area['row1']<=change['row']<=area['row2'] and area['col1']<=change['col']<=area['col2']


def plan(book,cell_batch,table_batch):
    """Calls for this round, in stored order, each with the records inside its current scope."""
    items=stored(book)
    if not items or not (cell_batch or table_batch):return []
    from .workbook_code import modules
    known={item['id']:item for item in modules(book.db)};calls=[]
    for item in items:
        scope=item['scope'];kind=scope['kind']
        if kind=='table':changes=[change for change in table_batch if change['table']==scope['table_id']]
        elif kind=='sheet':changes=[change for change in cell_batch if scope['sheet_id'] is not None and change['sheet']==scope['sheet_id']]
        elif kind=='range':changes=[change for change in cell_batch if _inside(change,scope['sheet_id'],scope)]
        else:
            name=find_name(book.db,scope['name_id'])
            changes=[change for change in cell_batch if name is not None and _inside(change,name['sheet_id'],name)]
        if changes:
            module=known[item['module_id']]
            calls.append(dict(binding=item,module_name=module['name'],source=module['source'],function=item['function'],changes=changes))
    return calls


def available(source,function):
    """Static check without executing workbook code: it compiles and defines the top-level function."""
    try:tree=ast.parse(source);compile(tree,'<workbook>','exec',dont_inherit=True)
    except (SyntaxError,ValueError,RecursionError):return False
    return any(isinstance(node,ast.FunctionDef) and node.name==function for node in tree.body)


def admit(book,calls,admission):
    """Drop skipped optional calls and reject required ones that cannot run; approval is read once per operation."""
    if not calls:return calls
    if 'approved' not in admission:
        from .workbook_code import activation
        admission.update(approved=activation(book)['approved'],skipped=set())
    admitted=[]
    for call in calls:
        item=call['binding']
        if item['id'] in admission['skipped']:continue
        reason=None if admission['approved'] else 'BINDING_NOT_APPROVED'
        if reason is None and not available(call['source'],call['function']):reason='BINDING_UNAVAILABLE'
        if reason is None:admitted.append(call);continue
        if item['required']:
            prefix='Binding '+call['module_name']+'.'+call['function']+': ';named=dict(binding_id=item['id'],module=call['module_name'],function=call['function'])
            if reason=='BINDING_NOT_APPROVED':raise EngineError(reason,prefix+'workbook code is not approved, so edits in its scope are blocked',403,binding=named)
            raise EngineError(reason,prefix+'the module no longer defines this top-level function',409,binding=named)
        skip(book,admission,[(call,reason)])
    return admitted


def skip(book,admission,skipped):
    """Skip optional bindings for the rest of the operation with one notice each."""
    for call,reason in skipped:
        item=call['binding'];admission['skipped'].add(item['id'])
        book.binding_notices.append(dict(binding_id=item['id'],function=item['function'],reason=reason))


def adjust_structure(book,sheet_id,axis,start,count,delete):
    """Move, expand, contract or break range scopes after a whole-row/column edit."""
    keys=('row1','row2') if axis=='row' else ('col1','col2');broken=[]
    for item in stored(book):
        scope=item['scope']
        if scope['kind']!='range' or scope['sheet_id']!=sheet_id or scope['row1'] is None:continue
        moved=adjust_interval(scope[keys[0]],scope[keys[1]],start,count,delete,MAX_ROWS if axis=='row' else MAX_COLS)
        if moved is None:
            scope=dict(scope,row1=None,col1=None,row2=None,col2=None);broken.append(label(book.db,item))
        else:scope=dict(scope,**{keys[0]:moved[0],keys[1]:moved[1]})
        relational.store(book,'binding',definition(dict(item,scope=scope)))
    return sorted(broken)


def break_sheet(book,sheet_id):
    """Break every sheet or range scope on a sheet that is being deleted."""
    broken=[]
    for item in stored(book):
        scope=item['scope']
        if scope['kind'] not in ('sheet','range') or scope['sheet_id']!=sheet_id:continue
        broken.append(label(book.db,item))
        scope=dict(scope,sheet_id=None,**({key:None for key in SPAN} if scope['kind']=='range' else {}))
        relational.store(book,'binding',definition(dict(item,scope=scope)))
    return sorted(broken)
