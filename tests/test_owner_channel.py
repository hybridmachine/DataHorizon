# SPDX-License-Identifier: GPL-3.0-only
import contextlib
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
from datahorizon.client import APIError,Client,LocalService
from datahorizon.code_approval import ApprovalStore
from datahorizon.service import read_owner_channel

SOURCE="def f(ctx):\n    return 1\n"
MACROS=[dict(name='f',result=dict(type='integer'))]


def wait_until(check,seconds=10):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if check():return True
        time.sleep(0.05)
    return False


def key_for(book,digit='a'):return book+':'+digit*64


class ApprovalStoreTests(unittest.TestCase):
    def test_grant_replaces_same_workbook_and_keeps_startup_keys(self):
        book=str(uuid.uuid4());fixed=key_for(book,'1');store=ApprovalStore([fixed])
        for index in range(300):store.grant(key_for(book,'%x'%(index%10+2)))
        self.assertIn(fixed,store);self.assertEqual(len(store.snapshot()),2)
        store.grant(key_for(book,'f'));self.assertNotIn(key_for(book,'3'),store);self.assertIn(fixed,store)

    def test_cap_counts_startup_plus_granted_and_rejection_changes_nothing(self):
        store=ApprovalStore([key_for(str(uuid.uuid4())) for _ in range(255)])
        first=key_for(str(uuid.uuid4()));store.grant(first)
        with self.assertRaises(ValueError):store.grant(key_for(str(uuid.uuid4())))
        self.assertEqual(len(store.snapshot()),256);self.assertIn(first,store)
        with self.assertRaises(ValueError):store.grant('not a key')

    def test_reader_applies_valid_lines_and_survives_garbage(self):
        store=ApprovalStore();good=key_for(str(uuid.uuid4()));other=key_for(str(uuid.uuid4()),'b')
        lines=[b'not json\n',b'[1]\n',json.dumps(dict(approve=good,extra=1)).encode()+b'\n',b'{"approve":"short"}\n',b'x'*5000+b'\n',b'\xff\xfe\n',json.dumps(dict(approve=good)).encode()+b'\n',json.dumps(dict(approve=other)).encode()]
        with contextlib.redirect_stderr(io.StringIO()) as captured:read_owner_channel(io.BytesIO(b''.join(lines)),store)
        self.assertEqual(len(captured.getvalue().splitlines()),6)
        self.assertEqual(store.snapshot(),frozenset([good,other]))

    def test_reader_joins_partial_writes_and_stops_at_end_of_input(self):
        store=ApprovalStore();key=key_for(str(uuid.uuid4()));data=json.dumps(dict(approve=key)).encode()+b'\n'
        read,write=os.pipe()
        with os.fdopen(read,'rb',buffering=0) as stream:
            thread=threading.Thread(target=read_owner_channel,args=(stream,store));thread.start()
            for index in range(0,len(data),7):os.write(write,data[index:index+7]);time.sleep(0.01)
            self.assertTrue(wait_until(lambda:key in store));os.close(write);thread.join(5)
            self.assertFalse(thread.is_alive())


class OwnerChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'owner.dhwb'
        with LocalService() as client,client.open(self.path,create=True) as book:
            book.save_module('Example',SOURCE,MACROS,fingerprint=book.code()['fingerprint'])

    def test_grant_reaches_an_open_workbook(self):
        service=LocalService(owner_channel=True)
        with service as client,client.open(self.path) as book:
            code=book.code();self.assertFalse(code['approved'])
            service.approve(code['activation_key'])
            self.assertTrue(wait_until(lambda:book.code()['approved']))
            self.assertEqual(book.run_macro('Example','f',fingerprint=code['fingerprint'])['macro_result'],1)
            self.assertIn('owner channel',client.request('/v1/capabilities')['macro_activation'])

    def test_startup_keys_survive_grants(self):
        with LocalService() as client,client.open(self.path) as book:key=book.code()['activation_key']
        service=LocalService(approved_code=[key],owner_channel=True)
        with service as client,client.open(self.path) as book:
            service.approve(key_for(str(uuid.uuid4())));time.sleep(0.3)
            self.assertTrue(book.code()['approved'])

    def test_key_for_changed_code_does_not_approve(self):
        service=LocalService(owner_channel=True)
        with service as client,client.open(self.path) as book:
            key=book.code()['activation_key']
            with closing(sqlite3.connect(self.path,isolation_level=None)) as db:
                value=json.loads(db.execute("SELECT payload FROM dh_relational WHERE kind='module'").fetchone()[0]);value['source']='def f(ctx):\n    return 2\n'
                db.execute("UPDATE dh_relational SET payload=? WHERE kind='module'",(json.dumps(value),))
            book.refresh();service.approve(key);time.sleep(0.5)
            self.assertFalse(book.code()['approved'])

    def test_http_cannot_grant(self):
        with LocalService() as client,client.open(self.path) as book:
            key=book.code()['activation_key']
            for field in (dict(approve=key),dict(approved=True),dict(approved_code=[key])):
                for route in ('/code','/operations'):
                    try:client.request(book.base+route,dict(expected_state=book.state,**field))
                    except APIError:pass
                self.assertFalse(book.code()['approved'])
            for route in ('/v1/approve','/v1/approvals'):
                with self.assertRaises(APIError):client.request(route,dict(approve=key))
            self.assertFalse(book.code()['approved'])

    def test_service_without_flag_ignores_stdin(self):
        service=LocalService()
        with service as client,client.open(self.path) as book:
            with self.assertRaises(RuntimeError):service.approve(book.code()['activation_key'])
            self.assertFalse(book.code()['approved'])
        command=[sys.executable,'-I','-B',str(Path(sys.modules['datahorizon.client'].__file__).with_name('launch.py'))]
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def stop():
            process.terminate();process.wait()
            for stream in (process.stdin,process.stdout,process.stderr):
                stream.close()
        self.addCleanup(stop)
        info=json.loads(process.stdout.readline())
        attached=Client(info['url'],info['token'])
        if True:
            with attached.open(self.path) as book:
                process.stdin.write(json.dumps(dict(approve=book.code()['activation_key']))+'\n');process.stdin.flush();time.sleep(0.5)
                self.assertFalse(book.code()['approved'])

    def test_malformed_lines_do_not_stop_the_service(self):
        service=LocalService(owner_channel=True)
        with service as client,client.open(self.path) as book:
            code=book.code()
            for line in ('garbage\n','{"approve": 5}\n','{"approve":"x","other":1}\n','z'*6000+'\n'):
                service.process.stdin.write(line);service.process.stdin.flush()
            service.approve(code['activation_key'])
            self.assertTrue(wait_until(lambda:book.code()['approved']))
            self.assertTrue(wait_until(lambda:len(service.diagnostics)>=4))
            self.assertNotIn(code['activation_key'],'\n'.join(service.diagnostics))

    def test_end_of_input_leaves_service_running_and_closed_service_refuses(self):
        service=LocalService(owner_channel=True)
        with service as client,client.open(self.path) as book:
            service.process.stdin.close();time.sleep(0.3)
            self.assertIsNone(service.process.poll());book.code()
        with self.assertRaises(RuntimeError):service.approve(key_for(str(uuid.uuid4())))

    def test_interrupt_exits_cleanly_while_stdin_is_open_and_idle(self):
        command=[sys.executable,'-I','-B',str(Path(sys.modules['datahorizon.client'].__file__).with_name('launch.py')),'--owner-channel']
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            json.loads(process.stdout.readline());time.sleep(0.3);process.send_signal(signal.SIGINT)
            code=process.wait(timeout=10);errors=process.stderr.read()
        finally:
            if process.poll() is None:process.kill();process.wait()
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()
        self.assertEqual(code,0);self.assertNotIn('Fatal Python error',errors)

    def test_attached_client_has_no_approve(self):
        self.assertFalse(hasattr(Client,'approve'));self.assertFalse(hasattr(Client,'grant'))


if __name__=='__main__':unittest.main()
