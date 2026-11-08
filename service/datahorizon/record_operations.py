"""Snapshot-reviewed record bulk editing and referent-preserving projection export."""
from contextlib import contextmanager
import json
import time
from .engine import require,integer,encoded,typed,display,EngineError
from . import records
from .formula import Parser,ADDRESS,split_ref,address,translate,FormulaError
from .temporal import Temporal,KINDS


@contextmanager
def snapshot(book,request):
    with book.lock:
        book.refresh();book.db.execute('BEGIN')
        try:
            book.refresh();require(request.get('expected_state')==book.state(),'STALE_STATE','Review current record targets',409);yield
        finally:book.db.execute('ROLLBACK')


def projection(book,request):
    sheet=book.sheet(request.get('sheet','Sheet1'));identifier=request.get('id')
    payload=next((p for s,i,p in records.objects(book) if (s,i)==(sheet,identifier)),None)
    require(payload is not None,'LIST_NOT_FOUND','Declared list is unavailable',404)
    spec={**payload['spec'],**{key:request[key] for key in ('sort','filters') if key in request}}
    result=book._list_rows(sheet,spec,book.calculator(seconds=2),time.monotonic()+2)
    excluded=request.get('exclude_rows',[]);require(isinstance(excluded,list) and len(excluded)<=1024,'LIMIT','At most 1,024 excluded rows')
    for row in excluded:integer(row,1,1048576,'excluded row')
    selected=set(result['rows'])-set(excluded);lookup={item['row']:item for item in payload['rows']}
    result.update(state=book.state(),sheet=sheet,sheet_name=book.db.execute('SELECT name FROM sheets WHERE id=?',(sheet,)).fetchone()[0],id=identifier,spec=spec,verified=payload['fingerprint']==records.digest(book,sheet,payload['spec']) and (sheet,identifier) not in book.unverified_records,
                  records=[lookup[row] for row in result['rows'] if row in selected and row in lookup])
    return result


def review(book,request):
    with snapshot(book,request):
        result=projection(book,request);require(len(encoded(result).encode())<=4*1024*1024,'READ_LIMIT','Record review exceeds 4 MiB');return result


def columns(spec,request):
    result=request.get('columns',list(range(spec['c1'],spec['c2']+1)))
    require(isinstance(result,list) and result and all(type(c) is int for c in result) and len(set(result))==len(result),'INVALID_ARGUMENT','Choose unique source columns')
    for col in result:integer(col,spec['c1'],spec['c2'],'source column')
    return result


def copy(book,request):
    with snapshot(book,request):
        result=projection(book,request);require(result['verified'],'UNVERIFIED_RECORDS','Reconcile declared records before copying',409)
        cols=columns(result['spec'],request);rows=[item['row'] for item in result['records']]
        if request.get('include_header',False) and result['spec']['header']:rows.insert(0,result['spec']['r1'])
        mode=request.get('mode','all');require(mode in ('all','values','formats'),'INVALID_ARGUMENT','Unsupported record copy mode')
        matrix=[];calc=book.calculator(seconds=2);size=len(encoded(result).encode());deadline=time.monotonic()+2
        for row in rows:
            line=[]
            for col in cols:
                require(time.monotonic()<=deadline,'TIMEOUT','Record copy exceeded its budget')
                raw=book.raw(result['sheet'],row,col)
                if raw is None:cell={}
                else:
                    item=typed(raw);cell=dict(row=row,col=col,source=raw['source'],input_kind=raw['kind'],kind=item['kind'],style=item['style'])
                    if mode!='formats':
                        try:
                            value=calc.cell(result['sheet'],row,col) if item['kind']=='formula' else item['value']
                            cell.update(value=value,display=display(value,item['style']),calculation='current')
                            if isinstance(value,Temporal):cell['value_kind']=value.kind
                        except FormulaError as error:
                            require(mode!='values','CALCULATION_ERROR','Values copy cannot discard formula errors')
                            cell.update(value=None,display=error.code,calculation='error')
                size+=len(encoded(cell).encode());require(size<=4*1024*1024,'READ_LIMIT','Record copy exceeds 4 MiB');line.append(cell)
            matrix.append(line)
        result.update(rows=matrix,source_rows=rows,source_columns=cols)
        require(len(encoded(result).encode())<=4*1024*1024,'READ_LIMIT','Record snapshot exceeds 4 MiB');return result


