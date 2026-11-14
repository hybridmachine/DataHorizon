# SPDX-License-Identifier: GPL-3.0-only
"""Native saved definitions: authorize/prepare shape without evaluating data rows."""
import hashlib
import re
import sqlite3
import time
import uuid
from contextlib import closing
from .engine import EngineError,encoded,require,valid_uuid,sql_deadline
from .relational import objects,tables,store,structure_hook
from .query_sql import bindings,authorize
from .table_promotion import convert

ACTIONS={'query_save','query_rename','query_delete'}


def queries(db):return [item for _,item in objects(db,'query')]

def definition_digest(item):return hashlib.sha256(encoded(item).encode()).hexdigest()


def query_name(value):
    require(isinstance(value,str) and 1<=len(value)<=128 and len(value.encode())<=512 and value==value.strip() and not any(ord(c)<32 or ord(c)==127 for c in value),'INVALID_IDENTIFIER','Query names need 1–128 characters without surrounding whitespace or controls')
    return value


def query(db,identifier):
    require(isinstance(identifier,str),'INVALID_ARGUMENT','A saved query name or ID is required')
    # An ID names exactly one query; a query that is merely NAMED like another query's ID never shadows it.
    saved=queries(db);item=next((item for item in saved if identifier==item['id']),None) or next((item for item in saved if identifier.casefold()==item['name'].casefold()),None)
    require(item is not None,'QUERY_NOT_FOUND','Saved query does not exist',404);return item


def statement(sql):
    """Remove an optional terminal separator; reject additional statements."""
    i=0;separator=None
    while i<len(sql):
        if sql[i].isspace():i+=1;continue
        if sql.startswith('--',i):
            end=sql.find('\n',i+2);i=len(sql) if end<0 else end+1;continue
        if sql.startswith('/*',i):
            end=sql.find('*/',i+2);require(end>=0,'INVALID_SQL','Unterminated SQL comment');i=end+2;continue
        require(separator is None,'SQL_DENIED','Save one SELECT/CTE statement')
        if sql[i] in "'\"`[":
            quote=']' if sql[i]=='[' else sql[i];i+=1
            while i<len(sql):
                if sql[i]==quote:
                    i+=1
                    if quote!=']' and i<len(sql) and sql[i]==quote:i+=1;continue
                    break
                i+=1
        elif sql[i]==';':separator=i;i+=1
        else:i+=1
    return sql[:separator] if separator is not None else sql


def prepare(db,sql,parameters,registered,deadline):
    require(isinstance(parameters,list),'INVALID_PARAMETERS','Declare parameter types')
    defaults={'text':'','integer':0,'number':0,'boolean':False,'date':'2000-01-01','time':'00:00:00','datetime':'2000-01-01T00:00:00'}
    require(all(isinstance(p,dict) and isinstance(p.get('type','text'),str) for p in parameters),'INVALID_PARAMETERS','Invalid parameter declaration')
    dummy={p.get('name'):defaults.get(p.get('type','text')) for p in parameters if isinstance(p.get('name'),str)}
    declarations,values=bindings(sql,parameters,dummy);clean=statement(sql)
    limits=((sqlite3.SQLITE_LIMIT_SQL_LENGTH,65792),(sqlite3.SQLITE_LIMIT_LENGTH,4*1024*1024),(sqlite3.SQLITE_LIMIT_COLUMN,512),(sqlite3.SQLITE_LIMIT_EXPR_DEPTH,100),(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,64),(sqlite3.SQLITE_LIMIT_VDBE_OP,250000))
    previous=[];denied=[]
    try:
        for category,limit in limits:previous.append((category,db.setlimit(category,limit)))
        dependencies,denied=authorize(db,registered)
        with sql_deadline(db,deadline):
            cursor=db.execute(zero_rows(clean),values)
            shape=[dict(id=str(i),name=c[0]) for i,c in enumerate(cursor.description)]
            require(1<=len(shape)<=256 and all(len(c['name'].encode())<=1024 for c in shape) and sum(len(c['name'].encode()) for c in shape)<=65536,'QUERY_LIMIT','Saved result shape exceeds query limits')
            require(cursor.fetchone() is None,'INTERNAL_ERROR','Definition review cannot evaluate result rows')
            return declarations,[dict(table=key,columns=sorted(value)) for key,value in sorted(dependencies.items())],shape
    except sqlite3.Error as error:
        raise EngineError('SQL_DENIED' if denied else 'INVALID_SQL',str(error)[:512],409) from error
    finally:
        db.set_authorizer(None)
        for category,limit in previous:db.setlimit(category,limit)


