# SPDX-License-Identifier: GPL-3.0-only
"""Whole-workbook ODS reading and writing as streams: no sheet or row of unbounded size is held in memory.

The tree reader in ods stays capped at 10,000 cells. This reader applies the package and XML safety rules of
xlsx_stream (whose open_package, read_part, ByteBudget, progress counters and LARGE_* limits it shares), converts every
cell with the same ods.convert_ods_cell, and hands prepared cells to a sink in batches of at most 1,000. It reads
content.xml twice: a first pass collects the sheet names (which formula conversion needs for every sheet) and the
style definitions wherever they appear, so cells convert exactly as the tree reader converts them; the second pass
converts the cells. Within the tree reader's limits both produce the same sheets, cells and report entries.

The writer is the mirror of the bounded whole-workbook ODS export: every cell goes through the same
ods.export_ods_cell and every style through the same ods.OdsStyleTable. Automatic styles precede the body in
content.xml but are only known once every cell has been written, so the body is written to a scratch file first and
content.xml is then assembled, header, automatic styles and the copied body, while it is streamed into the archive.
"""
import hashlib
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from typing import BinaryIO, Callable

from . import xlsx_stream
from .conversion_report import ConversionReport
from .engine import EngineError, require
from .formula import address
from .interchange import mode, validate_sheet_names, workbook_report, xml
from .ods import (F, M, MIME, N, O, OF, STYLE_DEFINITION_TAGS, T, WORKBOOK_IMPORT_LINE, WHOLE_FORMULA_LINE, X, Y, OdsCellContext,
                  OdsStyles, OdsStyleTable, convert_ods_cell, export_lines as ods_export_lines, export_ods_cell, package_parts, q,
                  repeat, uses_modern_function)
from .temporal import export_temporal_style

MAX_ROW, MAX_COL = 1_048_576, 16_384
MAX_STYLE_DEFINITIONS = 65_536  # cell styles, data styles and font faces kept for conversion
STYLE_BYTES = 16*1024*1024  # text of the style definitions kept, measured as in xlsx_stream._deferred_copy
STYLE_OVERHEAD = 64  # bytes charged per kept element besides its text
REPEATED_ROW_BYTES = 16*1024*1024  # stored input held to repeat one row, measured likewise
ELEMENTS_PER_CHECKPOINT = 10_000

DOCUMENT, BODY, SPREADSHEET, TABLE = q(O, "document-content"), q(O, "body"), q(O, "spreadsheet"), q(T, "table")
ROW, CELL, COVERED = q(T, "table-row"), q(T, "table-cell"), q(T, "covered-table-cell")
ROW_GROUPS = frozenset((q(T, "table-row-group"), q(T, "table-header-rows"), q(T, "table-rows")))
RECORDS = STYLE_DEFINITION_TAGS | {CELL, COVERED}
SHEET_PATH = [DOCUMENT, BODY, SPREADSHEET, TABLE]
MANIFEST, ENCRYPTION = "META-INF/manifest.xml", q(M, "encryption-data")


def scope_lines(request) -> list[str]:
    """The fixed report lines of a whole-workbook ODS import; the conversion report's lines follow them."""
    return workbook_report()+[WORKBOOK_IMPORT_LINE]


def _check_package(archive, budget):
    """The ODS package rules: the exact mimetype as the first entry, stored; no encrypted entries in the manifest."""
    items = archive.infolist()
    require(bool(items) and items[0].filename == "mimetype" and items[0].compress_type == zipfile.ZIP_STORED
            and items[0].file_size == len(MIME), "INVALID_SPREADSHEET",
            "Package is not an ODS spreadsheet: its first entry must be the uncompressed spreadsheet mimetype")
    budget.charge(items[0].file_size)
    try: mimetype = archive.read("mimetype")
    except (zipfile.BadZipFile, OSError, EOFError) as error: raise EngineError("INVALID_SPREADSHEET", "Invalid ODS mimetype entry") from error
    require(mimetype == MIME.encode(), "INVALID_SPREADSHEET", "Package is not an ODS spreadsheet")
    names = archive.namelist()
    if MANIFEST in names:
        manifest = xlsx_stream._read_whole(archive, MANIFEST, budget)
        require(not any(item.tag == ENCRYPTION for item in manifest.iter()), "INVALID_SPREADSHEET",
                "Encrypted ODS packages are not supported")
    require("content.xml" in names, "INVALID_SPREADSHEET", "Invalid or missing XML part: content.xml")


