# SPDX-License-Identifier: GPL-3.0-only
"""Validate and execute durable-job API examples in an isolated test environment."""
from pathlib import Path
import base64
import copy
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
from datahorizon.client import LocalService,APIError
from datahorizon import service as service_module
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


FIXTURES=Path(__file__).resolve().parent/'fixtures'
# Every route of the large XLSX/ODS path, with the request schema it takes and the 200 response schema it returns.
LARGE_ROUTES={
    ('/v1/uploads/{upload}','put'):(None,'UploadReceipt'),
    ('/v1/uploads/{upload}/release','post'):('JobAction','UploadReleased'),
    ('/v1/import/xlsx/jobs','post'):('XlsxPreparationRequest','SpreadsheetPreparationJob'),
    ('/v1/import/ods/jobs','post'):('OdsPreparationRequest','SpreadsheetPreparationJob'),
    ('/v1/jobs/{job}','get'):(None,'PreparationJob'),
    ('/v1/jobs/{job}/cancel','post'):('JobAction','PreparationJob'),
    ('/v1/jobs/{job}/release','post'):('JobAction','JobReleased'),
    ('/v1/workbooks/{workbook}/operations','post'):('OperationRequest','Receipt'),
    ('/v1/workbooks/{workbook}/export/xlsx/jobs','post'):('XlsxExportJobRequest','ExportJob'),
    ('/v1/workbooks/{workbook}/export/ods/jobs','post'):('OdsExportJobRequest','ExportJob'),
    ('/v1/workbooks/{workbook}/export/xlsx','post'):('SpreadsheetExport','SpreadsheetExportResult'),
    ('/v1/workbooks/{workbook}/export/ods','post'):('SpreadsheetExport','SpreadsheetExportResult'),
    ('/v1/exports/{export}','get'):(None,None),
    ('/v1/exports/{export}/release','post'):('JobAction','ExportReleased'),
}


