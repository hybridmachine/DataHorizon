# SPDX-License-Identifier: GPL-3.0-only
"""Complete, bounded values snapshots for structured tables and query spools."""
from contextlib import closing
import base64
import csv
import io
import json
from pathlib import Path
import sqlite3
import time
from .engine import encoded,require,boolean,sql_deadline,interchange_text
from .relational import table,tables,quote,from_sql,objects
from .table_browse import criteria
from .temporal import Temporal


def convert(value,kind=None):
    fallback=False
    if isinstance(value,dict):
        require(set(value)=={'type','value'} and value['type']=='integer','QUERY_VALUE','Unsupported tagged export value');value=value['value'];kind='text';fallback=True
    elif type(value) is int and abs(value)>=10**15:value=str(value);kind='text';fallback=True
    elif value is not None and kind in ('date','time','datetime'):value=Temporal(value,kind)
    return value,kind,fallback


def encode(columns,rows,state,format,omissions,*,include_header=True,kinds=None,scope='snapshot'):
    require(format in ('csv','xlsx','ods'),'INVALID_ARGUMENT','Choose CSV, XLSX or ODS')
    width=len(columns);require(width>0,'NO_RESULTS','No columns to export');matrix=[];size=0;fallbacks=0;record_count=0
    if include_header:matrix.append([(label,'text') for label in columns])
    for row in rows:
        record_count+=1;require((record_count+int(include_header))*width<=10000,'EXPORT_LIMIT','Complete export exceeds the current 10,000-value interchange tier, including headers. Narrow the scope or retain the native workbook.',409)
        line=[]
        for index,value in enumerate(row):
            value,kind,fallback=convert(value,None if kinds is None else kinds[index]);fallbacks+=int(fallback);size+=len(encoded(value).encode())+128
            require(size<=3*1024*1024,'EXPORT_LIMIT','Complete export exceeds the current interchange payload budget; no partial file produced',409)
            line.append((value,kind))
        matrix.append(line)
    require(matrix,'NO_RESULTS','Empty output without headers has no interchange representation')
    report=[f'Complete {scope}: {record_count} records × {width} columns; {int(include_header)} header row. No page truncation.',
        'Values only. Stable identities, constraints, indexes, relationships, query definitions, service Python code and history remain in the native source.',
        'Native relational objects omitted: '+(', '.join(omissions) if omissions else 'none'),
        f'{fallbacks} exact integers with more than 15 decimal digits or tagged large integers exported as Text to preserve all digits.']
    if format=='csv':
        output=io.StringIO(newline='');writer=csv.writer(output)
        for row in matrix:writer.writerow([interchange_text(value) for value,_ in row])
        result=dict(csv=output.getvalue(),file_base64=base64.b64encode(output.getvalue().encode('utf-8')).decode(),state=state,
            report=report+['UTF-8 CSV is untyped. NULL and empty Text become empty fields; temporal values use ISO text. Import exact integer and formula-looking Text columns explicitly as Text. No temporal companion is generated for this relational values export.'])
    else:
        cells=[]
        for r,row in enumerate(matrix,1):
            for c,(value,kind) in enumerate(row,1):
                actual='null' if value is None else kind if kind in ('date','time','datetime') else 'boolean' if type(value) is bool else 'number' if type(value) in (int,float) else 'text'
                source='' if value is None else interchange_text(value)
                cells.append(dict(row=r,col=c,kind=actual,input_kind=actual,source=source,value=value,value_kind=actual,style={},calculation='current'))
        snapshot=dict(cells=cells,state=state,range=[1,1,len(matrix),width])
        from .interchange import xlsx_export
        from .ods import ods_export
        result=(xlsx_export if format=='xlsx' else ods_export)(snapshot,dict(mode='values'));result['report']=report+result['report'][2:]
    result.update(records=record_count,columns=width,header=include_header,format=format)
    require(len(encoded(result).encode())<=4*1024*1024,'EXPORT_LIMIT','Complete export response exceeds 4 MiB; no partial file produced',409);return result


