# SPDX-License-Identifier: GPL-3.0-only
"""SQLite transaction owner. All managed clients enter through these operations."""

import csv
from contextlib import contextmanager
from functools import cmp_to_key
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .formula import Calculator, FormulaError, MAX_COLS, MAX_ROWS, Unmaintainable, address, has_sheet_reference, translate, move_references, transform_structure, rename_sheet_references, delete_sheet_references
from .hooks import HookFailure, invoke, supports_structure
from .formats import numeric_display, numeric_spec, temporal_display, TEMPORAL_FORMATS
from .borders import SIDES, BORDER_STYLES
from .temporal import Temporal, utc_clock, temporal_key, KINDS as TEMPORAL_KINDS
from .csv_types import temporal_schema
from .dependencies import DependencyIndex

MAX_CELLS = 10_000
MAX_HISTORY_BYTES = 16*1024*1024
MAX_REVISION = 2**63-1
MAX_SHEET_POSITION = 2**63-1
SNAPSHOT_SECONDS = 5
# workbook_import from a prepared stage: the service puts the stage file's path in this request field (a client
# cannot), the operation may exceed MAX_CELLS, and it runs under STAGE_SECONDS loading STAGE_BATCH cells at a time.
STAGE_PATH = "_stage_path"
STAGE_SECONDS = 300
STAGE_BATCH = 10_000
NUMBER_SOURCE = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
# Typed kinds whose value comes from the calculator; an unmaintained cell always yields its #REF! error.
EVALUATED = ("formula", "unmaintained")
SCHEMA = """
CREATE TABLE dh_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 format_version INTEGER NOT NULL, workbook_id TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE sheets (id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
 position INTEGER NOT NULL);
CREATE TABLE cells (sheet_id TEXT NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
 row INTEGER NOT NULL CHECK(row BETWEEN 1 AND 1048576),
 col INTEGER NOT NULL CHECK(col BETWEEN 1 AND 16384),
 source TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'auto',
 style TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(sheet_id,row,col)) WITHOUT ROWID;
CREATE TABLE operations (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
"""


METADATA_SCHEMA="""
CREATE TABLE dh_objects (namespace TEXT NOT NULL, id TEXT NOT NULL,
 sheet_id TEXT NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
 payload TEXT NOT NULL, PRIMARY KEY(namespace,sheet_id,id)) WITHOUT ROWID;
"""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class EngineError(Exception):
    def __init__(self, code, message, status=400, outcome="rejected", **details):
        super().__init__(message)
        self.status = status
        self.body = {"code": code, "message": message, "outcome": outcome, **details}


def require(condition, code, message, status=400):
    if not condition:
        raise EngineError(code, message, status)


_sql_guards = threading.local()


@contextmanager
def sql_deadline(db, deadline, *, cancel_event=None):
    connections=getattr(_sql_guards,'connections',None)
    if connections is None:connections=_sql_guards.connections={}
    stack=connections.setdefault(db,[])
    reason=None
    def check():
        nonlocal reason
        for bound,event,_ in stack:
            if event is not None and event.is_set():reason='CANCELLED';return 1
            if time.monotonic()>bound:reason='TIMEOUT';return 1
        reason=None;return 0
    def failure():
        return EngineError(reason,'Cancellation confirmed before commit' if reason=='CANCELLED' else 'Operation exceeded its time budget',409 if reason=='CANCELLED' else 400)
    stack.append((deadline,cancel_event,check))
    db.set_progress_handler(check,1000)
    try:
        if check():raise failure()
        yield
        if check():raise failure()
    except sqlite3.Error as error:
        if reason:
            raise failure() from error
        raise
    finally:
        stack.pop()
        if not stack:connections.pop(db,None)
        try:
            # Rollback and durable receipt recording remain possible on timeout.
            db.set_progress_handler(stack[-1][2] if stack else None,1000 if stack else 0)
        except sqlite3.ProgrammingError:
            # The connection owner may already have closed a rejected/replaced
            # file. Guard cleanup must not mask the original failure or leak it.
            pass


def check_integrity(db,deadline):
    with sql_deadline(db,deadline):
        require(db.execute("PRAGMA quick_check").fetchone()[0]=="ok", "CORRUPT", "SQLite integrity check failed; preserve the file for recovery",409)


def valid_uuid(value):
    if not isinstance(value,str):
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _reserve_destination(path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        created = os.fstat(fd)
        require(bool(created.st_ino),"FILE_IDENTITY_UNAVAILABLE","This filesystem cannot identify a reserved workbook destination; creation or copying is unavailable",409)
        return (created.st_dev,created.st_ino)
    finally:
        os.close(fd)


def _verify_destination(path, identity):
    if identity is None:
        return
    try:
        current = path.lstat()
    except FileNotFoundError as error:
        raise EngineError("FILE_CHANGED","The native workbook path was removed; reopen the intended file",409) from error
    require((current.st_dev,current.st_ino)==identity,"FILE_CHANGED","The native workbook path was replaced; reopen the intended file",409)


def integer(value, lower, upper, name):
    require(type(value) is int and lower <= value <= upper,
            "INVALID_ARGUMENT", f"{name} must be an integer from {lower} to {upper}")
    return value


def boolean(value, name):
    require(type(value) is bool, "INVALID_ARGUMENT", f"{name} must be boolean")
    return value


def rectangle(args, limit=MAX_CELLS):
    r1, c1 = integer(args.get("r1", 1), 1, MAX_ROWS, "r1"), integer(args.get("c1", 1), 1, MAX_COLS, "c1")
    r2, c2 = integer(args.get("r2", r1), r1, MAX_ROWS, "r2"), integer(args.get("c2", c1), c1, MAX_COLS, "c2")
    require((r2-r1+1)*(c2-c1+1) <= limit, "LIMIT", f"Range exceeds {limit} cells")
    return r1, c1, r2, c2


def validate_style(style):
    require(isinstance(style, dict), "INVALID_STYLE", "Style must be an object")
    allowed = {"format", "bold", "italic", "align", "background", "wrap", "border", "decimals", "grouping", "currency", "font_family", "font_size", "text_color", *("border_"+side for side in SIDES)}
    require(not set(style) - allowed, "UNSUPPORTED_STYLE", "Unsupported style property")
    for key, value in style.items():
        if key == "format":
            require(value in ("general", "text", "fixed2", "percent2", "number", "percent", "currency", *TEMPORAL_FORMATS), "UNSUPPORTED_STYLE", "Unsupported number format")
        elif key == "font_family":
            require(isinstance(value,str) and 1 <= len(value) <= 64 and value==value.strip() and re.fullmatch(r"[^\x00-\x1f\x7f,\\\"']+",value), "INVALID_STYLE", "Font family must be 1–64 characters without controls, quotes, comma or backslash")
        elif key == "font_size":
            require(type(value) is int and 6 <= value <= 72, "INVALID_STYLE", "Font size must be an integer from 6 to 72 points")
        elif key == "currency":
            require(isinstance(value,str) and re.fullmatch(r"[A-Z]{3}",value), "INVALID_STYLE", "Currency label must be three uppercase ASCII letters")
        elif key == "decimals":
            require(type(value) is int and 0 <= value <= 15, "INVALID_STYLE", "Decimal places must be an integer from 0 to 15")
        elif key == "align":
            require(value in ("left", "center", "right"), "INVALID_STYLE", "Invalid alignment")
        elif key in ("background","text_color"):
            require(isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value), "INVALID_STYLE", "Expected #RRGGBB")
        elif key.startswith("border_"):
            require(value in BORDER_STYLES, "INVALID_STYLE", "Unsupported border edge style")
        elif key == "border":
            require(value in ("none", "box"), "INVALID_STYLE", "Border must be none or box")
        else:
            require(type(value) is bool, "INVALID_STYLE", f"{key} must be boolean")
    return style


def typed(item):
    if item is None:
        return None
    source, kind, style = item["source"], item["kind"], item["style"]
    require(isinstance(source, str) and len(source.encode()) <= 65_536,
            "INVALID_INPUT", "Cell source must be text no larger than 64 KiB")
    require(kind in ("auto", "text", "number", "boolean", "null", "formula", "unmaintained", *TEMPORAL_KINDS), "INVALID_INPUT", "Unknown input kind")
    validate_style(style)
    value = source
    if kind in TEMPORAL_KINDS:
        try:
            value = Temporal(source, kind)
        except ValueError as error:
            raise EngineError("INVALID_INPUT", str(error)) from error
    elif kind == "null":
        value = None
    elif kind == "unmaintained":
        # Never evaluated: the calculator gives it a #REF! error (formula.UNMAINTAINED_MESSAGE).
        require(source.startswith("="), "INVALID_INPUT", "Formula source must start with =")
        value = None
    elif kind == "auto" and source == "":
        kind, value = "blank", None
    elif kind == "text":
        kind = "text"
    elif kind == "auto" and source.startswith("'"):
        kind, value = "text", source[1:]
    elif kind == "formula" or (kind == "auto" and source.startswith("=")):
        require(source.startswith("="), "INVALID_INPUT", "Formula source must start with =")
        kind, value = "formula", None
    elif kind == "boolean" or (kind == "auto" and source.upper() in ("TRUE", "FALSE")):
        require(source.upper() in ("TRUE", "FALSE"), "INVALID_INPUT", "Boolean source must be TRUE or FALSE")
        kind, value = "boolean", source.upper() == "TRUE"
    elif kind == "number" or (kind == "auto" and re.fullmatch(NUMBER_SOURCE, source)):
        require(re.fullmatch(NUMBER_SOURCE,source),"INVALID_INPUT","Numbers require invariant decimal notation without spaces or separators")
        try:
            value = float(source)
        except ValueError as error:
            raise EngineError("INVALID_INPUT", "Invalid number") from error
        require(math.isfinite(value), "INVALID_INPUT", "Numbers must be finite")
        if re.fullmatch(r"[+-]?\d+(?:\.0*)?(?:[eE][+-]?0+)?", source):
            require(abs(value) <= 9_007_199_254_740_991, "PRECISION", "Enter large integers as Text")
        kind = "number"
    else:
        kind, value = "text", source
    return {**item, "kind": kind, "value": value}


BLANK_INPUT = {"source": "", "kind": "auto", "style": {}}


def edited(item, edit, *, preserve_inputs=False, copied=False):
    """The input a cell edit leaves in a cell currently holding item (None when empty), or None to clear it."""
    for flag in ("clear", "clear_input", "replace_style"):
        boolean(edit.get(flag, False), flag)
    if edit.get("clear"):
        return None
    item = dict(item or BLANK_INPUT)
    if edit.get("clear_input"):
        item.update(source="", kind="auto")
    if "source" in edit:
        require(copied or edit.get("kind") != "unmaintained", "INVALID_INPUT",
                "Only structural edits mark a formula unmaintained; re-enter the formula with another kind")
        item.update(source=edit["source"], kind=edit.get("kind", "auto"))
    if "style" in edit:
        style=validate_style(edit["style"])
        if ("decimals" in style or "grouping" in style) and "format" not in style and not edit.get("replace_style"):
            if "decimals" not in style:
                current=numeric_spec(item["style"]);style={**style,"decimals":current[1] if current else 2}
            style={**style,"format":"percent" if item["style"].get("format") in ("percent","percent2") else "currency" if item["style"].get("format")=="currency" else "number"}
        previous=item["style"]
        if "border" in style: previous={key:value for key,value in previous.items() if key not in {"border_"+side for side in SIDES}}
        item["style"] = style if edit.get("replace_style") else validate_style({**previous, **style})
    # Text format affects subsequent entry, never retypes an existing
    # number/formula merely because its presentation changed.
    if not preserve_inputs and "source" in edit and item["source"] != "" and item["kind"] == "auto" and item["style"].get("format") == "text":
        item["kind"] = "text"
    elif not preserve_inputs and "source" in edit and item["kind"] == "auto" and isinstance(item["source"], str) and item["source"].startswith("="):
        item["kind"] = "formula"
    return item


def stored_edit(edit):
    """What the per-cell path stores when this edit is applied to an empty cell: the validated input, or None when
    nothing is stored. A bulk loader that stores these rows matches a cell-by-cell import exactly."""
    item = edited(None, edit)
    if item is not None:
        typed(item)
    return None if item is None or item == BLANK_INPUT else item


def display(value, style):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value,Temporal) or (type(value) in (int,float) and style.get("format") in TEMPORAL_FORMATS):
        return temporal_display(value,style)
    if isinstance(value, (float, int)):
        return numeric_display(value, style)
    return str(value)


def interchange_text(value):
    if type(value) is float:
        return str(int(value)) if value.is_integer() and abs(value) <= 9_007_199_254_740_991 else repr(value)
    return display(value, {})


def copied_formula(source, rows, cols):
    """Copy/fill/paste offset; with none, even text no transform can read copies as is."""
    return translate(source, rows, cols) if rows or cols else source


def unmaintained(source, style):
    """A formula a structural edit could not read, marked so it is never calculated against moved cells."""
    return {"source": source, "kind": "unmaintained", "style": json.loads(style)}


def unmaintained_report(marked):
    """Receipt fields for the (sheet name, row, col) locations a structural edit marked unmaintained."""
    if not marked:
        return {}
    return {"unmaintained": [{"sheet": name, "cell": address(row, col)} for name, row, col in marked[:100]],
            "unmaintained_count": len(marked)}


def input_signature(item):
    if item is None or (item["kind"] == "auto" and item["source"] == ""):
        return None
    return item["kind"], item["source"]


