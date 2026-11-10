# SPDX-License-Identifier: GPL-3.0-only
"""Registered native indexes; schema changes remain atomic managed operations."""
import uuid
from .engine import require, boolean, sql_deadline
from .relational import objects, table, tables, quote, name, store, structure_hook

ACTIONS={'index_create','index_drop'}


def indexes(db):return [item for _,item in objects(db,'index')]


def validate(item,registered):
    require(set(item)=={'id','name','table','columns','unique','owner'},'INVALID_FORMAT','Invalid index metadata')
    name(item['name']);boolean(item['unique'],'unique')
    target=next((t for t in registered if t['id']==item['table']),None);require(target is not None,'TABLE_NOT_FOUND','Index table is missing')
    keys={col['id'] for col in target['columns']};entries=item['columns'];seen=set()
    require(isinstance(entries,list) and 1<=len(entries)<=16,'INVALID_SCHEMA','Choose 1–16 index columns')
    for entry in entries:
        require(isinstance(entry,dict) and set(entry)=={'column','direction'} and isinstance(entry['column'],str) and entry['column'] in keys and entry['column'] not in seen and entry['direction'] in ('asc','desc'),'INVALID_SCHEMA','Choose unique existing index columns and asc/desc order');seen.add(entry['column'])
    require(item['owner']=='user' or isinstance(item['owner'],str) and any(r['id']==item['owner'] for r in []),'INDEX_OWNED','Unsupported index owner')
    return target


def sql(item,target):
    by_id={col['id']:col for col in target['columns']}
    return 'CREATE '+('UNIQUE ' if item['unique'] else '')+'INDEX '+quote(item['name'])+' ON '+quote(target['name'])+' ('+', '.join(quote(by_id[entry['column']]['name'])+' '+entry['direction'].upper() for entry in item['columns'])+')'


def dependencies(book,target,proposed):
    after={col['id'] for col in proposed['columns']}
    for index in indexes(book.db):
        if index['table']==target['id']:require(all(entry['column'] in after for entry in index['columns']),'DEPENDENCY_BLOCKED','Remove dependent index '+index['name']+' before removing its column',409)


def recreate(book,target):
    for index in indexes(book.db):
        if index['table']==target['id']:book.db.execute(sql(index,target))


def mutate(book,args):
    before=tables(book.db);structure_hook(book,args,'before',before)
    if args['action']=='index_create':
        target=table(book.db,args.get('table'));item=dict(id=str(uuid.uuid4()),name=name(args.get('name')),table=target['id'],columns=args.get('columns'),unique=boolean(args.get('unique',False),'unique'),owner='user')
        validate(item,before);existing=indexes(book.db)
        require(len(existing)<128,'LIMIT','At most 128 indexes');require(not any(i['name'].lower()==item['name'].lower() for i in [*existing,*before]),'INDEX_EXISTS','SQL object name already exists',409)
        with sql_deadline(book.db,book.deadline,cancel_event=book.cancel_event):book.db.execute(sql(item,target))
        store(book,'index',item);result=dict(index=item)
    else:
        item=next((i for i in indexes(book.db) if i['id']==args.get('index') or i['name']==args.get('index')),None)
        require(item is not None,'INDEX_NOT_FOUND','Index does not exist',404);require(item['owner']=='user','INDEX_OWNED','Remove the owning constraint or relationship instead',409)
        book.db.execute('DROP INDEX '+quote(item['name']));book.db.execute("DELETE FROM dh_relational WHERE kind='index' AND id=?",(item['id'],));result={}
    structure_hook(book,args,'after',before);return result
