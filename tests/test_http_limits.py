# SPDX-License-Identifier: GPL-3.0-only
"""Real-socket admission, slow-input and operation-outcome regression checks."""
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from datahorizon.client import APIError, Client, UnknownOutcome
from datahorizon.service import Service


@contextmanager
def running(**limits):
    with Service(**limits) as server:
        worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        worker.start()
        try:
            yield server,Client(f'http://127.0.0.1:{server.server_port}',server.token)
        finally:
            server.shutdown(); worker.join(timeout=2)


def response(peer):
    peer.settimeout(2)
    chunks=[]
    try:
        while chunk:=peer.recv(8192): chunks.append(chunk)
    except (ConnectionResetError,ConnectionAbortedError):
        pass  # Some platforms reset when closing with unread peer bytes.
    return b''.join(chunks)


def wait_for_capacity(server):
    # Reading a complete HTTP response can precede the handler's bounded socket
    # teardown. Synchronize the fixture before expecting another admission;
    # SERVICE_BUSY remains valid while its single request slot is occupied.
    if not server._request_slots.acquire(timeout=2):
        raise AssertionError('Request capacity was not released after response')
    server._request_slots.release()


class HttpLimitsTests(unittest.TestCase):
    def test_overload_keeps_pending_id_and_preserves_admitted_commit(self):
        with tempfile.TemporaryDirectory() as temp,running(max_requests=1) as (server,client):
            path=Path(temp)/'capacity.dhwb'
            admitted=client.open(path,create=True); excess=client.open(path)
            entered,released=threading.Event(),threading.Event()
            dispatch=server.dispatch; results=[]
            def gated(method,path,body):
                if path.endswith('/operations'):
                    entered.set()
                    if not released.wait(3): raise RuntimeError('Test admission gate timed out')
                return dispatch(method,path,body)
            def write():
                try: results.append(admitted.set(1,1,'7'))
                except Exception as error: results.append(error)
            with patch.object(server,'dispatch',side_effect=gated):
                writer=threading.Thread(target=write,daemon=True); writer.start()
                try:
                    self.assertTrue(entered.wait(2))
                    with self.assertRaises(APIError) as busy: client.request('/v1/capabilities')
                    self.assertEqual(busy.exception.status,503)
                    self.assertEqual(busy.exception.body['code'],'SERVICE_BUSY')
                    self.assertEqual(busy.exception.body['outcome'],'unknown')
                    self.assertNotIn('operation_id',busy.exception.body)
                    with self.assertRaises(UnknownOutcome) as pending: excess.set(2,1,'wrong')
                    self.assertEqual(excess.pending_operation,pending.exception.operation_id)
                finally:
                    released.set(); writer.join(timeout=3)
            self.assertEqual(len(results),1)
            self.assertIsInstance(results[0],dict,results)
            self.assertEqual(results[0]['outcome'],'committed')
            wait_for_capacity(server)
            self.assertEqual(admitted.outcome(results[0]['operation_id']),results[0])
            wait_for_capacity(server)
            self.assertEqual(admitted.read(r2=2,c2=1)['cells'][0]['value'],7)
            wait_for_capacity(server)
            self.assertEqual(len(admitted.read(r2=2,c2=1)['cells']),1)
            wait_for_capacity(server)
            with self.assertRaises(UnknownOutcome): excess.outcome(pending.exception.operation_id)
            with self.assertRaises(UnknownOutcome): excess.set(2,1,'no automatic retry')

    def test_header_trickle_has_total_deadline_and_releases_capacity(self):
        with running(max_requests=1,read_seconds=.25) as (server,client):
            with socket.create_connection(server.server_address) as peer:
                started=time.monotonic()
                peer.sendall(b'GET /v1/capabilities HTTP/1.0\r\nHost: localhost\r\nX-Trickle: ')
                # More frequent than the timeout: an inactivity-only timer fails.
                for _ in range(20):
                    time.sleep(.04)
                    try: peer.sendall(b'x')
                    except OSError: break
                self.assertEqual(response(peer),b'')
                self.assertLess(time.monotonic()-started,1.5)
            self.assertEqual(client.request('/v1/capabilities')['api_version'],1)

    def test_body_trickle_never_dispatches_and_can_recover(self):
        with tempfile.TemporaryDirectory() as temp,running(max_requests=1,read_seconds=.3) as (server,client):
            book=client.open(Path(temp)/'body.dhwb',create=True)
            body=json.dumps({'operation_id':'incomplete','expected_state':book.state,'action':'edit',
                             'cells':[{'row':1,'col':1,'source':'wrong'}]}).encode()
            with socket.create_connection(server.server_address) as peer:
                started=time.monotonic()
                peer.sendall((f'POST {book.base}/operations HTTP/1.0\r\nHost: localhost\r\nAuthorization: Bearer {server.token}\r\nContent-Length: {len(body)}\r\n\r\n').encode())
                stopped=threading.Event()
                def trickle():
                    for byte in body[:20]:
                        if stopped.wait(.04): return
                        try: peer.sendall(bytes([byte]))
                        except OSError: return
                sender=threading.Thread(target=trickle,daemon=True);sender.start()
                try: raw=response(peer)
                finally: stopped.set();sender.join(timeout=2)
                self.assertFalse(sender.is_alive())
                self.assertLess(time.monotonic()-started,1.5)
                self.assertIn(b'408',raw.split(b'\r\n')[0])
                self.assertEqual(json.loads(raw.split(b'\r\n\r\n')[1])['code'],'REQUEST_TIMEOUT')
            self.assertEqual(book.read(r2=1,c2=1)['cells'],[])
            self.assertEqual(book.set(1,1,'8')['outcome'],'committed')

    def test_aggregate_headers_are_bounded_and_prefetched_body_is_preserved(self):
        with running(max_header_bytes=256) as (server,client):
            with socket.create_connection(server.server_address) as peer:
                peer.sendall(b'GET /docs HTTP/1.0\r\nHost: localhost\r\n'+b''.join(b'X-Test: '+b'x'*60+b'\r\n' for _ in range(5))+b'\r\n')
                self.assertIn(b'431',response(peer).split(b'\r\n')[0])
            # Headers and JSON fit together in the input buffer. Normal parsing
            # must not discard those prefetched body bytes.
            self.assertEqual(client.request('/v1/import/csv/preview',{'csv':'A,B\n1,2'})['rows'],[['A','B'],['1','2']])

    def test_thread_start_failure_returns_slot(self):
        with Service(max_requests=1) as server:
            with patch('threading.Thread.start',side_effect=RuntimeError('Cannot start handler')):
                with self.assertRaises(RuntimeError): server.process_request(None,None)
            self.assertTrue(server._request_slots.acquire(blocking=False))
            server._request_slots.release()

    def test_invalid_limits_fail_before_binding(self):
        for kwargs in ({'max_requests':0},{'max_requests':True},{'read_seconds':float('nan')},
                       {'read_seconds':float('inf')},{'read_seconds':0},{'max_header_bytes':0}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError): Service(**kwargs)
