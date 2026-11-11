# SPDX-License-Identifier: GPL-3.0-only
"""Full-scope typed predicates and authenticated, state-bound keyset pages."""
import base64
import hashlib
import hmac
import json
import secrets
import time
from .engine import EngineError, encoded, require, integer, boolean, sql_deadline
from .relational import table, quote, value, from_sql
from .relational_rows import identity


def criteria(item,args):
    known={col['id']:col for col in item['columns']};sort=args.get('sort',[]);filters=args.get('filters',[])
    require(isinstance(sort,list) and len(sort)<=8 and isinstance(filters,list) and len(filters)<=8,'INVALID_ARGUMENT','At most eight sort keys and filters')
    order=[];seen=set();terms=[];parameters=[]
    for entry in sort:
        require(isinstance(entry,dict) and set(entry)=={'column','direction'} and isinstance(entry['column'],str) and entry['column'] in known and entry['column'] not in seen and entry['direction'] in ('asc','desc'),'INVALID_ARGUMENT','Choose unique existing sort columns and asc/desc order')
        seen.add(entry['column']);order.append((known[entry['column']],entry['direction']))
    for entry in filters:
        require(isinstance(entry,dict) and not set(entry)-{'column','op','value'} and isinstance(entry.get('column'),str) and entry['column'] in known,'INVALID_ARGUMENT','Choose an existing filter column')
        col=known[entry['column']];identifier=quote(col['name']);op=entry.get('op')
        require(op in ('eq','ne','lt','le','gt','ge','contains','is_null','not_null'),'INVALID_ARGUMENT','Unsupported table filter operator')
        if op in ('is_null','not_null'):terms.append(identifier+(' IS NULL' if op=='is_null' else ' IS NOT NULL'));continue
        require('value' in entry,'INVALID_ARGUMENT','Filter value is required');stored=value({**col,'required':False},entry['value'])
        if stored is None:
            require(op in ('eq','ne'),'INVALID_ARGUMENT','NULL supports equality or is_null filters');terms.append(identifier+(' IS NULL' if op=='eq' else ' IS NOT NULL'));continue
        if op=='contains':
            require(col['type']=='text','INVALID_ARGUMENT','Contains requires Text');terms.append('instr('+identifier+',?)>0')
        else:terms.append(identifier+{'eq':'=','ne':'<>','lt':'<','le':'<=','gt':'>','ge':'>='}[op]+'?')
        parameters.append(int(stored) if type(stored) is bool else stored)
    return order,terms,parameters


def token(book,payload):
    if not hasattr(book,'_table_cursor_key'):book._table_cursor_key=secrets.token_bytes(32)
    data=encoded(payload).encode();signature=hmac.digest(book._table_cursor_key,data,'sha256')
    return base64.urlsafe_b64encode(signature+data).decode().rstrip('=')


def untoken(book,text):
    require(isinstance(text,str) and len(text)<=16384,'INVALID_CURSOR','Invalid table cursor',409)
    try:
        blob=base64.b64decode(text+'='*(-len(text)%4),altchars=b'-_',validate=True)
        require(hasattr(book,'_table_cursor_key') and hmac.compare_digest(blob[:32],hmac.digest(book._table_cursor_key,blob[32:],'sha256')),'INVALID_CURSOR','Cursor signature does not match this session',409)
        payload=json.loads(blob[32:]);require(isinstance(payload,dict),'INVALID_CURSOR','Invalid cursor payload',409);return payload
    except (ValueError,UnicodeError) as error:raise EngineError('INVALID_CURSOR','Invalid table cursor',409) from error


def after_clause(order,last):
    """SQLite NULL ASC-first/DESC-last, then positive row identity ASC."""
    clauses=[];parameters=[];prefix=[];prior=[]
    for (col,direction),stored in zip(order,last):
        identifier=quote(col['name'])
        if stored is None:term=identifier+' IS NOT NULL' if direction=='asc' else '0'
        else:term=identifier+('>?' if direction=='asc' else '<? OR '+identifier+' IS NULL')
        clauses.append('('+' AND '.join([*prefix,'('+term+')'])+')');parameters.extend(prior)
        if stored is not None:parameters.append(stored)
        prefix.append(identifier+' IS ?');prior.append(stored)
    clauses.append('('+' AND '.join([*prefix,'"_dh_row_id">?'])+')');parameters.extend([*prior,identity(last[-1])])
    return '('+' OR '.join(clauses)+')',parameters


