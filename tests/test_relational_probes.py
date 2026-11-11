# SPDX-License-Identifier: GPL-3.0-only
"""Executable SQLite contracts preceding the relational implementation."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest


class RelationalContractProbes(unittest.TestCase):
    def test_ddl_upgrade_and_rebuild_roll_back_with_parent_savepoint(self):
        with closing(sqlite3.connect(':memory:', isolation_level=None)) as db:
            db.execute('CREATE TABLE meta(version INTEGER)')
            db.execute('INSERT INTO meta VALUES(3)')
            db.execute('BEGIN IMMEDIATE')
            db.execute('SAVEPOINT operation_changes')
            db.execute('CREATE TABLE Customers(_dh_row_id INTEGER PRIMARY KEY AUTOINCREMENT, CustomerID TEXT UNIQUE NOT NULL)')
            db.execute("INSERT INTO Customers(CustomerID) VALUES('C001')")
            db.execute('ALTER TABLE Customers RENAME TO prior')
            db.execute('CREATE TABLE Customers(_dh_row_id INTEGER PRIMARY KEY AUTOINCREMENT, CustomerID TEXT UNIQUE NOT NULL)')
            db.execute('INSERT INTO Customers SELECT * FROM prior')
            db.execute('DROP TABLE prior')
            db.execute('UPDATE meta SET version=4')
            db.execute('ROLLBACK TO operation_changes')
            db.execute('RELEASE operation_changes')
            db.execute('COMMIT')
            self.assertEqual(db.execute('SELECT version FROM meta').fetchone()[0],3)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_schema WHERE name='Customers'").fetchone())

    def test_sqlite_generates_identity_without_callbacks_and_restore_keeps_identity(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE t(_dh_row_id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)')
            first=db.execute("INSERT INTO t(value) VALUES('a')").lastrowid
            db.execute('DELETE FROM t WHERE _dh_row_id=?',(first,))
            second=db.execute("INSERT INTO t(value) VALUES('b')").lastrowid
            self.assertGreater(second,first)
            db.execute('INSERT INTO t VALUES(?,?)',(first,'a'))
            self.assertEqual(db.execute('SELECT value FROM t WHERE _dh_row_id=?',(first,)).fetchone(),('a',))

    def test_native_required_unique_and_relationship_restrict(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('CREATE TABLE parent(id TEXT NOT NULL UNIQUE)')
            db.execute('CREATE TABLE child(id TEXT REFERENCES parent(id) ON DELETE RESTRICT ON UPDATE RESTRICT)')
            db.execute("INSERT INTO parent VALUES('C001')")
            db.execute("INSERT INTO child VALUES('C001')")
            db.execute('INSERT INTO child VALUES(NULL)')
            for sql in ("INSERT INTO parent VALUES(NULL)","INSERT INTO parent VALUES('C001')",
                        "INSERT INTO child VALUES('C999')","DELETE FROM parent",
                        "UPDATE parent SET id='C002'"):
                with self.assertRaises(sqlite3.IntegrityError):db.execute(sql)
            db.execute('CREATE TABLE nullable(value TEXT UNIQUE)')
            db.executemany('INSERT INTO nullable VALUES(?)',[(None,),(None,)])
            self.assertEqual(db.execute('SELECT count(*) FROM nullable').fetchone()[0],2)

    def test_readonly_authorizer_cte_dependencies_and_indirect_denials(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'query.sqlite'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('CREATE TABLE Customers(CustomerID TEXT); CREATE TABLE dh_meta(secret TEXT);')
                db.execute("INSERT INTO Customers VALUES('C001')");db.commit()
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                db.execute('PRAGMA query_only=ON')
                dependencies=set()
                def authorize(action,first,second,database,source):
                    if action==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
                    if action==sqlite3.SQLITE_READ and first=='Customers' and (database=='main' or (database is None and second=='')):
                        dependencies.add((first,second));return sqlite3.SQLITE_OK
                    if action==sqlite3.SQLITE_FUNCTION and second in ('count','sum','coalesce'):
                        return sqlite3.SQLITE_OK
                    return sqlite3.SQLITE_DENY
                db.set_authorizer(authorize)
                self.assertEqual(db.execute('WITH c AS (SELECT * FROM Customers) SELECT count(*) FROM c').fetchone(),(1,))
                self.assertIn(('Customers','CustomerID'),dependencies)
                self.assertEqual(db.execute('SELECT CustomerID FROM Customers WHERE CustomerID=:key',{'key':'C001'}).fetchone(),('C001',))
                for sql in ('DELETE FROM Customers','SELECT * FROM dh_meta',
                            'SELECT * FROM sqlite_schema','SELECT * FROM pragma_table_info(\'Customers\')',
                            "SELECT load_extension('x')",'PRAGMA user_version','BEGIN',
                            "ATTACH ':memory:' AS other",'WITH c AS (SELECT 1) DELETE FROM Customers'):
                    with self.assertRaises(sqlite3.DatabaseError):db.execute(sql)

    def test_backup_pins_read_snapshot_despite_independent_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'live.sqlite'
            with closing(sqlite3.connect(path,isolation_level=None)) as source:
                source.execute('PRAGMA journal_mode=WAL')
                source.execute('CREATE TABLE t(value)');source.execute('INSERT INTO t VALUES(1)')
                source.execute('BEGIN');self.assertEqual(source.execute('SELECT value FROM t').fetchone(),(1,))
                with closing(sqlite3.connect(path,isolation_level=None)) as writer:writer.execute('UPDATE t SET value=2')
                with closing(sqlite3.connect(':memory:')) as snapshot:
                    source.backup(snapshot)
                    self.assertEqual(snapshot.execute('SELECT value FROM t').fetchone(),(1,))
                source.execute('ROLLBACK')
                self.assertEqual(source.execute('SELECT value FROM t').fetchone(),(2,))
