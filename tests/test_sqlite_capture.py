"""M1 capture evidence, independent of the production managed SQL surface."""

from contextlib import closing
import sqlite3
import unittest

from sqlite_capture_experiment import (CASES, Journal, correctness_report, difference,
                                       direct_access_report, fixture, negative_controls,
                                       operation, snapshot)


class SQLiteCaptureTests(unittest.TestCase):
    def setUp(self):
        self.db = fixture()
        self.addCleanup(self.db.close)

    def assertStoredEqual(self, expected):
        self.assertEqual(difference(expected, snapshot(self.db)), {})
        self.assertFalse(self.db.in_transaction)

    def test_statement_matrix_replays_every_typed_image_against_independent_snapshot(self):
        results = correctness_report()
        self.assertEqual(len(results), 18)
        self.assertEqual(results["update_and_after_trigger"]["event_count"], 2)
        self.assertEqual(results["delete_cascade"]["changed_rows"], 3)
        self.assertEqual(results["update_cascade"]["changed_rows"], 6)
        self.assertEqual(results["no_op"]["changed_rows"], 0)
        self.assertEqual(results["coalesced"]["changed_rows"], 0)
        self.assertEqual(results["ignore"]["event_count"], 0)

    def test_replacement_has_deleted_old_row_and_inserted_new_row(self):
        journal = Journal(self.db)
        result = operation(self.db, journal, CASES["replace_unique_key"])
        self.assertEqual([(table, action, old, new) for _, table, action, old, new in result["events"]],
                         [("records", "DELETE", (1, "a", 10), None),
                          ("records", "INSERT", None, (2, "a", 12))])

    def test_storage_types_and_embedded_nul_survive_iteration(self):
        journal = Journal(self.db)
        operation(self.db, journal, CASES["typed_values"])
        images = [new[2] for _, table, _, _, new in journal.events(batch_size=1) if table == "records"]
        self.assertEqual(images, [None, 1, 1.0, "1", b"\x00\xff", "\x00\u00e9"])
        self.assertEqual(list(map(type, images)), [type(None), int, float, str, bytes, str])
        for size in (0, 129):
            with self.assertRaises(ValueError):
                list(journal.events(batch_size=size))

    def test_explicit_rollback_removes_data_events_and_budget(self):
        journal = Journal(self.db)
        before = snapshot(self.db)
        result = operation(self.db, journal, CASES["update_and_after_trigger"], rollback=True)
        self.assertEqual(result["event_count"], 2)
        self.assertStoredEqual(before)
        self.assertEqual(list(journal.events()), [])
        self.assertEqual(self.db.execute("SELECT * FROM temp.capture_budget").fetchone(), (0, 0))

    def test_constraint_abort_and_fail_roll_back_prior_statements_and_partial_statement(self):
        journal = Journal(self.db)
        before = snapshot(self.db)
        for conflict in ("ABORT", "FAIL", "ROLLBACK"):
            with self.subTest(conflict=conflict), self.assertRaises(sqlite3.IntegrityError):
                operation(self.db, journal, [
                    ("UPDATE records SET value=11 WHERE key='a'", ()),
                    (f"INSERT OR {conflict} INTO records VALUES(1,'d',40),(1,'a',50)", ())])
            self.assertStoredEqual(before)
            self.assertEqual(list(journal.events()), [])
            # Rollback/FAIL must not leave the progress handler/authorizer active.
            self.db.execute("PRAGMA foreign_keys")

    def test_deferred_foreign_key_commit_failure_rolls_back_capture(self):
        journal = Journal(self.db)
        before = snapshot(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            operation(self.db, journal, [("INSERT INTO records VALUES(999,'d',40)", ())])
        self.assertStoredEqual(before)
        self.assertEqual(list(journal.events()), [])

    def test_row_limit_includes_trigger_and_cascade_events_and_rejects_atomically(self):
        journal = Journal(self.db, max_events=2)
        before = snapshot(self.db)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CAPTURE_ROW_LIMIT"):
            operation(self.db, journal, CASES["delete_cascade"])
        self.assertStoredEqual(before)
        self.assertEqual(list(journal.events()), [])
        # Exactly the limit remains accepted; a trigger-generated effect counts.
        self.assertEqual(operation(self.db, journal, CASES["update_and_after_trigger"])["event_count"], 2)

    def test_byte_limit_checks_utf8_and_blob_payload_before_appending(self):
        # One insertion charges 128 + '1' + 'd' + 4 bytes of UTF-8/BLOB payload.
        for payload in ("\u00e9\u00e9", b"\x00\xff\x00\xff"):
            with self.subTest(payload=payload), closing(fixture()) as db:
                journal = Journal(db, max_bytes=134)
                accepted = operation(db, journal, [("INSERT INTO records VALUES(1,'d',?)", (payload,))])
                self.assertEqual(accepted["charged_bytes"], 134)
                before = snapshot(db)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "CAPTURE_BYTE_LIMIT"):
                    operation(db, journal, [("INSERT INTO records VALUES(1,'e',?)", (payload+payload,))])
                self.assertEqual(difference(before, snapshot(db)), {})
                self.assertEqual(list(journal.events()), [])

    def test_outer_ignore_or_replace_cannot_bypass_capture_limit(self):
        journal = Journal(self.db, max_events=1)
        before = snapshot(self.db)
        for conflict in ("IGNORE", "REPLACE"):
            with self.subTest(conflict=conflict), self.assertRaisesRegex(sqlite3.IntegrityError, "CAPTURE_ROW_LIMIT"):
                operation(self.db, journal, [(f"INSERT OR {conflict} INTO records VALUES(1,'d',40),(1,'e',50)", ())])
            self.assertStoredEqual(before)

    def test_bounded_large_statement_aborts_without_materializing_returning(self):
        journal = Journal(self.db, max_events=100)
        before = snapshot(self.db)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CAPTURE_ROW_LIMIT"):
            operation(self.db, journal, [("""WITH RECURSIVE n(x) AS
                (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000)
                INSERT INTO records SELECT 1,'bulk-'||x,x FROM n RETURNING *""", ())])
        self.assertStoredEqual(before)
        self.assertEqual(list(journal.events()), [])

    def test_instruction_cancellation_rolls_back_and_next_operation_can_commit(self):
        journal = Journal(self.db)
        before = snapshot(self.db)
        with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
            operation(self.db, journal, [
                ("UPDATE records SET value=11 WHERE key='a'", ()),
                ("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n", ())],
                instruction_limit=2000)
        self.assertStoredEqual(before)
        self.assertEqual(list(journal.events()), [])
        self.assertEqual(operation(self.db, journal, CASES["update_and_after_trigger"])["event_count"], 2)

    def test_transaction_schema_attachment_and_private_state_are_unavailable(self):
        journal = Journal(self.db)
        before = snapshot(self.db)
        statements = ["BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT user", "RELEASE user", "ROLLBACK TO user",
                      "PRAGMA recursive_triggers=OFF", "PRAGMA foreign_keys=OFF", "ATTACH ':memory:' AS other",
                      "DETACH main", "CREATE TABLE unmanaged(x)", "CREATE TEMP TABLE unmanaged(x)",
                      "DROP TABLE records", "ALTER TABLE records ADD COLUMN x", "DROP TRIGGER record_effect",
                      "CREATE TEMP TRIGGER capture_fake AFTER INSERT ON records BEGIN SELECT 1; END",
                      "UPDATE reserved SET revision=1", "SELECT * FROM temp.capture",
                      "DELETE FROM temp.capture", "UPDATE temp.capture_budget SET events=0", "VACUUM"]
        for sql in statements:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                operation(self.db, journal, [("UPDATE records SET value=11 WHERE key='a'", ()), (sql, ())])
            self.assertStoredEqual(before)
            self.assertEqual(list(journal.events()), [])
            self.assertEqual(self.db.execute("SELECT revision FROM reserved").fetchone()[0], 0)

    def test_user_trigger_cannot_write_reserved_metadata(self):
        self.db.execute("""CREATE TRIGGER bad AFTER UPDATE ON records BEGIN
            UPDATE reserved SET revision=1; END""")
        journal = Journal(self.db)
        before = snapshot(self.db)
        with self.assertRaises(sqlite3.DatabaseError):
            operation(self.db, journal, CASES["update_and_after_trigger"])
        self.assertStoredEqual(before)
        self.assertEqual(self.db.execute("SELECT revision FROM reserved").fetchone()[0], 0)

    def test_private_savepoint_rollback_discards_only_its_images(self):
        journal = Journal(self.db)
        before = snapshot(self.db)
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("INSERT INTO records VALUES(1,'d',40)")
        self.db.execute("SAVEPOINT engine_owned")
        self.db.execute("UPDATE records SET value=11 WHERE key='a'")
        self.db.execute("ROLLBACK TO engine_owned")
        self.db.execute("RELEASE engine_owned")
        self.assertEqual(len(list(journal.events())), 1)
        self.assertEqual(difference(journal.replay(before), snapshot(self.db)), {})
        self.db.execute("COMMIT")

    def test_journal_is_local_to_connection_and_not_in_snapshot(self):
        self.assertEqual(direct_access_report(), {"independent_write_value": 42, "managed_capture_events": 0,
                                                "independent_temp_triggers": 0, "snapshot_matches": True})

    def test_negative_controls_expose_incomplete_capture(self):
        controls = negative_controls()
        self.assertEqual(controls["returning_and_changes"],
                         {"returned_rows": 1, "actual_changed_rows": 3, "changes_count": 1})
        self.assertEqual(controls["recursive_triggers_off"]["missed_rows"], 1)


if __name__ == "__main__":
    unittest.main()
