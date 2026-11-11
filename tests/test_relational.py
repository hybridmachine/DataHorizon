# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from datahorizon.engine import Workbook, EngineError
from datahorizon.relational import catalog


class NativeTableTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'tables.dhwb';self.book=Workbook(self.path,create=True)
        self.addCleanup(lambda:self.book.close())

    def apply(self,action='table_create',**args):
        return self.book.mutate(dict(operation_id=str(uuid.uuid4()),expected_state=self.book.state(),action=action,**args))

    def create(self):
        return self.apply(name='Customers',columns=[dict(name='CustomerID',required=True,unique=True),dict(name='Active',type='boolean')])['table']

    def test_upgrade_direct_access_snapshot_and_reopen(self):
        item=self.create();self.assertEqual(self.book.info()['format_version'],4)
        with closing(sqlite3.connect(self.path)) as direct:
            direct.execute("INSERT INTO Customers(CustomerID,Active) VALUES('00123',1)");direct.commit()
        self.book.refresh();self.assertEqual(self.book.db.execute('SELECT CustomerID FROM Customers').fetchone()[0],'00123')
        target=Path(self.temp.name)/'copy.dhwb';self.book.snapshot(target)
        with closing(Workbook(target,readonly=True)) as copy:
            self.assertEqual(catalog(copy,dict(expected_state=copy.state()))['tables'][0],item)
            self.assertEqual(copy.db.execute('SELECT _dh_row_id FROM Customers').fetchone()[0],1)

    def test_handler_opt_in_and_migration_rollback(self):
        handler=Path(self.temp.name)/'legacy.py';handler.write_text('def on_change(ctx,changes): pass\n')
        self.book.handler=str(handler)
        with self.assertRaises(EngineError) as failed:self.create()
        self.assertEqual(failed.exception.body['code'],'UNSUPPORTED_HOOK_SCOPE')
        self.assertEqual(self.book.info()['format_version'],1)
        self.assertIsNone(self.book.db.execute("SELECT name FROM sqlite_schema WHERE name='dh_relational'").fetchone())
        self.assertEqual(self.book.operation(failed.exception.body['operation_id'])['outcome'],'rolled_back')

    def test_failed_after_structure_hook_rolls_back_catalog_and_generated_cell(self):
        handler=Path(self.temp.name)/'reject.py'
        handler.write_text("def on_table_structure(ctx,event):\n ctx.set('Sheet1',1,1,'generated')\n if event['phase']=='after': raise ValueError('reject table')\ndef on_change(ctx,changes): pass\n")
        self.book.handler=str(handler)
        with self.assertRaises(EngineError) as failure:self.create()
        self.assertEqual(failure.exception.body['code'],'HOOK_FAILED')
        self.assertEqual(self.book.info()['format_version'],1)
        self.assertIsNone(self.book.raw(self.book.sheet('Sheet1'),1,1))

    def test_rejected_identifiers_and_columns_leave_original_format(self):
        for args in (dict(name='cells',columns=[dict(name='x')]),dict(name='Bad name',columns=[dict(name='x')]),
                     dict(name='T',columns=[dict(name='x'),dict(name='X')]),dict(name='T',columns=[dict(name='x',type='blob')]),
                     dict(name='T',columns=[dict(name='x',required=1)])):
            with self.assertRaises(EngineError):self.apply(**args)
            self.assertEqual(self.book.info()['format_version'],1)

    def test_deduplication_stale_state_and_drop(self):
        request=dict(action='table_create',name='T',columns=[dict(name='x')],operation_id=str(uuid.uuid4()),expected_state=self.book.state())
        first=self.book.mutate(request);self.assertEqual(self.book.mutate(request),first)
        with self.assertRaises(EngineError):self.book.mutate({**request,'operation_id':str(uuid.uuid4()),'name':'Other'})
        self.apply('table_drop',table='T');self.assertEqual(self.book.info()['tables'],[])
        self.book.validate()

    def test_v4_continues_exact_schema_protection(self):
        self.create()
        with closing(sqlite3.connect(self.path)) as direct:direct.execute('CREATE TABLE unrelated(x)');direct.commit()
        with self.assertRaises(EngineError) as failure:self.book.refresh()
        self.assertEqual(failure.exception.body['code'],'SCHEMA_CHANGED')

    def test_v4_header_without_its_catalog_is_a_structured_format_error(self):
        self.book.db.execute('UPDATE dh_meta SET format_version=4')
        with self.assertRaises(EngineError) as failure:self.book.validate()
        self.assertEqual(failure.exception.body['code'],'FORMAT_VERSION')

    def test_existing_schema_versions_upgrade_without_losing_cells(self):
        self.apply('edit',cells=[dict(row=1,col=1,source='2024-02-29',kind='date')])
        self.assertEqual(self.book.info()['format_version'],2)
        self.apply('metadata_set',namespace='list',id='Named',payload=dict(r2=3))
        self.assertEqual(self.book.info()['format_version'],3)
        self.create();self.book.validate()
        self.assertEqual(self.book.raw(self.book.sheet('Sheet1'),1,1)['source'],'2024-02-29')
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM dh_objects').fetchone()[0],1)

    def test_whole_workbook_import_rejects_relational_destination(self):
        self.create()
        with self.assertRaises(EngineError) as failure:self.apply('workbook_import',sheets=[dict(name='Replacement',cells=[])])
        self.assertEqual(failure.exception.body['code'],'DESTINATION_NOT_EMPTY')
        self.assertEqual(len(self.book.info()['tables']),1)

    def test_whole_workbook_export_reports_actual_omitted_tables(self):
        from datahorizon.interchange import xlsx_export
        from datahorizon.ods import ods_export
        self.create();snapshot=self.book.read_workbook()
        for exporter in (xlsx_export,ods_export):
            result=exporter(snapshot,dict(scope='workbook',mode='values'))
            self.assertTrue(any('Customers' in entry for entry in result['report']))