def schema_signature(db):
    return [(row[0], row[1], row[2]) for row in db.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name")]


def expected_schema(source=SCHEMA):
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(source)
        return schema_signature(db)
    finally:
        db.close()


EXPECTED_SCHEMA = expected_schema()
EXPECTED_SCHEMA3 = expected_schema(SCHEMA+METADATA_SCHEMA)


class Workbook:
    def __init__(self, path, create=False, readonly=False, handler=None, *, cancel_event=None, repair=False, approved_code=()):
        create, readonly = boolean(create, "create"), boolean(readonly, "readonly")
        from .workbook_code import approval_store
        self.approved_code=approval_store(approved_code)
        self.path = Path(path).expanduser().resolve()
        self.readonly, self.handler = readonly, handler
        self.repair=boolean(repair,'repair')
        self.relational_issues=[]
        self.pending_tables={};self.table_record_keys=set();self.table_field_changes=set()
        self.table_schema_keys=set();self.relational_before={}
        self.lock = threading.RLock()
        self.undo, self.redo = [], []
        self.dependencies=DependencyIndex()
        self.unverified_records=set()
        self._calculation_jobs=None
        self._operation_jobs=None
        self._query_jobs=None
        self.cancel_event=None
        self._read_cancel_event=cancel_event
        self.epoch = str(uuid.uuid4())
        created_identity = None
        if create:
            # Exclusive creation never truncates an existing workbook.
            created_identity = _reserve_destination(self.path)
        require(self.path.is_file(), "NOT_FOUND", "Workbook file does not exist", 404)
        current=self.path.stat()
        require(bool(current.st_ino),"FILE_IDENTITY_UNAVAILABLE","This filesystem cannot identify an open native workbook",409)
        self.file_identity=(current.st_dev,current.st_ino)
        self._opening_deadline=time.monotonic()+5
        self.db = None
        try:
            self.db = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                                      uri=True, isolation_level=None, check_same_thread=False, timeout=1)
            _verify_destination(self.path,created_identity)
            if create:
                # Keep common 1-KiB Text records inline in WITHOUT ROWID leaves.
                # At SQLite's 4-KiB default these spill to an overflow page per
                # cell, multiplying file size and native validation reads.
                self.db.execute("PRAGMA page_size=8192")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            if create:
                self.db.executescript(SCHEMA)
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("INSERT INTO dh_meta VALUES(1,1,?,0)", (str(uuid.uuid4()),))
                self.db.execute("INSERT INTO sheets VALUES(?,?,0)", (str(uuid.uuid4()), "Sheet1"))
                self.db.execute("COMMIT")
            self.verify_file()
            self.db.execute("BEGIN")
            try:
                self.version = self.data_version()
                self.verify_file()
                with sql_deadline(self.db,self._opening_deadline,cancel_event=cancel_event):
                    self.validate()
                    check_integrity(self.db,self._opening_deadline)
                    self.id = self.db.execute("SELECT workbook_id FROM dh_meta").fetchone()[0]
                    self.verify_file()
            finally:
                try: self.db.execute("ROLLBACK")
                except sqlite3.Error: pass
            del self._opening_deadline
            _verify_destination(self.path,created_identity)
        except Exception:
            if self.db is not None:
                self.db.close()
            # A pathname check followed by unlink can race with replacement by
            # another process. Preserve failed destinations for inspection.
            raise

    def verify_file(self):
        _verify_destination(self.path,self.file_identity)

    def close(self):
        if self._query_jobs is not None:self._query_jobs.close()
        if self._operation_jobs is not None:self._operation_jobs.close()
        if self._calculation_jobs is not None:self._calculation_jobs.close()
        with self.lock:self.db.close()

    def ensure_metadata(self):
        if self.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]<3:
            self.db.execute(METADATA_SCHEMA);self.db.execute('UPDATE dh_meta SET format_version=3')

    def calculation_jobs(self):
        if self._calculation_jobs is None:
            from .calculation_jobs import CalculationJobs
            self._calculation_jobs=CalculationJobs(self)
        return self._calculation_jobs

    def query_jobs(self):
        with self.lock:
            if self._query_jobs is None:
                from .query_jobs import QueryJobs
                self._query_jobs=QueryJobs(self)
            return self._query_jobs

    def operation_jobs(self):
        if self._operation_jobs is not None:return self._operation_jobs
        with self.lock:
            if self._operation_jobs is None:
                from .operation_jobs import OperationJobs
                self._operation_jobs=OperationJobs(self)
            return self._operation_jobs

    def check_operation(self):
        require(self.cancel_event is None or not self.cancel_event.is_set(),"CANCELLED","Cancellation confirmed before commit",409)
        require(time.monotonic()<=self.deadline,"TIMEOUT","Operation exceeded its time budget")

    def validate(self, db=None, *, deadline=None):
        db = self.db if db is None else db
        deadline = getattr(self,"_opening_deadline",time.monotonic()+5) if deadline is None else deadline
        with sql_deadline(db,deadline,cancel_event=self._read_cancel_event):
            signature=schema_signature(db)
            reserved=[row for row in signature if row[1] in {entry[1] for entry in EXPECTED_SCHEMA3}]
            require(signature in (EXPECTED_SCHEMA,EXPECTED_SCHEMA3) or reserved==EXPECTED_SCHEMA3,
                    "SCHEMA_CHANGED", "Unsupported schema. Reopen a compatible native workbook; no repair was attempted", 409)
            meta = db.execute("SELECT format_version,revision,workbook_id FROM dh_meta WHERE singleton=1").fetchall()
            require(len(meta) == 1 and meta[0][0] in (1,2,3,4,5,6,7) and type(meta[0][1]) is int and meta[0][1] >= 0,
                    "FORMAT_VERSION", "Unsupported or invalid native format", 409)
            if meta[0][0]>=4:
                from . import relational
                registered=relational.validate_schema(db,signature,SCHEMA+METADATA_SCHEMA)
                self.relational_issues=relational.validate_data(db,registered,deadline,repair=self.repair)
            else:
                require(signature == (EXPECTED_SCHEMA3 if meta[0][0]==3 else EXPECTED_SCHEMA),"FORMAT_VERSION","Native schema/version mismatch",409)
            if meta[0][0]>=3:
                from .metadata import validate
                record_count=record_cells=0
                for count,(namespace,identifier,payload) in enumerate(db.execute("SELECT namespace,id,substr(payload,1,1048577) FROM dh_objects"),1):
                    require(count<=256 and time.monotonic()<=deadline,"LIMIT","Native metadata exceeds validation bounds")
                    value=json.loads(payload)
                    if namespace in ("presentation","list"):validate(namespace,identifier,value)
                    else:
                        require(namespace=="records","INVALID_FORMAT","Unknown native metadata")
                        from .records import validate as validate_records
                        validate_records(value)
                        from .metadata import name
                        name(identifier);record_count+=1;spec=value["spec"];record_cells+=(spec["r2"]-spec["r1"]+1)*(spec["c2"]-spec["c1"]+1)
                require(record_count<=16 and record_cells<=10000,"LIMIT","Declared record scopes exceed native bounds")
            require(valid_uuid(meta[0][2]) and (not hasattr(self,"id") or meta[0][2] == self.id),
                    "INVALID_FORMAT","Invalid or changed workbook identity",409)
            positions = set()
            for sheet_id,name,position in db.execute("SELECT id,name,position FROM sheets"):
                require(time.monotonic()<=deadline,"TIMEOUT","Workbook validation exceeded its time budget")
                require(valid_uuid(sheet_id) and type(position) is int and position>=0 and position not in positions,
                        "INVALID_FORMAT","Invalid sheet identity or display position",409)
                try:
                    self.check_name(name)
                except EngineError as error:
                    raise EngineError("INVALID_FORMAT","Invalid native sheet name",409) from error
                positions.add(position)
            require(bool(positions),"INVALID_FORMAT","Workbook must contain a sheet",409)
            # Inspect stored cells once. Three independent scans of the native
            # WITHOUT ROWID table repeatedly fetched long-text overflow pages
            # even when no temporal inputs or invalid rows were present.
            rows=db.execute("""SELECT CASE WHEN kind IN ('date','time','datetime','unmaintained') THEN source END,kind,
                typeof(row)='integer' AND typeof(col)='integer'
                    AND row BETWEEN 1 AND ? AND col BETWEEN 1 AND ? AS coordinates_valid,
                sheet_id IN (SELECT id FROM sheets) AS sheet_valid
                FROM cells
                WHERE kind IN ('date','time','datetime','unmaintained')
                    OR typeof(row)<>'integer' OR typeof(col)<>'integer'
                    OR row NOT BETWEEN 1 AND ? OR col NOT BETWEEN 1 AND ?
                    OR sheet_id NOT IN (SELECT id FROM sheets)""",
                (MAX_ROWS,MAX_COLS,MAX_ROWS,MAX_COLS))
            for source,kind,coordinates_valid,sheet_valid in rows:
                require(time.monotonic()<=deadline,"TIMEOUT","Workbook validation exceeded its time budget")
                require(coordinates_valid,"INVALID_FORMAT","Native cells require integer coordinates within the supported grid",409)
                require(sheet_valid,"INVALID_FORMAT","Native workbook contains orphaned cell references",409)
                if kind=="unmaintained":
                    require(meta[0][0]>=7,"INVALID_FORMAT","Unmaintained formulas require native format 7",409)
                else:
                    require(meta[0][0]>=2,"FORMAT_VERSION","Temporal inputs require native format 2",409)
                typed({"source":source,"kind":kind,"style":{}})
            if meta[0][0]>=3:
                require(db.execute("PRAGMA foreign_key_check(dh_objects)").fetchone() is None,
                        "INVALID_FORMAT","Native metadata contains orphaned sheet references",409)

    def data_version(self):
        return self.db.execute("PRAGMA data_version").fetchone()[0]

    def refresh(self, *, deadline=None):
        self.verify_file()
        current = self.data_version()
        if current != self.version:
            self.validate(deadline=deadline)
            self.version, self.epoch = current, str(uuid.uuid4())
            self.undo.clear()
            self.redo.clear()
            self.dependencies.clear()
            if self.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]>=3:
                self.unverified_records.update((sheet,identifier) for sheet,identifier in self.db.execute("SELECT sheet_id,id FROM dh_objects WHERE namespace='records'"))

    def state(self):
        revision = self.db.execute("SELECT revision FROM dh_meta").fetchone()[0]
        return f"{self.epoch}:{revision}"

    def info(self):
        with self.lock:
            self.refresh()
            from .relational import tables
            from .query_views import summaries
            return {"id": self.id, "path": str(self.path), "state": self.state(), "readonly": self.readonly,
                    "tables":[dict(id=item['id'],name=item['name']) for item in tables(self.db)],"queries":summaries(self.db),
                    "repair":self.repair,"relational_issues":self.relational_issues,
                    "sheets": [{"id": s, "name": n} for s, n in self.db.execute("SELECT id,name FROM sheets ORDER BY position,id")],
                    "undo": self.undo[-1][0] if self.undo else None, "redo": self.redo[-1][0] if self.redo else None,
                    "storage": "stored", "calculation": "evaluated_on_read", "format_version": self.db.execute("SELECT format_version FROM dh_meta").fetchone()[0]}

    def sheet(self, sheet):
        require(isinstance(sheet,str) and bool(sheet), "INVALID_ARGUMENT", "Sheet must be a nonempty name or ID")
        row = self.db.execute("SELECT id FROM sheets WHERE id=? OR name=? COLLATE NOCASE", (sheet, sheet)).fetchone()
        require(row is not None, "SHEET_NOT_FOUND", "Sheet does not exist", 404)
        return row[0]

    def raw(self, sheet, row, col):
        data = self.db.execute("SELECT source,kind,style FROM cells WHERE sheet_id=? AND row=? AND col=?", (sheet, row, col)).fetchone()
        if data is None:
            return None
        try:
            return {"source": data[0], "kind": data[1], "style": json.loads(data[2])}
        except (ValueError, TypeError) as error:
            raise EngineError("INVALID_INPUT", "Invalid externally stored cell metadata") from error

    def calculator(self, seconds=0.25, *, clock=None, transactional=False):
        def resolve(name):
            try:
                return self.sheet(name)
            except EngineError as error:
                raise FormulaError("#REF!", "Referenced sheet does not exist") from error
        read=lambda s,r,c: typed(self.raw(s,r,c))
        return Calculator(read,resolve,seconds,clock=clock) if transactional else self.dependencies.calculator(read,resolve,seconds,clock)

    def range_cells(self, sheet, rows, c1, c2, deadline):
        for row in rows:
            require(time.monotonic()<=deadline,"TIMEOUT","Range query exceeded its time budget")
            # Row equality lets the primary key bound the column scan as well.
            yield from self.db.execute(
                "SELECT row,col,source,kind,style FROM cells WHERE sheet_id=? AND row=? AND col BETWEEN ? AND ? ORDER BY col",
                (sheet,row,c1,c2))

    def read(self, sheet, args, *, limit=4096, native=False):
        require(args.get('calculation','fresh') in ('fresh','cached'),'INVALID_ARGUMENT','calculation must be fresh or cached')
        preview=args.get("preview",False)
        require(type(preview) is bool, "INVALID_ARGUMENT", "preview must be boolean")
        if "rows" in args:
            selected = args["rows"]
            require(isinstance(selected, list) and 0 < len(selected) <= 256, "LIMIT", "Provide 1-256 source rows")
            for row in selected:
                integer(row, 1, MAX_ROWS, "source row")
            c1 = integer(args.get("c1",1),1,MAX_COLS,"c1")
            c2 = integer(args.get("c2",c1),c1,MAX_COLS,"c2")
            require(c2 >= c1 and len(selected)*(c2-c1+1) <= limit, "LIMIT", f"Read exceeds {limit} cells")
            r1, r2 = min(selected), max(selected)
            rows = sorted(set(selected))
        else:
            r1, c1, r2, c2 = rectangle(args, limit)
            rows = range(r1,r2+1)
        with self.lock:
            deadline = time.monotonic()+2
            self.refresh(deadline=deadline)
            self.db.execute("BEGIN")
            try:
                # Refresh again after establishing the read snapshot.
                self.refresh(deadline=deadline)
                with sql_deadline(self.db,deadline,cancel_event=self._read_cancel_event):
                    sheet, calc = self.sheet(sheet), self.calculator()
                    cells = []
                    response = {"cells":cells, "state":self.state(), "sheet":sheet, "range":[r1,c1,r2,c2]}
                    if native: response.update(self.native_catalog())
                    response_bytes = len(encoded(response).encode())
                    for row, col, source, kind, style in self.range_cells(sheet,rows,c1,c2,deadline):
                        require(time.monotonic()<=deadline,"TIMEOUT","Read exceeded its time budget")
                        item = typed({"source": source, "kind": kind, "style": json.loads(style)})
                        result = {"row": row, "col": col, "source": source, "input_kind": kind, "kind": item["kind"], "style": item["style"]}
                        try:
                            if item['kind']=='formula' and args.get('calculation')=='cached' and (sheet,row,col) not in self.dependencies.values:
                                result.update(value=None,display='…',calculation='pending')
                            else:
                                value = calc.cell(sheet, row, col) if item["kind"] in EVALUATED else item["value"]
                                result.update(value=value, display=display(value, item["style"]), calculation="current")
                                if isinstance(value,Temporal): result["value_kind"] = value.kind
                        except FormulaError as error:
                            result.update(value=None, display=error.code, error=str(error), calculation="error")
                        if preview:
                            if len(source)>128:
                                result.update(source=source[:128], source_omitted=True)
                            if isinstance(result["value"],str):
                                result.update(value=None, value_omitted=True)
                            for field in ("display","error"):
                                if field in result and len(result[field])>128:
                                    result[field]=result[field][:128]+"…"
                        response_bytes += len(encoded(result).encode()) + int(bool(cells))
                        require(response_bytes <= 4 * 1024 * 1024, "READ_LIMIT", "Read response exceeds 4 MiB; request a smaller range")
                        cells.append(result)
                    return response
            finally:
                self.db.execute("ROLLBACK")

    def read_styles(self, sheet, args):
        r1,c1,r2,c2=rectangle(args)
        expected=args.get("expected_state")
        require(isinstance(expected,str) and bool(expected),"INVALID_ARGUMENT","Style reads require expected_state")
        with self.lock:
            deadline=time.monotonic()+2;self.refresh(deadline=deadline);self.db.execute("BEGIN")
            try:
                self.refresh(deadline=deadline)
                require(expected==self.state(),"STALE_STATE","Workbook changed. Copy formatting again",409)
                sheet=self.sheet(sheet);rows=[];response={"state":self.state(),"sheet":sheet,"range":[r1,c1,r2,c2],"rows":rows}
                size=len(encoded(response).encode())
                with sql_deadline(self.db,deadline):
                    for row in range(r1,r2+1):
                        require(time.monotonic()<=deadline,"TIMEOUT","Style read exceeded two seconds")
                        # Select only style metadata: large inputs and broken formulas are irrelevant.
                        stored={col:validate_style(json.loads(style)) for col,style in self.db.execute("SELECT col,style FROM cells WHERE sheet_id=? AND row=? AND col BETWEEN ? AND ? ORDER BY col",(sheet,row,c1,c2))}
                        values=[stored.get(col,{}) for col in range(c1,c2+1)]
                        size+=len(encoded(values).encode())+int(bool(rows));require(size<=4*1024*1024,"READ_LIMIT","Style response exceeds 4 MiB; copy a smaller range")
                        rows.append(values)
                require(time.monotonic()<=deadline,"TIMEOUT","Style read exceeded two seconds")
                return response
            finally:self.db.execute("ROLLBACK")

    def navigate(self, sheet, args):
        row=integer(args.get("row"),1,MAX_ROWS,"row");col=integer(args.get("col"),1,MAX_COLS,"col")
        direction=args.get("direction")
        require(direction in ("up","down","left","right"),"INVALID_ARGUMENT","Direction must be up, down, left or right")
        expected=args.get("expected_state")
        require(isinstance(expected,str) and bool(expected),"INVALID_ARGUMENT","Navigation requires the reviewed expected_state")
        vertical=direction in ("up","down");step=-1 if direction in ("up","left") else 1
        moving,fixed=("row","col") if vertical else ("col","row")
        position=row if vertical else col;maximum=MAX_ROWS if vertical else MAX_COLS
        with self.lock:
            deadline=time.monotonic()+2;self.refresh(deadline=deadline);self.db.execute("BEGIN")
            try:
                self.refresh(deadline=deadline)
                require(expected==self.state(),"STALE_STATE","Workbook changed. Refresh before navigating",409)
                with sql_deadline(self.db,deadline):
                    identity=self.sheet(sheet);input_bytes=0
                    def occupied(item):
                        nonlocal input_bytes
                        if item is None:return False
                        input_bytes+=len(encoded(item).encode())
                        require(input_bytes<=4*1024*1024,"READ_LIMIT","Navigation inputs exceed 4 MiB; use a nearer address")
                        return typed(item)["kind"] not in ("blank","null")
                    current=occupied(self.raw(identity,row,col));target=position;mode=None
                    if 1<=position+step<=maximum:
                        operator,order=(">","ASC") if step>0 else ("<","DESC")
                        records=self.db.execute(f"SELECT row,col,source,kind,style FROM cells WHERE sheet_id=? AND {fixed}=? AND {moving}{operator}? ORDER BY {moving} {order} LIMIT ?",
                            (identity,col if vertical else row,position,MAX_CELLS+1))
                        for count,(r,c,source,kind,style) in enumerate(records,1):
                            require(time.monotonic()<=deadline,"TIMEOUT","Navigation exceeded its time budget")
                            require(count<=MAX_CELLS,"NAVIGATION_LIMIT","Boundary scan exceeds 10,000 stored cells; use a nearer address")
                            value=r if vertical else c
                            present=occupied({"source":source,"kind":kind,"style":json.loads(style)})
                            if mode is None:mode="block" if current and value==position+step and present else "seek"
                            if mode=="block":
                                if value!=target+step or not present:break
                                target=value
                            elif present:target=value;break
                        else:
                            if mode!="block":target=maximum if step>0 else 1
                    return {"state":self.state(),"sheet":identity,"row":target if vertical else row,"col":col if vertical else target,"basis":"stored_inputs"}
            finally:self.db.execute("ROLLBACK")

    def summary(self, sheet, args):
        """Stored selection, one read snapshot, no hooks/history/cache writes."""
        require("rows" not in args,"INVALID_ARGUMENT","Summary requires one rectangle, not projected rows")
        r1,c1,r2,c2=rectangle(args)
        with self.lock:
            deadline=time.monotonic()+2
            self.refresh(deadline=deadline)
            self.db.execute("BEGIN")
            try:
                self.refresh(deadline=deadline)
                with sql_deadline(self.db,deadline):
                    sheet=self.sheet(sheet);calc=self.calculator(seconds=max(0,deadline-time.monotonic()))
                    count=numeric_count=error_count=source_bytes=0
                    total=0.0;numeric_error=None;errors=[]
                    for row,col,source,kind,style in self.range_cells(sheet,range(r1,r2+1),c1,c2,deadline):
                        require(time.monotonic()<=deadline,"TIMEOUT","Summary exceeded its time budget")
                        source_bytes+=len(source.encode())+len(style.encode())
                        require(source_bytes<=4*1024*1024,"READ_LIMIT","Summary inputs exceed 4 MiB; select a smaller rectangle")
                        item=typed({"source":source,"kind":kind,"style":json.loads(style)})
                        try:
                            value=calc.cell(sheet,row,col) if item["kind"] in EVALUATED else item["value"]
                        except FormulaError as error:
                            count+=1;error_count+=1
                            if len(errors)<100:errors.append({"row":row,"col":col,"code":error.code})
                            continue
                        if value is not None:count+=1
                        if type(value) in (int,float):
                            numeric_count+=1
                            if numeric_error is None:
                                total+=value
                                if not math.isfinite(total):numeric_error="#NUM!"
                    available=not error_count and numeric_error is None
                    return {"state":self.state(),"sheet":sheet,"range":[r1,c1,r2,c2],
                            "selected_cells":(r2-r1+1)*(c2-c1+1),"count":count,"numeric_count":numeric_count,
                            "sum":total if available else None,
                            "average":total/numeric_count if available and numeric_count else None,
                            "error_count":error_count,"errors":errors,"errors_truncated":error_count>len(errors),"numeric_error":numeric_error}
            finally:
                self.db.execute("ROLLBACK")

    def native_catalog(self, *, omissions=False):
        """Names and binding labels (and optionally other native objects) in the current read snapshot, for export reports."""
        from .relational import objects
        from .bindings import label as binding_label
        found=list(objects(self.db))
        result={'native_bindings':[binding_label(self.db,item) for kind,item in found if kind=='binding'],
                'native_names':[item['name'] for kind,item in found if kind=='name']}
        if omissions:result['native_omissions']=[item['name'] for kind,item in found if kind not in ('name','binding')]
        return result

    def read_workbook(self):
        """One bounded read transaction, including every sheet and formula dependency."""
        from .interchange import validate_sheet_names
        with self.lock:
            deadline=time.monotonic()+5
            self.refresh(deadline=deadline)
            self.db.execute("BEGIN")
            try:
                self.refresh(deadline=deadline)
                with sql_deadline(self.db,deadline):
                    catalog=list(self.db.execute("SELECT id,name FROM sheets ORDER BY position,id LIMIT 65"))
                    validate_sheet_names([name for _,name in catalog])
                    rows=self.db.execute("SELECT sheet_id,row,col,source,kind,style FROM cells ORDER BY sheet_id,row,col LIMIT ?",(MAX_CELLS+1,))
                    result={"state":self.state(),"sheets":[{"name":name,"cells":[]} for _,name in catalog]}
                    result.update(self.native_catalog(omissions=True))
                    targets={key:result["sheets"][index]["cells"] for index,(key,_) in enumerate(catalog)}
                    calc=self.calculator(seconds=2); size=0
                    for count,(sheet,row,col,source,kind,style) in enumerate(rows,1):
                        require(count<=MAX_CELLS,"LIMIT","Workbook export exceeds 10,000 stored cells")
                        require(time.monotonic()<=deadline,"TIMEOUT","Workbook export exceeded its time budget")
                        item=typed({"source":source,"kind":kind,"style":json.loads(style)})
                        cell={"row":row,"col":col,"source":source,"kind":item["kind"],"style":item["style"]}
                        try:
                            value=calc.cell(sheet,row,col) if item["kind"] in EVALUATED else item["value"]
                            cell.update(value=value,calculation="current")
                            if isinstance(value,Temporal):cell["value_kind"]=value.kind
                        except FormulaError as error:
                            cell.update(value=None,calculation="error",error=str(error))
                        if item["kind"]=="formula":
                            for identity,name in catalog:
                                if identity in cell["source"]:
                                    try: cell["source"]=rename_sheet_references(cell["source"],identity,name)
                                    except FormulaError as error:
                                        raise EngineError("UNSUPPORTED_FORMULA",f"Cannot export formula at {sheet}!{address(row,col)}: {error}") from error
                        size+=len(encoded(cell).encode())
                        require(size<=4*1024*1024,"READ_LIMIT","Workbook snapshot exceeds 4 MiB")
                        targets[sheet].append(cell)
                    return result
            finally:
                self.db.execute("ROLLBACK")

    def plan_import(self,request):
        """Validate a workbook_import request before any change. Reviewed sheets come inline (at most 10,000 cells) or
        from a prepared stage, which is opened in one read snapshot and must still be what was reviewed: its content
        digest, and for a local file the source's recorded size and modification time."""
        from .interchange import validate_sheet_names
        target=request.get("target","replace_empty")
        require(target in ("replace_empty","add_sheets"),"INVALID_ARGUMENT","target must be replace_empty or add_sheets")
        if "stage_id" not in request:
            require("stage_digest" not in request,"INVALID_ARGUMENT","stage_digest applies only with stage_id")
            sheets=request.get("sheets")
            require(isinstance(sheets,list) and all(isinstance(s,dict) for s in sheets),"INVALID_ARGUMENT","sheets must be an array of sheet objects")
            validate_sheet_names([s.get("name") for s in sheets])
            require(all(isinstance(s.get("cells"),list) for s in sheets),"INVALID_ARGUMENT","Each sheet needs cells")
            require(sum(len(s["cells"]) for s in sheets)<=MAX_CELLS,"LIMIT","Workbook import exceeds 10,000 stored cells")
            return {"target":target,"sheets":sheets,"stage":None,"names":[s["name"] for s in sheets],"counts":[len(s["cells"]) for s in sheets]}
        require("sheets" not in request,"INVALID_ARGUMENT","Supply sheets or stage_id, not both")
        require(isinstance(request["stage_id"],str) and bool(request["stage_id"]),"INVALID_ARGUMENT","stage_id must be text")
        digest=request.get("stage_digest")
        require(isinstance(digest,str) and re.fullmatch(r"[0-9a-f]{64}",digest) is not None,"INVALID_ARGUMENT","stage_digest must be the reviewed SHA-256 digest")
        path=request.get(STAGE_PATH)
        require(isinstance(path,str),"JOB_NOT_FOUND","Import stage is unavailable; prepare the workbook again",404)
        from .import_stage import open_readonly
        from .xlsx_stream import LARGE_SHEETS
        stage=open_readonly(path)
        try:
            # Held until the last cell is loaded, so the cells loaded are the cells verified.
            stage.db.execute("BEGIN")
            with sql_deadline(stage.db,self.deadline,cancel_event=self.cancel_event):
                summary=stage.summary()
                # The recomputed digest must match both the review and the digest the stage was sealed with.
                require(stage.digest()==digest==summary.get("stage_digest"),"STALE_PLAN","The import stage is not the reviewed one; prepare the workbook again",409)
                source=summary.get("source")
                if isinstance(source,dict) and source.get("kind")=="path":
                    try: info=os.stat(source["path"])
                    except (OSError,KeyError,TypeError,ValueError) as error:
                        raise EngineError("STALE_PLAN","The source file is no longer available; prepare it again",409) from error
                    require((info.st_size,info.st_mtime_ns)==(source.get("size"),source.get("mtime_ns")),"STALE_PLAN",
                            "The source file changed after it was reviewed; prepare it again",409)
                names=stage.sheets(); counts=stage.sheet_counts(); temporal=stage.has_temporal()
            validate_sheet_names(names,limit=LARGE_SHEETS)
        except BaseException:
            stage.close()
            raise
        return {"target":target,"sheets":None,"stage":stage,"names":names,"counts":counts,"temporal":temporal}

    @staticmethod
    def import_arguments(plan):
        """The structure event's description of an import: its target and each sheet's name and cell count."""
        return {"target":plan["target"],"sheets":[{"name":name,"cells":count} for name,count in zip(plan["names"],plan["counts"])]}

    def import_workbook(self,plan):
        """Load a planned import in the open operation: replace an empty workbook's sheets, or add sheets after the existing ones."""
        from .interchange import fold_name
        names=plan["names"]
        if plan["target"]=="replace_empty":
            from .relational import objects
            present={kind for kind,_ in objects(self.db)}
            if present:
                labels=[label for kind,label in (("table","tables"),("query","saved queries"),("relationship","relationships"),("index","indexes"),
                    ("name","names"),("module","Python modules"),("binding","event bindings")) if kind in present]
                labels+=sorted(kind for kind in present if kind not in ("table","query","relationship","index","name","module","binding"))
                listed=labels[0] if len(labels)==1 else ", ".join(labels[:-1])+" or "+labels[-1]
                raise EngineError("DESTINATION_NOT_EMPTY",f"Whole-workbook import requires a destination without {listed}; create a new workbook")
            require(self.db.execute("SELECT 1 FROM cells LIMIT 1").fetchone() is None,"DESTINATION_NOT_EMPTY","Whole-workbook import requires an empty destination; create a new workbook")
            self.db.execute("DELETE FROM sheets")
            start=0
        else:
            existing={fold_name(name) for (name,) in self.db.execute("SELECT name FROM sheets")}
            conflicts=[name for name in names if fold_name(name) in existing]
            if conflicts:
                raise EngineError("SHEET_NAME_CONFLICT","Imported sheet names already exist in this workbook; rename them and import again",409,conflicts=conflicts)
            start=self.next_sheet_position()
            require(len(names)-1<=MAX_SHEET_POSITION-start,"POSITION_LIMIT","Reorder sheets before adding more; display positions are exhausted",409)
        identities=[str(uuid.uuid4()) for _ in names]
        if plan["stage"] is None:
            for index,(identity,sheet) in enumerate(zip(identities,plan["sheets"])):
                self.db.execute("INSERT INTO sheets VALUES(?,?,?)",(identity,sheet["name"],start+index))
                if sheet["cells"]: self.apply_edit(identity,sheet["cells"])
            # Imported cells are new structure, not edits: no change events, as for a staged import.
            fresh=set(identities)
            self.pending={key:change for key,change in self.pending.items() if key[0] not in fresh}
            marks=",".join("?"*len(identities))
            cells=self.db.execute(f"SELECT count(*) FROM cells WHERE sheet_id IN ({marks})",identities).fetchone()[0]
        else:
            cells=self.load_stage(plan["stage"],identities,names,start)
            if plan["temporal"]:self.db.execute("UPDATE dh_meta SET format_version=2 WHERE format_version=1")
        return {"imported":{"sheets":len(names),"cells":cells}}

    def load_stage(self,stage,identities,names,start):
        """Insert every staged cell straight into cells, in batches, without per-cell change images; the stage was
        validated when it was written. Deadline and cancellation are checked between batches and inside SQLite."""
        loaded=0;unmaintained=False
        with sql_deadline(self.db,self.deadline,cancel_event=self.cancel_event),sql_deadline(stage.db,self.deadline,cancel_event=self.cancel_event):
            for position,(identity,name) in enumerate(zip(identities,names)):
                self.db.execute("INSERT INTO sheets VALUES(?,?,?)",(identity,name,start+position))
                for rows in stage.iter_cells(position,batch=STAGE_BATCH):
                    self.check_operation()
                    self.db.executemany("INSERT INTO cells VALUES(?,?,?,?,?,?)",[(identity,*row) for row in rows])
                    loaded+=len(rows)
                    unmaintained=unmaintained or any(row[3]=="unmaintained" for row in rows)
            self.check_operation()
        if unmaintained:
            from .relational import ensure_version
            ensure_version(self,7)
        self.bulk_loaded=loaded
        return loaded

    def put(self, sheet, row, col, item):
        integer(row, 1, MAX_ROWS, "row")
        integer(col, 1, MAX_COLS, "col")
        if item is not None:
            typed(item)
            if item["kind"] in TEMPORAL_KINDS:
                self.db.execute("UPDATE dh_meta SET format_version=2 WHERE format_version=1")
            elif item["kind"] == "unmaintained" and self.db.execute("SELECT format_version FROM dh_meta").fetchone()[0] < 7:
                from .relational import ensure_version
                ensure_version(self,7)
            if item == BLANK_INPUT:
                item = None
        key = (sheet, row, col)
        before = self.raw(*key)
        self.check_operation()
        if before == item:
            return
        require(len(self.changes)-len(self.table_record_keys)+len(self.table_field_changes)+(key not in self.changes)<=MAX_CELLS,
                'LIMIT','Operation including generated edits exceeds 10,000 scalar fields')
        if key not in self.changes:
            require(len(self.changes) < MAX_CELLS, "LIMIT", "Operation including Python edits exceeds 10,000 changed cells")
            self.changes[key] = [before, item]
        else:
            self.changes[key][1] = item
        self.change_bytes += len(encoded([before, item]).encode())
        require(self.change_bytes <= 8 * 1024 * 1024, "LIMIT", "Operation before/after history exceeds 8 MiB")
        if item is None:
            self.db.execute("DELETE FROM cells WHERE sheet_id=? AND row=? AND col=?", key)
        else:
            self.db.execute("INSERT INTO cells VALUES(?,?,?,?,?,?) ON CONFLICT(sheet_id,row,col) DO UPDATE SET source=excluded.source,kind=excluded.kind,style=excluded.style",
                            (*key, item["source"], item["kind"], encoded(item["style"])))
        if key in self.pending or input_signature(before) != input_signature(item):
            original = self.pending.get(key, {}).get("before", before)
            if input_signature(original) == input_signature(item):
                self.pending.pop(key, None)
            else:
                self.pending[key] = {"sheet": sheet, "row": row, "col": col, "before": original, "after": item}

    def structural_put(self, key, item, origin=None):
        """A write made by a structural rewrite. Bindings skip it unless its content came from an earlier edit in this operation."""
        self.put(*key, item)
        self.structural_marks[key] = (origin or key) not in self.structure_touched

    def apply_edit(self, sheet, edits, *, preserve_inputs=False, copied=False):
        """copied: the edits paste cells read from the workbook, which may carry kind unmaintained."""
        require(isinstance(edits, list) and 0 < len(edits) <= MAX_CELLS, "LIMIT", "Provide 1 to 10,000 edits")
        for edit in edits:
            require(isinstance(edit, dict), "INVALID_ARGUMENT", "Each cell edit must be an object")
            row, col = edit.get("row"), edit.get("col")
            integer(row, 1, MAX_ROWS, "row")
            integer(col, 1, MAX_COLS, "col")
            self.put(sheet, row, col, edited(self.raw(sheet, row, col), edit, preserve_inputs=preserve_inputs, copied=copied))

    def hook_dispatch(self,method,args):
        if method.startswith('table_'):
            from .relational_rows import dispatch
            return dispatch(self,method,args)
        from .references import resolve
        if method=='values':
            sheet,r1,c1,r2,c2=resolve(self,args['ref'],single=False);sheet=self.sheet(sheet)
            calculator=self.calculator(clock=self.operation_clock,transactional=True)
            return [[calculator.cell(sheet,row,col) for col in range(c1,c2+1)] for row in range(r1,r2+1)]
        if 'ref' in args:
            sheet,row,col,_,_=resolve(self,args['ref'],single=True);sheet=self.sheet(sheet)
        else:
            sheet=self.sheet(args['sheet']);row,col=args['row'],args['col'];integer(row,1,MAX_ROWS,'row');integer(col,1,MAX_COLS,'col')
        if method=='get':return self.raw(sheet,row,col)
        if method=='value':return self.calculator(clock=self.operation_clock,transactional=True).cell(sheet,row,col)
        require(method=='set','INVALID_HOOK','Unknown handler context method')
        source=args['source']
        require(type(source) in (str,int,float,bool) and (type(source) is not float or math.isfinite(source)),'CONTEXT_TYPE','set accepts text, a finite number or a boolean')
        if type(source) is bool:source='TRUE' if source else 'FALSE'
        elif type(source) is int:source=str(source)
        elif type(source) is float:source=interchange_text(source)
        self.apply_edit(sheet,[dict(row=row,col=col,source=source,kind=args.get('kind','auto'))])

    def structure_hook(self,operation,request,phase,before,arguments=None):
        if not self.handler:return
        require(supports_structure(self.handler),'UNSUPPORTED_HOOK_SCOPE','Required handler must explicitly define top-level on_structure(context,event)')
        catalog=[dict(id=i,name=n,position=p) for i,n,p in self.db.execute('SELECT id,name,position FROM sheets ORDER BY position,id LIMIT 257')]
        require(len(catalog)<=256,'LIMIT','Structural events support at most 256 sheets')
        event=dict(phase=phase,action=request['action'],arguments=arguments if arguments is not None else {k:request[k] for k in ('sheet','name','start','count','order') if k in request},before=before if before is not None else catalog,after=catalog if phase=='after' else None)
        invoke(self.handler,event,operation,self.hook_dispatch,self.deadline,event='on_structure')
        return catalog

    def run_hooks(self, operation):
        from .bindings import admit,plan,skip,stored
        if not self.handler and not stored(self):
            return
        admission={}
        for round_index in range(16):
            if not self.pending and not self.pending_tables:
                return
            batch = [{**self.pending[key],"action":self.operation_action} for key in sorted(self.pending)]
            table_batch=[{**self.pending_tables[key],"operation_id":operation,"origin":"managed","action":self.operation_action,"round":round_index+1} for key in sorted(self.pending_tables,key=lambda key:(key[1],int(key[2])))]
            self.pending = {}
            self.pending_tables={}

            if self.handler:
                if batch:invoke(self.handler, batch, operation, self.hook_dispatch, self.deadline)
                if table_batch:
                    from .hooks import supports_event
                    require(supports_event(self.handler,'on_table_change'),'UNSUPPORTED_HOOK_SCOPE','Required handler must explicitly define on_table_change(context,changes)')
                    require(len(encoded(table_batch).encode())<=8*1024*1024,'LIMIT','Table hook change images exceed 8 MiB')
                    invoke(self.handler,table_batch,operation,self.hook_dispatch,self.deadline,event='on_table_change')
            # A handler may rewrite a structural record (same key, new content); only unchanged ones stay quiet.
            quiet=self.structural_records if round_index==0 else {}
            events=[change for change in batch if (key:=(change["sheet"],change["row"],change["col"])) not in quiet or quiet[key]!=change["after"]]
            calls=admit(self,plan(self,events,table_batch),admission)
            if calls:
                from .macro_runtime import invoke_bindings
                skip(self,admission,invoke_bindings(calls,operation,self.hook_dispatch,self.deadline))
        require(not self.pending and not self.pending_tables, "HOOK_RECURSION", "Required handler or bindings exceeded 16 change rounds")

    def operation(self, operation_id):
        with self.lock:
            try: self.verify_file()
            except EngineError as error:
                error.body.update(outcome="unknown",operation_id=operation_id)
                raise
            row = self.db.execute("SELECT result FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise EngineError("OUTCOME_UNKNOWN", "No durable receipt. Do not assume rollback or repeat the operation",
                                  404,"unknown",operation_id=operation_id)
            return json.loads(row[0])

    def recorded_operation(self, operation, fingerprint):
        existing = self.db.execute("SELECT fingerprint,result FROM operations WHERE id=?", (operation,)).fetchone()
        if existing is None:
            return None
        require(existing[0] == fingerprint, "OPERATION_ID_REUSED", "Operation ID was used with different arguments", 409)
        receipt = json.loads(existing[1])
        if receipt["outcome"] != "committed":
            error = EngineError(receipt["code"],receipt["message"],receipt.get("http_status",409),receipt["outcome"])
            error.body=receipt
            raise error
        return receipt

    def mutate(self, request, *, cancel_event=None, on_commit=None):
        """on_commit, if given, is called with no arguments once this call itself has committed the operation;
        it is not called when a stored receipt is returned for a retried operation ID."""
        require(isinstance(request, dict), "INVALID_ARGUMENT", "Operation must be an object")
        operation = request.get("operation_id")
        require(isinstance(operation, str) and len(operation) <= 100 and bool(operation), "OPERATION_ID", "An operation_id is required")
        try:
            operation.encode("utf-8")
        except UnicodeEncodeError as error:
            raise EngineError("OPERATION_ID","operation_id must be valid Unicode text") from error
        # The service-only stage path is where a stage happens to be, not part of the request's identity.
        fingerprint = hashlib.sha256(encoded({key:value for key,value in request.items() if key!=STAGE_PATH}).encode()).hexdigest()
        staged = request.get("action")=="workbook_import" and "stage_id" in request
        with self.lock:
            self.cancel_event=cancel_event
            try:
                self.verify_file()
                receipt = self.recorded_operation(operation, fingerprint)
                if receipt is not None:
                    return receipt
                # A released stage can only be answered from a stored receipt, as just tried.
                require(not staged or isinstance(request.get(STAGE_PATH),str),"JOB_NOT_FOUND","Import stage is unavailable; prepare the workbook again",404)
                require(not self.readonly, "READ_ONLY", "Workbook is open read-only", 403)
                self.deadline = time.monotonic() + (STAGE_SECONDS if staged else 5)
                self.bulk_loaded = 0
                self.refresh(deadline=self.deadline)
                self.changes, self.pending = {}, {}
                self.pending_tables={};self.table_record_keys=set();self.table_field_changes=set()
                self.table_schema_keys=set();self.relational_before={}
                self.macro_diagnostics=None;self.binding_notices=[]
                self.structure_touched=set();self.structural_marks={};self.structural_records={}
                self.operation_clock=utc_clock()
                unverified_before=set(self.unverified_records)
                self.change_bytes = 0
                self.db.execute("BEGIN IMMEDIATE")
                # A different service may have committed this ID while we waited
                # for the writer lock. Replay outside new-operation rollback handling.
                receipt = self.recorded_operation(operation, fingerprint)
                if receipt is not None:
                    self.db.execute("ROLLBACK")
                    return receipt
            except Exception as error:
                if self.db.in_transaction:
                    try: self.db.execute("ROLLBACK")
                    except sqlite3.Error: pass
                if isinstance(error, EngineError):
                    if error.body.get("operation_id") != operation:
                        # A request refusal is not proof that another request
                        # using this ID cannot still commit.
                        error.body.update(outcome="unknown", operation_id=operation)
                    raise
                raise EngineError("OUTCOME_UNKNOWN", f"Operation admission was not confirmed: {error}. Reconcile the operation ID",
                                  409 if isinstance(error,(OSError,sqlite3.Error)) else 500,
                                  "unknown",operation_id=operation) from error
            in_transaction = True
            import_plan = None
            try:
                self.db.execute("SAVEPOINT operation_changes")
                self.check_operation()
                # The writer lock closes the check/write race with independent tools.
                self.refresh(deadline=self.deadline)
                require(request.get("expected_state") == self.state(), "STALE_STATE", "Workbook changed. Refresh and review the edit before retrying", 409)
                require(self.db.execute("SELECT revision FROM dh_meta").fetchone()[0]<MAX_REVISION,
                        "REVISION_LIMIT","Workbook revision limit reached; edits are unavailable without a format migration",409)
                try:
                    json.dumps(request,ensure_ascii=False,allow_nan=False).encode("utf-8")
                except UnicodeEncodeError as error:
                    raise EngineError("INVALID_ARGUMENT","Request text must be valid Unicode") from error
                require("label" not in request or isinstance(request["label"],str),"INVALID_ARGUMENT","Operation label must be text")
                kind = request.get("action")
                require(isinstance(kind,str),'INVALID_ARGUMENT','Operation action must be Text')
                self.operation_action=kind
                require(not self.repair or kind=='table_repair','REPAIR_SCOPE','Repair handles admit only reviewed orphan repairs',409)
                require(not self.relational_issues or kind=='table_repair' and self.repair,'DATA_REPAIR_REQUIRED','Use the explicit restricted repair interface before ordinary mutations',409)
                from . import relational
                sheet_structural = isinstance(kind, str) and (kind.startswith("sheet_") or kind == "workbook_import" or kind in ("rows_insert", "rows_delete", "columns_insert", "columns_delete"))
                structural=sheet_structural or kind in relational.STRUCTURAL or kind in ("module_save","module_delete","name_save","name_delete","binding_save","binding_delete")
                from . import records
                record_context=records.capture(self)
                self.relational_before={item['id']:item for item in relational.tables(self.db)}
                import_plan=self.plan_import(request) if kind=="workbook_import" else None
                event_arguments=self.import_arguments(import_plan) if import_plan else None
                before_catalog=self.structure_hook(operation,request,'before',None,event_arguments) if sheet_structural else None
                # Cells edited so far (by a before-structure handler) are not structural content when moved.
                self.structure_touched=set(self.changes)
                reference_report = {}
                sheet = self.sheet(request.get("sheet", "Sheet1")) if kind in ("edit", "paste", "move", "fill", "replace_all") else None
                if kind=="macro_run":
                    from .macros import run as run_macro
                    reference_report=run_macro(self,request)
                elif kind in ("module_save","module_delete"):
                    from .workbook_code import mutate as mutate_code
                    reference_report=mutate_code(self,request)
                elif kind in ("binding_save","binding_delete"):
                    from .bindings import mutate as mutate_binding
                    reference_report=mutate_binding(self,request)
                elif kind in ("name_save","name_delete"):
                    from .names import mutate as mutate_name
                    reference_report=mutate_name(self,request)
                elif kind in relational.ACTIONS:
                    reference_report=relational.mutate(self,request)
                elif kind=='list_edit':
                    from .record_operations import edit
                    edit(self,request)
                elif kind in ('list_declare','list_reconcile','list_forget'):
                    records.mutate(self,request)
                elif kind in ('metadata_set','metadata_delete'):
                    from .metadata import validate,name
                    identity=self.sheet(request.get('sheet','Sheet1'));namespace=request.get('namespace');identifier=name(request.get('id'))
                    require(namespace in ('presentation','list'),'INVALID_METADATA','Unsupported metadata namespace')
                    if kind=='metadata_set':payload=validate(namespace,identifier,request.get('payload'))
                    self.ensure_metadata()
                    if kind=='metadata_set':
                        self.db.execute('INSERT INTO dh_objects VALUES(?,?,?,?) ON CONFLICT(namespace,sheet_id,id) DO UPDATE SET payload=excluded.payload',(namespace,identifier,identity,encoded(payload)))
                        require(self.db.execute('SELECT count(*)<=256 AND sum(length(payload))<=16777216 FROM dh_objects').fetchone()[0],'LIMIT','Native metadata exceeds 256 objects/16 MiB')
                    else:self.db.execute('DELETE FROM dh_objects WHERE namespace=? AND sheet_id=? AND id=?',(namespace,identity,identifier))
                elif kind == "edit":
                    self.apply_edit(sheet, request.get("cells"))
                elif kind == "paste":
                    rows = request.get("rows")
                    require(isinstance(rows, list) and len(rows) > 0 and all(isinstance(row, list) for row in rows), "INVALID_ARGUMENT", "Paste rows must be a nonempty matrix")
                    require(sum(len(row) for row in rows) <= MAX_CELLS, "LIMIT", "Paste exceeds 10,000 cells")
                    start_row = integer(request.get("row"), 1, MAX_ROWS, "row")
                    start_col = integer(request.get("col"), 1, MAX_COLS, "col")
                    origin_row=integer(request.get("origin_row",start_row),1,MAX_ROWS,"origin_row")
                    origin_col=integer(request.get("origin_col",start_col),1,MAX_COLS,"origin_col")
                    mode = request.get("mode", "all")
                    record_projection=boolean(request.get("record_projection",False),"record_projection")
                    require(mode in ("all", "values", "formats"), "INVALID_ARGUMENT", "Unknown paste mode")
                    edits = []
                    for dr, values in enumerate(rows):
                        for dc, value in enumerate(values):
                            require(isinstance(value, dict), "INVALID_ARGUMENT", "Paste cells must be objects")
                            row, col = start_row + dr, start_col + dc
                            edit = {"row": row, "col": col}
                            require(mode != "all" or not value.get("source_omitted"), "INCOMPLETE_CLIPBOARD", "Use a full clipboard read, not a viewport source preview")
                            require(mode != "values" or not value.get("value_omitted"), "INCOMPLETE_CLIPBOARD", "Use calculated clipboard values, not viewport previews")
                            if not value or value.get("kind") == "blank":
                                if mode == "all":
                                    if not value:
                                        edits.append({**edit, "clear": True})
                                        continue
                                    edit["clear_input"] = True
                                elif mode == "values":
                                    edits.append({**edit, "clear_input": True})
                                    continue
                            if mode != "formats":
                                source = value.get("source", "")
                                if mode == "values":
                                    require("error" not in value and value.get("calculation") not in ("error", "pending"),
                                            "CALCULATION_ERROR", "Values-only paste cannot discard a formula error or pending result")
                                    require(value.get("kind") != "formula" or "value" in value,
                                            "CALCULATION_ERROR", "Formula clipboard cells require a calculated value")
                                    stored = value.get("value", source)
                                    require(isinstance(stored,(str,int,float,bool,type(None))),"INVALID_ARGUMENT","Clipboard values must be scalar")
                                    source = "" if stored is None else ("TRUE" if stored is True else "FALSE" if stored is False else str(stored))
                                    input_kind = value.get("value_kind") if value.get("value_kind") in TEMPORAL_KINDS else "null" if stored is None else "boolean" if type(stored) is bool else "number" if isinstance(stored, (int, float)) else "text"
                                else:
                                    require(isinstance(source,str),"INVALID_ARGUMENT","Clipboard source must be text")
                                    input_kind = value.get("input_kind", "text")
                                    if input_kind in ("auto", "formula") and source.startswith("="):
                                        source = copied_formula(source, row-integer(value.get("row"),1,MAX_ROWS,"source row"), col-integer(value.get("col"),1,MAX_COLS,"source column")) if record_projection else copied_formula(source, start_row-origin_row, start_col-origin_col)
                                if not edit.get("clear_input"):
                                    edit.update(source=source, kind=input_kind)
                            if mode != "values":
                                edit["style"] = value.get("style", {})
                                edit["replace_style"] = True
                            edits.append(edit)
                    self.apply_edit(sheet, edits, preserve_inputs=mode=="all", copied=True)
                elif kind == "fill":
                    r1, c1, r2, c2 = rectangle(request)
                    source = self.raw(sheet, r1, c1)
                    mode = request.get("mode", "copy")
                    require(mode in ("copy", "series", "down", "right"), "INVALID_ARGUMENT", "Fill mode must be copy, series, down or right")
                    # Snapshot independent seeds before writing any destination.
                    seeds = {}
                    seed_bytes = 0
                    for row, col in ([(r1,c) for c in range(c1,c2+1)] if mode=="down" else
                                     [(r,c1) for r in range(r1,r2+1)] if mode=="right" else [(r1,c1)]):
                        seeds[row,col] = self.raw(sheet,row,col)
                        seed_bytes += len(encoded(seeds[row,col]).encode())
                        require(seed_bytes<=4*1024*1024,"LIMIT","Fill seeds exceed 4 MiB; select a smaller range")
                    if mode == "series":
                        seed = typed(source)
                        require(seed and seed["kind"] == "number", "INVALID_ARGUMENT", "Numeric series requires a numeric top-left seed")
                        step = request.get("step", 1)
                        require(type(step) in (int,float),"INVALID_ARGUMENT","Series step must be finite")
                        try:
                            step=float(step)
                        except OverflowError as error:
                            raise EngineError("INVALID_ARGUMENT","Series step must be finite") from error
                        require(math.isfinite(step), "INVALID_ARGUMENT", "Series step must be finite")
                    for row in range(r1, r2+1):
                        for col in range(c1, c2+1):
                            seed_row,seed_col = (r1,col) if mode=="down" else (row,c1) if mode=="right" else (r1,c1)
                            raw_seed = seeds[seed_row,seed_col]
                            item = dict(raw_seed) if raw_seed else None
                            if mode == "series":
                                item["source"] = str(seed["value"] + step*((row-r1)*(c2-c1+1)+col-c1))
                                item["kind"] = "number"
                            elif item and typed(item)["kind"] == "formula":
                                item["source"] = copied_formula(item["source"], row-seed_row, col-seed_col)
                            self.put(sheet, row, col, item)
                elif kind == "move":
                    require(request.get("mode", "all") == "all", "UNSUPPORTED_PASTE_MODE", "Cut supports full paste only; use Copy for special paste")
                    r1, c1, r2, c2 = rectangle(request)
                    require(request.get("cut_state") == self.state(), "STALE_CUT", "Cut source changed. Copy or cut the range again", 409)
                    dest_row, dest_col = request.get("row"), request.get("col")
                    integer(dest_row, 1, MAX_ROWS-(r2-r1), "destination row")
                    integer(dest_col, 1, MAX_COLS-(c2-c1), "destination column")
                    reference_report = self.move_cells(sheet,(r1,c1,r2,c2),dest_row,dest_col)
                elif kind == "replace_all":
                    replacement = request.get("replacement")
                    require(isinstance(replacement, str), "INVALID_ARGUMENT", "Replacement must be text")
                    matches = self.matches(sheet, request, deadline=self.deadline)
                    needle = request["needle"]
                    needle_bytes, replacement_bytes = len(needle.encode()), len(replacement.encode())
                    for match in matches:
                        item = self.raw(sheet, match["row"], match["col"])
                        source = match["text"] if request.get("field") == "display" else item["source"]
                        source_bytes = len(source.encode())+source.count(needle)*(replacement_bytes-needle_bytes)
                        if request.get("field") == "display" and item["kind"] == "auto" and item["source"].startswith("'"):
                            source_bytes += 1
                        require(source_bytes<=65536,"INVALID_INPUT","Cell source must be text no larger than 64 KiB")
                        # Validate expansion before allocating the replacement string.
                        replaced = source.replace(needle, replacement)
                        if replaced == source:
                            continue
                        if request.get("field") == "display":
                            require(typed(item)["kind"] == "text", "DISPLAY_REPLACEMENT_UNSUPPORTED", "Only text displays can be replaced without conversion. Search stored input for numbers, booleans or formulas")
                            if item["kind"] == "auto" and item["source"].startswith("'"):
                                replaced = "'"+replaced
                            else:
                                item["kind"] = "text"
                        item["source"] = replaced
                        self.put(sheet, match["row"], match["col"], item)
                elif kind in ("undo", "redo"):
                    stack = self.undo if kind == "undo" else self.redo
                    require(bool(stack), "NO_HISTORY", f"Nothing to {kind}")
                    entries=list(stack[-1][1].items())
                    for key, (before, after) in (reversed(entries) if kind=='undo' else entries):
                        wanted, current = (before, after) if kind == "undo" else (after, before)
                        if key[0]=='table':
                            from .relational_rows import raw as row_raw,put as row_put
                            item=relational.table(self.db,key[1])
                            require(row_raw(self,item,key[2])==current,'HISTORY_CONFLICT','Undo record changed',409)
                            row_put(self,item,key[2],wanted)
                        else:
                            require(self.raw(*key) == current, "HISTORY_CONFLICT", "Undo target changed", 409)
                            self.put(*key, wanted)
                elif kind in ("rows_insert", "rows_delete", "columns_insert", "columns_delete"):
                    reference_report = self.change_structure(request)
                elif kind == "workbook_import":
                    reference_report=self.import_workbook(import_plan)
                elif kind == "sheet_add":
                    name = request.get("name", "Sheet")
                    self.check_name(name)
                    self.db.execute("INSERT INTO sheets VALUES(?,?,?)", (str(uuid.uuid4()), name, self.next_sheet_position()))
                elif kind == "sheet_rename":
                    target = self.sheet(request.get("sheet"))
                    self.check_name(request.get("name"))
                    reference_report = self.rename_references(target, request["name"])
                    self.db.execute("UPDATE sheets SET name=? WHERE id=?", (request["name"], target))
                elif kind == "sheet_duplicate":
                    target = self.sheet(request.get("sheet"))
                    self.check_name(request.get("name"))
                    reference_report = self.duplicate_sheet(target, request["name"])
                elif kind == "sheet_reorder":
                    order = request.get("order")
                    current = [row[0] for row in self.db.execute("SELECT id FROM sheets")]
                    require(isinstance(order, list) and all(isinstance(item,str) for item in order)
                            and len(order) == len(current) and set(order) == set(current), "INVALID_ARGUMENT", "Order must contain each sheet ID once")
                    self.db.executemany("UPDATE sheets SET position=? WHERE id=?", enumerate(order))
                elif kind == "sheet_delete":
                    target = self.sheet(request.get("sheet"))
                    require(self.db.execute("SELECT count(*) FROM sheets").fetchone()[0] > 1, "LAST_SHEET", "Cannot delete the last sheet")
                    reference_report = self.delete_sheet(target)
                else:
                    raise EngineError("UNSUPPORTED_OPERATION", f"Operation is not available: {kind}")
                # Records produced only by a structural rewrite are not cell-change events for bindings.
                self.structural_records={key:self.pending[key]['after'] for key,quiet in self.structural_marks.items() if quiet and key in self.pending}
                if sheet_structural:self.structure_hook(operation,request,'after',before_catalog,event_arguments)
                self.run_hooks(operation)
                repaired_issues=None
                if kind=='table_repair':
                    from .relationships import finish_repair
                    finish_repair(self)
                    repaired_issues=relational.validate_data(self.db,relational.tables(self.db),self.deadline,repair=True)
                records.finish(self,request,record_context)
                self.check_operation()
                self.changes = {key: pair for key, pair in self.changes.items() if pair[0] != pair[1] or key in self.table_schema_keys}
                self.db.execute("UPDATE dh_meta SET revision=revision+1")
                result = {"operation_id": operation, "outcome": "committed", "state": self.state(), "changed_cells": sum(key[0]!='table' for key in self.changes)+self.bulk_loaded,
                          "changed_records":sum(key[0]=='table' for key in self.changes)}
                result.update(reference_report)
                if self.binding_notices:result["binding_notices"]=self.binding_notices
                self.db.execute("INSERT INTO operations VALUES(?,?,?)", (operation, fingerprint, encoded(result)))
                self.check_operation()
                self.verify_file()
                self.check_operation()
                self.db.execute("COMMIT")
                in_transaction = False
                if repaired_issues is not None:self.relational_issues=repaired_issues
                if structural or self.table_record_keys:self.dependencies.clear()
                else:self.dependencies.invalidate(self.changes)
                if structural:
                    self.undo.clear()
                    self.redo.clear()
                elif kind == "undo":
                    label, _, _ = self.undo.pop()
                    reversed_changes = {key: [pair[1], pair[0]] for key, pair in reversed(list(self.changes.items()))}
                    self.redo.append((label, reversed_changes, self.history_cost(label,reversed_changes)))
                elif kind == "redo":
                    label, _, _ = self.redo.pop()
                    self.undo.append((label, self.changes, self.history_cost(label,self.changes)))
                elif self.changes:
                    self.redo.clear()
                    label = request.get("label",{'table_insert':'Insert records','table_update':'Edit records','table_delete':'Delete records'}.get(kind,kind.title()))
                    self.undo.append((label, self.changes, self.history_cost(label,self.changes)))
                    self.undo = self.undo[-100:]
                while sum(entry[2] for entry in self.undo + self.redo) > MAX_HISTORY_BYTES:
                    (self.undo if self.undo else self.redo).pop(0)
                if on_commit is not None:on_commit()
                return result
            except Exception as error:
                if not in_transaction:
                    raise EngineError("OUTCOME_UNKNOWN","Data may have committed; reconcile the durable receipt",500,"unknown",operation_id=operation) from error
                if not self.db.in_transaction:
                    # SQLITE_INTERRUPT during a write can roll back the whole
                    # transaction, including its savepoint. Reacquire ownership
                    # before reserving a failed receipt: another service may
                    # have acquired this ID after SQLite released our lock.
                    cause=error.__cause__
                    interrupted=(isinstance(error,EngineError) and error.body['code'] in ('CANCELLED','TIMEOUT')
                        and isinstance(cause,sqlite3.Error) and getattr(cause,'sqlite_errorcode',0)&255==sqlite3.SQLITE_INTERRUPT)
                    if not interrupted:raise EngineError("OUTCOME_UNKNOWN","Transaction outcome requires receipt lookup",500,"unknown",operation_id=operation) from error
                    try:
                        self.verify_file();self.db.execute('BEGIN IMMEDIATE')
                        receipt=self.recorded_operation(operation,fingerprint)
                        if receipt is not None:
                            self.db.execute('ROLLBACK');self.refresh();return receipt
                        self.db.execute('SAVEPOINT operation_changes')
                        self.dependencies.clear()
                    except Exception as reacquire_error:
                        if self.db.in_transaction:self.db.execute('ROLLBACK')
                        if isinstance(reacquire_error,EngineError) and reacquire_error.body.get('operation_id')==operation:raise
                        raise EngineError('OUTCOME_UNKNOWN','SQLite confirmed interruption, but receipt ownership could not be reacquired; reconcile the operation ID',500,'unknown',operation_id=operation) from reacquire_error
                try:
                    # Keep the writer lock while reverting data and reserving
                    # this ID's failed outcome. Another service cannot steal it.
                    self.db.execute("ROLLBACK TO operation_changes")
                    self.db.execute("RELEASE operation_changes")
                    self.unverified_records=unverified_before
                except sqlite3.Error as rollback_error:
                    try:
                        if self.db.in_transaction: self.db.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise EngineError("OUTCOME_UNKNOWN","Rollback could not be confirmed; reconcile the operation ID",500,"unknown",operation_id=operation) from rollback_error
                try:
                    self.verify_file()
                except (EngineError,OSError) as changed:
                    # Do not publish a failed receipt into a detached file or
                    # leave its writer transaction open after a path failure.
                    try: self.db.execute("ROLLBACK")
                    except sqlite3.Error as rollback_error:
                        raise EngineError("OUTCOME_UNKNOWN","File identity and rollback require reconciliation",500,"unknown",operation_id=operation) from rollback_error
                    if isinstance(changed,EngineError):
                        changed.body.update(outcome="unknown",operation_id=operation)
                        raise changed
                    raise EngineError("OUTCOME_UNKNOWN","Native file identity could not be confirmed; retain the original file and reconcile its operation ID",409,"unknown",operation_id=operation) from changed
                if isinstance(error, EngineError):
                    error.body.update(outcome="rolled_back", operation_id=operation)
                    failure=error
                elif isinstance(error, (HookFailure, FormulaError, sqlite3.Error)):
                    failure=EngineError("HOOK_FAILED" if isinstance(error, HookFailure) else "OPERATION_FAILED",
                                        str(error),409,"rolled_back",operation_id=operation)
                else:
                    logging.getLogger(__name__).exception("Managed operation failed before commit")
                    failure=EngineError("INTERNAL_ERROR","Operation rolled back; inspect service diagnostics",
                                        500,"rolled_back",operation_id=operation)
                if getattr(self,"macro_diagnostics",None) is not None:failure.body["macro"]=self.macro_diagnostics
                failure.body["http_status"]=failure.status
                try:
                    self.db.execute("INSERT INTO operations VALUES(?,?,?)",(operation,fingerprint,encoded(failure.body)))
                    self.db.execute("COMMIT")
                except sqlite3.Error as receipt_error:
                    try:
                        if self.db.in_transaction: self.db.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise EngineError("OUTCOME_UNKNOWN","Data changes were rolled back but the receipt could not be confirmed; reconcile the operation ID",500,"unknown",operation_id=operation) from receipt_error
                raise failure
            finally:
                if import_plan is not None and import_plan["stage"] is not None:import_plan["stage"].close()

    @staticmethod
    def history_cost(label,changes):
        # Compute once per receipt; never serialize all prior history for each edit.
        return len(encoded([label,[[key, *values] for key, values in changes.items()]]).encode())

    def move_cells(self, sheet, rectangle, dest_row, dest_col):
        r1,c1,r2,c2=rectangle;dr,dc=dest_row-r1,dest_col-c1
        if dr==0 and dc==0:return {}
        sources={};source_bytes=0;formulas={};scan_bytes=0;marked=[]
        target_name=self.db.execute("SELECT name FROM sheets WHERE id=?",(sheet,)).fetchone()[0]
        sheet_order=[name for (name,) in self.db.execute("SELECT name FROM sheets ORDER BY position,id")]
        with sql_deadline(self.db,self.deadline):
            for row in range(r1,r2+1):
                for col in range(c1,c2+1):
                    item=self.raw(sheet,row,col);source_bytes+=len(encoded(item).encode())
                    require(source_bytes<=4*1024*1024,"LIMIT","Cut source exceeds 4 MiB; select a smaller range")
                    sources[row,col]=item
            for scanned,(identity,name,row,col,source,kind,style) in enumerate(self.db.execute(
                    "SELECT c.sheet_id,s.name,c.row,c.col,c.source,c.kind,c.style FROM cells c JOIN sheets s ON s.id=c.sheet_id "
                    "WHERE c.source LIKE '=%' AND c.kind IN ('auto','formula')"),1):
                require(time.monotonic()<=self.deadline,"TIMEOUT","Move exceeded its time budget")
                require(scanned<=MAX_CELLS,"LIMIT","Move reference scan exceeds 10,000 formulas")
                scan_bytes+=len(source.encode())+len(style.encode())+128
                require(scan_bytes<=8*1024*1024,"LIMIT","Move reference sources exceed 8 MiB")
                moving=identity==sheet and r1<=row<=r2 and c1<=col<=c2
                overwritten=identity==sheet and dest_row<=row<=dest_row+r2-r1 and dest_col<=col<=dest_col+c2-c1
                if overwritten and not moving:continue
                try:transformed=move_references(source,name,target_name,rectangle,dr,dc,target_id=sheet,sheet_order=sheet_order)
                except Unmaintainable:
                    if identity!=sheet and not has_sheet_reference(source):continue  # Cannot refer to the moved cells.
                    item=unmaintained(source,style)
                    if moving:sources[row,col]=item;marked.append((name,row+dr,col+dc))
                    else:formulas[identity,row,col]=item;marked.append((name,row,col))
                    continue
                except FormulaError as error:
                    raise EngineError("REFERENCE_TRANSFORM_UNAVAILABLE",f"Cannot safely move reference at {name}!{address(row,col)}: {error}") from error
                if transformed!=source:
                    item={"source":transformed,"kind":kind,"style":json.loads(style)};typed(item)
                    if moving:sources[row,col]=item
                    else:formulas[identity,row,col]=item
            for row,col in sources:self.put(sheet,row,col,None)
            for (row,col),item in sources.items():self.put(sheet,row+dr,col+dc,item)
            for key,item in formulas.items():self.put(*key,item)
        return unmaintained_report(marked)

    def delete_sheet(self, target):
        target_name = self.db.execute("SELECT name FROM sheets WHERE id=?", (target,)).fetchone()[0]
        deleted, formulas, payload_bytes, warnings, locations, marked = [], [], 0, 0, [], []
        with sql_deadline(self.db,self.deadline):
            for row, col, source, style in self.db.execute(
                    "SELECT row,col,source,style FROM cells WHERE sheet_id=? LIMIT ?",(target,MAX_CELLS+1)):
                self.check_operation()
                require(len(deleted)<MAX_CELLS,"LIMIT","Sheet deletion exceeds 10,000 cells")
                payload_bytes += len(source.encode())+len(style.encode())+128
                require(payload_bytes<=8*1024*1024,"LIMIT","Sheet deletion sources exceed 8 MiB")
                deleted.append((target,row,col))
            for scanned, (sheet, name, row, col, source, kind, style) in enumerate(self.db.execute(
                    "SELECT c.sheet_id,s.name,c.row,c.col,c.source,c.kind,c.style FROM cells c JOIN sheets s ON s.id=c.sheet_id "
                    "WHERE c.sheet_id<>? AND c.source LIKE '=%' AND c.kind IN ('auto','formula')",(target,)),1):
                self.check_operation()
                require(scanned<=MAX_CELLS,"LIMIT","Sheet reference scan exceeds 10,000 formulas")
                payload_bytes += len(source.encode())+len(style.encode())+128
                require(payload_bytes<=8*1024*1024,"LIMIT","Sheet deletion sources exceed 8 MiB")
                try:
                    transformed, broken = delete_sheet_references(source,name,target_name,target_id=target)
                except Unmaintainable:
                    if has_sheet_reference(source):  # Only a qualified reference can name the deleted sheet.
                        formulas.append(((sheet,row,col),unmaintained(source,style)));marked.append((name,row,col))
                    continue
                except FormulaError as error:
                    raise EngineError("REFERENCE_TRANSFORM_UNAVAILABLE", f"Cannot safely transform {name}!{address(row,col)}: {error}") from error
                if transformed != source:
                    formulas.append(((sheet,row,col),{"source":transformed,"kind":kind,"style":json.loads(style)}))
                    warnings += broken
                    if len(locations)<100:
                        locations.append({"sheet":name,"row":row,"col":col})
            require(len(deleted)+len(formulas)<=MAX_CELLS,"LIMIT","Sheet deletion exceeds 10,000 affected cells")
            for key, item in formulas:
                self.structural_put(key,item)
            for key in deleted:
                self.structural_put(key,None)
            from .names import break_sheet
            broken_names = break_sheet(self,target)
            from .bindings import break_sheet as break_binding_sheet
            from .bindings import scoped_to_names
            broken_bindings = sorted(break_binding_sheet(self,target)+scoped_to_names(self,broken_names))
            self.db.execute("DELETE FROM sheets WHERE id=?",(target,))
        result = {"reference_warnings":warnings,"reference_issues":locations,**unmaintained_report(marked)}
        if broken_names:result["broken_names"] = broken_names
        if broken_bindings:result["broken_bindings"] = broken_bindings
        return result

    def duplicate_sheet(self, target, new_name):
        old_name = self.db.execute("SELECT name FROM sheets WHERE id=?", (target,)).fetchone()[0]
        new_id, cells, payload_bytes, marked = str(uuid.uuid4()), [], 0, []
        with sql_deadline(self.db, self.deadline):
            for row, col, source, kind, style in self.db.execute(
                    "SELECT row,col,source,kind,style FROM cells WHERE sheet_id=? LIMIT ?",(target,MAX_CELLS+1)):
                require(time.monotonic()<=self.deadline, "TIMEOUT", "Operation exceeded its time budget")
                require(len(cells)<MAX_CELLS, "LIMIT", "Sheet duplication exceeds 10,000 cells")
                payload_bytes += len(source.encode())+len(style.encode())+128
                require(payload_bytes<=8*1024*1024, "LIMIT", "Sheet duplication sources exceed 8 MiB")
                if kind in ("auto","formula") and source.startswith("="):
                    try:
                        source = rename_sheet_references(source, old_name, new_name, target_id=target, new_id=new_id)
                    except Unmaintainable:
                        if has_sheet_reference(source):  # The copy cannot retarget its own sheet's name.
                            kind = "unmaintained";marked.append((new_name,row,col))
                    except FormulaError as error:
                        raise EngineError("REFERENCE_TRANSFORM_UNAVAILABLE", f"Cannot safely copy {old_name}!{address(row,col)}: {error}") from error
                item = {"source":source,"kind":kind,"style":json.loads(style)}
                typed(item)
                cells.append((row,col,item))
            self.db.execute("INSERT INTO sheets VALUES(?,?,?)", (new_id,new_name,self.next_sheet_position()))
            for row, col, item in cells:
                self.structural_put((new_id,row,col),item,(target,row,col))
            if self.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]>=3:
                self.db.execute("INSERT INTO dh_objects SELECT namespace,id,?,payload FROM dh_objects WHERE sheet_id=? AND namespace IN ('presentation','list')",(new_id,target))
                require(self.db.execute('SELECT count(*)<=256 AND coalesce(sum(length(payload)),0)<=16777216 FROM dh_objects').fetchone()[0],'LIMIT','Native metadata exceeds its budget')
        return unmaintained_report(marked)

    def rename_references(self, target, new_name):
        old_name = self.db.execute("SELECT name FROM sheets WHERE id=?", (target,)).fetchone()[0]
        formulas, payload_bytes, marked = [], 0, []
        with sql_deadline(self.db, self.deadline):
            for scanned, (sheet, name, row, col, source, kind, style) in enumerate(self.db.execute(
                    "SELECT c.sheet_id,s.name,c.row,c.col,c.source,c.kind,c.style FROM cells c JOIN sheets s ON s.id=c.sheet_id "
                    "WHERE c.source LIKE '=%' AND c.kind IN ('auto','formula')"), 1):
                require(time.monotonic()<=self.deadline, "TIMEOUT", "Operation exceeded its time budget")
                require(scanned<=MAX_CELLS, "LIMIT", "Sheet reference scan exceeds 10,000 formulas")
                payload_bytes += len(source.encode())+len(style.encode())+128
                require(payload_bytes<=8*1024*1024, "LIMIT", "Sheet reference sources exceed 8 MiB")
                try:
                    transformed = rename_sheet_references(source, old_name, new_name, target_id=target)
                except Unmaintainable:
                    if has_sheet_reference(source):  # Only a qualified reference can name the renamed sheet.
                        formulas.append(((sheet,row,col),unmaintained(source,style)))
                        marked.append((new_name if sheet==target else name,row,col))
                    continue
                except FormulaError as error:
                    raise EngineError("REFERENCE_TRANSFORM_UNAVAILABLE", f"Cannot safely transform {name}!{address(row,col)}: {error}") from error
                if transformed != source:
                    item = {"source":transformed,"kind":kind,"style":json.loads(style)}
                    typed(item)
                    formulas.append(((sheet,row,col),item))
            for key, item in formulas:
                self.structural_put(key,item)
        return unmaintained_report(marked)

    def change_structure(self, request):
        """Bounded whole-row/column changes, including references on other sheets."""
        target = self.sheet(request.get("sheet", "Sheet1"))
        target_name = self.db.execute("SELECT name FROM sheets WHERE id=?", (target,)).fetchone()[0]
        sheet_order = [name for (name,) in self.db.execute("SELECT name FROM sheets ORDER BY position,id")]
        axis = "row" if request["action"].startswith("rows_") else "col"
        maximum = MAX_ROWS if axis == "row" else MAX_COLS
        start = integer(request.get("start"), 1, maximum, "start")
        count = integer(request.get("count", 1), 1, maximum-start+1, "count")
        delete = request["action"].endswith("_delete")
        end = start+count-1
        moved, formulas, payload_bytes, warnings, locations, marked = {}, {}, 0, 0, [], []
        with sql_deadline(self.db, self.deadline):
            # Axis is selected internally. Stream sources so long cells cannot
            # allocate an unbounded fetchall before the ordinary-operation limit.
            for row, col, source, kind, style in self.db.execute(
                    f"SELECT row,col,source,kind,style FROM cells WHERE sheet_id=? AND {axis}>=? LIMIT ?",
                    (target, start, MAX_CELLS+1)):
                require(len(moved)<MAX_CELLS, "LIMIT", "Structure change exceeds 10,000 stored cells")
                payload_bytes += len(source.encode())+len(style.encode())+128
                require(payload_bytes<=8*1024*1024, "LIMIT", "Structure sources exceed 8 MiB")
                value = row if axis == "row" else col
                destination = None if delete and value<=end else value+(-count if delete else count)
                require(destination is None or destination<=maximum, "GRID_OVERFLOW", "Insertion would discard stored cells beyond the grid edge")
                key = (target,row,col)
                new_key = None if destination is None else (target, destination if axis == "row" else row, destination if axis == "col" else col)
                moved[key] = (new_key, {"source":source,"kind":kind,"style":json.loads(style)})
            scanned = 0
            for sheet, name, row, col, source, kind, style in self.db.execute(
                    "SELECT c.sheet_id,s.name,c.row,c.col,c.source,c.kind,c.style FROM cells c JOIN sheets s ON s.id=c.sheet_id "
                    "WHERE c.source LIKE '=%' AND c.kind IN ('auto','formula')"):
                scanned += 1
                self.check_operation()
                require(scanned<=MAX_CELLS, "LIMIT", "Structure reference scan exceeds 10,000 formulas; a large-job path is not available")
                key = (sheet,row,col)
                if key in moved and moved[key][0] is None:
                    continue
                payload_bytes += len(source.encode())+len(style.encode())+128
                require(payload_bytes<=8*1024*1024, "LIMIT", "Structure sources exceed 8 MiB")
                try:
                    transformed, broken = transform_structure(source,name,target_name,axis,start,count,delete,target_id=target,sheet_order=sheet_order)
                except Unmaintainable:
                    if sheet!=target and not has_sheet_reference(source):continue  # Cannot refer to the edited sheet.
                    destination = moved[key][0] if key in moved else key
                    marked.append((name,destination[1],destination[2]))
                    if key in moved:moved[key][1]["kind"] = "unmaintained"
                    else:formulas[key] = unmaintained(source,style)
                    continue
                except FormulaError as error:
                    raise EngineError("REFERENCE_TRANSFORM_UNAVAILABLE", f"Cannot safely transform {name}!{address(row,col)}: {error}") from error
                warnings += broken
                if broken and len(locations)<100:
                    destination = moved[key][0] if key in moved else key
                    locations.append({"sheet":name,"row":destination[1],"col":destination[2]})
                if key in moved:
                    moved[key][1]["source"] = transformed
                elif transformed != source:
                    formulas[key] = {"source":transformed,"kind":kind,"style":json.loads(style)}
            affected = set(moved) | {dest for dest, _ in moved.values() if dest is not None} | set(formulas)
            require(len(affected)<=MAX_CELLS, "LIMIT", "Structure change exceeds 10,000 affected coordinates")
            for key in moved:
                self.structural_put(key,None)
            for key, (destination, item) in moved.items():
                if destination is not None:
                    self.structural_put(destination,item,key)
            for key, item in formulas.items():
                self.structural_put(key,item)
            from .names import adjust_structure
            broken_names = adjust_structure(self,target,axis,start,count,delete)
            from .bindings import adjust_structure as adjust_bindings
            from .bindings import scoped_to_names
            broken_bindings = sorted(adjust_bindings(self,target,axis,start,count,delete)+scoped_to_names(self,broken_names))
        result = {"reference_warnings":warnings,"reference_issues":locations,**unmaintained_report(marked)}
        if broken_names:result["broken_names"] = broken_names
        if broken_bindings:result["broken_bindings"] = broken_bindings
        return result

    @staticmethod
    def check_name(name):
        require(isinstance(name, str) and 0 < len(name) <= 31 and not re.search(r"[\[\]:*?/\\!\x00-\x1f]", name),
                "INVALID_NAME", "Sheet name must be 1-31 characters without []:*?/\\! or control characters")

    def next_sheet_position(self):
        with sql_deadline(self.db,self.deadline):
            position = self.db.execute("SELECT max(position) FROM sheets").fetchone()[0]
        require(type(position) is int and 0<=position<MAX_SHEET_POSITION,
                "POSITION_LIMIT","Reorder sheets before adding or duplicating another sheet; display positions are exhausted",409)
        # Check in Python before addition: SQLite promotes integer overflow to REAL.
        return position+1

    def _list_rows(self, sheet, args, calc, deadline):
        r1, c1, r2, c2 = rectangle(args)
        criteria, keys = args.get("filters", []), args.get("sort", [])
        require(isinstance(criteria, list) and len(criteria) <= 8 and isinstance(keys, list) and len(keys) <= 8,
                "INVALID_ARGUMENT", "At most eight filters and sort keys are supported")
        require(all(isinstance(item,dict) for item in criteria+keys),"INVALID_ARGUMENT","Each filter and sort key must be an object")
        for criterion in criteria:
            integer(criterion.get("col"), c1, c2, "filter column")
            require(criterion.get("op") in ("=", "<>", ">", "<", ">=", "<=", "contains"), "INVALID_ARGUMENT", "Unsupported filter comparison")
            require(isinstance(criterion.get("value"), (str, float, int, bool, type(None))), "INVALID_ARGUMENT", "Filter value must be scalar")
            require("value_kind" not in criterion or criterion["value_kind"] in TEMPORAL_KINDS,"INVALID_ARGUMENT","Unsupported filter value_kind")
            require(criterion.get("op")!="contains" or "value_kind" not in criterion,"INVALID_ARGUMENT","Contains requires Text")
        try:
            criteria=[{**item,"value":Temporal(item["value"],item["value_kind"])} if "value_kind" in item else item for item in criteria]
        except ValueError as error:raise EngineError("INVALID_ARGUMENT",str(error)) from error
        for key in keys:
            integer(key.get("col"), c1, c2, "sort column")
            require(type(key.get("descending", False)) is bool, "INVALID_ARGUMENT", "Sort direction must be boolean")
        require(type(args.get("header", False)) is bool, "INVALID_ARGUMENT", "Header must be explicit boolean")
        first = r1 + int(args.get("header", False))
        rows, cache, cache_bytes = [], {}, 0

        def value(row, col):
            nonlocal cache_bytes
            if (row, col) not in cache:
                scalar=calc.cell(sheet,row,col)
                cache_bytes+=len(encoded(scalar).encode())
                require(cache_bytes<=4*1024*1024,"READ_LIMIT","List criteria values exceed 4 MiB")
                cache[row, col] = scalar
            return cache[row, col]

        def accepts(row, criterion):
            left, right, op = value(row, criterion["col"]), criterion.get("value"), criterion["op"]
            if op == "contains":
                return type(left) is str and type(right) is str and right in left
            if isinstance(left,Temporal) or isinstance(right,Temporal):
                if not isinstance(left,Temporal) or not isinstance(right,Temporal) or temporal_key(left)[0]!=temporal_key(right)[0]:return op=="<>"
                left,right=temporal_key(left)[1],temporal_key(right)[1]
            same_type = type(left) == type(right) or (type(left) in (int, float) and type(right) in (int, float))
            if not same_type:
                return op == "<>"
            if op in ("<", ">", "<=", ">=") and left is None:
                return False
            return {"=": lambda: left == right, "<>": lambda: left != right,
                    ">": lambda: left > right, "<": lambda: left < right,
                    ">=": lambda: left >= right, "<=": lambda: left <= right}[op]()

        for row in range(first, r2+1):
            require(time.monotonic()<=deadline,"TIMEOUT","List evaluation exceeded its time budget")
            if all(accepts(row, criterion) for criterion in criteria):
                rows.append(row)

        def sort_value(value):
            if isinstance(value,Temporal):return 2,temporal_key(value)
            rank = 0 if value is None else 1 if type(value) in (int, float) else 3 if isinstance(value, str) else 4
            return rank, "" if value is None else value

        def compare(left, right):
            require(time.monotonic()<=deadline,"TIMEOUT","List evaluation exceeded its time budget")
            for key in keys:
                a, b = sort_value(value(left, key["col"])), sort_value(value(right, key["col"]))
                comparison = (a > b) - (a < b)
                if comparison:
                    return -comparison if key.get("descending") else comparison
            return left-right

        if keys:
            rows.sort(key=cmp_to_key(compare))
        count = len(rows)
        if args.get("header"):
            rows.insert(0, r1)
        return {"rows": rows, "matching": count, "total": max(0, r2-first+1), "state": self.state(),
                "scope": [r1, c1, r2, c2], "semantics": "presentation_only; edits target original row coordinates"}

    def list_view(self, sheet, args):
        """An explicit list is a presentation projection over stable source rows."""
        with self.lock:
            deadline=time.monotonic()+2
            self.refresh(deadline=deadline)
            self.db.execute("BEGIN")
            try:
                self.refresh(deadline=deadline)
                with sql_deadline(self.db,deadline):
                    return self._list_rows(self.sheet(sheet),args,self.calculator(seconds=2),deadline)
            except FormulaError as error:
                raise EngineError("CALCULATION_ERROR", f"List evaluation blocked: {error}") from error
            finally:
                self.db.execute("ROLLBACK")

    def list_values(self, sheet, args):
        """Evaluate criteria and read displayed values in the same input snapshot."""
        r1,c1,r2,c2=rectangle(args)
        expected=args.get("expected_state")
        require(isinstance(expected,str) and bool(expected),"INVALID_ARGUMENT","List values require expected_state")
        excluded=[]
        for name,lower,upper in (("exclude_rows",r1,r2),("exclude_columns",c1,c2)):
            items=args.get(name,[])
            require(isinstance(items,list) and len(items)<=1024,"LIMIT","At most 1,024 excluded sections per axis")
            excluded.append({integer(item,lower,upper,name) for item in items})
        left=integer(args.get("select_c1",c1),c1,c2,"select_c1")
        right=integer(args.get("select_c2",c2),left,c2,"select_c2")
        with self.lock:
            deadline=time.monotonic()+2
            self.refresh(deadline=deadline);self.db.execute("BEGIN")
            try:
                self.refresh(deadline=deadline)
                require(self.state()==expected,"STALE_STATE","List inputs changed; refresh and review again",409)
                with sql_deadline(self.db,deadline):
                    sheet=self.sheet(sheet);calc=self.calculator(seconds=2)
                    result=self._list_rows(sheet,args,calc,deadline)
                    available=result.pop("rows")
                    first=integer(args.get("view_r1",1),1,max(1,len(available)),"view_r1")
                    last=integer(args.get("view_r2",len(available)),first if available else 0,len(available) if available else 0,"view_r2")
                    selected=[row for row in available[first-1:last] if row not in excluded[0]]
                    columns=[col for col in range(left,right+1) if col not in excluded[1]]
                    matrix=[[{} for col in columns] for row in selected]
                    result.update(sheet=sheet,rows=matrix,source_rows=selected,source_columns=columns,
                                  formulas_as_values=0,excluded_rows=len(available[first-1:last])-len(selected),excluded_columns=right-left+1-len(columns))
                    rowmap={row:index for index,row in enumerate(selected)};colmap={col:index for index,col in enumerate(columns)}
                    size=len(encoded(result).encode())
                    for row,col,source,kind,style in self.range_cells(sheet,selected,left,right,deadline):
                        if col not in colmap:continue
                        item=typed({"source":source,"kind":kind,"style":json.loads(style)})
                        value=calc.cell(sheet,row,col) if item["kind"] in EVALUATED else item["value"]
                        result["formulas_as_values"]+=int(item["kind"]=="formula")
                        scalar_kind=value.kind if isinstance(value,Temporal) else "null" if value is None else "boolean" if type(value) is bool else "number" if type(value) in (int,float) else "text"
                        scalar_source="NULL" if value is None else "TRUE" if value is True else "FALSE" if value is False else str(value)
                        if item["kind"]=="blank":scalar_kind,scalar_source="auto",""
                        cell={"source":scalar_source,"input_kind":scalar_kind,"kind":scalar_kind,"value":value,"style":item["style"],"display":display(value,item["style"]),"calculation":"current"}
                        if isinstance(value,Temporal):cell["value_kind"]=value.kind
                        size+=len(encoded(cell).encode())
                        require(size<=4*1024*1024,"READ_LIMIT","List values exceed 4 MiB; narrow the scope")
                        matrix[rowmap[row]][colmap[col]]=cell
                    require(time.monotonic()<=deadline,"TIMEOUT","List values exceeded their time budget")
                    require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","List values exceed 4 MiB")
                    return result
            except FormulaError as error:
                raise EngineError("CALCULATION_ERROR",f"List values blocked: {error}") from error
            finally:
                self.db.execute("ROLLBACK")

    def matches(self, sheet, args, *, deadline=None):
        r1, c1, r2, c2 = rectangle(args)
        needle, field = args.get("needle"), args.get("field", "source")
        require(isinstance(needle, str) and bool(needle), "INVALID_ARGUMENT", "Find text must be nonempty")
        require(field in ("source", "display"), "INVALID_ARGUMENT", "Search field must be source or display")
        deadline = time.monotonic()+2 if deadline is None else deadline
        calc, matches, match_bytes = self.calculator(seconds=min(2,max(0,deadline-time.monotonic()))), [], 0
        with sql_deadline(self.db,deadline):
            for row, col, source, kind, style in self.range_cells(sheet,range(r1,r2+1),c1,c2,deadline):
                require(time.monotonic()<=deadline,"TIMEOUT","Search exceeded its time budget")
                text = source
                if field == "display":
                    try:
                        text = display(calc.cell(sheet, row, col), json.loads(style))
                    except FormulaError as error:
                        text = error.code
                if needle in text:
                    match_bytes += len(text.encode())
                    require(match_bytes <= 8*1024*1024, "LIMIT", "Matched source/display data exceeds 8 MiB; narrow the search scope")
                    matches.append({"row": row, "col": col, "address": address(row, col), "text": text})
        return matches

    def trace(self, sheet, args):
        from .tracing import trace
        return trace(self,sheet,args)

    def find(self, sheet, args):
        offset=integer(args.get("offset",0),0,MAX_CELLS,"offset")
        limit=integer(args.get("limit",500),1,500,"limit")
        expected=args.get("expected_state")
        require(expected is None or isinstance(expected,str) and bool(expected),"INVALID_ARGUMENT","expected_state must be nonempty text")
        require(not offset or expected is not None,"INVALID_ARGUMENT","Later Find pages require expected_state")
        with self.lock:
            deadline=time.monotonic()+2
            self.refresh(deadline=deadline);self.db.execute("BEGIN")
            try:
                self.refresh(deadline=deadline)
                require(expected is None or expected==self.state(),"STALE_STATE","Find inputs changed; refresh and search again",409)
                matches=self.matches(self.sheet(sheet),args,deadline=deadline)
                page=[{**match,"text":match["text"][:256]} for match in matches[offset:offset+limit]]
                require(time.monotonic()<=deadline,"TIMEOUT","Search exceeded its time budget")
                more=offset+len(page)<len(matches)
                return {"matches":page,"count":len(matches),"state":self.state(),"offset":offset,"limit":limit,
                        "has_more":more,"next_offset":offset+len(page) if more else None,"truncated":len(matches)>len(page)}
            finally:
                self.db.execute("ROLLBACK")

    def snapshot(self, path, *, cancel_event=None):
        with self.lock:
            deadline = time.monotonic()+SNAPSHOT_SECONDS
            self.verify_file()
            destination = Path(path).expanduser().resolve()
            require(destination != self.path, "SAME_FILE", "Choose a different path")
            identity = _reserve_destination(destination)
            target = sqlite3.connect(destination.as_uri()+"?mode=rw",uri=True)
            try:
                _verify_destination(destination,identity)
                def progress(*_):
                    require(cancel_event is None or not cancel_event.is_set(),"CANCELLED","Snapshot cancelled",409)
                    require(time.monotonic()<=deadline,"TIMEOUT","Snapshot exceeded its time budget; the destination is preserved for inspection")
                self.db.backup(target,pages=256,progress=progress,sleep=0.01)
                _verify_destination(destination,identity)
                with sql_deadline(target,deadline,cancel_event=cancel_event):
                    self.validate(target,deadline=deadline)
                    check_integrity(target,deadline)
                self.verify_file()
                _verify_destination(destination,identity)
            finally:
                target.close()
            return {"path": str(destination), "outcome": "committed"}

    def csv_export(self, sheet, args):
        r1, c1, r2, c2 = rectangle(args)
        with self.lock:
            self.refresh()
            self.db.execute("BEGIN")
            try:
                self.refresh()
                sheet, calc = self.sheet(sheet), self.calculator(seconds=2)
                output = io.StringIO(newline="")
                result = {"csv":"", "state":self.state(),
                          "report":["Values only; formulas, styles, code and native metadata are omitted.",
                                    "Blank, NULL and empty text all become empty CSV fields."]}
                output_bytes = len(encoded(result).encode())
                temporal_entries=[]
                for row in range(r1, r2+1):
                    # Values export uses canonical text, independent of number format.
                    row_output = io.StringIO(newline="")
                    values=[calc.cell(sheet,row,col) for col in range(c1,c2+1)]
                    temporal_entries.extend({"row":row-r1+1,"col":col+1,"kind":value.kind} for col,value in enumerate(values) if isinstance(value,Temporal))
                    csv.writer(row_output).writerow([interchange_text(value) for value in values])
                    text = row_output.getvalue()
                    output_bytes += len(encoded(text).encode())-2  # Exclude each string's enclosing quotes.
                    require(output_bytes <= 4*1024*1024, "LIMIT", "CSV response exceeds 4 MiB; export a smaller selection")
                    output.write(text)
                result["csv"] = output.getvalue()
                from .interchange import native_only_report
                result["report"].extend(native_only_report(self.native_catalog()))
                if temporal_entries:
                    result["temporal_schema"]=temporal_schema(result["csv"],temporal_entries)
                    result["report"].append("ISO date/time values require the companion temporal_schema to restore native kinds automatically; CSV alone is untyped.")
                require(len(encoded(result).encode()) <= 4*1024*1024, "LIMIT", "CSV response exceeds 4 MiB; export a smaller selection")
                return result
            except FormulaError as error:
                raise EngineError("CALCULATION_ERROR", f"Export blocked: {error}") from error
            finally:
                self.db.execute("ROLLBACK")
