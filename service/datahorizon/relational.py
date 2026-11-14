# SPDX-License-Identifier: GPL-3.0-only
"""Versioned structured data. All writes run inside Workbook.mutate."""
import json
import math
import re
import sqlite3
import time
import uuid

from .engine import EngineError, encoded, require, valid_uuid, sql_deadline
from .temporal import Temporal
from .hooks import invoke, supports_event

SCHEMA = '''CREATE TABLE dh_relational (kind TEXT NOT NULL, id TEXT NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(kind,id)) WITHOUT ROWID;'''
TYPES = ('text','integer','number','boolean','date','time','datetime')
STRUCTURAL = {'table_create','table_drop','table_promote','table_schema','index_create','index_drop','relationship_create','relationship_drop','table_repair','query_save','query_rename','query_delete'}
ACTIONS = set(STRUCTURAL)
DATA_ACTIONS={'table_insert','table_update','table_delete'}
ACTIONS.update(DATA_ACTIONS)
MAX_INTEGER = 2**53-1


def quote(name):
    return '"'+name.replace('"','""')+'"'


def name(value):
    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',value) is not None,
            'INVALID_IDENTIFIER','SQL names need 1–64 ASCII letters/digits/underscores, beginning with a letter')
    require(value.lower() not in ('cells','sheets','operations') and not value.lower().startswith(('dh_','sqlite_')),
            'RESERVED_IDENTIFIER','This SQL identifier is reserved')
    return value


def objects(db,kind=None):
    if db.execute('SELECT format_version FROM dh_meta').fetchone()[0]<4:return []
    rows=db.execute('SELECT kind,id,substr(payload,1,262145) FROM dh_relational'+(' WHERE kind=?' if kind else '')+' ORDER BY kind,id',
                    (kind,) if kind else ())
    result=[];size=0
    for key,identity,payload in rows:
        require(isinstance(payload,str),'INVALID_FORMAT','Relational metadata must be Text')
        require(len(payload.encode())<=256*1024,'LIMIT','Relational object exceeds 256 KiB')
        size+=len(payload.encode())
        require(len(result)<512 and size<=8*1024*1024,'LIMIT','Relational catalog exceeds 512 objects/8 MiB')
        try:value=json.loads(payload)
        except ValueError as error:raise EngineError('INVALID_FORMAT','Invalid relational catalog JSON',409) from error
        require(isinstance(value,dict) and value.get('id')==identity and valid_uuid(identity),'INVALID_FORMAT','Invalid relational object identity',409)
        result.append((key,value))
    return result


def tables(db):return [value for _,value in objects(db,'table')]


def table(db,identity):
    require(isinstance(identity,str),'INVALID_ARGUMENT','A table name or ID is required')
    found=next((item for item in tables(db) if item['id']==identity or item['name'].lower()==identity.lower()),None)
    require(found is not None,'TABLE_NOT_FOUND','Table does not exist',404)
    return found


def columns(value,*,stored=False):
    require(isinstance(value,list) and 1<=len(value)<=256,'INVALID_SCHEMA','A table needs 1–256 columns')
    result=[];names=set();identities=set()
    for item in value:
        require(isinstance(item,dict) and not set(item)-{'id','name','type','required','unique'},'INVALID_SCHEMA','Unsupported column definition')
        column=dict(id=item.get('id',str(uuid.uuid4())),name=name(item.get('name')),type=item.get('type','text'),
                    required=item.get('required',False),unique=item.get('unique',False))
        require(valid_uuid(column['id']) and column['id'] not in identities and column['name'].lower() not in names,
                'INVALID_SCHEMA','Duplicate column name or identity')
        require(str(uuid.UUID(column['id']))==column['id'],'INVALID_SCHEMA','Column IDs use canonical lowercase UUID spelling')
        require(column['type'] in TYPES and type(column['required']) is bool and type(column['unique']) is bool,
                'INVALID_SCHEMA','Invalid column type or constraint')
        require(not stored or item==column,'INVALID_FORMAT','Incomplete stored column metadata',409)
        names.add(column['name'].lower());identities.add(column['id']);result.append(column)
    return result


