# SPDX-License-Identifier: GPL-3.0-only
"""Whole-workbook XLSX preparation from an upload or a local path into an on-disk import stage."""
from contextlib import contextmanager
import base64
import copy
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xlsx_builder import workbook_bytes

from datahorizon import service as service_module
from datahorizon.client import APIError, LocalService
from datahorizon.engine import EngineError
from datahorizon.jobs import PreparationJobs
from datahorizon.service import Service, openapi
from datahorizon.xlsx_stream import LARGE_PACKAGE

try:
    from openapi_schema_validator import OAS30Validator
except ImportError:
    OAS30Validator = None

FIXTURE = Path(__file__).resolve().parent/'fixtures'/'fidelity.xlsx'


def large_workbook(rows, cols=10):
    data = ''.join(f'<row r="{r}">' + ''.join(f'<c r="{chr(64+c)}{r}"><v>{r*c}</v></c>' for c in range(1, cols+1)) + '</row>'
                   for r in range(1, rows+1))
    return workbook_bytes({'Big': data})


def encrypted_package():
    data = bytearray(FIXTURE.read_bytes())
    central = data.index(b'PK\x01\x02')
    data[central+8] |= 1
    return bytes(data)


@contextmanager
def running(**limits):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)/'staging'
        with Service(staging_root=root, **limits) as server:
            worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
            worker.start()
            try:
                yield server, Path(temp)
            finally:
                server.shutdown(); worker.join(timeout=2)


def call(port, token, method, path, body=None, content_type='application/json'):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
    try:
        data = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
        connection.request(method, path, body=data if method != 'GET' else None,
                           headers={'Authorization': 'Bearer '+token, 'Content-Type': content_type})
        reply = connection.getresponse()
        return reply.status, json.loads(reply.read())
    finally:
        connection.close()


