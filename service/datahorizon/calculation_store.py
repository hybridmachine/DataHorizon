# SPDX-License-Identifier: GPL-3.0-only
"""Derived calculation tables, in either the workbook file or an attached temporary database."""

import re
import sqlite3

from .formula import FormulaError
from .temporal import Temporal

STORE_VERSION = 1
BLOCK_ROWS = 64

TABLES = (
    "dh_calc_meta",
    "dh_calc_sheets",
    "dh_calc_results",
    "dh_calc_precedents",
    "dh_calc_digests",
    "dh_calc_roots",
)

SCHEMA = """
CREATE TABLE {schema}.dh_calc_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 store_version INTEGER NOT NULL, calculation_version INTEGER, sheets_digest BLOB,
 generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE {schema}.dh_calc_sheets (number INTEGER PRIMARY KEY,
 sheet_id TEXT NOT NULL UNIQUE);
CREATE TABLE {schema}.dh_calc_results (sheet INTEGER NOT NULL,
 row INTEGER NOT NULL, col INTEGER NOT NULL,
 kind INTEGER NOT NULL, value, message TEXT,
 PRIMARY KEY(sheet,row,col)) WITHOUT ROWID;
CREATE TABLE {schema}.dh_calc_precedents (sheet INTEGER NOT NULL,
 row INTEGER NOT NULL, col INTEGER NOT NULL,
 p_sheet INTEGER NOT NULL, r1 INTEGER NOT NULL, c1 INTEGER NOT NULL,
 r2 INTEGER NOT NULL, c2 INTEGER NOT NULL,
 PRIMARY KEY(sheet,row,col,p_sheet,r1,c1,r2,c2)) WITHOUT ROWID;
CREATE TABLE {schema}.dh_calc_digests (sheet INTEGER NOT NULL,
 block INTEGER NOT NULL, digest BLOB NOT NULL,
 PRIMARY KEY(sheet,block)) WITHOUT ROWID;
CREATE TABLE {schema}.dh_calc_roots (sheet INTEGER NOT NULL,
 r1 INTEGER NOT NULL, c1 INTEGER NOT NULL, r2 INTEGER NOT NULL, c2 INTEGER NOT NULL,
 PRIMARY KEY(sheet,r1,c1,r2,c2)) WITHOUT ROWID;
"""

_SCHEMA_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1

# Value kind tags stored in dh_calc_results.kind.
_KIND_NONE = 0
_KIND_BOOL = 1
_KIND_INT = 2
_KIND_FLOAT = 3
_KIND_TEXT = 4
_KIND_DATE = 5
_KIND_TIME = 6
_KIND_DATETIME = 7
_KIND_ERROR = 8

_TEMPORAL_TO_KIND = {"date": _KIND_DATE, "time": _KIND_TIME, "datetime": _KIND_DATETIME}
_KIND_TO_TEMPORAL = {_KIND_DATE: "date", _KIND_TIME: "time", _KIND_DATETIME: "datetime"}


def _check_schema(schema: str) -> str:
    if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError(f"Invalid SQLite schema name: {schema!r}")
    return schema


