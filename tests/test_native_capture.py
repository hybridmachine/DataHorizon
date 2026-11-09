# SPDX-License-Identifier: GPL-3.0-only
"""Optional native binding contracts; base runtime needs no pip dependencies."""
from contextlib import closing
import time
import unittest
from unittest.mock import patch

try:
    import apsw
except ImportError:
    apsw=None
if apsw is not None:
    from native_capture_experiment import (CASES, CaptureFailure, NativeCapture,
        correctness_report, direct_access_report, fixture, operation,
        replacement_control, rollback_control, difference, snapshot)


@unittest.skipUnless(apsw is not None,'Optional APSW capture experiment is not installed')
class NativeCaptureTests(unittest.TestCase):
    def setUp(self):
        self.db=fixture(); self.addCleanup(self.db.close)

    def assert_restored(self, before, capture):
        self.assertEqual(difference(before,snapshot(self.db)),{})
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(capture.rows,[])
        self.assertIsNone(capture.failure)
        self.db.execute('PRAGMA foreign_keys')  # Policy/interrupt handlers cleared.

    def test_statement_matrix_replays_typed_images(self):
        results=correctness_report()
        self.assertEqual(len(results),18)
        self.assertEqual(results['delete_cascade']['changed_rows'],3)
        self.assertEqual(results['update_cascade']['changed_rows'],6)
        self.assertGreaterEqual(results['update_and_after_trigger']['max_trigger_depth'],1)
        self.assertEqual(results['no_op']['changed_rows'],0)
        self.assertEqual(results['coalesced']['changed_rows'],0)
        self.assertEqual(results['ignore']['event_count'],0)

    def test_without_rowid_replacement_sees_delete_without_recursive_triggers(self):
        result=replacement_control()
        self.assertEqual(result['native_actions'],['DELETE','INSERT'])
        self.assertEqual(result['missed_rows'],0)
        self.assertEqual(result['temp_journal_control_missed_rows'],1)

    def test_callback_rollback_requires_explicit_driver_discard(self):
        result=rollback_control()
        self.assertEqual(result['attempted_events'],2)
        self.assertEqual(result['events_after_raw_rollback'],2)
        self.assertEqual(result['events_after_driver_discard'],0)

    def test_explicit_rollback_clears_attempted_images(self):
        capture=NativeCapture(self.db); before=snapshot(self.db)
        result=operation(self.db,capture,CASES['update_and_after_trigger'],rollback=True)
        self.assertEqual(result['event_count'],2)
        self.assert_restored(before,capture)

    def test_constraint_policies_roll_back_prior_and_partial_statements(self):
        capture=NativeCapture(self.db); before=snapshot(self.db)
        for conflict in ('ABORT','FAIL','ROLLBACK'):
            with self.subTest(conflict=conflict),self.assertRaises(apsw.ConstraintError):
                operation(self.db,capture,[('UPDATE records SET value=11 WHERE key=\'a\'',()),
                    (f"INSERT OR {conflict} INTO records VALUES(1,'d',40),(1,'a',50)",())])
            self.assert_restored(before,capture)

    def test_deferred_foreign_key_commit_failure_discards_images(self):
        capture=NativeCapture(self.db); before=snapshot(self.db)
        with self.assertRaises(apsw.ConstraintError):
            operation(self.db,capture,[("INSERT INTO records VALUES(999,'d',40)",())])
        self.assert_restored(before,capture)

    def test_row_limit_counts_cascades_and_allows_exact_limit_after_failure(self):
        capture=NativeCapture(self.db,max_events=2); before=snapshot(self.db)
        with self.assertRaisesRegex(CaptureFailure,'CAPTURE_ROW_LIMIT'):
            operation(self.db,capture,CASES['delete_cascade'])
        self.assert_restored(before,capture)
        self.assertEqual(operation(self.db,capture,CASES['update_and_after_trigger'])['event_count'],2)

    def test_byte_limit_counts_utf8_and_blob_before_retaining_event(self):
        for payload in ('éé',b'\x00\xff\x00\xff'):
            with self.subTest(payload=payload),closing(fixture()) as db:
                capture=NativeCapture(db,max_bytes=134)
                self.assertEqual(operation(db,capture,[("INSERT INTO records VALUES(1,'d',?)",(payload,))])['charged_bytes'],134)
                before=snapshot(db)
                with self.assertRaisesRegex(CaptureFailure,'CAPTURE_BYTE_LIMIT'):
                    operation(db,capture,[("INSERT INTO records VALUES(1,'e',?)",(payload+payload,))])
                self.assertEqual(difference(before,snapshot(db)),{})
                self.assertFalse(db.in_transaction)
                self.assertEqual(capture.rows,[])

    def test_outer_ignore_and_replace_cannot_suppress_capture_failure(self):
        capture=NativeCapture(self.db,max_events=1); before=snapshot(self.db)
        for conflict in ('IGNORE','REPLACE'):
            with self.subTest(conflict=conflict),self.assertRaisesRegex(CaptureFailure,'CAPTURE_ROW_LIMIT'):
                operation(self.db,capture,[(f"INSERT OR {conflict} INTO records VALUES(1,'d',40),(1,'e',50)",())])
            self.assert_restored(before,capture)

    def test_callback_exception_with_empty_message_cannot_commit_uncaptured_changes(self):
        capture=NativeCapture(self.db); before=snapshot(self.db)
        with patch('native_capture_experiment.charge',side_effect=MemoryError()):
            with self.assertRaisesRegex(CaptureFailure,'MemoryError'):
                operation(self.db,capture,CASES['update_and_after_trigger'])
        self.assert_restored(before,capture)

    def test_large_returning_statement_stops_at_small_event_cap(self):
        capture=NativeCapture(self.db,max_events=100); before=snapshot(self.db)
        started=time.monotonic()
        with self.assertRaisesRegex(CaptureFailure,'CAPTURE_ROW_LIMIT'):
            operation(self.db,capture,[("""WITH RECURSIVE n(x) AS
                (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000)
                INSERT INTO records SELECT 1,'bulk-'||x,x FROM n RETURNING *""",())])
        self.assertLess(time.monotonic()-started,2)
        self.assert_restored(before,capture)

    def test_instruction_cancellation_rolls_back_and_recovers(self):
        capture=NativeCapture(self.db); before=snapshot(self.db)
        with self.assertRaises(apsw.InterruptError):
            operation(self.db,capture,[("UPDATE records SET value=11 WHERE key='a'",()),
                ('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n',())],
                instruction_limit=2000)
        self.assert_restored(before,capture)
        self.assertEqual(operation(self.db,capture,CASES['update_and_after_trigger'])['event_count'],2)

    def test_transaction_schema_private_state_and_multi_statement_entries_fail(self):
        capture=NativeCapture(self.db); before=snapshot(self.db)
        for sql in ('BEGIN','COMMIT','ROLLBACK','SAVEPOINT user','RELEASE user','ROLLBACK TO user',
                    'PRAGMA recursive_triggers=OFF','ATTACH \':memory:\' AS other','DETACH main',
                    'CREATE TABLE unmanaged(x)','CREATE TEMP TABLE unmanaged(x)','DROP TABLE records',
                    'ALTER TABLE records ADD COLUMN x','UPDATE reserved SET revision=1','SELECT * FROM reserved',
                    'SELECT * FROM sqlite_temp_schema'):
            with self.subTest(sql=sql),self.assertRaises(apsw.Error):
                operation(self.db,capture,[("UPDATE records SET value=11 WHERE key='a'",()),(sql,())])
            self.assert_restored(before,capture)
        with self.assertRaises(CaptureFailure):
            operation(self.db,capture,[("UPDATE records SET value=11 WHERE key='a'; UPDATE records SET value=12 WHERE key='a'",())])
        self.assert_restored(before,capture)

    def test_user_trigger_cannot_write_reserved_state(self):
        self.db.execute('CREATE TRIGGER bad AFTER UPDATE ON records BEGIN UPDATE reserved SET revision=1; END')
        capture=NativeCapture(self.db); before=snapshot(self.db)
        with self.assertRaises(apsw.AuthError): operation(self.db,capture,CASES['update_and_after_trigger'])
        self.assert_restored(before,capture)
        self.assertEqual(self.db.execute('SELECT revision FROM reserved').fetchone()[0],0)

    def test_independent_stdlib_connection_runs_without_callback(self):
        self.assertEqual(direct_access_report(),{'independent_stdlib_write_value':42,'native_events':0,
                                               'callbacks_registered_on_direct_connection':False})
