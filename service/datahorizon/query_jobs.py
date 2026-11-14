# SPDX-License-Identifier: GPL-3.0-only
"""Private SQLite snapshots and bounded disk-spooled read-only query results."""
from contextlib import closing,contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from .engine import EngineError,encoded,require,integer,boolean,sql_deadline
from .relational import tables
from .query_sql import bindings,authorize,configure,scalar
from .query_storage import create


@contextmanager
def owned(book,event,deadline):
    while not book.lock.acquire(timeout=.05):
        require(not event.is_set(),'CANCELLED','Query cancelled');require(time.monotonic()<=deadline,'TIMEOUT','Query ownership deadline exceeded')
    try:
        require(not event.is_set(),'CANCELLED','Query cancelled');yield
    finally:book.lock.release()


class _Stop:
    """Cancellation of a run outside the job store: its caller's event, or the manager stopping."""
    def __init__(self,jobs,event):self.jobs=jobs;self.event=event
    def is_set(self):return self.event.is_set() or self.jobs.stopping


class QueryJobs:
    def __init__(self,book):
        self.book=book;self.lock=threading.RLock();self.entries={};self.runs=set();self.generation=0;self.stopping=False
        self.seconds=60;self.snapshot_seconds=5;self.max_rows=100000;self.max_values=1000000;self.max_bytes=64*1024*1024;self.retention=300

    def _cleanup(self,entry):
        entry['directory'].cleanup()

    def _expire(self):
        for key,entry in list(self.entries.items()):
            if entry['finished'] is not None and time.monotonic()-entry['finished']>=self.retention:self.entries.pop(key);self._cleanup(entry)

    def start(self,request):
        job_id=request.get('job_id');require(isinstance(job_id,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',job_id),'INVALID_JOB','Query job_id needs 1–64 ASCII letters/digits/_/-')
        fingerprint=hashlib.sha256(encoded(request).encode()).hexdigest()
        with self.book.lock:
            self.book.refresh();require(request.get('expected_state')==self.book.state(),'STALE_STATE','Refresh before running SQL',409)
            require(not self.book.repair,'REPAIR_SCOPE','Reopen normally before running queries',409)
            saved=None
            if 'query' in request:saved,request=self._saved(request)
            declarations,values=bindings(request.get('sql'),request.get('parameters',[]),request.get('values',{}))
            with self.lock:
                self._expire();require(not self.stopping,'SERVICE_STOPPING','Query manager is stopping',503)
                if job_id in self.entries:
                    entry=self.entries[job_id];require(entry['fingerprint']==fingerprint,'JOB_CONFLICT','Query ID has different inputs',409);return self._view(job_id,entry)
                require(len(self.entries)<4,'JOB_LIMIT','Release a retained query job before starting another',409)
                require(not any(e['status']=='running' for e in self.entries.values()),'JOB_BUSY','One query may execute at a time per workbook',409)
                directory=create();self.generation+=1
                entry=dict(status='running',phase='Snapshot queued',rows_processed=0,state=self.book.state(),generation=self.generation,fingerprint=fingerprint,
                    cancel=threading.Event(),finished=None,directory=directory,sql=request['sql'],parameters=declarations,values=values,saved=saved)
                try:
                    worker=threading.Thread(target=self._run,args=(job_id,entry),daemon=True);entry['worker']=worker;self.entries[job_id]=entry;worker.start()
                except BaseException:self.entries.pop(job_id,None);self.generation-=1;self._cleanup(entry);raise
                return self._view(job_id,entry)

    def _saved(self,request):
        """Prepare a saved run under the book lock: stored SQL, unchanged shape/dependencies, defaults under request values."""
        require('sql' not in request and 'parameters' not in request,'INVALID_ARGUMENT','Saved query runs use the stored SQL and declarations')
        from .query_views import query,prepare,definition_digest
        saved=query(self.book.db,request['query'])
        require('definition_digest' not in request or request['definition_digest']==definition_digest(saved),'STALE_DEFINITION','Reload the changed saved definition before running it',409)
        declarations,deps,shape=prepare(self.book.db,saved['sql'],saved['parameters'],tables(self.book.db),time.monotonic()+2)
        require(shape==saved['shape'] and deps==saved['dependencies'],'QUERY_DEPENDENCY_CHANGED','Review the saved query dependencies and result shape',409)
        require(isinstance(request.get('values',{}),dict),'INVALID_PARAMETERS','Parameter values must be an object')
        return saved,{**request,'sql':saved['sql'],'parameters':declarations,'values':{**saved['defaults'],**request.get('values',{})}}

    def run_once(self,query_id,*,state,cancel_event,seconds,max_rows):
        """Run a saved query synchronously on a private snapshot, outside the job store; rows are held in memory."""
        done=threading.Event();directory=None
        with self.lock:require(not self.stopping,'SERVICE_STOPPING','Query manager is stopping',503);self.runs.add(done)
        try:
            with self.book.lock:
                self.book.refresh();require(state==self.book.state(),'STALE_STATE','Refresh before running SQL',409)
                require(not self.book.repair,'REPAIR_SCOPE','Reopen normally before running queries',409)
                saved,request=self._saved(dict(query=query_id));declarations,values=bindings(request['sql'],request['parameters'],request['values'])
            # Closing the manager cancels this run as it cancels job workers.
            directory=create();entry=dict(state=state,cancel=_Stop(self,cancel_event),directory=directory,sql=request['sql'],values=values,saved=saved)
            snapshot,registered=self._snapshot(entry);deadline=time.monotonic()+seconds;rows=[]
            with closing(sqlite3.connect(snapshot,isolation_level=None)) as db:
                configure(db);dependencies,denied=authorize(db,registered)
                with self._executing(entry,db,deadline,denied):
                    results=self._results(entry,db,deadline,dependencies);columns=[col['name'] for col in next(results)]
                    for row in results:require(len(rows)<max_rows,'QUERY_LIMIT',f'Query exceeds {max_rows:,} rows',409);rows.append(row)
                    self._checkpoint(entry,deadline)
            return dict(columns=columns,rows=rows)
        finally:
            if directory is not None:directory.cleanup()
            with self.lock:self.runs.discard(done)
            done.set()

    def _checkpoint(self,entry,deadline):
        require(not entry['cancel'].is_set(),'CANCELLED','Query cancelled; no workbook data changed',409)
        require(time.monotonic()<=deadline,'TIMEOUT','Query exceeded its time budget',409)

    def _snapshot(self,entry):
        deadline=time.monotonic()+self.snapshot_seconds;destination=Path(entry['directory'].name)/'snapshot.sqlite';source=None
        try:
            with owned(self.book,entry['cancel'],deadline):
                with sql_deadline(self.book.db,deadline,cancel_event=entry['cancel']):
                    self.book.refresh(deadline=deadline);require(self.book.state()==entry['state'],'STALE_STATE','Query source changed before snapshot',409);self.book.verify_file()
                    source=sqlite3.connect(self.book.path.as_uri()+'?mode=ro',uri=True,isolation_level=None,timeout=.1)
                    source.execute('BEGIN');source.execute('SELECT revision FROM dh_meta').fetchone()
                    self.book.refresh(deadline=deadline);require(self.book.state()==entry['state'],'STALE_STATE','Query source changed while pinning snapshot',409)
                    registered=tables(self.book.db);self.book.verify_file()
            with closing(sqlite3.connect(destination)) as target:
                def progress(*_):self._checkpoint(entry,deadline)
                source.backup(target,pages=256,progress=progress,sleep=.001);progress();self.book.verify_file()
            return destination,registered
        finally:
            if source is not None:source.close()

    @contextmanager
    def _executing(self,entry,db,deadline,denied):
        """Bound execution by the deadline and cancellation and translate SQLite failures."""
        with sql_deadline(db,deadline,cancel_event=entry['cancel']):
            try:yield
            except sqlite3.Error as error:
                if getattr(error,'sqlite_errorcode',0)&255==sqlite3.SQLITE_INTERRUPT:self._checkpoint(entry,deadline)
                if denied:raise EngineError('SQL_DENIED','SQL attempted a forbidden operation, object or function',403) from error
                code=getattr(error,'sqlite_errorcode',0)&255
                raise EngineError('QUERY_LIMIT' if code in (sqlite3.SQLITE_FULL,sqlite3.SQLITE_TOOBIG,sqlite3.SQLITE_NOMEM) else 'QUERY_FAILED',str(error)[:512],409) from error

    def _results(self,entry,db,deadline,dependencies):
        """Yield the result columns, then each row of transport scalars, under the shared result checks."""
        cursor=db.execute(entry['sql'],entry['values']);require(cursor.description is not None,'SQL_DENIED','Only a read-only SELECT result is supported')
        columns=[dict(id=str(index),name=col[0]) for index,col in enumerate(cursor.description)]
        require(1<=len(columns)<=256,'QUERY_LIMIT','At most 256 result columns')
        require(all(len(col['name'].encode())<=1024 for col in columns) and sum(len(col['name'].encode()) for col in columns)<=65536,'QUERY_LIMIT','Result labels exceed 1 KiB each or 64 KiB total; use shorter explicit aliases')
        if entry['saved'] is not None:
            actual=[dict(table=key,columns=sorted(value)) for key,value in sorted(dependencies.items())]
            require(columns==entry['saved']['shape'] and actual==entry['saved']['dependencies'],'QUERY_DEPENDENCY_CHANGED','Saved query shape/dependencies changed; no result published',409)
        yield columns;count=0
        for row in cursor:
            self._checkpoint(entry,deadline);count+=1;require(count<=self.max_rows and count*len(columns)<=self.max_values,'QUERY_LIMIT','Query exceeds 100,000 rows or 1,000,000 values',409)
            yield [scalar(value) for value in row]

    def _execute(self,entry):
        with self.lock:entry['phase']='Copying private snapshot'
        started=time.monotonic();snapshot,registered=self._snapshot(entry);snapshot_seconds=time.monotonic()-started;snapshot_bytes=snapshot.stat().st_size
        execution_started=time.monotonic();deadline=execution_started+self.seconds;spool=Path(entry['directory'].name)/'result.sqlite'
        try:
            with closing(sqlite3.connect(snapshot,isolation_level=None)) as db,closing(sqlite3.connect(spool)) as output:
                configure(db);dependencies,denied=authorize(db,registered)
                output.execute('PRAGMA page_size=4096');output.execute('PRAGMA cache_size=-2048');output.execute('PRAGMA max_page_count='+str(self.max_bytes//4096));output.execute('CREATE TABLE result(row_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
                count=size=0;columns=[]
                with self.lock:entry['phase']='Executing read-only SQL'
                with self._executing(entry,db,deadline,denied):
                    results=self._results(entry,db,deadline,dependencies);columns=next(results)
                    for row in results:
                        count+=1;payload=encoded(row);size+=len(payload.encode());require(size<=self.max_bytes,'QUERY_LIMIT','Query result spool exceeds 64 MiB',409)
                        output.execute('INSERT INTO result VALUES(?,?)',(count-1,payload))
                        if count%128==0:
                            with self.lock:entry['rows_processed']=count
                    output.commit();self._checkpoint(entry,deadline)
                disk=spool.stat().st_size;require(disk<=self.max_bytes,'QUERY_LIMIT','Query result file exceeds 64 MiB',409)
                return dict(columns=columns,row_count=count,value_count=count*len(columns),spool_bytes=disk,encoded_bytes=size,snapshot_bytes=snapshot_bytes,snapshot_seconds=snapshot_seconds,execution_seconds=time.monotonic()-execution_started,
                    dependencies=[dict(table=key,columns=sorted(value)) for key,value in sorted(dependencies.items())],
                    report=['Read-only SQLite result; aliases do not imply declared types. Large integers use tagged decimal strings. No partial results are published.'])
        finally:
            for path in (snapshot,Path(str(snapshot)+'-wal'),Path(str(snapshot)+'-shm')):
                path.unlink(missing_ok=True)

    def _run(self,job_id,entry):
        try:
            result=self._execute(entry)
            publication_deadline=time.monotonic()+2
            with owned(self.book,entry['cancel'],publication_deadline):
                with sql_deadline(self.book.db,publication_deadline,cancel_event=entry['cancel']):
                    self.book.refresh(deadline=publication_deadline);require(self.book.state()==entry['state'],'STALE_STATE','Query source changed before publication',409)
                with self.lock:
                    self._checkpoint(entry,float('inf'));require(not self.stopping,'CANCELLED','Query manager closed')
                    entry.update(status='completed',phase='Complete frozen result',result=result,rows_processed=result['row_count'])
        except EngineError as error:
            with self.lock:entry.update(status='cancelled' if error.body['code']=='CANCELLED' else 'stale' if error.body['code']=='STALE_STATE' else 'failed',phase='No result published',error=error.body)
        except Exception as error:
            with self.lock:entry.update(status='failed',phase='No result published',error=dict(code='QUERY_STORAGE_FAILED',message=str(error)[:512],outcome='rejected'))
        finally:
            with self.lock:
                entry['finished']=time.monotonic()
                if entry['status']!='completed' or self.stopping:self._cleanup(entry)

    @staticmethod
    def _view(job_id,entry):
        return dict(job_id=job_id,**{key:entry[key] for key in ('status','phase','rows_processed','state','generation','result','error') if key in entry})

    def access(self,job_id,action=None):
        if action in ('cancel','release'):
            with self.lock:
                self._expire();entry=self.entries.get(job_id);require(entry is not None,'JOB_NOT_FOUND','Query job expired or is unavailable after restart',404)
                if action=='cancel':
                    entry['cancel'].set()
                    if entry['status']=='completed':entry.pop('result',None);entry.update(status='cancelled',phase='Discarded');self._cleanup(entry)
                else:
                    require(entry['status']!='running','JOB_RUNNING','Cancel and wait before releasing query storage',409)
                    self.entries.pop(job_id);self._cleanup(entry);return dict(job_id=job_id,released=True)
                return self._view(job_id,entry)
        with self.book.lock:
            error=None
            try:self.book.refresh()
            except EngineError as failure:error=failure.body
            with self.lock:
                self._expire();entry=self.entries.get(job_id);require(entry is not None,'JOB_NOT_FOUND','Query job expired or is unavailable after restart',404)
                response=self._view(job_id,entry)
                if error is not None or entry['state']!=self.book.state() or entry['generation']!=self.generation:
                    response.pop('result',None)
                    if response['status']=='completed':response['status']='stale';response['phase']='Source or query generation changed; rerun explicitly'
                    if error is not None:response['source_error']=error
                return response

    def page(self,job_id,args):
        deadline=time.monotonic()+2
        with self.book.lock:
            with sql_deadline(self.book.db,deadline):
                status=self.access(job_id);require(status['status']=='completed','STALE_RESULT' if status['status']=='stale' else 'QUERY_NOT_READY','Only complete, current query results can be paged',409)
                require(args.get('expected_state')==status['state'],'STALE_STATE','Review this query result state',409)
                with self.lock:
                    entry=self.entries[job_id];result=entry['result'];known={col['id']:col for col in result['columns']};requested=args.get('columns',list(known)[:16])
                    require(isinstance(requested,list) and 1<=len(requested)<=256 and all(isinstance(key,str) and key in known for key in requested) and len(requested)==len(set(requested)),'INVALID_ARGUMENT','Choose unique result column ordinals')
                    limit=integer(args.get('limit',min(256,4096//len(requested))),1,min(256,4096//len(requested)),'limit');offset=integer(args.get('offset',0),0,result['row_count'],'offset');preview=boolean(args.get('preview',False),'preview')
                    rows=[];size=0
                    with closing(sqlite3.connect(Path(entry['directory'].name)/'result.sqlite')) as db:
                        with sql_deadline(db,deadline):
                            for row_id,payload in db.execute('SELECT row_id,payload FROM result WHERE row_id>=? ORDER BY row_id LIMIT ?',(offset,limit)):
                                stored=json.loads(payload);values=[];omitted=[]
                                for key in requested:
                                    value=stored[int(key)]
                                    if preview and isinstance(value,str) and len(value)>128:value=value[:128];omitted.append(key)
                                    values.append(value)
                                row=dict(row=row_id,values=values,omitted_columns=omitted);size+=len(encoded(row).encode());require(size<=3*1024*1024,'READ_LIMIT','Query page exceeds 3 MiB; reduce rows/columns');rows.append(row)
                    response=dict(job_id=job_id,state=status['state'],generation=status['generation'],columns=[known[key] for key in requested],rows=rows,row_count=result['row_count'],next_offset=offset+len(rows) if offset+len(rows)<result['row_count'] else None)
                    require(len(encoded(response).encode())<=4*1024*1024,'READ_LIMIT','Query response exceeds 4 MiB');return response

    def close(self):
        with self.lock:
            self.stopping=True;entries=list(self.entries.values());runs=list(self.runs)
            for entry in entries:entry['cancel'].set()
        for entry in entries:entry['worker'].join(timeout=2)
        for done in runs:done.wait(timeout=2)
        with self.lock:
            for key,entry in list(self.entries.items()):
                if not entry['worker'].is_alive():self.entries.pop(key);self._cleanup(entry)