def blocks(r1: int, r2: int) -> range:
    """Block numbers covering 1-based rows r1..r2 inclusive."""
    return range((r1 - 1) // BLOCK_ROWS, (r2 - 1) // BLOCK_ROWS + 1)


def _encode_result(value):
    if value is None:
        return _KIND_NONE, None, None
    if isinstance(value, FormulaError):
        return _KIND_ERROR, value.code, str(value)
    if type(value) is bool:
        return _KIND_BOOL, 1 if value else 0, None
    if type(value) is int:
        stored = value if _INT64_MIN <= value <= _INT64_MAX else str(value)
        return _KIND_INT, stored, None
    if type(value) is float:
        return _KIND_FLOAT, value, None
    if isinstance(value, Temporal):
        return _TEMPORAL_TO_KIND[value.kind], str(value), None
    if type(value) is str:
        return _KIND_TEXT, value, None
    raise TypeError(f"Unsupported calculation result type: {type(value).__name__}")


def _decode_result(kind, value, message):
    if kind == _KIND_NONE:
        return None
    if kind == _KIND_BOOL:
        return bool(value)
    if kind == _KIND_INT:
        return int(value)
    if kind == _KIND_FLOAT:
        return float(value)
    if kind == _KIND_TEXT:
        return value
    if kind in _KIND_TO_TEMPORAL:
        return Temporal(value, _KIND_TO_TEMPORAL[kind])
    if kind == _KIND_ERROR:
        return FormulaError(value, message or value)
    raise ValueError(f"Unknown stored result kind: {kind!r}")


class CalculationStore:
    """Access to the six derived calculation tables in one SQLite schema.

    No method begins or commits a transaction; the caller owns it.
    """

    def __init__(self, db: sqlite3.Connection, schema: str):
        self.db = db
        self.schema = _check_schema(schema)

    @staticmethod
    def create(db: sqlite3.Connection, schema: str) -> None:
        schema = _check_schema(schema)
        for statement in SCHEMA.format(schema=schema).split(";"):
            statement = statement.strip()
            if statement:
                db.execute(statement)
        db.execute(
            f"INSERT INTO {schema}.dh_calc_meta(singleton,store_version,calculation_version,sheets_digest,generation) "
            "VALUES(1,?,NULL,NULL,0)",
            (STORE_VERSION,),
        )

    def _sheet_map(self) -> dict[str, int]:
        return dict(self.db.execute(f"SELECT sheet_id, number FROM {self.schema}.dh_calc_sheets"))

    def _sheet_names(self) -> dict[int, str]:
        return dict(self.db.execute(f"SELECT number, sheet_id FROM {self.schema}.dh_calc_sheets"))

    def _ensure_sheet(self, sheets: dict[str, int], sheet_id: str) -> int:
        number = sheets.get(sheet_id)
        if number is None:
            cursor = self.db.execute(f"INSERT INTO {self.schema}.dh_calc_sheets(sheet_id) VALUES(?)", (sheet_id,))
            number = cursor.lastrowid
            sheets[sheet_id] = number
        return number

    def _sheet_number(self, sheet_id: str) -> int | None:
        row = self.db.execute(
            f"SELECT number FROM {self.schema}.dh_calc_sheets WHERE sheet_id=?",
            (sheet_id,),
        ).fetchone()
        return None if row is None else row[0]

    def result(self, key) -> tuple[bool, object]:
        sheet_id, row, col = key
        found = self.db.execute(
            f"SELECT kind, value, message FROM {self.schema}.dh_calc_results "
            f"WHERE sheet=(SELECT number FROM {self.schema}.dh_calc_sheets WHERE sheet_id=?) "
            "AND row=? AND col=?",
            (sheet_id, row, col),
        ).fetchone()
        if found is None:
            return False, None
        return True, _decode_result(*found)

    def results_in(self, rectangle) -> dict:
        sheet_id, r1, c1, r2, c2 = rectangle
        rows = self.db.execute(
            f"SELECT row, col, kind, value, message FROM {self.schema}.dh_calc_results "
            f"WHERE sheet=(SELECT number FROM {self.schema}.dh_calc_sheets WHERE sheet_id=?) "
            "AND row BETWEEN ? AND ? AND col BETWEEN ? AND ?",
            (sheet_id, r1, r2, c1, c2),
        )
        return {(sheet_id, row, col): _decode_result(kind, val, msg) for row, col, kind, val, msg in rows}

    def write(self, items) -> None:
        s = self.schema
        sheets = self._sheet_map()
        wrote = False
        upsert_result = (
            f"INSERT INTO {s}.dh_calc_results(sheet,row,col,kind,value,message) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(sheet,row,col) DO UPDATE SET kind=excluded.kind,value=excluded.value,message=excluded.message"
        )
        delete_prec = f"DELETE FROM {s}.dh_calc_precedents WHERE sheet=? AND row=? AND col=?"
        insert_prec = (
            f"INSERT OR IGNORE INTO {s}.dh_calc_precedents(sheet,row,col,p_sheet,r1,c1,r2,c2) "
            "VALUES(?,?,?,?,?,?,?,?)"
        )
        for key, value, rectangles in items:
            wrote = True
            sheet_id, row, col = key
            snum = self._ensure_sheet(sheets, sheet_id)
            kind, encoded_val, msg = _encode_result(value)
            self.db.execute(upsert_result, (snum, row, col, kind, encoded_val, msg))
            self.db.execute(delete_prec, (snum, row, col))
            prec_rows = [
                (snum, row, col, self._ensure_sheet(sheets, p_sheet), r1, c1, r2, c2)
                for p_sheet, r1, c1, r2, c2 in rectangles
            ]
            if prec_rows:
                self.db.executemany(insert_prec, prec_rows)
        if wrote:
            self.db.execute(
                f"INSERT INTO {s}.dh_calc_meta(singleton,store_version,calculation_version,sheets_digest,generation) "
                "VALUES(1,?,NULL,NULL,1) "
                "ON CONFLICT(singleton) DO UPDATE SET generation=generation+1",
                (STORE_VERSION,),
            )

    def delete_results(self, keys) -> None:
        sheets = self._sheet_map()
        params = [(sheets[sheet_id], row, col) for sheet_id, row, col in keys if sheet_id in sheets]
        if params:
            self.db.executemany(
                f"DELETE FROM {self.schema}.dh_calc_results WHERE sheet=? AND row=? AND col=?",
                params,
            )

    def delete_formula(self, key) -> None:
        sheet_id, row, col = key
        snum = self._sheet_number(sheet_id)
        if snum is None:
            return
        self.db.execute(
            f"DELETE FROM {self.schema}.dh_calc_results WHERE sheet=? AND row=? AND col=?",
            (snum, row, col),
        )
        self.db.execute(
            f"DELETE FROM {self.schema}.dh_calc_precedents WHERE sheet=? AND row=? AND col=?",
            (snum, row, col),
        )

    def formulas_in_rows(self, sheet_id: str, r1: int, r2: int) -> list:
        """All formula keys the store holds (results or precedents) in sheet_id rows r1..r2."""
        snum = self._sheet_number(sheet_id)
        if snum is None:
            return []
        keys = {
            (sheet_id, row, col)
            for row, col in self.db.execute(
                f"SELECT row, col FROM {self.schema}.dh_calc_results WHERE sheet=? AND row BETWEEN ? AND ?",
                (snum, r1, r2),
            )
        }
        keys.update(
            (sheet_id, row, col)
            for row, col in self.db.execute(
                f"SELECT DISTINCT row, col FROM {self.schema}.dh_calc_precedents WHERE sheet=? AND row BETWEEN ? AND ?",
                (snum, r1, r2),
            )
        )
        return sorted(keys)

    def precedents(self):
        names = self._sheet_names()
        rows = self.db.execute(
            f"SELECT sheet, row, col, p_sheet, r1, c1, r2, c2 FROM {self.schema}.dh_calc_precedents"
        )
        for snum, row, col, pnum, r1, c1, r2, c2 in rows:
            sheet_id = names.get(snum)
            p_sheet = names.get(pnum)
            if sheet_id is not None and p_sheet is not None:
                yield (sheet_id, row, col), (p_sheet, r1, c1, r2, c2)

    def formula_count(self) -> int:
        return self.db.execute(f"SELECT count(*) FROM {self.schema}.dh_calc_results").fetchone()[0]

    def digest(self, sheet_id: str, block: int) -> bytes | None:
        row = self.db.execute(
            f"SELECT digest FROM {self.schema}.dh_calc_digests "
            f"WHERE sheet=(SELECT number FROM {self.schema}.dh_calc_sheets WHERE sheet_id=?) AND block=?",
            (sheet_id, block),
        ).fetchone()
        return None if row is None else bytes(row[0])

    def digests(self):
        names = self._sheet_names()
        for snum, block, digest in self.db.execute(
            f"SELECT sheet, block, digest FROM {self.schema}.dh_calc_digests"
        ):
            sheet_id = names.get(snum)
            if sheet_id is not None:
                yield sheet_id, block, bytes(digest)

    def set_digests(self, items) -> None:
        s = self.schema
        sheets = self._sheet_map()
        upsert = (
            f"INSERT INTO {s}.dh_calc_digests(sheet,block,digest) VALUES(?,?,?) "
            "ON CONFLICT(sheet,block) DO UPDATE SET digest=excluded.digest"
        )
        delete = f"DELETE FROM {s}.dh_calc_digests WHERE sheet=? AND block=?"
        to_upsert = []
        to_delete = []
        for sheet_id, block, digest in items:
            if digest is None:
                snum = sheets.get(sheet_id)
                if snum is not None:
                    to_delete.append((snum, block))
            else:
                snum = self._ensure_sheet(sheets, sheet_id)
                to_upsert.append((snum, block, bytes(digest)))
        if to_delete:
            self.db.executemany(delete, to_delete)
        if to_upsert:
            self.db.executemany(upsert, to_upsert)

    def drop_digests(self, sheet_ids) -> None:
        sheets = self._sheet_map()
        params = [(sheets[sid],) for sid in set(sheet_ids) if sid in sheets]
        if params:
            self.db.executemany(f"DELETE FROM {self.schema}.dh_calc_digests WHERE sheet=?", params)

    def sheets_digest(self) -> bytes | None:
        row = self.db.execute(f"SELECT sheets_digest FROM {self.schema}.dh_calc_meta WHERE singleton=1").fetchone()
        if row is None or row[0] is None:
            return None
        return bytes(row[0])

    def set_sheets_digest(self, digest: bytes) -> None:
        self.db.execute(
            f"INSERT INTO {self.schema}.dh_calc_meta(singleton,store_version,calculation_version,sheets_digest,generation) "
            "VALUES(1,?,NULL,?,0) "
            "ON CONFLICT(singleton) DO UPDATE SET sheets_digest=excluded.sheets_digest",
            (STORE_VERSION, bytes(digest)),
        )

    def add_roots(self, rectangles) -> None:
        sheets = self._sheet_map()
        params = [
            (self._ensure_sheet(sheets, sheet_id), r1, c1, r2, c2)
            for sheet_id, r1, c1, r2, c2 in rectangles
        ]
        if params:
            self.db.executemany(
                f"INSERT OR IGNORE INTO {self.schema}.dh_calc_roots(sheet,r1,c1,r2,c2) VALUES(?,?,?,?,?)",
                params,
            )

    def roots(self) -> list:
        names = self._sheet_names()
        out = []
        for snum, r1, c1, r2, c2 in self.db.execute(
            f"SELECT sheet, r1, c1, r2, c2 FROM {self.schema}.dh_calc_roots"
        ):
            sheet_id = names.get(snum)
            if sheet_id is not None:
                out.append((sheet_id, r1, c1, r2, c2))
        return out

    def clear_roots(self) -> None:
        self.db.execute(f"DELETE FROM {self.schema}.dh_calc_roots")

    def clear_results(self) -> None:
        s = self.schema
        self.db.execute(f"DELETE FROM {s}.dh_calc_results")
        self.db.execute(f"DELETE FROM {s}.dh_calc_precedents")
        self.db.execute(f"DELETE FROM {s}.dh_calc_roots")

    def empty(self) -> None:
        s = self.schema
        self.db.execute(f"DELETE FROM {s}.dh_calc_results")
        self.db.execute(f"DELETE FROM {s}.dh_calc_precedents")
        self.db.execute(f"DELETE FROM {s}.dh_calc_digests")
        self.db.execute(f"DELETE FROM {s}.dh_calc_roots")
        self.db.execute(f"DELETE FROM {s}.dh_calc_sheets")
        self.db.execute(
            f"INSERT INTO {s}.dh_calc_meta(singleton,store_version,calculation_version,sheets_digest,generation) "
            "VALUES(1,?,NULL,NULL,0) "
            "ON CONFLICT(singleton) DO UPDATE SET sheets_digest=NULL",
            (STORE_VERSION,),
        )

    def calculation_version(self) -> int | None:
        row = self.db.execute(
            f"SELECT calculation_version FROM {self.schema}.dh_calc_meta WHERE singleton=1"
        ).fetchone()
        return None if row is None else row[0]

    def set_calculation_version(self, version: int) -> None:
        self.db.execute(
            f"INSERT INTO {self.schema}.dh_calc_meta(singleton,store_version,calculation_version,sheets_digest,generation) "
            "VALUES(1,?,?,NULL,0) "
            "ON CONFLICT(singleton) DO UPDATE SET calculation_version=excluded.calculation_version",
            (STORE_VERSION, version),
        )

    def generation(self) -> int:
        row = self.db.execute(f"SELECT generation FROM {self.schema}.dh_calc_meta WHERE singleton=1").fetchone()
        return 0 if row is None else row[0]