class LargePreparationTests(unittest.TestCase):
    def setUp(self):
        self.stack = []

    def post(self, path, body):
        return call(self.server.server_port, self.server.token, 'POST', path, body)

    def get(self, path):
        return call(self.server.server_port, self.server.token, 'GET', path)

    def upload(self, identifier, data):
        status, body = call(self.server.server_port, self.server.token, 'PUT', '/v1/uploads/'+identifier, data, 'application/octet-stream')
        self.assertEqual(status, 200, body)
        return body

    def prepare(self, body, status=200):
        got, result = self.post('/v1/import/xlsx/jobs', body)
        self.assertEqual(got, status, result)
        return result

    def rejected(self, body, status, code):
        result = self.prepare(body, status)
        self.assertEqual(result['code'], code, result)
        return result

    def terminal(self, job_id, seconds=60):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            status, job = self.get('/v1/jobs/'+job_id)
            self.assertEqual(status, 200, job)
            if job['status'] != 'running': return job
            time.sleep(.01)
        self.fail('Job did not terminate')

    def stages(self):
        return sorted(p.name for p in self.server.staging.root.iterdir() if p.name.startswith('stage-'))

    @contextmanager
    def service(self, **limits):
        with running(**limits) as (server, temp):
            self.server, self.temp = server, temp
            yield server

    def test_upload_prepares_a_stage_summary_without_cells(self):
        with self.service():
            receipt = self.upload('fidelity', FIXTURE.read_bytes())
            started = self.prepare({'job_id': 'big-1', 'upload_id': 'fidelity', 'scope': 'workbook', 'mode': 'mixed'})
            self.assertEqual(started['status'], 'running')
            job = self.terminal('big-1')
            self.assertEqual(job['status'], 'completed', job)
            summary = job['result']
            self.assertEqual([s['name'] for s in summary['sheets']], ['Inputs', 'Summary', 'Empty'])
            self.assertEqual(summary['stored_cells'], 49)
            self.assertRegex(summary['stage_digest'], '^[0-9a-f]{64}$')
            self.assertEqual(summary['stage_id'], 'big-1')
            self.assertTrue(summary['sample'][0]['cells'])
            self.assertNotIn('workbook_sheets', summary)
            self.assertEqual(summary['format'], 'xlsx'); self.assertEqual(summary['mode'], 'mixed')
            self.assertEqual(summary['source'], {'kind': 'upload', 'upload_id': 'fidelity', 'sha256': receipt['sha256']})
            self.assertEqual(self.stages(), ['stage-big-1.stage'])
            # The upload stays until the client releases it.
            self.assertIn('upload-fidelity.upload', os.listdir(self.server.staging.root))

    def test_retry_returns_the_same_job_and_one_stage(self):
        with self.service():
            self.upload('u', FIXTURE.read_bytes())
            request = {'job_id': 'retry', 'upload_id': 'u', 'scope': 'workbook'}
            self.prepare(request)
            first = self.terminal('retry')
            again = self.prepare(request)
            self.assertEqual(again['status'], 'completed'); self.assertEqual(again['result'], first['result'])
            self.assertEqual(self.stages(), ['stage-retry.stage'])
            self.rejected({**request, 'mode': 'values'}, 409, 'JOB_CONFLICT')
            self.assertEqual(self.stages(), ['stage-retry.stage'])

    def test_release_cancel_and_expiry_remove_the_stage(self):
        with self.service():
            self.upload('u', FIXTURE.read_bytes())
            for job_id in ('released', 'discarded', 'expired'):
                self.prepare({'job_id': job_id, 'upload_id': 'u', 'scope': 'workbook'})
                self.assertEqual(self.terminal(job_id)['status'], 'completed')
            self.assertEqual(len(self.stages()), 3)
            self.assertEqual(self.post('/v1/jobs/released/release', {})[0], 200)
            self.assertNotIn('stage-released.stage', self.stages())
            status, job = self.post('/v1/jobs/discarded/cancel', {})
            self.assertEqual((status, job['status']), (200, 'cancelled')); self.assertNotIn('result', job)
            self.assertNotIn('stage-discarded.stage', self.stages())
            self.server.jobs.retention = 0
            self.assertEqual(self.get('/v1/jobs/expired')[1]['code'], 'JOB_NOT_FOUND')
            self.assertEqual(self.stages(), [])

    def test_cancel_during_reading_stops_and_removes_the_stage(self):
        reached, proceed = threading.Event(), threading.Event()
        real = service_module.read_workbook
        def gated(path, request, sink, checkpoint):
            def tick(completed, phase):
                if completed >= 1000 and not reached.is_set():
                    reached.set(); proceed.wait(10)
                checkpoint(completed, phase)
            return real(path, request, sink, tick)
        with self.service(), patch.object(service_module, 'read_workbook', gated):
            self.upload('u', large_workbook(1000))
            self.prepare({'job_id': 'stop', 'upload_id': 'u', 'scope': 'workbook'})
            self.assertTrue(reached.wait(10))
            self.assertEqual(self.stages(), ['stage-stop.stage'])
            self.assertEqual(self.post('/v1/jobs/stop/cancel', {})[0], 200)
            proceed.set()
            job = self.terminal('stop')
            self.assertEqual(job['status'], 'cancelled'); self.assertEqual(job['error']['code'], 'CANCELLED')
            self.assertEqual(self.stages(), [])

    def test_encrypted_package_fails_and_removes_the_stage(self):
        with self.service():
            self.upload('enc', encrypted_package())
            self.prepare({'job_id': 'enc', 'upload_id': 'enc', 'scope': 'workbook'})
            job = self.terminal('enc')
            self.assertEqual(job['status'], 'failed'); self.assertEqual(job['error']['code'], 'INVALID_SPREADSHEET')
            self.assertEqual(self.stages(), [])

    def test_request_validation_is_immediate(self):
        with self.service():
            self.upload('u', FIXTURE.read_bytes())
            data = base64.b64encode(FIXTURE.read_bytes()).decode()
            for body in ({'job_id': 'v', 'upload_id': 'u', 'scope': 'selection'},
                         {'job_id': 'v', 'upload_id': 'u'},
                         {'job_id': 'v', 'upload_id': 'u', 'file_base64': data, 'scope': 'workbook'},
                         {'job_id': 'v', 'upload_id': 'u', 'source_path': str(FIXTURE), 'scope': 'workbook'},
                         {'job_id': 'v', 'scope': 'workbook'},
                         {'job_id': 'v', 'upload_id': 'u', 'scope': 'workbook', 'row': 2},
                         {'job_id': 'v', 'upload_id': 'u', 'scope': 'workbook', 'sheet_index': 1},
                         {'job_id': 'v', 'upload_id': 'u', 'scope': 'workbook', 'mode': 'cached'}):
                self.rejected(body, 400, 'INVALID_ARGUMENT')
            self.rejected({'job_id': 'x'*65, 'upload_id': 'u', 'scope': 'workbook'}, 400, 'INVALID_JOB')
            self.rejected({'job_id': 'v', 'upload_id': 'missing', 'scope': 'workbook'}, 404, 'JOB_NOT_FOUND')
            directory = self.temp/'folder'; directory.mkdir()
            oversize = self.temp/'huge.xlsx'
            with open(oversize, 'wb') as handle: handle.truncate(LARGE_PACKAGE+1)
            for path in ('fidelity.xlsx', str(directory), str(self.temp/'missing.xlsx')):
                self.rejected({'job_id': 'v', 'source_path': path, 'scope': 'workbook'}, 400, 'INVALID_PATH')
            self.rejected({'job_id': 'v', 'source_path': str(oversize), 'scope': 'workbook'}, 400, 'LIMIT')
            self.assertEqual(self.stages(), [])
            self.assertEqual(self.get('/v1/jobs/v')[1]['code'], 'JOB_NOT_FOUND')

    def test_source_path_resolves_symlinks_and_records_the_file(self):
        with self.service():
            target = self.temp/'real.xlsx'; target.write_bytes(FIXTURE.read_bytes())
            link = self.temp/'link.xlsx'; link.symlink_to(target)
            self.prepare({'job_id': 'local', 'source_path': str(link), 'scope': 'workbook', 'mode': 'values'})
            job = self.terminal('local')
            self.assertEqual(job['status'], 'completed', job)
            info = target.stat()
            self.assertEqual(job['result']['source'], {'kind': 'path', 'path': str(target.resolve()),
                                                       'size': info.st_size, 'mtime_ns': info.st_mtime_ns})
            self.assertEqual(job['result']['mode'], 'values')

    def test_source_changed_while_reading_fails(self):
        real = service_module.read_workbook
        def touching(path, request, sink, checkpoint):
            report = real(path, request, sink, checkpoint)
            os.utime(path, ns=(0, 0))
            return report
        with self.service(), patch.object(service_module, 'read_workbook', touching):
            target = self.temp/'moving.xlsx'; target.write_bytes(FIXTURE.read_bytes())
            self.prepare({'job_id': 'moving', 'source_path': str(target), 'scope': 'workbook'})
            job = self.terminal('moving')
            self.assertEqual((job['status'], job['error']['code']), ('failed', 'STALE_PLAN'))
            self.assertEqual(self.stages(), [])

    def test_fifth_stage_is_refused_when_started(self):
        with self.service():
            self.upload('u', FIXTURE.read_bytes())
            for index in range(4):
                self.prepare({'job_id': f'j{index}', 'upload_id': 'u', 'scope': 'workbook'})
                self.terminal(f'j{index}')
            self.rejected({'job_id': 'j4', 'upload_id': 'u', 'scope': 'workbook'}, 409, 'JOB_LIMIT')
            self.assertEqual(len(self.stages()), 4)

    def test_sixty_thousand_cells_exceed_the_old_cap(self):
        with self.service():
            self.upload('big', large_workbook(6000))
            self.prepare({'job_id': 'sixty', 'upload_id': 'big', 'scope': 'workbook'})
            job = self.terminal('sixty', 120)
            self.assertEqual(job['status'], 'completed', job)
            self.assertEqual(job['result']['stored_cells'], 60000)
            self.assertLess(len(json.dumps(job['result']).encode()), 1024*1024)

    def test_file_base64_whole_workbook_is_unchanged(self):
        with self.service():
            data = base64.b64encode(FIXTURE.read_bytes()).decode()
            self.prepare({'job_id': 'inline', 'file_base64': data, 'scope': 'workbook'})
            job = self.terminal('inline')
            self.assertEqual(job['status'], 'completed', job)
            self.assertIn('workbook_sheets', job['result']); self.assertNotIn('stage_id', job['result'])
            self.assertEqual(self.stages(), [])

    def test_capabilities_and_openapi_examples_execute(self):
        spec = openapi(); schemas = spec['components']['schemas']
        route = spec['paths']['/v1/import/xlsx/jobs']['post']['requestBody']['content']['application/json']['schema']
        self.assertEqual(route, {'$ref': '#/components/schemas/XlsxPreparationRequest'})
        variants = [item['$ref'].rsplit('/', 1)[-1] for item in schemas['XlsxPreparationRequest']['oneOf']]
        self.assertEqual(variants, ['SpreadsheetPreparation', 'XlsxUploadPreparation', 'XlsxPathPreparation'])
        self.assertIn('same machine', schemas['XlsxPathPreparation']['properties']['source_path']['description'])
        with self.service():
            self.assertTrue(self.get('/v1/capabilities')[1]['import_source_path'])
            upload = copy.deepcopy(schemas['XlsxUploadPreparation']['example'])
            self.upload(upload['upload_id'], FIXTURE.read_bytes())
            local = copy.deepcopy(schemas['XlsxPathPreparation']['example'])
            local['source_path'] = str(FIXTURE)
            inline = copy.deepcopy(schemas['SpreadsheetPreparation']['example'])
            inline['file_base64'] = base64.b64encode(FIXTURE.read_bytes()).decode()
            if OAS30Validator is not None:
                from jsonschema.exceptions import ValidationError
                with self.assertRaises(ValidationError):
                    OAS30Validator({**schemas['XlsxPreparationRequest'], 'components': spec['components']}).validate({**inline, 'upload_id': 'u'})
            for request in (upload, local, inline):
                if OAS30Validator is not None:
                    OAS30Validator({**schemas['XlsxPreparationRequest'], 'components': spec['components']}).validate(request)
                self.prepare(request)
                job = self.terminal(request['job_id'])
                self.assertEqual(job['status'], 'completed', job)
                self.assertEqual(self.post(f"/v1/jobs/{request['job_id']}/release", {})[0], 200)
            self.assertEqual(self.stages(), [])


