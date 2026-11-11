# SPDX-License-Identifier: GPL-3.0-only
"""Measured relational workload; no universal capacity or OS-cold claims."""
from contextlib import closing
from pathlib import Path
import argparse
import json
import platform
import resource
import sqlite3
import subprocess
import sys
import time
from datahorizon.client import LocalService


def wait(book,result,timeout=70):
    deadline=time.monotonic()+timeout
    while result['status']=='running' and time.monotonic()<deadline:time.sleep(.01);result=book.query(result['job_id'])
    if result['status']=='running':raise RuntimeError('Query did not terminate within declared budgets')
    return result


def run(directory,rows=300000,payload_bytes=1024,reuse=False):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True);path=directory/'LargeRelational.dhwb'
    if not reuse:
        with LocalService() as client:
            with client.open(path,create=True) as book:
                book.create_table('Large',[dict(name='ID',type='integer',required=True,unique=True),dict(name='GroupKey',type='integer',required=True),dict(name='Payload')]);book.apply('edit',cells=[dict(row=1,col=1,source='=SUM(1,2,3)'),dict(row=1000000,col=26,source='Sparse corner',kind='text')])
        started=time.perf_counter()
        with closing(sqlite3.connect(path,isolation_level=None)) as db:
            db.execute('PRAGMA cache_size=-4096');db.execute('BEGIN IMMEDIATE')
            db.executemany('INSERT INTO Large(ID,GroupKey,Payload) VALUES(?,?,?)',((i,i%128,'x'*(payload_bytes-16)+f'{i:016d}') for i in range(rows)));db.execute('COMMIT')
    else:
        started=None
        with closing(sqlite3.connect(path)) as db:assert db.execute("SELECT count(*) FROM Large").fetchone()[0]==rows
    report=dict(platform=platform.platform(),python=platform.python_version(),sqlite=sqlite3.sqlite_version,rows=rows,payload_bytes=payload_bytes,native_bytes=path.stat().st_size,
        fixture_construction='Independent SQLite, one transaction; deliberately bypasses managed hooks/ingestion limits',fixture_build_seconds=None if started is None else time.perf_counter()-started,fixture_reused=reuse,logical_payload_bytes=rows*payload_bytes,
        provenance='Fresh service process; OS cache not purged and warmed by fixture construction. Subsequent reads are warm.',service_memory_target_bytes=256*1024*1024)
    report['dataset_larger_than_service_memory_target']=report['native_bytes']>report['service_memory_target_bytes']
    service=LocalService()
    with service as client:
        started=time.perf_counter()
        with client.open(path) as book:
            report['fresh_service_open_ms']=(time.perf_counter()-started)*1000
            catalog=book.tables()
            for index in catalog['indexes']:
                if index['name']=='LargeByGroupAndID':book.drop_index(index['id'])
            table=catalog['tables'][0];cols={col['name']:col['id'] for col in table['columns']};scope=dict(columns=[cols['ID'],cols['GroupKey']],sort=[dict(column=cols['GroupKey'],direction='asc'),dict(column=cols['ID'],direction='asc')],filters=[dict(column=cols['GroupKey'],op='lt',value=16)],count=True,limit=256)
            samples=[]
            for _ in range(3):
                started=time.perf_counter();page=book.read_table('Large',**scope);samples.append((time.perf_counter()-started)*1000)
            report['unindexed_first_page_ms']=samples
            started=time.perf_counter();book.create_index('Large','LargeByGroupAndID',[dict(column=cols['GroupKey'],direction='asc'),dict(column=cols['ID'],direction='asc')]);report['managed_index_ms']=(time.perf_counter()-started)*1000
            with closing(sqlite3.connect(path)) as db:
                expected=[str(row[0]) for row in db.execute('SELECT _dh_row_id FROM Large WHERE GroupKey<16 ORDER BY GroupKey,ID,_dh_row_id')]
                report['indexed_plan']=[row[3] for row in db.execute('EXPLAIN QUERY PLAN SELECT _dh_row_id,ID,GroupKey FROM Large WHERE GroupKey<16 ORDER BY GroupKey,ID,_dh_row_id LIMIT 256')]
            ids=[];cursor=None;page_ms=[];encoded_max=0;started_all=time.perf_counter()
            while True:
                started=time.perf_counter();page=book.read_table('Large',**scope,**(dict(cursor=cursor) if cursor else {}));page_ms.append((time.perf_counter()-started)*1000);ids.extend(row['row_id'] for row in page['records']);encoded_max=max(encoded_max,len(json.dumps(page).encode()));cursor=page['next_cursor']
                if cursor is None:break
            assert ids==expected and len(ids)==len(set(ids));assert page['count_status']=='exact' and page['count']==len(expected)
            report['indexed_full_filter_paging']=dict(records=len(ids),pages=len(page_ms),seconds=time.perf_counter()-started_all,first_page_ms=page_ms[0],median_page_ms=sorted(page_ms)[len(page_ms)//2],max_page_ms=max(page_ms),max_encoded_page_bytes=encoded_max,all_ids_match_independent_sqlite=True)
            result=wait(book,book.run_query('SELECT GroupKey,count(*) AS Records,sum(ID) AS Total FROM Large GROUP BY GroupKey ORDER BY GroupKey'))
            assert result['status']=='completed',result;report['aggregate_query']=result['result'];assert result['result']['row_count']==128;book.query(result['job_id'],'release')
            result=wait(book,book.run_query('SELECT ID,GroupKey FROM Large WHERE GroupKey<8 ORDER BY GroupKey,ID'))
            assert result['status']=='completed',result;offset=0;read=0
            while True:
                page=book.query(result['job_id'],'page',offset=offset);read+=len(page['rows'])
                if page['next_offset'] is None:break
                offset=page['next_offset']
            with closing(sqlite3.connect(path)) as db:expected_count=db.execute('SELECT count(*) FROM Large WHERE GroupKey<8').fetchone()[0]
            assert read==expected_count;report['complete_query_paging']=dict(records=read,spool_bytes=result['result']['spool_bytes'],snapshot_bytes=result['result']['snapshot_bytes']);book.query(result['job_id'],'release')
            result=book.run_query('SELECT count(*) FROM Large a CROSS JOIN Large b');deadline=time.monotonic()+10
            while result['status']=='running' and 'Executing' not in result['phase'] and time.monotonic()<deadline:time.sleep(.01);result=book.query(result['job_id'])
            started=time.perf_counter();book.query(result['job_id'],'cancel');cancelled=wait(book,result);report['query_cancel_ms']=(time.perf_counter()-started)*1000;assert cancelled['status']=='cancelled',cancelled;book.query(result['job_id'],'release')
            report['sparse_sheet_value']=book.read(r1=1,c1=1,r2=1,c2=1)['cells'][0]['value'];assert report['sparse_sheet_value']==6
            try:report['service_rss_sample_bytes']=int(subprocess.check_output(['ps','-o','rss=','-p',str(service.process.pid)],text=True).strip())*1024
            except (OSError,subprocess.CalledProcessError,ValueError):report['service_rss_sample_bytes']=None
    peak=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss;report['service_process_peak_rss_bytes']=peak if sys.platform=='darwin' else peak*1024
    report['memory_scope']='Combined engine and query threads in one service process; no separate query worker process. Child peak is OS-reported after service exit.'
    report['unqualified']='True OS-cold runs, eight concurrent workbooks, worst-case SQLite temporary-sort disk and universal capacity are not claimed.'
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path);parser.add_argument('--rows',type=int,default=300000);parser.add_argument('--payload-bytes',type=int,default=1024);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--reuse',action='store_true',help='Reuse this script’s disposable fixture, replacing its benchmark index');args=parser.parse_args();result=run(args.directory,args.rows,args.payload_bytes,args.reuse);args.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
