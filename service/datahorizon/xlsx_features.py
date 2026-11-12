# SPDX-License-Identifier: GPL-3.0-only
"""Worksheet and workbook features of an XLSX package that DataHorizon does not import, as report entries.

Every child of <worksheet> other than <sheetData>, every row, column and cell attribute, every child of <workbook>
and every advanced package part is either imported elsewhere, carries nothing to lose in the form found (such as
<dimension>, or Excel's default page margins), or is counted here under a stable feature id and reported as one
`omitted` entry per feature (category sheet.feature or workbook.feature, detail = the id, count = items).
docs/design/excel-spec-coverage.md lists each element and the id it produces.

Both readers feed the same scanner: the tree reader walks each parsed sheet in document order, and the streaming
reader passes each element as its end event streams past, so neither holds a sheet for this. The scanner only
reads the element that has just ended and the tags of its ancestors, never its (possibly released) children.

Nothing the scanners keep grows with the file beyond a named bound: column widths are kept as a count per distinct
width, distinct identifiers in a DistinctCount that saturates (the entry then reads "at least" its count), and item
counts no valid sheet can reach (more <col> elements than columns, more rows than the grid, more merged ranges,
hyperlinks or validation rules than LARGE_CELLS) reject the file with LIMIT.
"""
import posixpath
import re

from .conversion_report import MAX_SAMPLES, SHEET_FEATURE, WORKBOOK_FEATURE
from .engine import require

# Report order of the per-sheet ids; an id seen nowhere in a sheet produces no entry.
SHEET_FEATURES = (
    "merged_cells", "column_widths", "row_heights", "hidden_rows_columns", "outline", "row_column_styles",
    "sheet_format_defaults", "frozen_panes", "split_panes", "sheet_view", "custom_views", "tab_colour",
    "conditional_formatting", "data_validation", "hyperlinks", "comments", "rich_text", "rich_values", "phonetic",
    "excel_tables", "auto_filter", "sort_state", "sheet_protection", "protected_ranges", "scenarios",
    "data_consolidation", "page_setup", "drawings", "legacy_drawings", "background_image", "ole_objects",
    "form_controls", "sparklines", "slicers", "cell_watches", "ignored_errors", "smart_tags", "custom_properties",
    "web_publish", "extensions", "relationships_unreadable")
WORKBOOK_FEATURES = (
    "chart_sheets", "dialog_sheets", "macro_sheets", "defined_names", "external_references", "hidden_sheets", "workbook_protection", "file_sharing",
    "calculation_settings", "custom_views", "rich_text", "phonetic", "function_groups", "smart_tags", "web_publish",
    "slicers", "data_model", "extensions", "charts", "pivot_tables", "vba", "custom_xml", "connections",
    "query_tables", "embedded_objects", "document_properties")
# Sheets of a workbook that are not worksheets, by the last segment of their relationship type. They are not
# imported: each is reported (workbook.feature, with the sheet as sample) and no native sheet is created for it.
SHEET_KINDS = {"chartsheet": "chart_sheets", "dialogsheet": "dialog_sheets", "xlMacrosheet": "macro_sheets",
               "xlIntlMacrosheet": "macro_sheets"}
KIND_NAMES = {"chart_sheets": "a chart sheet", "dialog_sheets": "a dialog sheet", "macro_sheets": "a macro sheet"}
# Header and footer texts LibreOffice writes into every sheet (font codes &"..." and sizes &NN removed first).
STOCK_HEADER_FOOTER = {"&C&A", "&CPage &P", "&C&P", "&CPage &P of &N"}

