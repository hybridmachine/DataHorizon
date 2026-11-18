# SPDX-License-Identifier: GPL-3.0-only
"""Admission and disk-backed execution for whole-row, column and sheet edits."""

from dataclasses import dataclass
import json
import time
import uuid

from .engine import BLANK_INPUT, EngineError, MAX_CELLS, encoded, input_signature, integer, require, sql_deadline, typed
from .formula import (
    MAX_COLS, MAX_ROWS, FormulaError, Unmaintainable, address, delete_sheet_references,
    has_sheet_reference, rename_sheet_references, transform_structure,
)


ACTIONS = ("rows_insert", "rows_delete", "columns_insert", "columns_delete", "sheet_duplicate", "sheet_delete")
SECONDS = 300
ORDINARY_BYTES = 8 * 1024 * 1024
# Below these totals an edit fits every ordinary budget without projecting it: at most
# 2,000 moved coordinates plus 1,000 rewrites, and history images under 8 MiB even if every
# character needs a 12-byte escape and every rewritten formula grows eightfold.
SMALL_CELLS = 1000
SMALL_CHARACTERS = 16384
FORMULA = "c.source LIKE '=%' AND c.kind IN ('auto','formula')"
NONBLANK = """NOT (source='' AND kind='auto' AND typeof(style)='text' AND
                   replace(replace(replace(replace(style,' ',''),char(9),''),char(10),''),char(13),'')='{}')"""
STAGE_SCHEMA = """(
    sheet_id TEXT NOT NULL, row INTEGER NOT NULL, col INTEGER NOT NULL,
    source TEXT NOT NULL, kind TEXT NOT NULL, style TEXT NOT NULL,
    PRIMARY KEY(sheet_id,row,col)
) WITHOUT ROWID"""
OLD = "temp.dh_structure_before"
NEW = "temp.dh_structure_after"
COPY_PREVIEW_ID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class Plan:
    target: str
    name: str
    axis: str | None = None
    start: int = 0
    count: int = 0
    delete: bool = False
    new_name: str | None = None

    @property
    def end(self):
        return self.start + self.count - 1

    @property
    def shift(self):
        return -self.count if self.delete else self.count

    def source_filter(self):
        if self.axis:
            return f"sheet_id=? AND {self.axis}>=?", (self.target, self.start)
        return "sheet_id=?", (self.target,)

    def check_grid(self, db):
        if self.axis and not self.delete:
            maximum = MAX_ROWS if self.axis == "row" else MAX_COLS
            overflow = db.execute(
                f"SELECT 1 FROM cells WHERE sheet_id=? AND {self.axis}>=? AND {self.axis}>? LIMIT 1",
                (self.target, self.start, maximum - self.count),
            ).fetchone()
            require(overflow is None, "GRID_OVERFLOW", "Insertion would discard stored cells beyond the grid edge")

    def destination(self, key, new_id):
        sheet, row, col = key
        if sheet != self.target:
            return key
        if self.new_name:
            return new_id, row, col
        if self.axis is None:
            return None
        value = row if self.axis == "row" else col
        if value < self.start:
            return key
        if self.delete and value <= self.end:
            return None
        if self.axis == "row":
            return sheet, row + self.shift, col
        return sheet, row, col + self.shift

    def origin(self, key, new_id):
        sheet, row, col = key
        if self.new_name and sheet == new_id:
            return self.target, row, col
        if self.axis == "row" and sheet == self.target and row >= self.start:
            return sheet, row - self.shift, col
        if self.axis == "col" and sheet == self.target and col >= self.start:
            return sheet, row, col - self.shift
        return key


def plan(book, request):
    action = request["action"]
    target = book.sheet(request.get("sheet", "Sheet1" if action.startswith(("rows_", "columns_")) else None))
    name = book.db.execute("SELECT name FROM sheets WHERE id=?", (target,)).fetchone()[0]
    if action == "sheet_delete":
        require(book.db.execute("SELECT count(*) FROM sheets").fetchone()[0] > 1, "LAST_SHEET", "Cannot delete the last sheet")
        return Plan(target, name, delete=True)
    if action == "sheet_duplicate":
        new_name = request.get("name")
        book.check_name(new_name)
        return Plan(target, name, new_name=new_name)
    axis = "row" if action.startswith("rows_") else "col"
    maximum = MAX_ROWS if axis == "row" else MAX_COLS
    start = integer(request.get("start"), 1, maximum, "start")
    count = integer(request.get("count", 1), 1, maximum-start+1, "count")
    return Plan(target, name, axis, start, count, action.endswith("_delete"))


