# SPDX-License-Identifier: GPL-3.0-only
"""List packages contain the same ordered values as the reviewed snapshot."""
import base64
from contextlib import closing
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile
from datahorizon.engine import Workbook,EngineError
from datahorizon.list_export import export_list
from datahorizon.client import LocalService,APIError
from datahorizon.service import openapi

class ListExportTests(unittest.TestCase):
    def test_csv_xlsx_ods_values_types_styles_order_and_one_snapshot(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'export.dhwb',create=True)) as book:
            book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[
                {'row':1,'col':1,'source':'ID'},{'row':1,'col':2,'source':'Value'},
                {'row':2,'col':1,'source':'00123','kind':'text'},{'row':2,'col':2,'source':'=1/4','style':{'format':'percent2'}},
                {'row':3,'col':1,'source':'Z'},{'row':3,'col':2,'source':'TRUE'},{'row':4,'col':1,'source':'hide'}]})
            state=book.state();args={'r2':4,'c2':2,'header':True,'sort':[{'col':1,'descending':True}],'exclude_rows':[4],'expected_state':state}
            with patch.object(book,'list_values',wraps=book.list_values) as read:
                csv_result=export_list(book,'csv',args);self.assertEqual(read.call_count,1)
            self.assertEqual(list(csv.reader(io.StringIO(csv_result['csv']))),[['ID','Value'],['Z','TRUE'],['00123','0.25']])
            for format in ('xlsx','ods'):
                result=export_list(book,format,args);self.assertEqual(result['state'],state);self.assertEqual(result['source_rows'],[1,3,2]);self.assertIn('1 formula inputs',' '.join(result['report']))
                archive=zipfile.ZipFile(io.BytesIO(base64.b64decode(result['file_base64'])))
                if format=='xlsx':
                    root=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'));ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                    cells={c.get('r'):c for c in root.findall('.//s:c',ns)};self.assertEqual(cells['A3'].find('.//s:t',ns).text,'00123');self.assertEqual(cells['B3'].find('s:v',ns).text,'0.25');self.assertEqual(cells['B2'].get('t'),'b');self.assertFalse(root.findall('.//s:f',ns));self.assertNotEqual(cells['B3'].get('s'),'0')
                else:
                    data=archive.read('content.xml').decode();self.assertIn('00123',data);self.assertIn('0.25',data);self.assertNotIn('table:formula=',data);self.assertIn('percentage',data)
            self.assertEqual(book.state(),state)
            for args2 in ({**args,'expected_state':'stale'},{**args,'mode':'formulas'},{**args,'header':False,'filters':[{'col':1,'op':'=','value':'missing'}]}):
                with self.assertRaises(EngineError):export_list(book,'csv',args2)

    def test_sdk_readonly_and_error_exports_preserve_source(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'export.dhwb'
            with client.open(path,create=True) as book:book.apply('edit',cells=[{'row':1,'col':1,'source':'=2+3'},{'row':2,'col':1,'source':'=1/0'}])
            with client.open(path,readonly=True) as book:
                state=book.state;self.assertEqual(book.export_list('csv',r2=1,c2=1)['csv'],'5\r\n')
                with self.assertRaises(APIError):book.export_list('xlsx',r2=2,c2=1)
                self.assertEqual(book.refresh()['state'],state);self.assertIsNone(book.pending_operation)
        for format in ('csv','xlsx','ods'):self.assertIn('/v1/workbooks/{workbook}/list/export/'+format,openapi()['paths'])