class DiscardTests(unittest.TestCase):
    def wait(self, jobs, job_id):
        deadline = time.monotonic()+5
        while (job := jobs.access(job_id))['status'] == 'running':
            self.assertLess(time.monotonic(), deadline); time.sleep(.005)
        return job

    def test_seconds_override_and_discard_once_after_running(self):
        jobs = PreparationJobs()
        calls, entered, resume = [], threading.Event(), threading.Event()
        def blocked(payload, checkpoint):
            entered.set(); resume.wait(5); checkpoint(1, 'Reading')
            return {'ok': True}
        try:
            jobs.start({'job_id': 'slow'}, blocked, seconds=-1, on_discard=lambda: calls.append('slow'))
            self.assertTrue(entered.wait(5))
            jobs.access('slow', 'cancel')
            self.assertEqual(calls, [])  # Never while the job is still running.
            resume.set()
            self.assertEqual(self.wait(jobs, 'slow')['status'], 'cancelled')
            self.assertEqual(calls, ['slow'])
            jobs.access('slow', 'release'); self.assertEqual(calls, ['slow'])
            jobs.start({'job_id': 'late'}, lambda payload, checkpoint: checkpoint(1, 'x'), seconds=-1, on_discard=lambda: calls.append('late'))
            self.assertEqual(self.wait(jobs, 'late')['error']['code'], 'TIMEOUT')
            self.assertEqual(calls, ['slow', 'late'])
            # A retry does not adopt a second callback.
            jobs.start({'job_id': 'late'}, blocked, on_discard=lambda: calls.append('again'))
            jobs.access('late', 'release'); self.assertEqual(calls, ['slow', 'late'])
        finally:
            resume.set(); jobs.close()

    def test_completed_results_discard_on_cancel_expiry_release_and_close(self):
        jobs, calls = PreparationJobs(capacity=5), []
        def failing():
            calls.append('failing'); raise OSError('boom')
        try:
            for name in ('cancel', 'expire', 'release', 'close', 'failing'):
                jobs.start({'job_id': name}, lambda payload, checkpoint: {'ok': True},
                           on_discard=failing if name == 'failing' else (lambda name=name: calls.append(name)))
                self.assertEqual(self.wait(jobs, name)['status'], 'completed')
            self.assertEqual(calls, [])
            jobs.access('cancel', 'cancel'); jobs.access('cancel', 'cancel')
            jobs.access('release', 'release')
            self.assertEqual(calls, ['cancel', 'release'])
            jobs.entries['expire']['finished'] -= jobs.retention
            with self.assertRaises(EngineError): jobs.access('expire')
            self.assertEqual(calls, ['cancel', 'release', 'expire'])
        finally:
            with patch('sys.stderr'): jobs.close()
        self.assertEqual(sorted(calls), ['cancel', 'close', 'expire', 'failing', 'release'])


class LocalServiceTests(unittest.TestCase):
    def test_upload_then_prepare_through_a_local_service(self):
        with LocalService() as client:
            url = urlsplit(client.url)
            status, receipt = call(url.port, client.token, 'PUT', '/v1/uploads/fidelity', FIXTURE.read_bytes(), 'application/octet-stream')
            self.assertEqual(status, 200, receipt)
            client.request('/v1/import/xlsx/jobs', {'job_id': 'local-big', 'upload_id': 'fidelity', 'scope': 'workbook', 'mode': 'mixed'})
            deadline = time.monotonic()+60
            while (job := client.preparation('local-big'))['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(job['status'], 'completed', job)
            self.assertEqual(job['result']['stored_cells'], 49)
            self.assertNotIn('workbook_sheets', job['result'])
            with self.assertRaises(APIError) as failure:
                client.request('/v1/import/xlsx/jobs', {'job_id': 'other', 'upload_id': 'fidelity', 'scope': 'selection'})
            self.assertEqual(failure.exception.body['code'], 'INVALID_ARGUMENT')
            self.assertEqual(client.preparation('local-big', 'release'), {'released': True, 'job_id': 'local-big'})


if __name__ == '__main__':
    unittest.main()
