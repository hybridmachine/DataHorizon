"""Persistent declared-list identities; A1 coordinates remain positional."""
import hashlib
import json
import time
import uuid
from .engine import require,integer,encoded,valid_uuid,EngineError
from .metadata import list_spec,name


def validate(payload):
    require(isinstance(payload,dict) and set(payload)=={'spec','rows','fingerprint'},'INVALID_FORMAT','Invalid record declaration')
    spec=list_spec(payload['spec']);rows=payload['rows'];first=spec['r1']+int(spec['header'])
    require(isinstance(rows,list) and len(rows)==max(0,spec['r2']-first+1),'INVALID_FORMAT','Record extent mismatch')
    seen=set()
    for row,item in enumerate(rows,first):
        require(isinstance(item,dict) and set(item)=={'id','row'} and type(item['row']) is int and item['row']==row and valid_uuid(item['id']) and item['id'] not in seen,'INVALID_FORMAT','Invalid or duplicate record identity');seen.add(item['id'])
    require(isinstance(payload['fingerprint'],str) and len(payload['fingerprint'])==64 and all(c in '0123456789abcdef' for c in payload['fingerprint']),'INVALID_FORMAT','Invalid record fingerprint')
    return payload


def objects(book):
    if book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]<3:return []
    return [(sheet,identifier,validate(json.loads(payload))) for sheet,identifier,payload in book.db.execute("SELECT sheet_id,id,payload FROM dh_objects WHERE namespace='records' ORDER BY sheet_id,id")]


def digest(book,sheet,spec,deadline=None):
    deadline=time.monotonic()+2 if deadline is None else deadline
    value=hashlib.sha256();size=0
    for row,col,source,kind in book.db.execute('SELECT row,col,source,kind FROM cells WHERE sheet_id=? AND row BETWEEN ? AND ? AND col BETWEEN ? AND ? ORDER BY row,col',(sheet,spec['r1'],spec['r2'],spec['c1'],spec['c2'])):
        chunk=encoded([row,col,source,kind]).encode();size+=len(chunk)
        require(size<=8*1024*1024 and time.monotonic()<=deadline,'LIMIT','Record verification exceeded its input/time budget');value.update(chunk);value.update(b'\n')
    return value.hexdigest()


def capture(book):
    return {(sheet,identifier):(payload,payload['fingerprint']==digest(book,sheet,payload['spec'],book.deadline) and (sheet,identifier) not in book.unverified_records) for sheet,identifier,payload in objects(book)}


def store(book,sheet,identifier,payload):
    validate(payload);require(len(encoded(payload).encode())<=1024*1024,'LIMIT','Record declaration exceeds 1 MiB')
    book.ensure_metadata();book.db.execute("INSERT INTO dh_objects VALUES('records',?,?,?) ON CONFLICT(namespace,sheet_id,id) DO UPDATE SET payload=excluded.payload",(identifier,sheet,encoded(payload)))
    require(book.db.execute('SELECT count(*)<=256 AND coalesce(sum(length(payload)),0)<=16777216 FROM dh_objects').fetchone()[0],'LIMIT','Native metadata exceeds its budget')


