# SPDX-License-Identifier: GPL-3.0-only
"""Whole-workbook XLSX reading as a stream: no sheet or shared-string table is held in memory.

The tree reader in interchange parses each part whole and stays capped at 10,000 cells. This reader
applies the same package and XML safety rules, converts every cell with the same convert_xlsx_cell, and
hands prepared cells to a sink in bounded batches. Within the tree reader's limits both produce the same
sheets, cells and report entries.
"""
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from typing import Callable, Iterator, Protocol

from .conversion_report import ConversionReport
from .engine import EngineError, require
from .interchange import (MAX_EXPANDED, SharedFormulas, XlsxCellContext, convert_xlsx_cell, date_system, mode,
                          namespaces, read_xml, shared_strings_path, styles_path, validate_sheet_names,
                          workbook_report, worksheet_path, xlsx_text)
from .xlsx_styles import XlsxStyles

LARGE_CELLS = 1_000_000
LARGE_EXPANDED = 256*1024*1024
LARGE_PACKAGE = 64*1024*1024
LARGE_SHEETS = 255
LARGE_PARTS = 4096
DEFERRED_MEMBERS = 10_000  # shared-formula members met before their master, per sheet
BATCH = 1000
CHUNK = 65536
MAX_DEPTH = 64
# Elements attached to the tree at once: the open ancestors plus one unfinished record. A part within the
# tree reader's 100,000-node bound never reaches it; it stops one huge record from exhausting memory.
MAX_HELD = 100_000

_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")
_OVERLAP = max(len(item) for item in _FORBIDDEN)-1
_ENCODING = re.compile(rb"""\sencoding\s*=\s*(["'])(.*?)\1""", re.S)
_INVALID_XML = "UTF-8 XML without DTDs or entities is required"


class StringStore(Protocol):
    def put(self, index: int, text: str) -> None: ...
    def get(self, index: int) -> str: ...


class Sink(Protocol):
    """Receives one workbook: sheet() opens a sheet, cells() delivers at most 1,000 edits of the open sheet.

    The sink owns duplicate-coordinate detection (it must reject a repeated coordinate in a sheet with
    INVALID_SPREADSHEET) and spools the shared-string table through `strings`."""
    strings: StringStore
    def sheet(self, name: str) -> None: ...
    def cells(self, batch: list[dict]) -> None: ...


class ByteBudget:
    """Decompressed bytes actually produced, shared by every part read from one package."""
    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    def charge(self, size):
        self.used += size
        require(self.used <= self.limit, "LIMIT", f"Expanded spreadsheet exceeds {self.limit:,} bytes")


def open_package(path: Path) -> zipfile.ZipFile:
    """Open a package from disk with the entry, encryption and compression checks of interchange.package."""
    path = Path(path)
    require(path.stat().st_size <= LARGE_PACKAGE, "LIMIT", f"Spreadsheet package exceeds {LARGE_PACKAGE//(1024*1024)} MiB")
    try:
        archive = zipfile.ZipFile(path)
    except (ValueError, EOFError, zipfile.BadZipFile) as error:
        raise EngineError("INVALID_SPREADSHEET", "Invalid spreadsheet ZIP data") from error
    try:
        items = archive.infolist()
        require(len(items) <= LARGE_PARTS, "LIMIT", f"Package has more than {LARGE_PARTS:,} entries")
        require(len({i.filename for i in items}) == len(items), "INVALID_SPREADSHEET", "Package has duplicate entries")
        require(sum(i.file_size for i in items) <= LARGE_EXPANDED, "LIMIT",
                f"Expanded spreadsheet exceeds {LARGE_EXPANDED//(1024*1024)} MiB")
        require(all(not i.flag_bits & 1 and i.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) for i in items),
                "INVALID_SPREADSHEET", "Encrypted or unsupported package compression")
    except BaseException:
        archive.close()
        raise
    return archive


def _check_declaration(chunk):
    data = chunk[3:] if chunk.startswith(b"\xef\xbb\xbf") else chunk
    if not data.startswith(b"<?xml"): return
    end = data.find(b"?>")
    require(end >= 0, "INVALID_SPREADSHEET", _INVALID_XML)
    match = _ENCODING.search(data, 0, end)
    require(match is None or match[2].lower() in (b"utf-8", b"utf8"), "INVALID_SPREADSHEET", _INVALID_XML)


