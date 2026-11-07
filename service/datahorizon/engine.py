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

from .formula import Calculator, FormulaError, MAX_COLS, MAX_ROWS, address, translate, transform_structure, rename_sheet_references, delete_sheet_references
from .hooks import HookFailure, invoke

MAX_CELLS = 10_000
MAX_HISTORY_BYTES = 16*1024*1024
MAX_REVISION = 2**63-1
MAX_SHEET_POSITION = 2**63-1
SNAPSHOT_SECONDS = 5
NUMBER_SOURCE = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
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


@contextmanager
def sql_deadline(db, deadline):
    expired = False
    def check():
        nonlocal expired
        expired = time.monotonic() > deadline
        return int(expired)
    db.set_progress_handler(check,1000)
    try:
        require(not check(),"TIMEOUT","Operation exceeded its time budget")
        yield
        require(not check(),"TIMEOUT","Operation exceeded its time budget")
    except sqlite3.Error as error:
        if expired:
            raise EngineError("TIMEOUT","Operation exceeded its time budget") from error
        raise
    finally:
        # Rollback and durable receipt recording must remain possible on timeout.
        db.set_progress_handler(None,0)


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
    allowed = {"format", "bold", "italic", "align", "background", "wrap", "border"}
    require(not set(style) - allowed, "UNSUPPORTED_STYLE", "Unsupported style property")
    for key, value in style.items():
        if key == "format":
            require(value in ("general", "text", "fixed2"), "UNSUPPORTED_STYLE", "Unsupported number format")
        elif key == "align":
            require(value in ("left", "center", "right"), "INVALID_STYLE", "Invalid alignment")
        elif key == "background":
            require(isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value), "INVALID_STYLE", "Expected #RRGGBB")
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
    require(kind in ("auto", "text", "number", "boolean", "null", "formula"), "INVALID_INPUT", "Unknown input kind")
    validate_style(style)
    value = source
    if kind == "null":
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


def display(value, style):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (float, int)):
        return f"{value:.2f}" if style.get("format") == "fixed2" else format(value, ".15g")
    return str(value)


def interchange_text(value):
    if type(value) is float:
        return str(int(value)) if value.is_integer() and abs(value) <= 9_007_199_254_740_991 else repr(value)
    return display(value, {})


def input_signature(item):
    if item is None or (item["kind"] == "auto" and item["source"] == ""):
        return None
    return item["kind"], item["source"]


