"""Bounded M1 service/Python comparison, isolated from the installed API."""
import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import platform
import runpy
import secrets
from socketserver import TCPServer
import statistics
import tempfile
import threading
import time
import uuid
from unittest.mock import patch
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

from datahorizon.client import APIError, Client
from datahorizon.engine import EngineError, Workbook
from datahorizon.hooks import HookFailure

HOOK = Path(__file__).resolve().parent/'fixtures'/'execution_hook.py'


def inline_invoke(path, changes, operation, dispatch, deadline):
    """Fast trusted baseline only; deliberately cannot contain a crash or loop."""
    class Context:
        operation_id = operation

        def get(self, sheet, row, col):
            return dispatch('get', dict(sheet=sheet, row=row, col=col))

        def value(self, sheet, row, col):
            return dispatch('value', dict(sheet=sheet, row=row, col=col))

        def set(self, sheet, row, col, source, kind='auto'):
            return dispatch('set', dict(sheet=sheet, row=row, col=col, source=source, kind=kind))
    try:
        runpy.run_path(str(path))['on_change'](Context(), changes)
    except Exception as error:
        raise HookFailure(str(error)) from error


class Application:
    """Both adapters dispatch the same tiny engine surface and JSON responses."""
    def __init__(self, path):
        self.book = Workbook(path, create=not path.exists(), handler=HOOK)
        self.token = secrets.token_hex(24)

    def dispatch(self, method, path, token, payload):
        if token != 'Bearer '+self.token:
            return 401, {'code': 'AUTH', 'message': 'Authentication required', 'outcome': 'rejected'}
        try:
            if method == 'GET' and path == '/info':
                return 200, self.book.info()
            if method == 'GET' and path.startswith('/outcome/'):
                return 200, self.book.operation(path.removeprefix('/outcome/'))
            if method == 'POST' and path == '/edit':
                return 200, self.book.mutate(json.loads(payload))
            return 404, {'code': 'NOT_FOUND', 'message': 'Unknown experiment route', 'outcome': 'rejected'}
        except EngineError as error:
            return error.status, error.body


