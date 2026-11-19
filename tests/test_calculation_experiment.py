# SPDX-License-Identifier: GPL-3.0-only
"""Small-input tests for the calculation stage 1 experiment (docs/design/calculation-stage-1-plan.md)."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from datahorizon.engine import Workbook

from calculation_fixture import create_fixture, expected_value
from calculation_experiment import HASHES, MODES, cell_hash, scan_digests, sheets_digest, updated_digest


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "calc.dhwb"

    def connect(self, path=None):
        db = sqlite3.connect(path or self.path)
        self.addCleanup(db.close)
        return db


class FixtureTests(Case):
    def test_shape_and_expected_values(self):
        ids = create_fixture(self.path, rows=200, prefix_rows=50)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM cells WHERE sheet_id=?", (ids["data"],)).fetchone()[0], 200*10+50)
            self.assertEqual(db.execute("SELECT count(*) FROM cells WHERE kind='formula'").fetchone()[0], 200+50+3)
            self.assertEqual(db.execute("SELECT source FROM cells WHERE sheet_id=? AND row=2 AND col=10", (ids["data"],)).fetchone()[0], "=J1+A2")
        self.assertEqual(expected_value(200, "Sheet1", 200, 10), sum(r % 97 for r in range(1, 201)))
        self.assertEqual(expected_value(200, "Summary", 3, 1), 0)

    def test_fixture_opens_and_engine_agrees_on_a_short_chain(self):
        create_fixture(self.path, rows=100)
        with closing(Workbook(self.path)) as book:
            cell = book.read("Sheet1", dict(r1=100, r2=100, c1=10, c2=10))["cells"][0]
        self.assertEqual(cell["value"], expected_value(100, "Sheet1", 100, 10))


class DigestTests(Case):
    def setUp(self):
        super().setUp()
        self.data = create_fixture(self.path, rows=300)["data"]
        self.db = self.connect()

    def digests(self, mode="sum", hash_name="blake2b16"):
        return scan_digests(self.db, 64, hash_name, mode)

    def test_field_boundaries_are_unambiguous(self):
        for name in HASHES:
            self.assertNotEqual(cell_hash(name, 1, 1, "ab", "c"), cell_hash(name, 1, 1, "a", "bc"))
            self.assertNotEqual(cell_hash(name, 1, 11, "x", "text"), cell_hash(name, 11, 1, "x", "text"))

    def test_each_kind_of_change_alters_only_its_block(self):
        for mode in MODES:
            before = self.digests(mode)
            for sql in ("UPDATE cells SET source='5' WHERE row=70 AND col=1",
                        "UPDATE cells SET kind='text' WHERE row=70 AND col=2",
                        "DELETE FROM cells WHERE row=70 AND col=3",
                        "INSERT INTO cells(sheet_id,row,col,source,kind) SELECT id,70,40,'new','text' FROM sheets WHERE name='Sheet1'"):
                with self.subTest(mode=mode, sql=sql):
                    self.db.execute(sql)
                    after = self.digests(mode)
                    self.assertEqual([k for k in before.keys() | after.keys() if before.get(k) != after.get(k)], [(self.data, 1)])
                    before = after
            self.db.rollback()

    def test_style_is_not_covered(self):
        before = self.digests()
        self.db.execute("""UPDATE cells SET style='{"bold":true}' WHERE row=5""")
        self.assertEqual(self.digests(), before)

    def test_sum_update_equals_rescan(self):
        for name in HASHES:
            before = self.digests(hash_name=name)
            old = self.db.execute("SELECT row,col,source,kind FROM cells WHERE row=70 AND col=1").fetchone()
            self.db.execute("UPDATE cells SET source='99' WHERE row=70 AND col=1")
            block = updated_digest(before[(self.data, 1)], name, old, (70, 1, "99", old[3]))
            self.assertEqual(block, self.digests(hash_name=name)[(self.data, 1)])
            self.db.execute("DELETE FROM cells WHERE row=70 AND col=1")
            self.assertEqual(updated_digest(block, name, (70, 1, "99", old[3]), None), self.digests(hash_name=name)[(self.data, 1)])
            self.db.rollback()

    def test_awkward_sources(self):
        for mode in MODES:
            for source in ("", "\u00e9" * 10, "a\x00b", "x" * 65536):
                with self.subTest(mode=mode, source=source[:8]):
                    self.db.execute("UPDATE cells SET source=? WHERE row=1 AND col=9", (source,))
                    first = self.digests(mode)
                    self.db.execute("UPDATE cells SET source=? WHERE row=1 AND col=9", (source + "!",))
                    self.assertNotEqual(self.digests(mode)[(self.data, 0)], first[(self.data, 0)])

    def test_empty_workbook_and_sheet_rename(self):
        empty = Path(self.temp.name) / "empty.dhwb"
        Workbook(empty, create=True).close()
        for mode in MODES:
            self.assertEqual(scan_digests(self.connect(empty), 64, "sha256", mode), {})
        blocks, sheets = self.digests(), sheets_digest(self.db, "sha256")
        self.db.execute("UPDATE sheets SET name='Renamed' WHERE name='Summary'")
        self.assertNotEqual(sheets_digest(self.db, "sha256"), sheets)
        self.assertEqual(self.digests(), blocks)


if __name__ == "__main__":
    unittest.main()
