"""Validate and execute durable-job API examples in an isolated test environment."""
from pathlib import Path
import copy
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
from datahorizon.client import LocalService,APIError
from datahorizon.service import openapi
try:
    from jsonschema.exceptions import ValidationError
    from openapi_schema_validator import OAS30Validator
    from openapi_spec_validator import validate as validate_spec
except ImportError:
    OAS30Validator=None


@unittest.skipIf(OAS30Validator is None,'Optional build-only OpenAPI validators unavailable')
class JobOpenApiTests(unittest.TestCase):
    def setUp(self):self.spec=openapi();self.schemas=self.spec['components']['schemas']
    def validate(self,name,value):
        OAS30Validator({**self.schemas[name],'components':self.spec['components']}).validate(value)
    def terminal(self,book,job,staging=False):
        for _ in range(1000):
            if job['status'] not in (('running','staging') if staging else ('running',)):break
            time.sleep(.005);job=book.operation_job(job['job_id'])
        self.validate('OperationJobStatus',job);return job
    def execute(self,book,job,action='start'):
        book.refresh();request=copy.deepcopy(self.schemas[action.title()+'JobRequest']['example'])
        request.update(expected_state=book.state,plan_digest=job['plan_digest'],execution_id=job['execution_id'])
        self.validate(action.title()+'JobRequest',request)
        result=book.operation_job(job['job_id'],action,**request)
        return self.terminal(book,result)

    def test_document_and_action_schemas_are_valid_openapi_30(self):
        validate_spec(self.spec)
        for schema in self.schemas.values():OAS30Validator.check_schema(schema)
        base='/v1/workbooks/{workbook}/operation-jobs'
        for action,name in (('append','AppendJobRequest'),('status','JobPageRequest'),('start','StartJobRequest'),('resume','ResumeJobRequest'),('cancel','CancelJobRequest'),('release','ReleaseJobRequest')):
            endpoint=self.spec['paths'][base+'/{job}/'+action]['post']
            self.assertTrue(endpoint['requestBody']['content']['application/json']['schema']['$ref'].endswith('/'+name))

    def test_edit_csv_find_and_list_examples_execute_through_rest(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/'api.dhwb',create=True) as book:
            csv=Path(temp)/'typed.csv';csv.write_text('10\n20\n')
            for name in ('EditJobRequest','CsvJobRequest','FindJobRequest','ListJobRequest'):
                book.refresh();request=copy.deepcopy(self.schemas[name]['example']);request.update(job_id=uuid.uuid4().hex,expected_state=book.state)
                if name=='CsvJobRequest':request['source_path']=str(csv)
                self.validate(name,request);self.validate('OperationJobRequest',request)
                job=client.request(book.base+'/operation-jobs',request)
                job=self.terminal(book,job,True);self.assertIn(job['status'],('staging','prepared'))
                if name=='EditJobRequest':
                    append=self.schemas['AppendJobRequest']['example'];self.validate('AppendJobRequest',append)
                    book.operation_job(job['job_id'],'append',**append)
                    job=book.operation_job(job['job_id'],'seal')
                result=self.execute(book,job);self.assertEqual(result['status'],'completed',result)
                self.validate('JobPageRequest',dict(offset=0,limit=2));self.validate('OperationJobStatus',book.operation_job(job['job_id'],offset=0,limit=2))
                released=book.operation_job(job['job_id'],'release');self.validate('JobReleased',released)

    def test_cancel_resume_and_lost_acknowledgement_use_inspection(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/'resume.dhwb',create=True) as book:
            job=book.create_job();book.operation_job(job['job_id'],'append',cells=[dict(row=1,col=1,source='once')]);job=book.operation_job(job['job_id'],'seal')
            cancellation=dict(execution_id=job['execution_id']);self.validate('CancelJobRequest',cancellation)
            self.assertEqual(book.operation_job(job['job_id'],'cancel',**cancellation)['status'],'cancelled')
            request=dict(expected_state=book.state,plan_digest=job['plan_digest'],execution_id=job['execution_id']);self.validate('ResumeJobRequest',request)
            original=client.request;executions=[]
            def lost(path,body=None):
                result=original(path,body)
                if path.endswith('/resume'):executions.append(path);raise OSError('lost execution acknowledgement')
                return result
            with patch.object(client,'request',lost):
                with self.assertRaises(OSError):book.operation_job(job['job_id'],'resume',**request)
            result=self.terminal(book,book.operation_job(job['job_id']))
            self.assertEqual(result['status'],'completed');self.assertEqual(len(executions),1);self.assertEqual(len(result['batches']),1)
            book.refresh();self.assertEqual(book.read(r2=1,c2=1)['cells'][0]['value'],'once')

    def test_schema_and_service_reject_missing_and_malformed_inputs(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/'bad.dhwb',create=True) as book:
            common=dict(job_id='invalid',expected_state=book.state)
            bad=[{**common,'action':'find'}, {**common,'action':'find','needle':True},
                 {**common,'action':'list','header':'true'}, {**common,'action':'list','sort':[dict(col=1,descending='false')]},
                 {**common,'action':'list','r1':1.5}, {**common,'source_path':'source','encoding':'invalid'},
                 {**common,'source_path':'source','column_types':{'1':'invalid'}}]
            for request in bad:
                with self.assertRaises(ValidationError):self.validate('OperationJobRequest',request)
                with self.assertRaises(APIError):client.request(book.base+'/operation-jobs',request)
            job=book.create_job();bad_append=dict(cells=[dict(row=1,col=1,clear='false')])
            with self.assertRaises(ValidationError):self.validate('AppendJobRequest',bad_append)
            with self.assertRaises(APIError):book.operation_job(job['job_id'],'append',**bad_append)
            with self.assertRaises(ValidationError):self.validate('StartJobRequest',dict(expected_state=book.state))
            with self.assertRaises(APIError):book.operation_job(job['job_id'],'start',expected_state=book.state)
            with self.assertRaises(ValidationError):self.validate('JobPageRequest',dict(limit=True))
            with self.assertRaises(APIError):book.operation_job(job['job_id'],limit=True)