def zero_rows(sql):
    """Replace only an outer LIMIT; preserve original duplicate result labels."""
    i=depth=0;limit=None;select=False
    while i<len(sql):
        if sql.startswith('--',i):
            end=sql.find('\n',i+2);i=len(sql) if end<0 else end+1;continue
        if sql.startswith('/*',i):
            end=sql.find('*/',i+2);i=len(sql) if end<0 else end+2;continue
        ch=sql[i]
        if ch in "'\"`[":
            closing=']' if ch=='[' else ch;i+=1
            while i<len(sql):
                if sql[i]==closing:
                    i+=1
                    if closing!=']' and i<len(sql) and sql[i]==closing:i+=1;continue
                    break
                i+=1
            continue
        if ch=='(':depth+=1
        elif ch==')':depth-=1
        elif ch==':':
            match=re.match(r'[A-Za-z][A-Za-z0-9_]*',sql[i+1:]);i+=1+(len(match.group()) if match else 0);continue
        match=re.match(r'[A-Za-z_][A-Za-z0-9_]*',sql[i:])
        if match:
            if depth==0 and match.group().upper()=='SELECT':select=True
            if depth==0 and select and match.group().upper()=='LIMIT':limit=i
            i+=len(match.group());continue
        i+=1
    if not select:return 'SELECT * FROM (\n'+sql+'\n) LIMIT 0'
    return (sql[:limit] if limit is not None else sql)+'\nLIMIT 0'


def build(book,args,deadline):
    identity=args.get('query',str(uuid.uuid4()));require(valid_uuid(identity),'INVALID_ARGUMENT','Saved query identity must be a UUID')
    existing=next((item for item in queries(book.db) if item['id']==identity),None)
    require(existing is not None or len(queries(book.db))<64,'LIMIT','At most 64 saved queries')
    name=query_name(args.get('name'));require(not any(item['id']!=identity and item['name'].casefold()==name.casefold() for item in queries(book.db)),'QUERY_EXISTS','A query with this name already exists',409)
    sql=args.get('sql');parameters,deps,shape=prepare(book.db,sql,args.get('parameters',[]),tables(book.db),deadline)
    defaults=args.get('defaults',{});require(isinstance(defaults,dict),'INVALID_PARAMETERS','Saved defaults must be an explicit object');known={p['name']:p for p in parameters};normalized={}
    for key,value in defaults.items():
        require(key in known,'INVALID_PARAMETERS','Default has no matching parameter declaration');p=known[key];normalized[key]=convert(dict(name=key,type=p['type'],required=not p['nullable']),value,'invariant')
    item=dict(id=identity,name=name,sql=sql,parameters=parameters,defaults=normalized,dependencies=deps,shape=shape)
    require(len(encoded(item).encode())<=256*1024,'LIMIT','Saved query definition/defaults exceed 256 KiB')
    digest=hashlib.sha256(encoded(dict(state=book.state(),query=item)).encode()).hexdigest()
    return dict(state=book.state(),query=item,previous=existing,review_digest=digest,source_request=dict(query=identity,name=name,sql=sql,parameters=parameters,defaults=normalized),report=['Authorization, dependencies and result labels prepared with LIMIT 0; no data rows evaluated. Save does not run the query. Only explicit defaults are stored. Session history clears.'])


def preview(book,args):
    with book.lock:
        deadline=time.monotonic()+2;book.refresh(deadline=deadline);book.db.execute('BEGIN')
        try:
            with sql_deadline(book.db,deadline):
                book.refresh(deadline=deadline);require(args.get('expected_state')==book.state(),'STALE_STATE','Refresh before reviewing this query definition',409);return build(book,args,deadline)
        finally:book.db.execute('ROLLBACK')


def read(book,args):
    with book.lock:
        deadline=time.monotonic()+2;book.refresh(deadline=deadline);book.db.execute('BEGIN')
        try:
            with sql_deadline(book.db,deadline):
                book.refresh(deadline=deadline);require(args.get('expected_state')==book.state(),'STALE_STATE','Refresh the saved query catalog',409)
                item=query(book.db,args.get('query'));return dict(state=book.state(),query=item,definition_digest=definition_digest(item))
        finally:book.db.execute('ROLLBACK')


def summaries(db):return [dict(id=item['id'],name=item['name'],parameter_count=len(item['parameters']),columns=len(item['shape'])) for item in queries(db)]


