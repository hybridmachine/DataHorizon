# SPDX-License-Identifier: GPL-3.0-only
"""Applying a prepared import stage as one workbook_import operation, replacing an empty workbook or adding sheets."""
import base64
from contextlib import closing
import copy
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xlsx_builder import workbook_bytes

from datahorizon import engine as engine_module
from datahorizon.client import APIError
from datahorizon.conversion_report import ConversionReport
from datahorizon.engine import STAGE_PATH, EngineError, Workbook
from datahorizon.import_stage import ImportStage, StageReader
from datahorizon.service import Service, openapi
from datahorizon.workbook_code import fingerprint

FIDELITY = Path(__file__).resolve().parent/'fixtures'/'fidelity.xlsx'
IMPORT_HOOK = Path(__file__).resolve().parent/'fixtures'/'import_hook.py'


def large_workbook(rows, cols=10):
    data = ''.join(f'<row r="{r}">' + ''.join(f'<c r="{chr(64+c)}{r}"><v>{r*c}</v></c>' for c in range(1, cols+1)) + '</row>'
                   for r in range(1, rows+1))
    return workbook_bytes({'Big': data})


def build_stage(path, sheets, source=None):
    """Write a finished stage directly: sheets is a list of (name, cells)."""
    Path(path).touch()
    stage = ImportStage(path)
    try:
        for name, cells in sheets:
            stage.sheet(name)
            if cells: stage.cells(cells)
        return stage.finish(ConversionReport(), {"source": source or {"kind": "upload", "upload_id": "test"}})
    finally:
        stage.close()


def number_cells(count, start=1):
    return [{"row": row, "col": 1, "source": str(row), "kind": "number", "style": {}} for row in range(start, start+count)]


