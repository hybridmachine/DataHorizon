"""Dataset-specific dependency, background calculation and durable-job measurements."""
from contextlib import closing
from datetime import datetime,timezone
import json
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'service'))
from datahorizon.engine import Workbook
from cache_rss_benchmark import measured_service,distribution


def retained_job_disk(path):
    root=path.parent/('.'+path.name+'.dhjobs')
    result=dict(snapshot_bytes=0,journal_bytes=0,other_bytes=0)
    if root.exists():
        for file in root.iterdir():
            if file.is_file():
                kind='snapshot_bytes' if '.snapshot-' in file.name else 'journal_bytes' if file.suffix=='.sqlite' else 'other_bytes'
                result[kind]+=file.stat().st_size
    result['total_bytes']=sum(result.values())
    return result


def wait(book,job,calculation=False):
    deadline=time.monotonic()+120
    while job['status']=='running' and time.monotonic()<deadline:
        time.sleep(.005);job=book.calculation(job['job_id']) if calculation else book.operation_job(job['job_id'])
    if job['status']!='completed':raise AssertionError(job)
    return job


def measurements():
    with tempfile.TemporaryDirectory(prefix='dh-m3-m5-') as temp:
        path=Path(temp)/'measurement.dhwb'
        with closing(Workbook(path,create=True)) as engine:
            sheet=engine.sheet('Sheet1');engine.mutate(dict(action='sheet_add',name='Disk',operation_id='disk',expected_state=engine.state()));disk=engine.sheet('Disk')
        with closing(sqlite3.connect(path)) as direct,direct:
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'number')",((sheet,row,str(100001-row)) for row in range(1,100001)))
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,2,?,'formula')",((sheet,row,'=A'+str(row)+'*2') for row in range(1,1001)))
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'text')",((disk,row,'x'*1024) for row in range(1,280001)))
        database_bytes=path.stat().st_size
        assert database_bytes>256*1024*1024
        # Inspect disposable cache accounting in an isolated engine, then release it
        # before metering the separate service process.
        with closing(Workbook(path)) as engine:
            scope=dict(r2=1000,c1=2,c2=2);engine.read('Sheet1',scope);before=dict(hits=engine.dependencies.hits,nodes=len(engine.dependencies.values),edges=sum(map(len,engine.dependencies.edges.values())),bytes=engine.dependencies.bytes)
            engine.read('Sheet1',scope);reused=engine.dependencies.hits-before['hits'];engine.mutate(dict(action='edit',operation_id='invalidate',expected_state=engine.state(),cells=[dict(row=1,col=1,source='200001')]))
            assert engine.read('Sheet1',dict(r2=2,c1=2,c2=2))['cells'][0]['value']==400002
            assert reused>=1000 and before['nodes']<=4096 and before['edges']<=50000 and before['bytes']<=4*1024*1024
        with measured_service() as client,client.open(path) as book:
            times=[]
            for _ in range(20):
                started=time.perf_counter();cells=book.read(r2=1000,c1=2,c2=2)['cells'];times.append((time.perf_counter()-started)*1000);assert cells[0]['value']==400002
            started=time.perf_counter();calc=wait(book,book.calculate(r2=1000,c1=2,c2=2),True);calculation_ms=(time.perf_counter()-started)*1000;assert len(calc['result']['cells'])==1000;book.calculation(calc['job_id'],'release')
            started=time.perf_counter();job=book.create_job(action='list',r2=100000,c2=1,sort=[dict(col=1)])
            job=wait(book,book.operation_job(job['job_id'],'start',plan_digest=job['plan_digest'],expected_state=book.state));sort_ms=(time.perf_counter()-started)*1000
            count=0;page_times=[]
            for offset in range(0,100000,256):
                started=time.perf_counter();page=book.operation_job(job['job_id'],offset=offset,limit=256);page_times.append((time.perf_counter()-started)*1000)
                for result in page['results']:
                    assert result['row']==100000-count;count+=1
            assert count==100000;read_disk=retained_job_disk(path);book.operation_job(job['job_id'],'release');released_disk=retained_job_disk(path)
            assert read_disk['snapshot_bytes']>=database_bytes and released_disk['total_bytes']==0
            job=book.create_job(mode='batches',batch_size=1000)
            for start in range(1,20001,1000):book.operation_job(job['job_id'],'append',cells=[dict(row=row,col=3,source='job-'+str(row),kind='text') for row in range(start,start+1000)])
            staged_disk=retained_job_disk(path)
            job=book.operation_job(job['job_id'],'seal');job=book.operation_job(job['job_id'],'start',plan_digest=job['plan_digest'],expected_state=book.state)
            deadline=time.monotonic()+10
            while job['next']==0 and job['status']=='running' and time.monotonic()<deadline:time.sleep(.005);job=book.operation_job(job['job_id'])
            started=time.perf_counter();book.operation_job(job['job_id'],'cancel')
            while job['status']=='running' and time.monotonic()-started<10:time.sleep(.005);job=book.operation_job(job['job_id'])
            cancel_ms=(time.perf_counter()-started)*1000;assert job['status']=='cancelled' and 0<job['next']<20000
            committed_before_resume=job['next'];before_ids=[batch['operation_id'] for batch in job['batches'] if batch['receipt'] and batch['receipt']['outcome']=='committed']
            book.refresh();started=time.perf_counter();job=wait(book,book.operation_job(job['job_id'],'resume',plan_digest=job['plan_digest'],expected_state=book.state));resume_ms=(time.perf_counter()-started)*1000
            after_ids=[batch['operation_id'] for batch in job['batches'] if batch['receipt'] and batch['receipt']['outcome']=='committed'];assert after_ids[:len(before_ids)]==before_ids and len(after_ids)==20 and job['next']==20000
            assert book.read(r1=20000,r2=20000,c1=3,c2=3)['cells'][0]['value']=='job-20000'
            memory=client.request('/benchmark/memory')['peak_rss_bytes'];book.operation_job(job['job_id'],'release')
        assert memory<256*1024*1024
        return dict(dataset=dict(native_bytes=database_bytes,numeric_cells=100000,formulas=1000,long_text_cells=280000,larger_than_256_mib=True),dependency_cache=dict(**before,reused_hits=reused),formula_range=distribution(times),background_calculation_ms=round(calculation_ms,3),global_sort_100000_rows_ms=round(sort_ms,3),sorted_rows_verified=count,result_pages=distribution(page_times),read_job_retained_disk=read_disk,read_job_disk_after_release=released_disk,edit_job_staged_disk=staged_disk,cancel_after_committed_cells=committed_before_resume,cancel_confirmation_ms=round(cancel_ms,3),resume_remaining_ms=round(resume_ms,3),unique_committed_batches=len(after_ids),service_peak_rss_bytes=memory)


if __name__=='__main__':
    report=dict(recorded_at_utc=datetime.now(timezone.utc).isoformat(),platform=platform.platform(),python=platform.python_version(),**measurements(),limitations=['Fresh process and warm OS caches; no OS cache purge or compositor latency measurement.','The memory target applies to this single-workbook workload; no universal memory/latency guarantee.','Worker cancellation is cooperative at declared checkpoints; external side effects cannot be rolled back.'])
    Path(sys.argv[1]).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
