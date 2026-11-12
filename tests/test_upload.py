# SPDX-License-Identifier: GPL-3.0-only
"""Streamed raw-body uploads to the private staging directory over real sockets."""
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from datahorizon.client import Client
from datahorizon.service import Service, openapi
from datahorizon.xlsx_stream import LARGE_CELLS, LARGE_PACKAGE

try:
    from openapi_schema_validator import OAS30Validator
except ImportError:
    OAS30Validator = None


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


def staged(server):
    return sorted(p.name for p in server.staging.root.iterdir())


def wait_idle(server):
    # A response can be read before the handler thread returns its slot.
    for _ in range(200):
        if server._request_slots._value == server._request_slots._initial_value: return
        time.sleep(.01)
    raise AssertionError('Request slots were not released')


def request(server, method, path, body=b'', headers=None, *, token=True, content_type='application/octet-stream'):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
    try:
        sent = {}
        if content_type is not None: sent['Content-Type'] = content_type
        if token: sent['Authorization'] = 'Bearer '+server.token
        sent.update(headers or {})
        connection.request(method, path, body=body, headers=sent)
        reply = connection.getresponse()
        return reply.status, json.loads(reply.read())
    finally:
        connection.close()


def upload(server, identifier, body, **kwargs):
    return request(server, 'PUT', '/v1/uploads/'+identifier, body, **kwargs)


def release(server, identifier):
    return request(server, 'POST', f'/v1/uploads/{identifier}/release', b'{}', content_type='application/json')


def raw_response(peer, timeout=5):
    peer.settimeout(timeout)
    chunks = []
    try:
        while chunk := peer.recv(8192): chunks.append(chunk)
    except (ConnectionResetError, ConnectionAbortedError):
        pass
    data = b''.join(chunks)
    head, _, body = data.partition(b'\r\n\r\n')
    return int(head.split(b' ')[1]), json.loads(body)


def headers_only(server, identifier, length, extra=b''):
    return (f'PUT /v1/uploads/{identifier} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {server.token}\r\n'
            f'Content-Type: application/octet-stream\r\nContent-Length: {length}\r\n').encode()+extra+b'\r\n'


