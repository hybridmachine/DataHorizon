"""Disk-staged, explicitly reviewed jobs with durable per-batch receipts."""
from contextlib import closing,contextmanager
import csv
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .engine import Workbook,EngineError,encoded,require,integer,boolean,typed,validate_style,rectangle,display,sql_deadline
from .temporal import Temporal,temporal_key,utc_clock
from .csv_import import preview_csv,ENCODINGS

JOURNAL='''CREATE TABLE meta(singleton INTEGER PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE cells(seq INTEGER PRIMARY KEY,row INTEGER,col INTEGER,payload TEXT NOT NULL,UNIQUE(row,col));
CREATE TABLE attempts(id TEXT PRIMARY KEY,start INTEGER,count INTEGER,request TEXT NOT NULL,result TEXT);
CREATE TABLE results(seq INTEGER PRIMARY KEY,payload TEXT NOT NULL,k0r,k0v,k1r,k1v,k2r,k2v,k3r,k3v,k4r,k4v,k5r,k5v,k6r,k6v,k7r,k7v);'''


class OperationJobs:
    def __init__(self,book):
        self.book=book;self.root=book.path.parent/('.'+book.path.name+'.dhjobs');self.lock=threading.RLock();self.active={};self.stopping=False

    def path(self,identifier):
        require(isinstance(identifier,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',identifier),'INVALID_JOB','Job ID needs 1–64 ASCII letters/digits/_/-')
        return self.root/(identifier+'.sqlite')

    @contextmanager
    def journal(self,identifier):
        path=self.path(identifier);require(path.is_file(),'JOB_NOT_FOUND','Operation journal is unavailable',404)
        with closing(sqlite3.connect(path,timeout=1)) as db:
            meta=json.loads(db.execute('SELECT payload FROM meta WHERE singleton=1').fetchone()[0])
            require(meta.get('version')==1 and meta.get('workbook')==self.book.id and meta.get('identity')==list(self.book.file_identity),'JOB_CONFLICT','Journal belongs to another native file',409)
            yield db,meta

    @staticmethod
    def save(db,meta):db.execute('UPDATE meta SET payload=? WHERE singleton=1',(encoded(meta),))

    def create(self,request):
        require(isinstance(request,dict) and len(encoded(request).encode())<=65536,'INVALID_JOB','Job options must be an object of at most 64 KiB')
        batch_size=integer(request.get('batch_size',1000),1,1000,'batch_size')
        if 'source_path' in request:
            require(isinstance(request['source_path'],str) and bool(request['source_path']),'INVALID_JOB','CSV source_path must be nonempty Text')
            integer(request.get('row',1),1,1048576,'row');integer(request.get('col',1),1,16384,'col');boolean(request.get('header',False),'header')
        identifier=request.get('job_id');path=self.path(identifier);kind=request.get('action','edit');mode=request.get('mode','atomic')
        require(kind in ('edit','find','list') and mode in ('atomic','batches'),'INVALID_JOB','Choose edit/find/list and atomic/batches')
        with self.book.lock:
            self.book.refresh();require(request.get('expected_state')==self.book.state(),'STALE_STATE','Review current job targets',409)
            sheet=self.book.sheet(request.get('sheet','Sheet1'));state=self.book.state()
        scope_count=0
        if kind!='edit':
            r1,c1,r2,c2=rectangle(request,100000);scope_count=(r2-r1+1)*(c2-c1+1);require(request.get('c2',request.get('c1',1))-request.get('c1',1)+1<=256,'LIMIT','Large read jobs support at most 256 source columns')
            if kind=='find':require(isinstance(request.get('needle'),str) and bool(request['needle']),'INVALID_JOB','Find needs nonempty Text')
        fingerprint=hashlib.sha256(encoded(request).encode()).hexdigest()
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        with self.lock:
            require(not self.stopping,'SERVICE_STOPPING','Service is stopping',503)
            existing=path.exists()
            if existing:
                with self.journal(identifier) as (_,meta):require(meta['fingerprint']==fingerprint,'JOB_CONFLICT','Job ID has different inputs',409)
            else:
                require(len(list(self.root.glob('*.sqlite')))<4,'JOB_LIMIT','Release an old operation journal first',409)
                fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
                meta=dict(version=1,job_id=identifier,workbook=self.book.id,identity=list(self.book.file_identity),fingerprint=fingerprint,action=kind,mode=mode,
                          sheet=sheet,expected_state=state,status='staging',phase='Staging',total=scope_count,bytes=0,next=0,committed_batches=0,attempt=0,plan_digest=None,
                          batch_size=batch_size,options=request,clock=utc_clock().isoformat(),processed=0,matches=0)
                with closing(sqlite3.connect(path)) as db,db:db.executescript(JOURNAL);db.execute('INSERT INTO meta VALUES(1,?)',(encoded(meta),))
        if existing:return self.view(identifier)
        if kind!='edit':return self.seal(identifier)
        if 'source_path' in request:self.launch(identifier,self.stage_csv)
        return self.view(identifier)

    def append(self,identifier,cells):
        require(isinstance(cells,list) and 0<len(cells)<=1000,'LIMIT','Upload 1–1,000 staged cells')
        prepared=[];size=0
        for item in cells:
            require(isinstance(item,dict) and not set(item)-{'row','col','source','kind','style','clear','clear_input','replace_style'},'INVALID_JOB','Unsupported staged edit field')
            integer(item.get('row'),1,1048576,'row');integer(item.get('col'),1,16384,'col')
            for flag in ('clear','clear_input','replace_style'):boolean(item.get(flag,False),flag)
            if 'source' in item:typed(dict(source=item['source'],kind=item.get('kind','auto'),style=item.get('style',{})))
            if 'style' in item:validate_style(item['style'])
            payload=encoded(item);size+=len(payload.encode());prepared.append((item,payload))
        require(size<=1024*1024,'LIMIT','Upload chunks are at most 1 MiB')
        with self.lock,self.journal(identifier) as (db,meta),db:
            require(meta['status']=='staging' and meta['action']=='edit','JOB_SEALED','Only staging edit jobs accept data',409)
            require(meta['total']+len(cells)<= (10000 if meta['mode']=='atomic' else 100000) and meta['bytes']+size<=64*1024*1024,'LIMIT','Job exceeds its declared cell/64-MiB stage budget')
            try:
                db.executemany('INSERT INTO cells VALUES(?,?,?,?)',[(meta['total']+offset,item['row'],item['col'],payload) for offset,(item,payload) in enumerate(prepared)])
            except sqlite3.IntegrityError as error:raise EngineError('DUPLICATE_TARGET','Staged coordinates must be unique') from error
            meta['total']+=len(cells);meta['bytes']+=size;self.save(db,meta)
        return self.view(identifier)

    def seal(self,identifier):
        with self.lock,self.journal(identifier) as (db,meta),db:
            require(meta['status']=='staging','JOB_SEALED','Job is not staging',409)
            require('source_path' not in meta['options'] or meta.get('source_complete'),'JOB_STAGING','CSV staging must finish before review',409)
            require((meta['total']+meta['batch_size']-1)//meta['batch_size']<=1000,'LIMIT','At most 1,000 explicit batches per job')
            if meta['mode']=='atomic':require(meta['bytes']<=3*1024*1024,'LIMIT','Atomic job payload exceeds the ordinary request budget')
            digest=hashlib.sha256(encoded({k:meta[k] for k in ('action','mode','sheet','options','total')}).encode())
            for (payload,) in db.execute('SELECT payload FROM cells ORDER BY seq'):digest.update(payload.encode());digest.update(b'\n')
            meta.update(plan_digest=digest.hexdigest(),status='prepared',phase='Ready for explicit review');self.save(db,meta)
        return self.view(identifier)

    def view(self,identifier,offset=0,limit=100):
        integer(offset,0,100000,'offset');integer(limit,1,256,'limit')
        with self.lock:active=identifier in self.active
        with self.journal(identifier) as (db,meta):
            result={k:v for k,v in meta.items() if k not in ('options','fingerprint','identity')}
            if not active and (meta['status']=='running' or (meta['status']=='staging' and 'source_path' in meta['options'])):result['status']='interrupted'
            result['batches']=[dict(operation_id=i,start=s,count=c,receipt=json.loads(r) if r else None) for i,s,c,r in db.execute('SELECT id,start,count,result FROM attempts ORDER BY rowid')]
            result['plan']={k:v for k,v in meta['options'].items() if k not in ('job_id','expected_state')}
            result['preview']=[json.loads(p) for (p,) in db.execute('SELECT payload FROM cells ORDER BY seq LIMIT 8')]
            if meta['action']!='edit' and meta['status']=='completed':
                order=meta.get('order','seq')
                with sql_deadline(db,time.monotonic()+2):result['results']=[json.loads(p) for (p,) in db.execute('SELECT payload FROM results ORDER BY '+order+' LIMIT ? OFFSET ?',(limit,offset))]
                result['offset']=offset;result['has_more']=offset+limit<meta['matches']
            result['report']=['Staging never changes workbook inputs. Atomic edits use one ordinary transaction; explicit batches have separate durable receipts.',
                              'Cancellation retains committed batches. Unknown admitted writes are reconciled from native receipts, never automatically retried.',
                              'CSV stages default to Text unless explicit column types/profile were supplied. Read results are frozen snapshots with the recorded UTC clock.']
        if 'results' in result:
            with self.book.lock:
                require(not self.stopping,'SERVICE_STOPPING','Service is stopping',503)
                self.book.refresh();current=self.book.state()==meta['expected_state']
            if not current:result.pop('results');result.update(status='stale',phase='Input snapshot changed; results withheld')
        require(len(encoded(result).encode())<=4*1024*1024,'READ_LIMIT','Job page exceeds 4 MiB');return result

    def launch(self,identifier,function):
        with self.lock:
            require(not self.stopping and not any(t.is_alive() for _,t in self.active.values()),'JOB_BUSY','Another operation job is running',409)
            cancel=threading.Event()
            def run():
                try:function(identifier,cancel)
                except BaseException as error:
                    with self.journal(identifier) as (db,meta),db:
                        body=error.body if isinstance(error,EngineError) else dict(code='JOB_FAILED',message=str(error),outcome='rejected')
                        meta.update(status='cancelled' if body.get('code')=='CANCELLED' else 'failed',phase='Stopped; inspect batch receipts',error=body);self.save(db,meta)
                finally:
                    with self.lock:self.active.pop(identifier,None)
            thread=threading.Thread(target=run,name='DataHorizonOperationJob',daemon=True);self.active[identifier]=(cancel,thread);thread.start()

    @staticmethod
    def checkpoint(cancel,deadline):
        require(not cancel.is_set(),'CANCELLED','Job cancellation confirmed; committed batches remain committed',409)
        require(time.monotonic()<=deadline,'TIMEOUT','Job exceeded its execution budget')

    def stage_csv(self,identifier,cancel):
        with self.journal(identifier) as (_,meta):options=meta['options']
        source=Path(options['source_path']).expanduser().resolve();require(source.is_file() and source.stat().st_size<=64*1024*1024,'LIMIT','CSV source must be a file of at most 64 MiB')
        encoding=options.get('encoding','utf-8-sig');require(encoding in ENCODINGS,'INVALID_CSV_ENCODING','Unsupported CSV encoding')
        delimiter=options.get('delimiter',',');require(isinstance(delimiter,str) and len(delimiter)==1 and delimiter not in '\r\n\0"','INVALID_CSV','Invalid CSV delimiter')
        deadline=time.monotonic()+300;total_bytes=0;row_offset=0;buffer=[];fields=0;width=None
        def flush():
            nonlocal row_offset,buffer,fields
            output=io.StringIO(newline='');csv.writer(output,delimiter=delimiter).writerows(buffer)
            text=output.getvalue();require(len(text.encode())<=1024*1024,'LIMIT','CSV staging row/chunk exceeds 1 MiB')
            preview=preview_csv({k:v for k,v in options.items() if k in ('column_types','locale') }|dict(csv=text,delimiter=delimiter,header=options.get('header',False) and row_offset==0))
            require(preview['can_import'],'INVALID_CONVERSION','CSV conversion failed at staged row '+str(row_offset+1)+': '+encoded(preview['conversion_issues'][:4]))
            inputs=preview.get('inputs',[[dict(source=value,kind='text') for value in row] for row in buffer])
            cells=[dict(item,row=options.get('row',1)+row_offset+r,col=options.get('col',1)+c) for r,row in enumerate(inputs) for c,item in enumerate(row)]
            self.append(identifier,cells);row_offset+=len(buffer);buffer=[];fields=0
        with source.open('r',encoding=encoding,newline='') as stream:
            def lines():
                nonlocal total_bytes
                while True:
                    self.checkpoint(cancel,deadline);line=stream.readline(1024*1024+1)
                    if not line:return
                    count=len(line.encode());total_bytes+=count;require(count<=1024*1024 and total_bytes<=64*1024*1024,'LIMIT','CSV decoded line/total exceeds 1/64 MiB');yield line
            for row in csv.reader(lines(),delimiter=delimiter,strict=True):
                if width is None:width=len(row)
                require(bool(width) and len(row)==width and width<=1000,'INVALID_CSV','Large CSV staging requires rectangular rows of at most 1,000 fields')
                if fields+len(row)>1000:flush()
                buffer.append(row);fields+=len(row)
                if fields==1000:flush()
            if buffer:flush()
        with self.journal(identifier) as (db,meta),db:meta['source_complete']=True;self.save(db,meta)
        self.seal(identifier)

    def recover(self,identifier):
        with self.journal(identifier) as (db,meta),db:
            for operation,start,count,request in db.execute('SELECT id,start,count,request FROM attempts WHERE result IS NULL ORDER BY rowid').fetchall():
                try:receipt=self.book.recorded_operation(operation,hashlib.sha256(request.encode()).hexdigest())
                except EngineError as error:receipt=error.body
                if receipt is None or receipt.get('outcome') not in ('committed','rolled_back') or receipt.get('operation_id')!=operation:meta.update(status='unknown',phase='Missing receipt is not proof of rollback',pending_operation=operation);self.save(db,meta);return False
                db.execute('UPDATE attempts SET result=? WHERE id=?',(encoded(receipt),operation))
                if receipt['outcome']=='committed':meta['next']=max(meta['next'],start+count);meta['committed_batches']+=1
            self.save(db,meta);return True

    def start(self,identifier,request,resume=False):
        with self.lock:require(not self.stopping and not self.active,'JOB_BUSY','Wait for the active operation job to stop before execution',409)
        with self.book.lock:
            self.book.refresh()
            require(self.recover(identifier),'OUTCOME_UNKNOWN','Reconcile the pending native operation before resuming',409)
            with self.journal(identifier) as (db,meta),db:
                require(db.execute('SELECT count(*) FROM attempts').fetchone()[0]<1000,'LIMIT','Job has exhausted 1,000 admitted attempts')
                require(request.get('expected_state')==self.book.state() and request.get('plan_digest')==meta['plan_digest'] and meta['plan_digest'] is not None,'STALE_PLAN','Review the sealed plan and current workbook before execution',409)
                require(meta['status'] in (('prepared','cancelled','failed','interrupted','stale','running') if resume else ('prepared',)),'INVALID_JOB_STATE','Use explicit resume for partially executed jobs',409)
                require(resume or meta['expected_state']==self.book.state(),'STALE_STATE','Inputs changed during staging; review and explicitly resume',409)
                previous=meta['status'];meta.update(status='running',phase='Executing',expected_state=self.book.state(),attempt=meta['attempt']+1);self.save(db,meta)
            try:self.launch(identifier,self.execute)
            except EngineError:
                with self.journal(identifier) as (db,meta),db:meta['status']=previous;self.save(db,meta)
                raise
        return self.view(identifier)

    def committed(self,db,meta,operation,receipt,count):
        db.execute('UPDATE attempts SET result=? WHERE id=?',(encoded(receipt),operation));meta['next']+=count;meta['committed_batches']+=1;meta['expected_state']=receipt['state'];meta['processed']=meta['next'];self.save(db,meta)

    def execute(self,identifier,cancel):
        with self.journal(identifier) as (_,meta):kind=meta['action']
        if kind!='edit':return self.read_job(identifier,cancel)
        deadline=time.monotonic()+300
        while True:
            with self.book.lock,self.journal(identifier) as (db,meta):
                if meta['next']>=meta['total']:
                    with db:meta.update(status='completed',phase='All declared batches committed');self.save(db,meta)
                    return
                self.checkpoint(cancel,deadline)
                self.book.refresh();require(meta['expected_state']==self.book.state(),'STALE_STATE','Inputs changed between batches; committed receipts are retained',409)
                count=meta['total'] if meta['mode']=='atomic' else meta['batch_size']
                cells=[json.loads(payload) for (payload,) in db.execute('SELECT payload FROM cells WHERE seq>=? ORDER BY seq LIMIT ?',(meta['next'],count))]
                require(db.execute('SELECT count(*) FROM attempts').fetchone()[0]<1000,'LIMIT','At most 1,000 admitted attempts per job')
                operation='job-'+identifier+'-'+str(meta['next'])+'-'+str(meta['attempt'])
                request=dict(action='edit',operation_id=operation,expected_state=self.book.state(),sheet=meta['sheet'],cells=cells,label='Operation job '+identifier)
                self.checkpoint(cancel,deadline)
                with db:db.execute('INSERT INTO attempts VALUES(?,?,?,?,NULL)',(operation,meta['next'],len(cells),encoded(request)))
                try:receipt=self.book.mutate(request,cancel_event=cancel)
                except EngineError as error:
                    with db:
                        unknown=error.body.get('outcome')=='unknown'
                        if not unknown:db.execute('UPDATE attempts SET result=? WHERE id=?',(encoded(error.body),operation))
                        meta.update(status='unknown' if unknown else 'cancelled' if error.body.get('code')=='CANCELLED' else 'failed',error=error.body,phase='Stopped; committed batches retained');self.save(db,meta)
                    return
                with db:self.committed(db,meta,operation,receipt,len(cells))

    def read_job(self,identifier,cancel):
        with self.journal(identifier) as (_,meta):options=meta['options']
        destination=self.root/(identifier+'.snapshot-'+uuid.uuid4().hex+'.dhwb')
        with self.book.lock:
            self.checkpoint(cancel,time.monotonic()+60)
            self.book.refresh();require(self.book.state()==meta['expected_state'],'STALE_STATE','Read inputs changed before snapshot',409);self.book.snapshot(destination,cancel_event=cancel)
            self.book.refresh();require(self.book.state()==meta['expected_state'],'STALE_STATE','Read inputs changed during snapshot',409)
        deadline=time.monotonic()+60;matches=processed=size=0;clock=datetime.fromisoformat(meta['clock'])
        with closing(Workbook(destination,readonly=True)) as source,self.journal(identifier) as (db,meta):
            with db:db.execute('DELETE FROM results')
            r1,c1,r2,c2=rectangle(options,100000);keys=options.get('sort',[])
            require(isinstance(keys,list) and len(keys)<=8,'INVALID_ARGUMENT','At most eight sort keys')
            for key in keys:
                require(isinstance(key,dict),'INVALID_ARGUMENT','Sort keys must be objects');integer(key.get('col'),c1,c2,'sort column');boolean(key.get('descending',False),'descending')
            def add(payload,values=()):
                nonlocal size,matches
                text=encoded(payload);size+=len(text.encode());require(size<=64*1024*1024,'LIMIT','Read spool exceeds 64 MiB')
                columns=[]
                for value in values:
                    if value is None:pair=(0,0)
                    elif type(value) in (int,float):pair=(1,value)
                    elif isinstance(value,Temporal):family,number=temporal_key(value);pair=(2 if family=='calendar' else 3,number)
                    elif type(value) is str:pair=(4,value)
                    else:pair=(5,int(value))
                    columns.extend(pair)
                columns.extend([None]*(16-len(columns)));db.execute('INSERT INTO results VALUES('+','.join('?' for _ in range(18))+')',(matches,text,*columns));matches+=1
            if meta['action']=='find':
                field=options.get('field','source');require(field in ('source','display'),'INVALID_JOB','Find field must be source/display')
                calc=source.calculator(seconds=2,clock=clock,transactional=True)
                for row,col,raw,kind,style in source.db.execute('SELECT row,col,source,kind,style FROM cells WHERE sheet_id=? AND row BETWEEN ? AND ? AND col BETWEEN ? AND ? ORDER BY row,col',(meta['sheet'],r1,r2,c1,c2)):
                    self.checkpoint(cancel,deadline);processed+=1
                    if processed%256==0:calc=source.calculator(seconds=2,clock=clock,transactional=True)
                    require(len(raw.encode())<=65536,'READ_LIMIT','Find source exceeds 64 KiB')
                    text=raw if field=='source' else display(calc.cell(meta['sheet'],row,col),json.loads(style))
                    if options['needle'] in text:add(dict(row=row,col=col,text=text[:256]))
                    if processed%256==0:
                        meta.update(processed=processed,matches=matches);self.save(db,meta);db.commit()
            else:
                first=r1+int(options.get('header',False));stride=max(1,1000//(c2-c1+1))
                for start in range(first,r2+1,stride):
                    self.checkpoint(cancel,deadline);calc=source.calculator(seconds=2,clock=clock,transactional=True)
                    scope={**options,'r1':start,'r2':min(start+stride-1,r2),'header':False,'sort':[]}
                    result=source._list_rows(meta['sheet'],scope,calc,deadline)
                    for row in result['rows']:add(dict(row=row),[calc.cell(meta['sheet'],row,key['col']) for key in keys])
                    processed+=scope['r2']-start+1;meta.update(processed=processed,matches=matches);self.save(db,meta);db.commit()
            order=','.join('k'+str(index)+suffix+(' DESC' if key.get('descending',False) else ' ASC') for index,key in enumerate(keys) for suffix in ('r','v'))
            if order:
                with sql_deadline(db,deadline):db.execute('CREATE INDEX IF NOT EXISTS result_order ON results('+order+',seq)')
            meta.update(status='completed',phase='Frozen read snapshot ready',processed=processed,matches=matches,order=(order+',' if order else '')+'seq');self.checkpoint(cancel,deadline)
            with db:self.save(db,meta)

    def cancel(self,identifier):
        with self.lock:
            if identifier in self.active:self.active[identifier][0].set()
            else:
                with self.journal(identifier) as (db,meta),db:
                    if meta['status'] not in ('completed','unknown','running'):meta.update(status='cancelled',phase='Cancelled; committed receipts retained');self.save(db,meta)
        return self.view(identifier)

    def release(self,identifier):
        with self.lock:
            require(identifier not in self.active,'JOB_RUNNING','Cancel and wait before release',409)
            with self.journal(identifier) as (db,meta):require(meta['status'] not in ('running','unknown') and db.execute('SELECT count(*) FROM attempts WHERE result IS NULL').fetchone()[0]==0,'OUTCOME_UNKNOWN','Reconcile admitted operations before release',409)
            self.path(identifier).unlink()
            for file in self.root.glob(identifier+'.snapshot-*.dhwb'):file.unlink()
        return dict(released=True,job_id=identifier)

    def close(self):
        with self.lock:self.stopping=True;workers=list(self.active.values())
        for cancel,_ in workers:cancel.set()
        for _,thread in workers:thread.join(timeout=.1)
