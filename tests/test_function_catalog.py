# SPDX-License-Identifier: GPL-3.0-only
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
        with tempfile.TemporaryDirectory() as temp:
            examples=[example for item in catalog['functions'] for example in item['examples']]
            for index,example in enumerate(examples):
                with closing(Workbook(Path(temp)/(str(index)+'.dhwb'),create=True)) as book:
                    book.mutate({'action':'edit','operation_id':'example','expected_state':book.state(),'cells':[*example.get('inputs',[]),{'row':1,'col':1,'source':example['formula']}]})
                    value=book.read('Sheet1',{'r2':1,'c2':1})['cells'][0]
                    self.assertEqual(value['value'],example['result'],example['formula']);self.assertEqual(value['calculation'],'current')
        catalog['functions'][0]['examples'].clear();self.assertTrue(function_catalog()['functions'][0]['examples'])

    def test_sdk_catalog_needs_no_workbook_and_matches_openapi(self):
        with LocalService() as client:
            catalog=client.functions();self.assertEqual(catalog,function_catalog())
            self.assertEqual(client.request('/v1/capabilities')['formulas'],CAPABILITIES['formulas'])
        schema=openapi();self.assertEqual(schema['paths']['/v1/functions']['get']['responses']['200']['content']['application/json']['schema']['$ref'],'#/components/schemas/FunctionCatalog')