# Advanced package parts, one workbook entry per id counting the matching parts. Drawings, tables, comments and
# external links are reported where a sheet or the workbook refers to them, not here.
PACKAGE_PARTS = tuple((feature, re.compile(pattern, re.IGNORECASE)) for feature, pattern in (
    ("charts", r"xl/charts/chart[^/]*\.xml"), ("pivot_tables", r"xl/pivotTables/[^/]+\.xml"),
    ("vba", r"xl/vbaProject\.bin"), ("custom_xml", r"customXml/item[0-9]*\.xml"),
    ("connections", r"xl/connections\.xml"), ("query_tables", r"xl/queryTables/[^/]+\.xml"),
    ("embedded_objects", r"xl/embeddings/[^/]+"), ("slicers", r"xl/(?:slicers|timelines)/[^/]+\.xml"),
    ("data_model", r"xl/model/[^/]+"), ("document_properties", r"docProps/custom\.xml")))

TRUE = ("1", "true")
WIDTH_TOLERANCE = 0.1  # characters
# Structural bounds of a worksheet: Excel's grid, and xlsx_stream.LARGE_CELLS (a test keeps them equal) for the items
# a sheet can hold at most one per cell. Beyond any of them the package is not a valid sheet: LIMIT.
MAX_COLUMN_ELEMENTS = 16_384
MAX_ROW_ELEMENTS = 1_048_576
MAX_FEATURE_ITEMS = 1_000_000
COUNTED_ITEMS = {"merged_cells": "merged ranges", "hyperlinks": "hyperlinks", "data_validation": "validation rules"}
# A DistinctCount keeps at most this many identifiers and this much of their text, then saturates.
MAX_DISTINCT = 65_536
DISTINCT_BYTES = 4*1024*1024
# Margins Excel, older Excel/openpyxl and LibreOffice write by default: left, right, top, bottom, header, footer.
DEFAULT_MARGINS = ((0.7, 0.7, 0.75, 0.75, 0.3, 0.3), (0.75, 0.75, 1.0, 1.0, 0.5, 0.5),
                   (0.7875, 0.7875, 1.05277777777778, 1.05277777777778, 0.7875, 0.7875))
MARGINS = ("left", "right", "top", "bottom", "header", "footer")
# sheetView attributes with their defaults; any other value is view state worth naming (sheet_view).
VIEW_DEFAULTS = {"showGridLines": TRUE, "showRowColHeaders": TRUE, "showZeros": TRUE, "rightToLeft": ("0", "false"),
                 "showFormulas": ("0", "false"), "showOutlineSymbols": TRUE, "defaultGridColor": TRUE,
                 "showWhiteSpace": TRUE, "view": ("normal",), "zoomScale": ("100",), "zoomScaleNormal": ("0", "100")}
# pageSetup attributes that are a print choice; paper size, resolution and the printer-settings part are not
# reported because every package carries the default of its locale and printer.
PAGE_DEFAULTS = {"orientation": ("default", "portrait"), "scale": ("100",), "pageOrder": ("downThenOver",),
                 "blackAndWhite": ("0", "false"), "draft": ("0", "false"), "cellComments": ("none",),
                 "errors": ("displayed",), "copies": ("1",), "fitToWidth": ("1",), "fitToHeight": ("1",)}
# Children of <worksheet> reported as a whole when present, unless a rule below decides.
SHEET_ELEMENTS = {"autoFilter": "auto_filter", "sortState": "sort_state", "dataConsolidate": "data_consolidation",
                  "customProperties": "custom_properties", "smartTags": "smart_tags", "drawing": "drawings",
                  "picture": "background_image", "webPublishItems": "web_publish", "legacyDrawingHF": "page_setup",
                  "drawingHF": "page_setup"}
# Children of <worksheet> that are imported elsewhere, or carry nothing to lose, or whose items are counted.
SHEET_SILENT = {"sheetData", "dimension", "sheetCalcPr", "phoneticPr", "sheetPr", "sheetViews", "cols", "mergeCells",
                "conditionalFormatting", "dataValidations", "hyperlinks", "tableParts", "protectedRanges", "scenarios",
                "customSheetViews", "rowBreaks", "colBreaks", "headerFooter", "oleObjects", "controls", "cellWatches",
                "ignoredErrors", "legacyDrawing", "extLst"}
