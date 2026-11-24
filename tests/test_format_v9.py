# SPDX-License-Identifier: GPL-3.0-only
"""Native format 9: derived calculation tables, upgrade on first commit, and temporary stores."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from datahorizon.calculation_store import TABLES
from datahorizon.client import LocalService
from datahorizon.engine import EngineError, METADATA_SCHEMA, SCHEMA, Workbook
from datahorizon import relational


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mutate(book, **request):
    return book.mutate(dict(operation_id=str(uuid.uuid4()), expected_state=book.state(), **request))


def make_downgraded_v8(path: Path):
    """Build a valid v8 workbook by creating a v9 workbook, dropping the six derived tables, and setting v8."""
    book = Workbook(path, create=True)
    mutate(book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "42"}])
    book.close()
    with closing(sqlite3.connect(path)) as db:
        for table in TABLES:
            db.execute(f"DROP TABLE {table}")
        db.execute("UPDATE dh_meta SET format_version=8")
        db.commit()


def make_workbook_at_level(path: Path, level: int):
    """Build a valid native workbook at any format level 1..8."""
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA)
        if level >= 3:
            db.executescript(METADATA_SCHEMA)
        if level >= 4:
            db.executescript(relational.SCHEMA)
        sheet_id = str(uuid.uuid4())
        db.execute("INSERT INTO dh_meta VALUES(1,?,?,0)", (level, str(uuid.uuid4())))
        db.execute("INSERT INTO sheets VALUES(?,?,0)", (sheet_id, "Sheet1"))
        db.execute("INSERT INTO cells VALUES(?,1,1,'123','number','{}')", (sheet_id,))
        db.commit()


class FormatV9Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "v9.dhwb"

    def test_new_workbook_is_v9(self):
        book = Workbook(self.path, create=True)
        self.addCleanup(book.close)
        self.assertEqual(book.info()["format_version"], 9)
        self.assertIsNotNone(book.store)
        self.assertEqual(book.store.schema, "main")
        found = {
            row[0]
            for row in book.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        self.assertTrue(set(TABLES) <= found)

    def test_first_commit_upgrades_a_v8_workbook(self):
        make_downgraded_v8(self.path)
        book = Workbook(self.path)
        self.addCleanup(book.close)
        self.assertEqual(book.info()["format_version"], 8)
        self.assertEqual(book.store.schema, "dhcalc")
        temp_dir = Path(book._temp_store_dir)
        self.assertTrue(temp_dir.is_dir())
        mutate(book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 2, "source": "=A1*2"}])
        self.assertEqual(book.info()["format_version"], 9)
        self.assertEqual(book.store.schema, "main")
        self.assertFalse(temp_dir.exists())
        found = {
            row[0]
            for row in book.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        self.assertTrue(set(TABLES) <= found)

    def test_open_below_v9_and_read_only_do_not_write(self):
        v8_path = Path(self.temp.name) / "below_v9.dhwb"
        make_downgraded_v8(v8_path)
        before_v8 = sha256_file(v8_path)
        book = Workbook(v8_path)
        self.assertEqual(book.store.schema, "dhcalc")
        self.assertEqual(book.read("Sheet1", {"r1": 1, "c1": 1, "r2": 1, "c2": 1})["cells"][0]["value"], 42)
        book.close()
        self.assertEqual(sha256_file(v8_path), before_v8)

        v9_path = Path(self.temp.name) / "ro_v9.dhwb"
        writer = Workbook(v9_path, create=True)
        mutate(writer, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "7"}])
        writer.close()
        before_v9 = sha256_file(v9_path)
        ro = Workbook(v9_path, readonly=True)
        self.assertEqual(ro.store.schema, "dhcalc")
        self.assertEqual(ro.read("Sheet1", {"r1": 1, "c1": 1, "r2": 1, "c2": 1})["cells"][0]["value"], 7)
        ro.close()
        self.assertEqual(sha256_file(v9_path), before_v9)

    def test_failed_operation_does_not_upgrade(self):
        make_downgraded_v8(self.path)
        book = Workbook(self.path)
        self.addCleanup(book.close)
        with self.assertRaises(EngineError):
            mutate(book, action="edit", sheet="Sheet1", cells=[{"row": 1, "col": 2, "source": "not-a-number", "kind": "number"}])
        self.assertEqual(book.info()["format_version"], 8)
        self.assertEqual(book.store.schema, "dhcalc")

    def test_temporary_store_is_removed_on_close(self):
        make_downgraded_v8(self.path)
        book = Workbook(self.path)
        temp_dir = Path(book._temp_store_dir)
        self.assertTrue(temp_dir.is_dir())
        book.close()
        self.assertFalse(temp_dir.exists())

    def test_job_snapshot_has_no_store(self):
        book = Workbook(self.path, create=True)
        book.close()
        snap = Workbook(self.path, readonly=True, store=False)
        self.addCleanup(snap.close)
        self.assertIsNone(snap.store)
        self.assertIsNone(snap._temp_store_dir)

    def test_capabilities(self):
        with LocalService() as client:
            caps = client.request("/v1/capabilities")
            self.assertEqual(caps["format_version"], 9)
            self.assertIn(9, caps["supported_format_versions"])
            self.assertEqual(caps["new_file_format_version"], 9)

    def test_older_levels_still_open(self):
        for level in range(1, 9):
            level_path = Path(self.temp.name) / f"level_{level}.dhwb"
            make_workbook_at_level(level_path, level)
            book = Workbook(level_path)
            try:
                self.assertEqual(book.info()["format_version"], level)
                self.assertEqual(book.store.schema, "dhcalc")
                cell = book.read("Sheet1", {"r1": 1, "c1": 1, "r2": 1, "c2": 1})["cells"][0]
                self.assertEqual(cell["value"], 123)
            finally:
                book.close()

    def test_v10_is_rejected_on_open(self):
        book = Workbook(self.path, create=True)
        book.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dh_meta SET format_version=10")
            db.commit()
        with self.assertRaises(EngineError) as raised:
            Workbook(self.path)
        self.assertEqual(raised.exception.body["code"], "FORMAT_VERSION")


if __name__ == "__main__":
    unittest.main()
