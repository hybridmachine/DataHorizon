# SPDX-License-Identifier: GPL-3.0-only
"""Sheet and workbook features of an ODS package that DataHorizon does not import, as report entries.

The ODS counterpart of xlsx_features, with the same categories (sheet.feature, workbook.feature) and feature ids
where the formats share a feature. Both ODS readers feed the same scanners: the tree reader walks each parsed
table:table in document order and the streaming reader passes each element as its end event streams past, so the
entries agree without either holding a sheet. A scanner reads only the element that has just ended and the tags of
its ancestors.

Frozen and split panes are read from settings.xml (view settings, not content). Column widths are reported for the
columns whose width differs from the sheet's most common column width: LibreOffice writes a width for every column,
so only a departure from the width most columns share is a setting worth naming.
"""
import re

from .conversion_report import SHEET_FEATURE, WORKBOOK_FEATURE
from .ods import O, T, X, Y, q
from .xlsx_features import SHEET_FEATURES

CALCEXT = "urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0"
DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
FORM = "urn:oasis:names:tc:opendocument:xmlns:form:1.0"
CONFIG = "urn:oasis:names:tc:opendocument:xmlns:config:1.0"
DRAW_PREFIX = "{" + DRAW + "}"

TABLE, ROW, CELL, COVERED = q(T, "table"), q(T, "table-row"), q(T, "table-cell"), q(T, "covered-table-cell")
COLUMN = q(T, "table-column")
GROUPS = frozenset((q(T, "table-row-group"), q(T, "table-column-group")))
ANNOTATION, LINK, SPAN = q(O, "annotation"), q(X, "a"), q(X, "span")
CONDITIONAL = q(CALCEXT, "conditional-format")
SCENARIO, SHAPES, FORMS = q(T, "scenario"), q(T, "shapes"), q(O, "forms")
HIDDEN = ("collapse", "filter")
# Every non-drawing tag OdsSheetFeatures reads; a streaming caller passes only these (and draw:* elements).
SHEET_TAGS = frozenset((TABLE, ROW, CELL, COVERED, COLUMN, *GROUPS, ANNOTATION, LINK, SPAN, CONDITIONAL, SCENARIO,
                        q(FORM, "form")))

WORKBOOK_FEATURES = ("defined_names", "external_references", "hidden_sheets", "workbook_protection",
                     "calculation_settings", "tracked_changes", "label_ranges", "data_consolidation", "pivot_tables",
                     "embedded_objects", "macros")
NAMED = frozenset((q(T, "named-range"), q(T, "named-expression")))
DATABASE_RANGE, FILTER = q(T, "database-range"), q(T, "filter")
# Every tag OdsWorkbookFeatures reads, wherever it appears in content.xml.
WORKBOOK_TAGS = frozenset((*NAMED, DATABASE_RANGE, q(T, "dde-link"), q(T, "table-source"), q(O, "spreadsheet"),
                           q(T, "iteration"), q(T, "calculation-settings"), q(T, "changed-region"), q(T, "label-range"),
                           q(T, "consolidation"), q(T, "data-pilot-table")))
OBJECT_DIRECTORY = re.compile(r"(Object [^/]+)/")
MACRO_DIRECTORY = re.compile(r"(?:Basic|Scripts)/")


class LayoutStyles:
    """Column, row and table styles by name: the column width, the custom row height and whether a table is shown."""
    def __init__(self):
        self.widths, self.heights, self.hidden_tables = {}, set(), set()

    def add(self, item):
        if item.tag != q(Y, "style"): return
        family, name = item.get(q(Y, "family")), item.get(q(Y, "name"))
        if family == "table-column":
            props = item.find(q(Y, "table-column-properties"))
            self.widths[name] = props.get(q(Y, "column-width")) if props is not None else None
        elif family == "table-row":
            props = item.find(q(Y, "table-row-properties"))
            custom = props is not None and props.get(q(Y, "row-height")) is not None and props.get(q(Y, "use-optimal-row-height")) != "true"
            if custom: self.heights.add(name)
            else: self.heights.discard(name)
        elif family == "table":
            props = item.find(q(Y, "table-properties"))
            if props is not None and props.get(q(T, "display")) == "false": self.hidden_tables.add(name)
            else: self.hidden_tables.discard(name)


def mapped_styles(cell_styles):
    """Names of the cell styles that carry conditions (style:map), from OdsStyles.styles."""
    return frozenset(name for name, item in cell_styles.items() if item.find(q(Y, "map")) is not None)