def export_table(book,format,args):
    require(not set(args)-{'expected_state','table','columns','sort','filters','include_header','mode'} and args.get('mode','values')=='values','INVALID_EXPORT_SCOPE','Choose a complete values-only table scope without page coordinates')
    require(not set(args)&{'offset','limit','cursor','after','record_ids'},'INVALID_EXPORT_SCOPE','Export the complete table/filter scope, not page coordinates')
    with book.lock:
        deadline=time.monotonic()+2;book.refresh(deadline=deadline);book.db.execute('BEGIN')
        try:
            with sql_deadline(book.db,deadline):
                book.refresh(deadline=deadline);require(args.get('expected_state')==book.state(),'STALE_STATE','Review the current table before export',409)
                item=table(book.db,args.get('table'));known={col['id']:col for col in item['columns']};selected=args.get('columns',list(known))
                require(isinstance(selected,list) and selected and all(isinstance(key,str) and key in known for key in selected) and len(selected)==len(set(selected)),'INVALID_ARGUMENT','Choose unique table columns')
                selected=[known[key] for key in selected];order,terms,parameters=criteria(item,args)
                sql='SELECT '+','.join(quote(col['name']) for col in selected)+' FROM '+quote(item['name'])+(' WHERE '+' AND '.join(terms) if terms else '')+' ORDER BY '+','.join([quote(col['name'])+' '+direction for col,direction in order]+['"_dh_row_id" ASC'])+' LIMIT ?'
                rows=([from_sql(col,value) for col,value in zip(selected,row)] for row in book.db.execute(sql,[*parameters,10000//len(selected)+1]))
                return encode([c['name'] for c in selected],rows,book.state(),format,[item['name'] for _,item in objects(book.db)],include_header=boolean(args.get('include_header',True),'include_header'),kinds=[c['type'] for c in selected],scope='table/filter snapshot')
        finally:book.db.execute('ROLLBACK')


def export_query(manager,job_id,format,args):
    require(not set(args)-{'expected_state','columns','include_header','mode'} and args.get('mode','values')=='values','INVALID_EXPORT_SCOPE','Choose a complete values-only query result without page coordinates')
    require(not set(args)&{'offset','limit','cursor','after'},'INVALID_EXPORT_SCOPE','Export the complete completed query result, not a page')
    deadline=time.monotonic()+2
    with manager.book.lock:
        with sql_deadline(manager.book.db,deadline):
            status=manager.access(job_id);require(status['status']=='completed','STALE_RESULT','Only a complete current query can be exported',409);require(args.get('expected_state')==status['state'],'STALE_STATE','Review the current query state',409)
            with manager.lock:
                entry=manager.entries[job_id];metadata=entry['result'];known={col['id']:col for col in metadata['columns']};selected=args.get('columns',list(known));header=boolean(args.get('include_header',True),'include_header')
                require(isinstance(selected,list) and selected and all(isinstance(key,str) and key in known for key in selected) and len(selected)==len(set(selected)),'INVALID_ARGUMENT','Choose unique query column ordinals')
                require((metadata['row_count']+int(header))*len(selected)<=10000,'EXPORT_LIMIT','Complete query export exceeds the 10,000-value interchange tier, including headers; no partial file produced',409)
                with closing(sqlite3.connect(Path(entry['directory'].name)/'result.sqlite')) as db:
                    with sql_deadline(db,deadline):
                        rows=([row[int(key)] for key in selected] for (payload,) in db.execute('SELECT payload FROM result ORDER BY row_id') for row in (json.loads(payload),))
                        result=encode([known[key]['name'] for key in selected],rows,status['state'],format,[item['name'] for _,item in objects(manager.book.db)],include_header=header,scope='query snapshot')
                        result.update(job_id=job_id,generation=status['generation']);return result