def edit(book,request):
    sheet=book.sheet(request.get('sheet','Sheet1'));identifier=request.get('id')
    payload=next((p for s,i,p in records.objects(book) if (s,i)==(sheet,identifier)),None)
    require(payload is not None,'LIST_NOT_FOUND','Declared list is unavailable',404)
    require(payload['fingerprint']==records.digest(book,sheet,payload['spec'],book.deadline,cancel_event=book.cancel_event) and (sheet,identifier) not in book.unverified_records,'UNVERIFIED_RECORDS','Reconcile records before editing',409)
    ids=request.get('record_ids');require(isinstance(ids,list) and ids and all(isinstance(i,str) for i in ids) and len(set(ids))==len(ids),'INVALID_ARGUMENT','Provide reviewed unique record IDs')
    lookup={item['id']:item['row'] for item in payload['rows']};require(all(i in lookup for i in ids),'STALE_RECORDS','A reviewed record no longer exists',409)
    cols=columns(payload['spec'],request);require(len(ids)*len(cols)<=10000,'LIMIT','Record edit exceeds 10,000 cells')
    mode=request.get('mode','all');require(mode in ('all','values','formats','clear'),'INVALID_ARGUMENT','Unsupported record edit mode')
    matrix=request.get('rows',[]);repeat=request.get('repeat',False);require(type(repeat) is bool,'INVALID_ARGUMENT','repeat must be Boolean')
    if mode!='clear':require(isinstance(matrix,list) and len(matrix)==(1 if repeat else len(ids)) and all(isinstance(row,list) and len(row)==len(cols) for row in matrix),'INVALID_ARGUMENT','Clipboard dimensions must match reviewed records/columns')
    edits=[]
    for index,identifier in enumerate(ids):
        row=lookup[identifier]
        for offset,col in enumerate(cols):
            cell=dict(row=row,col=col)
            if mode=='clear':
                scope=request.get('clear_scope','contents');require(scope in ('contents','formats','all'),'INVALID_ARGUMENT','Unsupported clear scope')
                cell.update({'clear':True} if scope=='all' else {'style':{},'replace_style':True} if scope=='formats' else {'clear_input':True})
            else:
                value=matrix[0 if repeat else index][offset];require(isinstance(value,dict),'INVALID_ARGUMENT','Clipboard cells must be objects')
                if mode=='all' and not value:cell['clear']=True
                elif mode=='values' and not value:cell['clear_input']=True
                else:
                    if mode!='formats':
                        require(not value.get('source_omitted') and not value.get('value_omitted'),'INCOMPLETE_CLIPBOARD','Full source/value cells are required')
                        source=value.get('source','');kind=value.get('input_kind',value.get('kind','text'))
                        if mode=='values':
                            require(value.get('calculation')=='current' and 'value' in value,'CALCULATION_ERROR','Values paste requires calculated current values')
                            scalar=value['value'];require(isinstance(scalar,(str,int,float,bool,type(None))),'INVALID_ARGUMENT','Values must be scalar');kind=value.get('value_kind') if value.get('value_kind') in KINDS else 'null' if scalar is None else 'boolean' if type(scalar) is bool else 'number' if type(scalar) in (int,float) else 'text'
                            source='' if scalar is None else 'TRUE' if scalar is True else 'FALSE' if scalar is False else str(scalar)
                        elif kind in ('auto','formula') and isinstance(source,str) and source.startswith('='):
                            origin_row=integer(value.get('row',row),1,1048576,'origin row');origin_col=integer(value.get('col',col),1,16384,'origin column')
                            source=translate(source,row-origin_row,col-origin_col)
                        if kind=='blank':cell['clear_input']=True
                        else:cell.update(source=source,kind=kind)
                    if mode!='values':cell.update(style=value.get('style',{}),replace_style=True)
            edits.append(cell)
    book.apply_edit(sheet,edits,preserve_inputs=True)