def _repeats(item, name):
    value = item.get(q(T, name), "1")
    return int(value) if re.fullmatch(r"[1-9][0-9]{0,6}", value) else 1


class OdsSheetFeatures:
    """Counts the unimported features of one table:table as its elements end, then reports them."""
    def __init__(self, layout, mapped=frozenset(), pane=None):
        self.layout, self.mapped, self.pane = layout, mapped, pane
        self.counts, self.widths = {}, {}
        self.validations, self.mapped_used = set(), set()
        self.rich_cell = False

    def add(self, feature, count=1):
        if count > 0: self.counts[feature] = self.counts.get(feature, 0) + count

    def end(self, ancestors, item):
        """An element of the table that has ended; `ancestors` are the tags from the table:table down to its parent."""
        tag = item.tag
        if tag == CELL or tag == COVERED:
            if tag == CELL:
                if _repeats(item, "number-columns-spanned") > 1 or _repeats(item, "number-rows-spanned") > 1:
                    self.add("merged_cells", _repeats(item, "number-columns-repeated"))
                validation = item.get(q(T, "content-validation-name"))
                if validation: self.validations.add(validation)
                style = item.get(q(T, "style-name"))
                if style in self.mapped: self.mapped_used.add(style)
            if self.rich_cell: self.add("rich_text"); self.rich_cell = False
        elif tag == ROW:
            repeats = _repeats(item, "number-rows-repeated")
            if item.get(q(T, "visibility")) in HIDDEN: self.add("hidden_rows_columns", repeats)
            if item.get(q(T, "style-name")) in self.layout.heights: self.add("row_heights", repeats)
        elif tag == COLUMN:
            repeats = _repeats(item, "number-columns-repeated")
            if item.get(q(T, "visibility")) in HIDDEN: self.add("hidden_rows_columns", repeats)
            width = self.layout.widths.get(item.get(q(T, "style-name")))
            self.widths[width] = self.widths.get(width, 0) + repeats
            if item.get(q(T, "default-cell-style-name")) not in (None, "Default"): self.add("row_column_styles", repeats)
        elif tag in GROUPS: self.add("outline")
        elif tag == CONDITIONAL: self.add("conditional_formatting")
        elif tag == SCENARIO: self.add("scenarios")
        elif tag == TABLE and not ancestors:
            if item.get(q(T, "protected")) == "true": self.add("sheet_protection")
            if item.get(q(T, "print-ranges")): self.counts["page_setup"] = 1
        elif tag.startswith(DRAW_PREFIX):
            if ancestors and ancestors[-1] in (SHAPES, CELL): self.add("drawings")
        elif CELL in ancestors or COVERED in ancestors:
            inside_note = ANNOTATION in ancestors
            if tag == ANNOTATION: self.add("comments")
            elif tag == LINK and not inside_note: self.add("hyperlinks")
            elif tag == SPAN and not inside_note and item.get(q(X, "style-name")): self.rich_cell = True
        elif tag == q(FORM, "form") and ancestors and ancestors[-1] == FORMS: self.add("form_controls")

    def report(self, report, sheet, extra=()):
        """Add one omitted entry per feature found, in xlsx_features.SHEET_FEATURES order. `extra` holds
        (feature, count) pairs found outside the table (its auto-filter database ranges)."""
        if len(self.widths) > 1: self.add("column_widths", sum(self.widths.values()) - max(self.widths.values()))
        self.add("data_validation", len(self.validations))
        if "conditional_formatting" not in self.counts: self.add("conditional_formatting", len(self.mapped_used))
        if self.pane: self.add(self.pane)
        for feature, count in extra: self.add(feature, count)
        for feature in SHEET_FEATURES:
            if feature in self.counts: report.add(SHEET_FEATURE, "omitted", feature, sheet=sheet, count=self.counts[feature])


def scan_table(table, features):
    """Feed a parsed table:table to `features` in document order, as the streaming reader does."""
    ancestors = []
    def walk(item):
        ancestors.append(item.tag)
        for child in item: walk(child)
        ancestors.pop()
        features.end(ancestors, item)
    walk(table)


def _range_sheet(address):
    """The sheet of a cell range address such as Sheet1.A1:Sheet1.B3 or $'My sheet'.A1, or None."""
    match = re.match(r"\$?(?:'((?:[^']|'')*)'|([^.' ]+))\.", address or "")
    if match is None: return None
    return match[1].replace("''", "'") if match[1] is not None else match[2]