@contextmanager
def serve(path, adapter):
    app = Application(path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond(b'')

        def do_POST(self):
            length = int(self.headers['Content-Length'])
            if not 0 <= length <= 4*1024*1024:
                self.send_error(413)
                return
            self.respond(self.rfile.read(length))

        def respond(self, payload):
            status, result = app.dispatch(self.command, self.path, self.headers.get('Authorization'), payload)
            data = json.dumps(result, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # A disconnected caller does not undo the transaction.

    class NumericHTTP(ThreadingHTTPServer):
        def server_bind(self):
            TCPServer.server_bind(self)

    class NumericWSGI(WSGIServer):
        def server_bind(self):
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address
            self.setup_environ()

    class QuietWSGI(WSGIRequestHandler):
        def log_message(self, *_args):
            pass

    def wsgi(environ, start_response):
        length = int(environ.get('CONTENT_LENGTH') or 0)
        if not 0 <= length <= 4*1024*1024:
            status, result = 413, {'code': 'LIMIT'}
        else:
            status, result = app.dispatch(environ['REQUEST_METHOD'], environ['PATH_INFO'],
                                          environ.get('HTTP_AUTHORIZATION'), environ['wsgi.input'].read(length))
        data = json.dumps(result, allow_nan=False).encode()
        start_response(f'{status} result', [('Content-Type', 'application/json'), ('Content-Length', str(len(data)))])
        return [data]

    try:
        if adapter == 'threaded_http':
            server = NumericHTTP(('127.0.0.1', 0), Handler)
        elif adapter == 'serial_wsgi':
            server = make_server('127.0.0.1', 0, wsgi, server_class=NumericWSGI, handler_class=QuietWSGI)
        else:
            raise ValueError('Unknown experiment adapter')
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
        thread.start()
        try:
            yield app, Client(f'http://127.0.0.1:{server.server_port if adapter == "serial_wsgi" else server.server_address[1]}', app.token)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
    finally:
        app.book.close()


def edit(client, value, operation_id=None):
    info = client.request('/info')
    request = {'action': 'edit', 'sheet': 'Sheet1', 'operation_id': operation_id or str(uuid.uuid4()),
               'expected_state': info['state'], 'cells': [{'row': 1, 'col': 1, 'source': str(value)}]}
    return request


def stored(app):
    with app.book.lock:
        return app.book.db.execute('SELECT row,col,source,kind,style FROM cells ORDER BY row,col').fetchall()


def values(app):
    with app.book.lock:
        calc = app.book.calculator()
        sheet = app.book.sheet('Sheet1')
        return [calc.cell(sheet, 1, col) for col in (1, 2)]


def contracts(adapter):
    result = {}
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp)/'experiment.dhwb'
        with serve(path, adapter) as (app, client):
            request = edit(client, 7)
            receipt = client.request('/edit', request)
            if values(app) != [7, 14] or receipt['changed_cells'] != 2:
                raise AssertionError('Generated edit did not share the parent transaction')
            if client.request('/edit', request) != receipt:
                raise AssertionError('Receipt replay changed the result')
            result['generated_edit'] = {'outcome': receipt['outcome'], 'changed_cells': receipt['changed_cells']}
            before = stored(app)
            for name, value in (('exception', -1), ('worker_crash', -3), ('worker_timeout', -2)):
                request = edit(client, value)
                started = time.perf_counter()
                try:
                    client.request('/edit', request)
                except APIError as error:
                    if error.body.get('outcome') != 'rolled_back' or stored(app) != before:
                        raise AssertionError(f'{name}: data did not roll back') from error
                    outcome = client.request('/outcome/'+request['operation_id'])
                    if outcome != error.body:
                        raise AssertionError('Failure receipt mismatch')
                    result[name] = {'outcome': outcome['outcome'], 'code': outcome['code'],
                                    'elapsed_ms': round((time.perf_counter()-started)*1000, 3)}
                else:
                    raise AssertionError(f'{name} was accepted')
            # Client closes after sending a complete request, before reading ACK.
            lost = edit(client, 8)
            port = int(client.url.rsplit(':', 1)[1])
            connection = HTTPConnection('127.0.0.1', port, timeout=10)
            connection.request('POST', '/edit', json.dumps(lost), {'Authorization': 'Bearer '+app.token})
            connection.close()
            deadline = time.monotonic()+5
            while True:
                try:
                    recovered = client.request('/outcome/'+lost['operation_id'])
                    break
                except APIError as error:
                    if error.status != 404 or time.monotonic() > deadline:
                        raise
                    time.sleep(.01)
            if recovered['outcome'] != 'committed' or values(app) != [8, 16]:
                raise AssertionError('Lost acknowledgement did not reconcile')
            result['lost_acknowledgement'] = {'outcome': recovered['outcome'], 'changed_cells': recovered['changed_cells']}
        with serve(path, adapter) as (_app, client):
            if client.request('/outcome/'+lost['operation_id']) != recovered:
                raise AssertionError('Receipt did not survive restart')
            result['receipt_survives_restart'] = True
    return result


def measurements(adapter, samples, inline=False):
    with tempfile.TemporaryDirectory() as temp:
        with serve(Path(temp)/'timing.dhwb', adapter) as (_app, client):
            durations = []
            manager = patch('datahorizon.engine.invoke', inline_invoke) if inline else nullcontext()
            with manager:
                for value in range(1, samples+1):
                    request = edit(client, value)
                    started = time.perf_counter()
                    receipt = client.request('/edit', request)
                    if receipt['changed_cells'] != 2 or values(_app) != [value, value*2]:
                        raise AssertionError('Timing workload lost generated edits')
                    durations.append((time.perf_counter()-started)*1000)
            return {'samples': samples, 'median_ms': round(statistics.median(durations), 3),
                    'p95_ms': round(sorted(durations)[math.ceil(samples*.95)-1], 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=20)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    report = {'recorded_at_utc': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
              'python': platform.python_version(), 'contracts': {}, 'measurements': {},
              'limitations': ['Identical minimal experiment routes, not complete alternative production APIs.',
                              'Inline runs only fast trusted code; cannot contain a loop/crash and is not a service option.',
                              'Cancellation evidence is required-hook timeout rollback; public job cancellation remains unavailable.',
                              'Warm local durable transactions; no network load, cold-cache or production hardening qualification.']}
    for adapter in ('threaded_http', 'serial_wsgi'):
        report['contracts'][adapter] = contracts(adapter)
        report['measurements'][adapter] = {name: measurements(adapter, args.samples, inline)
                                           for name, inline in (('spawned_broker', False), ('inline_baseline', True))}
    output = json.dumps(report, indent=2)+'\n'
    if args.output:
        args.output.write_text(output, encoding='utf-8')
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