def schema_signature(db):
    return [(row[0], row[1], row[2]) for row in db.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name")]


def expected_schema():
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        return schema_signature(db)
    finally:
        db.close()


EXPECTED_SCHEMA = expected_schema()


class Workbook:
    def __init__(self, path, create=False, readonly=False, handler=None):
        create, readonly = boolean(create, "create"), boolean(readonly, "readonly")
        self.path = Path(path).expanduser().resolve()
        self.readonly, self.handler = readonly, handler
        self.lock = threading.RLock()
        self.undo, self.redo = [], []
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
        self.db.close()

    def validate(self, db=None, *, deadline=None):
        db = self.db if db is None else db
        deadline = getattr(self,"_opening_deadline",time.monotonic()+5) if deadline is None else deadline
        with sql_deadline(db,deadline):
            require(schema_signature(db) == EXPECTED_SCHEMA,
                    "SCHEMA_CHANGED", "Unsupported schema. Reopen a compatible native workbook; no repair was attempted", 409)
            meta = db.execute("SELECT format_version,revision,workbook_id FROM dh_meta WHERE singleton=1").fetchall()
            require(len(meta) == 1 and meta[0][0] == 1 and type(meta[0][1]) is int and meta[0][1] >= 0,
                    "FORMAT_VERSION", "Unsupported or invalid native format", 409)
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
            require(db.execute("PRAGMA foreign_key_check").fetchone() is None,
                    "INVALID_FORMAT","Native workbook contains orphaned cell references",409)
            require(db.execute("SELECT 1 FROM cells WHERE typeof(row)<>'integer' OR typeof(col)<>'integer' OR row NOT BETWEEN 1 AND ? OR col NOT BETWEEN 1 AND ? LIMIT 1",
                               (MAX_ROWS,MAX_COLS)).fetchone() is None,
                    "INVALID_FORMAT","Native cells require integer coordinates within the supported grid",409)

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

    def state(self):
        revision = self.db.execute("SELECT revision FROM dh_meta").fetchone()[0]
        return f"{self.epoch}:{revision}"

    def info(self):
        with self.lock:
            self.refresh()
            return {"id": self.id, "path": str(self.path), "state": self.state(), "readonly": self.readonly,
                    "sheets": [{"id": s, "name": n} for s, n in self.db.execute("SELECT id,name FROM sheets ORDER BY position,id")],
                    "undo": self.undo[-1][0] if self.undo else None, "redo": self.redo[-1][0] if self.redo else None,
                    "storage": "stored", "calculation": "evaluated_on_read", "format_version": 1}

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

    def calculator(self, seconds=0.25):
        def resolve(name):
            try:
                return self.sheet(name)
            except EngineError as error:
                raise FormulaError("#REF!", "Referenced sheet does not exist") from error
        return Calculator(lambda s, r, c: typed(self.raw(s, r, c)), resolve, seconds)

    def range_cells(self, sheet, rows, c1, c2, deadline):
        for row in rows:
            require(time.monotonic()<=deadline,"TIMEOUT","Range query exceeded its time budget")
            # Row equality lets the primary key bound the column scan as well.
            yield from self.db.execute(
                "SELECT row,col,source,kind,style FROM cells WHERE sheet_id=? AND row=? AND col BETWEEN ? AND ? ORDER BY col",
                (sheet,row,c1,c2))

    def read(self, sheet, args, *, limit=4096):
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
                with sql_deadline(self.db,deadline):
                    sheet, calc = self.sheet(sheet), self.calculator()
                    cells = []
                    response = {"cells":cells, "state":self.state(), "sheet":sheet, "range":[r1,c1,r2,c2]}
                    response_bytes = len(encoded(response).encode())
                    for row, col, source, kind, style in self.range_cells(sheet,rows,c1,c2,deadline):
                        require(time.monotonic()<=deadline,"TIMEOUT","Read exceeded its time budget")
                        item = typed({"source": source, "kind": kind, "style": json.loads(style)})
                        result = {"row": row, "col": col, "source": source, "input_kind": kind, "kind": item["kind"], "style": item["style"]}
                        try:
                            value = calc.cell(sheet, row, col) if item["kind"] == "formula" else item["value"]
                            result.update(value=value, display=display(value, item["style"]), calculation="current")
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
                    targets={key:result["sheets"][index]["cells"] for index,(key,_) in enumerate(catalog)}
                    calc=self.calculator(seconds=2); size=0
                    for count,(sheet,row,col,source,kind,style) in enumerate(rows,1):
                        require(count<=MAX_CELLS,"LIMIT","Workbook export exceeds 10,000 stored cells")
                        require(time.monotonic()<=deadline,"TIMEOUT","Workbook export exceeded its time budget")
                        item=typed({"source":source,"kind":kind,"style":json.loads(style)})
                        cell={"row":row,"col":col,"source":source,"kind":item["kind"],"style":item["style"]}
                        try:
                            value=calc.cell(sheet,row,col) if item["kind"]=="formula" else item["value"]
                            cell.update(value=value,calculation="current")
                        except FormulaError as error:
                            cell.update(value=None,calculation="error",error=str(error))
                        if item["kind"]=="formula":
                            for identity,name in catalog:
                                cell["source"]=rename_sheet_references(cell["source"],identity,name)
                        size+=len(encoded(cell).encode())
                        require(size<=4*1024*1024,"READ_LIMIT","Workbook snapshot exceeds 4 MiB")
                        targets[sheet].append(cell)
                    return result
            finally:
                self.db.execute("ROLLBACK")

    def import_workbook(self,request):
        from .interchange import validate_sheet_names
        require(not self.handler,"UNSUPPORTED_HOOK_SCOPE","Whole-workbook import with required handlers needs a structure-event contract")
        require(self.db.execute("SELECT 1 FROM cells LIMIT 1").fetchone() is None,"DESTINATION_NOT_EMPTY","Whole-workbook import requires an empty destination; create a new workbook")
        sheets=request.get("sheets")
        require(isinstance(sheets,list) and all(isinstance(s,dict) for s in sheets),"INVALID_ARGUMENT","sheets must be an array of sheet objects")
        validate_sheet_names([s.get("name") for s in sheets])
        require(all(isinstance(s.get("cells"),list) for s in sheets),"INVALID_ARGUMENT","Each sheet needs cells")
        require(sum(len(s["cells"]) for s in sheets)<=MAX_CELLS,"LIMIT","Workbook import exceeds 10,000 stored cells")
        self.db.execute("DELETE FROM sheets")
        for index,sheet in enumerate(sheets):
            identity=str(uuid.uuid4())
            self.db.execute("INSERT INTO sheets VALUES(?,?,?)",(identity,sheet["name"],index))
            if sheet["cells"]: self.apply_edit(identity,sheet["cells"])

    def put(self, sheet, row, col, item):
        integer(row, 1, MAX_ROWS, "row")
        integer(col, 1, MAX_COLS, "col")
        if item is not None:
            typed(item)
            if item == {"source": "", "kind": "auto", "style": {}}:
                item = None
        key = (sheet, row, col)
        before = self.raw(*key)
        if time.monotonic() > self.deadline:
            raise EngineError("TIMEOUT", "Operation exceeded its time budget")
        if before == item:
            return
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

    def apply_edit(self, sheet, edits, *, preserve_inputs=False):
        require(isinstance(edits, list) and 0 < len(edits) <= MAX_CELLS, "LIMIT", "Provide 1 to 10,000 edits")
        for edit in edits:
            require(isinstance(edit, dict), "INVALID_ARGUMENT", "Each cell edit must be an object")
            for flag in ("clear", "clear_input", "replace_style"):
                boolean(edit.get(flag, False), flag)
            row, col = edit.get("row"), edit.get("col")
            integer(row, 1, MAX_ROWS, "row")
            integer(col, 1, MAX_COLS, "col")
            item = self.raw(sheet, row, col) or {"source": "", "kind": "auto", "style": {}}
            if edit.get("clear"):
                item = None
            else:
                if edit.get("clear_input"):
                    item.update(source="", kind="auto")
                if "source" in edit:
                    item.update(source=edit["source"], kind=edit.get("kind", "auto"))
                if "style" in edit:
                    style=validate_style(edit["style"])
                    item["style"] = style if edit.get("replace_style") else validate_style({**item["style"], **style})
                # Text format affects subsequent entry, never retypes an existing
                # number/formula merely because its presentation changed.
                if not preserve_inputs and "source" in edit and item["source"] != "" and item["kind"] == "auto" and item["style"].get("format") == "text":
                    item["kind"] = "text"
                elif not preserve_inputs and "source" in edit and item["kind"] == "auto" and isinstance(item["source"], str) and item["source"].startswith("="):
                    item["kind"] = "formula"
            self.put(sheet, row, col, item)

    def run_hooks(self, operation):
        if not self.handler:
            return
        for _ in range(16):
            if not self.pending:
                return
            batch = [self.pending[key] for key in sorted(self.pending)]
            self.pending = {}

            def dispatch(method, args):
                sheet = self.sheet(args["sheet"])
                row, col = args["row"], args["col"]
                integer(row, 1, MAX_ROWS, "row")
                integer(col, 1, MAX_COLS, "col")
                if method == "get":
                    return self.raw(sheet, row, col)
                if method == "value":
                    return self.calculator().cell(sheet, row, col)
                require(method == "set", "INVALID_HOOK", "Unknown handler context method")
                self.apply_edit(sheet, [{**args, "row": row, "col": col}])
                return None

            invoke(self.handler, batch, operation, dispatch, self.deadline)
        require(not self.pending, "HOOK_RECURSION", "Required handler exceeded 16 change rounds")

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

    def mutate(self, request):
        require(isinstance(request, dict), "INVALID_ARGUMENT", "Operation must be an object")
        operation = request.get("operation_id")
        require(isinstance(operation, str) and len(operation) <= 100 and bool(operation), "OPERATION_ID", "An operation_id is required")
        try:
            operation.encode("utf-8")
        except UnicodeEncodeError as error:
            raise EngineError("OPERATION_ID","operation_id must be valid Unicode text") from error
        fingerprint = hashlib.sha256(encoded(request).encode()).hexdigest()
        with self.lock:
            try:
                self.verify_file()
                receipt = self.recorded_operation(operation, fingerprint)
                if receipt is not None:
                    return receipt
                require(not self.readonly, "READ_ONLY", "Workbook is open read-only", 403)
                self.deadline = time.monotonic() + 5
                self.refresh(deadline=self.deadline)
                self.changes, self.pending = {}, {}
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
            try:
                self.db.execute("SAVEPOINT operation_changes")
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
                structural = isinstance(kind, str) and (kind.startswith("sheet_") or kind == "workbook_import" or kind in ("rows_insert", "rows_delete", "columns_insert", "columns_delete"))
                reference_report = {}
                sheet = self.sheet(request.get("sheet", "Sheet1")) if kind in ("edit", "paste", "move", "fill", "replace_all") else None
                if kind == "edit":
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
                                    input_kind = "null" if stored is None else "boolean" if type(stored) is bool else "number" if isinstance(stored, (int, float)) else "text"
                                else:
                                    require(isinstance(source,str),"INVALID_ARGUMENT","Clipboard source must be text")
                                    input_kind = value.get("input_kind", "text")
                                    if input_kind in ("auto", "formula") and source.startswith("="):
                                        source = translate(source, start_row-origin_row, start_col-origin_col)
                                if not edit.get("clear_input"):
                                    edit.update(source=source, kind=input_kind)
                            if mode != "values":
                                edit["style"] = value.get("style", {})
                                edit["replace_style"] = True
                            edits.append(edit)
                    self.apply_edit(sheet, edits, preserve_inputs=mode=="all")
                elif kind == "fill":
                    r1, c1, r2, c2 = rectangle(request)
                    source = self.raw(sheet, r1, c1)
                    mode = request.get("mode", "copy")
                    require(mode in ("copy", "series"), "INVALID_ARGUMENT", "Fill mode must be copy or series")
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
                            item = dict(source) if source else None
                            if mode == "series":
                                item["source"] = str(seed["value"] + step*((row-r1)*(c2-c1+1)+col-c1))
                                item["kind"] = "number"
                            elif item and typed(item)["kind"] == "formula":
                                item["source"] = translate(item["source"], row-r1, col-c1)
                            self.put(sheet, row, col, item)
                elif kind == "move":
                    require(request.get("mode", "all") == "all", "UNSUPPORTED_PASTE_MODE", "Cut supports full paste only; use Copy for special paste")
                    r1, c1, r2, c2 = rectangle(request)
                    require(request.get("cut_state") == self.state(), "STALE_CUT", "Cut source changed. Copy or cut the range again", 409)
                    with sql_deadline(self.db,self.deadline):
                        require(self.db.execute("SELECT 1 FROM cells WHERE source LIKE '=%' AND kind IN ('auto','formula') LIMIT 1").fetchone() is None,
                                "REFERENCE_TRANSFORM_UNAVAILABLE", "Cut is unavailable while the workbook contains formulas; use Copy")
                    dest_row, dest_col = request.get("row"), request.get("col")
                    integer(dest_row, 1, MAX_ROWS-(r2-r1), "destination row")
                    integer(dest_col, 1, MAX_COLS-(c2-c1), "destination column")
                    sources, source_bytes = [], 0
                    for row in range(r1,r2+1):
                        for col in range(c1,c2+1):
                            item=self.raw(sheet,row,col)
                            source_bytes+=len(encoded(item).encode())
                            require(source_bytes<=4*1024*1024,"LIMIT","Cut source exceeds 4 MiB; select a smaller range")
                            sources.append((row,col,item))
                    for row, col, _ in sources:
                        self.put(sheet, row, col, None)
                    for row, col, item in sources:
                        self.put(sheet, dest_row+row-r1, dest_col+col-c1, item)
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
                    for key, (before, after) in stack[-1][1].items():
                        wanted, current = (before, after) if kind == "undo" else (after, before)
                        require(self.raw(*key) == current, "HISTORY_CONFLICT", "Undo target changed", 409)
                        self.put(*key, wanted)
                elif kind in ("rows_insert", "rows_delete", "columns_insert", "columns_delete"):
                    reference_report = self.change_structure(request)
                elif kind == "workbook_import":
                    self.import_workbook(request)
                elif kind == "sheet_add":
                    name = request.get("name", "Sheet")
                    self.check_name(name)
                    self.db.execute("INSERT INTO sheets VALUES(?,?,?)", (str(uuid.uuid4()), name, self.next_sheet_position()))
                elif kind == "sheet_rename":
                    target = self.sheet(request.get("sheet"))
                    self.check_name(request.get("name"))
                    require(not self.handler, "UNSUPPORTED_HOOK_SCOPE", "Sheet rename with required handlers needs a structure-event contract")
                    self.rename_references(target, request["name"])
                    self.db.execute("UPDATE sheets SET name=? WHERE id=?", (request["name"], target))
                elif kind == "sheet_duplicate":
                    target = self.sheet(request.get("sheet"))
                    self.check_name(request.get("name"))
                    require(not self.handler, "UNSUPPORTED_HOOK_SCOPE", "Sheet duplication with required handlers needs a structure-event contract")
                    self.duplicate_sheet(target, request["name"])
                elif kind == "sheet_reorder":
                    order = request.get("order")
                    current = [row[0] for row in self.db.execute("SELECT id FROM sheets")]
                    require(isinstance(order, list) and all(isinstance(item,str) for item in order)
                            and len(order) == len(current) and set(order) == set(current), "INVALID_ARGUMENT", "Order must contain each sheet ID once")
                    self.db.executemany("UPDATE sheets SET position=? WHERE id=?", enumerate(order))
                elif kind == "sheet_delete":
                    target = self.sheet(request.get("sheet"))
                    require(self.db.execute("SELECT count(*) FROM sheets").fetchone()[0] > 1, "LAST_SHEET", "Cannot delete the last sheet")
                    require(not self.handler, "UNSUPPORTED_HOOK_SCOPE", "Sheet deletion with an activated required handler needs a structure-event contract")
                    reference_report = self.delete_sheet(target)
                else:
                    raise EngineError("UNSUPPORTED_OPERATION", f"Operation is not available: {kind}")
                self.run_hooks(operation)
                require(time.monotonic()<=self.deadline,"TIMEOUT","Operation exceeded its time budget")
                self.changes = {key: pair for key, pair in self.changes.items() if pair[0] != pair[1]}
                self.db.execute("UPDATE dh_meta SET revision=revision+1")
                result = {"operation_id": operation, "outcome": "committed", "state": self.state(), "changed_cells": len(self.changes)}
                result.update(reference_report)
                self.db.execute("INSERT INTO operations VALUES(?,?,?)", (operation, fingerprint, encoded(result)))
                require(time.monotonic()<=self.deadline,"TIMEOUT","Operation exceeded its time budget")
                self.verify_file()
                require(time.monotonic()<=self.deadline,"TIMEOUT","Operation exceeded its time budget")
                self.db.execute("COMMIT")
                in_transaction = False
                if structural:
                    self.undo.clear()
                    self.redo.clear()
                elif kind == "undo":
                    label, _, _ = self.undo.pop()
                    reversed_changes = {key: [pair[1], pair[0]] for key, pair in self.changes.items()}
                    self.redo.append((label, reversed_changes, self.history_cost(label,reversed_changes)))
                elif kind == "redo":
                    label, _, _ = self.redo.pop()
                    self.undo.append((label, self.changes, self.history_cost(label,self.changes)))
                elif self.changes:
                    self.redo.clear()
                    label = request.get("label",kind.title())
                    self.undo.append((label, self.changes, self.history_cost(label,self.changes)))
                    self.undo = self.undo[-100:]
                while sum(entry[2] for entry in self.undo + self.redo) > MAX_HISTORY_BYTES:
                    (self.undo if self.undo else self.redo).pop(0)
                return result
            except Exception as error:
                if not in_transaction:
                    raise EngineError("OUTCOME_UNKNOWN","Data may have committed; reconcile the durable receipt",500,"unknown",operation_id=operation) from error
                if not self.db.in_transaction:
                    raise EngineError("OUTCOME_UNKNOWN","Transaction outcome requires receipt lookup",500,"unknown",operation_id=operation) from error
                try:
                    # Keep the writer lock while reverting data and reserving
                    # this ID's failed outcome. Another service cannot steal it.
                    self.db.execute("ROLLBACK TO operation_changes")
                    self.db.execute("RELEASE operation_changes")
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

    @staticmethod
    def history_cost(label,changes):
        # Compute once per receipt; never serialize all prior history for each edit.
        return len(encoded([label,[[key, *values] for key, values in changes.items()]]).encode())

    def delete_sheet(self, target):
        target_name = self.db.execute("SELECT name FROM sheets WHERE id=?", (target,)).fetchone()[0]
        deleted, formulas, payload_bytes, warnings, locations = [], [], 0, 0, []
        with sql_deadline(self.db,self.deadline):
            for row, col, source, style in self.db.execute(
                    "SELECT row,col,source,style FROM cells WHERE sheet_id=? LIMIT ?",(target,MAX_CELLS+1)):
                require(time.monotonic()<=self.deadline,"TIMEOUT","Operation exceeded its time budget")
                require(len(deleted)<MAX_CELLS,"LIMIT","Sheet deletion exceeds 10,000 cells")
                payload_bytes += len(source.encode())+len(style.encode())+128
                require(payload_bytes<=8*1024*1024,"LIMIT","Sheet deletion sources exceed 8 MiB")
                deleted.append((target,row,col))
            for scanned, (sheet, name, row, col, source, kind, style) in enumerate(self.db.execute(
                    "SELECT c.sheet_id,s.name,c.row,c.col,c.source,c.kind,c.style FROM cells c JOIN sheets s ON s.id=c.sheet_id "
                    "WHERE c.sheet_id<>? AND c.source LIKE '=%' AND c.kind IN ('auto','formula')",(target,)),1):
                require(time.monotonic()<=self.deadline,"TIMEOUT","Operation exceeded its time budget")
                require(scanned<=MAX_CELLS,"LIMIT","Sheet reference scan exceeds 10,000 formulas")
                payload_bytes += len(source.encode())+len(style.encode())+128
                require(payload_bytes<=8*1024*1024,"LIMIT","Sheet deletion sources exceed 8 MiB")
                try:
                    transformed, broken = delete_sheet_references(source,name,target_name,target_id=target)
                except FormulaError as error:
                    raise EngineError("REFERENCE_TRANSFORM_UNAVAILABLE", f"Cannot safely transform {name}!{address(row,col)}: {error}") from error
                if transformed != source:
                    formulas.append(((sheet,row,col),{"source":transformed,"kind":kind,"style":json.loads(style)}))
                    warnings += broken
                    if len(locations)<100:
                        locations.append({"sheet":name,"row":row,"col":col})
            require(len(deleted)+len(formulas)<=MAX_CELLS,"LIMIT","Sheet deletion exceeds 10,000 affected cells")
            for key, item in formulas:
                self.put(*key,item)
            for key in deleted:
                self.put(*key,None)
            self.db.execute("DELETE FROM sheets WHERE id=?",(target,))
        return {"reference_warnings":warnings,"reference_issues":locations}

    def duplicate_sheet(self, target, new_name):
        old_name = self.db.execute("SELECT name FROM sheets WHERE id=?", (target,)).fetchone()[0]
        new_id, cells, payload_bytes = str(uuid.uuid4()), [], 0
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
                    except FormulaError as error:
                        raise EngineError("REFERENCE_TRANSFORM_UNAVAILABLE", f"Cannot safely copy {old_name}!{address(row,col)}: {error}") from error
                item = {"source":source,"kind":kind,"style":json.loads(style)}
                typed(item)
                cells.append((row,col,item))
            self.db.execute("INSERT INTO sheets VALUES(?,?,?)", (new_id,new_name,self.next_sheet_position()))
            for row, col, item in cells:
                self.put(new_id,row,col,item)

    def rename_references(self, target, new_name):
        old_name = self.db.execute("SELECT name FROM sheets WHERE id=?", (target,)).fetchone()[0]
        formulas, payload_bytes = [], 0
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
                except FormulaError as error:
                    raise EngineError("REFERENCE_TRANSFORM_UNAVAILABLE", f"Cannot safely transform {name}!{address(row,col)}: {error}") from error
                if transformed != source:
                    item = {"source":transformed,"kind":kind,"style":json.loads(style)}
                    typed(item)
                    formulas.append(((sheet,row,col),item))
            for key, item in formulas:
                self.put(*key,item)

    def change_structure(self, request):
        """Bounded whole-row/column changes, including references on other sheets."""
        require(not self.handler, "UNSUPPORTED_HOOK_SCOPE", "Row/column changes with required handlers need a structure-event contract")
        target = self.sheet(request.get("sheet", "Sheet1"))
        target_name = self.db.execute("SELECT name FROM sheets WHERE id=?", (target,)).fetchone()[0]
        axis = "row" if request["action"].startswith("rows_") else "col"
        maximum = MAX_ROWS if axis == "row" else MAX_COLS
        start = integer(request.get("start"), 1, maximum, "start")
        count = integer(request.get("count", 1), 1, maximum-start+1, "count")
        delete = request["action"].endswith("_delete")
        end = start+count-1
        moved, formulas, payload_bytes, warnings, locations = {}, {}, 0, 0, []
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
                require(time.monotonic()<=self.deadline,"TIMEOUT","Operation exceeded its time budget")
                require(scanned<=MAX_CELLS, "LIMIT", "Structure reference scan exceeds 10,000 formulas; a large-job path is not available")
                key = (sheet,row,col)
                if key in moved and moved[key][0] is None:
                    continue
                payload_bytes += len(source.encode())+len(style.encode())+128
                require(payload_bytes<=8*1024*1024, "LIMIT", "Structure sources exceed 8 MiB")
                try:
                    transformed, broken = transform_structure(source,name,target_name,axis,start,count,delete,target_id=target)
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
                self.put(*key,None)
            for destination, item in moved.values():
                if destination is not None:
                    self.put(*destination,item)
            for key, item in formulas.items():
                self.put(*key,item)
        return {"reference_warnings":warnings,"reference_issues":locations}

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

    def list_view(self, sheet, args):
        """An explicit list is a presentation projection over stable source rows."""
        r1, c1, r2, c2 = rectangle(args)
        with self.lock:
            self.refresh()
            self.db.execute("BEGIN")
            try:
                self.refresh()
                sheet, calc = self.sheet(sheet), self.calculator(seconds=2)
                criteria, keys = args.get("filters", []), args.get("sort", [])
                require(isinstance(criteria, list) and len(criteria) <= 8 and isinstance(keys, list) and len(keys) <= 8,
                        "INVALID_ARGUMENT", "At most eight filters and sort keys are supported")
                require(all(isinstance(item,dict) for item in criteria+keys),"INVALID_ARGUMENT","Each filter and sort key must be an object")
                for criterion in criteria:
                    integer(criterion.get("col"), c1, c2, "filter column")
                    require(criterion.get("op") in ("=", "<>", ">", "<", ">=", "<=", "contains"), "INVALID_ARGUMENT", "Unsupported filter comparison")
                    require(isinstance(criterion.get("value"), (str, float, int, bool, type(None))), "INVALID_ARGUMENT", "Filter value must be scalar")
                for key in keys:
                    integer(key.get("col"), c1, c2, "sort column")
                    require(type(key.get("descending", False)) is bool, "INVALID_ARGUMENT", "Sort direction must be boolean")
                require(type(args.get("header", False)) is bool, "INVALID_ARGUMENT", "Header must be explicit boolean")
                first = r1 + int(args.get("header", False))
                rows, cache = [], {}

                def value(row, col):
                    if (row, col) not in cache:
                        cache[row, col] = calc.cell(sheet, row, col)
                    return cache[row, col]

                def accepts(row, criterion):
                    left, right, op = value(row, criterion["col"]), criterion.get("value"), criterion["op"]
                    if op == "contains":
                        return isinstance(left, str) and isinstance(right, str) and right in left
                    same_type = type(left) == type(right) or (type(left) in (int, float) and type(right) in (int, float))
                    if not same_type:
                        return op == "<>"
                    if op in ("<", ">", "<=", ">=") and left is None:
                        return False
                    return {"=": lambda: left == right, "<>": lambda: left != right,
                            ">": lambda: left > right, "<": lambda: left < right,
                            ">=": lambda: left >= right, "<=": lambda: left <= right}[op]()

                for row in range(first, r2+1):
                    if all(accepts(row, criterion) for criterion in criteria):
                        rows.append(row)

                def sort_value(value):
                    rank = 0 if value is None else 1 if type(value) in (int, float) else 2 if isinstance(value, str) else 3
                    return rank, "" if value is None else value

                def compare(left, right):
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
            except FormulaError as error:
                raise EngineError("CALCULATION_ERROR", f"List evaluation blocked: {error}") from error
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

    def find(self, sheet, args):
        with self.lock:
            self.refresh()
            self.db.execute("BEGIN")
            try:
                self.refresh()
                matches = self.matches(self.sheet(sheet), args)
                return {"matches": [{**match, "text":match["text"][:256]} for match in matches[:500]], "count": len(matches), "state": self.state(), "truncated": len(matches) > 500}
            finally:
                self.db.execute("ROLLBACK")

    def snapshot(self, path):
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
                    require(time.monotonic()<=deadline,"TIMEOUT","Snapshot exceeded its time budget; the destination is preserved for inspection")
                self.db.backup(target,pages=256,progress=progress,sleep=0.01)
                _verify_destination(destination,identity)
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
                for row in range(r1, r2+1):
                    # Values export uses canonical text, independent of number format.
                    row_output = io.StringIO(newline="")
                    csv.writer(row_output).writerow([interchange_text(calc.cell(sheet, row, col)) for col in range(c1, c2+1)])
                    text = row_output.getvalue()
                    output_bytes += len(encoded(text).encode())-2  # Exclude each string's enclosing quotes.
                    require(output_bytes <= 4*1024*1024, "LIMIT", "CSV response exceeds 4 MiB; export a smaller selection")
                    output.write(text)
                result["csv"] = output.getvalue()
                require(len(encoded(result).encode()) <= 4*1024*1024, "LIMIT", "CSV response exceeds 4 MiB; export a smaller selection")
                return result
            except FormulaError as error:
                raise EngineError("CALCULATION_ERROR", f"Export blocked: {error}") from error
            finally:
                self.db.execute("ROLLBACK")