def formula_rows(db, edit):
    clause, args = "", ()
    if edit.new_name:
        clause, args = " AND c.sheet_id=?", (edit.target,)
    elif edit.axis is None:
        clause, args = " AND c.sheet_id<>?", (edit.target,)
    return db.execute(
        "SELECT c.sheet_id,s.name,c.row,c.col,c.source,c.kind,c.style"
        f" FROM cells c JOIN sheets s ON s.id=c.sheet_id WHERE {FORMULA}{clause}", args,
    )


def sheet_order(db, edit):
    if not edit.axis:
        return ()
    return [name for (name,) in db.execute("SELECT name FROM sheets ORDER BY position,id")]


def rewrite(edit, sheet, name, row, col, source, kind, *, new_id=None, order=()):
    """Return the maintained source, stored kind, and new broken-reference count."""
    try:
        if edit.new_name:
            source = rename_sheet_references(source, edit.name, edit.new_name, target_id=edit.target, new_id=new_id)
            broken = 0
        elif edit.axis:
            source, broken = transform_structure(
                source, name, edit.name, edit.axis, edit.start, edit.count, edit.delete,
                target_id=edit.target, sheet_order=order,
            )
        else:
            source, broken = delete_sheet_references(source, name, edit.name, target_id=edit.target)
    except Unmaintainable:
        return source, "unmaintained" if (edit.axis and sheet == edit.target) or has_sheet_reference(source) else kind, 0
    except FormulaError as error:
        verb = "copy" if edit.new_name else "transform"
        raise EngineError("REFERENCE_TRANSFORM_UNAVAILABLE", f"Cannot safely {verb} {name}!{address(row,col)}: {error}") from error
    return source, kind, broken


def item(entry):
    if entry is None:
        return None
    source, kind, style = entry
    require(isinstance(style, str), "INVALID_INPUT", "Invalid externally stored cell metadata")
    try:
        return {"source": source, "kind": kind, "style": {} if style == "{}" else json.loads(style)}
    except ValueError as error:
        raise EngineError("INVALID_INPUT", "Invalid externally stored cell metadata") from error


def source_bytes(source, kind, style):
    require(all(isinstance(value, str) for value in (source, kind, style)),
            "INVALID_INPUT", "Invalid externally stored cell input")
    return len(source.encode()) + len(style.encode()) + 128


def history_bytes(before, after):
    if before == after:
        return 0
    try:
        return len(encoded([before, after]))  # encoded() escapes non-ASCII.
    except ValueError as error:
        raise EngineError("INVALID_INPUT", "Invalid externally stored cell input") from error


def insertion_bytes(record):
    return 0 if record == BLANK_INPUT else history_bytes(None, record)


def clearly_ordinary(db, edit, where, args):
    """True when bounded counts alone prove the edit is far inside the ordinary limits."""
    measure = "SELECT count(*),coalesce(sum(length(source)+length(style)),0) FROM (SELECT source,style FROM cells"
    cells, characters = db.execute(f"{measure} WHERE {where} LIMIT ?)", (*args, SMALL_CELLS + 1)).fetchone()
    if not edit.new_name:  # A copy rewrites only its own cells.
        clause, extra = ("", ()) if edit.axis else (" AND c.sheet_id<>?", (edit.target,))
        formulas, more = db.execute(f"{measure} c WHERE {FORMULA}{clause} LIMIT ?)", (*extra, SMALL_CELLS + 1)).fetchone()
        cells, characters = cells + formulas, characters + more
    return cells <= SMALL_CELLS and characters <= SMALL_CHARACTERS


