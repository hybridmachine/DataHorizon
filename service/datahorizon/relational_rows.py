# SPDX-License-Identifier: GPL-3.0-only
"""Typed record reads and explicit known-target writes in the parent transaction."""
import re
import time
from .engine import EngineError, encoded, require, integer, sql_deadline
from .relational import table, quote, value, from_sql


def identity(row_id):
    require(isinstance(row_id,str) and re.fullmatch(r'[1-9][0-9]{0,18}',row_id) is not None and int(row_id)<2**63,
            'INVALID_ROW_ID','Record IDs must be positive 64-bit decimal strings')
    return int(row_id)


def fields(item,values):
    require(isinstance(values,dict),'INVALID_ARGUMENT','Record values must be an object keyed by column name or ID')
    mapped={};by_key={key:col for col in item['columns'] for key in (col['id'],col['name'])}
    for key,stored in values.items():
        require(key in by_key,'COLUMN_NOT_FOUND','Record column does not exist')
        col=by_key[key];require(col['id'] not in mapped,'INVALID_ARGUMENT','A column was specified twice')
        mapped[col['id']]=value(col,stored)
    return mapped


def raw(book,item,row_id,columns=None):
    row_id=identity(row_id)
    selected=item['columns']
    if columns is not None:
        require(isinstance(columns,list) and bool(columns) and len(columns)<=256 and all(isinstance(col,str) for col in columns),'INVALID_ARGUMENT','Choose a bounded list of column IDs')
        known={col['id']:col for col in selected};require(all(col in known for col in columns) and len(columns)==len(set(columns)),'COLUMN_NOT_FOUND','Choose unique existing column IDs')
        selected=[known[key] for key in columns]
    row=book.db.execute('SELECT '+','.join(quote(col['name']) for col in selected)+
        ' FROM '+quote(item['name'])+' WHERE "_dh_row_id"=?',(row_id,)).fetchone()
    if row is None:return None
    result={};size=0
    for col,stored in zip(selected,row):
        stored=from_sql(col,stored);value(col,stored);size+=len(encoded(stored).encode())+len(col['id'])+8
        require(size<=4*1024*1024,'LIMIT','A record image exceeds 4 MiB; choose projected reads or smaller records')
        result[col['id']]=stored
    return result


def put(book,item,row_id,values):
    """Replace a full row; None deletes, None identity allocates a new record."""
    book.check_operation()
    before=None if row_id is None else raw(book,item,row_id)
    if values is not None:
        require(isinstance(values,dict) and set(values)=={col['id'] for col in item['columns']},'INVALID_ARGUMENT','A complete row image is required')
        for col in item['columns']:value(col,values[col['id']])
    if before==values:return row_id
    changed={col['id'] for col in item['columns'] if before is None or values is None or before[col['id']]!=values[col['id']] or type(before[col['id']]) is not type(values[col['id']])}
    book.change_bytes+=len(encoded([before,values]).encode())
    require(book.change_bytes<=8*1024*1024,'LIMIT','Operation before/after history exceeds 8 MiB')
    # Preflight even a not-yet-allocated identity before its INSERT.
    new_fields=len(changed) if row_id is None else len({(item['id'],row_id,col) for col in changed}-book.table_field_changes)
    require(len(book.changes)-len(book.table_record_keys)+len(book.table_field_changes)+new_fields<=10000,
            'LIMIT','Operation including generated edits exceeds 10,000 scalar fields')
    names=[col['name'] for col in item['columns']]
    stored=None if values is None else [int(values[col['id']]) if col['type']=='boolean' and values[col['id']] is not None else values[col['id']] for col in item['columns']]
    if values is None:
        book.db.execute('DELETE FROM '+quote(item['name'])+' WHERE "_dh_row_id"=?',(identity(row_id),))
    elif row_id is None:
        sequence=book.db.execute('SELECT seq FROM sqlite_sequence WHERE name=?',(item['name'],)).fetchone()
        maximum=book.db.execute('SELECT coalesce(max("_dh_row_id"),0) FROM '+quote(item['name'])).fetchone()[0]
        require(sequence is None or type(sequence[0]) is int,'INVALID_FORMAT','Invalid native identity high-water mark',409)
        require(max(maximum,sequence[0] if sequence else 0)<2**63-1,'ROW_ID_LIMIT','Record identity limit reached; no new record was inserted',409)
        row_id=str(book.db.execute('INSERT INTO '+quote(item['name'])+' ('+','.join(map(quote,names))+') VALUES('+','.join('?' for _ in names)+')',stored).lastrowid)
    elif before is None:
        book.db.execute('INSERT INTO '+quote(item['name'])+' ("_dh_row_id",'+','.join(map(quote,names))+') VALUES('+','.join('?' for _ in range(len(names)+1))+')',[identity(row_id),*stored])
    else:
        book.db.execute('UPDATE '+quote(item['name'])+' SET '+','.join(quote(name)+'=?' for name in names)+' WHERE "_dh_row_id"=?',[*stored,identity(row_id)])
    after=None if values is None else raw(book,item,row_id)
    key=('table',item['id'],row_id);book.table_record_keys.add(key)
    book.table_field_changes.update((item['id'],row_id,col) for col in changed)
    require(len(book.table_record_keys)<=10000,'LIMIT','An operation affects at most 10,000 records')
    original=book.changes.get(key,[before,after])[0];book.changes[key]=[original,after]
    pending_original=book.pending_tables.get(key,{}).get('before',before)
    schema_changed=book.pending_tables.get(key,{}).get('schema_changed',False)
    if pending_original==after and not schema_changed:book.pending_tables.pop(key,None)
    else:book.pending_tables[key]=dict(table=item['id'],row_id=row_id,before=pending_original,after=after,schema_changed=schema_changed)
    return row_id


def insert(book,item,values):
    supplied=fields(item,values)
    return put(book,item,None,{col['id']:supplied.get(col['id']) for col in item['columns']})


def dispatch(book,method,args):
    if method=='table_schema' and args.get('phase','current')=='before':
        identifier=args.get('table');require(isinstance(identifier,str),'INVALID_ARGUMENT','A table name or ID is required')
        item=next((item for item in book.relational_before.values() if item['id']==identifier or item['name'].lower()==identifier.lower()),None)
        require(item is not None,'TABLE_NOT_FOUND','Table did not exist before this operation',404);return item
    if method=='table_schema':require(args.get('phase','current')=='current','INVALID_ARGUMENT','Schema phase is current or before')
    item=table(book.db,args.get('table'))
    if method=='table_schema':return item
    if method=='table_get':return dict(row_id=args.get('row_id'),values=raw(book,item,args.get('row_id'),args.get('columns')))
    if method=='table_insert':return dict(row_id=insert(book,item,args.get('values')))
    row_id=args.get('row_id');current=raw(book,item,row_id)
    require(current is not None,'RECORD_NOT_FOUND','Record does not exist',404)
    if method=='table_set':return dict(row_id=put(book,item,row_id,{**current,**fields(item,args.get('values'))}))
    require(method=='table_delete','INVALID_HOOK','Unknown table broker method')
    put(book,item,row_id,None)


def read(book,args):
    from .table_browse import read as browse
    return browse(book,args)