def _projected_formula(source,sheet,rows,cols,sheet_id=None):
    parser=Parser(source[1:],track_spans=True);pending=[parser.parse()];changes=[]
    while pending:
        node=pending.pop()
        if node[0]=='range':
            require(node[2][0]==node[3][0]=='ref','UNSUPPORTED_FORMULA','Projection needs ordinary rectangular ranges')
            _,r1,c1=split_ref(node[2][1]);_,r2,c2=split_ref(node[3][1]);r1,r2=sorted((r1,r2));c1,c2=sorted((c1,c2))
            require(all(r in rows for r in range(r1,r2+1)) and all(c in cols for c in range(c1,c2+1)),'UNSUPPORTED_FORMULA','A referenced range includes excluded records/columns; choose values')
            require(all(rows[r]==rows[r1]+r-r1 for r in range(r1,r2+1)) and all(cols[c]==cols[c1]+c-c1 for c in range(c1,c2+1)),'UNSUPPORTED_FORMULA','Projection would change referenced range order; choose values')
            pending.extend(node[2:])
        elif node[0]=='ref':
            name,row,col=split_ref(node[1]);require(name is None or name==sheet_id or name.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))==sheet.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')),'UNSUPPORTED_FORMULA','Cross-sheet referents are outside this projection')
            require(row in rows and col in cols,'UNSUPPORTED_FORMULA','A formula references an excluded cell; choose values')
            parts=ADDRESS.fullmatch(node[1].rsplit('!',1)[-1]);new=f'{parts[1]}{address(rows[row],cols[col])}'
            if parts[3]:new=new[:-len(str(rows[row]))]+'$'+str(rows[row])
            begin,end=parser.spans[id(node)];changes.append((begin+1,end+1,new))
        elif node[0]=='call':pending.extend(node[2])
        elif node[0]=='binary':pending.extend(node[2:])
        elif node[0]=='unary':pending.append(node[2])
    for begin,end,new in sorted(changes,reverse=True):source=source[:begin]+new+source[end:]
    return source


def projected_formula(source,sheet,rows,cols,sheet_id=None):
    try:return _projected_formula(source,sheet,rows,cols,sheet_id)
    except FormulaError as error:raise EngineError('UNSUPPORTED_FORMULA',str(error)) from error


def export(book,format,request):
    from .interchange import xlsx_export
    from .ods import ods_export
    require(format in ('xlsx','ods'),'INVALID_ARGUMENT','Record formula export supports XLSX/ODS')
    mode=request.get('mode','formulas');require(mode in ('formulas','values'),'INVALID_ARGUMENT','Choose formulas or values')
    result=copy(book,{**request,'mode':'all','include_header':request.get('include_header',True)})
    rowmap={row:i for i,row in enumerate(result['source_rows'],1)};colmap={col:i for i,col in enumerate(result['source_columns'],1)}
    sheet=result['sheet_name'];cells=[]
    for r,line in enumerate(result['rows'],1):
        for c,original in enumerate(line,1):
            if not original:continue
            cell={**original,'row':r,'col':c}
            if mode=='formulas' and cell['kind']=='formula':cell['source']=projected_formula(cell['source'],sheet,rowmap,colmap,result['sheet'])
            cells.append(cell)
    snapshot=dict(state=result['state'],range=[1,1,len(rowmap),len(colmap)],cells=cells)
    require(bool(rowmap),'NO_RESULTS','No records to export')
    exported=(xlsx_export if format=='xlsx' else ods_export)(snapshot,dict(mode=mode))
    exported['report'].append('Declared records exported in reviewed order. Native record IDs remain in the source; unsupported/excluded formula referents block formula export.')
    return exported
