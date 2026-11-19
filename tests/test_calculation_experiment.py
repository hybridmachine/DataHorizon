# SPDX-License-Identifier: GPL-3.0-only
"""Small-input tests for the calculation stage 1 experiment (docs/design/calculation-stage-1-plan.md)."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from datahorizon.engine import Workbook

from calculation_fixture import create_fixture, expected_value


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


if __name__ == "__main__":
    unittest.main()