def _size(item):
    """The UTF-8 size of an element's tags, attributes and text, plus STYLE_OVERHEAD per element."""
    utf8 = xlsx_stream._utf8
    return sum(STYLE_OVERHEAD+utf8(e.tag)+utf8(e.text)+utf8(e.tail)+sum(utf8(k)+utf8(v) for k, v in e.attrib.items()) for e in item.iter())


class _StyleCollector:
    """OdsStyles fed one definition at a time, within MAX_STYLE_DEFINITIONS and STYLE_BYTES."""
    def __init__(self):
        self.styles = OdsStyles([])
        self.bytes = 0

    def add(self, item):
        if not self.styles.add(item): return
        self.bytes += _size(item)
        require(self.styles.definitions() <= MAX_STYLE_DEFINITIONS, "LIMIT",
                f"ODS styles exceed {MAX_STYLE_DEFINITIONS:,} definitions")
        require(self.bytes <= STYLE_BYTES, "LIMIT", f"ODS style definitions exceed {STYLE_BYTES//(1024*1024)} MiB")


def _survey(archive, budget, checkpoint):
    """First pass over content.xml: the sheet names in order, and every style definition (styles.xml first)."""
    collector = _StyleCollector()
    if "styles.xml" in archive.namelist():
        root = xlsx_stream._read_whole(archive, "styles.xml", budget)
        for item in root.iter():
            if item.tag in STYLE_DEFINITION_TAGS: collector.add(item)
    names, tags, elements = [], [], 0
    for event, item in xlsx_stream.read_part(archive, "content.xml", budget, record_tags=RECORDS):
        if event == "start":
            if not tags: require(item.tag == DOCUMENT, "INVALID_SPREADSHEET", "Invalid ODS content")
            tags.append(item.tag)
            if item.tag == TABLE and tags == SHEET_PATH: names.append(item.get(q(T, "name"), ""))
            elements += 1
            if elements % ELEMENTS_PER_CHECKPOINT == 0: checkpoint(0, "Reading styles and sheets")
            continue
        tags.pop()
        # Wherever it appears, as the tree reader collects them.
        if item.tag in STYLE_DEFINITION_TAGS: collector.add(item)
    return names, collector.styles


class _SheetReader:
    """Converts the cells of one sheet as the tree reader does, emitting them to the sink in row-major order."""
    def __init__(self, sink, context, progress):
        self.sink, self.context, self.progress = sink, context, progress
        self.batch = []
        self.row = 1

    def flush(self):
        if self.batch: self.sink.cells(self.batch[:]); self.batch.clear()

    def emit(self, edit):
        self.progress.cell()
        self.batch.append(edit)
        if len(self.batch) == xlsx_stream.BATCH: self.flush()
        self.progress.step(self.flush, "Reading cells")

    def start_row(self, element):
        self.repeats = repeat(element, "number-rows-repeated", MAX_ROW)
        self.col, self.stored, self.before = 1, 0, self.progress.cells
        # The stored cells of a repeated row, as (first column, column count, edit) runs, and the input they hold.
        self.template, self.template_bytes = [], 0

    def cell(self, element):
        count = repeat(element, "number-columns-repeated", MAX_COL)
        col, self.col = self.col, self.col+count
        meaningful = (element.get(q(O, "value-type")) is not None or element.get(q(T, "formula")) is not None
                      or element.get(q(T, "style-name")) is not None or element.find(q(X, "p")) is not None)
        if element.tag != CELL or not meaningful: return
        # Only a cell that may be stored must lie on the grid; empty runs past its edge store nothing.
        require(self.row+self.repeats-1 <= MAX_ROW, "LIMIT", "ODS rows exceed the native grid")
        require(col+count-1 <= MAX_COL, "LIMIT", "ODS columns exceed the native grid")
        context = self.context
        context.row, context.col, context.count = self.row, col, self.repeats*count
        edit = convert_ods_cell(element, context=context)
        if edit is None: return
        # Checked before any copy is made: a repeat is never materialised past the limit.
        require(self.before+self.repeats*(self.stored+count) <= xlsx_stream.LARGE_CELLS, "LIMIT",
                f"Repeated ODS cells exceed {xlsx_stream.LARGE_CELLS:,} stored cells")
        self.stored += count
        if self.repeats > 1:
            self.template.append((col, count, edit))
            self.template_bytes += xlsx_stream.DEFERRED_OVERHEAD+xlsx_stream._utf8(edit["source"])
            require(self.template_bytes <= REPEATED_ROW_BYTES, "LIMIT",
                    f"A repeated ODS row holds more than {REPEATED_ROW_BYTES//(1024*1024)} MiB of cell input")
        for offset in range(count): self.emit({**edit, "col": col+offset})

    def end_row(self):
        for offset in range(1, self.repeats if self.template else 1):
            for col, count, edit in self.template:
                for column in range(col, col+count): self.emit({**edit, "row": self.row+offset, "col": column})
        self.row += self.repeats


