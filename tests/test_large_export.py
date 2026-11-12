# SPDX-License-Identifier: GPL-3.0-only
"""Whole-workbook XLSX export as a background job into private staging, downloaded as a streamed binary response."""
import base64
from contextlib import contextmanager
import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import random
import socket
import struct
import sys
import tempfile
import threading
import time
import tracemalloc
try:
    import resource
except ImportError:
    resource = None
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from datahorizon import engine as engine_module
from datahorizon import service as service_module
from datahorizon import xlsx_stream
from datahorizon import xlsx_styles
from datahorizon.client import APIError
from datahorizon.engine import EngineError
from datahorizon.interchange import EXPORT_UNEVALUABLE_DETAIL, xlsx_preview
from datahorizon.service import Service, openapi
from datahorizon.staging import Staging
from datahorizon.xlsx_stream import LARGE_CELLS

try:
    import openpyxl
except ImportError:
    openpyxl = None
try:
    from openapi_schema_validator import OAS30Validator
except ImportError:
    OAS30Validator = None

XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
MAIN = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
UNTOKENIZABLE = '=A5 ` 1'
IFS = '=IFS(A2>1,"big",TRUE,"small")'


class Strings:
    def __init__(self): self.items = {}
    def put(self, index, text): self.items[index] = text
    def get(self, index): return self.items[index]


class CollectingSink:
    """Collects what the streaming reader produces, sheet by sheet."""
    def __init__(self):
        self.strings = Strings()
        self.sheets = []

    def sheet(self, name): self.sheets.append({'name': name, 'cells': []})

    def cells(self, batch): self.sheets[-1]['cells'].extend(batch)


def stored_rows(engine):
    with engine.lock:
        return engine.db.execute("SELECT sheets.name, row, col, source, kind, style FROM cells JOIN sheets ON sheets.id = cells.sheet_id "
                                 "ORDER BY sheets.position, row, col").fetchall()


def snapshot(engine):
    """read_workbook without the state; a number's source is compared by value, because XLSX stores the number
    and the existing exporter writes it as Python float text (42 becomes 42.0), as the bounded export does."""
    result = engine.read_workbook()
    result.pop('state')
    for sheet in result['sheets']:
        for cell in sheet['cells']:
            if cell['kind'] == 'number': cell['source'] = float(cell['source'])
    return result


def inline_text(data, reference):
    """The inline-string text of one cell of a worksheet part."""
    root = ET.fromstring(data)
    for cell in root.iter(MAIN+'c'):
        if cell.get('r') == reference:
            return ''.join(t.text or '' for t in cell.iter(MAIN+'t'))
    return None