class LargeInterchangeOpenApiTests(unittest.TestCase):
    """Runs without the optional validators too: structure is always checked, and validation is added when they are installed."""
    def setUp(self):self.spec=openapi();self.schemas=self.spec['components']['schemas']
    def validate(self,name,value):
        if OAS30Validator is not None:OAS30Validator({**self.schemas[name],'components':self.spec['components']}).validate(value)
    def finish(self,client,view):
        for _ in range(2000):
            if view['status']!='running':break
            time.sleep(.005);view=client.preparation(view['job_id'])
        self.validate('PreparationJob',view);return view

    def test_every_large_route_has_request_and_response_schemas(self):
        for (path,method),(request,response) in LARGE_ROUTES.items():
            with self.subTest(path):
                operation=self.spec['paths'][path][method]
                if request:self.assertEqual(operation['requestBody']['content']['application/json']['schema']['$ref'],'#/components/schemas/'+request)
                if response:self.assertEqual(operation['responses']['200']['content']['application/json']['schema']['$ref'],'#/components/schemas/'+response)
        self.assertEqual(set(self.spec['paths']['/v1/uploads/{upload}']['put']['requestBody']['content']),{'application/octet-stream'})
        self.assertEqual(set(self.spec['paths']['/v1/exports/{export}']['get']['responses']['200']['content']),
                         {'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','application/vnd.oasis.opendocument.spreadsheet'})
        for name in ('ReportEntry','ReportTotals','StagedImportSummary','SpreadsheetImportPreview','ExportJobResult','PreparationJob',
                     'SpreadsheetPreparationJob','ExportJob','SpreadsheetExportResult','UploadReceipt','UploadReleased','ExportReleased',
                     'XlsxUploadPreparation','XlsxPathPreparation','OdsUploadPreparation','OdsPathPreparation','SpreadsheetPreparation',
                     'XlsxExportJobRequest','OdsExportJobRequest'):
            with self.subTest(name):
                self.assertIn('example',self.schemas[name]);self.validate(name,self.schemas[name]['example'])
        for name in ('StagedImportSummary','SpreadsheetImportPreview','ExportJobResult'):
            self.assertEqual(self.schemas[name]['properties']['report_entries']['items'],{'$ref':'#/components/schemas/ReportEntry'})
            self.assertEqual(self.schemas[name]['properties']['totals'],{'$ref':'#/components/schemas/ReportTotals'})
        self.assertIn('imported',self.schemas['Receipt']['properties']);self.assertIn('conflicts',self.schemas['Error']['properties'])
        self.assertIn('<option>PUT</option>',service_module.EXPLORER)

    def test_large_route_examples_execute(self):
        schemas=self.schemas
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            temp=Path(temp)
            # Upload and release, with the exact example body.
            body=self.spec['paths']['/v1/uploads/{upload}']['put']['requestBody']['content']['application/octet-stream']['example']
            (temp/'example.bin').write_bytes(body.encode())
            receipt=client.upload(temp/'example.bin',schemas['UploadReceipt']['example']['upload_id'])
            self.assertEqual(receipt,schemas['UploadReceipt']['example']);self.validate('UploadReceipt',receipt)
            self.assertEqual(client.release_upload(receipt['upload_id']),schemas['UploadReleased']['example'])
            for fmt,label in (('xlsx','Xlsx'),('ods','Ods')):
                fixture=FIXTURES/('fidelity.'+fmt)
                upload=copy.deepcopy(schemas[label+'UploadPreparation']['example']);client.upload(fixture,upload['upload_id'])
                path=copy.deepcopy(schemas[label+'PathPreparation']['example']);path['source_path']=str(fixture.resolve())
                inline=copy.deepcopy(schemas['SpreadsheetPreparation']['example']);inline.update(job_id='example-'+fmt+'-inline',file_base64=base64.b64encode(fixture.read_bytes()).decode())
                for request,result in ((upload,'StagedImportSummary'),(path,'StagedImportSummary'),(inline,'SpreadsheetImportPreview')):
                    with self.subTest(fmt=fmt,request=result):
                        self.validate(label+'PreparationRequest',request)
                        view=client.request('/v1/import/'+fmt+'/jobs',request);self.validate('SpreadsheetPreparationJob',view)
                        view=self.finish(client,view);self.assertEqual(view['status'],'completed',view)
                        self.validate('SpreadsheetPreparationJob',view);self.validate(result,view['result'])
                        self.assertEqual(set(view['result'])>=set(schemas[result]['required']),True)
                        self.validate('JobReleased',client.preparation(request['job_id'],'release'))
                client.release_upload(upload['upload_id'])
            # The staged workbook_import example, then both export job examples, the download and the release.
            staged=next(variant for variant in schemas['WorkbookImportOperation']['oneOf'] if variant['title']=='Prepared stage')['example']
            summary=client.prepare_spreadsheet_file(FIXTURES/'fidelity.xlsx',job_id=staged['stage_id'])
            with client.open(temp/'examples.dhwb',create=True) as book:
                request={**copy.deepcopy(staged),'stage_digest':summary['stage_digest'],'expected_state':book.state,'operation_id':str(uuid.uuid4())}
                self.validate('OperationRequest',request)
                receipt=client.request(book.base+'/operations',request,timeout=330);self.validate('Receipt',receipt)
                self.assertEqual(receipt['imported'],{'sheets':3,'cells':49})
                for fmt,label in (('xlsx','Xlsx'),('ods','Ods')):
                    bounded=client.request(book.base+'/export/'+fmt,{'scope':'workbook'});self.validate('SpreadsheetExportResult',bounded)
                    request=copy.deepcopy(schemas[label+'ExportJobRequest']['example'])
                    view=client.request(book.base+'/export/'+fmt+'/jobs',request);self.validate('ExportJob',view)
                    view=self.finish(client,view);self.assertEqual(view['status'],'completed',view)
                    self.validate('ExportJob',view);self.validate('ExportJobResult',view['result'])
                    written=client.download(view['result']['export_id'],temp/('example.'+fmt),expected=view['result'])
                    self.assertEqual(written['sha256'],view['result']['sha256'])
                    released=client.release_export(view['result']['export_id']);self.validate('ExportReleased',released)
                    if fmt=='xlsx':self.assertEqual(released,schemas['ExportReleased']['example'])


