# SPDX-License-Identifier: GPL-3.0-only
"""Whole-workbook XLSX reading and writing as streams: no sheet or shared-string table is held in memory.

The tree reader in interchange parses each part whole and stays capped at 10,000 cells. This reader
applies the same package and XML safety rules, converts every cell with the same convert_xlsx_cell, and
hands prepared cells to a sink in bounded batches. Within the tree reader's limits both produce the same
sheets, cells and report entries.

The writer is the mirror of the bounded whole-workbook formulas export: every cell goes through the same
interchange.export_cell, written to the archive as it is read.

The package and XML streaming pieces (open_package, read_part, ByteBudget, the progress counters and the LARGE_*
limits) are format-neutral; ods_stream uses them from here, so one patch of a limit here governs both formats.
"""
import hashlib
import os
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Protocol

from .conversion_report import ConversionReport
from .engine import EngineError, require
from .interchange import (MAX_EXPANDED, S, WHOLE_EXPORT_CACHES, SharedFormulas, XlsxCellContext, convert_xlsx_cell,
                          date_system, export_cell, export_report, mode, namespaces, package_parts, read_xml,
                          shared_strings_path, styles_path, uses_future_prefix, validate_sheet_names, workbook_report,
                          worksheet_path, xlsx_text, xml)
from .formula import address
from .temporal import export_temporal_style
from .xlsx_features import SheetFeatures, StringFeatures, relationship_summary, sheet_kinds, workbook_features
from .xlsx_styles import StyleTable, XlsxStyles, stylesheet

# Qualified on November 11, 2026 on the reference Mac (docs/design/evidence/excel-fidelity-1-capacity.json): 1,000,000 cells
# imported, reopened and exported through the SDK within 300 s per phase and under the 256-MiB service memory target.
# LARGE_PACKAGE is the measured largest qualifying package (31.2 MB) with 1.5x headroom, rounded up to 16 MiB.
LARGE_CELLS = 1_000_000
LARGE_EXPANDED = 256*1024*1024
LARGE_PACKAGE = 48*1024*1024
LARGE_SHEETS = 255
LARGE_PARTS = 4096
DEFERRED_MEMBERS = 10_000  # shared-formula members met before their master, per sheet
DEFERRED_BYTES = 16*1024*1024  # text kept for those members, per sheet
SHARED_GROUPS = 65_536  # shared-formula groups open at once (extent not yet passed), per sheet
SHARED_BYTES = 16*1024*1024  # master source text kept for them, per sheet
BATCH = 1000
CHUNK = 65536
SLICE = 4096  # bytes fed to the parser between bound checks
MAX_DEPTH = 64
# Elements attached to the tree at once: the open ancestors plus one unfinished record. A part within the
# tree reader's 100,000-node bound never reaches it; it stops one huge record from exhausting memory.
MAX_HELD = 100_000
# Raw XML bytes of one record (a <c>, an <si>, or any other element at the record depth), and of the text
# between two consecutive tags anywhere. Together they bound what the parser holds. Measured to within SLICE.
MAX_RECORD_BYTES = 1024*1024

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


def open_package(source: Path | BinaryIO) -> zipfile.ZipFile:
    """Open a package from disk with the entry, encryption and compression checks of interchange.package.

    `source` is a path or an open, seekable binary file; a file is read from its start and stays open
    (and owned by the caller) after the archive closes, so callers can parse exactly the bytes they checked."""
    opened = hasattr(source, "read")
    if not opened: source = Path(source)
    try: size = os.fstat(source.fileno()).st_size if opened else source.stat().st_size
    except OSError as error: raise EngineError("INVALID_SPREADSHEET", "Spreadsheet package cannot be read") from error
    require(size <= LARGE_PACKAGE, "LIMIT", f"Spreadsheet package exceeds {LARGE_PACKAGE//(1024*1024)} MiB")
    try:
        if opened: source.seek(0)
        archive = zipfile.ZipFile(source)
    except OSError as error:
        raise EngineError("INVALID_SPREADSHEET", "Spreadsheet package cannot be read") from error
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