class OdsWorkbookFeatures:
    """Workbook-wide features of content.xml, read from elements in WORKBOOK_TAGS as they end, plus package parts."""
    def __init__(self):
        self.counts = {}
        self.filters = {}  # sheet name (or None) -> auto-filtered database ranges
        self.tables = []   # (name, style) of each sheet

    def add(self, feature, count=1):
        if count > 0: self.counts[feature] = self.counts.get(feature, 0) + count

    def table(self, item):
        self.tables.append((item.get(q(T, "name"), ""), item.get(q(T, "style-name"))))

    def end(self, item):
        tag = item.tag
        if tag in NAMED: self.add("defined_names")
        elif tag == DATABASE_RANGE:
            if item.get(q(T, "display-filter-buttons")) == "true" or item.find(FILTER) is not None:
                sheet = _range_sheet(item.get(q(T, "target-range-address")))
                self.filters[sheet] = self.filters.get(sheet, 0) + 1
        elif tag in (q(T, "dde-link"), q(T, "table-source")): self.add("external_references")
        elif tag == q(O, "spreadsheet"):
            if item.get(q(T, "structure-protected")) == "true": self.add("workbook_protection")
        elif tag == q(T, "iteration"):
            if item.get(q(T, "status")) == "enable": self.counts["calculation_settings"] = 1
        elif tag == q(T, "calculation-settings"):
            if item.get(q(T, "precision-as-shown")) == "true": self.counts["calculation_settings"] = 1
        elif tag == q(T, "changed-region"): self.add("tracked_changes")
        elif tag == q(T, "label-range"): self.add("label_ranges")
        elif tag == q(T, "consolidation"): self.add("data_consolidation")
        elif tag == q(T, "data-pilot-table"): self.add("pivot_tables")

    def sheet_extra(self, sheet):
        """The (feature, count) pairs reported with a sheet's own features: its auto-filtered ranges."""
        count = self.filters.get(sheet, 0)
        return (("auto_filter", count),) if count else ()

    def report(self, report, layout, package_names):
        hidden = [name for name, style in self.tables if style in layout.hidden_tables]
        self.add("hidden_sheets", len(hidden))
        self.add("embedded_objects", len({m[1] for m in (OBJECT_DIRECTORY.match(n) for n in package_names) if m}))
        if any(MACRO_DIRECTORY.match(name) for name in package_names): self.add("macros")
        for feature in WORKBOOK_FEATURES:
            if feature in self.counts:
                report.add(WORKBOOK_FEATURE, "omitted", feature, count=self.counts[feature],
                           places=[(name, None) for name in hidden] if feature == "hidden_sheets" else None)
        names = {name for name, _ in self.tables}
        # A filtered range whose sheet cannot be named is reported for the workbook.
        stray = sum(count for sheet, count in self.filters.items() if sheet not in names)
        if stray: report.add(SHEET_FEATURE, "omitted", "auto_filter", count=stray)


def scan_content(root, features):
    """Feed a parsed content.xml to `features`, as the streaming survey does."""
    for table in root.findall(f'{q(O, "body")}/{q(O, "spreadsheet")}/{TABLE}'): features.table(table)
    for item in root.iter():
        if item.tag in WORKBOOK_TAGS: features.end(item)


def read_panes(archive, read):
    """(pane modes, readable) from settings.xml, parsed by `read(archive, name)`. settings.xml holds only view
    state, so a part that cannot be read never rejects the package: pane detection is skipped and reported."""
    from .engine import EngineError
    if "settings.xml" not in archive.namelist(): return {}, True
    try: return pane_modes(read(archive, "settings.xml")), True
    except EngineError as error:
        if error.body["code"] == "LIMIT" and "Expanded" in error.body["message"]: raise
        return {}, False


def report_unreadable_settings(report, readable):
    if not readable: report.add(WORKBOOK_FEATURE, "omitted", "view_settings_unreadable")


def pane_modes(settings):
    """Sheet name -> "frozen_panes" or "split_panes", from a parsed settings.xml (None when absent)."""
    modes = {}
    if settings is None: return modes
    for tables in settings.iter(q(CONFIG, "config-item-map-named")):
        if tables.get(q(CONFIG, "name")) != "Tables": continue
        for entry in tables.findall(q(CONFIG, "config-item-map-entry")):
            values = {item.get(q(CONFIG, "name")): (item.text or "").strip() for item in entry.findall(q(CONFIG, "config-item"))}
            split = (values.get("HorizontalSplitMode"), values.get("VerticalSplitMode"))
            if "2" in split: modes[entry.get(q(CONFIG, "name"), "")] = "frozen_panes"
            elif "1" in split: modes[entry.get(q(CONFIG, "name"), "")] = "split_panes"
    return modes
