# SPDX-License-Identifier: GPL-3.0-only
"""Tests for input block digests, sheets digests, and commit digest maintenance."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from datahorizon.calculation_freshness import Verdict, apply, block_digest, compare, ensure_digests, sheets_digest
from datahorizon.calculation_store import TABLES as CALC_TABLES
from datahorizon.engine import EngineError, Workbook
from datahorizon.formula import MAX_COLS


def mutate(book, **request):
    return book.mutate(dict(operation_id=str(uuid.uuid4()), expected_state=book.state(), **request))


class DigestMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fresh.dhwb"
        self.book = Workbook(self.path, create=True)
        self.addCleanup(self.book.close)
        self.sheet = self.book.sheet("Sheet1")

    def test_digest_covers_source_kind_and_coordinates_not_style(self):
        mutate(self.book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "42", "kind": "auto"}])
        d0 = block_digest(self.book.db, self.sheet, 0)
        self.assertIsInstance(d0, bytes)
        self.assertEqual(len(d0), 32)

        # Changing only style must not change the digest.
        mutate(self.book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "style": {"bold": True}}])
        self.assertEqual(block_digest(self.book.db, self.sheet, 0), d0)

        # Changing source changes the digest.
        mutate(self.book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "43", "kind": "auto"}])
        d_source = block_digest(self.book.db, self.sheet, 0)
        self.assertNotEqual(d_source, d0)

        # Changing kind changes the digest.
        mutate(self.book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "43", "kind": "text"}])
        d_kind = block_digest(self.book.db, self.sheet, 0)
        self.assertNotEqual(d_kind, d_source)

        # Moving the cell within the same block changes the digest.
        mutate(self.book, action="edit", sheet="Sheet1", cells=[
            {"row": 1, "col": 1, "clear": True},
            {"row": 2, "col": 1, "source": "43", "kind": "text"},
        ])
        d_moved = block_digest(self.book.db, self.sheet, 0)
        self.assertNotEqual(d_moved, d_kind)

    def test_digest_of_an_empty_block_is_none(self):
        self.assertIsNone(block_digest(self.book.db, self.sheet, 0))
        self.assertIsNone(block_digest(self.book.db, self.sheet, 5))

    def test_blob_source_hashes(self):
        mutate(self.book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "hello"}])
        before = block_digest(self.book.db, self.sheet, 0)
        with closing(sqlite3.connect(self.path)) as direct, direct:
            direct.execute("UPDATE cells SET source=x'00ff' WHERE sheet_id=? AND row=1 AND col=1", (self.sheet,))
        after = block_digest(self.book.db, self.sheet, 0)
        self.assertIsInstance(after, bytes)
        self.assertEqual(len(after), 32)
        self.assertNotEqual(after, before)

    def test_commit_rehashes_only_digested_blocks(self):
        mutate(self.book, action="edit", sheet="Sheet1", cells=[
            {"row": 1, "col": 1, "source": "1"},
            {"row": 100, "col": 1, "source": "2"},
        ])
        self.book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(self.book.store, self.book.db, [(self.sheet, 0)])
        self.book.db.execute("COMMIT")
        self.assertIsNotNone(self.book.store.digest(self.sheet, 0))
        self.assertIsNone(self.book.store.digest(self.sheet, 1))

        mutate(self.book, action="edit", sheet="Sheet1", cells=[
            {"row": 1, "col": 1, "source": "10"},
            {"row": 100, "col": 1, "source": "20"},
        ])
        self.assertEqual(self.book.store.digest(self.sheet, 0), block_digest(self.book.db, self.sheet, 0))
        self.assertIsNone(self.book.store.digest(self.sheet, 1))

    def test_commit_deleting_last_cell_removes_block_digest(self):
        mutate(self.book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "1"}])
        self.book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(self.book.store, self.book.db, [(self.sheet, 0)])
        self.book.db.execute("COMMIT")
        self.assertIsNotNone(self.book.store.digest(self.sheet, 0))

        mutate(self.book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "clear": True}])
        self.assertIsNone(self.book.store.digest(self.sheet, 0))

    def test_structural_edit_drops_digests_and_results(self):
        mutate(self.book, action="edit", sheet="Sheet1", cells=[
            {"row": 1, "col": 1, "source": "5"},
            {"row": 1, "col": 2, "source": "=A1*2"},
        ])
        self.book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(self.book.store, self.book.db, [(self.sheet, 0)])
        self.book.store.write([((self.sheet, 1, 2), 10, [(self.sheet, 1, 1, 1, 1)])])
        self.book.db.execute("COMMIT")
        self.assertEqual(self.book.store.formula_count(), 1)
        self.assertIsNotNone(self.book.store.digest(self.sheet, 0))

        mutate(self.book, action="rows_insert", sheet="Sheet1", start=1, count=1)
        self.assertIsNone(self.book.store.digest(self.sheet, 0))
        self.assertEqual(self.book.store.formula_count(), 0)
        self.assertEqual(self.book.store.sheets_digest(), sheets_digest(self.book.db))

    def test_external_edit_before_the_write_lock_empties_the_store(self):
        mutate(self.book, action="edit", sheet="Sheet1", cells=[
            {"row": 1, "col": 1, "source": "5"},
            {"row": 1, "col": 2, "source": "=A1*2"},
        ])
        self.book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(self.book.store, self.book.db, [(self.sheet, 0)])
        self.book.store.write([((self.sheet, 1, 2), 10, [(self.sheet, 1, 1, 1, 1)])])
        self.book.db.execute("COMMIT")

        real_refresh = Workbook.refresh
        calls = {"n": 0}

        def racy_refresh(book_self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                real_refresh(book_self, *args, **kwargs)
                with closing(sqlite3.connect(self.path)) as direct, direct:
                    direct.execute("UPDATE cells SET source='99' WHERE sheet_id=? AND row=1 AND col=1", (self.sheet,))
            # Second call inside BEGIN IMMEDIATE is suppressed here so the commit proceeds.

        with patch.object(Workbook, "refresh", racy_refresh):
            receipt = mutate(self.book, action="edit", sheet="Sheet1", cells=[{"row": 2, "col": 1, "source": "7"}])
        self.assertEqual(receipt["outcome"], "committed")
        self.assertEqual(list(self.book.store.digests()), [])
        self.assertEqual(self.book.store.formula_count(), 0)

    def test_rollback_leaves_digests_as_they_were(self):
        mutate(self.book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "5"}])
        self.book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(self.book.store, self.book.db, [(self.sheet, 0)])
        self.book.db.execute("COMMIT")
        before = self.book.store.digest(self.sheet, 0)

        handler = Path(self.temp.name) / "reject.py"
        handler.write_text("def on_change(ctx, changes):\n    raise ValueError('reject')\n")
        self.book.handler = str(handler)
        with self.assertRaises(EngineError) as raised:
            mutate(self.book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "10"}])
        self.assertEqual(raised.exception.body["code"], "HOOK_FAILED")
        self.assertEqual(self.book.store.digest(self.sheet, 0), before)


class OpenVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "open_verify.dhwb"

    def _new(self):
        book = Workbook(self.path, create=True)
        return book

    def _direct(self, sql, *params):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(sql, params)

    def test_unchanged_file_keeps_every_result(self):
        book = self._new()
        sheet = book.sheet("Sheet1")
        mutate(book, action="edit", sheet="Sheet1", cells=[
            {"row": 1, "col": 1, "source": "2"},
            {"row": 1, "col": 2, "source": "=A1*3"},
            {"row": 2, "col": 2, "source": "=A1+1"},
            {"row": 3, "col": 2, "source": "=7+8"},
        ])
        book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(book.store, book.db, [(sheet, 0)])
        book.store.write([
            ((sheet, 1, 2), 6, [(sheet, 1, 1, 1, 1)]),
            ((sheet, 2, 2), 3, [(sheet, 1, 1, 1, 1)]),
            ((sheet, 3, 2), 15, ()),
        ])
        book.db.execute("COMMIT")
        book.close()

        reopened = Workbook(self.path)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.verified)
        self.assertEqual(reopened.store.result((sheet, 1, 2)), (True, 6))
        self.assertEqual(reopened.store.result((sheet, 2, 2)), (True, 3))
        self.assertEqual(reopened.store.result((sheet, 3, 2)), (True, 15))
        self.assertEqual(reopened.store.roots(), [])

    def test_changed_cell_roots_its_block(self):
        book = self._new()
        sheet = book.sheet("Sheet1")
        mutate(book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "2"}])
        book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(book.store, book.db, [(sheet, 0)])
        book.db.execute("COMMIT")
        book.close()

        self._direct("UPDATE cells SET source='99' WHERE sheet_id=? AND row=1 AND col=1", sheet)
        reopened = Workbook(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.store.roots(), [(sheet, 1, 1, 64, MAX_COLS)])
        self.assertEqual(reopened.store.digest(sheet, 0), block_digest(reopened.db, sheet, 0))

    def test_cell_added_to_an_empty_block_roots_it(self):
        book = self._new()
        sheet = book.sheet("Sheet1")
        mutate(book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "2"}])
        book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(book.store, book.db, [(sheet, 0)])
        book.db.execute("COMMIT")
        book.close()

        self._direct("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,200,1,'5','number')", sheet)
        reopened = Workbook(self.path)
        self.addCleanup(reopened.close)
        self.assertIn((sheet, 193, 1, 256, MAX_COLS), reopened.store.roots())
        self.assertEqual(reopened.store.digest(sheet, 3), block_digest(reopened.db, sheet, 3))

    def test_blob_source_does_not_stop_the_scan(self):
        book = self._new()
        sheet = book.sheet("Sheet1")
        mutate(book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "hi"}])
        book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(book.store, book.db, [(sheet, 0)])
        book.db.execute("COMMIT")
        book.close()

        self._direct("UPDATE cells SET source=x'00ff' WHERE sheet_id=? AND row=1 AND col=1", sheet)
        reopened = Workbook(self.path)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.verified)
        self.assertIn((sheet, 1, 1, 64, MAX_COLS), reopened.store.roots())

    def test_deleting_the_only_cell_of_a_block(self):
        book = self._new()
        sheet = book.sheet("Sheet1")
        mutate(book, action="edit", sheet="Sheet1", cells=[
            {"row": 1, "col": 1, "source": "5"},
            {"row": 100, "col": 2, "source": "=A1+1"},
        ])
        book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(book.store, book.db, [(sheet, 0), (sheet, 1)])
        book.store.write([((sheet, 100, 2), 6, [(sheet, 1, 1, 1, 1)])])
        book.db.execute("COMMIT")
        book.close()

        self._direct("DELETE FROM cells WHERE sheet_id=? AND row=1 AND col=1", sheet)
        reopened = Workbook(self.path)
        self.addCleanup(reopened.close)
        self.assertIn((sheet, 1, 1, 64, MAX_COLS), reopened.store.roots())
        self.assertIsNone(reopened.store.digest(sheet, 0))

    def test_formula_with_no_precedents_given_new_text(self):
        book = self._new()
        sheet = book.sheet("Sheet1")
        mutate(book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "=1+1"}])
        book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(book.store, book.db, [(sheet, 0)])
        book.store.write([((sheet, 1, 1), 2, ())])
        book.db.execute("COMMIT")
        book.close()

        self._direct("UPDATE cells SET source='=3+4' WHERE sheet_id=? AND row=1 AND col=1", sheet)
        reopened = Workbook(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.store.result((sheet, 1, 1)), (False, None))

    def test_renamed_sheet_clears_everything(self):
        book = self._new()
        sheet = book.sheet("Sheet1")
        mutate(book, action="edit", sheet="Sheet1", cells=[
            {"row": 1, "col": 1, "source": "2"},
            {"row": 1, "col": 2, "source": "=A1*3"},
        ])
        book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(book.store, book.db, [(sheet, 0)])
        book.store.write([((sheet, 1, 2), 6, [(sheet, 1, 1, 1, 1)])])
        book.db.execute("COMMIT")
        book.close()

        self._direct("UPDATE sheets SET name='Renamed' WHERE id=?", sheet)
        reopened = Workbook(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.store.formula_count(), 0)
        self.assertEqual(list(reopened.store.precedents()), [])
        self.assertEqual(reopened.store.sheets_digest(), sheets_digest(reopened.db))

    def test_deleted_derived_rows_are_tolerated(self):
        book = self._new()
        sheet = book.sheet("Sheet1")
        mutate(book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "42"}])
        book.close()

        with closing(sqlite3.connect(self.path)) as db, db:
            for table in CALC_TABLES:
                db.execute(f"DELETE FROM {table}")
        reopened = Workbook(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.read("Sheet1", {"r1": 1, "c1": 1, "r2": 1, "c2": 1})["cells"][0]["value"], 42)
        self.assertEqual(reopened.store.sheets_digest(), sheets_digest(reopened.db))

    def test_scan_past_its_deadline_empties_and_still_opens(self):
        book = self._new()
        sheet = book.sheet("Sheet1")
        mutate(book, action="edit", sheet="Sheet1", cells=[
            {"row": 1, "col": 1, "source": "3"},
            {"row": 1, "col": 2, "source": "=A1*4"},
        ])
        book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(book.store, book.db, [(sheet, 0)])
        book.store.write([((sheet, 1, 2), 12, [(sheet, 1, 1, 1, 1)])])
        book.db.execute("COMMIT")
        book.close()

        with patch("datahorizon.engine.compare", return_value=None):
            reopened = Workbook(self.path)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.verified)
        self.assertEqual(reopened.store.formula_count(), 0)
        self.assertEqual(list(reopened.store.digests()), [])
        self.assertEqual(reopened.read("Sheet1", {"r1": 1, "c1": 2, "r2": 1, "c2": 2})["cells"][0]["value"], 12)

    def test_read_only_open_applies_to_the_temporary_store(self):
        book = self._new()
        sheet = book.sheet("Sheet1")
        mutate(book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "5"}])
        book.db.execute("BEGIN IMMEDIATE")
        ensure_digests(book.store, book.db, [(sheet, 0)])
        book.db.execute("COMMIT")
        book.close()

        self._direct("UPDATE cells SET source='8' WHERE sheet_id=? AND row=1 AND col=1", sheet)
        before_bytes = self.path.read_bytes()
        ro = Workbook(self.path, readonly=True)
        self.addCleanup(ro.close)
        self.assertEqual(ro.store.schema, "dhcalc")
        self.assertIn((sheet, 1, 1, 64, MAX_COLS), ro.store.roots())
        self.assertEqual(self.path.read_bytes(), before_bytes)

    def test_v8_open_does_not_scan(self):
        book = self._new()
        book.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            for table in CALC_TABLES:
                db.execute(f"DROP TABLE {table}")
            db.execute("UPDATE dh_meta SET format_version=8")
        with patch("datahorizon.engine.compare", side_effect=AssertionError("must not scan v8")):
            reopened = Workbook(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.info()["format_version"], 8)


if __name__ == "__main__":
    unittest.main()