def mutate(book,request):
    sheet=book.sheet(request.get('sheet','Sheet1'));identifier=name(request.get('id'));key=(sheet,identifier);action=request['action']
    existing=next((payload for s,i,payload in objects(book) if (s,i)==key),None)
    if action=='list_declare':
        require(existing is None,'LIST_EXISTS','Declared list already exists',409);spec=list_spec(request.get('spec'))
        all_objects=objects(book);require(len(all_objects)<16,'LIMIT','At most 16 declared lists')
        extent=(spec['r2']-spec['r1']+1)*(spec['c2']-spec['c1']+1)
        require(extent+sum((p['spec']['r2']-p['spec']['r1']+1)*(p['spec']['c2']-p['spec']['c1']+1) for _,_,p in all_objects)<=10000,'LIMIT','Declared list scopes total at most 10,000 cells')
        rows=[dict(id=str(uuid.uuid4()),row=row) for row in range(spec['r1']+int(spec['header']),spec['r2']+1)]
    else:
        require(existing is not None,'LIST_NOT_FOUND','Declare this list first',404)
        if action=='list_forget':book.db.execute("DELETE FROM dh_objects WHERE namespace='records' AND sheet_id=? AND id=?",key);book.unverified_records.discard(key);return
        require(request.get('mode') in ('retain_positions','regenerate'),'INVALID_ARGUMENT','Reconciliation requires retain_positions or regenerate')
        spec=existing['spec'];rows=existing['rows']
        if request['mode']=='regenerate':rows=[dict(item,id=str(uuid.uuid4())) for item in rows]
    store(book,sheet,identifier,dict(spec=spec,rows=rows,fingerprint=digest(book,sheet,spec)));book.unverified_records.discard(key)


def finish(book,request,context):
    action=request['action'];target=book.sheet(request.get('sheet','Sheet1')) if action in ('rows_insert','rows_delete','columns_insert','columns_delete') else None
    for (sheet,identifier),(payload,verified) in context.items():
        if not verified or action in ('list_reconcile','list_forget'):continue
        if book.db.execute("SELECT 1 FROM dh_objects WHERE namespace='records' AND sheet_id=? AND id=?",(sheet,identifier)).fetchone() is None:continue
        spec=dict(payload['spec']);rows=[dict(row) for row in payload['rows']]
        if sheet==target:
            axis='r' if action.startswith('rows_') else 'c';start=request['start'];count=request.get('count',1);end=start+count-1;low,high=spec[axis+'1'],spec[axis+'2'];delete=action.endswith('delete')
            if not delete:
                if start<=low:spec[axis+'1']+=count;spec[axis+'2']+=count
                elif start<=high:spec[axis+'2']+=count
                if axis=='r':
                    for item in rows:
                        if item['row']>=start:item['row']+=count
                    present={item['row']:item for item in rows};rows=[present.get(row,dict(id=str(uuid.uuid4()),row=row)) for row in range(spec['r1']+int(spec['header']),spec['r2']+1)]
                else:
                    for key in ('sort','filters'):spec[key]=[{**item,'col':item['col']+count if item['col']>=start else item['col']} for item in spec[key]]
            else:
                if start<=low and end>=high:book.db.execute("DELETE FROM dh_objects WHERE namespace='records' AND sheet_id=? AND id=?",(sheet,identifier));continue
                if axis=='r':require(not(spec['header'] and start<=low<=end),'LIST_HEADER_CHANGE','Forget/redeclare the list before deleting its header')
                else:require(not any(start<=item['col']<=end for key in ('sort','filters') for item in spec[key]),'LIST_COLUMN_CHANGE','Remove criteria before deleting their source column')
                survivors=[value for value in (low,high,start-1,end+1) if low<=value<=high and not start<=value<=end]
                spec[axis+'1']=min(survivors)-(count if min(survivors)>end else 0);spec[axis+'2']=max(survivors)-(count if max(survivors)>end else 0)
                if axis=='r':rows=[dict(item,row=item['row']-count if item['row']>end else item['row']) for item in rows if not start<=item['row']<=end]
                else:
                    for key in ('sort','filters'):spec[key]=[{**item,'col':item['col']-count if item['col']>end else item['col']} for item in spec[key]]
        store(book,sheet,identifier,dict(spec=spec,rows=rows,fingerprint=digest(book,sheet,spec)))

    require(sum((p['spec']['r2']-p['spec']['r1']+1)*(p['spec']['c2']-p['spec']['c1']+1) for _,_,p in objects(book))<=10000,'LIMIT','Declared list scopes exceed 10,000 cells')


def read(book,request):
    from .record_operations import review
    return review(book,request)