class ServiceStageImportTests(unittest.TestCase):
    """The REST path: the service resolves stage_id to its staging file and releases it after a successful apply."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.service = Service(staging_root=self.temp/'staging')
        worker = threading.Thread(target=self.service.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        worker.start()
        def stop():
            self.service.shutdown(); worker.join(timeout=2); self.service.server_close()
        self.addCleanup(stop)
        self.book = self.open(self.temp/'book.dhwb', create=True)

    def request(self, method, path, body=None, content_type='application/json'):
        connection = http.client.HTTPConnection('127.0.0.1', self.service.server_port, timeout=120)
        try:
            data = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
            connection.request(method, path, body=data if method != 'GET' else None,
                               headers={'Authorization': 'Bearer '+self.service.token, 'Content-Type': content_type})
            reply = connection.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            connection.close()

    def call(self, path, body=None, method='POST'):
        status, result = self.request(method, path, body)
        if status != 200: raise APIError(status, result)
        return result

    def open(self, path, create=False):
        return self.call('/v1/open', {'path': str(path), 'create': create})['id']

    def info(self, book=None):
        return self.call(f'/v1/workbooks/{book or self.book}', method='GET')

    def state(self):
        return self.info()['state']

    def sheet_names(self):
        return [sheet['name'] for sheet in self.info()['sheets']]

    def operate(self, request, book=None):
        request = {'expected_state': self.info(book)['state'], **request}
        request.setdefault('operation_id', 'op-'+str(time.monotonic_ns()))
        return self.call(f'/v1/workbooks/{book or self.book}/operations', request)

    def cells(self, sheet, **area):
        return self.call(f'/v1/workbooks/{self.book}/range', {'sheet': sheet, **area})['cells']

    def wait(self, job_id):
        deadline = time.monotonic()+120
        while time.monotonic() < deadline:
            job = self.call('/v1/jobs/'+job_id, method='GET')
            if job['status'] != 'running':
                self.assertEqual(job['status'], 'completed', job)
                return job['result']
            time.sleep(.01)
        self.fail('Preparation did not finish')

    def prepare(self, data=FIDELITY, job_id='stage', **request):
        if isinstance(data, Path):
            request = {'source_path': str(data), **request}
        else:
            self.assertEqual(self.request('PUT', '/v1/uploads/'+job_id, data, 'application/octet-stream')[0], 200)
            request = {'upload_id': job_id, **request}
        self.call('/v1/import/xlsx/jobs', {'job_id': job_id, 'scope': 'workbook', **request})
        return self.wait(job_id)

    def apply(self, summary, target=None, book=None, **extra):
        request = {'action': 'workbook_import', 'stage_id': summary['stage_id'], 'stage_digest': summary['stage_digest'], **extra}
        if target is not None: request['target'] = target
        return self.operate(request, book)

    def stage_exists(self, stage_id):
        try: self.service.staging.path('stage', stage_id)
        except EngineError: return False
        return True

    def test_stage_altered_after_review_is_refused(self):
        summary = self.prepare(FIDELITY)
        with closing(sqlite3.connect(self.service.staging.path('stage', summary['stage_id']))) as db, db:
            # The stage's cells table is WITHOUT ROWID, so the brief's rowid selector is spelled with the primary key.
            db.execute("UPDATE cells SET source='999' WHERE (sheet, row, col) IN (SELECT sheet, row, col FROM cells LIMIT 1)")
        with self.assertRaises(APIError) as raised: self.apply(summary)
        self.assertEqual(raised.exception.body['code'], 'STALE_PLAN')
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.sheet_names(), ['Sheet1'])
        self.assertTrue(self.stage_exists(summary['stage_id']), 'a refused apply keeps the stage for another try')

    def test_source_file_replaced_after_review_is_refused(self):
        source = self.temp/'source.xlsx'
        shutil.copyfile(FIDELITY, source)
        summary = self.prepare(source)
        before = self.state()
        info = source.stat()
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns+1_000_000_000))
        with self.assertRaises(APIError) as raised: self.apply(summary)
        self.assertEqual(raised.exception.body['code'], 'STALE_PLAN')
        self.assertEqual(self.sheet_names(), ['Sheet1'])
        self.assertEqual(self.cells('Sheet1'), [])
        self.assertEqual(self.state().split(':')[0], before.split(':')[0])
        source.unlink()
        with self.assertRaises(APIError) as raised: self.apply(summary)
        self.assertEqual(raised.exception.body['code'], 'STALE_PLAN')
        self.assertTrue(self.stage_exists(summary['stage_id']))

    def test_add_sheets_appends_and_keeps_existing_cells_and_modules(self):
        self.operate({'action': 'edit', 'sheet': 'Sheet1', 'cells': [{'row': 2, 'col': 3, 'source': '41'}]})
        self.operate({'action': 'sheet_add', 'name': 'Notes'})
        engine = self.service.workbooks[self.book]
        with engine.lock: code = fingerprint(engine.db)
        self.operate({'action': 'module_save', 'name': 'Rules', 'source': 'x = 1\n', 'macros': [], 'fingerprint': code})
        self.operate({'action': 'name_save', 'name': 'Answer', 'reference': 'Sheet1!C2'})
        before = self.info()
        summary = self.prepare(FIDELITY)
        receipt = self.apply(summary, 'add_sheets')
        self.assertEqual(receipt['outcome'], 'committed')
        self.assertEqual(receipt['imported'], {'sheets': 3, 'cells': summary['stored_cells']})
        after = self.info()
        self.assertEqual([s['name'] for s in after['sheets']], ['Sheet1', 'Notes', 'Inputs', 'Summary', 'Empty'])
        self.assertEqual(after['sheets'][:2], before['sheets'][:2])
        self.assertIsNone(after['undo'])
        self.assertEqual(self.cells('Sheet1', r1=2, c1=3)[0]['value'], 41)
        code_view = self.call(f'/v1/workbooks/{self.book}/code', {'expected_state': after['state']})
        self.assertEqual([module['name'] for module in code_view['modules']], ['Rules'])
        names = self.call(f'/v1/workbooks/{self.book}/names', {'expected_state': after['state']})
        self.assertEqual([item['name'] for item in names['names']], ['Answer'])
        self.assertFalse(self.stage_exists(summary['stage_id']), 'a successful apply releases the stage')
        with self.assertRaises(APIError) as raised: self.call('/v1/jobs/'+summary['stage_id'], method='GET')
        self.assertEqual(raised.exception.body['code'], 'JOB_NOT_FOUND')

    def test_add_sheets_name_conflict_rejects_everything(self):
        self.operate({'action': 'sheet_add', 'name': 'inputs'})
        self.operate({'action': 'sheet_add', 'name': 'EMPTY'})
        self.operate({'action': 'edit', 'sheet': 'Sheet1', 'cells': [{'row': 1, 'col': 1, 'source': '1'}]})
        before = self.info()
        summary = self.prepare(FIDELITY)
        with self.assertRaises(APIError) as raised: self.apply(summary, 'add_sheets')
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.body['code'], 'SHEET_NAME_CONFLICT')
        self.assertEqual(raised.exception.body['conflicts'], ['Inputs', 'Empty'])
        after = self.info()
        self.assertEqual(after['sheets'], before['sheets'])
        self.assertEqual(after['state'].split(':')[1], before['state'].split(':')[1])
        self.assertTrue(self.stage_exists(summary['stage_id']))

    def test_replace_empty_keeps_the_existing_destination_rule(self):
        self.operate({'action': 'edit', 'sheet': 'Sheet1', 'cells': [{'row': 1, 'col': 1, 'source': '1'}]})
        summary = self.prepare(FIDELITY)
        with self.assertRaises(APIError) as raised: self.apply(summary)
        self.assertEqual(raised.exception.body['code'], 'DESTINATION_NOT_EMPTY')
        with self.assertRaises(APIError) as raised: self.apply(summary, 'sideways')
        self.assertEqual(raised.exception.body['code'], 'INVALID_ARGUMENT')
        self.assertEqual(self.sheet_names(), ['Sheet1'])

    def test_staged_import_equals_small_path_import(self):
        summary = self.prepare(FIDELITY)
        staged_receipt = self.apply(summary)
        inline = self.call('/v1/import/xlsx/jobs', {'job_id': 'inline', 'scope': 'workbook',
                                                   'file_base64': base64.b64encode(FIDELITY.read_bytes()).decode()})
        reviewed = self.wait(inline['job_id'])
        small = self.open(self.temp/'small.dhwb', create=True)
        small_receipt = self.operate({'action': 'workbook_import', 'sheets': reviewed['workbook_sheets']}, small)
        for key in ('changed_cells', 'changed_records', 'imported'):
            self.assertEqual(staged_receipt[key], small_receipt[key], key)
        exported = []
        for book in (self.book, small):
            engine = self.service.workbooks[book]
            snapshot = engine.read_workbook()
            snapshot.pop('state')
            exported.append(snapshot)
        self.assertEqual(exported[0], exported[1])
        self.assertEqual(sum(len(sheet['cells']) for sheet in exported[0]['sheets']), summary['stored_cells'])

    def test_sixty_thousand_cells_apply_read_back_and_reopen(self):
        summary = self.prepare(large_workbook(6000))
        self.assertEqual(summary['stored_cells'], 60_000)
        before = self.state()
        receipt = self.apply(summary)
        self.assertEqual(receipt['imported'], {'sheets': 1, 'cells': 60_000})
        self.assertEqual(receipt['changed_cells'], 60_000)
        self.assertNotEqual(receipt['state'], before)
        self.assertEqual(self.state(), receipt['state'])
        self.assertEqual(self.sheet_names(), ['Big'])
        self.assertEqual(self.cells('Big', r1=1, c1=1)[0]['value'], 1)
        self.assertEqual(self.cells('Big', r1=6000, c1=10)[0]['value'], 60_000)
        self.assertEqual(self.cells('Big', r1=3001, c1=7)[0]['value'], 3001*7)
        path = self.temp/'book.dhwb'
        self.call(f'/v1/workbooks/{self.book}/close', {})
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM cells').fetchone()[0], 60_000)
        with closing(Workbook(path)) as reopened:
            self.assertEqual([s['name'] for s in reopened.info()['sheets']], ['Big'])
            big = reopened.sheet('Big')
            self.assertEqual(reopened.read(big, {'r1': 6000, 'c1': 10})['cells'][0]['value'], 60_000)

    def test_retry_with_same_operation_returns_the_receipt_and_does_not_import_twice(self):
        summary = self.prepare(FIDELITY)
        request = {'action': 'workbook_import', 'stage_id': summary['stage_id'], 'stage_digest': summary['stage_digest'],
                   'operation_id': 'apply-once', 'expected_state': self.state()}
        first = self.call(f'/v1/workbooks/{self.book}/operations', request)
        self.assertFalse(self.stage_exists(summary['stage_id']))
        again = self.call(f'/v1/workbooks/{self.book}/operations', request)
        self.assertEqual(again, first)
        self.assertEqual(self.sheet_names(), ['Inputs', 'Summary', 'Empty'])
        self.assertEqual(self.state(), first['state'])
        with self.assertRaises(APIError) as raised:
            self.apply(summary, 'add_sheets')
        self.assertEqual((raised.exception.status, raised.exception.body['code']), (404, 'JOB_NOT_FOUND'))
        with self.assertRaises(APIError) as raised:
            self.call(f'/v1/workbooks/{self.book}/operations', {**request, 'target': 'add_sheets'})
        self.assertEqual(raised.exception.body['code'], 'OPERATION_ID_REUSED')

    def test_sheets_with_stage_and_client_supplied_stage_path_are_refused(self):
        summary = self.prepare(FIDELITY)
        with self.assertRaises(APIError) as raised:
            self.apply(summary, sheets=[{'name': 'Other', 'cells': []}])
        self.assertEqual(raised.exception.body['code'], 'INVALID_ARGUMENT')
        foreign = self.temp/'foreign.stage'
        build_stage(foreign, [('Foreign', number_cells(3))])
        for request in ({'action': 'workbook_import', 'stage_id': summary['stage_id'], 'stage_digest': summary['stage_digest'], STAGE_PATH: str(foreign)},
                        {'action': 'workbook_import', 'sheets': [{'name': 'A', 'cells': []}], STAGE_PATH: str(foreign)},
                        {'action': 'edit', 'cells': [{'row': 1, 'col': 1, 'source': '1'}], STAGE_PATH: None}):
            with self.assertRaises(APIError) as raised: self.operate(request)
            self.assertEqual(raised.exception.body['code'], 'INVALID_ARGUMENT')
        self.assertEqual(self.sheet_names(), ['Sheet1'])
        self.assertEqual(self.cells('Sheet1'), [])
        self.assertTrue(self.stage_exists(summary['stage_id']))
        with self.assertRaises(APIError) as raised:
            self.apply({'stage_id': 'missing', 'stage_digest': '0'*64})
        self.assertEqual(raised.exception.body['code'], 'JOB_NOT_FOUND')

    def test_temporal_kinds_raise_the_format_version(self):
        stage = self.service.staging.new('stage', 'temporal')
        summary = build_stage(stage, [('Dates', [{'row': 1, 'col': 1, 'source': '2024-02-29', 'kind': 'date', 'style': {'format': 'date_iso'}}])])
        self.assertEqual(self.info()['format_version'], 1)
        self.apply(summary)
        self.assertEqual(self.info()['format_version'], 2)
        self.assertEqual(self.cells('Dates')[0]['value_kind'], 'date')

    def test_openapi_staged_example_executes(self):
        spec = openapi(); schema = spec['components']['schemas']['WorkbookImportOperation']
        self.assertEqual(schema['properties']['target']['enum'], ['replace_empty', 'add_sheets'])
        self.assertNotIn(STAGE_PATH, schema['properties'])
        inline, staged = schema['oneOf']
        self.assertIn('sheets', inline['required']); self.assertEqual(staged['required'], ['stage_id', 'stage_digest'])
        summary = self.prepare(FIDELITY, job_id=staged['example']['stage_id'])
        request = {**copy.deepcopy(staged['example']), 'stage_digest': summary['stage_digest'], 'expected_state': self.state()}
        self.assertTrue(set(request).issubset(schema['properties']))
        receipt = self.call(f'/v1/workbooks/{self.book}/operations', request)
        self.assertEqual(receipt['outcome'], 'committed')
        self.assertEqual(receipt['imported']['sheets'], 3)


class EngineStageImportTests(unittest.TestCase):
    """The engine is handed a stage path in the service-only field; these exercise rollback, hooks and cancellation."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.book = Workbook(self.temp/'book.dhwb', create=True)
        self.addCleanup(lambda: self.book.close())  # the reopen test replaces self.book

    def stage(self, sheets, name='book'):
        path = self.temp/(name+'.stage')
        return path, build_stage(path, sheets)

    def request(self, path, summary, **extra):
        return {'action': 'workbook_import', 'stage_id': summary['stage_id'], 'stage_digest': summary['stage_digest'],
                STAGE_PATH: str(path), 'operation_id': 'import', 'expected_state': self.book.state(), **extra}

    def catalog(self):
        return [(s['id'], s['name']) for s in self.book.info()['sheets']]

    def revision(self):
        return self.book.db.execute('SELECT revision FROM dh_meta').fetchone()[0]

    def test_failure_midway_rolls_back_sheets_cells_and_receipt(self):
        path, summary = self.stage([('One', number_cells(5)), ('Two', number_cells(5))])
        before, revision = self.catalog(), self.revision()
        original = StageReader.iter_cells
        def failing(stage, position, batch=10_000):
            if position == 1: raise EngineError('INJECTED', 'late failure')
            yield from original(stage, position, batch)
        with patch.object(StageReader, 'iter_cells', failing), self.assertRaises(EngineError) as failure:
            self.book.mutate(self.request(path, summary))
        self.assertEqual(failure.exception.body['outcome'], 'rolled_back')
        self.assertEqual(self.catalog(), before)
        self.assertEqual(self.revision(), revision)
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM cells').fetchone()[0], 0)
        self.assertEqual(self.book.operation('import')['code'], 'INJECTED')
        self.assertEqual(self.book.operation('import')['outcome'], 'rolled_back')

    def test_cancel_during_load_leaves_the_workbook_as_before(self):
        path, summary = self.stage([('One', number_cells(50)), ('Two', number_cells(50))])
        self.book.mutate({'action': 'edit', 'operation_id': 'seed', 'expected_state': self.book.state(), 'cells': [{'row': 1, 'col': 1, 'source': '5'}]})
        before, state, revision = self.catalog(), self.book.state(), self.revision()
        cancel = threading.Event()
        original = StageReader.iter_cells
        batches = []
        def cancelling(stage, position, batch=10_000):
            for rows in original(stage, position, batch):
                if batch != 10: yield rows; continue  # the digest check, before loading
                batches.append(len(rows))
                if len(batches) == 3: cancel.set()
                yield rows
        with patch.object(engine_module, 'STAGE_BATCH', 10), patch.object(StageReader, 'iter_cells', cancelling), \
                self.assertRaises(EngineError) as failure:
            self.book.mutate(self.request(path, summary, target='add_sheets'), cancel_event=cancel)
        self.assertEqual(batches, [10, 10, 10])
        self.assertEqual((failure.exception.body['code'], failure.exception.body['outcome']), ('CANCELLED', 'rolled_back'))
        self.assertEqual(self.catalog(), before)
        self.assertEqual((self.book.state(), self.revision()), (state, revision))
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM cells').fetchone()[0], 1)
        self.assertEqual(self.book.operation('import')['code'], 'CANCELLED')

    def test_required_handler_without_structure_opt_in_rejects_before_change(self):
        path, summary = self.stage([('One', number_cells(5))])
        self.book.handler = str(self.temp/'required.py')
        before = self.catalog()
        for target in ('replace_empty', 'add_sheets'):
            with self.assertRaises(EngineError) as failure:
                self.book.mutate(self.request(path, summary, target=target, operation_id='handler-'+target))
            self.assertEqual(failure.exception.body['code'], 'UNSUPPORTED_HOOK_SCOPE')
            self.assertEqual(self.catalog(), before)
        self.assertEqual(self.book.db.execute('SELECT count(*) FROM cells').fetchone()[0], 0)

    def test_structure_handler_sees_one_event_without_cell_changes_and_can_veto(self):
        self.book.handler = str(IMPORT_HOOK)
        path, summary = self.stage([('One', number_cells(30)), ('Two', number_cells(2))])
        receipt = self.book.mutate(self.request(path, summary, target='add_sheets'))
        self.assertEqual(receipt['imported'], {'sheets': 2, 'cells': 32})
        marker = self.book.raw(self.book.sheet('Sheet1'), 1, 30)
        self.assertEqual(json.loads(marker['source']), {'phases': ['before', 'after'], 'target': 'add_sheets',
                                                        'sheets': [{'name': 'One', 'cells': 30}, {'name': 'Two', 'cells': 2}]})
        self.assertEqual([name for _, name in self.catalog()], ['Sheet1', 'One', 'Two'])
        veto_path, veto = self.stage([('Veto', number_cells(3))], 'veto')
        before, revision = self.catalog(), self.revision()
        with self.assertRaises(EngineError) as failure:
            self.book.mutate(self.request(veto_path, veto, target='add_sheets', operation_id='veto'))
        self.assertEqual((failure.exception.body['code'], failure.exception.body['outcome']), ('HOOK_FAILED', 'rolled_back'))
        self.assertEqual((self.catalog(), self.revision()), (before, revision))
        small = {'action': 'workbook_import', 'target': 'add_sheets', 'operation_id': 'small', 'expected_state': self.book.state(),
                 'sheets': [{'name': 'Three', 'cells': [{'row': 1, 'col': 1, 'source': '7'}]}]}
        self.assertEqual(self.book.mutate(small)['imported'], {'sheets': 1, 'cells': 1})
        marker = self.book.raw(self.book.sheet('Sheet1'), 1, 30)
        self.assertEqual(json.loads(marker['source'])['sheets'], [{'name': 'Three', 'cells': 1}])

    def test_reopen_after_staged_import_finds_the_data(self):
        path, summary = self.stage([('Data', number_cells(25)), ('Empty', [])])
        receipt = self.book.mutate(self.request(path, summary))
        self.assertEqual(receipt['imported'], {'sheets': 2, 'cells': 25})
        self.assertIsNone(self.book.info()['undo'])
        self.book.close()
        self.book = Workbook(self.temp/'book.dhwb')
        self.assertEqual([name for _, name in self.catalog()], ['Data', 'Empty'])
        self.assertEqual(self.book.read('Data', {'r1': 25, 'c1': 1})['cells'][0]['value'], 25)
        self.assertEqual(self.book.operation('import'), receipt)

    def test_missing_stage_path_is_not_found_unless_the_operation_was_recorded(self):
        path, summary = self.stage([('Data', number_cells(2))])
        request = self.request(path, summary)
        receipt = self.book.mutate(request)
        path.unlink()
        self.assertEqual(self.book.mutate({**request, STAGE_PATH: None}), receipt)
        with self.assertRaises(EngineError) as failure:
            self.book.mutate({**request, STAGE_PATH: None, 'operation_id': 'other'})
        self.assertEqual(failure.exception.body['code'], 'JOB_NOT_FOUND')


if __name__ == '__main__':
    unittest.main()