def read_part(archive, name, budget: ByteBudget, *, record_depth=2) -> Iterator[tuple[str, ET.Element]]:
    """Yield ("start"|"end", element) events of one part, read in 64-KiB chunks.

    Once its "end" event has been consumed, an element at depth 2..record_depth (the root is depth 1) is
    detached from its parent, together with its subtree; deeper elements stay attached until then, so a
    consumer handling records at record_depth sees each record complete. Hold a reference to keep one."""
    try:
        with archive.open(name) as stream:
            parser = ET.XMLPullParser(events=("start", "end"))
            state = {"stack": [], "held": 0}
            tail = b""; first = True
            while chunk := stream.read(CHUNK):
                budget.charge(len(chunk))
                if first: _check_declaration(chunk); first = False
                window = (tail+chunk).upper()
                require(b"\0" not in chunk and not any(marker in window for marker in _FORBIDDEN),
                        "INVALID_SPREADSHEET", _INVALID_XML)
                tail = window[-_OVERLAP:]
                parser.feed(chunk)
                yield from _events(parser, state, record_depth)
            parser.close()
            yield from _events(parser, state, record_depth)
    except (KeyError, ET.ParseError, zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as error:
        raise EngineError("INVALID_SPREADSHEET", f"Invalid or missing XML part: {name}") from error


def _events(parser, state, record_depth):
    stack = state["stack"]
    for event, item in parser.read_events():
        if event == "start":
            stack.append(item)
            state["held"] += 1
            require(len(stack) <= MAX_DEPTH and state["held"] <= MAX_HELD, "LIMIT", "XML structure exceeds the supported bounds")
            yield event, item
        else:
            stack.pop()
            yield event, item
            if 1 <= len(stack) < record_depth:
                stack[-1].remove(item)
                state["held"] -= sum(1 for _ in item.iter())


def _charge_whole(archive, name, budget):
    """Charge a small part (workbook, relationships, styles) that interchange.read_xml parses whole."""
    try: size = archive.getinfo(name).file_size
    except KeyError as error: raise EngineError("INVALID_SPREADSHEET", f"Invalid or missing XML part: {name}") from error
    # zipfile never yields more than the declared size, so this is the most the read can produce.
    require(size <= MAX_EXPANDED, "LIMIT", f"Workbook part exceeds {MAX_EXPANDED//(1024*1024)} MiB: {name}")
    budget.charge(size)


def _read_whole(archive, name, budget):
    _charge_whole(archive, name, budget)
    return read_xml(archive, name)


class _SpooledStrings:
    """The list interface convert_xlsx_cell indexes, backed by the sink's string store."""
    def __init__(self, store, count):
        self.store = store
        self.count = count

    def __len__(self): return self.count

    def __getitem__(self, index): return self.store.get(index)


class _Progress:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.cells = 0

    def cell(self):
        self.cells += 1
        require(self.cells <= LARGE_CELLS, "LIMIT", f"Whole-workbook import exceeds {LARGE_CELLS:,} stored cells")


def _read_shared_strings(archive, path, budget, ns, store, checkpoint):
    count = depth = 0
    for event, item in read_part(archive, path, budget, record_depth=2):
        if event == "start":
            depth += 1
            if depth == 1: require(item.tag == f"{{{ns.main}}}sst", "INVALID_SPREADSHEET", "Shared strings part does not match the workbook's namespaces")
            continue
        depth -= 1
        if depth == 1:
            # Like the tree reader, every child of <sst> takes an index.
            require(count < LARGE_CELLS, "LIMIT", f"Shared strings exceed {LARGE_CELLS:,} entries")
            store.put(count, xlsx_text(item, ns))
            count += 1
            if count % BATCH == 0: checkpoint(0, "Reading shared strings")
    return count


def _read_sheet(archive, path, budget, context, sink, progress):
    ns = context.ns
    root, sheet_data, row, cell = (f"{{{ns.main}}}{tag}" for tag in ("worksheet", "sheetData", "row", "c"))
    shared = context.shared_formulas
    batch, deferred, tags = [], [], []

    def emit(item):
        edit = convert_xlsx_cell(item, context=context)
        if edit is None: return
        batch.append(edit)
        if len(batch) == BATCH:
            sink.cells(batch[:]); batch.clear()

    for event, item in read_part(archive, path, budget, record_depth=4):
        if event == "start":
            if not tags: require(item.tag == root, "INVALID_SPREADSHEET", "Invalid worksheet")
            tags.append(item.tag)
            continue
        tags.pop()
        if item.tag != cell or tags[1:] != [sheet_data, row]: continue
        progress.cell()
        if shared is not None:
            shared.register(item)
            formula = item.find(f"{{{ns.main}}}f")
            if formula is not None and formula.get("t") == "shared" and formula.get("si", "") not in shared.groups:
                # Resolved once the sheet's masters are known; a group still without one rejects the file there.
                require(len(deferred) < DEFERRED_MEMBERS, "LIMIT",
                        f"More than {DEFERRED_MEMBERS:,} shared formula members precede their master in one sheet")
                deferred.append(item)
                continue
        emit(item)
        if progress.cells % BATCH == 0:
            if batch: sink.cells(batch[:]); batch.clear()
            progress.checkpoint(progress.cells, "Reading cells")
    for item in deferred: emit(item)
    if batch: sink.cells(batch)


def scope_lines(request) -> list[str]:
    """The fixed report lines of a whole-workbook import; the conversion report's lines follow them."""
    return workbook_report()


def read_workbook(path: Path, request: dict, sink: Sink, checkpoint: Callable[[int, str], None]) -> ConversionReport:
    """Read every sheet of the package at `path` into `sink` at original coordinates; return the report."""
    selected_mode = mode(request)
    policy = request.get("timezone_policy", "reject")
    require(policy in ("reject", "utc"), "INVALID_ARGUMENT", "Choose reject or utc timestamp policy")
    require(request.get("row", 1) == 1 and request.get("col", 1) == 1, "INVALID_ARGUMENT", "Whole-workbook import uses original coordinates")
    budget = ByteBudget(LARGE_EXPANDED)
    with open_package(path) as archive:
        checkpoint(0, "Reading workbook")
        workbook = _read_whole(archive, "xl/workbook.xml", budget)
        ns = namespaces(workbook)
        sheets = workbook.findall(f"{{{ns.main}}}sheets/{{{ns.main}}}sheet")
        names = validate_sheet_names([s.get("name", "") for s in sheets], LARGE_SHEETS)
        system = date_system(workbook, ns)
        relationships = _read_whole(archive, "xl/_rels/workbook.xml.rels", budget)
        mapping = {item.get("Id"): item for item in relationships}
        shared_path = shared_strings_path(archive, relationships, ns)
        count = _read_shared_strings(archive, shared_path, budget, ns, sink.strings, checkpoint) if shared_path else 0
        strings = _SpooledStrings(sink.strings, count)
        style_path = styles_path(archive, relationships, ns)
        if style_path in archive.namelist(): _charge_whole(archive, style_path, budget)
        styles = XlsxStyles(archive, style_path, ConversionReport(), ns)
        conversions = ConversionReport()
        progress = _Progress(checkpoint)
        for sheet, name in zip(sheets, names):
            checkpoint(progress.cells, "Reading sheet "+name)
            # One report per sheet, merged as the tree reader merges them; styles report into the open sheet.
            sheet_report = styles.report = ConversionReport()
            context = XlsxCellContext(mode=selected_mode, shared_strings=strings, styles=styles, date_system=system,
                                      sheet_names=names, sheet=name, report=sheet_report,
                                      shared_formulas=SharedFormulas(ns=ns) if selected_mode != "values" else None,
                                      timezone_policy=policy, ns=ns, seen=None)
            part = worksheet_path(mapping, sheet, ns)
            sink.sheet(name)
            _read_sheet(archive, part, budget, context, sink, progress)
            conversions.merge(sheet_report)
        return conversions