def _read_sheets(archive, names, styles, base, sink, progress, conversions):
    """Second pass over content.xml: convert every sheet's cells into the sink."""
    tags, elements = [], 0
    sheet = reader = None
    row_depth = None
    index = 0
    budget = xlsx_stream.ByteBudget(xlsx_stream.LARGE_EXPANDED)  # the same bytes the first pass already bounded
    for event, item in xlsx_stream.read_part(archive, "content.xml", budget, record_tags=RECORDS):
        if event == "start":
            tags.append(item.tag)
            elements += 1
            if elements % ELEMENTS_PER_CHECKPOINT == 0:
                if reader is not None: reader.flush()
                progress.checkpoint(progress.cells, "Reading cells")
            if item.tag == TABLE and tags == SHEET_PATH:
                name = names[index]; index += 1
                progress.checkpoint(progress.cells, "Reading sheet "+name)
                sheet = styles.report = ConversionReport()
                context = OdsCellContext(report=sheet, sheet=name, styles=styles, **base)
                sink.sheet(name)
                reader = _SheetReader(sink, context, progress)
            elif (item.tag == ROW and reader is not None and row_depth is None and len(tags) >= 5 and tags[:4] == SHEET_PATH
                  and all(tag in ROW_GROUPS for tag in tags[4:-1])):
                row_depth = len(tags)
                reader.start_row(item)
            continue
        tags.pop()
        if row_depth is not None:
            if len(tags) == row_depth and item.tag in (CELL, COVERED): reader.cell(item)
            elif len(tags) == row_depth-1:
                reader.end_row(); row_depth = None
        elif item.tag == TABLE and tags == SHEET_PATH[:3] and reader is not None:
            reader.flush()
            conversions.merge(sheet)
            reader = None
    require(index == len(names), "INVALID_SPREADSHEET", "ODS content changed between reads")


def read_workbook(path: Path | BinaryIO, request: dict, sink, checkpoint: Callable[[int, str], None]) -> ConversionReport:
    """Read every sheet of the ODS package at `path` (or in an open binary file) into `sink` at original coordinates;
    return the report. `sink` is an xlsx_stream.Sink; ODS has no shared strings, so its string store is unused.

    Within the tree reader's limits, the sheets, cells and report entries agree with ods.ods_preview at workbook scope.
    Beyond them: up to LARGE_SHEETS sheets and LARGE_CELLS stored cells, and runs of empty rows or cells may extend past
    the native grid (a trailing empty row repeated 1,048,576 times costs nothing); a cell holding content or a style must
    lie on the grid with all its repeats (LIMIT)."""
    selected_mode = mode(request)
    policy = request.get("timezone_policy", "reject")
    require(policy in ("reject", "utc"), "INVALID_ARGUMENT", "Choose reject or utc timestamp policy")
    require(request.get("row", 1) == 1 and request.get("col", 1) == 1, "INVALID_ARGUMENT", "Whole-workbook import uses original coordinates")
    budget = xlsx_stream.ByteBudget(xlsx_stream.LARGE_EXPANDED)
    with xlsx_stream.open_package(path) as archive:
        checkpoint(0, "Reading workbook")
        _check_package(archive, budget)
        names, styles = _survey(archive, budget, checkpoint)
        names = validate_sheet_names(names, xlsx_stream.LARGE_SHEETS)
        progress = xlsx_stream._Progress(checkpoint)
        conversions = ConversionReport()
        base = {"mode": selected_mode, "sheet_names": names, "timezone_policy": policy}
        _read_sheets(archive, names, styles, base, sink, progress, conversions)
        return conversions