# Items counted inside a container child of <worksheet>: (container, item) -> feature.
SHEET_ITEMS = {("mergeCells", "mergeCell"): "merged_cells", ("conditionalFormatting", "cfRule"): "conditional_formatting",
               ("dataValidations", "dataValidation"): "data_validation", ("hyperlinks", "hyperlink"): "hyperlinks",
               ("tableParts", "tablePart"): "excel_tables", ("protectedRanges", "protectedRange"): "protected_ranges",
               ("scenarios", "scenario"): "scenarios", ("customSheetViews", "customSheetView"): "custom_views",
               ("cellWatches", "cellWatch"): "cell_watches", ("ignoredErrors", "ignoredError"): "ignored_errors",
               ("rowBreaks", "brk"): "page_setup", ("colBreaks", "brk"): "page_setup"}
# Items inside a worksheet <extLst> (any namespace) and inside <oleObjects>/<controls> (any depth).
EXTENSION_ITEMS = {"sparklineGroup": "sparklines", "cfRule": "conditional_formatting", "dataValidation": "data_validation",
                   "slicer": "slicers", "timelineRef": "slicers", "protectedRange": "protected_ranges",
                   "ignoredError": "ignored_errors"}
# Workbook <extLst> children that hold no workbook content (Excel and LibreOffice write them in every workbook).
WORKBOOK_EXTENSION_SILENT = {"workbookPr", "calcFeatures", "extCalcPr"}  # extCalcPr: LibreOffice's reference syntax
WORKBOOK_EXTENSION_ITEMS = {"slicerCache": "slicers", "timelineCacheRef": "slicers", "dataModel": "data_model",
                            "pivotCache": "pivot_tables"}


def local(tag):
    return tag.rsplit("}", 1)[-1]


class DistinctCount:
    """The number of distinct identifiers seen, keeping at most MAX_DISTINCT of them and DISTINCT_BYTES of their text.

    Once either bound is reached, a new identifier is no longer kept and the count `saturated`: it is then a lower
    bound, and membership of an identifier that was not kept is unknown (`known` answers False for it)."""
    def __init__(self, limit=MAX_DISTINCT, budget=DISTINCT_BYTES):
        self.limit, self.budget = limit, budget
        self.kept, self.bytes, self.saturated = set(), 0, False

    def add(self, key):
        if key in self.kept: return
        size = len(key.encode("utf-8", "surrogatepass"))
        if len(self.kept) >= self.limit or self.bytes+size > self.budget:
            self.saturated = True
            return
        self.kept.add(key); self.bytes += size

    def known(self, key):
        return key in self.kept

    def __len__(self):
        return len(self.kept)


def _number(value, default=0.0):
    try: return float(value)
    except (TypeError, ValueError): return default


def _span(col):
    """How many columns a <col> covers; a malformed range counts once (it is not imported, so it never rejects)."""
    try: first, last = int(col.get("min", "")), int(col.get("max", ""))
    except ValueError: return 1
    return max(1, min(last, 16384) - max(first, 1) + 1)


