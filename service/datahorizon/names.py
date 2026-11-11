# SPDX-License-Identifier: GPL-3.0-only
"""Workbook-scoped names for cells and rectangles, stored as native v6 catalog objects."""
import re
import uuid
from .engine import EngineError,require,valid_uuid
from . import relational
from .formula import MAX_ROWS,MAX_COLS,adjust_interval,ADDRESS,FormulaError,split_ref,column_name
from .functions import FUNCTIONS
from .hooks import invoke,supports_event

MAX_NAMES=256
IDENTIFIER=re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
FIELDS={'id','name','sheet_id','row1','col1','row2','col2'}
SPAN=('row1','col1','row2','col2')
# R1C1-notation cell, row and column references; rejected for new or renamed names only, so stored names still open.
R1C1=re.compile(r'(?:[Rr][0-9]+[Cc][0-9]+|[RrCc][0-9]+)\Z')


def valid_name(text):
    if not isinstance(text,str) or IDENTIFIER.match(text) is None:return False
    upper=text.upper()
    return ADDRESS.fullmatch(text) is None and upper not in ('TRUE','FALSE') and upper not in FUNCTIONS


def definition(value):
    require(isinstance(value,dict) and set(value)==FIELDS,'INVALID_FORMAT','Invalid stored name fields',409)
    require(valid_uuid(value['id']) and str(uuid.UUID(value['id']))==value['id'],'INVALID_FORMAT','Stored name identity must be a canonical UUID',409)
    require(valid_name(value['name']),'INVALID_FORMAT','Invalid stored name identifier',409)
    broken=value['sheet_id'] is None or value['row1'] is None
    if broken:
        require(all(value[key] is None for key in SPAN),'INVALID_FORMAT','A broken name has no coordinates',409)
        require(value['sheet_id'] is None or valid_uuid(value['sheet_id']),'INVALID_FORMAT','Invalid stored name target',409)
    else:
        require(valid_uuid(value['sheet_id']) and all(type(value[key]) is int for key in SPAN),'INVALID_FORMAT','Invalid stored name target',409)
        from .formula import MAX_ROWS,MAX_COLS
        require(1<=value['row1']<=value['row2']<=MAX_ROWS and 1<=value['col1']<=value['col2']<=MAX_COLS,'INVALID_FORMAT','Stored name target is outside the grid',409)
    return value


def names(db):
    result=[value for _,value in relational.objects(db,'name')]
    require(len(result)<=MAX_NAMES,'NAME_LIMIT','Workbook names exceed 256',409)
    seen=set()
    for value in result:
        definition(value);key=value['name'].lower()
        require(key not in seen,'INVALID_FORMAT','Duplicate stored names',409);seen.add(key)
    return result


def find(db,key):
    if not isinstance(key,str):return None
    items=names(db)
    return next((item for item in items if item['id']==key),None) or next((item for item in items if item['name'].lower()==key.lower()),None)


def quote_sheet(sheet):
    return "'"+sheet.replace("'","''")+"'"


def describe(db,item):
    sheet=None
    if item['sheet_id'] is not None:
        row=db.execute('SELECT name FROM sheets WHERE id=?',(item['sheet_id'],)).fetchone()
        sheet=row[0] if row else None
    result=dict(item,sheet=sheet,reference=None,broken=sheet is None or item['row1'] is None)
    if not result['broken']:
        first=column_name(item['col1'])+str(item['row1']);last=column_name(item['col2'])+str(item['row2'])
        result['reference']=quote_sheet(sheet)+'!'+(first if first==last else first+':'+last)
    return result


def parse_reference(book,reference,*,missing_sheet=('REFERENCE_INVALID',400)):
    require(isinstance(reference,str) and '!' in reference,'REFERENCE_INVALID','Reference requires a sheet-qualified cell or range such as Sheet1!A1:B2')
    sheet_text,_,span=reference.rpartition('!')
    require(sheet_text!='' and span!='','REFERENCE_INVALID','Reference requires a sheet and an address')
    if sheet_text.startswith("'"):
        require(len(sheet_text)>=3 and sheet_text.endswith("'") and re.fullmatch(r"(?:[^']|'')+",sheet_text[1:-1]) is not None,'REFERENCE_INVALID','Malformed quoted sheet name')
        sheet_text=sheet_text[1:-1].replace("''","'")
    parts=span.split(':');require(len(parts)<=2,'REFERENCE_INVALID','Reference must be one cell or one rectangle')
    try:
        corners=[split_ref(part) for part in parts]
    except FormulaError as error:raise EngineError('REFERENCE_INVALID','Reference is not a valid A1 address within the grid') from error
    require(all(corner[0] is None for corner in corners),'REFERENCE_INVALID','Qualify the whole reference with one sheet')
    row=sorted(corner[1] for corner in corners*(2 if len(corners)==1 else 1));col=sorted(corner[2] for corner in corners*(2 if len(corners)==1 else 1))
    row1,row2,col1,col2=row[0],row[-1],col[0],col[-1]
    found=book.db.execute('SELECT id FROM sheets WHERE id=? OR name=? COLLATE NOCASE',(sheet_text,sheet_text)).fetchone()
    require(found is not None,missing_sheet[0],'Reference names a sheet that does not exist',missing_sheet[1])
    return dict(sheet_id=found[0],row1=row1,col1=col1,row2=row2,col2=col2)


