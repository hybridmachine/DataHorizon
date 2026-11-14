# SPDX-License-Identifier: GPL-3.0-only
"""Reviewed transactional column conversion and catalog-preserving rebuilds."""
import hashlib
import time
import uuid
from .engine import EngineError, encoded, require, boolean, sql_deadline
from .relational import table, tables, columns, name, quote, table_sql, store, structure_hook
from .relational_rows import raw
from .table_promotion import convert


def prepare(book,args,*,deadline):
    require(args.get('expected_state')==book.state(),'STALE_STATE','Refresh the table before reviewing its schema',409)
    original=table(book.db,args.get('table'));supplied=args.get('columns')
    require(isinstance(supplied,list),'INVALID_SCHEMA','Review the complete column list')
    by_id={col['id']:col for col in original['columns']};by_name={col['name'].lower():col for col in original['columns']}
    for col in supplied:
        require(isinstance(col,dict),'INVALID_SCHEMA','Column definitions must be objects')
        retained=col.get('id') in by_id if isinstance(col.get('id'),str) else False
        require(retained or not isinstance(col.get('name'),str) or col['name'].lower() not in by_name,
                'COLUMN_ID_REQUIRED','Retained columns require their stable ID; use the catalog definition')
        if not retained:require(col.get('required',False) is False,'INVALID_SCHEMA','New columns must initially be nullable')
    definition=columns(supplied);new_ids={col['id'] for col in definition}
    dropped=set(by_id)-new_ids;reviewed=args.get('drop_columns',[])
    require(isinstance(reviewed,list) and all(isinstance(item,str) for item in reviewed) and len(reviewed)==len(set(reviewed)) and set(reviewed)==dropped,
            'DROP_REVIEW_REQUIRED','Explicitly review the IDs of all removed columns')
    target=dict(original,name=name(args.get('name',original['name'])),columns=definition)
    require(not any(item['id']!=original['id'] and item['name'].lower()==target['name'].lower() for item in tables(book.db)),
            'TABLE_EXISTS','The table name already exists',409)
    policy=args.get('conversion_policy','strict');require(policy in ('strict','invariant'),'INVALID_ARGUMENT','Choose strict or invariant conversion')
    keys=set(by_id)|new_ids;limit=10000//len(keys);rows=[];issues=[];size=0
    with sql_deadline(book.db,deadline):
        for (identity,) in book.db.execute('SELECT "_dh_row_id" FROM '+quote(original['name'])+' ORDER BY "_dh_row_id" LIMIT ?',(limit+1,)):
            require(len(rows)<limit,'LIMIT','Schema rebuild exceeds 10,000 affected scalar fields')
            require(time.monotonic()<=deadline,'TIMEOUT','Schema conversion exceeded its time budget')
            before=raw(book,original,str(identity));after={}
            for col in definition:
                try:after[col['id']]=convert(col,before.get(col['id']),policy)
                except EngineError as error:
                    if len(issues)<100:issues.append(dict(row_id=str(identity),column=col['name'],code=error.body['code'],message=str(error)[:256]))
            rows.append((str(identity),before,after));size+=len(encoded([before,after]).encode())
            require(size<=8*1024*1024,'LIMIT','Schema conversion before/after data exceeds 8 MiB')
        for col in definition:
            if col['unique']:
                seen=set()
                for identity,_,after in rows:
                    value=after.get(col['id'])
                    if value is not None:
                        if value in seen and len(issues)<100:issues.append(dict(row_id=identity,column=col['name'],code='UNIQUE_VALUE',message='Duplicate value in '+col['name']))
                        seen.add(value)
    payload=dict(state=book.state(),before=original,table=target,drop_columns=sorted(dropped),conversion_policy=policy,rows=rows)
    return dict(can_apply=not issues,table=target,original=original,rows=rows,record_count=len(rows),issues=issues,
                review_digest=hashlib.sha256(encoded(payload).encode()).hexdigest(),
                source_request=dict(table=original['id'],name=target['name'],columns=definition,drop_columns=sorted(dropped),conversion_policy=policy),
                report=['Schema conversion is atomic; row and retained column identities are preserved. Session Undo/Redo clears.',
                        str(len(dropped))+' columns are removed; omitted insert defaults remain NULL.'])