class SheetFeatures:
    """Counts the unimported features of one worksheet as its elements end, then reports them."""
    def __init__(self, ns, relationships=None):
        """`relationships` is what relationship_summary returns for the sheet part."""
        self.main = "{" + ns.main + "}"
        self.counts = {}
        self.relationships = relationships or Relationships()
        self.legacy_drawing = False
        self._found_in_extension = False
        self.default_width = None
        self.rows = self.columns = 0  # <row> and <col> elements seen
        # Columns of <col> elements by width (None: no readable width): all of them, and those without customWidth.
        self.width_shares, self.plain_widths = {}, {}
        self.custom_width_columns = 0
        self._conditional_ids = DistinctCount()  # x14 ids that base conditional rules point to
        self.saturated = set()  # features whose count is a lower bound

    def add(self, feature, count=1):
        if count > 0:
            self.counts[feature] = total = self.counts.get(feature, 0) + count
            if feature in COUNTED_ITEMS:
                require(total <= MAX_FEATURE_ITEMS, "LIMIT", f"A sheet holds more than {MAX_FEATURE_ITEMS:,} {COUNTED_ITEMS[feature]}")

    def row(self, item):
        """A <row> of <sheetData>."""
        self.rows += 1
        require(self.rows <= MAX_ROW_ELEMENTS, "LIMIT", f"A worksheet holds more than {MAX_ROW_ELEMENTS:,} rows")
        attrib = item.attrib
        if not attrib: return
        if attrib.get("customHeight") in TRUE: self.add("row_heights")
        if attrib.get("hidden") in TRUE: self.add("hidden_rows_columns")
        if attrib.get("outlineLevel", "0") not in ("", "0") or attrib.get("collapsed") in TRUE: self.add("outline")
        if attrib.get("customFormat") in TRUE: self.add("row_column_styles")

    def cell(self, item):
        """A <c> of a row."""
        attrib = item.attrib
        if "vm" in attrib: self.add("rich_values")
        if attrib.get("ph") in TRUE: self.add("phonetic")
        if attrib.get("t") == "inlineStr":
            inline = item.find(self.main + "is")
            if inline is not None and rich(inline, self.main): self.add("rich_text")

    def end(self, ancestors, item):
        """Any element outside <sheetData>; `ancestors` are the tags from <worksheet> down to its parent."""
        depth, tag = len(ancestors), item.tag
        main = tag.startswith(self.main)
        name = local(tag)
        if depth == 1:
            self._child(item, name, main)
            return
        if depth == 0: return
        top = ancestors[1]
        if not top.startswith(self.main): return  # inside a foreign wrapper, reported as a whole
        top = local(top)
        if top == "extLst" or top in ("oleObjects", "controls"):
            if any(local(a) == "Fallback" for a in ancestors): return
            if top == "oleObjects" and name == "oleObject": self.add("ole_objects")
            elif top == "controls" and name == "control": self.add("form_controls")
            elif top == "extLst":
                feature = EXTENSION_ITEMS.get(name)
                if feature == "conditional_formatting" and self._conditional_ids.known(item.get("id") or ""):
                    # The x14 twin of a rule already counted in <conditionalFormatting> (Excel writes one per data bar).
                    self._found_in_extension = True
                elif feature == "conditional_formatting" and self._conditional_ids.saturated:
                    # It may be the twin of a rule whose id was not kept: not counted again, so the count is a lower bound.
                    self.saturated.add(feature); self._found_in_extension = True
                elif feature is not None:
                    self.add(feature); self._found_in_extension = True
                elif depth == 2 and name == "ext":
                    if not self._found_in_extension: self.add("extensions")
                    self._found_in_extension = False
            return
        if top == "conditionalFormatting" and name == "id" and item.text:
            self._conditional_ids.add(item.text.strip())
            return
        if depth == 2:
            feature = SHEET_ITEMS.get((top, name))
            if feature == "page_setup": self.counts["page_setup"] = 1
            elif feature is not None: self.add(feature)
            elif top == "cols" and name == "col": self._col(item)
            elif top == "sheetPr":
                if name == "tabColor": self.add("tab_colour")
                elif name == "pageSetUpPr" and item.get("fitToPage") in TRUE: self.counts["page_setup"] = 1
            elif top == "sheetViews" and name == "sheetView":
                if any(item.get(key) is not None and item.get(key) not in values for key, values in VIEW_DEFAULTS.items()):
                    self.add("sheet_view")
            elif top == "headerFooter" and not stock_header_footer(item.text): self.counts["page_setup"] = 1
        elif depth == 3 and top == "sheetViews" and name == "pane":
            self.add("frozen_panes" if item.get("state") in ("frozen", "frozenSplit") else "split_panes")

    def _col(self, item):
        self.columns += 1
        require(self.columns <= MAX_COLUMN_ELEMENTS, "LIMIT", f"A worksheet holds more than {MAX_COLUMN_ELEMENTS:,} <col> elements")
        columns = _span(item)
        if "width" in item.attrib:
            # At most MAX_COLUMN_ELEMENTS distinct widths, one per element.
            width = _number(item.get("width"), None)
            self.width_shares[width] = self.width_shares.get(width, 0) + columns
            if item.get("customWidth") in TRUE: self.custom_width_columns += columns
            else: self.plain_widths[width] = self.plain_widths.get(width, 0) + columns
        if item.get("hidden") in TRUE: self.add("hidden_rows_columns", columns)
        if item.get("outlineLevel", "0") not in ("", "0") or item.get("collapsed") in TRUE: self.add("outline", columns)
        if item.get("style", "0") not in ("", "0"): self.add("row_column_styles", columns)

    def _child(self, item, name, main):
        if not main:
            # mc:AlternateContent and other foreign wrappers: their content is not read.
            self.add("extensions")
            return
        if name in SHEET_SILENT:
            if name == "legacyDrawing": self.legacy_drawing = True
            return
        if name == "sheetFormatPr":
            # Only an explicit default row height or rows hidden by default change the layout in a way worth
            # naming; producers write their own default widths and heights into every file (increment 2 owns sizes).
            self.default_width = _number(item.get("defaultColWidth"), None)
            if any(item.get(key) in TRUE for key in ("customHeight", "zeroHeight")): self.add("sheet_format_defaults")
        elif name == "sheetProtection":
            if item.get("sheet") in TRUE: self.add("sheet_protection")
        elif name == "printOptions":
            if any(item.get(key) in TRUE for key in ("gridLines", "headings", "horizontalCentered", "verticalCentered")):
                self.counts["page_setup"] = 1
        elif name == "pageMargins":
            values = tuple(_number(item.get(key), -1.0) for key in MARGINS)
            if not any(all(abs(a - b) < 1e-4 for a, b in zip(values, default)) for default in DEFAULT_MARGINS):
                self.counts["page_setup"] = 1
        elif name == "pageSetup":
            # A first page number of 1 is the default even when LibreOffice marks it as used.
            numbered = item.get("useFirstPageNumber") in TRUE and item.get("firstPageNumber", "1") != "1"
            if numbered or any(item.get(key) is not None and item.get(key) not in values for key, values in PAGE_DEFAULTS.items()):
                self.counts["page_setup"] = 1
        elif name in SHEET_ELEMENTS:
            feature = SHEET_ELEMENTS[name]
            if feature == "page_setup": self.counts["page_setup"] = 1
            else: self.add(feature)
        else:
            self.add("extensions")

    def _column_widths(self):
        """Columns whose width is custom or differs from the default: the sheet's default column width, or else the
        width most columns share (producers such as LibreOffice write a width for every column)."""
        default = self.default_width
        if default is None:
            shares = self.width_shares
            default = max(shares, key=shares.get) if shares else None
        # Within a tenth of a character of the default is the default: LibreOffice writes a default width of
        # 11.53515625 with columns of 11.52, a difference no grid shows.
        return self.custom_width_columns+sum(columns for width, columns in self.plain_widths.items()
                                             if width is None or default is None or abs(width - default) > WIDTH_TOLERANCE)

    def report(self, report, sheet):
        """Add one omitted entry per feature found, in SHEET_FEATURES order."""
        self.add("column_widths", self._column_widths())
        if self.relationships.comments: self.add("comments", self.relationships.comments)
        elif self.legacy_drawing: self.add("legacy_drawings")
        if self.relationships.unreadable: self.add("relationships_unreadable")
        for feature in SHEET_FEATURES:
            if feature in self.counts:
                report.add(SHEET_FEATURE, "omitted", feature, sheet=sheet, count=self.counts[feature], at_least=feature in self.saturated)