def needs_large(book, request):
    """Find the ordinary path's exact admission boundary in a pinned read snapshot.

    Only bounded inputs are retained for the formula projection. Disjoint
    destinations and cross-sheet rewrites count toward the coordinate limit.
    No policy code, history writes or ID reservation run during this review.
    """
    if request.get("action") not in ACTIONS:
        return False
    deadline = time.monotonic() + 5
    with sql_deadline(book.db, deadline):
        edit = plan(book, request)
        edit.check_grid(book.db)
        where, args = edit.source_filter()
        if clearly_ordinary(book.db, edit, where, args):
            return False
        moved = {}
        size = 0
        for row, col, source, kind, style in book.db.execute(
            f"SELECT row,col,source,kind,style FROM cells WHERE {where} LIMIT ?", (*args, MAX_CELLS + 1),
        ):
            size += source_bytes(source, kind, style)
            if len(moved) == MAX_CELLS or size > ORDINARY_BYTES:
                return True
            key = (edit.target, row, col)
            moved[key] = source, kind, style

        if edit.new_name:
            formulas = ((edit.target, edit.name, row, col, *entry) for (_, row, col), entry in moved.items()
                        if entry[1] in ("auto", "formula") and entry[0].startswith("="))
        else:
            bounded = []
            for scanned, formula in enumerate(formula_rows(book.db, edit), 1):
                if scanned > MAX_CELLS:
                    return True
                sheet, _, row, col, source, kind, style = formula
                if edit.destination((sheet, row, col), None) is None:
                    continue
                size += source_bytes(source, kind, style)
                if size > ORDINARY_BYTES:
                    return True
                bounded.append(formula)
            formulas = bounded

        affected = set() if edit.new_name else set(moved)
        affected.update(destination for key in moved if (destination := edit.destination(key, COPY_PREVIEW_ID)) is not None)
        if len(affected) > MAX_CELLS:
            return True
        # A shift writes each source deletion and subsequent destination
        # insertion separately, even where the coordinate sets overlap.
        total = 0
        for key, entry in moved.items():
            record = item(entry)
            if not edit.new_name:
                total += history_bytes(record, None)
            if edit.destination(key, COPY_PREVIEW_ID) is not None:
                total += insertion_bytes(record)
        order = sheet_order(book.db, edit)
        for sheet, name, row, col, source, kind, style in formulas:
            require(time.monotonic() <= deadline, "TIMEOUT", "Structural edit review exceeded its time budget")
            key = (sheet, row, col)
            destination = edit.destination(key, COPY_PREVIEW_ID)
            new_source, new_kind, _ = rewrite(edit, sheet, name, row, col, source, kind,
                                             new_id=COPY_PREVIEW_ID, order=order)
            if (source, kind) != (new_source, new_kind):
                affected.add(destination)
                if len(affected) > MAX_CELLS:
                    return True
                original = item((source, kind, style))
                final = dict(original, source=new_source, kind=new_kind)
                if key in moved:
                    total += insertion_bytes(final) - insertion_bytes(original)
                else:
                    total += history_bytes(original, final)
        return total > ORDINARY_BYTES


