# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from datahorizon.client import LocalService,APIError
from datahorizon.service import openapi
try:
    from openapi_schema_validator import OAS30Validator
    from openapi_spec_validator import validate
except ImportError:OAS30Validator=None

CLI=[sys.executable,'-m','datahorizon.cli']


class NamesClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'names.dhwb'

    def test_sdk_rest_and_cli_agree(self):
        with LocalService() as client:
            with client.open(self.path,create=True) as book:
                receipt=book.save_name('Quantities','Sheet1!B2:B4')
                sdk=book.names();raw=client.request(book.base+'/names',dict(expected_state=sdk['state']))
                self.assertEqual(sdk,raw);self.assertEqual(sdk['names'][0]['reference'],"'Sheet1'!B2:B4")
                self.assertEqual(sdk['names'][0]['id'],receipt['name_id'])
                self.assertEqual(book.names('quantities')['names'][0]['name'],'Quantities')
        saved=subprocess.run([*CLI,str(self.path),'names','save'],input=json.dumps(dict(name='Totals',reference='Sheet1!C1')),capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(saved.stdout)['outcome'],'committed')
        read=json.loads(subprocess.run([*CLI,str(self.path),'names','read'],capture_output=True,text=True,check=True).stdout)
        self.assertEqual(sorted(item['name'] for item in read['names']),['Quantities','Totals'])
        with LocalService() as client,client.open(self.path) as book:
            self.assertEqual(book.names()['names'],read['names'])
        victim=next(item['id'] for item in read['names'] if item['name']=='Quantities')
        subprocess.run([*CLI,str(self.path),'names','delete',victim],capture_output=True,text=True,check=True)
        with LocalService() as client,client.open(self.path) as book:
            self.assertEqual([item['name'] for item in book.names()['names']],['Totals'])
            with self.assertRaises(APIError) as error:book.delete_name(victim)
            self.assertEqual(error.exception.body['code'],'NAME_NOT_FOUND')

    def test_capabilities(self):
        with LocalService() as client:
            capabilities=client.request('/v1/capabilities')
            self.assertTrue(capabilities['names']);self.assertEqual(capabilities['max_names'],256)
            self.assertIn('name_save',capabilities['operations'])

    @unittest.skipIf(OAS30Validator is None,'Optional build-only OpenAPI validators unavailable')
    def test_openapi_validates_and_examples_execute(self):
        document=openapi();validate(document);schemas=document['components']['schemas']
        for name in ('NameSaveOperation','NameDeleteOperation'):
            validator=OAS30Validator({**schemas[name],'components':document['components']});validator.validate(schemas[name]['example'])
            self.assertIs(schemas[name]['additionalProperties'],False)
        self.assertEqual(document['paths']['/v1/workbooks/{workbook}/names']['post']['requestBody']['content']['application/json']['schema']['$ref'],'#/components/schemas/NamesReadRequest')
        self.assertIn('broken_names',schemas['Receipt']['properties'])
        with LocalService() as client,client.open(self.path,create=True) as book:
            save=dict(schemas['NameSaveOperation']['example'],operation_id=str(uuid.uuid4()),expected_state=book.state)
            receipt=client.request(book.base+'/operations',save);book.state=receipt['state']
            catalog=client.request(book.base+'/names',dict(schemas['NamesReadRequest']['example'],expected_state=book.state))
            OAS30Validator({**schemas['NameCatalog'],'components':document['components']}).validate(catalog)
            delete=dict(schemas['NameDeleteOperation']['example'],name_id=catalog['names'][0]['id'],operation_id=str(uuid.uuid4()),expected_state=catalog['state'])
            client.request(book.base+'/operations',delete)

    def test_rename_without_reference_over_http(self):
        with LocalService() as client,client.open(self.path,create=True) as book:
            nid=book.save_name('Quantities','Sheet1!B2:B4')['name_id']
            body=dict(action='name_save',name='Amounts',name_id=nid,operation_id=str(uuid.uuid4()),expected_state=book.state)
            receipt=client.request(book.base+'/operations',body)
            self.assertEqual(receipt['outcome'],'committed');self.assertEqual(receipt['name_id'],nid);book.state=receipt['state']
            item=book.names()['names'][0];self.assertEqual((item['name'],item['reference']),('Amounts',"'Sheet1'!B2:B4"))
        schemas=openapi()['components']['schemas']
        self.assertEqual(schemas['NameSaveOperation']['required'],['operation_id','expected_state','action','name'])
        self.assertIn('retarget',schemas['NameSaveOperation']['properties']['reference']['description'])
        self.assertIn('name_id',schemas['Receipt']['properties'])

    @unittest.skipIf(OAS30Validator is None,'Optional build-only OpenAPI validators unavailable')
    def test_rename_payload_validates_against_schema(self):
        document=openapi();schemas=document['components']['schemas']
        rename=dict(action='name_save',name='Amounts',name_id='00000000-0000-4000-8000-000000000000',operation_id=str(uuid.uuid4()),expected_state='copy-from-workbook-info')
        OAS30Validator({**schemas['NameSaveOperation'],'components':document['components']}).validate(rename)

    def test_unknown_field_rejected(self):
        with LocalService() as client,client.open(self.path,create=True) as book:
            body=dict(action='name_save',name='X',reference='Sheet1!A1',approved=True,operation_id=str(uuid.uuid4()),expected_state=book.state)
            with self.assertRaises(APIError) as error:client.request(book.base+'/operations',body)
            self.assertEqual(error.exception.status,400)
            with self.assertRaises(APIError) as error:client.request(book.base+'/names',dict(expected_state=book.state,approved=True))
            self.assertEqual(error.exception.status,400)

    def test_export_reports_names_as_native_only(self):
        with LocalService() as client,client.open(self.path,create=True) as book:
            book.save_name('Quantities','Sheet1!B2:B4')
            for form in ('xlsx','ods'):
                report=' '.join(book.export_spreadsheet(form,scope='workbook')['report'])
                self.assertIn('Workbook names are native-only and omitted from this export: Quantities',report)
                self.assertNotIn('relational objects omitted',report)
            clean=self.path.with_name('clean.dhwb')
            with client.open(clean,create=True) as empty:
                self.assertNotIn('native-only',' '.join(empty.export_spreadsheet('xlsx',scope='workbook')['report']))


if __name__=='__main__':unittest.main()
