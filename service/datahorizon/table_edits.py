# SPDX-License-Identifier: GPL-3.0-only
"""Known-target record mutations and read-only value conversion previews."""
import hashlib
import time
from .engine import encoded, require, boolean, sql_deadline
from .relational import table, DATA_ACTIONS
from .relational_rows import fields,identity,raw,put,insert
from .table_promotion import convert

ACTIONS=DATA_ACTIONS


def plan(book,args):
    item=table(book.db,args.get('table'));action=args.get('action');result=dict(action=action,table=item['id'])
    if action=='table_insert':
        rows=args.get('rows');require(isinstance(rows,list) and 1<=len(rows)<=10000,'INVALID_ARGUMENT','Insert needs 1–10,000 record objects')
        result['rows']=[fields(item,row) for row in rows]
    elif action=='table_update':
        edits=args.get('edits');require(isinstance(edits,list) and 1<=len(edits)<=10000,'INVALID_ARGUMENT','Update needs 1–10,000 explicit record targets')
        result['edits']=[];seen=set()
        for edit in edits:
            require(isinstance(edit,dict) and set(edit)=={'row_id','values'},'INVALID_ARGUMENT','Each update needs row_id and values')
            row_id=edit['row_id'];identity(row_id);require(row_id not in seen,'INVALID_ARGUMENT','Record targets must be unique');seen.add(row_id)
            values=fields(item,edit['values']);require(bool(values),'INVALID_ARGUMENT','Each update needs at least one value')
            result['edits'].append(dict(row_id=row_id,values=values))
    else:
        require(action=='table_delete','UNSUPPORTED_OPERATION','Unknown table data operation')
        ids=args.get('record_ids');require(isinstance(ids,list) and 1<=len(ids)<=10000 and all(isinstance(i,str) for i in ids) and len(ids)==len(set(ids)),
            'INVALID_ARGUMENT','Delete needs unique explicit record IDs')
        for row_id in ids:identity(row_id)
        result['record_ids']=ids
    count=(len(item['columns'])*len(result['rows']) if action=='table_insert' else
           sum(len(edit['values']) for edit in result['edits']) if action=='table_update' else len(item['columns'])*len(result['record_ids']))
    require(count<=10000,'LIMIT','Record operation exceeds 10,000 scalar fields')
    return item,result


def digest(book,item,operation):
    return hashlib.sha256(encoded(dict(state=book.state(),schema=item,operation=operation)).encode()).hexdigest()


def mutate(book,args):
    item,operation=plan(book,args)
    if 'review_digest' in args:require(args['review_digest']==digest(book,item,operation),'STALE_REVIEW','Review these exact typed record edits again',409)
    ids=[]
    if operation['action']=='table_insert':
        for values in operation['rows']:ids.append(insert(book,item,values))
    elif operation['action']=='table_update':
        for edit in operation['edits']:
            before=raw(book,item,edit['row_id']);require(before is not None,'RECORD_NOT_FOUND','Reviewed record no longer exists',404)
            put(book,item,edit['row_id'],{**before,**edit['values']});ids.append(edit['row_id'])
    else:
        for row_id in operation['record_ids']:
            require(raw(book,item,row_id) is not None,'RECORD_NOT_FOUND','Reviewed record no longer exists',404)
            put(book,item,row_id,None);ids.append(row_id)
    return dict(table_id=item['id'],record_ids=ids)


def preview(book,args):
    """Parse declared column types; native constraints/hooks still run on Apply."""
    from .engine import EngineError
    with book.lock:
        deadline=time.monotonic()+2;book.refresh(deadline=deadline);book.db.execute('BEGIN')
        try:
            with sql_deadline(book.db,deadline):
                book.refresh(deadline=deadline);require(args.get('expected_state')==book.state(),'STALE_STATE','Refresh the reviewed table',409)
                item=table(book.db,args.get('table'));action=args.get('action')
                require(isinstance(action,str) and action in ACTIONS,'INVALID_ARGUMENT','Choose table_insert, table_update or table_delete')
                as_null=boolean(args.get('blank_as_null',False),'blank_as_null')
                incoming=args.get('rows' if action=='table_insert' else 'edits',[])
                require(isinstance(incoming,list) and len(incoming)<=10000,'INVALID_ARGUMENT','Record preview needs bounded row objects')
                columns={key:col for col in item['columns'] for key in (col['id'],col['name'])};converted=[];issues=[];charged=0
                if action=='table_delete':incoming=[]
                for index,entry in enumerate(incoming,1):
                    require(time.monotonic()<=deadline,'TIMEOUT','Record conversion exceeded its time budget')
                    source=entry if action=='table_insert' else entry.get('values') if isinstance(entry,dict) else None
                    require(isinstance(source,dict),'INVALID_ARGUMENT','Record values must be objects')
                    values={};charged+=len(source);require(charged<=10000,'LIMIT','Record preview exceeds 10,000 fields')
                    for key,stored in source.items():
                        require(key in columns,'COLUMN_NOT_FOUND','Selected column does not exist');col=columns[key]
                        require(col['id'] not in values,'INVALID_ARGUMENT','A column was specified twice')
                        try:values[col['id']]=convert(col,None if as_null and stored=='' else stored,'invariant')
                        except EngineError as error:
                            if len(issues)<100:issues.append(dict(record=index,column=col['name'],code=error.body['code'],message=str(error)[:256]))
                    converted.append(values if action=='table_insert' else dict(row_id=entry.get('row_id'),values=values))
                request=dict(action=action,table=item['id'])
                if action=='table_insert':request['rows']=converted
                elif action=='table_update':request['edits']=converted
                else:request['record_ids']=args.get('record_ids')
                if not issues:
                    _,request=plan(book,request)
                    if action=='table_update':
                        for edit in request['edits']:require(raw(book,item,edit['row_id']) is not None,'RECORD_NOT_FOUND','Reviewed record no longer exists',404)
                result=dict(state=book.state(),can_apply=not issues,issues=issues,operation=request,
                    report=['Values use the declared column types and invariant conversion. Native constraints and required Python rules run on Apply.'])
                if not issues:result['review_digest']=digest(book,item,request)
                require(len(encoded(result).encode())<=4*1024*1024,'READ_LIMIT','Record preview exceeds 4 MiB')
                return result
        finally:book.db.execute('ROLLBACK')
