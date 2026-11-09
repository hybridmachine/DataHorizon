# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from datahorizon.engine import Workbook,EngineError,sql_deadline
from datahorizon.records import digest


class ScanCancellationTests(unittest.TestCase):
    def test_no_output_sql_checks_cancellation_and_restores_outer_guard(self):
        with closing(sqlite3.connect(':memory:')) as db:
            cancel=threading.Event();calls=[]
            def scan(value):
                calls.append(value)
                if value==200:cancel.set()
                return value
            db.create_function('scan',1,scan)
            sql='WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT x FROM n WHERE scan(x)<0'
            with sql_deadline(db,time.monotonic()+5,cancel_event=cancel):
                with self.assertRaises(EngineError) as error:
                    with sql_deadline(db,time.monotonic()+10):db.execute(sql).fetchall()
                self.assertEqual(error.exception.body['code'],'CANCELLED')
                # The inner finally must restore, not remove, the outer guard.
                with self.assertRaises(sqlite3.OperationalError):db.execute(sql).fetchall()
                cancel.clear()
            self.assertLess(len(calls),10000)
            self.assertEqual(db.execute('SELECT 7').fetchone()[0],7)

    def test_no_output_sql_deadline_interrupts_between_instructions(self):
        with closing(sqlite3.connect(':memory:')) as db:
            now=[0.0]
            def scan(value):now[0]=2.0;return value
            db.create_function('scan',1,scan)
            with patch('datahorizon.engine.time.monotonic',side_effect=lambda:now[0]):
                with self.assertRaises(EngineError) as error:
                    with sql_deadline(db,1):
                        db.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT x FROM n WHERE scan(x)<0').fetchall()
            self.assertEqual(error.exception.body['code'],'TIMEOUT')
            self.assertEqual(db.execute('SELECT 8').fetchone()[0],8)

    def test_record_digest_uses_indexed_row_probes_and_parent_cancellation(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'scan.dhwb',create=True)) as book:
            sheet=book.sheet('Sheet1')
            book.db.executemany("INSERT INTO cells(sheet_id,row,col,source) VALUES(?,?,?,'unrelated')",[(sheet,r,c) for r in range(1,21) for c in range(2,102)])
            queries=[];book.db.set_trace_callback(queries.append)
            spec=dict(r1=1,r2=20,c1=1,c2=1)
            self.assertEqual(digest(book,sheet,spec),__import__('hashlib').sha256().hexdigest())
            probes=[q for q in queries if q.startswith('SELECT row,col,source')]
            self.assertEqual(len(probes),20);self.assertTrue(all(' AND row=' in q for q in probes))
            cancel=threading.Event();cancel.set()
            with self.assertRaises(EngineError) as error:digest(book,sheet,spec,cancel_event=cancel)
            self.assertEqual(error.exception.body['code'],'CANCELLED')
            # No stale cancellation remains on the connection or a standalone read.
            self.assertEqual(digest(book,sheet,spec),__import__('hashlib').sha256().hexdigest())

    def test_cancel_during_record_capture_has_durable_rollback_and_reusable_connection(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'capture.dhwb',create=True)) as book:
            book.mutate(dict(action='list_declare',id='Records',spec=dict(r2=2),operation_id='declare',expected_state=book.state()))
            cancel=threading.Event();original=book.range_cells
            def cancelled(*args):
                cancel.set()
                yield from original(*args)
            with patch.object(book,'range_cells',cancelled):
                with self.assertRaises(EngineError) as error:
                    book.mutate(dict(action='edit',operation_id='cancelled',expected_state=book.state(),cells=[dict(row=1,col=1,source='new')]),cancel_event=cancel)
            self.assertEqual(error.exception.body['code'],'CANCELLED');self.assertEqual(book.operation('cancelled')['outcome'],'rolled_back')
            self.assertEqual(book.read('Sheet1',dict(r2=2))['cells'],[])
            result=book.mutate(dict(action='edit',operation_id='next',expected_state=book.state(),cells=[dict(row=1,col=1,source='valid')]))
            self.assertEqual(result['outcome'],'committed')
