# SPDX-License-Identifier: GPL-3.0-only
"""Reviewed, state-bound sheet values become an independent structured table."""
from datetime import datetime
import hashlib
import math
import time
from .engine import EVALUATED, EngineError, encoded, require, rectangle, boolean, typed, sql_deadline
from .formula import FormulaError
from .relational import columns, name, value, tables
from .temporal import Temporal, utc_clock


def convert(column,stored,policy):
    kind=column['type']
    if stored is not None and policy=='invariant':
        if kind=='text' and not isinstance(stored,str):stored='TRUE' if stored is True else 'FALSE' if stored is False else str(stored)
        elif kind in ('integer','number') and isinstance(stored,str):
            from .conversion import convert as convert_value
            cell=convert_value(dict(source=stored,kind='number',locale='invariant'))['cell']
            stored=typed({**cell,'style':{}})['value']
        elif kind=='boolean' and type(stored) is str and stored in ('TRUE','FALSE'):stored=stored=='TRUE'
    if kind=='integer' and type(stored) is float and math.isfinite(stored) and stored.is_integer():stored=int(stored)
    if kind in ('date','time','datetime') and stored is not None:
        try:stored=str(Temporal(stored,kind))
        except (TypeError,ValueError) as error:raise EngineError('INVALID_VALUE',column['name']+' requires canonical ISO '+kind) from error
    return value(column,stored)


def prepare(book,args,*,deadline=None):
    """Caller owns a read/write transaction and the workbook lock."""
    require(args.get('expected_state')==book.state(),'STALE_STATE','Refresh the source selection before creating a table',409)
    r1,c1,r2,c2=rectangle(args);sheet=book.sheet(args.get('sheet','Sheet1'));header=boolean(args.get('header',True),'header')
    require(c2-c1+1<=256,'LIMIT','Tables support at most 256 columns')
    policy=args.get('conversion_policy','strict');require(policy in ('strict','invariant'),'INVALID_ARGUMENT','Choose strict or invariant conversion')
    clock=args.get('preview_clock')
    if clock is None:clock=utc_clock()
    else:
        try:clock=datetime.fromisoformat(clock)
        except (ValueError,TypeError) as error:raise EngineError('INVALID_ARGUMENT','Invalid reviewed calculation clock') from error
        require(clock.tzinfo is None,'INVALID_ARGUMENT','The reviewed UTC calculation clock has no offset')
    deadline=min(deadline or time.monotonic()+2,time.monotonic()+2)
    calc=book.calculator(seconds=max(0.001,deadline-time.monotonic()),clock=clock,transactional=True)
    rows=[];issues=[];size=0;formula_count=style_count=0
    try:
        requested_name=name(args.get('name'))
        require(not any(item['name'].lower()==requested_name.lower() for item in tables(book.db)),'TABLE_EXISTS','Choose a table name that does not already exist')
    except EngineError as error:issues.append(dict(code=error.body['code'],message=str(error)))
    with sql_deadline(book.db,deadline):
        for row in range(r1,r2+1):
            values=[]
            for col in range(c1,c2+1):
                require(time.monotonic()<=deadline,'TIMEOUT','Table preview exceeded its time budget')
                raw=book.raw(sheet,row,col)
                try:
                    item=typed(raw);kind='blank' if item is None else item['kind']
                    if raw and raw['style']:style_count+=1
                    if kind in EVALUATED:formula_count+=1;stored=calc.cell(sheet,row,col)
                    else:stored=None if item is None else item.get('value')
                except (FormulaError,EngineError) as error:
                    stored=None
                    if len(issues)<100:issues.append(dict(row=row,col=col,code='CALCULATION_ERROR',message=str(error)[:256]))
                size+=len(encoded(stored).encode());require(size<=3*1024*1024,'LIMIT','Table preview values exceed 3 MiB')
                values.append(stored)
            rows.append(values)
    headers=rows.pop(0) if header else ['Column'+str(index+1) for index in range(c2-c1+1)]
    supplied=args.get('columns')
    if supplied is None:
        supplied=[]
        for index,label in enumerate(headers):
            present=[row[index] for row in rows if row[index] is not None]
            kinds={stored.kind if isinstance(stored,Temporal) else 'boolean' if type(stored) is bool else 'number' if type(stored) in (int,float) else 'text' for stored in present}
            supplied.append(dict(name=label if isinstance(label,str) else '' if label is None else str(label),type=next(iter(kinds)) if len(kinds)==1 else 'text'))
    require(isinstance(supplied,list) and len(supplied)==c2-c1+1,'INVALID_SCHEMA','Column definitions must match the selected width')
    try:definition=columns(supplied)
    except EngineError as error:
        return dict(can_create=False,columns=supplied,issues=[*issues,dict(code=error.body['code'],message=str(error))],record_count=len(rows),sheet=sheet,preview_clock=clock.isoformat())
    converted=[];changed=0;seen={col['id']:set() for col in definition if col['unique']}
    for index,row in enumerate(rows,r1+int(header)):
        target={}
        for col,stored in zip(definition,row):
            try:
                result=convert(col,stored,policy);target[col['id']]=result
                if result!=stored or type(result) is not type(stored):changed+=1
                if col['unique'] and result is not None:
                    require(result not in seen[col['id']],'UNIQUE_VALUE','Duplicate value in '+col['name']);seen[col['id']].add(result)
            except (EngineError,ValueError) as error:
                if len(issues)<100:issues.append(dict(row=index,column=col['name'],code='CONVERSION_ERROR',message=str(error)[:256]))
        converted.append(target)
    digest_payload=dict(state=book.state(),sheet=sheet,range=[r1,c1,r2,c2],header=header,name=args.get('name'),columns=definition,
                        clock=clock.isoformat(),conversion_policy=policy,records=converted)
    result=dict(can_create=not issues,sheet=sheet,columns=definition,records=converted,record_count=len(converted),issues=issues,
                preview_clock=clock.isoformat(),review_digest=hashlib.sha256(encoded(digest_payload).encode()).hexdigest(),
                report=[f'{formula_count} formula results become values; {style_count} cell styles are omitted. The source range is retained.',
                        f'{changed} values use the selected type conversion. Table records receive new identities; source list identities are retained.'])
    return result


def preview(book,args):
    with book.lock:
        deadline=time.monotonic()+2;book.refresh(deadline=deadline);book.db.execute('BEGIN')
        try:
            book.refresh(deadline=deadline);result=prepare(book,args,deadline=deadline)
            records=result.pop('records',[]);result['records_preview']=records[:min(20,4096//max(1,len(result['columns'])))];result['state']=book.state()
            r1,c1,r2,c2=rectangle(args)
            result['source_request']=dict(name=args.get('name'),sheet=book.sheet(args.get('sheet','Sheet1')),r1=r1,c1=c1,r2=r2,c2=c2,
                                          header=args.get('header',True),conversion_policy=args.get('conversion_policy','strict'))
            require(len(encoded(result).encode())<=4*1024*1024,'READ_LIMIT','Table preview exceeds 4 MiB')
            return result
        finally:book.db.execute('ROLLBACK')
