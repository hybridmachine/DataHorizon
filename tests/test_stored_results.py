# SPDX-License-Identifier: GPL-3.0-only
"""Tests for storing calculated results on read and serving them from the store."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from datahorizon import calculation_index, engine
from datahorizon.calculation_freshness import block_digest
from datahorizon.engine import Workbook
from datahorizon.formula import Calculator, FormulaError


class StoredResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "stored.dhwb"
        self.book = Workbook(self.path, create=True)
        self.addCleanup(self._close_book)

    def _close_book(self):
        if self.book is not None:
            self.book.close()
            self.book = None

    def reopen(self, **kwargs):
        self.book.close()
        self.book = Workbook(self.path, **kwargs)
        return self.book

    def apply(self, action="edit", sheet="Sheet1", **args):
        request = dict(action=action, operation_id=str(uuid.uuid4()), expected_state=self.book.state(), **args)
        if action in ("edit", "paste", "move", "fill", "replace_all"):
            request.setdefault("sheet", sheet)
        return self.book.mutate(request)

    def read_cell(self, row, col, sheet="Sheet1", **extra):
        cells = self.book.read(sheet, dict(r1=row, r2=row, c1=col, c2=col, **extra))["cells"]
        return cells[0] if cells else None

    def value(self, row, col, sheet="Sheet1"):
        return self.read_cell(row, col, sheet=sheet)["value"]

    def test_second_read_and_reopen_do_not_evaluate(self):
        self.apply(cells=[dict(row=1, col=1, source="2"), dict(row=1, col=2, source="=A1*3")])
        self.assertEqual(self.value(1, 2), 6)
        with patch.object(Calculator, "_evaluate_cell", side_effect=AssertionError("evaluated")):
            self.assertEqual(self.value(1, 2), 6)
            self.reopen()
            self.assertEqual(self.value(1, 2), 6)

    def test_precedents_and_digests_are_written_with_the_result(self):
        self.apply(cells=[dict(row=1, col=1, source="2"), dict(row=1, col=2, source="=A1*3")])
        self.assertEqual(self.value(1, 2), 6)
        sheet = self.book.sheet("Sheet1")
        self.assertEqual(self.book.store.result((sheet, 1, 2)), (True, 6))
        self.assertEqual(self.book.index.affected([(sheet, 1, 1, 1, 1)]), {(sheet, 1, 2)})
        self.assertEqual(self.book.store.digest(sheet, 0), block_digest(self.book.db, sheet, 0))

    def test_volatile_and_its_readers_are_not_stored(self):
        self.apply(cells=[
            dict(row=1, col=1, source="=NOW()"),
            dict(row=1, col=2, source="=A1"),
            dict(row=1, col=3, source="=7+8"),
        ])
        self.book.read("Sheet1", dict(r1=1, c1=1, r2=1, c2=3))
        sheet = self.book.sheet("Sheet1")
        self.assertEqual(self.book.store.result((sheet, 1, 1)), (False, None))
        self.assertEqual(self.book.store.result((sheet, 1, 2)), (False, None))
        self.assertEqual(self.book.store.result((sheet, 1, 3)), (True, 15))

    def test_limit_and_cycle_are_not_stored(self):
        self.apply(cells=[
            dict(row=1, col=1, source="=A1"),
            dict(row=1, col=2, source="=A1+1"),
            dict(row=2, col=1, source="1"),
            dict(row=2, col=2, source="=A2+1"),
            dict(row=2, col=3, source="=B2+1"),
        ])
        sheet = self.book.sheet("Sheet1")
        self.book.read("Sheet1", dict(r1=1, c1=1, r2=1, c2=2))
        self.assertEqual(self.book.store.result((sheet, 1, 1)), (False, None))
        self.assertEqual(self.book.store.result((sheet, 1, 2)), (False, None))

        # Force #LIMIT! with a zero-second calculator; neither it nor its reader is stored.
        calc = self.book.calculator(seconds=0)
        with self.assertRaises(FormulaError) as raised:
            calc.cell(sheet, 2, 3)
        self.assertEqual(raised.exception.code, "#LIMIT!")
        self.book.flush_results()
        self.assertEqual(self.book.store.result((sheet, 2, 2)), (False, None))
        self.assertEqual(self.book.store.result((sheet, 2, 3)), (False, None))

    def test_cell_errors_are_stored(self):
        self.apply(cells=[dict(row=1, col=1, source="=1/0")])
        cell = self.read_cell(1, 1)
        self.assertEqual((cell["calculation"], cell["display"]), ("error", "#DIV/0!"))
        sheet = self.book.sheet("Sheet1")
        found, err = self.book.store.result((sheet, 1, 1))
        self.assertTrue(found)
        self.assertIsInstance(err, FormulaError)
        self.assertEqual(err.code, "#DIV/0!")

    def test_cached_read_serves_stored_and_marks_the_rest_pending(self):
        self.apply(cells=[dict(row=1, col=1, source="2"), dict(row=1, col=2, source="=A1*3")])
        before = self.read_cell(1, 2, calculation="cached")
        self.assertEqual((before["calculation"], before["display"]), ("pending", "…"))
        self.assertEqual(self.value(1, 2), 6)
        after = self.read_cell(1, 2, calculation="cached")
        self.assertEqual((after["calculation"], after["value"]), ("current", 6))

    def test_batch_is_discarded_when_the_file_changed_under_the_read(self):
        self.apply(cells=[dict(row=1, col=1, source="2"), dict(row=1, col=2, source="=A1*3")])
        sheet = self.book.sheet("Sheet1")
        real_flush = Workbook.flush_results

        def racing_flush(book_self, *args, **kwargs):
            with closing(sqlite3.connect(self.path)) as direct, direct:
                direct.execute("UPDATE cells SET source='9' WHERE sheet_id=? AND row=1 AND col=1", (sheet,))
            return real_flush(book_self, *args, **kwargs)

        with patch.object(Workbook, "flush_results", racing_flush):
            self.assertEqual(self.value(1, 2), 6)
        self.assertEqual(self.book.store.result((sheet, 1, 2)), (False, None))
        self.assertEqual(self.value(1, 2), 27)

    def test_write_failure_does_not_fail_the_read(self):
        self.apply(cells=[dict(row=1, col=1, source="2"), dict(row=1, col=2, source="=A1*3")])
        skips_before = self.book.store_skips
        with patch.object(self.book, "store_write", side_effect=sqlite3.OperationalError("database or disk is full")):
            self.assertEqual(self.value(1, 2), 6)
        self.assertGreater(self.book.store_skips, skips_before)
        self.assertEqual(self.value(1, 2), 6)

    def test_range_too_large_for_the_allowance_is_not_stored(self):
        self.apply(cells=[dict(row=row, col=1, source="1") for row in range(1, 1001)]
                   + [dict(row=1, col=2, source="=SUM(A1:A1000)")])
        sheet = self.book.sheet("Sheet1")
        with patch.object(engine, "STORE_WRITE_SECONDS", 0):
            self.assertEqual(self.value(1, 2), 1000)
        self.assertEqual(self.book.store.result((sheet, 1, 2)), (False, None))
        self.reopen()
        self.assertEqual(self.value(1, 2), 1000)

    def test_cap_stops_storing_without_discarding(self):
        self.apply(cells=[
            dict(row=1, col=1, source="=1+1"),
            dict(row=2, col=1, source="=2+2"),
            dict(row=3, col=1, source="=3+3"),
        ])
        with patch.object(engine, "STORED_FORMULA_LIMIT", 2), patch.object(calculation_index, "STORED_FORMULA_LIMIT", 2):
            cells = self.book.read("Sheet1", dict(r1=1, c1=1, r2=3, c2=1))["cells"]
            self.assertEqual([c["value"] for c in cells], [2, 4, 6])
            self.assertEqual(self.book.store.formula_count(), 2)
            cells2 = self.book.read("Sheet1", dict(r1=1, c1=1, r2=3, c2=1))["cells"]
            self.assertEqual([c["value"] for c in cells2], [2, 4, 6])
            self.assertEqual(self.book.store.formula_count(), 2)

    def test_transactional_calculators_do_not_use_the_store(self):
        self.apply(cells=[dict(row=1, col=1, source="5"), dict(row=1, col=2, source="=A1*2")])
        handler = Path(self.temp.name) / "read_hook.py"
        handler.write_text(
            "def on_change(ctx, changes):\n"
            "    val = ctx.value('Sheet1', 1, 2)\n"
            "    if val != 20:\n"
            "        raise ValueError(f'unexpected {val}')\n"
        )
        self.book.handler = str(handler)
        with patch.object(self.book.store, "result", side_effect=AssertionError("must not read store in hook")):
            receipt = self.apply(cells=[dict(row=1, col=1, source="10")])
        self.assertEqual(receipt["outcome"], "committed")


if __name__ == "__main__":
    unittest.main()
