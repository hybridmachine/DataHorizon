# SPDX-License-Identifier: GPL-3.0-only
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datahorizon.client import LocalService, APIError


class TableClientTests(unittest.TestCase):
    def test_sdk_rest_cli_catalog_and_read_only_parity(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'clients.dhwb'
            with client.open(path,create=True) as book:
                receipt=book.create_table('Customers',[dict(name='ID',required=True,unique=True)])
                self.assertEqual(receipt['outcome'],'committed')
                self.assertEqual(book.tables()['tables'][0]['name'],'Customers')
                args=[sys.executable,'-m','datahorizon.cli','--url',client.url,str(path),'tables']
                result=subprocess.run(args,env={**os.environ,'DATAHORIZON_TOKEN':client.token},capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout)['tables'][0]['id'],receipt['table']['id'])
                book.drop_table('Customers')
            with client.open(path,readonly=True) as book:
                self.assertEqual(book.tables()['tables'],[])
                with self.assertRaises(APIError):book.create_table('Other',[dict(name='x')])

    def test_capabilities_and_action_examples_cover_table_operations(self):
        with LocalService() as client:
            capabilities=client.request('/v1/capabilities');api=client.request('/openapi.json')
            self.assertIn(4,capabilities['supported_format_versions'])
            self.assertIn('table_create',capabilities['operations'])
            self.assertIn('/v1/workbooks/{workbook}/tables',api['paths'])
            self.assertIn('table_create',api['components']['schemas']['OperationRequest']['discriminator']['mapping'])
