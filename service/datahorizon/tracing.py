"""Bounded syntactic reference tracing; no evaluation, hooks or retained graph."""
import json
import time
from .formula import Parser,FormulaError,split_ref,address
from .engine import EngineError,require,integer,typed,sql_deadline,MAX_ROWS,MAX_COLS


def trace(book,sheet,args):
    row=integer(args.get('row'),1,MAX_ROWS,'row');col=integer(args.get('col'),1,MAX_COLS,'col')
    direction=args.get('direction');expected=args.get('expected_state')
    require(direction in ('precedents','dependents'),'INVALID_ARGUMENT','direction must be precedents or dependents')
    require(isinstance(expected,str) and bool(expected),'INVALID_ARGUMENT','expected_state is required')
    with book.lock:
        deadline=time.monotonic()+2;book.refresh(deadline=deadline);book.db.execute('BEGIN')
        try:
            book.refresh(deadline=deadline)
            with sql_deadline(book.db,deadline):
                state=book.state();require(expected==state,'STALE_STATE','Trace inputs changed; refresh and trace again',409)
                sheet=book.sheet(sheet);catalog=dict(book.db.execute('SELECT id,name FROM sheets LIMIT 65'))
                require(len(catalog)<=64,'LIMIT','Trace supports at most 64 sheets')
                targets=[];issues=[];seen=set();work=size=scanned=0
                def check():
                    nonlocal work
                    work+=1
                    require(work<=10000 and time.monotonic()<=deadline,'LIMIT','Trace syntax work/time budget exceeded')
                def references(source,origin):
                    pending=[Parser(source[1:]).parse()];refs=[];broken=[]
                    def endpoint(node,default):
                        require(node[0]=='ref','UNSUPPORTED_FORMULA','Trace requires rectangular reference endpoints')
                        name,r,c=split_ref(node[1]);return book.sheet(name) if name is not None else default,r,c
                    while pending:
                        check();node=pending.pop();kind=node[0]
                        if kind in ('ref','range'):
                            try:
                                first=endpoint(node if kind=='ref' else node[2],origin)
                                last=first if kind=='ref' else endpoint(node[3],first[0])
                                require(first[0]==last[0],'UNSUPPORTED_FORMULA','Trace does not support 3D ranges')
                                refs.append((first[0],min(first[1],last[1]),min(first[2],last[2]),max(first[1],last[1]),max(first[2],last[2])))
                            except EngineError as error:
                                if getattr(error,'body',{}).get('code')!='SHEET_NOT_FOUND':raise
                                broken.append('Referenced sheet does not exist')
                        elif kind=='error':broken.append('Broken #REF! reference')
                        elif kind=='call':pending.extend(reversed(node[2]))
                        elif kind=='binary':pending.extend(reversed(node[2:]))
                        elif kind=='unary':pending.append(node[2])
                    return refs,broken
                if direction=='precedents':
                    raw=book.raw(sheet,row,col);item=typed(raw)
                    rows=[] if item is None or item['kind']!='formula' else [(sheet,row,col,raw['source'],raw['kind'],json.dumps(raw['style']))]
                else:
                    rows=book.db.execute("SELECT sheet_id,row,col,source,kind,style FROM cells WHERE kind='formula' OR (kind='auto' AND substr(source,1,1)='=') ORDER BY sheet_id,row,col LIMIT 10001")
                for origin,r,c,source,kind,style in rows:
                    check();scanned+=1;size+=len(source.encode())+len(style.encode())
                    require(scanned<=10000 and size<=8*1024*1024,'LIMIT','Trace exceeds 10,000 formulas or 8 MiB of inputs')
                    typed({'source':source,'kind':kind,'style':json.loads(style)})
                    try:refs,broken=references(source,origin)
                    except FormulaError as error:
                        require(False,'UNSUPPORTED_FORMULA',f'Cannot completely trace {catalog[origin]}!{address(r,c)}: {error.code}')
                    issues.extend({'sheet':origin,'sheet_name':catalog[origin],'address':address(r,c),'message':message} for message in broken)
                    chosen=refs if direction=='precedents' else [(origin,r,c,r,c)] if any(s==sheet and r1<=row<=r2 and c1<=col<=c2 for s,r1,c1,r2,c2 in refs) else []
                    for target in chosen:
                        if target in seen:continue
                        seen.add(target);s,r1,c1,r2,c2=target
                        targets.append(dict(sheet=s,sheet_name=catalog[s],r1=r1,c1=c1,r2=r2,c2=c2,address=address(r1,c1)+(':'+address(r2,c2) if (r1,c1)!=(r2,c2) else '')))
                    require(len(targets)<=1000 and len(issues)<=1000,'LIMIT','Trace exceeds 1,000 targets or broken-reference notices')
                check()
                return dict(state=state,sheet=sheet,row=row,col=col,direction=direction,semantics='direct_syntactic',targets=targets,issues=issues,scanned_formulas=scanned)
        finally:book.db.execute('ROLLBACK')