class StagedEdit:
    """Per-transaction coordinate images, with bounded in-memory hook changes."""

    def __init__(self, book, edit, progress):
        self.book = book
        self.db = book.db
        self.edit = edit
        self.progress = progress
        self.new_id = str(uuid.uuid4()) if edit.new_name else None
        self.authored_before = book.structure_touched
        self.changed = 0

    def stage(self):
        edit = self.edit
        where, args = edit.source_filter()
        self.db.execute(f"CREATE TABLE {OLD} {STAGE_SCHEMA}")
        self.db.execute(f"CREATE TABLE {NEW} {STAGE_SCHEMA}")
        if not edit.new_name:
            self.db.execute(f"INSERT INTO {OLD} SELECT * FROM main.cells WHERE {where}", args)
        if edit.new_name:
            self.db.execute(
                f"INSERT INTO {NEW} SELECT ?,row,col,source,kind,style FROM main.cells WHERE {where} AND {NONBLANK}",
                (self.new_id, *args),
            )
        elif edit.axis:
            row = "row+?" if edit.axis == "row" else "row"
            col = "col+?" if edit.axis == "col" else "col"
            self.db.execute(
                f"INSERT INTO {NEW} SELECT sheet_id,{row},{col},source,kind,style"
                f" FROM {OLD} WHERE {edit.axis}>? AND {NONBLANK}",
                (edit.shift, edit.end if edit.delete else edit.start - 1),
            )

    def rewrite_formulas(self):
        edit = self.edit
        order = sheet_order(self.db, edit)
        marked_count, warnings = 0, 0
        marked, locations = [], []
        for sheet, name, row, col, source, kind, style in formula_rows(self.db, edit):
            self.book.check_operation()
            key = sheet, row, col
            destination = edit.destination(key, self.new_id)
            if destination is None:
                continue
            transformed, new_kind, broken = rewrite(
                edit, sheet, name, row, col, source, kind, new_id=self.new_id, order=order,
            )
            if (source, kind) == (transformed, new_kind):
                continue
            if new_kind == "unmaintained":
                marked_count += 1
                if len(marked) < 100:
                    marked.append({"sheet": edit.new_name or name, "cell": address(*destination[1:])})
            warnings += broken
            # A sheet-delete receipt historically lists every rewritten formula,
            # including rewrites for which the transform reports zero breaks.
            if (broken or edit.axis is None and edit.delete) and new_kind != "unmaintained" and len(locations) < 100:
                locations.append({"sheet": name, "row": destination[1], "col": destination[2]})
            if destination == key:
                self.db.execute(f"INSERT INTO {OLD} VALUES(?,?,?,?,?,?)", (*key, source, kind, style))
                self.db.execute(f"INSERT INTO {NEW} VALUES(?,?,?,?,?,?)", (*key, transformed, new_kind, style))
            else:
                self.db.execute(f"UPDATE {NEW} SET source=?,kind=? WHERE sheet_id=? AND row=? AND col=?",
                                (transformed, new_kind, *destination))
        result = {} if edit.new_name else {"reference_warnings": warnings, "reference_issues": locations}
        if marked_count:
            from .relational import ensure_version
            ensure_version(self.book, 7)
            result.update(unmaintained=marked, unmaintained_count=marked_count)
        return result

    def validate_cells(self):
        # Direct SQLite writes can evade managed input validation. Stream the
        # final staged records without turning the coordinate shift into
        # individual Python writes or retaining a cell-sized object graph.
        for entry in self.db.execute(f"SELECT source,kind,style FROM {NEW}"):
            self.book.check_operation()
            typed(item(entry))
        if self.edit.delete:
            where, args = self.edit.source_filter()
            if self.edit.axis:
                where += f" AND {self.edit.axis}<=?"
                args = (*args, self.edit.end)
            for entry in self.db.execute(f"SELECT source,kind,style FROM {OLD} WHERE {where}", args):
                self.book.check_operation()
                source_bytes(*entry)
                item(entry)

    def write_cells(self):
        edit = self.edit
        if edit.new_name:
            self.db.execute("INSERT INTO sheets VALUES(?,?,?)", (self.new_id, edit.new_name, self.book.next_sheet_position()))
        elif edit.axis:
            where, args = edit.source_filter()
            self.db.execute(f"DELETE FROM main.cells WHERE {where}", args)
        self.db.execute(
            f"INSERT INTO main.cells SELECT * FROM {NEW} WHERE true"
            " ON CONFLICT(sheet_id,row,col) DO UPDATE SET source=excluded.source,kind=excluded.kind,style=excluded.style"
        )

    def calculate_changed(self):
        join = "USING(sheet_id,row,col)"
        self.changed = self.db.execute(
            f"SELECT count(*) FROM {OLD} o LEFT JOIN {NEW} n {join}"
            " WHERE n.sheet_id IS NULL OR (o.source,o.kind,o.style) IS NOT (n.source,n.kind,n.style)"
        ).fetchone()[0]
        self.changed += self.db.execute(
            f"SELECT count(*) FROM {NEW} n LEFT JOIN {OLD} o {join} WHERE o.sheet_id IS NULL"
        ).fetchone()[0]
        # SQLite clients can write equivalent style JSON with different text
        # formatting. Use the same semantic comparison as the ordinary engine.
        for before, after in self.db.execute(
            f"SELECT o.style,n.style FROM {OLD} o JOIN {NEW} n {join}"
            " WHERE o.source=n.source AND o.kind=n.kind AND o.style<>n.style"
        ):
            self.book.check_operation()
            self.changed -= json.loads(before) == json.loads(after)

    def stage_pair(self, key):
        return tuple(item(self.db.execute(
            f"SELECT source,kind,style FROM {table} WHERE sheet_id=? AND row=? AND col=?", key,
        ).fetchone()) for table in (OLD, NEW))

    def transfer_pending(self):
        """Retain before-hook edits at their final coordinates, with ordinary baseline inputs."""
        if not self.authored_before:
            return
        edit = self.edit
        candidates = set(self.authored_before)
        for key in self.authored_before:
            if key[0] not in ("table", "chart"):
                destination = edit.destination(key, self.new_id)
                if destination is not None:
                    candidates.add(destination)
        for key in candidates:
            if key[0] in ("table", "chart"):
                continue
            old, new = self.stage_pair(key)
            if old is None and new is None:
                continue
            origin = edit.origin(key, self.new_id) if new is not None else key
            if key in self.authored_before:
                old = self.book.changes[key][0]
            if origin in self.authored_before and input_signature(old) != input_signature(new):
                sheet, row, col = key
                self.book.pending[key] = {"sheet": sheet, "row": row, "col": col, "before": old, "after": new}
            else:
                self.book.pending.pop(key, None)

    def before_image(self, key, current):
        """Use the transaction baseline for a change authored by the after event."""
        old, new = self.stage_pair(key)
        if old is None and new is None:
            return current
        return self.book.changes[key][0] if key in self.authored_before else old

    def apply(self):
        book, edit = self.book, self.edit
        with sql_deadline(self.db, book.deadline, cancel_event=book.cancel_event):
            self.progress("staging_cells")
            edit.check_grid(self.db)
            self.stage()
            self.progress("rewriting_references")
            report = self.rewrite_formulas()
            self.progress("validating_cells")
            self.validate_cells()
            self.progress("applying_cells")
            book.check_operation()
            self.calculate_changed()
            self.write_cells()
            self.transfer_pending()
            self.progress("updating_metadata")
            if edit.axis:
                report.update(axis_metadata(book, edit.target, edit.axis, edit.start, edit.count, edit.delete))
            elif edit.new_name:
                copy_metadata(book, edit.target, self.new_id)
            else:
                report.update(delete_metadata(book, edit.target))
                self.db.execute("DELETE FROM sheets WHERE id=?", (edit.target,))
        return report

    def finish(self):
        """Combine the SQL delta with the bounded cell writes authored by hooks."""
        book = self.book
        with sql_deadline(self.db, book.deadline, cancel_event=book.cancel_event):
            for key, pair in book.changes.items():
                if key[0] in ("table", "chart"):
                    continue
                book.check_operation()
                old, new = self.stage_pair(key)
                self.changed -= old != new
                before = pair[0] if key in self.authored_before or (old is None and new is None) else old
                self.changed += before != book.raw(*key)
            self.db.execute(f"DROP TABLE {OLD}")
            self.db.execute(f"DROP TABLE {NEW}")
        return self.changed