def value(column,stored):
    if stored is None:
        require(not column['required'],'REQUIRED_VALUE',column['name']+' requires a value');return None
    kind=column['type']
    if kind=='text':
        require(isinstance(stored,str) and len(stored.encode())<=65536,'INVALID_VALUE',column['name']+' requires Text of at most 64 KiB')
    elif kind=='integer':
        require(type(stored) is int and -MAX_INTEGER<=stored<=MAX_INTEGER,'INVALID_VALUE',column['name']+' requires an exact Integer')
    elif kind=='number':
        require(type(stored) in (int,float),'INVALID_VALUE',column['name']+' requires a finite Number')
        try:finite=math.isfinite(stored)
        except OverflowError:finite=False
        require(finite and (type(stored) is not int or abs(stored)<=MAX_INTEGER),'INVALID_VALUE','Number is nonfinite or loses integer precision')
        stored=float(stored)
    elif kind=='boolean':
        require(type(stored) is bool,'INVALID_VALUE',column['name']+' requires Boolean true/false')
    else:
        try:Temporal(stored,kind)
        except (ValueError,TypeError) as error:raise EngineError('INVALID_VALUE',column['name']+' requires canonical ISO '+kind) from error
    return stored


def from_sql(column,stored):
    if column['type']=='boolean' and type(stored) is int and stored in (0,1):return bool(stored)
    return stored


def table_sql(item,relationships=(),all_tables=()):
    fields=['"_dh_row_id" INTEGER PRIMARY KEY AUTOINCREMENT CHECK("_dh_row_id">0)']
    for col in item['columns']:
        identifier=quote(col['name']);kind=col['type']
        affinity='INTEGER' if kind in ('integer','boolean') else 'REAL' if kind=='number' else 'TEXT'
        check=(f"typeof({identifier})='integer' AND {identifier} BETWEEN {-MAX_INTEGER} AND {MAX_INTEGER}" if kind=='integer' else
               f"typeof({identifier})='integer' AND {identifier} IN (0,1)" if kind=='boolean' else
               f"typeof({identifier}) IN ('integer','real') AND {identifier} BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308" if kind=='number' else
               f"typeof({identifier})='text' AND length(CAST({identifier} AS BLOB))<=65536")
        fields.append(f'{identifier} {affinity}'+(' NOT NULL' if col['required'] else '')+
                      (' UNIQUE' if col['unique'] else '')+f' CHECK({identifier} IS NULL OR ({check}))')
    from .relationships import foreign_keys
    fields.extend(foreign_keys(item,relationships,all_tables))
    return 'CREATE TABLE '+quote(item['name'])+' ('+', '.join(fields)+')'


def ensure(book):
    if book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]<4:
        book.ensure_metadata();book.db.execute(SCHEMA);book.db.execute('UPDATE dh_meta SET format_version=4')


def ensure_version(book,minimum):
    ensure(book);book.db.execute('UPDATE dh_meta SET format_version=MAX(format_version,?)',(minimum,))


def store(book,kind,item):
    ensure(book)
    payload=encoded(item)
    require(len(payload.encode())<=256*1024,'LIMIT','Relational object exceeds 256 KiB')
    book.db.execute('INSERT INTO dh_relational VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload',(kind,item['id'],payload))
    objects(book.db)


