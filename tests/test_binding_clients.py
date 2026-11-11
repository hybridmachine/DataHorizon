# SPDX-License-Identifier: GPL-3.0-only
"""Event bindings through REST, OpenAPI, SDK and CLI agree and cannot be approved by clients."""
import copy
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
SOURCE='''def check_quantity(ctx, changes):
    for c in changes:
        value = ctx.value(c['sheet'], c['row'], c['col'])
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError('Quantity cannot be negative')
'''
MACROS=[]
SCOPE=dict(kind='range',reference="'Sheet1'!B2:B4")


class BindingClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'bindings.dhwb'

    def prepare(self,**options):
        """Create a workbook with module 'rules' and one required range binding; return its binding id and code catalog."""
        with LocalService() as client,client.open(self.path,create=True) as book:
            book.set(2,2,'4')
            book.save_module('rules',SOURCE,MACROS,fingerprint=book.code()['fingerprint'])
            made=book.save_binding('cell_change',SCOPE,'rules','check_quantity',fingerprint=book.code()['fingerprint'],**options)
            return made['binding_id'],book.code()

    def test_gui_shaped_rest_edit_cli_edit_and_sdk_edit_hit_the_same_rule(self):
        binding_id,code=self.prepare()
        self.assertEqual(code['bindings'][0]['id'],binding_id)
        key=code['activation_key']
        with LocalService(approved_code=[key]) as client,client.open(self.path) as book:
            def check(error):
                body=error.body;self.assertEqual(body['code'],'BINDING_FAILED')
                self.assertEqual(body['binding']['binding_id'],binding_id);self.assertEqual(body['binding']['function'],'check_quantity')
                self.assertEqual(body['binding']['module'],'rules');self.assertIn('rules.check_quantity',body['message'])
            raw=dict(action='edit',operation_id=str(uuid.uuid4()),expected_state=book.state,sheet='Sheet1',cells=[dict(row=2,col=2,source='-1')])
            with self.assertRaises(APIError) as error:client.request(book.base+'/operations',raw)
            check(error.exception);self.assertEqual(error.exception.status,409)
            book.refresh()
            with self.assertRaises(APIError) as error:book.set(2,2,'-2')
            check(error.exception)
            book.refresh()
            with self.assertRaises(APIError) as error:book.set(3,2,'-3')
            check(error.exception)
            self.assertEqual(book.read()['cells'][0]['value'],4)
            self.assertEqual(book.set(2,2,'7')['outcome'],'committed')
            book.set(2,2,'4')
            token=client.token
            import os
            cli=subprocess.run([*CLI,'--url',client.url,str(self.path),'set','B2','-1'],env={**os.environ,'DATAHORIZON_TOKEN':token},capture_output=True,text=True)
            self.assertEqual(cli.returncode,1);err=json.loads(cli.stderr);check(APIError(409,err))
            book.refresh();self.assertEqual(book.read()['cells'][0]['value'],4)
        owned=subprocess.run([*CLI,'--approve-code',key,str(self.path),'set','B2','-1'],capture_output=True,text=True)
        self.assertEqual(owned.returncode,1);check(APIError(409,json.loads(owned.stderr)))
        with LocalService() as client,client.open(self.path) as book:
            self.assertEqual(book.read()['cells'][0]['value'],4)

    def test_sdk_rest_and_cli_agree(self):
        binding_id,code=self.prepare()
        fingerprint=code['fingerprint']
        read=json.loads(subprocess.run([*CLI,str(self.path),'bindings','read'],capture_output=True,text=True,check=True).stdout)
        self.assertEqual(read,code['bindings']);self.assertEqual(read[0]['reference'],"'Sheet1'!B2:B4");self.assertEqual(read[0]['module'],'rules')
        saved=subprocess.run([*CLI,str(self.path),'bindings','save'],input=json.dumps(dict(event='cell_change',scope=dict(kind='sheet',sheet='Sheet1'),module='rules',function='check_quantity',required=False,fingerprint=fingerprint)),capture_output=True,text=True,check=True)
        second=json.loads(saved.stdout);self.assertEqual(second['outcome'],'committed')
        with LocalService() as client,client.open(self.path) as book:
            found={item['id']:item for item in book.code()['bindings']}
            self.assertEqual(set(found),{binding_id,second['binding_id']});self.assertFalse(found[second['binding_id']]['required'])
            raw=client.request(book.base+'/code',dict(expected_state=book.state));self.assertEqual(raw['bindings'],book.code()['bindings'])
            book.save_binding('cell_change',SCOPE,'rules','check_quantity',fingerprint=book.code()['fingerprint'],required=False,binding_id=binding_id)
            self.assertFalse(next(i for i in book.code()['bindings'] if i['id']==binding_id)['required'])
        fingerprint=json.loads(subprocess.run([*CLI,str(self.path),'code','read'],capture_output=True,text=True,check=True).stdout)['fingerprint']
        refused=subprocess.run([*CLI,str(self.path),'bindings','delete',second['binding_id']],capture_output=True,text=True)
        self.assertEqual(refused.returncode,1)
        done=subprocess.run([*CLI,str(self.path),'bindings','delete',second['binding_id'],'--fingerprint',fingerprint],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(done.stdout)['binding_id'],second['binding_id'])
        with LocalService() as client,client.open(self.path) as book:
            self.assertEqual([i['id'] for i in book.code()['bindings']],[binding_id])
            with self.assertRaises(APIError) as error:book.delete_binding(second['binding_id'],fingerprint=book.code()['fingerprint'])
            self.assertEqual(error.exception.body['code'],'BINDING_NOT_FOUND')
            with self.assertRaises(APIError) as error:book.delete_binding(binding_id,fingerprint='0'*64)
            self.assertEqual(error.exception.body['code'],'CODE_CHANGED')

    def test_capabilities(self):
        with LocalService() as client:
            capabilities=client.request('/v1/capabilities')
            self.assertTrue(capabilities['event_bindings']);self.assertEqual(capabilities['max_bindings'],16)
            self.assertEqual(capabilities['binding_events'],['cell_change','table_change'])
            self.assertIn('binding_save',capabilities['operations']);self.assertIn('binding_delete',capabilities['operations'])

    def test_openapi_documents_bindings(self):
        document=openapi();schemas=document['components']['schemas']
        for name in ('BindingSaveOperation','BindingDeleteOperation'):self.assertIs(schemas[name]['additionalProperties'],False)
        self.assertIn('bindings',schemas['CodeCatalog']['properties'])
        for key in ('binding_notices','broken_names','broken_bindings'):self.assertIn(key,schemas['Receipt']['properties'])
        self.assertIn('binding',schemas['Error']['properties'])
        self.assertEqual(schemas['BindingNotice']['properties']['reason']['enum'],['BINDING_NOT_APPROVED','BINDING_UNAVAILABLE'])

    @unittest.skipIf(OAS30Validator is None,'Optional build-only OpenAPI validators unavailable')
    def test_openapi_validates_schemas(self):
        document=openapi();validate(document);schemas=document['components']['schemas']
        for name in ('BindingSaveOperation','BindingDeleteOperation'):
            OAS30Validator({**schemas[name],'components':document['components']}).validate(schemas[name]['example'])

    def test_openapi_examples_execute_over_http(self):
        schemas=openapi()['components']['schemas']
        with LocalService() as client,client.open(self.path,create=True) as book:
            book.save_module('Examples',SOURCE,MACROS,fingerprint=book.code()['fingerprint'])
            digest=book.code()['fingerprint']
            save=copy.deepcopy(schemas['BindingSaveOperation']['example']);save.update(operation_id=str(uuid.uuid4()),expected_state=book.state,fingerprint=digest)
            receipt=client.request(book.base+'/operations',save);book.refresh()
            catalog=client.request(book.base+'/code',dict(schemas['CodeReadRequest']['example'],expected_state=book.state))
            self.assertEqual([i['id'] for i in catalog['bindings']],[receipt['binding_id']])
            if OAS30Validator is not None:
                document=openapi();OAS30Validator({**schemas['CodeCatalog'],'components':document['components']}).validate(catalog)
            delete=copy.deepcopy(schemas['BindingDeleteOperation']['example']);delete.update(binding_id=receipt['binding_id'],fingerprint=catalog['fingerprint'],operation_id=str(uuid.uuid4()),expected_state=book.state)
            client.request(book.base+'/operations',delete);book.refresh();self.assertEqual(book.code()['bindings'],[])

    def test_request_fields_cannot_claim_approval(self):
        with LocalService() as client,client.open(self.path,create=True) as book:
            book.save_module('rules',SOURCE,MACROS,fingerprint=book.code()['fingerprint'])
            digest=book.code()['fingerprint']
            for extra in (dict(approved=True),dict(activation_key=book.code()['activation_key']),dict(approve_code=[book.code()['activation_key']])):
                body=dict(action='binding_save',event='cell_change',scope=SCOPE,module='rules',function='check_quantity',fingerprint=digest,operation_id=str(uuid.uuid4()),expected_state=book.state,**extra)
                with self.assertRaises(APIError) as error:client.request(book.base+'/operations',body)
                self.assertEqual(error.exception.status,400)
            with self.assertRaises(APIError) as error:client.request(book.base+'/code',dict(expected_state=book.state,approved=True))
            self.assertEqual(error.exception.status,400)
            self.assertEqual(book.code()['bindings'],[])
            # A saved binding is still unapproved: the bearer token alone never runs it.
            book.save_binding('cell_change',SCOPE,'rules','check_quantity',fingerprint=digest)
            with self.assertRaises(APIError) as error:book.set(2,2,'1')
            self.assertEqual(error.exception.body['code'],'BINDING_NOT_APPROVED');self.assertEqual(error.exception.status,403)

    def test_export_reports_bindings_as_native_only(self):
        self.prepare()
        with LocalService() as client,client.open(self.path) as book:
            for form in ('xlsx','ods'):
                report=' '.join(book.export_spreadsheet(form,scope='workbook')['report'])
                self.assertIn('Event bindings are native-only and omitted from this export: rules.check_quantity',report)
                self.assertNotIn('binding check_quantity',report)
            clean=self.path.with_name('clean.dhwb')
            with client.open(clean,create=True) as empty:
                self.assertNotIn('Event bindings',' '.join(empty.export_spreadsheet('xlsx',scope='workbook')['report']))


if __name__=='__main__':unittest.main()
