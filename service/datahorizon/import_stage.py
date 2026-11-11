# SPDX-License-Identifier: GPL-3.0-only
"""A prepared whole-workbook import held in a private SQLite file for review and a later atomic apply.

Everything in the file comes from an uploaded package and is data only. The writer is a sink for
xlsx_stream.read_workbook; once finished, the file is read-only and can be opened from any thread or process.
The file is throwaway: it is written with journal_mode=OFF and synchronous=OFF, so a crash mid-write leaves it
unusable and it must be discarded (it is never an input to anything until finish has committed it)."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

from .conversion_report import ConversionReport
from .engine import EngineError, TEMPORAL_KINDS, encoded, require, typed
from .xlsx_stream import LARGE_CELLS, LARGE_SHEETS, scope_lines

VERSION = 1
SUMMARY_LIMIT = 1024*1024
# fit_summary measures compact JSON; the service's response encoding is not pinned here, so aim for 900 KiB to
# leave about 12% headroom for a more spacious serialisation.
SUMMARY_TARGET = 900*1024
MAX_EXTRA = 64*1024
SAMPLE_SOURCE = 256
SAMPLE_ENTRY_SAMPLES = 10
MAX_STYLES = 65_536
MAX_ROW, MAX_COL = 1_048_576, 16_384
SCHEMA = """CREATE TABLE meta(singleton INTEGER PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE sheets(position INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE styles(id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE cells(sheet INTEGER NOT NULL, row INTEGER NOT NULL, col INTEGER NOT NULL, source TEXT NOT NULL,
                   kind TEXT NOT NULL, style_id INTEGER NOT NULL, PRIMARY KEY(sheet, row, col)) WITHOUT ROWID;
CREATE TABLE strings(idx INTEGER PRIMARY KEY, text TEXT NOT NULL);"""
SELECT_CELLS = ("SELECT cells.row, cells.col, cells.source, cells.kind, styles.payload FROM cells "
                "JOIN styles ON styles.id = cells.style_id WHERE cells.sheet = ? ORDER BY cells.row, cells.col")


class StageReader:
    """Read access to a stage; the writer inherits it so a finished stage can be inspected in place."""
    db: sqlite3.Connection

    def _meta(self):
        row = self.db.execute("SELECT payload FROM meta WHERE singleton = 1").fetchone()
        if row is None: return None
        meta = json.loads(row[0])
        if not isinstance(meta, dict) or not isinstance(meta.get("summary", {}), dict): raise ValueError("bad meta")
        return meta

    def summary(self):
        meta = self._meta()
        require(meta is not None, "JOB_CONFLICT", "The stage is not finished", 409)
        return meta["summary"]

    def sheets(self):
        return [name for (name,) in self.db.execute("SELECT name FROM sheets ORDER BY position")]

    def count(self):
        return self.db.execute("SELECT count(*) FROM cells").fetchone()[0]

    def sheet_counts(self):
        """Stored cells per sheet, in sheet order."""
        return [total for (total,) in self.db.execute(
            "SELECT count(cells.row) FROM sheets LEFT JOIN cells ON cells.sheet = sheets.position "
            "GROUP BY sheets.position ORDER BY sheets.position")]

    def has_temporal(self):
        marks = ",".join("?"*len(TEMPORAL_KINDS))
        return self.db.execute(f"SELECT 1 FROM cells WHERE kind IN ({marks}) LIMIT 1", tuple(TEMPORAL_KINDS)).fetchone() is not None

    def iter_cells(self, sheet_position, batch=10_000):
        """Yield lists of (row, col, source, kind, style_json) for one sheet in row-major order."""
        require(type(batch) is int and batch >= 1, "INVALID_ARGUMENT", "Batch size must be a positive integer")
        cursor = self.db.execute(SELECT_CELLS, (sheet_position,))
        while rows := cursor.fetchmany(batch):
            yield rows

    def digest(self):
        """SHA-256 over the sheet names in order and every cell in primary-key order, with resolved styles."""
        hasher = hashlib.sha256(encoded(self.sheets()).encode())
        for position in range(len(self.sheets())):
            for rows in self.iter_cells(position):
                for row in rows:
                    hasher.update(b"\n"+json.dumps([position, *row], separators=(",", ":")).encode())
        return hasher.hexdigest()

    def sample(self, per_sheet=100, *, source_limit=None):
        """The first `per_sheet` cells of each sheet in row-major order, with resolved styles."""
        source = "cells.source" if source_limit is None else f"substr(cells.source, 1, {int(source_limit)+1})"
        result = []
        for position, name in self.db.execute("SELECT position, name FROM sheets ORDER BY position").fetchall():
            cells = []
            for row, col, text, kind, style in self.db.execute(
                    f"SELECT cells.row, cells.col, {source}, cells.kind, styles.payload FROM cells "
                    "JOIN styles ON styles.id = cells.style_id WHERE cells.sheet = ? "
                    "ORDER BY cells.row, cells.col LIMIT ?", (position, per_sheet)):
                cell = {"row": row, "col": col, "source": text, "kind": kind, "style": json.loads(style)}
                if source_limit is not None and len(text) > source_limit:
                    cell["source"], cell["truncated"] = text[:source_limit], True
                cells.append(cell)
            result.append({"sheet": name, "cells": cells})
        return result

    def close(self):
        self.db.close()

    def __enter__(self): return self

    def __exit__(self, *exc): self.close()


class _ReadOnlyStage(StageReader):
    def __init__(self, path):
        require(Path(path).is_file(), "JOB_NOT_FOUND", "Import stage is unavailable", 404)
        db = None
        try:
            db = self.db = sqlite3.connect(f"file:{quote(str(Path(path).resolve()))}?mode=ro", uri=True, check_same_thread=False)
            meta = self._meta()
        except (sqlite3.Error, ValueError) as error:
            if db is not None: db.close()
            raise EngineError("INVALID_STAGE", "The import stage is not a readable stage database", 409) from error
        if meta is None or meta.get("version") != VERSION:
            db.close()
            raise EngineError("JOB_CONFLICT", "The import stage is unfinished or of an unsupported version", 409)


def open_readonly(path):
    """Open a finished stage for reading only; safe from another thread or process."""
    return _ReadOnlyStage(path)


class _Strings:
    """The shared string table; entries are buffered and written in bulk."""
    def __init__(self, stage):
        self.stage, self.pending = stage, []

    def put(self, index, text):
        self.stage._writable()
        self.pending.append((index, text))
        if len(self.pending) >= 1000: self.flush()

    def flush(self):
        if self.pending:
            self.stage._execute_many("INSERT INTO strings(idx, text) VALUES (?, ?)", self.pending)
            self.pending = []

    def get(self, index):
        self.stage._writable()
        self.flush()
        row = self.stage.db.execute("SELECT text FROM strings WHERE idx = ?", (index,)).fetchone()
        if row is None: raise IndexError(index)
        return row[0]


class ImportStage(StageReader):
    """The sink for xlsx_stream.read_workbook: sheet(), cells(batch) and strings.put/get."""

    def __init__(self, path):
        path = Path(path)
        require(path.is_file(), "JOB_NOT_FOUND", "Import stage is unavailable", 404)
        self.path, self.finished, self._failed = path, False, False
        db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        try:
            self.db = db
            db.execute("PRAGMA journal_mode = OFF")
            db.execute("PRAGMA synchronous = OFF")
            db.execute("PRAGMA cache_size = -16384")
            self._strings = _Strings(self)
            db.execute("BEGIN")
            if path.stat().st_size == 0:
                for statement in SCHEMA.split(";\n"):
                    db.execute(statement)
            else:
                self.finished = self._meta() is not None
            self._styles = {payload: ident for ident, payload in db.execute("SELECT id, payload FROM styles")}
            self._sheet_count = db.execute("SELECT count(*) FROM sheets").fetchone()[0]
            self._count = self.count()
        except (sqlite3.Error, ValueError) as error:
            db.close()
            raise EngineError("INVALID_STAGE", "The file is not a usable import stage database", 409) from error
        except BaseException:
            db.close()
            raise

    open_readonly = staticmethod(open_readonly)

    @property
    def strings(self):
        return self._strings

    def _writable(self):
        require(not self.finished, "JOB_CONFLICT", "The import stage is finished and read-only", 409)
        require(not self._failed, "JOB_CONFLICT", "The import stage failed and must be discarded", 409)

    def _execute_many(self, statement, rows):
        try:
            self.db.executemany(statement, rows)
        except sqlite3.IntegrityError as error:
            self._failed = True
            raise EngineError("INVALID_SPREADSHEET", "Duplicate cell coordinates or string indexes in the workbook") from error

    def sheet(self, name):
        self._writable()
        require(isinstance(name, str) and name, "INVALID_SPREADSHEET", "Sheet names must be nonempty text")
        require(self._sheet_count < LARGE_SHEETS, "LIMIT", f"Whole-workbook import exceeds {LARGE_SHEETS} sheets")
        self.db.execute("INSERT INTO sheets(position, name) VALUES (?, ?)", (self._sheet_count, name))
        self._sheet_count += 1

    def cells(self, batch):
        self._writable()
        require(self._sheet_count > 0, "INVALID_SPREADSHEET", "Cells arrived before any sheet")
        require(self._count+len(batch) <= LARGE_CELLS, "LIMIT", f"Whole-workbook import exceeds {LARGE_CELLS:,} stored cells")
        sheet, rows = self._sheet_count-1, []
        for cell in batch:
            row, col = cell["row"], cell["col"]
            require(type(row) is int and type(col) is int and 1 <= row <= MAX_ROW and 1 <= col <= MAX_COL,
                    "INVALID_SPREADSHEET", "Cell coordinates are outside the sheet")
            typed(cell)
            style = encoded(cell["style"])
            style_id = self._styles.get(style)
            if style_id is None:
                require(len(self._styles) < MAX_STYLES, "LIMIT", f"Whole-workbook import exceeds {MAX_STYLES:,} distinct styles")
                style_id = self._styles[style] = len(self._styles)
                self.db.execute("INSERT INTO styles(id, payload) VALUES (?, ?)", (style_id, style))
            rows.append((sheet, row, col, cell["source"], cell["kind"], style_id))
        self._execute_many("INSERT INTO cells(sheet, row, col, source, kind, style_id) VALUES (?, ?, ?, ?, ?, ?)", rows)
        self._count += len(rows)

    def finish(self, report: ConversionReport, extra: dict) -> dict:
        """Drop the string table, seal the stage and return the review summary (always under 1 MiB).

        Nothing is dropped until the summary is built and validated; any failure marks the stage failed."""
        self._writable()
        try:
            self._strings.flush()
            summary = {"scope": "workbook", "stage_id": self.stage_id(), "stage_digest": None, "sheets": [],
                       "stored_cells": self._count, "report": scope_lines({})+report.lines(),
                       "report_entries": report.entries(), "totals": report.totals(), "sample": [], "can_import": True}
            clash = set(extra) & set(summary)
            if clash: raise ValueError(f"extra must not override summary keys: {sorted(clash)}")
            require(len(encoded(extra).encode()) <= MAX_EXTRA, "LIMIT", "Import summary extras exceed 64 KiB")
            summary["stage_digest"] = self.digest()
            summary["sheets"] = [{"name": name, "stored_cells": total} for name, total in zip(self.sheets(), self.sheet_counts())]
            summary.update(extra)
            summary["sample"] = self.sample(100, source_limit=SAMPLE_SOURCE)
            summary = fit_summary(summary)
            payload = json.dumps({"version": VERSION, "summary": summary})
            # No VACUUM and no in-memory temp store: either would rebuild the whole file in RAM.
            self.db.execute("DROP TABLE strings")
            self.db.execute("INSERT INTO meta(singleton, payload) VALUES (1, ?)", (payload,))
            self.db.execute("COMMIT")
        except BaseException:
            self._failed = True
            raise
        self.finished = True
        return summary

    def stage_id(self):
        stem = self.path.name.split(".")[0]
        return stem[len("stage-"):] if stem.startswith("stage-") else stem


def fit_summary(summary):
    """Trim the summary below SUMMARY_LIMIT: entry samples, then sampled cells. Totals and counts are kept."""
    def size(): return len(json.dumps(summary, separators=(",", ":")).encode())
    if size() <= SUMMARY_TARGET: return summary
    summary["report_entries"] = [{**entry, "samples": entry["samples"][:SAMPLE_ENTRY_SAMPLES]} for entry in summary["report_entries"]]
    keep = max((len(item["cells"]) for item in summary["sample"]), default=0)
    while size() > SUMMARY_TARGET and keep > 0:
        keep //= 2
        for item in summary["sample"]:
            item["cells"] = item["cells"][:keep]
    require(size() <= SUMMARY_TARGET, "LIMIT", "The review summary exceeds 1 MiB")
    return summary