def read_part(archive, name, budget: ByteBudget, *, record_depth=2, record_tags=None) -> Iterator[tuple[str, ET.Element]]:
    """Yield ("start"|"end", element) events of one part, read in 64-KiB chunks.

    The records are the elements at record_depth (the root is depth 1), or, given record_tags, the outermost
    elements whose tag is in record_tags, at any depth. Once its "end" event has been consumed, an element that
    is not inside a record (a record itself included) is detached from its parent, together with its subtree;
    elements inside a record stay attached until then, so a consumer sees each record complete. Hold a
    reference to keep one.

    A record longer than MAX_RECORD_BYTES of XML, or text longer than that between two tags, rejects the
    part with LIMIT, so the parser never holds more than about that much of one record's text."""
    try:
        with archive.open(name) as stream:
            parser = ET.XMLPullParser(events=("start", "end"))
            state = _PartState(record_depth, record_tags)
            tail = b""; first = True
            while chunk := stream.read(CHUNK):
                budget.charge(len(chunk))
                if first: _check_declaration(chunk); first = False
                window = (tail+chunk).upper()
                require(b"\0" not in chunk and not any(marker in window for marker in _FORBIDDEN),
                        "INVALID_SPREADSHEET", _INVALID_XML)
                tail = window[-_OVERLAP:]
                for offset in range(0, len(chunk), SLICE):
                    piece = chunk[offset:offset+SLICE]
                    parser.feed(piece)
                    state.fed += len(piece)
                    yield from state.events(parser)
                    state.check()
            parser.close()
            yield from state.events(parser)
    except (KeyError, ET.ParseError, zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as error:
        raise EngineError("INVALID_SPREADSHEET", f"Invalid or missing XML part: {name}") from error


_CELL_RECORDS = {"c": "a cell", "si": "a shared string", "table-cell": "a cell", "covered-table-cell": "a cell"}


class _PartState:
    """Open elements and the byte positions read_part bounds; positions are taken after each fed slice."""
    def __init__(self, record_depth, record_tags=None):
        self.record_depth = record_depth
        self.record_tags = record_tags
        self.stack = []
        self.held = 0
        self.fed = 0           # bytes fed to the parser so far
        self.last_tag = 0      # fed when the latest tag was reported
        self.record = None     # (fed when the open record began, its tag, its depth)

    def check(self):
        if self.record is not None and self.fed-self.record[0] > MAX_RECORD_BYTES:
            local = self.record[1].rsplit("}", 1)[-1]
            raise EngineError("LIMIT", "A single cell or shared string exceeds 1 MiB of XML" if local in _CELL_RECORDS
                              else f"A worksheet record <{local}> exceeds 1 MiB of XML")
        require(self.fed-self.last_tag <= MAX_RECORD_BYTES, "LIMIT", "Text between two XML tags exceeds 1 MiB")

    def events(self, parser):
        stack, tags = self.stack, self.record_tags
        for event, item in parser.read_events():
            self.last_tag = self.fed
            if event == "start":
                stack.append(item)
                self.held += 1
                require(len(stack) <= MAX_DEPTH, "LIMIT", "XML nesting exceeds 64 levels")
                if self.held > MAX_HELD:
                    local = self.record[1].rsplit("}", 1)[-1] if self.record else ""
                    where = _CELL_RECORDS.get(local, "another worksheet block")
                    raise EngineError("LIMIT", f"XML structure exceeds the supported bounds: {where} holds too many elements")
                if self.record is None and (len(stack) == self.record_depth if tags is None else item.tag in tags):
                    self.record = (self.fed, item.tag, len(stack))
                yield event, item
            else:
                if self.record is not None and len(stack) == self.record[2]: self.record = None
                inside = self.record is not None
                stack.pop()
                yield event, item
                if stack and not inside:
                    stack[-1].remove(item)
                    self.held -= sum(1 for _ in item.iter())


def _admit_whole(archive, name, budget):
    """Charge and pre-check a small part (workbook, relationships, styles) that interchange.read_xml parses whole.

    read_xml does not check the XML declaration, so the streaming path applies read_part's UTF-8 rule here."""
    try:
        size = archive.getinfo(name).file_size
        # zipfile never yields more than the declared size, so this is the most the read can produce.
        require(size <= MAX_EXPANDED, "LIMIT", f"Workbook part exceeds {MAX_EXPANDED//(1024*1024)} MiB: {name}")
        budget.charge(size)
        with archive.open(name) as stream: _check_declaration(stream.read(CHUNK))
    except (KeyError, zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as error:
        raise EngineError("INVALID_SPREADSHEET", f"Invalid or missing XML part: {name}") from error


def _read_whole(archive, name, budget):
    _admit_whole(archive, name, budget)
    return read_xml(archive, name)


class _SpooledStrings:
    """The list interface convert_xlsx_cell indexes, backed by the sink's string store."""
    def __init__(self, store, count):
        self.store = store
        self.count = count

    def __len__(self): return self.count

    def __getitem__(self, index): return self.store.get(index)


class _Progress:
    """Stored cells read so far, and units of work (a cell read, or a deferred cell resolved) between checkpoints."""
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.cells = 0
        self.work = 0

    def cell(self):
        self.cells += 1
        require(self.cells <= LARGE_CELLS, "LIMIT", f"Whole-workbook import exceeds {LARGE_CELLS:,} stored cells")

    def step(self, flush, phase):
        """Count one unit of work; every 1,000 units, flush pending edits to the sink and checkpoint."""
        self.work += 1
        if self.work % BATCH == 0:
            flush()
            self.checkpoint(self.cells, phase)


DEFERRED_OVERHEAD = 256  # bytes charged per deferred member besides the text it keeps


def _utf8(text):
    return len((text or "").encode("utf-8", "surrogatepass"))


def _deferred_copy(cell):
    """A small detached element with only what convert_xlsx_cell reads from a deferred member, and its size.

    convert_xlsx_cell reads the <c> attributes and the first <f>, <v> and <is>. An inline string is reduced to one
    <t> holding the undecoded text xlsx_text concatenates (phonetic runs excluded), so it converts identically.
    The size is the UTF-8 length of every key, value and text kept, plus DEFERRED_OVERHEAD."""
    main = cell.tag[:-len("c")]
    copy = ET.Element(cell.tag, dict(cell.attrib))
    size = DEFERRED_OVERHEAD+sum(_utf8(k)+_utf8(v) for k, v in cell.attrib.items())
    for local in ("f", "v"):
        child = cell.find(main+local)
        if child is None: continue
        kept = ET.SubElement(copy, child.tag, dict(child.attrib))
        kept.text = child.text
        size += _utf8(child.text)+sum(_utf8(k)+_utf8(v) for k, v in child.attrib.items())
    inline = cell.find(main+"is")
    if inline is not None:
        raw = "".join((child.text or "") if child.tag == main+"t" else (child.findtext(main+"t") or "") if child.tag == main+"r" else ""
                      for child in inline)
        ET.SubElement(ET.SubElement(copy, main+"is"), main+"t").text = raw
        size += _utf8(raw)
    return copy, size


def _read_shared_strings(archive, path, budget, ns, store, checkpoint, features):
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
            features.string(item)
            count += 1
            if count % BATCH == 0: checkpoint(0, "Reading shared strings")
    return count


def _read_sheet(archive, path, budget, context, sink, progress, features):
    """Convert one worksheet's cells into the sink; every other element ending is passed to `features`."""
    ns = context.ns
    root, sheet_data, row, cell = (f"{{{ns.main}}}{tag}" for tag in ("worksheet", "sheetData", "row", "c"))
    shared = context.shared_formulas
    batch, deferred, tags = [], [], []
    deferred_bytes = 0

    def flush():
        if batch: sink.cells(batch[:]); batch.clear()

    def emit(item):
        edit = convert_xlsx_cell(item, context=context)
        if edit is None: return
        batch.append(edit)
        if len(batch) == BATCH: flush()

    for event, item in read_part(archive, path, budget, record_depth=4):
        if event == "start":
            if not tags: require(item.tag == root, "INVALID_SPREADSHEET", "Invalid worksheet")
            tags.append(item.tag)
            continue
        tags.pop()
        depth = len(tags)
        if depth < 2 or tags[1] != sheet_data:
            features.end(tags, item)
            continue
        if depth == 2:
            if item.tag == row: features.row(item)
            continue
        if item.tag != cell or depth != 3 or tags[2] != row: continue
        features.cell(item)
        progress.cell()
        if shared is not None:
            shared.register(item)
            formula = item.find(f"{{{ns.main}}}f")
            key = None if formula is None or formula.get("t") != "shared" else formula.get("si", "")
            if key is not None and key not in shared.groups and not shared.gone(key):
                # Resolved once the sheet's masters are known; a group still without one rejects the file there.
                # Its group, once registered, is kept to the end of the sheet.
                require(len(deferred) < DEFERRED_MEMBERS, "LIMIT",
                        f"More than {DEFERRED_MEMBERS:,} shared formula members precede their master in one sheet")
                kept, size = _deferred_copy(item)
                deferred_bytes += size
                require(deferred_bytes <= DEFERRED_BYTES, "LIMIT",
                        f"Shared formula members preceding their master hold more than {DEFERRED_BYTES//(1024*1024)} MiB of text in one sheet")
                deferred.append(kept)
                shared.pinned.add(key)
                progress.step(flush, "Reading cells")
                continue
        emit(item)
        progress.step(flush, "Reading cells")
    for item in deferred:
        emit(item)
        progress.step(flush, "Resolving shared formulas")
    flush()


def scope_lines(request) -> list[str]:
    """The fixed report lines of a whole-workbook import; the conversion report's lines follow them."""
    return workbook_report()


def read_workbook(path: Path | BinaryIO, request: dict, sink: Sink, checkpoint: Callable[[int, str], None]) -> ConversionReport:
    """Read every sheet of the package at `path` (or in an open binary file) into `sink` at original coordinates; return the report.

    Within the tree reader's limits, the sheets, cells and report entries agree with interchange.xlsx_preview at
    workbook scope, with one exception: a shared-formula member stored before its master is converted at the
    end of its sheet, so its cell reaches the sink after later cells, and the samples of the report entries it
    contributes (and the order of entries that tie when sorted) can differ from the tree reader's."""
    selected_mode = mode(request)
    policy = request.get("timezone_policy", "reject")
    require(policy in ("reject", "utc"), "INVALID_ARGUMENT", "Choose reject or utc timestamp policy")
    require(request.get("row", 1) == 1 and request.get("col", 1) == 1, "INVALID_ARGUMENT", "Whole-workbook import uses original coordinates")
    budget = ByteBudget(LARGE_EXPANDED)
    with open_package(path) as archive:
        checkpoint(0, "Reading workbook")
        workbook = _read_whole(archive, "xl/workbook.xml", budget)
        ns = namespaces(workbook)
        every = workbook.findall(f"{{{ns.main}}}sheets/{{{ns.main}}}sheet")
        # A package cannot hold more sheets than parts; only worksheets count toward the sheet limit, checked before
        # their names are kept.
        require(len(every) <= LARGE_PARTS, "LIMIT", f"Workbook declares more than {LARGE_PARTS:,} sheets")
        system = date_system(workbook, ns)
        relationships = _read_whole(archive, "xl/_rels/workbook.xml.rels", budget)
        mapping = {item.get("Id"): item for item in relationships}
        # Chart, dialog and macro sheets are not imported; they are reported with the workbook's features.
        kinds = sheet_kinds(every, mapping, ns)
        require(kinds.count(None) <= LARGE_SHEETS, "LIMIT", f"Provide 1-{LARGE_SHEETS} sheets")
        sheets = [sheet for sheet, kind in zip(every, kinds) if kind is None]
        skipped = [(sheet.get("name", ""), kind) for sheet, kind in zip(every, kinds) if kind is not None]
        names = [s.get("name", "") for s in sheets]
        if names or not skipped: names = validate_sheet_names(names, LARGE_SHEETS)
        shared_path = shared_strings_path(archive, relationships, ns)
        string_features = StringFeatures(ns)
        count = _read_shared_strings(archive, shared_path, budget, ns, sink.strings, checkpoint, string_features) if shared_path else 0
        strings = _SpooledStrings(sink.strings, count)
        style_path = styles_path(archive, relationships, ns)
        if style_path in archive.namelist(): _admit_whole(archive, style_path, budget)
        styles = XlsxStyles(archive, style_path, ConversionReport(), ns)
        conversions = ConversionReport()
        workbook_features(conversions, workbook, ns, archive.namelist(), string_features, skipped)
        progress = _Progress(checkpoint)
        for sheet, name in zip(sheets, names):
            checkpoint(progress.cells, "Reading sheet "+name)
            # One report per sheet, merged as the tree reader merges them; styles report into the open sheet.
            sheet_report = styles.report = ConversionReport()
            context = XlsxCellContext(mode=selected_mode, shared_strings=strings, styles=styles, date_system=system,
                                      sheet_names=names, sheet=name, report=sheet_report,
                                      shared_formulas=SharedFormulas(ns=ns, limit=SHARED_GROUPS, budget=SHARED_BYTES) if selected_mode != "values" else None,
                                      timezone_policy=policy, ns=ns, seen=None)
            part = worksheet_path(mapping, sheet, ns)
            features = SheetFeatures(ns, relationship_summary(archive, part, budget))
            sink.sheet(name)
            _read_sheet(archive, part, budget, context, sink, progress, features)
            features.report(sheet_report, name)
            conversions.merge(sheet_report)
        return conversions


# --- Writing ---------------------------------------------------------------------------------------------------

EXPORT_FLUSH = 65536  # characters of worksheet XML gathered before a write to the archive
_DECLARATION = b"<?xml version='1.0' encoding='utf-8'?>\n"  # as interchange.xml writes it
FORMULA_DISPLAY_LINE = ("Formula results are not evaluated during a large export, so a formula whose result is a date or time "
                        "takes no calendar display: give such cells a date/time number format, or use the bounded export.")
# Itemizes the cells FORMULA_DISPLAY_LINE is about: formulas without a number format, which the bounded export
# would give a calendar display when their result is temporal.
FORMULA_DISPLAY_CATEGORY = "formula.display_not_evaluated"
FORMULA_DISPLAY_DETAIL = "result not evaluated; a date or time result takes no calendar display"


def export_lines(native: dict, future_functions: bool, unevaluated_formulas: int) -> list[str]:
    """The fixed report lines of a whole-workbook streamed export; the conversion report's lines follow them.
    The formula-display line appears only when an unformatted formula was written."""
    head = workbook_report()+[WHOLE_EXPORT_CACHES]+([FORMULA_DISPLAY_LINE] if unevaluated_formulas else [])
    return export_report(head, native, future_functions)


class _Expanded:
    """Uncompressed bytes written to the package."""
    def __init__(self, label="XLSX"):
        self.used = 0
        self.label = label

    def charge(self, size):
        self.used += size
        require(self.used <= LARGE_EXPANDED, "LIMIT", f"Expanded {self.label} export exceeds {LARGE_EXPANDED//(1024*1024)} MiB")


def _write_sheet(part, name, cells, context, expanded, progress):
    """Write one worksheet part cell by cell; only a bounded buffer of XML text is held."""
    pending, size = [], 0

    def emit(text):
        nonlocal size
        pending.append(text)
        size += len(text)
        if size >= EXPORT_FLUSH: flush()

    def flush():
        nonlocal size
        if not pending: return
        data = "".join(pending).encode("utf-8")
        pending.clear(); size = 0
        expanded.charge(len(data))
        part.write(data)

    expanded.charge(len(_DECLARATION))
    part.write(_DECLARATION)
    emit(f'<worksheet xmlns="{S}">')
    current = None
    for cell in cells:
        progress.cell(name)
        cell = export_temporal_style(cell)
        if current is None: emit("<sheetData>")
        elif cell["row"] != current: emit("</row>")
        if cell["row"] != current:
            current = cell["row"]
            emit(f'<row r="{current}">')
        if cell["kind"] == "formula":
            if not context["future"]: context["future"] = uses_future_prefix(cell["source"])
            if cell.get("style", {}).get("format", "general") == "general":
                context["report"].add(FORMULA_DISPLAY_CATEGORY, "approximated", FORMULA_DISPLAY_DETAIL,
                                      sheet=name, cell=address(cell["row"], cell["col"]))
                context["unevaluated"] += 1
        element = export_cell(cell, context["styles"].index(cell.get("style", {})), sheet=name, mode="formulas",
                              report=context["report"], whole=True, names=context["names"])
        emit(ET.tostring(element, encoding="unicode"))
    emit("<sheetData />" if current is None else "</row></sheetData>")
    emit("</worksheet>")
    flush()


class _WriteProgress:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.cells = 0

    def cell(self, name):
        self.cells += 1
        require(self.cells <= LARGE_CELLS, "LIMIT", f"Whole-workbook export exceeds {LARGE_CELLS:,} stored cells")
        if self.cells % BATCH == 0: self.checkpoint(self.cells, "Writing sheet "+name)


def write_workbook(path: Path, sheets, report: ConversionReport, checkpoint: Callable[[int, str], None]) -> dict:
    """Write a whole workbook as an XLSX package at `path`, formulas without cached values, cell by cell.

    `sheets` yields (sheet name, iterator of cells) as Workbook.stream_workbook does; only the pairs are taken up
    front, and each sheet's cells are read as they are written. Each cell is written by interchange.export_cell,
    exactly as the bounded whole-workbook formulas export writes it, with strings inline (no shared-string table).
    Worksheets are written first and the stylesheet last, so style indexes follow first use as in that export.
    Approximations go to `report`. More than LARGE_CELLS cells, LARGE_SHEETS sheets, LARGE_EXPANDED uncompressed
    bytes or 1,024 distinct styles is LIMIT. On any failure the partial file is removed.

    Returns {"bytes", "sha256", "sheets", "cells", "future_functions", "unevaluated_formulas"}: the file's size and
    digest, the counts written, whether any formula uses an _xlfn identifier, and how many formulas without a number
    format were written (each itemized in `report` as FORMULA_DISPLAY_CATEGORY, approximated)."""
    path = Path(path)
    try:
        sheets = list(sheets)
        require(len(sheets) <= LARGE_SHEETS, "LIMIT", f"Whole-workbook export exceeds {LARGE_SHEETS} sheets")
        names = validate_sheet_names([name for name, _ in sheets], LARGE_SHEETS)
        context = {"styles": StyleTable(), "report": report, "names": names, "future": False, "unevaluated": 0}
        expanded, progress = _Expanded(), _WriteProgress(checkpoint)
        flags = os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os, "O_NOFOLLOW", 0)|getattr(os, "O_BINARY", 0)
        with open(os.open(path, flags, 0o600), "wb") as output:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for index, (name, cells) in enumerate(sheets, 1):
                    with archive.open(f"xl/worksheets/sheet{index}.xml", "w") as part:
                        checkpoint(progress.cells, "Writing sheet "+name)
                        _write_sheet(part, name, cells, context, expanded, progress)
                checkpoint(progress.cells, "Writing workbook")
                for part_name, root in package_parts(names)+[("xl/styles.xml", stylesheet(context["styles"].styles))]:
                    data = xml(root)
                    expanded.charge(len(data))
                    archive.writestr(part_name, data)
        digest, size = hashlib.sha256(), 0
        with open(path, "rb") as written:
            while chunk := written.read(1024*1024):
                digest.update(chunk); size += len(chunk)
                checkpoint(progress.cells, "Checking export")
        return {"bytes": size, "sha256": digest.hexdigest(), "sheets": len(names), "cells": progress.cells,
                "future_functions": context["future"], "unevaluated_formulas": context["unevaluated"]}
    except BaseException:
        path.unlink(missing_ok=True)
        raise
