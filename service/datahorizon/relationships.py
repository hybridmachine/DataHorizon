# SPDX-License-Identifier: GPL-3.0-only
"""Native RESTRICT relationships and explicit, reviewed orphan repair."""
import hashlib
import time
import uuid
from .engine import encoded,require,sql_deadline
from .relational import objects,tables,table,quote,name,store,structure_hook

ACTIONS={'relationship_create','relationship_drop','table_repair'}


def relationships(db):return [item for _,item in objects(db,'relationship')]


def definition(item,registered):
    require(set(item)=={'id','name','parent_table','parent_column','child_table','child_column','on_delete','on_update'},'INVALID_RELATIONSHIP','Invalid relationship fields')
    name(item['name']);known={t['id']:t for t in registered}
    require(isinstance(item['parent_table'],str) and isinstance(item['child_table'],str) and item['parent_table'] in known and item['child_table'] in known,'TABLE_NOT_FOUND','Relationship table is missing')
    require(item['parent_table']!=item['child_table'],'UNSUPPORTED_RELATIONSHIP','Self relationships are outside this initial rebuild tier')
    parent=known[item['parent_table']];child=known[item['child_table']]
    p=next((c for c in parent['columns'] if c['id']==item['parent_column']),None);c=next((c for c in child['columns'] if c['id']==item['child_column']),None)
    require(p is not None and c is not None,'COLUMN_NOT_FOUND','Relationship column is missing')
    require(p['required'] and p['unique'],'INVALID_RELATIONSHIP','Parent key must be a required UNIQUE column')
    require(p['type']==c['type'],'INVALID_RELATIONSHIP','Parent and child column types must match exactly')
    require(item['on_delete']=='restrict' and item['on_update']=='restrict','UNSUPPORTED_RELATIONSHIP','Only RESTRICT updates and deletes are supported')
    return parent,p,child,c


def validate(items,registered):
    require(len(items)<=64,'LIMIT','At most 64 relationships');names=set();children=set();graph={}
    for item in items:
        definition(item,registered);require(item['name'].lower() not in names and (item['child_table'],item['child_column']) not in children,'INVALID_RELATIONSHIP','Duplicate relationship name or child key')
        names.add(item['name'].lower());children.add((item['child_table'],item['child_column']));graph.setdefault(item['child_table'],[]).append(item['parent_table'])
    visited=set()
    def visit(node,path):
        require(node not in path,'UNSUPPORTED_RELATIONSHIP','Cyclic relationship graphs are outside this initial rebuild tier')
        if node in visited:return
        for parent in graph.get(node,[]):visit(parent,{*path,node})
        visited.add(node)
    for node in graph:visit(node,set())


def foreign_keys(item,items,registered):
    fields=[]
    for rel in items:
        if rel['child_table']!=item['id']:continue
        parent,p,_,c=definition(rel,registered)
        fields.append('CONSTRAINT '+quote(rel['name'])+' FOREIGN KEY ('+quote(c['name'])+') REFERENCES '+quote(parent['name'])+' ('+quote(p['name'])+') ON DELETE RESTRICT ON UPDATE RESTRICT')
    return fields


def dependent_rebuild(db,identity):
    require(not any(r['parent_table']==identity for r in relationships(db)),'DEPENDENCY_BLOCKED','Remove incoming relationships before rebuilding or renaming this parent table',409)


def schema_dependencies(book,old,target):
    dependent_rebuild(book.db,old['id']);registered=[target if t['id']==target['id'] else t for t in tables(book.db)]
    validate(relationships(book.db),registered)


def orphans(db,rel,registered,limit=101):
    parent,p,child,c=definition(rel,registered)
    sql='SELECT c."_dh_row_id",c.'+quote(c['name'])+' FROM '+quote(child['name'])+' c WHERE c.'+quote(c['name'])+' IS NOT NULL AND NOT EXISTS (SELECT 1 FROM '+quote(parent['name'])+' p WHERE p.'+quote(p['name'])+'=c.'+quote(c['name'])+') ORDER BY c."_dh_row_id" LIMIT ?'
    return db.execute(sql,(limit,))


