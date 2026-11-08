"""Read-only background calculations with input-state and generation guards."""
from contextlib import closing
import hashlib
from .engine import Workbook,encoded,require,rectangle,MAX_CELLS
from .jobs import PreparationJobs
from .temporal import utc_clock


class CalculationJobs:
    def __init__(self,book):
        self.book=book;self.jobs=PreparationJobs(seconds=10);self.generation=0;self.metadata={}

    def start(self,request):
        rectangle(request,MAX_CELLS)
        with self.book.lock:
            self.book.refresh();state=self.book.state()
            require(request.get('expected_state')==state,'STALE_STATE','Refresh before calculating',409)
            fingerprint=hashlib.sha256(encoded(request).encode()).hexdigest();job_id=request.get('job_id')
            self.metadata={key:value for key,value in self.metadata.items() if key in self.jobs.entries}
            if isinstance(job_id,str) and job_id in self.metadata:
                require(self.metadata[job_id]['fingerprint']==fingerprint,'JOB_CONFLICT','Calculation ID has different inputs',409)
                return self.access(job_id)
            previous=self.generation;self.generation+=1;generation=self.generation;clock=utc_clock()
            def calculate(payload,checkpoint):
                with closing(Workbook(self.book.path,readonly=True)) as snapshot:
                    base=snapshot.calculator
                    def calculator(seconds=.25,**kwargs):
                        calc=base(seconds=2,clock=clock,transactional=True)
                        check=calc.check
                        def checked():checkpoint(calc.count,'Calculating');check()
                        calc.check=checked
                        return calc
                    snapshot.calculator=calculator
                    checkpoint(0,'Reading snapshot')
                    result=snapshot.read(payload.get('sheet','Sheet1'),{**payload,'preview':False,'calculation':'fresh'},limit=MAX_CELLS)
                    result.update(state=state,generation=generation,clock=clock.isoformat(),calculation='error' if any(c['calculation']=='error' for c in result['cells']) else 'current')
                    return result
            try:
                response=self.jobs.start(request,calculate,'calculation')
            except Exception:self.generation=previous;raise
            self.metadata[job_id]=dict(state=state,generation=generation,clock=clock.isoformat(),fingerprint=fingerprint)
            return {**response,**{k:v for k,v in self.metadata[job_id].items() if k!='fingerprint'},'calculation':'pending'}

    def access(self,job_id,action=None):
        with self.book.lock:
            self.book.refresh();response=self.jobs.access(job_id,action);metadata=self.metadata[job_id]
            if action=='release':self.metadata.pop(job_id,None);return response
            current=metadata['state']==self.book.state() and metadata['generation']==self.generation
            if not current and response['status']=='completed':
                response.pop('result',None);response['status']='stale'
            response.update({k:v for k,v in metadata.items() if k!='fingerprint'})
            response['calculation']=response['result']['calculation'] if response['status']=='completed' else 'pending' if response['status']=='running' else response['status']
            return response

    def close(self):self.jobs.close()
