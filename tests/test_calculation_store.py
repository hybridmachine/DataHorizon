# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the calculation store's derived tables in both file and temporary homes."""

from pathlib import Path
import sqlite3
import tempfile
import unittest

from datahorizon.calculation_store import BLOCK_ROWS, CalculationStore, SCHEMA, STORE_VERSION, TABLES, blocks
from datahorizon.formula import FormulaError
from datahorizon.temporal import Temporal, from_serial


class StoreContractMixin:
    def test_result_types_round_trip_exactly(self):
        values = [
            True, False, 1, 1.0, 0.5, -0.0, 0.1 + 0.2, 2**53 + 1, 2**80 + 7,
            "", "1", "TRUE", "a" * 40000, None,
            from_serial(46000, "date"),
            from_serial(46000.5, "datetime"),
            Temporal("12:34:56.789", "time"),
            FormulaError("#DIV/0!", "Division by zero"),
            FormulaError("#N/A", "Value is not available"),
            FormulaError("#REF!"),
        ]
        self.db.execute("BEGIN")
        self.store.write(((self.sheet, row, 1), value, ()) for row, value in enumerate(values, 1))
        self.db.execute("COMMIT")
        for row, value in enumerate(values, 1):
            found, back = self.store.result((self.sheet, row, 1))
            self.assertTrue(found)
            self.assertIs(type(back), type(value))
            if isinstance(value, FormulaError):
                self.assertEqual((back.code, str(back)), (value.code, str(value)))
            elif isinstance(value, Temporal):
                self.assertEqual((back.kind, str(back)), (value.kind, str(value)))
            else:
                self.assertEqual(repr(back), repr(value))

    def test_missing_key_returns_not_found(self):
        self.assertEqual(self.store.result((self.sheet, 99, 99)), (False, None))

    def test_write_replaces_earlier_precedents(self):
        key = (self.sheet, 1, 2)
        self.db.execute("BEGIN")
        self.store.write([(key, 10, [(self.sheet, 1, 1, 1, 1), (self.sheet, 2, 1, 5, 1)])])
        self.store.write([(key, 20, [(self.sheet, 3, 3, 3, 3)])])
        self.db.execute("COMMIT")
        self.assertEqual(self.store.result(key), (True, 20))
        self.assertEqual(list(self.store.precedents()), [(key, (self.sheet, 3, 3, 3, 3))])

    def test_delete_results_keeps_precedents_and_delete_formula_removes_both(self):
        k1 = (self.sheet, 1, 2)
        k2 = (self.sheet, 2, 2)
        r1 = (self.sheet, 1, 1, 1, 1)
        r2 = (self.sheet, 2, 1, 2, 1)
        self.db.execute("BEGIN")
        self.store.write([(k1, 1, [r1]), (k2, 2, [r2])])
        self.store.delete_results([k1])
        self.db.execute("COMMIT")
        self.assertEqual(self.store.result(k1), (False, None))
        self.assertEqual(self.store.result(k2), (True, 2))
        self.assertEqual(set(self.store.precedents()), {(k1, r1), (k2, r2)})
        self.assertEqual(self.store.formula_count(), 1)
        self.db.execute("BEGIN")
        self.store.delete_formula(k1)
        self.store.delete_formula(k2)
        self.db.execute("COMMIT")
        self.assertEqual(self.store.result(k2), (False, None))
        self.assertEqual(list(self.store.precedents()), [])
        self.assertEqual(self.store.formula_count(), 0)

    def test_results_in_returns_only_keys_inside_rectangle(self):
        other = "sheet-other"
        self.db.execute("BEGIN")
        self.store.write([
            ((self.sheet, 1, 1), 1, ()),
            ((self.sheet, 2, 2), 2, ()),
            ((self.sheet, 3, 3), 3, ()),
            ((self.sheet, 4, 2), 4, ()),
            ((other, 2, 2), 99, ()),
        ])
        self.db.execute("COMMIT")
        got = self.store.results_in((self.sheet, 2, 1, 3, 3))
        self.assertEqual(got, {(self.sheet, 2, 2): 2, (self.sheet, 3, 3): 3})

    def test_set_digests_with_none_deletes(self):
        d0 = b"\x01" * 32
        d1 = b"\x02" * 32
        self.db.execute("BEGIN")
        self.store.set_digests([(self.sheet, 0, d0), (self.sheet, 1, d1)])
        self.db.execute("COMMIT")
        self.assertEqual(self.store.digest(self.sheet, 0), d0)
        self.assertEqual(set(self.store.digests()), {(self.sheet, 0, d0), (self.sheet, 1, d1)})
        self.db.execute("BEGIN")
        self.store.set_digests([(self.sheet, 0, None)])
        self.db.execute("COMMIT")
        self.assertIsNone(self.store.digest(self.sheet, 0))
        self.assertEqual(list(self.store.digests()), [(self.sheet, 1, d1)])

    def test_drop_digests_removes_only_named_sheets(self):
        other = "sheet-other"
        d = b"\x03" * 32
        self.db.execute("BEGIN")
        self.store.set_digests([(self.sheet, 0, d), (other, 0, d)])
        self.store.drop_digests([self.sheet])
        self.db.execute("COMMIT")
        self.assertIsNone(self.store.digest(self.sheet, 0))
        self.assertEqual(self.store.digest(other, 0), d)

    def test_clear_results_keeps_digests_and_empty_removes_them(self):
        d = b"\x04" * 32
        sd = b"\x05" * 32
        self.db.execute("BEGIN")
        self.store.write([((self.sheet, 1, 2), 42, [(self.sheet, 1, 1, 1, 1)])])
        self.store.set_digests([(self.sheet, 0, d)])
        self.store.set_sheets_digest(sd)
        self.store.add_roots([(self.sheet, 1, 1, 1, 1)])
        self.store.set_calculation_version(7)
        self.store.clear_results()
        self.db.execute("COMMIT")
        self.assertEqual(self.store.formula_count(), 0)
        self.assertEqual(list(self.store.precedents()), [])
        self.assertEqual(self.store.roots(), [])
        self.assertEqual(self.store.digest(self.sheet, 0), d)
        self.assertEqual(self.store.sheets_digest(), sd)
        self.assertEqual(self.store.calculation_version(), 7)
        self.db.execute("BEGIN")
        self.store.empty()
        self.db.execute("COMMIT")
        self.assertEqual(list(self.store.digests()), [])
        self.assertIsNone(self.store.sheets_digest())
        self.assertEqual(self.store.calculation_version(), 7)

    def test_generation_rises_after_write(self):
        before = self.store.generation()
        self.db.execute("BEGIN")
        self.store.write([((self.sheet, 1, 1), 1, ())])
        self.db.execute("COMMIT")
        after = self.store.generation()
        self.assertGreater(after, before)

    def test_blocks_boundaries(self):
        self.assertEqual(BLOCK_ROWS, 64)
        self.assertEqual(blocks(1, 64), range(0, 1))
        self.assertEqual(blocks(64, 65), range(0, 2))
        self.assertEqual(blocks(65, 128), range(1, 2))

    def test_sheet_identifier_seen_twice_gets_one_number(self):
        self.db.execute("BEGIN")
        self.store.write([
            ((self.sheet, 1, 1), 1, [(self.sheet, 1, 2, 1, 2)]),
            ((self.sheet, 2, 1), 2, [(self.sheet, 2, 2, 2, 2)]),
        ])
        self.store.set_digests([(self.sheet, 0, b"\x06" * 32)])
        self.store.add_roots([(self.sheet, 1, 1, 1, 1)])
        self.db.execute("COMMIT")
        rows = self.db.execute(f"SELECT number, sheet_id FROM {self.store.schema}.dh_calc_sheets").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], self.sheet)

    def test_roots_and_metadata(self):
        self.assertIsNone(self.store.calculation_version())
        self.assertIsNone(self.store.sheets_digest())
        self.db.execute("BEGIN")
        self.store.set_calculation_version(3)
        self.store.set_sheets_digest(b"\x07" * 32)
        self.store.add_roots([(self.sheet, 1, 1, 64, 16384), (self.sheet, 1, 1, 64, 16384)])
        self.db.execute("COMMIT")
        self.assertEqual(self.store.calculation_version(), 3)
        self.assertEqual(self.store.sheets_digest(), b"\x07" * 32)
        self.assertEqual(self.store.roots(), [(self.sheet, 1, 1, 64, 16384)])
        self.db.execute("BEGIN")
        self.store.clear_roots()
        self.db.execute("COMMIT")
        self.assertEqual(self.store.roots(), [])

    def test_tables_and_constants_exist(self):
        self.assertEqual(STORE_VERSION, 1)
        self.assertEqual(len(TABLES), 6)
        self.assertIn("{schema}", SCHEMA)
        found = {
            row[0]
            for row in self.db.execute(f"SELECT name FROM {self.store.schema}.sqlite_schema WHERE type='table'")
        }
        self.assertTrue(set(TABLES) <= found)


class MainCalculationStoreTests(StoreContractMixin, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "main.sqlite"
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.execute("BEGIN")
        CalculationStore.create(self.db, "main")
        self.db.execute("COMMIT")
        self.store = CalculationStore(self.db, "main")
        self.sheet = "sheet-1"


class AttachedCalculationStoreTests(StoreContractMixin, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.main_path = Path(self.temp.name) / "main.sqlite"
        self.calc_path = Path(self.temp.name) / "calc.sqlite"
        self.db = sqlite3.connect(self.main_path, isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.execute("ATTACH DATABASE ? AS dhcalc", (str(self.calc_path),))
        self.db.execute("BEGIN")
        CalculationStore.create(self.db, "dhcalc")
        self.db.execute("COMMIT")
        self.store = CalculationStore(self.db, "dhcalc")
        self.sheet = "sheet-1"


if __name__ == "__main__":
    unittest.main()
