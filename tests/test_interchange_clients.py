# SPDX-License-Identifier: GPL-3.0-only
"""The large XLSX/ODS path through the Python SDK and the managed CLI: upload or local path, staged import, export download."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from datahorizon import cli as cli_module
from datahorizon.client import APIError, Client, IntegrityError, LocalService
from datahorizon.engine import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ods_builder import manifest_document, workbook_bytes
from xlsx_builder import workbook_bytes as xlsx_bytes

FIXTURES = Path(__file__).resolve().parent/'fixtures'
CLI = [sys.executable, '-m', 'datahorizon.cli']
FIDELITY_SHEETS = ['Inputs', 'Summary', 'Empty']


def contents(path):
    """Every sheet's stored cells and calculated values, read directly from the native file."""
    with closing(Workbook(path)) as book:
        sheets = book.read_workbook()['sheets']
    return [{'name': sheet['name'], 'cells': [{key: cell.get(key) for key in ('row', 'col', 'source', 'kind', 'style', 'value')}
                                              for cell in sheet['cells']]} for sheet in sheets]


def gone(test, call, code='JOB_NOT_FOUND'):
    with test.assertRaises(APIError) as error: call()
    test.assertEqual(error.exception.body['code'], code)


class SdkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); self.temp = Path(temp.name)

    def spy_upload(self, client):
        uploads, original = [], client.upload
        def upload(*args, **kwargs):
            receipt = original(*args, **kwargs); uploads.append(receipt['upload_id']); return receipt
        return uploads, patch.object(client, 'upload', upload)

    def test_round_trip_through_upload_staged_import_export_and_reimport(self):
        for fmt in ('xlsx', 'ods'):
            with self.subTest(fmt), LocalService() as client:
                first, second, exported = self.temp/f'first-{fmt}.dhwb', self.temp/f'second-{fmt}.dhwb', self.temp/f'out.{fmt}'
                progress, (uploads, spy) = [], self.spy_upload(client)
                with client.open(first, create=True) as book:
                    with spy: summary = client.prepare_spreadsheet_file(FIXTURES/f'fidelity.{fmt}', progress=lambda *step: progress.append(step))
                    self.assertEqual((summary['format'], summary['mode'], summary['source']['kind']), (fmt, 'mixed', 'upload'))
                    self.assertTrue(summary['can_import']); self.assertTrue(progress)
                    self.assertEqual([sheet['name'] for sheet in summary['sheets']], FIDELITY_SHEETS)
                    gone(self, lambda: client.release_upload(uploads[0]))  # Released after preparation.
                    receipt = book.import_staged(summary)
                    self.assertEqual(receipt['imported'], {'sheets': 3, 'cells': 49})
                    gone(self, lambda: client.preparation(summary['stage_id']))  # The service consumed the stage.
                    written = book.export_spreadsheet_file(exported)
                    self.assertEqual(set(written), {'path', 'bytes', 'sha256', 'state', 'report', 'scope_lines', 'report_entries', 'totals'})
                    self.assertEqual((written['path'], written['bytes'], written['state']), (str(exported), exported.stat().st_size, book.state))
                with client.open(second, create=True) as book:
                    again = client.prepare_spreadsheet_file(exported)
                    self.assertEqual(book.import_prepared(again)['imported']['cells'], 49)  # A staged summary delegates.
            # The large writers spell a number's source as written (7 becomes 7.0) and the ODS writer canonicalises formula
            # text (=($A1*2.0), quoted sheet names): kinds, styles and calculated values round-trip, and so does other source.
            before, after = contents(first), contents(second)
            lexical = 'formula' if fmt == 'ods' else None
            strip = lambda sheets: [{**sheet, 'cells': [{**cell, 'source': None if cell['kind'] in ('number', lexical) else cell['source']}
                                                        for cell in sheet['cells']]} for sheet in sheets]
            self.assertEqual(strip(after), strip(before))
            self.assertEqual([sheet['name'] for sheet in after], FIDELITY_SHEETS)
            self.assertFalse([name for name in os.listdir(self.temp) if name.startswith('.datahorizon-')])

    def test_local_path_sends_source_path_and_uploads_nothing(self):
        with LocalService() as client, client.open(self.temp/'local.dhwb', create=True) as book:
            with patch.object(client, 'upload', side_effect=AssertionError('uploaded')):
                summary = client.prepare_spreadsheet_file(FIXTURES/'fidelity.ods', local=True, mode='formulas')
            self.assertEqual(summary['source'], {**summary['source'], 'kind': 'path', 'path': str((FIXTURES/'fidelity.ods').resolve())})
            self.assertEqual(summary['mode'], 'formulas')
            self.assertEqual(book.import_staged(summary)['imported'], {'sheets': 3, 'cells': 49})

    def test_format_comes_from_the_extension(self):
        with LocalService() as client:
            with self.assertRaises(ValueError): client.prepare_spreadsheet_file(self.temp/'data.csv')
            data = self.temp/'renamed.bin'; data.write_bytes((FIXTURES/'fidelity.xlsx').read_bytes())
            self.assertEqual(client.prepare_spreadsheet_file(data, format='xlsx', local=True)['format'], 'xlsx')

    def test_upload_detects_a_hash_mismatch_and_releases_the_upload(self):
        with LocalService() as client:
            original = client._open
            def tampered(request, timeout):
                response = original(request, timeout)
                if request.get_method() != 'PUT': return response
                with response: receipt = json.load(response)
                return io.BytesIO(json.dumps({**receipt, 'sha256': '0'*64}).encode())
            with patch.object(client, '_open', tampered), self.assertRaises(IntegrityError) as error:
                client.upload(FIXTURES/'fidelity.xlsx', 'tampered')
            self.assertEqual(error.exception.body['code'], 'INTEGRITY_MISMATCH')
            gone(self, lambda: client.release_upload('tampered'))
            receipt = client.upload(FIXTURES/'fidelity.xlsx', 'tampered')
            self.assertEqual(receipt['bytes'], (FIXTURES/'fidelity.xlsx').stat().st_size)

    def test_oversized_file_fails_before_sending(self):
        with LocalService() as client:
            sent = []
            with patch.object(client, 'capabilities', return_value={'max_upload_bytes': 16}), \
                 patch.object(client, '_open', side_effect=lambda *args: sent.append(args)), self.assertRaises(ValueError):
                client.upload(FIXTURES/'fidelity.xlsx')
            self.assertEqual(sent, [])

    def test_failed_preparation_releases_the_upload_and_the_job(self):
        bad = self.temp/'bad.xlsx'; bad.write_bytes(b'not a zip package')
        encrypted = self.temp/'encrypted.ods'
        encrypted.write_bytes(workbook_bytes({'S': ''}, manifest=manifest_document(
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml">'
            '<manifest:encryption-data manifest:checksum-type="SHA1" manifest:checksum="AAAA"/></manifest:file-entry>')))
        for source in (bad, encrypted):
            with self.subTest(source.name), LocalService() as client:
                uploads, spy = self.spy_upload(client)
                with spy, self.assertRaises(APIError) as error: client.prepare_spreadsheet_file(source, job_id='broken')
                self.assertEqual((error.exception.status, error.exception.body['code'], error.exception.body['job_id']), (None, 'INVALID_SPREADSHEET', 'broken'))
                gone(self, lambda: client.preparation('broken'))
                gone(self, lambda: client.release_upload(uploads[0]))

    def test_import_staged_errors_carry_the_structured_body(self):
        with LocalService() as client, client.open(self.temp/'errors.dhwb', create=True) as book:
            summary = client.prepare_spreadsheet_file(FIXTURES/'fidelity.xlsx', job_id='stale')
            with self.assertRaises(APIError) as error: book.import_staged({**summary, 'stage_digest': '0'*64})
            self.assertEqual((error.exception.status, error.exception.body['code']), (409, 'STALE_PLAN'))
            book.import_staged(summary)
            conflict = client.prepare_spreadsheet_file(FIXTURES/'fidelity.ods', job_id='conflict')
            with self.assertRaises(APIError) as error: book.import_staged(conflict, target='add_sheets')
            self.assertEqual((error.exception.status, error.exception.body['code'], error.exception.body['conflicts']),
                             (409, 'SHEET_NAME_CONFLICT', FIDELITY_SHEETS))
            with self.assertRaises(ValueError): book.import_staged({**conflict, 'can_import': False})
            client.discard_job('conflict'); gone(self, lambda: client.preparation('conflict'))

    def test_export_refuses_an_existing_destination_without_overwrite(self):
        destination = self.temp/'taken.xlsx'; destination.write_bytes(b'keep')
        with LocalService() as client, client.open(self.temp/'export.dhwb', create=True) as book:
            book.set(1, 1, '7')
            with patch.object(client, 'request', wraps=client.request) as requests, self.assertRaises(FileExistsError):
                book.export_spreadsheet_file(destination)
            self.assertFalse([call for call in requests.call_args_list if '/export/' in call.args[0]])  # No job was started.
            self.assertEqual(destination.read_bytes(), b'keep')
            self.assertEqual([name for name in os.listdir(self.temp) if not name.startswith('export.dhwb')], ['taken.xlsx'])
            written = book.export_spreadsheet_file(destination, overwrite=True)
            self.assertEqual(destination.stat().st_size, written['bytes'])
            with self.assertRaises(ValueError): book.export_spreadsheet_file(self.temp/'export.dhwb', format='xlsx', overwrite=True)

    def test_failed_download_leaves_no_temporary_file_and_releases_the_export(self):
        with LocalService() as client, client.open(self.temp/'download.dhwb', create=True) as book:
            book.set(1, 1, '7')
            original_open, original_finish = client._open, client.finish_job
            class Truncated:
                def __init__(self, response): self.response, self.headers, self.reads = response, response.headers, 0
                def read(self, size):
                    self.reads += 1
                    if self.reads > 1: raise ConnectionResetError('dropped')
                    return self.response.read(16)
                def __enter__(self): return self
                def __exit__(self, *exc): self.response.close()
            def truncated(request, timeout):
                response = original_open(request, timeout)
                return Truncated(response) if request.get_method() == 'GET' and '/v1/exports/' in request.full_url else response
            def corrupted(view, progress, label):
                return {**original_finish(view, progress, label), 'sha256': '0'*64}
            for name, replacement, failure in (('_open', truncated, ConnectionResetError), ('finish_job', corrupted, IntegrityError)):
                with self.subTest(name):
                    released = []
                    original_release = client.release_export
                    def release(export_id): released.append(export_id); return original_release(export_id)
                    with patch.object(client, name, replacement), patch.object(client, 'release_export', release), self.assertRaises(failure):
                        book.export_spreadsheet_file(self.temp/'out.xlsx')
                    self.assertEqual([name for name in os.listdir(self.temp) if not name.startswith('download.dhwb')], [])
                    self.assertEqual(len(released), 1); gone(self, lambda: client.release_export(released[0]))

    def test_import_staged_on_a_released_stage_is_rejected_and_the_handle_stays_usable(self):
        with LocalService() as client:
            book = client.open(self.temp/'released.dhwb', create=True)
            summary = client.prepare_spreadsheet_file(FIXTURES/'fidelity.xlsx')
            client.discard_job(summary['stage_id'])
            with self.assertRaises(APIError) as error: book.import_staged(summary, operation_id='released-stage')
            self.assertEqual((error.exception.status, error.exception.body['code'], error.exception.body['outcome'], error.exception.body['operation_id']),
                             (404, 'JOB_NOT_FOUND', 'rejected', 'released-stage'))
            self.assertIsNone(book.pending_operation)
            self.assertEqual(book.set(1, 1, 'after')['outcome'], 'committed')
            self.assertEqual(book.close(), {'closed': True})

    def test_published_files_take_ordinary_or_existing_permissions(self):
        mask = os.umask(0o022); os.umask(mask)
        with LocalService() as client, client.open(self.temp/'modes.dhwb', create=True) as book:
            book.set(1, 1, '7')
            fresh = self.temp/'fresh.xlsx'; book.export_spreadsheet_file(fresh)
            self.assertEqual(fresh.stat().st_mode & 0o777, 0o666 & ~mask)
            kept = self.temp/'kept.ods'; kept.write_bytes(b'old'); os.chmod(kept, 0o640)
            book.export_spreadsheet_file(kept, overwrite=True)
            self.assertEqual(kept.stat().st_mode & 0o777, 0o640); self.assertNotEqual(kept.read_bytes(), b'old')

    def test_a_dangling_symlink_destination_is_refused_before_the_job(self):
        link = self.temp/'dangling.xlsx'; os.symlink(self.temp/'missing-target', link)
        with LocalService() as client, client.open(self.temp/'links.dhwb', create=True) as book:
            with patch.object(client, 'start_job', side_effect=AssertionError('job started')), self.assertRaises(FileExistsError) as error:
                book.export_spreadsheet_file(link)
            self.assertIn(str(link), str(error.exception))
        self.assertTrue(link.is_symlink()); self.assertFalse((self.temp/'missing-target').exists())

    def test_an_unanswered_job_creation_discards_the_job(self):
        with LocalService() as client, client.open(self.temp/'lost.dhwb', create=True) as book:
            original = client.request
            def lost(path, body=None, **options):
                result = original(path, body, **options)
                if path.endswith('/jobs'): raise TimeoutError('lost acknowledgement')
                return result
            for start in (lambda: client.prepare_spreadsheet_file(FIXTURES/'fidelity.xlsx', job_id='lost-import', local=True),
                          lambda: book.export_spreadsheet_file(self.temp/'lost.xlsx')):
                discarded = []
                with patch.object(client, 'request', lost), patch.object(client, 'discard_job', side_effect=discarded.append), self.assertRaises(TimeoutError):
                    start()
                self.assertEqual(len(discarded), 1)
                client.discard_job(discarded[0]); gone(self, lambda: client.preparation(discarded[0]))
            refused = []
            with patch.object(client, 'discard_job', side_effect=refused.append), self.assertRaises(APIError):
                client.prepare_spreadsheet_file(FIXTURES/'fidelity.xlsx', mode='bogus', local=True)
            self.assertEqual(refused, [])  # A structured refusal created no job.

class CliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); self.temp = Path(temp.name)
        self.book = self.temp/'book.dhwb'
        self.run_cli('create')

    def run_cli(self, *args, input='', book=None):
        return subprocess.run([*CLI, str(book or self.book), *args], input=input, capture_output=True, text=True, timeout=120)

    def sheets(self):
        with LocalService() as client, client.open(self.book, readonly=True) as book:
            return [sheet['name'] for sheet in book.info['sheets']]

    def test_import_with_yes_prints_the_grouped_report_and_counts(self):
        run = self.run_cli('import-spreadsheet', str(FIXTURES/'fidelity.xlsx'), '--yes')
        self.assertEqual(run.returncode, 0, run.stderr)
        lines = run.stdout.splitlines()
        self.assertEqual(lines[0], 'Conversion report: unevaluable 0, omitted 33, approximated 16, converted 0')
        omitted = next(index for index, line in enumerate(lines) if line.split()[:1] == ['omitted'])
        approximated = next(index for index, line in enumerate(lines) if line.split()[:1] == ['approximated'])
        self.assertLess(omitted, approximated)
        self.assertIn('33  style.font:', lines[omitted]); self.assertTrue(lines[omitted].endswith('first at Inputs!A1'))
        self.assertIn('  Inputs: 48 cells', lines); self.assertIn('Total: 49 cells in 3 sheets', lines)
        self.assertNotIn('[y/N]', run.stdout + run.stderr)
        self.assertTrue(lines[-1].startswith('Imported 49 cells into 3 sheets'))
        self.assertEqual(self.sheets(), FIDELITY_SHEETS)

    def test_confirmation_on_stdin(self):
        declined = self.run_cli('import-spreadsheet', str(FIXTURES/'fidelity.ods'), input='n\n')
        self.assertEqual(declined.returncode, 1); self.assertEqual(json.loads(declined.stderr.splitlines()[-1])['code'], 'DECLINED')
        self.assertIn('Import 49 cells into 3 sheets? [y/N]', declined.stderr)  # Seen even when stdout is redirected.
        self.assertNotIn('[y/N]', declined.stdout)
        unanswered = self.run_cli('import-spreadsheet', str(FIXTURES/'fidelity.ods'), input='')
        self.assertEqual(unanswered.returncode, 2)
        self.assertEqual(json.loads(unanswered.stderr.splitlines()[-1])['code'], 'CONFIRMATION_REQUIRED')
        self.assertEqual(self.sheets(), ['Sheet1'])
        accepted = self.run_cli('import-spreadsheet', str(FIXTURES/'fidelity.ods'), input='y\n')
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn('Imported 49 cells into 3 sheets', accepted.stdout)
        self.assertEqual(self.sheets(), FIDELITY_SHEETS)

    def test_add_sheets_and_a_name_conflict(self):
        self.assertEqual(self.run_cli('set', 'A1', 'kept').returncode, 0)
        added = self.run_cli('import-spreadsheet', str(FIXTURES/'fidelity.xlsx'), '--add-sheets', '--yes')
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(self.sheets(), ['Sheet1', *FIDELITY_SHEETS])
        conflict = self.run_cli('import-spreadsheet', str(FIXTURES/'fidelity.ods'), '--add-sheets', '--yes')
        self.assertEqual(conflict.returncode, 1)
        error = json.loads(conflict.stderr)
        self.assertEqual((error['code'], error['conflicts']), ('SHEET_NAME_CONFLICT', FIDELITY_SHEETS))
        self.assertIn('already exist', error['message'])
        not_empty = self.run_cli('import-spreadsheet', str(FIXTURES/'fidelity.ods'), '--yes')
        self.assertEqual((not_empty.returncode, json.loads(not_empty.stderr)['code']), (1, 'DESTINATION_NOT_EMPTY'))
        self.assertEqual(self.sheets(), ['Sheet1', *FIDELITY_SHEETS])

    def test_report_json_and_local(self):
        report = self.temp/'report.json'
        run = self.run_cli('import-spreadsheet', str(FIXTURES/'fidelity.xlsx'), '--local', '--mode', 'values', '--report-json', str(report), '--yes')
        self.assertEqual(run.returncode, 0, run.stderr)
        written = json.loads(report.read_text())
        with LocalService() as client:
            summary = client.prepare_spreadsheet_file(FIXTURES/'fidelity.xlsx', mode='values', local=True)
            client.discard_job(summary['stage_id'])
        self.assertEqual(written, {key: summary[key] for key in ('format', 'mode', 'stored_cells', 'sheets', 'totals', 'report', 'scope_lines', 'report_entries')})
        self.assertEqual(written['mode'], 'values')

    def test_errors_are_structured_with_a_nonzero_exit(self):
        bad = self.temp/'bad.xlsx'; bad.write_bytes(b'not a zip package')
        run = self.run_cli('import-spreadsheet', str(bad), '--yes')
        self.assertEqual((run.returncode, json.loads(run.stderr)['code']), (1, 'INVALID_SPREADSHEET'))
        run = self.run_cli('import-spreadsheet', str(self.temp/'data.csv'), '--yes')
        self.assertEqual((run.returncode, json.loads(run.stderr)['code']), (1, 'CLIENT_ERROR'))

    def test_export_with_and_without_overwrite(self):
        self.assertEqual(self.run_cli('import-spreadsheet', str(FIXTURES/'fidelity.xlsx'), '--yes').returncode, 0)
        for fmt in ('xlsx', 'ods'):
            destination, report = self.temp/f'out.{fmt}', self.temp/f'export-{fmt}.json'
            run = self.run_cli('export-spreadsheet', str(destination), '--report-json', str(report))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(run.stdout.startswith('Conversion report: unevaluable 0'))
            self.assertEqual(run.stdout.splitlines()[-1], f'Wrote {destination} ({destination.stat().st_size} bytes)')
            self.assertEqual(json.loads(report.read_text())['bytes'], destination.stat().st_size)
            before = destination.read_bytes()
            refused = self.run_cli('export-spreadsheet', str(destination))
            self.assertEqual(refused.returncode, 1); self.assertIn('overwrite', json.loads(refused.stderr)['message'])
            self.assertEqual(destination.read_bytes(), before)
            replaced = self.run_cli('export-spreadsheet', str(destination), '--overwrite')
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertFalse([name for name in os.listdir(self.temp) if name.startswith('.datahorizon-')])

    def test_untrusted_text_is_escaped_for_the_terminal(self):
        name = 'A\x9bB\u202eC\x7f'
        source = self.temp/'hostile.xlsx'; source.write_bytes(xlsx_bytes({name: '<row r="1"><c r="A1"><v>1</v></c></row>'}))
        run = self.run_cli('import-spreadsheet', str(source), '--add-sheets', '--yes')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('  A\\x9bB\\u202eC\\x7f: 1 cell', run.stdout.splitlines())
        conflict = self.run_cli('import-spreadsheet', str(source), '--add-sheets', '--yes')
        self.assertEqual(conflict.returncode, 1)
        for text in (run.stdout + run.stderr, conflict.stdout + conflict.stderr):
            self.assertFalse({'\x9b', '\u202e', '\x7f'} & set(text))
        self.assertEqual(json.loads(conflict.stderr)['conflicts'], [name])

    def test_report_json_is_published_atomically_and_never_silently_replaced(self):
        report = self.temp/'report.json'; report.write_text('keep')
        refused = self.run_cli('import-spreadsheet', str(FIXTURES/'fidelity.xlsx'), '--report-json', str(report), '--yes')
        self.assertEqual(refused.returncode, 1); self.assertIn('--overwrite', json.loads(refused.stderr)['message'])
        self.assertEqual(report.read_text(), 'keep'); self.assertEqual(self.sheets(), ['Sheet1'])  # Refused before any work.
        replaced = self.run_cli('import-spreadsheet', str(FIXTURES/'fidelity.xlsx'), '--report-json', str(report), '--overwrite', '--yes')
        self.assertEqual(replaced.returncode, 0, replaced.stderr); self.assertEqual(json.loads(report.read_text())['stored_cells'], 49)
        exported = self.run_cli('export-spreadsheet', str(self.temp/'out.xlsx'), '--report-json', str(report))
        self.assertEqual(exported.returncode, 1); self.assertFalse((self.temp/'out.xlsx').exists())
        exported = self.run_cli('export-spreadsheet', str(self.temp/'out.xlsx'), '--report-json', str(report), '--overwrite')
        self.assertEqual(exported.returncode, 0, exported.stderr); self.assertIn('sha256', json.loads(report.read_text()))
        self.assertFalse([name for name in os.listdir(self.temp) if name.startswith('.datahorizon-')])

    def test_a_stage_released_at_the_prompt_is_a_rejected_error(self):
        """Through --url: the stage disappears while the CLI waits for confirmation (as after retention)."""
        with LocalService() as client:
            stages, original = [], Client.prepare_spreadsheet_file
            def prepare(self, *args, **kwargs):
                summary = original(self, *args, **kwargs); stages.append(summary['stage_id']); return summary
            class Answer:
                def readline(self): client.discard_job(stages[0]); return 'y\n'
                def isatty(self): return False
            out, err = io.StringIO(), io.StringIO()
            argv = ['datahorizon', '--url', client.url, str(self.book), 'import-spreadsheet', str(FIXTURES/'fidelity.xlsx')]
            with patch.object(Client, 'prepare_spreadsheet_file', prepare), patch.object(sys, 'argv', argv), \
                 patch.dict(os.environ, {'DATAHORIZON_TOKEN': client.token}), patch.object(sys, 'stdin', Answer()), \
                 redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as exit:
                cli_module.main()
            self.assertEqual(exit.exception.code, 1)
            error = json.loads(err.getvalue().splitlines()[-1])
            self.assertEqual((error['code'], error['outcome']), ('JOB_NOT_FOUND', 'rejected'))
            self.assertNotIn('cleanup_errors', error); self.assertNotIn('Imported', out.getvalue())
            with client.open(self.book, readonly=True) as book:
                self.assertEqual([sheet['name'] for sheet in book.info['sheets']], ['Sheet1'])

if __name__ == '__main__':
    unittest.main()