def issues(db,registered,deadline,*,repair=False):
    found=[]
    for rel in relationships(db):
        for row_id,key in orphans(db,rel,registered):
            require(time.monotonic()<=deadline,'TIMEOUT','Relationship validation exceeded its budget')
            require(repair,'DATA_REPAIR_REQUIRED','Stored relationship orphans require explicit repair: '+rel['name'],409)
            if len(found)<100:found.append(dict(relationship=rel['id'],table=rel['child_table'],row_id=str(row_id),code='FOREIGN_KEY_ORPHAN',message='Child key has no matching parent'))
    return found


def inspect(book,args):
    with book.lock:
        deadline=time.monotonic()+2;book.refresh(deadline=deadline);book.db.execute('BEGIN')
        try:
            with sql_deadline(book.db,deadline):
                book.refresh(deadline=deadline);require(args.get('expected_state')==book.state(),'STALE_STATE','Refresh the relationship inspector',409)
                registered=tables(book.db);rows=[];more=False
                for rel in relationships(book.db):
                    for row_id,key in orphans(book.db,rel,registered):
                        if len(rows)>=100:more=True;break
                        display=str(key)[:256];rows.append(dict(relationship=rel['id'],table=rel['child_table'],column=rel['child_column'],row_id=str(row_id),key_preview=display,key_omitted=len(str(key))>256))
                return dict(state=book.state(),relationships=relationships(book.db),orphans=rows,has_more=more,repair=book.repair,semantic_issues=book.relational_issues)
        finally:book.db.execute('ROLLBACK')


def bounded_rows(book,item):
    from .relational_rows import raw
    result=[];size=0;limit=10000//len(item['columns'])
    for (row_id,) in book.db.execute('SELECT "_dh_row_id" FROM '+quote(item['name'])+' ORDER BY "_dh_row_id" LIMIT ?',(limit+1,)):
        require(len(result)<limit,'LIMIT','Relationship rebuild exceeds 10,000 scalar fields');book.check_operation();values=raw(book,item,str(row_id));size+=len(encoded(values).encode())
        require(size<=4*1024*1024,'LIMIT','Relationship rebuild exceeds 4 MiB row images');result.append((str(row_id),values,values))
    return result


def mutate(book,args):
    if args['action']=='table_repair':return apply_repair(book,args)
    registered=tables(book.db);before=registered;structure_hook(book,args,'before',before)
    from .table_indexes import indexes
    from .table_schema import rebuild
    if args['action']=='relationship_create':
        parent=table(book.db,args.get('parent_table'));child=table(book.db,args.get('child_table'))
        def column(item,key):return next((c['id'] for c in item['columns'] if key in (c['id'],c['name'])),key)
        rel=dict(id=str(uuid.uuid4()),name=name(args.get('name')),parent_table=parent['id'],parent_column=column(parent,args.get('parent_column')),
            child_table=child['id'],child_column=column(child,args.get('child_column')),on_delete=args.get('on_delete','restrict'),on_update=args.get('on_update','restrict'))
        dependent_rebuild(book.db,child['id']);validate([*relationships(book.db),rel],registered)
        require(next(orphans(book.db,rel,registered,1),None) is None,'FOREIGN_KEY_ORPHAN','Correct orphan child keys before creating this relationship',409)
        rows=bounded_rows(book,child);store(book,'relationship',rel)
        index=dict(id=str(uuid.uuid4()),name='Rel'+uuid.UUID(rel['id']).hex,table=child['id'],columns=[dict(column=rel['child_column'],direction='asc')],unique=False,owner=rel['id'])
        require(len(indexes(book.db))<128,'LIMIT','At most 128 indexes');store(book,'index',index);rebuild(book,child,child,rows);result=dict(relationship=rel)
    else:
        rel=next((r for r in relationships(book.db) if args.get('relationship') in (r['id'],r['name'])),None);require(rel is not None,'RELATIONSHIP_NOT_FOUND','Relationship does not exist',404)
        child=table(book.db,rel['child_table']);dependent_rebuild(book.db,child['id']);rows=bounded_rows(book,child)
        for index in indexes(book.db):
            if index['owner']==rel['id']:book.db.execute("DELETE FROM dh_relational WHERE kind='index' AND id=?",(index['id'],))
        book.db.execute("DELETE FROM dh_relational WHERE kind='relationship' AND id=?",(rel['id'],));rebuild(book,child,child,rows);result={}
    structure_hook(book,args,'after',before);return result


