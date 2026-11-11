# SPDX-License-Identifier: GPL-3.0-only
"""Workbook-scoped names for cells and rectangles, stored as native v6 catalog objects."""
import re
import uuid
from .engine import EngineError,require,valid_uuid
from . import relational
from .formula import ADDRESS,FormulaError,split_ref,column_name
from .functions import FUNCTIONS
from .hooks import invoke,supports_event

MAX_NAMES=256
IDENTIFIER=re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
FIELDS={'id','name','sheet_id','row1','col1','row2','col2'}
SPAN=('row1','col1','row2','col2')


def valid_name(text):
    if not isinstance(text,str) or IDENTIFIER.match(text) is None:return False
    upper=text.upper()
    return ADDRESS.fullmatch(text) is None and upper not in ('TRUE','FALSE') and upper not in FUNCTIONS


def definition(value):
    require(isinstance(value,dict) and set(value)==FIELDS,'INVALID_FORMAT','Invalid stored name fields',409)
    require(valid_uuid(value['id']) and str(uuid.UUID(value['id']))==value['id'],'INVALID_FORMAT','Stored name identity must be a canonical UUID',409)
    require(valid_name(value['name']),'INVALID_FORMAT','Invalid stored name identifier',409)
    broken=value['sheet_id'] is None
    if broken:
        require(all(value[key] is None for key in SPAN),'INVALID_FORMAT','A broken name has no coordinates',409)
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
    result=dict(item,sheet=sheet,reference=None,broken=sheet is None)
    if sheet is not None:
        first=column_name(item['col1'])+str(item['row1']);last=column_name(item['col2'])+str(item['row2'])
        result['reference']=quote_sheet(sheet)+'!'+(first if first==last else first+':'+last)
    return result


def parse_reference(book,reference):
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
    require(found is not None,'REFERENCE_INVALID','Reference names a sheet that does not exist')
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
        if 'reference' in request:target=parse_reference(book,request['reference'])
        else:
            require(before is not None,'REFERENCE_INVALID','A new name requires a reference')
            require(before['sheet_id'] is not None,'NAME_BROKEN','A broken name needs a new reference',409)
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
    return dict(name_id=(after or before)['id'])