# Every chart route, with the request schema it takes and the 200 response schema it returns.
CHART_ROUTES={
    ('/v1/workbooks/{workbook}/charts','post'):('ChartsReadRequest','ChartCatalog'),
    ('/v1/workbooks/{workbook}/charts/suggest','post'):('ChartSuggestRequest','ChartSuggestion'),
    ('/v1/workbooks/{workbook}/charts/jobs','post'):('ChartJobRequest','ChartJob'),
    ('/v1/workbooks/{workbook}/charts/jobs/{job}','get'):(None,'ChartJob'),
    ('/v1/workbooks/{workbook}/charts/jobs/{job}/cancel','post'):('JobAction','ChartJob'),
    ('/v1/workbooks/{workbook}/charts/jobs/{job}/release','post'):('JobAction','JobReleased'),
}


class ChartJobOpenApiTests(unittest.TestCase):
    """Runs without the optional validators too: structure is always checked, and validation is added when they are installed."""
    def setUp(self):self.spec=openapi();self.schemas=self.spec['components']['schemas']
    def validate(self,name,value):
        if OAS30Validator is not None:OAS30Validator({**self.schemas[name],'components':self.spec['components']}).validate(value)

    def test_every_chart_route_has_request_and_response_schemas(self):
        for (path,method),(request,response) in CHART_ROUTES.items():
            with self.subTest(path):
                operation=self.spec['paths'][path][method]
                if request:self.assertEqual(operation['requestBody']['content']['application/json']['schema']['$ref'],'#/components/schemas/'+request)
                self.assertEqual(operation['responses']['200']['content']['application/json']['schema']['$ref'],'#/components/schemas/'+response)
                self.assertIn(response,self.schemas)
        for name in ('ChartSource','ChartSpec','ChartSuggestRequest','ChartJobRequest','ChartJob'):
            with self.subTest(name):self.assertIn('example',self.schemas[name]);self.validate(name,self.schemas[name]['example'])

    def test_chart_route_examples_execute(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client,client.open(Path(temp)/'charts.dhwb',create=True) as book:
            for row in range(1,14):
                book.set(row,1,'Month' if row==1 else 'M'+str(row));book.set(row,2,'Units' if row==1 else str(row));book.set(row,3,'Cost' if row==1 else '=B'+str(row)+'*2')
            book.refresh();suggest={**copy.deepcopy(self.schemas['ChartSuggestRequest']['example']),'expected_state':book.state}
            self.validate('ChartSuggestRequest',suggest)
            suggestion=client.request(book.base+'/charts/suggest',suggest);self.validate('ChartSuggestion',suggestion)
            self.assertEqual(len(suggestion['spec']['series']),2)
            receipt=client.request(book.base+'/operations',{'action':'chart_create','operation_id':str(uuid.uuid4()),'expected_state':book.state,'spec':suggestion['spec']})
            book.refresh();catalog=client.request(book.base+'/charts',{'expected_state':book.state});self.validate('ChartCatalog',catalog)
            request={**copy.deepcopy(self.schemas['ChartJobRequest']['example']),'expected_state':book.state,'chart_id':receipt['chart_id']}
            self.validate('ChartJobRequest',request)
            view=client.request(book.base+'/charts/jobs',request);self.validate('ChartJob',view)
            deadline=time.monotonic()+30
            while view['status']=='running':
                self.assertLess(time.monotonic(),deadline);time.sleep(.01);view=client.request(book.base+'/charts/jobs/'+view['job_id'])
            self.assertEqual(view['status'],'completed',view);self.validate('ChartJob',view)
            self.assertEqual((view['result']['width'],view['result']['height']),(480,300))
            released=client.request(book.base+'/charts/jobs/'+view['job_id']+'/release',{});self.validate('JobReleased',released)
            self.assertEqual(released,{'released':True,'job_id':request['job_id']})
