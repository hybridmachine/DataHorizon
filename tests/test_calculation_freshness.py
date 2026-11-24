# SPDX-License-Identifier: GPL-3.0-only
"""Tests for input block digests, sheets digests, and commit digest maintenance."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from datahorizon.calculation_freshness import block_digest, ensure_digests, sheets_digest
from datahorizon.engine import EngineError, Workbook


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


if __name__ == "__main__":
    unittest.main()