def validate_schema(db,signature,base_schema):
    from .engine import expected_schema
    require(expected_schema(SCHEMA)[0] in signature,'FORMAT_VERSION','Native format v4 requires its matching relational catalog',409)
    catalog=objects(db);registered=[];names=set();version=db.execute('SELECT format_version FROM dh_meta').fetchone()[0]
    for kind,item in catalog:
        require(kind in ('table','index','relationship','query') or kind=='module' and version>=5 or kind in ('name','binding') and version>=6,'INVALID_FORMAT','Unsupported relational object kind',409)
        if kind=='name':
            from .names import definition
            definition(item)
        if kind=='binding':
            from .bindings import definition as binding_definition
            binding_definition(item)
        if kind!='table':continue
        require(set(item)=={'id','name','columns'},'INVALID_FORMAT','Invalid table catalog fields',409)
        name(item['name']);columns(item['columns'],stored=True)
        require(item['name'].lower() not in names,'INVALID_FORMAT','Duplicate table names',409)
        names.add(item['name'].lower());registered.append(item)
    require(len(registered)<=64,'LIMIT','At most 64 structured tables')
    from .table_indexes import indexes,validate,sql
    from .relationships import relationships,validate as validate_relationships
    registered_relationships=relationships(db);validate_relationships(registered_relationships,registered)
    registered_indexes=indexes(db)
    for item in registered_indexes:
        validate(item,registered,registered_relationships);require(item['name'].lower() not in names,'INVALID_FORMAT','Duplicate SQL names');names.add(item['name'].lower())
    for rel in registered_relationships:require(sum(index['owner']==rel['id'] for index in registered_indexes)==1,'INVALID_FORMAT','Relationship supporting index is missing')
    expected=expected_schema(base_schema+SCHEMA+';'.join([*(table_sql(item,registered_relationships,registered) for item in registered),*(sql(item,validate(item,registered,registered_relationships)) for item in registered_indexes)])+';')
    require(signature==expected,'SCHEMA_CHANGED','Relational schema does not match its supported catalog; preserve the file for recovery',409)
    from .workbook_code import modules
    modules(db)
    from .bindings import validate as validate_bindings
    validate_bindings(db)
    from .query_views import validate as validate_queries
    validate_queries(db,registered,time.monotonic()+2)
    return registered


def validate_data(db,registered,deadline,*,repair=False):
    issues=[]
    for item in registered:
        projections=['"_dh_row_id"']+[f'CASE WHEN typeof({quote(col["name"])}) IN (\'text\',\'blob\') THEN substr({quote(col["name"])},1,65537) ELSE {quote(col["name"])} END' for col in item['columns']]
        for row in db.execute('SELECT '+','.join(projections)+' FROM '+quote(item['name'])):
            require(time.monotonic()<=deadline,'TIMEOUT','Table validation exceeded the opening budget')
            try:
                require(type(row[0]) is int and row[0]>0,'INVALID_VALUE','Invalid record identity')
                for col,stored in zip(item['columns'],row[1:]):value(col,from_sql(col,stored))
            except (EngineError,UnicodeError) as error:
                issue=dict(table=item['id'],row_id=str(row[0]),code='INVALID_VALUE',message=str(error)[:256])
                require(repair,'DATA_REPAIR_REQUIRED','Stored table values require explicit inspection/repair: '+item['name'],409)
                if len(issues)<100:issues.append(issue)
    from .relationships import issues as relationship_issues
    return (issues+relationship_issues(db,registered,deadline,repair=repair))[:100]


def catalog(book,args):
    with book.lock:
        deadline=time.monotonic()+2;book.refresh(deadline=deadline);book.db.execute('BEGIN')
        try:
            with sql_deadline(book.db,deadline):
                book.refresh(deadline=deadline)
                require(args.get('expected_state')==book.state(),'STALE_STATE','Refresh the workbook catalog',409)
                from .table_indexes import indexes
                from .relationships import relationships
                from .query_views import summaries
                result=dict(state=book.state(),tables=tables(book.db),indexes=indexes(book.db),relationships=relationships(book.db),queries=summaries(book.db))
                require(len(encoded(result).encode())<=4*1024*1024,'READ_LIMIT','Relational catalog exceeds 4 MiB')
                return result
        finally:book.db.execute('ROLLBACK')


def structure_hook(book,request,phase,before):
    if not book.handler:return
    require(supports_event(book.handler,'on_table_structure'),'UNSUPPORTED_HOOK_SCOPE','Required handler must explicitly define on_table_structure(context,event)')
    kind='query' if request['action'].startswith('query_') else 'relationship' if request['action'].startswith('relationship_') else 'index' if request['action'].startswith('index_') else 'table'
    identifier=request.get(kind,request.get('name'))
    current=next((item for key,item in objects(book.db,kind) if identifier in (item['id'],item.get('name'))),None)
    if phase=='before':book.relational_event_before=current
    event=dict(phase=phase,action=request['action'],origin='managed',operation_id=request['operation_id'],
               arguments={key:request[key] for key in ('table','name','columns','sheet','r1','c1','r2','c2','header','conversion_policy','drop_columns','allow_data_loss','query','sql','parameters','defaults','parent_table','parent_column','child_table','child_column','on_delete','on_update','relationship','index','unique','definition_digest') if key in request},
               object_before=getattr(book,'relational_event_before',None),object_after=current if phase=='after' else None,
               before=before,after=tables(book.db) if phase=='after' else None)
    invoke(book.handler,event,request['operation_id'],book.hook_dispatch,book.deadline,event='on_table_structure')