def axis_metadata(book, target, axis, start, count, delete):
    from . import bindings, charts, names
    args = book, target, axis, start, count, delete
    broken_names = names.adjust_structure(*args)
    broken_bindings = sorted(bindings.adjust_structure(*args) + bindings.scoped_to_names(book, broken_names))
    broken_charts, over_limit_charts = charts.adjust_structure(*args)
    return {key: value for key, value in (
        ("broken_names", broken_names), ("broken_charts", broken_charts),
        ("charts_over_limit", over_limit_charts), ("broken_bindings", broken_bindings),
    ) if value}


def delete_metadata(book, target):
    from . import bindings, charts, names
    broken_names = names.break_sheet(book, target)
    broken_bindings = sorted(bindings.break_sheet(book, target) + bindings.scoped_to_names(book, broken_names))
    broken_charts = charts.break_sheet(book, target)
    return {key: value for key, value in (
        ("broken_names", broken_names), ("broken_charts", broken_charts), ("broken_bindings", broken_bindings),
    ) if value}


def copy_metadata(book, target, new_id):
    if book.db.execute("SELECT format_version FROM dh_meta").fetchone()[0] >= 3:
        book.db.execute(
            "INSERT INTO dh_objects SELECT namespace,id,?,payload FROM dh_objects"
            " WHERE sheet_id=? AND namespace IN ('presentation','list')", (new_id, target),
        )
        require(book.db.execute(
            "SELECT count(*)<=256 AND coalesce(sum(length(payload)),0)<=16777216 FROM dh_objects"
        ).fetchone()[0], "LIMIT", "Native metadata exceeds its budget")