# --- Writing ---------------------------------------------------------------------------------------------------

CACHE_CATEGORY = "formula.cache_omitted"
CACHE_DETAIL = "result not evaluated; written without a cached value, so the target must recalculate it"
FORMULA_LINE = ("OpenFormula uses canonical named sheet references; formula results are not evaluated during a large export, so "
                "formulas carry no cached values and the target must recalculate them. Sheet-relative movement semantics are not retained. NULL becomes empty.")
_ROOT = (f'<office:document-content xmlns:office="{O}" xmlns:style="{Y}" xmlns:text="{X}" xmlns:table="{T}" '
         f'xmlns:fo="{F}" xmlns:number="{N}" xmlns:of="{OF}" office:version="1.3">')
_WRAPPER = q(O, "wrapper")
_WRAPPER_END = "</office:wrapper>"


def export_lines(native: dict, modern_functions: bool, formulas: int, unevaluated_formulas: int) -> list[str]:
    """The fixed report lines of a whole-workbook streamed ODS export; the conversion report's lines follow them.
    The formula lines appear only when a formula was written."""
    head = workbook_report()+[FORMULA_LINE if formulas else WHOLE_FORMULA_LINE]
    if unevaluated_formulas: head.append(xlsx_stream.FORMULA_DISPLAY_LINE)
    return ods_export_lines(head, native, modern_functions)


def _markup(element):
    """`element` serialized exactly as it is inside a whole ElementTree document, without namespace declarations."""
    wrapper = ET.Element(_WRAPPER)
    wrapper.append(element)
    text = ET.tostring(wrapper, encoding="unicode")
    return text[text.index(">")+1:-len(_WRAPPER_END)]


class _Output:
    """Buffered UTF-8 text written to a binary file, every byte charged to the expanded budget."""
    def __init__(self, file, expanded):
        self.file, self.expanded = file, expanded
        self.pending, self.size = [], 0

    def emit(self, text):
        self.pending.append(text)
        self.size += len(text)
        if self.size >= xlsx_stream.EXPORT_FLUSH: self.flush()

    def flush(self):
        if not self.pending: return
        data = "".join(self.pending).encode("utf-8")
        self.pending.clear(); self.size = 0
        self.expanded.charge(len(data))
        self.file.write(data)


def _write_table(out, name, cells, context, progress):
    """One table:table, cell by cell, laid out exactly as ods.ods_export lays it out."""
    opening = _markup(ET.Element(TABLE, {q(T, "name"): name}))
    previous_row = previous_col = 0
    for cell in cells:
        progress.cell(name)
        cell = export_temporal_style(cell)
        row, col = cell["row"], cell["col"]
        if row != previous_row:
            out.emit(opening[:-len(" />")]+">" if previous_row == 0 else "</table:table-row>")
            if row-previous_row > 1: out.emit(f'<table:table-row table:number-rows-repeated="{row-previous_row-1}" />')
            out.emit("<table:table-row>"); previous_row = row; previous_col = 0
        if col-previous_col > 1: out.emit(f'<table:table-cell table:number-columns-repeated="{col-previous_col-1}" />')
        previous_col = col
        if cell["kind"] == "formula":
            context["formulas"] += 1
            context["modern"] = context["modern"] or uses_modern_function(cell["source"])
            where = {"sheet": name, "cell": address(row, col)}
            context["report"].add(CACHE_CATEGORY, "approximated", CACHE_DETAIL, **where)
            if cell.get("style", {}).get("format", "general") == "general":
                context["report"].add(xlsx_stream.FORMULA_DISPLAY_CATEGORY, "approximated", xlsx_stream.FORMULA_DISPLAY_DETAIL, **where)
                context["unevaluated"] += 1
        element, _ = export_ods_cell(cell, context["styles"].name(cell.get("style", {})), sheet=name, mode="formulas",
                                     report=context["report"], whole=True, names=context["names"], caches=False)
        out.emit(_markup(element))
    out.emit(opening if previous_row == 0 else "</table:table-row></table:table>")