def read(book,args):
    with book.lock:
        deadline=time.monotonic()+2;book.refresh(deadline=deadline);book.db.execute('BEGIN')
        try:
            with sql_deadline(book.db,deadline):
                book.refresh(deadline=deadline);require(args.get('expected_state')==book.state(),'STALE_STATE','Refresh the table before paging',409)
                item=table(book.db,args.get('table'));known={col['id']:col for col in item['columns']};requested=args.get('columns',list(known)[:16])
                require(isinstance(requested,list) and 1<=len(requested)<=256 and all(isinstance(col,str) and col in known for col in requested) and len(requested)==len(set(requested)),'COLUMN_NOT_FOUND','Choose unique projected column IDs')
                selected=[known[key] for key in requested];limit=integer(args.get('limit',min(256,4096//len(selected))),1,min(256,4096//len(selected)),'limit')
                preview=boolean(args.get('preview',False),'preview');count=boolean(args.get('count',False),'count')
                order,terms,parameters=criteria(item,args);base_terms=list(terms);base_parameters=list(parameters)
                fingerprint=hashlib.sha256(encoded(dict(state=book.state(),schema=item,sort=args.get('sort',[]),filters=args.get('filters',[]),columns=requested,preview=preview)).encode()).hexdigest()
                cursor=args.get('cursor');after=args.get('after');targets=args.get('record_ids')
                require(not (cursor is not None and after is not None),'INVALID_ARGUMENT','Choose one cursor')
                if cursor is not None:
                    payload=untoken(book,cursor);require(payload.get('scope')==fingerprint,'STALE_CURSOR','Cursor belongs to another state, schema or view',409)
                    last_id=payload.get('last');identity(last_id)
                    stored=book.db.execute('SELECT '+','.join([quote(col['name']) for col,_ in order]+['"_dh_row_id"'])+' FROM '+quote(item['name'])+' WHERE "_dh_row_id"=?',(int(last_id),)).fetchone()
                    require(stored is not None,'STALE_CURSOR','Cursor record no longer exists',409);last=[*stored[:-1],str(stored[-1])]
                    clause,values=after_clause(order,last);terms.append(clause);parameters.extend(values)
                elif after is not None:
                    require(not order and not args.get('filters'),'INVALID_ARGUMENT','Sorted/filtered pages use authenticated cursors');terms.append('"_dh_row_id">?');parameters.append(identity(after))
                if targets is not None:
                    require(cursor is None and after is None and not order and not args.get('filters') and isinstance(targets,list) and 1<=len(targets)<=limit and all(isinstance(r,str) for r in targets) and len(targets)==len(set(targets)),'INVALID_ARGUMENT','Choose bounded explicit IDs without view criteria')
                    terms.append('"_dh_row_id" IN ('+','.join('?' for _ in targets)+')');parameters.extend(identity(row_id) for row_id in targets)
                sort_sql=','.join([quote(col['name'])+' '+direction.upper() for col,direction in order]+['"_dh_row_id" ASC'])
                projections=[('substr('+quote(col['name'])+',1,129)' if preview and col['type']=='text' else quote(col['name'])) for col in selected]
                query='SELECT "_dh_row_id",'+','.join(projections)+' FROM '+quote(item['name'])+(' WHERE '+' AND '.join('('+term+')' for term in terms) if terms else '')+' ORDER BY '+sort_sql+' LIMIT ?'
                rows=[];size=0;last=None;more=False
                for index,row in enumerate(book.db.execute(query,[*parameters,limit+1])):
                    if index==limit:more=True;break
                    values={};omitted=[];record_size=0
                    for col,stored in zip(selected,row[1:1+len(selected)]):
                        stored=from_sql(col,stored)
                        if preview and col['type']=='text' and stored is not None and len(stored)>128:stored=stored[:128];omitted.append(col['id'])
                        record_size+=len(encoded(stored).encode())+len(col['id'])+8;require(record_size<=3*1024*1024,'READ_LIMIT','Record exceeds page budget; request fewer columns');values[col['id']]=stored
                    record=dict(row_id=str(row[0]),values=values,omitted_columns=omitted)
                    size+=len(encoded(record).encode());require(size<=3*1024*1024,'READ_LIMIT','Table page exceeds 3 MiB');rows.append(record)
                    last=str(row[0])
                if targets is not None:
                    mapped={row['row_id']:row for row in rows};require(set(mapped)==set(targets),'RECORD_NOT_FOUND','A requested record does not exist',404);rows=[mapped[row_id] for row_id in targets];more=False
                result=dict(table=item['id'],state=book.state(),columns=selected,records=rows,next_cursor=token(book,dict(scope=fingerprint,last=last)) if rows and more else None,
                    next_after=rows[-1]['row_id'] if rows and more and not order and not args.get('filters') else None,count_status='not_requested')
                if count:
                    try:
                        with sql_deadline(book.db,min(deadline,time.monotonic()+.25)):
                            result['count']=book.db.execute('SELECT count(*) FROM '+quote(item['name'])+(' WHERE '+' AND '.join(base_terms) if base_terms else ''),base_parameters).fetchone()[0]
                            result['count_status']='exact'
                    except EngineError as error:
                        if error.body['code']!='TIMEOUT':raise
                        result['count_status']='unavailable'
                require(len(encoded(result).encode())<=4*1024*1024,'READ_LIMIT','Table response exceeds 4 MiB');return result
        finally:book.db.execute('ROLLBACK')
