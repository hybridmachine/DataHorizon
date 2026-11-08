"""The catalog advertises executable examples of the implemented tier."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from datahorizon.functions import function_catalog,FUNCTIONS
from datahorizon.formula import SUPPORTED_FUNCTIONS
from datahorizon.engine import Workbook
from datahorizon.client import LocalService
from datahorizon.service import CAPABILITIES,openapi

class FunctionCatalogTests(unittest.TestCase):
    def test_names_examples_and_copy_isolation(self):
        catalog=function_catalog();names=[item['name'] for item in catalog['functions']]
        self.assertEqual(names,sorted(SUPPORTED_FUNCTIONS));self.assertEqual(set(names),set(FUNCTIONS));self.assertTrue(set(names)<=set(CAPABILITIES['formulas']))
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'examples.dhwb',create=True)) as book:
            examples=[example for item in catalog['functions'] for example in item['examples']]
            book.mutate({'action':'edit','operation_id':'examples','expected_state':book.state(),'cells':[{'row':i,'col':1,'source':example['formula']} for i,example in enumerate(examples,1)]})
            values=book.read('Sheet1',{'r2':len(examples),'c2':1})['cells']
            self.assertEqual([item['value'] for item in values],[item['result'] for item in examples]);self.assertTrue(all(item['calculation']=='current' for item in values))
        catalog['functions'][0]['examples'].clear();self.assertTrue(function_catalog()['functions'][0]['examples'])

    def test_sdk_catalog_needs_no_workbook_and_matches_openapi(self):
        with LocalService() as client:
            catalog=client.functions();self.assertEqual(catalog,function_catalog())
            self.assertEqual(client.request('/v1/capabilities')['formulas'],CAPABILITIES['formulas'])
        schema=openapi();self.assertEqual(schema['paths']['/v1/functions']['get']['responses']['200']['content']['application/json']['schema']['$ref'],'#/components/schemas/FunctionCatalog')