class UploadTests(unittest.TestCase):
    def test_five_mib_upload_round_trips_with_sha256_into_private_staging_file(self):
        data = os.urandom(5*1024*1024)
        with running() as (server, _):
            status, result = upload(server, 'book-1', data)
            self.assertEqual(status, 200, result)
            self.assertEqual(result, {'upload_id': 'book-1', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            path = server.staging.path('upload', 'book-1')
            self.assertEqual(path.parent, server.staging.root)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), data)

    def test_oversized_and_empty_lengths_are_rejected_without_reading_or_creating(self):
        with running() as (server, _):
            with socket.create_connection(server.server_address) as peer:
                peer.sendall(headers_only(server, 'big', LARGE_PACKAGE+1))
                status, error = raw_response(peer)
            self.assertEqual((status, error['code']), (413, 'REQUEST_LIMIT'))
            wait_idle(server)
            self.assertEqual(upload(server, 'empty', b'')[1]['code'], 'REQUEST_LIMIT')
            self.assertEqual(staged(server), [])

    def test_short_body_and_disconnect_leave_no_file(self):
        with running() as (server, _):
            with socket.create_connection(server.server_address) as peer:
                peer.sendall(headers_only(server, 'short', 1000)+b'x'*10)
                peer.shutdown(socket.SHUT_WR)
                status, error = raw_response(peer)
            self.assertEqual((status, error['code']), (400, 'INVALID_UPLOAD'))
            self.assertEqual(staged(server), [])
            with socket.create_connection(server.server_address) as peer:
                peer.sendall(headers_only(server, 'gone', 100000)+b'x'*70000)
                for _ in range(200):
                    if staged(server): break
                    time.sleep(.01)
            wait_idle(server)
            self.assertEqual(staged(server), [])

    def test_stalled_body_is_cut_off_by_the_body_deadline(self):
        with running(read_seconds=5) as (server, _), patch('datahorizon.service.UPLOAD_SECONDS', .3):
            with socket.create_connection(server.server_address) as peer:
                started = time.monotonic()
                peer.sendall(headers_only(server, 'slow', 1000)+b'x'*10)
                status, error = raw_response(peer)
                self.assertLess(time.monotonic()-started, 2)
            self.assertEqual((status, error['code']), (408, 'REQUEST_TIMEOUT'))
            wait_idle(server)
            self.assertEqual(staged(server), [])

    def test_admission_rejections_create_nothing(self):
        with running() as (server, temp):
            cases = [
                (dict(token=False), 401, 'UNAUTHORIZED'),
                (dict(headers={'Origin': 'http://evil.example'}), 403, 'INVALID_ORIGIN'),
                (dict(content_type='application/json'), 415, 'UNSUPPORTED_MEDIA_TYPE'),
                (dict(content_type=None), 415, 'UNSUPPORTED_MEDIA_TYPE'),
            ]
            for kwargs, status, code in cases:
                with self.subTest(code=code, kwargs=kwargs):
                    got, error = upload(server, 'ok', b'data', **kwargs)
                    self.assertEqual((got, error['code'], error['outcome']), (status, code, 'rejected'))
            for identifier in ('../x', '%2E%2E%2Fx', 'a'*65, ''):
                with self.subTest(identifier=identifier):
                    got, error = upload(server, identifier, b'data')
                    self.assertEqual((got, error['code']), (400, 'INVALID_JOB'))
            self.assertEqual(staged(server), [])
            self.assertEqual(sorted(p.name for p in temp.iterdir()), ['staging'])

    def test_missing_length_and_chunked_encoding_are_length_required(self):
        with running() as (server, _):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.putrequest('PUT', '/v1/uploads/nolength')
            connection.putheader('Authorization', 'Bearer '+server.token)
            connection.putheader('Content-Type', 'application/octet-stream')
            connection.endheaders()
            reply = connection.getresponse()
            self.assertEqual((reply.status, json.loads(reply.read())['code']), (411, 'LENGTH_REQUIRED'))
            connection.close()
            wait_idle(server)
            status, error = upload(server, 'chunked', iter([b'abc']), headers={'Transfer-Encoding': 'chunked'})
            self.assertEqual((status, error['code']), (411, 'LENGTH_REQUIRED'))
            wait_idle(server)
            self.assertEqual(upload(server, 'bad', b'x', headers={'Content-Length': '1x'})[1]['code'], 'LENGTH_REQUIRED')
            self.assertEqual(staged(server), [])

    def test_capacity_conflict_release_and_reuse(self):
        with running() as (server, _):
            for index in range(4):
                self.assertEqual(upload(server, f'u{index}', b'data')[0], 200)
            status, error = upload(server, 'u4', b'data')
            self.assertEqual((status, error['code']), (409, 'JOB_LIMIT'))
            status, error = upload(server, 'u0', b'again')
            self.assertEqual((status, error['code']), (409, 'JOB_CONFLICT'))
            self.assertEqual(release(server, 'u0'), (200, {'released': True, 'upload_id': 'u0'}))
            status, error = release(server, 'u0')
            self.assertEqual((status, error['code']), (404, 'JOB_NOT_FOUND'))
            self.assertEqual(release(server, '..')[1]['code'], 'INVALID_JOB')
            status, result = upload(server, 'u0', b'again')
            self.assertEqual((status, result['bytes']), (200, 5))
            self.assertEqual(server.staging.path('upload', 'u0').read_bytes(), b'again')

    def test_third_concurrent_upload_is_busy(self):
        with running() as (server, _):
            peers = [socket.create_connection(server.server_address) for _ in range(2)]
            try:
                for index, peer in enumerate(peers):
                    peer.sendall(headers_only(server, f'hold{index}', 8)+b'1234')
                for _ in range(200):
                    if len(staged(server)) == 2: break
                    time.sleep(.01)
                self.assertEqual(len(staged(server)), 2)
                status, error = upload(server, 'third', b'data')
                self.assertEqual((status, error['code']), (409, 'UPLOAD_BUSY'))
                status, error = release(server, 'hold0')
                self.assertEqual((status, error['code']), (409, 'JOB_CONFLICT'))
                for peer in peers:
                    peer.sendall(b'5678')
                    self.assertEqual(raw_response(peer)[0], 200)
            finally:
                for peer in peers: peer.close()
            wait_idle(server)
            self.assertEqual(upload(server, 'third', b'data')[0], 200)

    def test_put_elsewhere_is_a_json_not_found(self):
        with running() as (server, _):
            for path in ('/v1/capabilities', '/v1/uploads', '/v1/uploads/a/release', '/docs'):
                with self.subTest(path=path):
                    status, error = request(server, 'PUT', path, b'data')
                    self.assertIn(status, (404, 405))
                    self.assertEqual(error['outcome'], 'rejected')
            self.assertEqual(staged(server), [])

    def test_capabilities_and_openapi_example_execute(self):
        with running() as (server, _):
            client = Client(f'http://127.0.0.1:{server.server_port}', server.token)
            capabilities = client.request('/v1/capabilities')
            self.assertTrue(capabilities['uploads'])
            self.assertEqual((capabilities['max_upload_bytes'], capabilities['max_import_cells'], capabilities['max_concurrent_uploads']),
                             (LARGE_PACKAGE, LARGE_CELLS, 2))
            spec = openapi(); schemas = spec['components']['schemas']
            put = spec['paths']['/v1/uploads/{upload}']['put']
            media = put['requestBody']['content']['application/octet-stream']
            self.assertEqual(media['schema'], {'type': 'string', 'format': 'binary', 'minLength': 1, 'maxLength': LARGE_PACKAGE})
            self.assertEqual(put['responses']['200']['content']['application/json']['schema']['$ref'], '#/components/schemas/UploadReceipt')
            for status in ('400', '401', '403', '408', '409', '411', '413', '415'):
                self.assertIn(status, put['responses'])
            self.assertEqual(put['parameters'][0]['schema']['pattern'], '^[A-Za-z0-9_-]{1,64}$')
            receipt = schemas['UploadReceipt']['example']
            got = upload(server, receipt['upload_id'], media['example'].encode())
            self.assertEqual(got, (200, receipt))
            post = spec['paths']['/v1/uploads/{upload}/release']['post']
            self.assertEqual(post['requestBody']['content']['application/json']['schema']['$ref'], '#/components/schemas/JobAction')
            self.assertEqual(post['responses']['200']['content']['application/json']['schema']['$ref'], '#/components/schemas/UploadReleased')
            self.assertEqual(release(server, receipt['upload_id']), (200, schemas['UploadReleased']['example']))
            if OAS30Validator is not None:
                for name, value in (('UploadReceipt', got[1]), ('UploadReleased', schemas['UploadReleased']['example'])):
                    OAS30Validator({**schemas[name], 'components': spec['components']}).validate(value)


if __name__ == '__main__':
    unittest.main()