def mutate(book,request):
    if request['action'].startswith('query_'):
        from .query_views import mutate as query_mutate
        return query_mutate(book,request)
    if request['action'].startswith('relationship_') or request['action']=='table_repair':
        from .relationships import mutate as relationship_mutate
        return relationship_mutate(book,request)
    if request['action'].startswith('index_'):
        from .table_indexes import mutate as index_mutate
        return index_mutate(book,request)
    if request['action'] in DATA_ACTIONS:
        from .table_edits import mutate as edit
        return edit(book,request)
    if request['action']=='table_schema':
        from .table_schema import apply
        return apply(book,request)
    action=request['action'];before=tables(book.db)
    prepared=None
    if action=='table_promote':
        from .table_promotion import prepare
        prepared=prepare(book,request,deadline=book.deadline)
        require(prepared['can_create'],'TABLE_CONVERSION_FAILED','Correct conversion issues before creating a table')
        require(request.get('review_digest')==prepared.get('review_digest'),'STALE_REVIEW','Review the exact selection, column types and values again',409)
        from datetime import datetime
        book.operation_clock=datetime.fromisoformat(prepared['preview_clock'])
    structure_hook(book,request,'before',before)
    if action in ('table_create','table_promote'):
        require(len(before)<64,'LIMIT','At most 64 structured tables')
        item=dict(id=str(uuid.uuid4()),name=name(request.get('name')),columns=columns(request.get('columns')))
        require(not any(t['name'].lower()==item['name'].lower() for t in before),'TABLE_EXISTS','A table with that SQL name already exists',409)
        ensure(book);book.db.execute(table_sql(item));store(book,'table',item)
        result=dict(table=item)
        if prepared is not None:
            from .relational_rows import insert
            for values in prepared['records']:insert(book,item,values)
            result['created_records']=len(prepared['records'])
    elif action=='table_drop':
        item=table(book.db,request.get('table'))
        from .query_views import schema_dependencies
        schema_dependencies(book.db,item)
        from .relationships import relationships
        require(not any(item['id'] in (rel['parent_table'],rel['child_table']) for rel in relationships(book.db)),'DEPENDENCY_BLOCKED','Remove dependent relationships before dropping the table',409)
        from .table_indexes import indexes
        require(not any(index['table']==item['id'] for index in indexes(book.db)),'DEPENDENCY_BLOCKED','Remove dependent indexes before dropping the table',409)
        from .relational_rows import raw
        from .table_schema import capture
        from .engine import boolean
        permitted=boolean(request.get('allow_data_loss',False),'allow_data_loss');rows=[]
        limit=10000//len(item['columns'])
        for (identity,) in book.db.execute('SELECT "_dh_row_id" FROM '+quote(item['name'])+' LIMIT ?',(limit+1,)):
            book.check_operation();require(permitted,'TABLE_NOT_EMPTY','Explicitly review data loss before dropping a populated table')
            require(len(rows)<limit,'LIMIT','Table deletion exceeds 10,000 affected scalar fields')
            rows.append((str(identity),raw(book,item,str(identity))))
        for identity,record_before in rows:capture(book,item,identity,record_before,None)
        book.db.execute('DROP TABLE '+quote(item['name']));book.db.execute("DELETE FROM dh_relational WHERE kind='table' AND id=?",(item['id'],));result={}
        from .bindings import scoped
        broken=scoped(book,'table',[item['id']])
        if broken:result['broken_bindings']=broken
        from .charts import break_table
        broken_charts=break_table(book,item['id'])
        if broken_charts:result['broken_charts']=broken_charts
    else:raise EngineError('UNSUPPORTED_OPERATION','Unsupported table operation')
    structure_hook(book,request,'after',before)
    return result