def rich(item, main):
    """Whether a shared or inline string holds formatted runs."""
    return any(child.tag == main + "r" and child.find(main + "rPr") is not None for child in item)


def phonetic(item, main):
    return item.find(main + "rPh") is not None


def scan_tree(root, features):
    """Feed a parsed worksheet to `features` in document order, as the streaming reader does."""
    main = features.main
    sheet_data = main + "sheetData"
    for child in root:
        if child.tag == sheet_data:
            for row in child:
                if row.tag != main + "row": continue
                for cell in row:
                    if cell.tag == main + "c": features.cell(cell)
                features.row(row)
            continue
        _post_order(child, [root.tag], features)


def _post_order(item, ancestors, features):
    ancestors.append(item.tag)
    for child in item: _post_order(child, ancestors, features)
    ancestors.pop()
    features.end(ancestors, item)


def relationships_path(part):
    return posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")


def stock_header_footer(text):
    """Whether a header or footer text is empty or one LibreOffice writes into every sheet."""
    text = re.sub(r'&"[^"]*"|&[0-9]+', "", (text or "").strip())
    return not text or text in STOCK_HEADER_FOOTER


class Relationships:
    """What the report needs from a worksheet's relationships: how many comments the sheet holds (0 for none) and
    whether its relationship part could not be read."""
    def __init__(self, comments=0, unreadable=False):
        self.comments, self.unreadable = comments, unreadable


