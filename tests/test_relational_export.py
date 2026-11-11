# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import base64
import csv
import io
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch
from datahorizon.engine import Workbook,EngineError
from datahorizon.relational_export import export_table,export_query
from datahorizon.publication import publish
from datahorizon.relational import table
from datahorizon.client import LocalService

class RelationalExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'exports.dhwb';self.book=Workbook(self.path,create=True);self.addCleanup(self.book.close)
        self.apply('table_create',name='Data',columns=[dict(name='ID'),dict(name='Qty',type='integer'),dict(name='Flag',type='boolean'),dict(name='Day',type='date'),dict(name='Note')]);self.apply('table_insert',table='Data',rows=[dict(ID='00123',Qty=2,Flag=True,Day='2024-02-29',Note='=literal'),dict(ID='B',Qty=4,Flag=False,Note='')]);self.cols=[col['id'] for col in table(self.book.db,'Data')['columns']]
    def apply(self,action,**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))
    def export(self,format='csv',**scope):return export_table(self.book,format,dict(table='Data',expected_state=self.book.state(),**scope))
    def query(self,sql):
        jobs=self.book.query_jobs();result=jobs.start(dict(sql=sql,job_id=str(uuid.uuid4()),expected_state=self.book.state()));deadline=time.monotonic()+4
        while result['status']=='running' and time.monotonic()<deadline:time.sleep(.005);result=jobs.access(result['job_id'])
        self.assertEqual(result['status'],'completed',result);return result
    def test_full_table_values_and_filter_order_are_independent_of_visible_page(self):
        self.apply('table_insert',table='Data',rows=[dict(ID=str(i),Qty=i) for i in range(300)])
        result=self.export(sort=[dict(column=self.cols[1],direction='desc')],filters=[dict(column=self.cols[1],op='ge',value=40)],columns=self.cols[:2]);rows=list(csv.reader(io.StringIO(result['csv'])))
        self.assertEqual(len(rows),261);self.assertEqual(rows[1],['299','299']);self.assertEqual(rows[-1],['40','40']);self.assertEqual(result['records'],260)
        for coordinate in ('offset','limit','after','cursor'):
            with self.assertRaises(EngineError):self.export(**{coordinate:1})
    def test_xlsx_and_ods_independent_xml_preserve_types_literal_text_and_header(self):
        xlsx=self.export('xlsx');ods=self.export('ods');self.assertIn('Data',' '.join(xlsx['report']))
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(xlsx['file_base64']))) as archive:
            root=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'));ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'};cells={c.get('r'):c for c in root.findall('.//s:c',ns)}
            self.assertEqual(cells['A2'].get('t'),'inlineStr');self.assertEqual(cells['A2'].find('s:is/s:t',ns).text,'00123');self.assertEqual(cells['C2'].get('t'),'b');self.assertEqual(cells['D2'].get('t'),'d');self.assertEqual(cells['E2'].find('s:is/s:t',ns).text,'=literal');self.assertIsNone(cells['E2'].find('s:f',ns));self.assertEqual(len(root.findall('.//s:row',ns)),3)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(ods['file_base64']))) as archive:
            root=ET.fromstring(archive.read('content.xml'));ns={'t':'urn:oasis:names:tc:opendocument:xmlns:table:1.0','o':'urn:oasis:names:tc:opendocument:xmlns:office:1.0'};rows=root.findall('.//t:table-row',ns);cells=rows[1].findall('t:table-cell',ns)
            self.assertEqual(len(rows),3);self.assertEqual(cells[0].get('{'+ns['o']+'}value-type'),'string');self.assertEqual(cells[2].get('{'+ns['o']+'}boolean-value'),'true');self.assertEqual(cells[3].get('{'+ns['o']+'}date-value'),'2024-02-29')
    def test_complete_query_export_preserves_order_and_large_integer_text_fallback(self):
        result=self.query('SELECT ID,Qty,9223372036854775807 AS Exact FROM Data ORDER BY Qty DESC')
        for format in ('csv','xlsx','ods'):
            exported=export_query(self.book.query_jobs(),result['job_id'],format,dict(expected_state=self.book.state()));self.assertEqual(exported['records'],2);self.assertTrue(any('2 exact integers' in line for line in exported['report']))
            if format=='csv':self.assertEqual(list(csv.reader(io.StringIO(exported['csv'])))[1],['B','4','9223372036854775807'])
            if format=='xlsx':
                with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported['file_base64']))) as archive:
                    root=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'));ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'};cell=root.find('.//s:c[@r="C2"]',ns);self.assertEqual(cell.get('t'),'inlineStr');self.assertEqual(cell.find('s:is/s:t',ns).text,'9223372036854775807')
    def test_empty_results_headers_and_oversized_complete_exports_are_explicit(self):
        self.assertEqual(list(csv.reader(io.StringIO(self.export(filters=[dict(column=self.cols[1],op='gt',value=100)])['csv']))),[['ID','Qty','Flag','Day','Note']])
        result=self.query('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT x FROM n')
        with self.assertRaises(EngineError) as failure:export_query(self.book.query_jobs(),result['job_id'],'csv',dict(expected_state=self.book.state()))
        self.assertEqual(failure.exception.body['code'],'EXPORT_LIMIT');exact=export_query(self.book.query_jobs(),result['job_id'],'csv',dict(expected_state=self.book.state(),include_header=False));self.assertEqual(exact['records'],10000)
    def test_stale_query_and_export_type_limits_do_not_produce_partial_files(self):
        result=self.query('SELECT * FROM Data');self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Qty=8))])
        with self.assertRaises(EngineError):export_query(self.book.query_jobs(),result['job_id'],'csv',dict(expected_state=self.book.state()))
        self.apply('table_update',table='Data',edits=[dict(row_id='1',values=dict(Note='x'*40000))])
        with self.assertRaises(EngineError):self.export('xlsx')
    def test_atomic_publication_native_aliases_state_failure_and_no_clobber(self):
        snapshot=self.export();out=Path(self.temp.name)/'values.csv';native_before=self.path.read_bytes();alias=Path(self.temp.name)/'alias.csv';os.link(self.path,alias)
        for destination in (self.path,alias):
            with self.assertRaises(ValueError):publish(snapshot,destination,self.path,current=lambda:None,overwrite=True)
        self.assertEqual(self.path.read_bytes(),native_before);out.write_bytes(b'old')
        with self.assertRaises(FileExistsError):publish(snapshot,out,self.path,current=lambda:None)
        with self.assertRaises(RuntimeError):publish(snapshot,out,self.path,current=lambda:(_ for _ in ()).throw(RuntimeError('stale')),overwrite=True)
        self.assertEqual(out.read_bytes(),b'old')
        with patch('datahorizon.publication.os.replace',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):publish(snapshot,out,self.path,current=lambda:None,overwrite=True)
        self.assertEqual(out.read_bytes(),b'old');result=publish(snapshot,out,self.path,current=lambda:None,overwrite=True);self.assertEqual(result['outcome'],'committed');self.assertEqual(out.read_bytes(),base64.b64decode(snapshot['file_base64']));self.assertEqual(list(Path(self.temp.name).glob('.datahorizon-export-*')),[])
    def test_final_review_alias_swap_rejects_before_atomic_replacement(self):
        snapshot=self.export();destination=Path(self.temp.name)/'chosen.csv';destination.write_bytes(b'old');before=self.path.read_bytes()
        def swap():destination.unlink();os.link(self.path,destination)
        with self.assertRaises(ValueError):publish(snapshot,destination,self.path,current=swap,overwrite=True)
        self.assertEqual(self.path.read_bytes(),before)

class RelationalExportClientTests(unittest.TestCase):
    def test_sdk_table_query_exports_and_protected_publication(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            with client.open(Path(temp)/'client.dhwb',create=True) as book:
                book.create_table('Data',[dict(name='Value',type='integer')]);book.insert_records('Data',[dict(Value=2)]);snapshot=book.export_table('Data','csv');path=Path(temp)/'values.csv';book.publish_export(snapshot,path);self.assertEqual(path.read_text(),'Value\n2\n')
                result=book.run_query('SELECT Value FROM Data');deadline=time.monotonic()+4
                while result['status']=='running' and time.monotonic()<deadline:time.sleep(.005);result=book.query(result['job_id'])
                self.assertEqual(book.export_query(result['job_id'],'xlsx')['records'],1);book.query(result['job_id'],'release')