class LargeExportTests(unittest.TestCase):
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

    # --- HTTP helpers -------------------------------------------------------------------------------------------
    def connection(self):
        return http.client.HTTPConnection('127.0.0.1', self.service.server_port, timeout=120)

    def request(self, method, path, body=None, content_type='application/json', headers=None):
        connection = self.connection()
        try:
            data = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
            sent = {'Authorization': 'Bearer '+self.service.token, 'Content-Type': content_type, **(headers or {})}
            connection.request(method, path, body=data if method != 'GET' else None, headers={k: v for k, v in sent.items() if v is not None})
            reply = connection.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            connection.close()

    def call(self, path, body=None, method='POST'):
        status, result = self.request(method, path, body)
        if status != 200: raise APIError(status, result)
        return result

    def download(self, export_id, headers=None):
        """(status, headers, body bytes); a JSON error body is decoded."""
        connection = self.connection()
        try:
            sent = {'Authorization': 'Bearer '+self.service.token, **(headers or {})}
            connection.request('GET', '/v1/exports/'+export_id, headers={k: v for k, v in sent.items() if v is not None})
            reply = connection.getresponse()
            data = reply.read()
            if reply.status != 200: data = json.loads(data)
            return reply.status, dict(reply.getheaders()), data
        finally:
            connection.close()

    def open(self, path, create=False):
        return self.call('/v1/open', {'path': str(path), 'create': create})['id']

    def info(self, book=None):
        return self.call(f'/v1/workbooks/{book or self.book}', method='GET')

    def operate(self, request, book=None):
        request = {'expected_state': self.info(book)['state'], **request}
        request.setdefault('operation_id', 'op-'+str(time.monotonic_ns()))
        return self.call(f'/v1/workbooks/{book or self.book}/operations', request)

    def edit(self, cells, sheet='Sheet1', book=None):
        return self.operate({'action': 'edit', 'sheet': sheet, 'cells': cells}, book)

    def engine(self, book=None):
        return self.service.workbooks[book or self.book]

    def start(self, job_id='export', book=None, **request):
        return self.call(f'/v1/workbooks/{book or self.book}/export/xlsx/jobs', {'job_id': job_id, 'mode': 'formulas', **request})

    def terminal(self, job_id, seconds=120):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            job = self.call('/v1/jobs/'+job_id, method='GET')
            if job['status'] != 'running': return job
            time.sleep(.01)
        self.fail('Export did not finish')

    def export(self, job_id='export', book=None):
        self.start(job_id, book)
        job = self.terminal(job_id)
        self.assertEqual(job['status'], 'completed', job)
        return job['result']

    def downloaded(self, export_id):
        status, headers, data = self.download(export_id)
        self.assertEqual(status, 200, data)
        return data

    def export_file(self, export_id):
        return self.service.staging.root/f'export-{export_id}.export'

    def wait_idle(self):
        slots = self.service._request_slots
        for _ in range(500):
            if slots._value == slots._initial_value: return
            time.sleep(.01)
        self.fail('Request slots were not released')

    def reimport(self, data, job_id):
        """Upload, prepare a stage and apply it into a new workbook; return the new handle."""
        status, receipt = self.request('PUT', '/v1/uploads/'+job_id, data, 'application/octet-stream')
        self.assertEqual(status, 200, receipt)
        self.call('/v1/import/xlsx/jobs', {'job_id': job_id, 'upload_id': job_id, 'scope': 'workbook'})
        job = self.terminal(job_id)
        self.assertEqual(job['status'], 'completed', job)
        summary = job['result']
        target = self.open(self.temp/(job_id+'.dhwb'), create=True)
        receipt = self.operate({'action': 'workbook_import', 'stage_id': summary['stage_id'], 'stage_digest': summary['stage_digest']}, target)
        self.assertEqual(receipt['outcome'], 'committed')
        return target, summary

    @contextmanager
    def gated(self):
        """Hold the export's writer at its first checkpoint, after the snapshot and with the file open."""
        reached, proceed = threading.Event(), threading.Event()
        real = service_module.write_workbook
        def wrapper(path, sheets, report, checkpoint):
            def gate(completed, phase):
                if not reached.is_set():
                    reached.set()
                    proceed.wait(30)
                checkpoint(completed, phase)
            return real(path, sheets, report, gate)
        with patch.object(service_module, 'write_workbook', wrapper):
            try:
                yield reached, proceed
            finally:
                proceed.set()

    def fixture_book(self):
        """Text, numbers, an unevaluable IFS, a cross-sheet formula, a styled cell and an empty sheet."""
        self.operate({'action': 'sheet_add', 'name': 'Data'})
        self.operate({'action': 'sheet_add', 'name': 'Empty'})
        self.edit([{'row': 1, 'col': 1, 'source': '5'}, {'row': 2, 'col': 1, 'source': 'second'}], 'Data')
        self.edit([{'row': 1, 'col': 1, 'source': 'hello <&> "world"'},
                   {'row': 2, 'col': 1, 'source': '42'},
                   {'row': 1, 'col': 2, 'source': IFS},
                   {'row': 2, 'col': 2, 'source': '=Data!A1*2'},
                   {'row': 3, 'col': 2, 'source': '=SUM(A2,Data!A1)'},
                   {'row': 1, 'col': 3, 'source': '7', 'style': {'bold': True, 'background': '#ff0000', 'align': 'center'}},
                   {'row': 2, 'col': 3, 'source': 'TRUE'},
                   {'row': 3, 'col': 3, 'source': '2.5', 'style': {'format': 'percent', 'decimals': 1}}])

    # --- Tests --------------------------------------------------------------------------------------------------
    def test_round_trip_through_download_and_stage_import(self):
        self.fixture_book()
        before = snapshot(self.engine())
        result = self.export()
        self.assertEqual(set(result), {'export_id', 'bytes', 'sha256', 'state', 'report', 'report_entries', 'totals'})
        self.assertEqual(result['export_id'], 'export')
        self.assertEqual(result['state'], self.info()['state'])
        self.assertTrue(any('_xlfn' in line for line in result['report']), result['report'])
        status, headers, data = self.download('export')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], XLSX_TYPE)
        self.assertEqual(int(headers['Content-Length']), len(data))
        self.assertEqual(headers['Content-Disposition'], 'attachment; filename="export.xlsx"')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (result['bytes'], result['sha256']))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertNotIn('xl/sharedStrings.xml', archive.namelist())
            sheet1 = archive.read('xl/worksheets/sheet1.xml')
            workbook = archive.read('xl/workbook.xml')
        self.assertIn(b'_xlfn.IFS(', sheet1)
        self.assertNotIn(b'<v>', sheet1.split(b'</c>')[1], 'formulas carry no cached value')
        self.assertIn(b'fullCalcOnLoad="1"', workbook)
        target, summary = self.reimport(data, 'again')
        self.assertTrue(any(e['category'] == 'formula.unsupported_function' and e['severity'] == 'unevaluable'
                            for e in summary['report_entries']), summary['report_entries'])
        self.assertEqual(snapshot(self.engine(target)), before)
        self.assertEqual([s['name'] for s in self.info(target)['sheets']], ['Sheet1', 'Data', 'Empty'])

    @unittest.skipIf(openpyxl is None, 'openpyxl is not installed')
    def test_independent_reader_sees_values_and_future_prefix(self):
        self.fixture_book()
        self.export()
        loaded = openpyxl.load_workbook(io.BytesIO(self.downloaded('export')))
        self.assertEqual(loaded.sheetnames, ['Sheet1', 'Data', 'Empty'])
        sheet = loaded['Sheet1']
        self.assertEqual(sheet['A1'].value, 'hello <&> "world"')
        self.assertEqual(sheet['A2'].value, 42)
        self.assertEqual(sheet['B1'].value, '='+'_xlfn.'+IFS[1:])
        self.assertEqual(sheet['B2'].value, '=Data!A1*2')
        self.assertTrue(sheet['C1'].font.b)
        self.assertEqual(loaded['Empty'].max_row, 1)

    def test_streamed_file_matches_the_bounded_export_and_both_readers(self):
        self.fixture_book()
        self.edit([{'row': 4, 'col': 1, 'source': '2024-02-29', 'kind': 'date'},
                   {'row': 5, 'col': 1, 'source': 'tab\tand control \x01 _x0041_'},
                   {'row': 4, 'col': 2, 'source': '3', 'style': {'italic': True, 'font_size': 14}},
                   {'row': 6, 'col': 1, 'source': '', 'kind': 'null', 'style': {'border': 'box'}}])
        streamed = self.downloaded(self.export()['export_id'])
        bounded = base64.b64decode(self.call(f'/v1/workbooks/{self.book}/export/xlsx', {'scope': 'workbook'})['file_base64'])
        with zipfile.ZipFile(io.BytesIO(streamed)) as left, zipfile.ZipFile(io.BytesIO(bounded)) as right:
            self.assertEqual(sorted(left.namelist()), sorted(right.namelist()))
            for name in right.namelist():
                # Byte for byte: one cell writer, and style indexes in the same first-use order.
                self.assertEqual(left.read(name), right.read(name), name)
        # The streaming reader and the tree reader read the streamed file back identically.
        sink = CollectingSink()
        package = self.temp/'streamed.xlsx'
        package.write_bytes(streamed)
        report = xlsx_stream.read_workbook(package, {}, sink, lambda *_: None)
        tree = xlsx_preview({'scope': 'workbook', 'file_base64': base64.b64encode(streamed).decode()})
        self.assertEqual([s['name'] for s in sink.sheets], [s['name'] for s in tree['workbook_sheets']])
        for streamed_sheet, tree_sheet in zip(sink.sheets, tree['workbook_sheets']):
            self.assertEqual(streamed_sheet['cells'], tree_sheet['cells'], streamed_sheet['name'])
        self.assertEqual(report.entries(), tree['report_entries'])
        self.assertEqual(sum(len(s['cells']) for s in sink.sheets), 14)

    def test_formula_results_are_not_evaluated_for_display(self):
        # The one difference from the bounded export: it gives an unformatted formula with a temporal result a
        # calendar display, which needs evaluation; the large export does not evaluate and says so in its report.
        self.edit([{'row': 1, 'col': 1, 'source': '2024-02-29', 'kind': 'date'}, {'row': 1, 'col': 2, 'source': '=A1'},
                   {'row': 1, 'col': 3, 'source': '=A1', 'style': {'format': 'date_iso'}}])
        result = self.export()
        self.assertIn(xlsx_stream.FORMULA_DISPLAY_LINE, result['report'])
        # Only the unformatted formula is itemized; a formatted one displays as its format says.
        self.assertEqual([(e['category'], e['severity'], e['count'], e['samples']) for e in result['report_entries']],
                         [(xlsx_stream.FORMULA_DISPLAY_CATEGORY, 'approximated', 1, [{'sheet': 'Sheet1', 'cell': 'B1'}])])
        bounded = base64.b64decode(self.call(f'/v1/workbooks/{self.book}/export/xlsx', {'scope': 'workbook'})['file_base64'])
        styles = []
        for data in (self.downloaded('export'), bounded):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            styles.append({c.get('r'): c.get('s') for c in root.iter(MAIN+'c')})
        self.assertEqual(styles, [{'A1': '1', 'B1': '0', 'C1': '1'}, {'A1': '1', 'B1': '1', 'C1': '1'}])

    def test_sixty_thousand_cells_bounded_memory_identical_reimport_and_disconnect(self):
        rng = random.Random(15)
        for start in range(1, 60_001, 10_000):
            # Text kind: XLSX has no automatic kind, so automatic text would come back as text.
            self.edit([{'row': (n-1)//10+1, 'col': (n-1) % 10+1, 'source': '%032x' % rng.getrandbits(128), 'kind': 'text'}
                       for n in range(start, start+10_000)])
        # ru_maxrss is a high-water mark (KiB on Linux, bytes on macOS), so its growth is a ceiling check: it cannot
        # show memory the export used below an earlier peak, only that the process never grew past the bound.
        scale = 1 if sys.platform == 'darwin' else 1024
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*scale if resource is not None else None
        tracemalloc.start()
        try:
            result = self.export('big')
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 64*1024*1024)
        if rss is not None:
            self.assertLess(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*scale-rss, 192*1024*1024)
        self.assertEqual(result['totals'], {'converted': 0, 'approximated': 0, 'omitted': 0, 'unevaluable': 0})
        self.assertNotIn(xlsx_stream.FORMULA_DISPLAY_LINE, result['report'], 'no formulas, so no formula-display line')
        data = self.downloaded('big')
        target, summary = self.reimport(data, 'bigagain')
        self.assertEqual(summary['stored_cells'], 60_000)
        self.assertEqual(stored_rows(self.engine(target)), stored_rows(self.engine()))
        # A client that disconnects mid-download neither crashes the handler nor leaks the file handle.
        self.assertGreater(len(data), 1024*1024)
        export = self.export_file('big').stat()
        peer = socket.create_connection(('127.0.0.1', self.service.server_port))
        peer.sendall(f'GET /v1/exports/big HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {self.service.token}\r\n\r\n'.encode())
        self.assertTrue(peer.recv(4096).startswith(b'HTTP/1.0 200'))
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        peer.close()
        self.wait_idle()
        self.assertEqual(self.service._downloads, {})
        open_handles = []
        for name in os.listdir('/dev/fd') if os.path.isdir('/dev/fd') else ():
            try: info = os.fstat(int(name))
            except OSError: continue
            if (info.st_dev, info.st_ino) == (export.st_dev, export.st_ino): open_handles.append(name)
        self.assertEqual(open_handles, [])
        self.assertEqual(hashlib.sha256(self.downloaded('big')).hexdigest(), result['sha256'])

    def test_edit_after_start_is_not_exported_and_download_waits_for_completion(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'old'}])
        state = self.info()['state']
        with self.gated() as (reached, proceed):
            self.start()
            self.assertTrue(reached.wait(30))
            status, _, error = self.download('export')
            self.assertEqual((status, error['code']), (404, 'JOB_NOT_FOUND'))
            # The export holds no workbook lock while it writes: a managed edit commits meanwhile.
            self.edit([{'row': 1, 'col': 1, 'source': 'new'}])
            proceed.set()
            job = self.terminal('export')
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(job['result']['state'], state)
        self.assertNotEqual(self.info()['state'], state)
        with zipfile.ZipFile(io.BytesIO(self.downloaded('export'))) as archive:
            self.assertEqual(inline_text(archive.read('xl/worksheets/sheet1.xml'), 'A1'), 'old')

    def test_cancel_removes_the_partial_file(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        with self.gated() as (reached, proceed):
            self.start()
            self.assertTrue(reached.wait(30))
            self.assertTrue(self.export_file('export').exists())
            self.call('/v1/jobs/export/cancel')
            proceed.set()
            job = self.terminal('export')
        self.assertEqual((job['status'], job['error']['code']), ('cancelled', 'CANCELLED'))
        self.assertFalse(self.export_file('export').exists())
        self.assertEqual(self.download('export')[0], 404)
        self.assertEqual([p.name for p in self.service.staging.root.iterdir()], [])

    def test_release_removes_the_file_and_downloads_repeat_until_then(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        result = self.export()
        first, second = self.downloaded('export'), self.downloaded('export')
        self.assertEqual(first, second)
        self.assertEqual(hashlib.sha256(first).hexdigest(), result['sha256'])
        self.assertEqual(self.call('/v1/exports/export/release'), {'released': True, 'export_id': 'export'})
        self.assertFalse(self.export_file('export').exists())
        status, _, error = self.download('export')
        self.assertEqual((status, error['code']), (404, 'JOB_NOT_FOUND'))
        self.assertEqual(self.request('GET', '/v1/jobs/export')[0], 404, 'release consumes the job')
        status, error = self.request('POST', '/v1/exports/export/release')
        self.assertEqual((status, error['code']), (404, 'JOB_NOT_FOUND'))

    def test_download_refreshes_retention(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        self.export()
        old = time.time()-600
        os.utime(self.export_file('export'), (old, old))
        with self.service.jobs.lock: self.service.jobs.entries['export']['finished'] -= 200
        self.downloaded('export')
        self.assertGreater(self.export_file('export').stat().st_mtime, old+500)
        with self.service.jobs.lock:
            self.assertLess(time.monotonic()-self.service.jobs.entries['export']['finished'], 100)

    def test_import_job_ids_are_not_downloads(self):
        status, _ = self.request('PUT', '/v1/uploads/imported', b'not a zip', 'application/octet-stream')
        self.assertEqual(status, 200)
        self.call('/v1/import/csv/jobs', {'job_id': 'csvjob', 'csv_base64': base64.b64encode(b'a,b\n1,2\n').decode()})
        self.assertEqual(self.terminal('csvjob')['status'], 'completed')
        for identifier in ('csvjob', 'imported', 'missing'):
            status, _, error = self.download(identifier)
            self.assertEqual((status, error['code']), (404, 'JOB_NOT_FOUND'), identifier)
        status, _, error = self.download('bad.name')
        self.assertEqual((status, error['code']), (400, 'INVALID_JOB'))

    def test_download_requires_token_and_same_origin(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        self.export()
        status, _, error = self.download('export', {'Authorization': None})
        self.assertEqual((status, error['code']), (401, 'UNAUTHORIZED'))
        status, _, error = self.download('export', {'Authorization': 'Bearer wrong'})
        self.assertEqual((status, error['code']), (401, 'UNAUTHORIZED'))
        status, _, error = self.download('export', {'Origin': 'http://evil.example'})
        self.assertEqual((status, error['code']), (403, 'INVALID_ORIGIN'))
        self.assertEqual(self.download('export', {'Origin': f'http://127.0.0.1:{self.service.server_port}'})[0], 200)

    def test_third_concurrent_download_is_busy(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        self.export()
        with self.service.download_slot('export'), self.service.download_slot('export'):
            status, _, error = self.download('export')
            self.assertEqual((status, error['code']), (409, 'DOWNLOAD_BUSY'))
            status, error = self.request('POST', '/v1/exports/export/release')
            self.assertEqual((status, error['code']), (409, 'JOB_CONFLICT'))
        self.assertEqual(self.service._downloads, {})
        self.assertEqual(self.download('export')[0], 200)

    def test_values_and_mixed_modes_and_bad_requests_are_refused(self):
        for mode in ('values', 'mixed'):
            status, error = self.request('POST', f'/v1/workbooks/{self.book}/export/xlsx/jobs', {'job_id': 'export', 'mode': mode})
            self.assertEqual((status, error['code']), (400, 'UNSUPPORTED'), mode)
            self.assertIn('/export/xlsx', error['message'])
        for body, code in (({'job_id': 'export', 'mode': 'sideways'}, 'INVALID_ARGUMENT'),
                           ({'job_id': 'x'*65}, 'INVALID_JOB'),
                           ({'job_id': 'bad.name'}, 'INVALID_JOB'),
                           ({'job_id': 'export', 'scope': 'selection'}, 'INVALID_ARGUMENT')):
            status, error = self.request('POST', f'/v1/workbooks/{self.book}/export/xlsx/jobs', body)
            self.assertEqual((status, error['code']), (400, code), body)
        self.assertEqual(list(self.service.staging.root.iterdir()), [])
        self.assertEqual(self.service.jobs.entries, {})

    def test_retry_returns_the_existing_job_and_another_workbook_conflicts(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        result = self.export()
        again = self.call(f'/v1/workbooks/{self.book}/export/xlsx/jobs', {'job_id': 'export'})
        self.assertEqual((again['status'], again['result']), ('completed', result))
        other = self.open(self.temp/'other.dhwb', create=True)
        status, error = self.request('POST', f'/v1/workbooks/{other}/export/xlsx/jobs', {'job_id': 'export', 'mode': 'formulas'})
        self.assertEqual((status, error['code']), (409, 'JOB_CONFLICT'))

    def test_style_limit_fails_the_job_with_limit(self):
        self.edit([{'row': n, 'col': 1, 'source': str(n), 'style': {'font_size': 10+n}} for n in range(1, 4)])
        with patch.object(xlsx_styles, 'MAX_STYLES', 3):
            self.start()
            job = self.terminal('export')
        self.assertEqual((job['status'], job['error']['code']), ('failed', 'LIMIT'), job)
        self.assertIn('3 distinct native styles', job['error']['message'])
        self.assertFalse(self.export_file('export').exists())
        self.call('/v1/jobs/export/release')
        with patch.object(xlsx_styles, 'MAX_STYLES', 4):
            self.export()

    def test_cell_and_expanded_limits_fail_the_job_with_limit(self):
        self.edit([{'row': n, 'col': 1, 'source': str(n)} for n in range(1, 7)])
        for name, value, job_id in (('LARGE_CELLS', 5, 'cells'), ('LARGE_EXPANDED', 2048, 'expanded')):
            with patch.object(xlsx_stream, name, value):
                self.start(job_id)
                job = self.terminal(job_id)
            self.assertEqual((job['status'], job['error']['code']), ('failed', 'LIMIT'), job)
            self.assertFalse(self.export_file(job_id).exists())

    @contextmanager
    def staged_kinds(self):
        """Record every staging entry the service reserves."""
        kinds, real = [], Staging.new
        def new(staging, kind, identifier):
            kinds.append(kind)
            return real(staging, kind, identifier)
        with patch.object(Staging, 'new', new):
            yield kinds

    def failed_before_copy(self, code):
        with self.staged_kinds() as kinds:
            self.start()
            job = self.terminal('export')
        self.assertEqual((job['status'], job['error']['code']), ('failed', code), job)
        self.assertEqual(kinds, ['export'], 'no snapshot is reserved for a workbook that cannot be exported')
        self.assertEqual(list(self.service.staging.root.iterdir()), [])
        return job

    def test_over_limit_workbook_fails_before_any_copy(self):
        self.edit([{'row': n, 'col': 1, 'source': str(n)} for n in range(1, 7)])
        with patch.object(xlsx_stream, 'LARGE_CELLS', 5):
            self.assertIn('5 stored cells', self.failed_before_copy('LIMIT')['error']['message'])
        self.call('/v1/jobs/export/release')
        with patch.object(xlsx_stream, 'LARGE_SHEETS', 1):
            self.operate({'action': 'sheet_add', 'name': 'Two'})
            self.assertIn('1 sheets', self.failed_before_copy('LIMIT')['error']['message'])
        self.call('/v1/jobs/export/release')
        with patch.object(engine_module, 'SNAPSHOT_BYTES', 4096):
            self.assertIn('native file', self.failed_before_copy('LIMIT')['error']['message'])

    def test_too_little_free_space_fails_with_storage_error_and_leaves_nothing(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        with patch.object(engine_module, 'SNAPSHOT_RESERVE', 1 << 62):
            self.start()
            job = self.terminal('export')
        self.assertEqual((job['status'], job['error']['code']), ('failed', 'STORAGE_ERROR'), job)
        self.assertIn('free disk space', job['error']['message'])
        self.assertEqual(list(self.service.staging.root.iterdir()), [])

    def test_cancel_during_the_copy_leaves_no_snapshot_or_export(self):
        self.edit([{'row': n, 'col': 1, 'source': 'x'*100} for n in range(1, 200)])
        engine = self.engine()
        reached, proceed = threading.Event(), threading.Event()
        real = engine.stream_workbook
        def stream_workbook(reserve, *, checkpoint):
            def gate(completed, phase):
                if phase == 'Copying workbook' and not reached.is_set():
                    reached.set()
                    proceed.wait(30)
                checkpoint(completed, phase)
            return real(reserve, checkpoint=gate)
        with patch.object(engine_module, 'SNAPSHOT_PAGES', 1), patch.object(engine, 'stream_workbook', stream_workbook):
            self.start()
            try:
                self.assertTrue(reached.wait(30))
                self.assertEqual(sorted(p.name for p in self.service.staging.root.iterdir() if not p.name.endswith('-journal')),
                                 ['export-export.export', 'snapshot-export.snapshot'])
                self.call('/v1/jobs/export/cancel')
            finally:
                proceed.set()
            job = self.terminal('export')
        self.assertEqual((job['status'], job['error']['code']), ('cancelled', 'CANCELLED'), job)
        self.assertEqual(list(self.service.staging.root.iterdir()), [])
        # The workbook is usable again at once: the lock was released when the copy stopped.
        self.edit([{'row': 1, 'col': 2, 'source': 'after'}])

    def test_completed_export_leaves_no_snapshot(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        self.export()
        self.assertEqual([p.name for p in self.service.staging.root.iterdir()], ['export-export.export'])

    def test_text_over_the_excel_cell_limit_fails_the_job_with_limit(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'x'*32_768, 'kind': 'text'}])
        self.start()
        job = self.terminal('export')
        self.assertEqual((job['status'], job['error']['code']), ('failed', 'LIMIT'), job)
        self.assertIn('32,767', job['error']['message'])

    def test_unmaintained_and_unevaluable_formulas_export_with_their_report(self):
        self.edit([{'row': 5, 'col': 1, 'source': '5'}, {'row': 1, 'col': 2, 'source': UNTOKENIZABLE},
                   {'row': 1, 'col': 3, 'source': IFS}])
        state = self.info()['state']
        receipt = self.operate({'action': 'move', 'sheet': 'Sheet1', 'r1': 5, 'c1': 1, 'r2': 5, 'c2': 1, 'row': 6, 'col': 1, 'cut_state': state})
        self.assertEqual(receipt['unmaintained'], [{'sheet': 'Sheet1', 'cell': 'B1'}])
        result = self.export()
        # The IFS formula is written, and counted as one DataHorizon cannot evaluate.
        self.assertEqual(result['report_entries'], [
            {'category': 'formula.unsupported_function', 'severity': 'unevaluable', 'detail': EXPORT_UNEVALUABLE_DETAIL,
             'count': 1, 'samples': [{'sheet': 'Sheet1', 'cell': 'C1'}]},
            {'category': xlsx_stream.FORMULA_DISPLAY_CATEGORY, 'severity': 'approximated', 'detail': xlsx_stream.FORMULA_DISPLAY_DETAIL,
             'count': 1, 'samples': [{'sheet': 'Sheet1', 'cell': 'C1'}]},
            {'category': 'formula.unmaintained', 'severity': 'approximated',
             'detail': 'references were not maintained through a structural edit; the source is written unchanged',
             'count': 1, 'samples': [{'sheet': 'Sheet1', 'cell': 'B1'}]}])
        self.assertEqual(result['totals'], {'converted': 0, 'approximated': 2, 'omitted': 0, 'unevaluable': 1})
        self.assertTrue(any('_xlfn' in line for line in result['report']))
        self.assertIn('1 cell: references were not maintained through a structural edit; the source is written unchanged '
                      '(approximated); first at Sheet1!B1', result['report'])
        with zipfile.ZipFile(io.BytesIO(self.downloaded('export'))) as archive:
            root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        formulas = {c.get('r'): c.findtext(MAIN+'f') for c in root.iter(MAIN+'c') if c.find(MAIN+'f') is not None}
        self.assertEqual(formulas, {'B1': UNTOKENIZABLE[1:], 'C1': '_xlfn.'+IFS[1:]})

    def test_capabilities_and_openapi_example_execute(self):
        capabilities = self.call('/v1/capabilities', method='GET')
        self.assertEqual((capabilities['export_jobs'], capabilities['max_export_cells']), (True, LARGE_CELLS))
        spec = openapi(); schemas = spec['components']['schemas']
        start = spec['paths']['/v1/workbooks/{workbook}/export/xlsx/jobs']['post']
        self.assertEqual(start['requestBody']['content']['application/json']['schema']['$ref'], '#/components/schemas/XlsxExportJobRequest')
        get = spec['paths']['/v1/exports/{export}']['get']
        self.assertEqual(get['parameters'][0]['schema']['pattern'], '^[A-Za-z0-9_-]{1,64}$')
        self.assertIn(XLSX_TYPE, get['responses']['200']['content'])
        self.assertIn('409', get['responses'])
        release = spec['paths']['/v1/exports/{export}/release']['post']
        self.assertEqual(release['responses']['200']['content']['application/json']['schema']['$ref'], '#/components/schemas/ExportReleased')
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        request = copy.deepcopy(schemas['XlsxExportJobRequest']['example'])
        self.call(f'/v1/workbooks/{self.book}/export/xlsx/jobs', request)
        job = self.terminal(request['job_id'])
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(set(job['result']), set(schemas['XlsxExportJobResult']['required']))
        released = schemas['ExportReleased']['example']
        self.assertEqual(hashlib.sha256(self.downloaded(released['export_id'])).hexdigest(), job['result']['sha256'])
        self.assertEqual(self.call(f'/v1/exports/{released["export_id"]}/release'), released)
        if OAS30Validator is not None:
            for name, value in (('XlsxExportJobRequest', request), ('XlsxExportJobResult', job['result']), ('ExportReleased', released)):
                OAS30Validator({**schemas[name], 'components': spec['components']}).validate(value)


if __name__ == '__main__':
    unittest.main()