def write_workbook(path: Path, sheets, report: ConversionReport, checkpoint: Callable[[int, str], None], *,
                   scratch: Callable[[], Path] | None = None) -> dict:
    """Write a whole workbook as an ODS package at `path`, formulas without cached values, cell by cell.

    `sheets` yields (sheet name, iterator of cells) as Workbook.stream_workbook does; only the pairs are taken up
    front, and each sheet's cells are read as they are written. Each cell is written by ods.export_ods_cell and each
    style named by ods.OdsStyleTable, exactly as the bounded whole-workbook export writes them, except that a formula
    carries no cached value (itemized in `report` as CACHE_CATEGORY, approximated) and an unformatted formula takes no
    calendar display (xlsx_stream.FORMULA_DISPLAY_CATEGORY, approximated). The body of content.xml is written to the
    file `scratch()` returns (an empty private file the caller owns and removes; by default a temporary file this call
    removes), then copied into the package after the automatic styles. More than LARGE_CELLS cells, LARGE_SHEETS sheets,
    LARGE_EXPANDED uncompressed bytes or 1,024 distinct styles is LIMIT. On any failure the partial file is removed and
    the scratch file emptied.

    Returns {"bytes", "sha256", "sheets", "cells", "formulas", "modern_functions", "unevaluated_formulas"}."""
    path = Path(path)
    body = None
    owned = scratch is None
    try:
        sheets = list(sheets)
        require(len(sheets) <= xlsx_stream.LARGE_SHEETS, "LIMIT", f"Whole-workbook export exceeds {xlsx_stream.LARGE_SHEETS} sheets")
        names = validate_sheet_names([name for name, _ in sheets], xlsx_stream.LARGE_SHEETS)
        automatic = ET.Element(q(O, "automatic-styles"))
        context = {"styles": OdsStyleTable(automatic), "report": report, "names": names, "modern": False, "formulas": 0, "unevaluated": 0}
        expanded, progress = xlsx_stream._Expanded("ODS"), xlsx_stream._WriteProgress(checkpoint)
        if owned:
            descriptor, name = tempfile.mkstemp(prefix="ods-body-", suffix=".part")
            os.close(descriptor)
            body = Path(name)
        else:
            body = Path(scratch())
        flags = os.O_WRONLY|os.O_TRUNC|getattr(os, "O_NOFOLLOW", 0)|getattr(os, "O_BINARY", 0)
        with open(os.open(body, flags), "wb") as file:
            out = _Output(file, expanded)
            out.emit("<office:body><office:spreadsheet>")
            for name, cells in sheets:
                checkpoint(progress.cells, "Writing sheet "+name)
                _write_table(out, name, cells, context, progress)
            out.emit("</office:spreadsheet></office:body></office:document-content>")
            out.flush()
        checkpoint(progress.cells, "Writing workbook")
        head = xlsx_stream._DECLARATION+(_ROOT+_markup(automatic)).encode("utf-8")
        expanded.charge(len(head))
        flags = os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os, "O_NOFOLLOW", 0)|getattr(os, "O_BINARY", 0)
        with open(os.open(path, flags, 0o600), "wb") as output:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("mimetype", MIME, compress_type=zipfile.ZIP_STORED)
                with archive.open("content.xml", "w") as part, open(body, "rb") as source:
                    part.write(head)
                    while chunk := source.read(1024*1024):
                        part.write(chunk)
                        checkpoint(progress.cells, "Writing workbook")
                for part_name, root in package_parts():
                    data = xml(root)
                    expanded.charge(len(data))
                    archive.writestr(part_name, data)
        digest, size = hashlib.sha256(), 0
        with open(path, "rb") as written:
            while chunk := written.read(1024*1024):
                digest.update(chunk); size += len(chunk)
                checkpoint(progress.cells, "Checking export")
        return {"bytes": size, "sha256": digest.hexdigest(), "sheets": len(names), "cells": progress.cells,
                "formulas": context["formulas"], "modern_functions": context["modern"], "unevaluated_formulas": context["unevaluated"]}
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        if body is not None:
            if owned: body.unlink(missing_ok=True)
            else:
                try: os.truncate(body, 0)
                except OSError: pass
