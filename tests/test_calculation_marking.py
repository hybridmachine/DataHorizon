# SPDX-License-Identifier: GPL-3.0-only
"""Tests for recording pending roots in commits and sliced marking/deletion."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from datahorizon import engine
from datahorizon.calculation_store import TABLES as CALC_TABLES
from datahorizon.engine import Workbook
from datahorizon.formula import Calculator


class CalculationMarkingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "marking.dhwb"
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

    def value(self, row, col, sheet="Sheet1"):
        return self.book.read(sheet, dict(r1=row, r2=row, c1=col, c2=col))["cells"][0]["value"]

    def test_edit_unserves_dependents_at_once(self):
        self.apply(cells=[
            dict(row=1, col=1, source="2"),
            dict(row=1, col=2, source="=A1*3"),
            dict(row=1, col=3, source="=B1+1"),
            dict(row=1, col=4, source="=7+8"),
        ])
        self.assertEqual(self.value(1, 3), 7)
        self.assertEqual(self.value(1, 4), 15)
        sheet = self.book.sheet("Sheet1")
        d1 = (sheet, 1, 4)

        self.apply(cells=[dict(row=1, col=1, source="5")])
        self.assertEqual(self.value(1, 3), 16)

        real_eval = Calculator._evaluate_cell

        def fail_on_d1(calc_self, key):
            if key == d1:
                raise AssertionError("D1 was re-evaluated")
            return real_eval(calc_self, key)

        with patch.object(Calculator, "_evaluate_cell", fail_on_d1):
            self.assertEqual(self.value(1, 4), 15)

    def test_edited_formula_with_no_precedents(self):
        self.apply(cells=[dict(row=1, col=1, source="=1+1")])
        self.assertEqual(self.value(1, 1), 2)
        sheet = self.book.sheet("Sheet1")
        self.assertEqual(self.book.store.result((sheet, 1, 1)), (True, 2))
        self.apply(cells=[dict(row=1, col=1, source="=3+4")])
        self.assertEqual(self.value(1, 1), 7)

    def test_formula_that_becomes_volatile(self):
        self.apply(cells=[
            dict(row=1, col=1, source="2"),
            dict(row=1, col=2, source="=A1"),
            dict(row=1, col=3, source="=B1"),
        ])
        self.assertEqual(self.value(1, 3), 2)
        sheet = self.book.sheet("Sheet1")
        self.assertEqual(self.book.store.result((sheet, 1, 2)), (True, 2))
        self.assertEqual(self.book.store.result((sheet, 1, 3)), (True, 2))

        self.apply(cells=[dict(row=1, col=2, source="=NOW()")])
        self.book.read("Sheet1", dict(r1=1, c1=2, r2=1, c2=3))
        self.assertEqual(self.book.store.result((sheet, 1, 2)), (False, None))
        self.assertEqual(self.book.store.result((sheet, 1, 3)), (False, None))

    def test_undo_and_redo_mark_roots(self):
        self.apply(cells=[dict(row=1, col=1, source="2"), dict(row=1, col=2, source="=A1*3")])
        self.assertEqual(self.value(1, 2), 6)
        self.apply(cells=[dict(row=1, col=1, source="5")])
        self.assertEqual(self.value(1, 2), 15)
        self.apply("undo")
        self.assertEqual(self.value(1, 2), 6)
        self.apply("redo")
        self.assertEqual(self.value(1, 2), 15)

    def test_undo_of_the_upgrading_commit_stays_v9(self):
        self.apply(cells=[dict(row=1, col=1, source="2"), dict(row=1, col=2, source="=A1*3")])
        self.book.close()
        with closing(sqlite3.connect(self.path)) as direct, direct:
            for table in reversed(CALC_TABLES):
                direct.execute(f"DROP TABLE {table}")
            direct.execute("UPDATE dh_meta SET format_version=8")
        self.book = Workbook(self.path)
        self.assertEqual(self.book.info()["format_version"], 8)
        self.assertEqual(self.value(1, 2), 6)

        self.apply(cells=[dict(row=1, col=1, source="5")])
        self.assertEqual(self.book.info()["format_version"], 9)
        self.assertEqual(self.value(1, 2), 15)

        self.apply("undo")
        self.assertEqual(self.book.info()["format_version"], 9)
        self.assertEqual(self.value(1, 2), 6)

    def test_slices_and_roots(self):
        self.apply(cells=[dict(row=1, col=1, source="2")]
                   + [dict(row=row, col=2, source=f"=A1+{row}") for row in range(1, 6)])
        self.book.read("Sheet1", dict(r1=1, c1=2, r2=5, c2=2))
        self.assertEqual(self.book.store.formula_count(), 5)

        with patch.object(engine, "DELETE_SLICE", 2):
            self.apply(cells=[dict(row=1, col=1, source="10")])
            self.assertEqual(len(self.book.store.roots()), 1)
            self.assertEqual(len(self.book.unserved), 3)

            self.book.info()
            self.assertEqual(len(self.book.store.roots()), 1)
            self.assertEqual(len(self.book.unserved), 1)

            self.book.info()
            self.assertEqual(self.book.unserved, set())
            self.assertEqual(self.book.store.roots(), [])

    def test_reopen_with_roots_finishes_the_deletion(self):
        self.apply(cells=[dict(row=1, col=1, source="2")]
                   + [dict(row=row, col=2, source=f"=A1+{row}") for row in range(1, 6)])
        self.book.read("Sheet1", dict(r1=1, c1=2, r2=5, c2=2))

        with patch.object(engine, "DELETE_SLICE", 2):
            self.apply(cells=[dict(row=1, col=1, source="10")])
            self.assertEqual(len(self.book.store.roots()), 1)
            self.book.close()

        # Verify close() did not settle the remaining root on disk.
        with closing(sqlite3.connect(self.path)) as direct:
            self.assertEqual(direct.execute("SELECT count(*) FROM dh_calc_roots").fetchone()[0], 1)

        self.book = Workbook(self.path)
        cells = self.book.read("Sheet1", dict(r1=1, c1=2, r2=5, c2=2))["cells"]
        self.assertEqual([c["value"] for c in cells], [11, 12, 13, 14, 15])
        self.assertEqual(self.book.store.roots(), [])

    def test_roots_survive_a_request_that_makes_no_calculator(self):
        self.apply(cells=[dict(row=1, col=1, source="2")]
                   + [dict(row=row, col=2, source=f"=A1+{row}") for row in range(1, 6)])
        self.book.read("Sheet1", dict(r1=1, c1=2, r2=5, c2=2))
        sheet = self.book.sheet("Sheet1")

        with patch.object(engine, "DELETE_SLICE", 2):
            self.apply(cells=[dict(row=1, col=1, source="10")])
            self.book.close()

        self.book = Workbook(self.path)
        with patch.object(engine, "DELETE_SLICE", 2):
            self.book.info()
            # No root may be cleared while any dependent result still exists in SQLite.
            remaining_in_db = [
                row for row in range(1, 6) if self.book.store.result((sheet, row, 2))[0]
            ]
            self.assertGreater(len(remaining_in_db), 0)
            self.assertEqual(len(self.book.store.roots()), 1)
            for row in remaining_in_db:
                self.assertIn((sheet, row, 2), self.book.unserved)
            # Cached read never serves any stale dependent.
            cached = self.book.read("Sheet1", dict(r1=1, c1=2, r2=5, c2=2, calculation="cached"))["cells"]
            self.assertTrue(all(c["calculation"] == "pending" for c in cached))

    def test_result_recalculated_before_its_slice_is_kept(self):
        self.apply(cells=[
            dict(row=1, col=1, source="2"),
            dict(row=1, col=2, source="=A1*3"),
            dict(row=1, col=3, source="=A1*4"),
        ])
        self.assertEqual(self.value(1, 2), 6)
        self.assertEqual(self.value(1, 3), 8)
        sheet = self.book.sheet("Sheet1")
        b1 = (sheet, 1, 2)

        with patch.object(engine, "DELETE_SLICE", 0):
            self.apply(cells=[dict(row=1, col=1, source="5")])
            self.assertIn(b1, self.book.unserved)
            self.assertEqual(self.value(1, 2), 15)
            self.assertEqual(self.book.store.result(b1), (True, 15))
            self.assertNotIn(b1, self.book.unserved)

    def test_snapshot_copy_with_roots_opens_fresh(self):
        self.apply(cells=[dict(row=1, col=1, source="2")]
                   + [dict(row=row, col=2, source=f"=A1+{row}") for row in range(1, 6)])
        self.book.read("Sheet1", dict(r1=1, c1=2, r2=5, c2=2))

        copy_path = Path(self.temp.name) / "snap.dhwb"
        with patch.object(engine, "DELETE_SLICE", 2):
            self.apply(cells=[dict(row=1, col=1, source="10")])
            self.assertEqual(len(self.book.store.roots()), 1)
            self.book.snapshot(copy_path)

        copy_book = Workbook(copy_path)
        self.addCleanup(copy_book.close)
        cells = copy_book.read("Sheet1", dict(r1=1, c1=2, r2=5, c2=2))["cells"]
        self.assertEqual([c["value"] for c in cells], [11, 12, 13, 14, 15])

    def test_structural_edit_records_no_root(self):
        self.apply(cells=[dict(row=1, col=1, source="2"), dict(row=1, col=2, source="=A1*3")])
        self.assertEqual(self.value(1, 2), 6)
        self.apply("rows_insert", sheet="Sheet1", start=1, count=1)
        self.assertEqual(self.book.store.roots(), [])
        self.assertEqual(self.book.store.formula_count(), 0)
        self.assertEqual(self.value(2, 2), 6)

    def test_direct_edit_recalculates_dependents_and_no_others(self):
        # Put D1 in a different block (row 65) from A1 (row 1) so block 0's direct edit does not touch D1's block.
        self.apply(cells=[
            dict(row=1, col=1, source="2"),
            dict(row=1, col=2, source="=A1*3"),
            dict(row=65, col=3, source="=B1+1"),
            dict(row=65, col=4, source="=7+8"),
        ])
        self.assertEqual(self.value(65, 3), 7)
        self.assertEqual(self.value(65, 4), 15)
        sheet = self.book.sheet("Sheet1")
        d65 = (sheet, 65, 4)

        real_eval = Calculator._evaluate_cell

        def fail_on_d65(calc_self, key):
            if key == d65:
                raise AssertionError("unrelated D65 was re-evaluated")
            return real_eval(calc_self, key)

        # Case 1: closed during direct edit.
        self.book.close()
        with closing(sqlite3.connect(self.path)) as direct, direct:
            direct.execute("UPDATE cells SET source='5' WHERE sheet_id=? AND row=1 AND col=1", (sheet,))
        self.book = Workbook(self.path)
        with patch.object(Calculator, "_evaluate_cell", fail_on_d65):
            self.assertEqual(self.value(1, 2), 15)
            self.assertEqual(self.value(65, 3), 16)
            self.assertEqual(self.value(65, 4), 15)

        # Case 2: open during direct edit.
        with closing(sqlite3.connect(self.path)) as direct, direct:
            direct.execute("UPDATE cells SET source='8' WHERE sheet_id=? AND row=1 AND col=1", (sheet,))
        with patch.object(Calculator, "_evaluate_cell", fail_on_d65):
            self.assertEqual(self.value(1, 2), 24)
            self.assertEqual(self.value(65, 3), 25)
            self.assertEqual(self.value(65, 4), 15)

    def test_renamed_sheet_then_edit_reaches_the_reader(self):
        self.apply("sheet_add", name="Draft")
        self.apply(sheet="Draft", cells=[dict(row=1, col=1, source="42")])
        self.apply(cells=[dict(row=1, col=1, source="=Budget!A1")])
        cell = self.book.read("Sheet1", dict(r1=1, r2=1, c1=1, c2=1))["cells"][0]
        self.assertEqual((cell["calculation"], cell["display"]), ("error", "#REF!"))

        self.book.close()
        with closing(sqlite3.connect(self.path)) as direct, direct:
            direct.execute("UPDATE sheets SET name='Budget' WHERE name='Draft'")
        self.book = Workbook(self.path)
        self.assertEqual(self.value(1, 1), 42)

        self.apply(sheet="Budget", cells=[dict(row=1, col=1, source="99")])
        self.assertEqual(self.value(1, 1), 99)


if __name__ == "__main__":
    unittest.main()