def _forbidden(archive, path):
    """Whether a part contains a DTD or entity declaration (read in chunks)."""
    from .xlsx_stream import CHUNK, _FORBIDDEN, _OVERLAP
    tail = b""
    with archive.open(path) as stream:
        while chunk := stream.read(CHUNK):
            window = (tail + chunk).upper()
            if any(marker in window for marker in _FORBIDDEN): return True
            tail = window[-_OVERLAP:]
    return False


def _count(archive, path, budget, tag):
    """The number of `tag` elements in a part (streamed, bounded); 1 when the part cannot be read."""
    from .engine import EngineError
    from .xlsx_stream import read_part
    try:
        return max(1, sum(1 for event, item in read_part(archive, path, budget, record_depth=3)
                          if event == "end" and local(item.tag) == tag))
    except EngineError as error:
        if error.body["code"] == "LIMIT" and "Expanded" in error.body["message"]: raise
        return 1


COMMENT_KINDS = {"comments": "comment", "threadedComment": "threadedComment"}  # relationship kind -> counted tag


def relationship_summary(archive, part, budget):
    """The sheet's comment count and whether its relationship part was unreadable, read as streams under the
    package byte budget (a sheet can declare one relationship per hyperlink).

    An absent or empty relationship part declares nothing. One with a DTD or entity rejects the package; any other
    unreadable one is reported (relationships_unreadable) instead of rejecting a file whose cells are valid."""
    from .engine import EngineError, require
    from .xlsx_stream import read_part
    path = relationships_path(part)
    names = archive.namelist()
    if path not in names or archive.getinfo(path).file_size == 0: return Relationships()
    targets = {}  # only the kinds read below, so it never grows with the part
    try:
        for event, item in read_part(archive, path, budget, record_depth=2):
            if event == "end" and local(item.tag) == "Relationship" and item.get("TargetMode") != "External":
                kind = item.get("Type", "").rstrip("/").rsplit("/", 1)[-1]
                if kind not in COMMENT_KINDS or kind in targets: continue
                target = item.get("Target", "")
                targets[kind] = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(part), target))
    except EngineError as error:
        if error.body["code"] == "LIMIT" and "Expanded" in error.body["message"]: raise
        require(not _forbidden(archive, path), "INVALID_SPREADSHEET", "UTF-8 XML without DTDs or entities is required")
        return Relationships(unreadable=True)
    for kind, tag in COMMENT_KINDS.items():
        if kind in targets:
            return Relationships(_count(archive, targets[kind], budget, tag) if targets[kind] in names else 1)
    return Relationships()