def validate(db,registered,deadline):
    names=set();items=queries(db);require(len(items)<=64,'LIMIT','At most 64 saved queries')
    for item in items:
        require(set(item)=={'id','name','sql','parameters','defaults','dependencies','shape'},'INVALID_FORMAT','Invalid saved query fields')
        query_name(item['name']);require(item['name'].casefold() not in names,'INVALID_FORMAT','Duplicate saved query name');names.add(item['name'].casefold())
        declarations,deps,shape=prepare(db,item['sql'],item['parameters'],registered,deadline)
        require(declarations==item['parameters'] and deps==item['dependencies'] and shape==item['shape'],'QUERY_DEPENDENCY_CHANGED','Saved query dependencies or shape changed; preserve the native file for recovery',409)
        require(isinstance(item['defaults'],dict),'INVALID_FORMAT','Invalid saved defaults');known={p['name']:p for p in declarations}
        for key,value in item['defaults'].items():
            require(key in known,'INVALID_FORMAT','Unknown saved default');p=known[key];require(convert(dict(name=key,type=p['type'],required=not p['nullable']),value,'strict')==value,'INVALID_FORMAT','Invalid saved default type')


def schema_dependencies(db,old,target=None):
    affected=[]
    for item in queries(db):
        dep=next((d for d in item['dependencies'] if d['table']==old['id']),None)
        if dep is None:continue
        require(target is not None and old['name']==target['name'],'DEPENDENCY_BLOCKED','Remove or revise dependent query '+item['name']+' before dropping/renaming this table',409)
        before={c['id']:c for c in old['columns']};after={c['id']:c for c in target['columns']}
        used=set(dep['columns']);require(('*' not in used or old['columns']==target['columns']) and all(key in after and before.get(key)==after[key] for key in used if key not in ('*','_dh_row_id')),'DEPENDENCY_BLOCKED','Remove or revise dependent query '+item['name']+' before changing referenced columns',409)
        affected.append(item)
    if affected and target!=old:
        from .relational import quote
        proposed=[target if t['id']==old['id'] else t for t in tables(db)];deadline=time.monotonic()+2
        # Preparation against an empty proposed schema catches SELECT *, new
        # ambiguity and changed binding/shape without rewriting SQL or copying
        # data. Affinities and names match the native tables.
        with closing(sqlite3.connect(':memory:')) as scratch:
            with sql_deadline(scratch,deadline):
                for table in proposed:
                    fields=['"_dh_row_id" INTEGER PRIMARY KEY']+[quote(c['name'])+(' INTEGER' if c['type'] in ('integer','boolean') else ' REAL' if c['type']=='number' else ' TEXT') for c in table['columns']]
                    scratch.execute('CREATE TABLE '+quote(table['name'])+' ('+','.join(fields)+')')
                for item in affected:
                    try:_,dependencies,shape=prepare(scratch,item['sql'],item['parameters'],proposed,deadline)
                    except EngineError as error:raise EngineError('DEPENDENCY_BLOCKED','Proposed schema invalidates saved query '+item['name'],409) from error
                    require(dependencies==item['dependencies'] and shape==item['shape'],'DEPENDENCY_BLOCKED','Proposed schema changes saved query '+item['name']+' dependencies or result shape',409)


def mutate(book,args):
    before=tables(book.db)
    if args['action']=='query_save':
        result=build(book,args,book.deadline);require(args.get('review_digest')==result['review_digest'],'STALE_REVIEW','Review this exact saved query definition again',409)
        structure_hook(book,args,'before',before);store(book,'query',result['query']);out=dict(query=result['query'],definition_digest=definition_digest(result['query']))
    else:
        item=query(book.db,args.get('query'))
        require('definition_digest' not in args or args['definition_digest']==definition_digest(item),'STALE_DEFINITION','Reload the changed saved definition before modifying it',409)
        structure_hook(book,args,'before',before)
        if args['action']=='query_rename':
            name=query_name(args.get('name'));require(not any(q['id']!=item['id'] and q['name'].casefold()==name.casefold() for q in queries(book.db)),'QUERY_EXISTS','Query name already exists',409);item={**item,'name':name};store(book,'query',item);out=dict(query=item,definition_digest=definition_digest(item))
        else:
            book.db.execute("DELETE FROM dh_relational WHERE kind='query' AND id=?",(item['id'],));out={}
            from .charts import break_query
            broken_charts=break_query(book,item['id'])
            if broken_charts:out['broken_charts']=broken_charts
    structure_hook(book,args,'after',before);return out