def preview(book,args):
    with book.lock:
        deadline=time.monotonic()+2;book.refresh(deadline=deadline);book.db.execute('BEGIN')
        try:
            with sql_deadline(book.db,deadline):
                book.refresh(deadline=deadline);result=prepare(book,args,deadline=deadline)
                rows=result.pop('rows');result['records_preview']=[dict(row_id=identity,before=before,after=after) for identity,before,after in rows[:min(20,4096//len(result['table']['columns']))]];result['state']=book.state()
                require(len(encoded(result).encode())<=4*1024*1024,'READ_LIMIT','Schema preview exceeds 4 MiB')
                return result
        finally:book.db.execute('ROLLBACK')


def capture(book,item,identity,before,after,*,schema_changed=False,changed_columns=None):
    key=('table',item['id'],identity)
    changed=set(changed_columns or set(before or {})|set(after or {}))
    added={(item['id'],identity,col) for col in changed}-book.table_field_changes
    require(len(book.changes)-len(book.table_record_keys)+len(book.table_field_changes)+len(added)<=10000,
            'LIMIT','Operation including schema/hook changes exceeds 10,000 scalar fields')
    book.change_bytes+=len(encoded([before,after]).encode());require(book.change_bytes<=8*1024*1024,'LIMIT','Operation before/after history exceeds 8 MiB')
    book.table_field_changes.update(added);book.table_record_keys.add(key)
    original=book.changes.get(key,[before,after])[0];book.changes[key]=[original,after]
    pending_before=book.pending_tables.get(key,{}).get('before',before)
    if schema_changed:book.table_schema_keys.add(key)
    book.pending_tables[key]=dict(table=item['id'],row_id=identity,before=pending_before,after=after,schema_changed=schema_changed)


def apply(book,args):
    prepared=prepare(book,args,deadline=book.deadline)
    require(prepared['can_apply'],'TABLE_CONVERSION_FAILED','Correct schema conversion issues before Apply')
    require(args.get('review_digest')==prepared['review_digest'],'STALE_REVIEW','Review the complete current schema conversion again',409)
    old=prepared['original'];target=prepared['table'];before_catalog=tables(book.db)
    from .table_indexes import dependencies,recreate
    dependencies(book,old,target)
    from .query_views import schema_dependencies as query_dependencies
    query_dependencies(book.db,old,target)
    from .relationships import schema_dependencies
    schema_dependencies(book,old,target)
    structure_hook(book,args,'before',before_catalog)
    # Preparatory edits must never be silently replaced by frozen conversion rows.
    rechecked=prepare(book,args,deadline=book.deadline)
    require(rechecked['review_digest']==prepared['review_digest'],'HOOK_CHANGED_SCHEMA_SOURCE','A required hook changed schema source rows; operation rolled back',409)
    rebuild(book,old,target,prepared['rows'])
    old_columns={col['id']:col for col in old['columns']};new_columns={col['id']:col for col in target['columns']}
    semantic_changes={key for key in set(old_columns)|set(new_columns) if key not in old_columns or key not in new_columns or old_columns[key]['type']!=new_columns[key]['type']}
    for identity,before,after in prepared['rows']:
        if encoded(before)!=encoded(after) or semantic_changes:
            capture(book,target,identity,before,after,schema_changed=bool(semantic_changes),changed_columns=semantic_changes|{key for key in before.keys()&after.keys() if encoded(before[key])!=encoded(after[key])})
    structure_hook(book,args,'after',before_catalog)
    from .charts import break_table
    result=dict(table=target,converted_records=len(prepared['rows']))
    broken_charts=break_table(book,target['id'],set(new_columns))
    if broken_charts:result['broken_charts']=broken_charts
    return result


def rebuild(book,old,target,rows):
    sequence=book.db.execute('SELECT seq FROM sqlite_sequence WHERE name=?',(old['name'],)).fetchone()
    temporary='_dh_rebuild_'+uuid.uuid4().hex
    from .relationships import relationships
    registered=[target if item['id']==target['id'] else item for item in tables(book.db)]
    book.db.execute(table_sql({**target,'name':temporary},relationships(book.db),registered))
    sql='INSERT INTO '+quote(temporary)+' ("_dh_row_id",'+','.join(quote(col['name']) for col in target['columns'])+') VALUES ('+','.join('?' for _ in range(len(target['columns'])+1))+')'
    for identity,_,after in rows:
        book.check_operation();book.db.execute(sql,[int(identity),*[after[col['id']] for col in target['columns']]])
    book.db.execute('DROP TABLE '+quote(old['name']))
    book.db.execute('ALTER TABLE '+quote(temporary)+' RENAME TO '+quote(target['name']))
    if sequence:
        current=book.db.execute('SELECT seq FROM sqlite_sequence WHERE name=?',(target['name'],)).fetchone()
        if current:book.db.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?',(sequence[0],target['name']))
        else:book.db.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)',(target['name'],sequence[0]))
    store(book,'table',target)
    from .table_indexes import recreate
    recreate(book,target)