def repair_plan(book,args):
    from .table_edits import plan
    require(book.repair and not book.readonly,'REPAIR_HANDLE_REQUIRED','Open explicitly for relationship repair',409)
    rel=next((r for r in relationships(book.db) if args.get('relationship') in (r['id'],r['name'])),None);require(rel is not None,'RELATIONSHIP_NOT_FOUND','Relationship does not exist',404)
    mode=args.get('mode');require(mode in ('update','delete'),'INVALID_ARGUMENT','Repair mode is update or delete')
    item,operation=plan(book,dict(action='table_'+mode,table=rel['child_table'],edits=args.get('edits'),record_ids=args.get('record_ids')))
    targets={edit['row_id'] for edit in operation['edits']} if mode=='update' else set(operation['record_ids'])
    invalid={str(row[0]) for row in orphans(book.db,rel,tables(book.db),10001)}
    require(targets and targets<=invalid,'REPAIR_SCOPE','Repair targets must be current orphans of the selected relationship',409)
    if mode=='update':require(all(set(edit['values'])=={rel['child_column']} for edit in operation['edits']),'REPAIR_SCOPE','Repair updates only the orphan child-key column',409)
    canonical=dict(action='table_repair',relationship=rel['id'],mode=mode,**{k:v for k,v in operation.items() if k in ('edits','record_ids')})
    digest=hashlib.sha256(encoded(dict(state=book.state(),relationship=rel,operation=canonical)).encode()).hexdigest()
    return rel,operation,canonical,digest


def preview_repair(book,args):
    from .table_edits import preview
    with book.lock:
        require(book.repair,'REPAIR_HANDLE_REQUIRED','Open explicitly for relationship repair',409)
        rel=next((r for r in relationships(book.db) if args.get('relationship') in (r['id'],r['name'])),None);require(rel is not None,'RELATIONSHIP_NOT_FOUND','Relationship does not exist',404)
        mode=args.get('mode');require(mode in ('update','delete'),'INVALID_ARGUMENT','Repair mode is update or delete')
        result=preview(book,{**args,'action':'table_'+mode,'table':rel['child_table']})
        if not result['can_apply']:return result
        book.refresh();require(result['state']==book.state(),'STALE_STATE','Review changed repair data',409)
        _,_,canonical,digest=repair_plan(book,{**result['operation'],'mode':mode,'relationship':rel['id']})
        return {**result,'operation':canonical,'review_digest':digest,'report':['Only reviewed orphan child-key updates/deletions are admitted. All relationship violations must be resolved after required hooks before commit. Repair clears session history.']}


def apply_repair(book,args):
    from .table_edits import mutate as edit
    rel,operation,canonical,digest=repair_plan(book,args);require(args.get('review_digest')==digest,'STALE_REVIEW','Review these exact orphan repairs again',409)
    return edit(book,operation)


def finish_repair(book):
    registered=tables(book.db)
    require(not issues(book.db,registered,book.deadline,repair=True),'REPAIR_INCOMPLETE','Repair must leave no relationship orphans after required hooks',409)