def sheet_kinds(sheets, mapping, ns):
    """For each workbook <sheet>, None for a worksheet (or an unknown relationship, which the readers reject) or the
    feature id of a sheet kind that is not imported."""
    kinds = []
    for sheet in sheets:
        relationship = mapping.get(sheet.get(f"{{{ns.rel}}}id"))
        kind = relationship.get("Type", "").rstrip("/").rsplit("/", 1)[-1] if relationship is not None else ""
        kinds.append(SHEET_KINDS.get(kind))
    return kinds


class StringFeatures:
    """Shared strings with formatted runs or phonetic runs, counted as the table is read."""
    def __init__(self, ns):
        self.main = "{" + ns.main + "}"
        self.rich = self.phonetic = 0

    def string(self, item):
        if rich(item, self.main): self.rich += 1
        if phonetic(item, self.main): self.phonetic += 1


def workbook_features(report, workbook, ns, package_names, strings=None, skipped=()):
    """Add the omitted workbook.feature entries of a parsed <workbook>, its package parts and its shared strings.

    `skipped` lists (sheet name, feature id) for the sheets that are not worksheets. Samples stop at MAX_SAMPLES."""
    main = "{" + ns.main + "}"
    counts, samples = {}, {}
    for name, feature in skipped:
        counts[feature] = counts.get(feature, 0) + 1
        kept = samples.setdefault(feature, [])
        if len(kept) < MAX_SAMPLES: kept.append(name)
    skipped_names = {name for name, _ in skipped}

    def add(feature, count=1):
        if count > 0: counts[feature] = counts.get(feature, 0) + count

    for child in workbook:
        if not child.tag.startswith(main): continue  # mc:AlternateContent (absPath), xr:revisionPtr: no content
        name = local(child.tag)
        if name == "definedNames": add("defined_names", sum(1 for item in child if item.tag == main + "definedName"))
        elif name == "externalReferences": add("external_references", sum(1 for item in child if item.tag == main + "externalReference"))
        elif name == "sheets":
            hidden = [item.get("name", "") for item in child if item.tag == main + "sheet" and item.get("state") in ("hidden", "veryHidden")
                      and item.get("name", "") not in skipped_names]
            if hidden: add("hidden_sheets", len(hidden)); samples["hidden_sheets"] = hidden[:MAX_SAMPLES]
        elif name == "workbookProtection":
            if any(child.get(key) in TRUE for key in ("lockStructure", "lockWindows", "lockRevision")): add("workbook_protection")
        elif name == "fileSharing":
            if child.attrib: add("file_sharing")
        elif name == "calcPr":
            if (child.get("calcMode", "auto") != "auto" or child.get("iterate") in TRUE
                    or child.get("fullPrecision") in ("0", "false")): add("calculation_settings")
        elif name == "customWorkbookViews": add("custom_views", sum(1 for item in child if item.tag == main + "customWorkbookView"))
        elif name == "pivotCaches": pass  # counted from the pivot table parts
        elif name == "functionGroups": add("function_groups")
        elif name in ("smartTagPr", "smartTagTypes"): counts["smart_tags"] = 1
        elif name in ("webPublishing", "webPublishObjects"): counts["web_publish"] = 1
        elif name == "extLst":
            for ext in child:
                found = False
                for item in ext.iter():
                    feature = WORKBOOK_EXTENSION_ITEMS.get(local(item.tag))
                    if feature is not None: add(feature); found = True
                if not found and not any(local(item.tag) in WORKBOOK_EXTENSION_SILENT for item in ext): add("extensions")
        # fileVersion, workbookPr (date1904 is imported), bookViews, oleSize, fileRecoveryPr: nothing to lose.
    if strings is not None:
        add("rich_text", strings.rich); add("phonetic", strings.phonetic)
    for feature, pattern in PACKAGE_PARTS:
        add(feature, sum(1 for part in package_names if pattern.fullmatch(part)))
    for feature in WORKBOOK_FEATURES:
        if feature not in counts: continue
        report.add(WORKBOOK_FEATURE, "omitted", feature, count=counts[feature],
                   places=[(sheet, None) for sheet in samples.get(feature, ())])