def read(book,args):
    require(not set(args)-{'expected_state','name'},'INVALID_ARGUMENT','Unsupported name inspection field')
    with book.lock:
        book.refresh();book.db.execute('BEGIN')
        try:
            book.refresh();require(args.get('expected_state')==book.state(),'STALE_STATE','Refresh names before inspection',409)
            items=names(book.db)
            if 'name' in args:
                found=find(book.db,args['name']);require(found is not None,'NAME_NOT_FOUND','Name does not exist',404);items=[found]
            return dict(state=book.state(),names=[describe(book.db,item) for item in items])
        finally:book.db.execute('ROLLBACK')


def mutate(book,request):
    require(not set(request)-{'action','sheet','operation_id','expected_state','label','name_id','name','reference'},'INVALID_ARGUMENT','Unsupported name operation field')
    action=request['action'];current=names(book.db)
    before=None
    if 'name_id' in request:
        before=next((item for item in current if item['id']==request['name_id']),None) if isinstance(request['name_id'],str) else None
        require(before is not None,'NAME_NOT_FOUND','Name does not exist',404)
    after=None
    if action=='name_save':
        require(valid_name(request.get('name')),'NAME_INVALID','Names are identifiers of at most 64 letters, digits and underscores that are not cell addresses, TRUE/FALSE or function names')
        require(before is not None and before['name']==request['name'] or R1C1.match(request['name']) is None,'NAME_INVALID','Names cannot look like R1C1-style references such as R1C1, R2 or C3')
        if 'reference' in request:target=parse_reference(book,request['reference'])
        else:
            require(before is not None,'REFERENCE_INVALID','A new name requires a reference')
            require(before['row1'] is not None,'NAME_BROKEN','A broken name needs a new reference',409)
            target={key:before[key] for key in ('sheet_id',*SPAN)}
        require(all(item['id']==(before or {}).get('id') or item['name'].lower()!=request['name'].lower() for item in current),'NAME_DUPLICATE','A name with that identifier already exists',409)
        require(before is not None or len(current)<MAX_NAMES,'NAME_LIMIT','A workbook holds at most 256 names',409)
        after=definition(dict(id=before['id'] if before else str(uuid.uuid4()),name=request['name'],**target))
    else:
        require(before is not None,'NAME_NOT_FOUND','Choose the name to delete',404)
    if book.handler:require(supports_event(book.handler,'on_name_structure'),'UNSUPPORTED_HOOK_SCOPE','Required handler must explicitly define on_name_structure(ctx,event)')
    def event(phase):
        if book.handler:invoke(book.handler,dict(phase=phase,action=action,operation_id=request['operation_id'],before=before,after=after),request['operation_id'],book.hook_dispatch,book.deadline,event='on_name_structure')
    event('before')
    relational.ensure_version(book,6)
    if after:relational.store(book,'name',after)
    else:book.db.execute("DELETE FROM dh_relational WHERE kind='name' AND id=?",(before['id'],))
    names(book.db);event('after')
    result=dict(name_id=(after or before)['id'])
    if after is None:
        from .bindings import scoped
        broken=scoped(book,'name',[before['id']])
        if broken:result['broken_bindings']=broken
    return result


def adjust_structure(book,sheet_id,axis,start,count,delete):
    """Move, expand, contract or break stored names after a whole-row/column edit."""
    if book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]<6:return []
    keys=('row1','row2') if axis=='row' else ('col1','col2');broken=[]
    for item in names(book.db):
        if item['sheet_id']!=sheet_id or item['row1'] is None:continue
        moved=adjust_interval(item[keys[0]],item[keys[1]],start,count,delete,MAX_ROWS if axis=='row' else MAX_COLS)
        if moved is None:
            item=dict(item,row1=None,col1=None,row2=None,col2=None);broken.append(item['name'])
        else:item=dict(item,**{keys[0]:moved[0],keys[1]:moved[1]})
        relational.store(book,'name',definition(item))
    return sorted(broken)


def break_sheet(book,sheet_id):
    """Break every name that targets a sheet that is being deleted."""
    if book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]<6:return []
    broken=[]
    for item in names(book.db):
        if item['sheet_id']!=sheet_id:continue
        if item['row1'] is not None:broken.append(item['name'])
        relational.store(book,'name',definition(dict(item,sheet_id=None,row1=None,col1=None,row2=None,col2=None)))
    return sorted(broken)
