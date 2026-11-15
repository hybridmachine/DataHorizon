# SPDX-License-Identifier: GPL-3.0-only
"""Format-neutral chart import: conversion rules, record checks and creation.

A format reader hands ``convert`` a draft (see the design, section 4) and gets a
record: a ``chart_create`` request whose range sources name a worksheet by its
position among the imported worksheets.  ``for_sheet`` rebases records for the
one-sheet import and ``create`` stores them on a workbook inside an operation.
"""
import copy
import re

from . import charts
from .chart_render import LIMIT_PIE_POINTS, LIMIT_POINTS, LIMIT_SERIES
from .engine import MAX_COLS, MAX_ROWS, EngineError
from .formula import FormulaError, column_name, split_ref
from .interchange import fold_name

UNUSABLE = object()
# What a chart converts without; the vocabulary of a draft's ``styled`` list.
STYLED = ("colours", "data labels", "gap width", "markers", "smoothing", "gridlines", "number formats", "fonts")

SKIPPED = "chart.skipped"
CONVERTED = "chart.converted"
NAME_LENGTH = 64
SIZE_LIMITS = ((64, 4096), (48, 4096))
OFFSET_LIMIT = 4096
RECORD_KEYS = ("sheet", "name", "anchor", "offset", "size", "spec")
SOURCE_PARTS = ("categories", "name", "values", "x")

# Shared by the format readers: default cell sizes, the measurement budget, series cap and draft defaults.
DEFAULT_COLUMN_PX = 64
DEFAULT_ROW_PX = 20
MAX_STEPS = 20_000  # cell sizes asked of the caller, in total for one budget
MAX_DRAFT_SERIES = LIMIT_SERIES + 1  # one more than convert accepts is enough for it to skip the chart as ``limit``
DEFAULT_GROUPING = {"bar": "clustered", "line": "standard", "area": "standard"}
EMPTY_TITLE = {"text": None, "from_cell": False}


class Steps:
    """What is left of a budget of per-cell size lookups, and whether it ever ran out."""
    def __init__(self, limit=MAX_STEPS):
        self.left = limit
        self.exhausted = False

    def take(self):
        if self.left <= 0:
            self.exhausted = True
            return False
        self.left -= 1
        return True


def locate(position, size, default, limit, steps):
    """The 1-based cell containing pixel ``position`` and the offset into it.

    ``size(index)`` is the pixel size of the 1-based cell ``index`` (``None`` for the default sizes); it is asked
    only while ``steps`` allows, and past that the cells are measured with ``default``."""
    index = 1
    while size is not None and index < limit and steps.take():
        step = size(index)
        if step <= 0 or position < step:
            return index, position
        position -= step
        index += 1
    skipped, offset = divmod(position, default)
    return min(index + skipped, limit), offset


# References.

_XLSX_CELL = r"\$?([A-Za-z]{1,3})\$?([0-9]+)"
_XLSX_AREA = re.compile(rf"(?:{_XLSX_CELL}(?::{_XLSX_CELL})?|\$?([A-Za-z]{{1,3}}):\$?([A-Za-z]{{1,3}})|\$?([0-9]+):\$?([0-9]+))")
_XLSX_SHEET = re.compile(r"(?:'((?:[^']|'')+)'|([^'!\[\]()\{\},;:\s]+))!(.+)", re.DOTALL)
_ODS_SHEET = r"\$?(?:'((?:[^']|'')+)'|([^.\s'$:\[\]()]+))"
_ODS_CELL = r"\$?([A-Za-z]{1,3})\$?([0-9]+)"
_ODS_REF = re.compile(rf"{_ODS_SHEET}\.{_ODS_CELL}(?::(?:{_ODS_SHEET}?\.)?{_ODS_CELL})?")


def _column_number(letters):
    number = 0
    for letter in letters.upper():
        number = number * 26 + ord(letter) - 64
    return number


def _rectangle(sheet, rows, cols):
    if not (1 <= min(rows) and max(rows) <= MAX_ROWS and 1 <= min(cols) and max(cols) <= MAX_COLS):
        return UNUSABLE
    return (sheet, min(rows), min(cols), max(rows), max(cols))


def _parse_xlsx(text):
    found = _XLSX_SHEET.fullmatch(text)
    if found is None:
        return UNUSABLE
    sheet = found.group(1).replace("''", "'") if found.group(1) is not None else found.group(2)
    if "[" in sheet or "]" in sheet:
        return UNUSABLE
    area = _XLSX_AREA.fullmatch(found.group(3))
    if area is None:
        return UNUSABLE
    first_col, first_row, last_col, last_row, col_a, col_b, row_a, row_b = area.groups()
    if first_col is not None:
        last_col, last_row = (first_col, first_row) if last_col is None else (last_col, last_row)
        return _rectangle(sheet, (int(first_row), int(last_row)), (_column_number(first_col), _column_number(last_col)))
    if col_a is not None:
        return _rectangle(sheet, (1, MAX_ROWS), (_column_number(col_a), _column_number(col_b)))
    return _rectangle(sheet, (int(row_a), int(row_b)), (1, MAX_COLS))


def _parse_ods(text):
    found = _ODS_REF.fullmatch(text)
    if found is None:
        return UNUSABLE
    quoted, bare, first_col, first_row, quoted2, bare2, last_col, last_row = found.groups()
    sheet = quoted.replace("''", "'") if quoted is not None else bare
    if last_col is None:
        last_col, last_row = first_col, first_row
    else:
        other = quoted2.replace("''", "'") if quoted2 is not None else bare2
        if other is not None and other != sheet:
            return UNUSABLE
    return _rectangle(sheet, (int(first_row), int(last_row)), (_column_number(first_col), _column_number(last_col)))


def parse_reference(text, dialect):
    """The ``(sheet, row1, col1, row2, col2)`` a spreadsheet reference names, or ``UNUSABLE``."""
    if not isinstance(text, str):
        return UNUSABLE
    return (_parse_xlsx if dialect == "xlsx" else _parse_ods)(text.strip())


# Conversion.

def _a1(ref):
    _, row1, col1, row2, col2 = ref
    first, last = column_name(col1) + str(row1), column_name(col2) + str(row2)
    return first if first == last else f"{first}:{last}"


def _clamp(value, low, high):
    return min(max(value, low), high)


def _clean_name(name):
    """A stored-valid chart name, or ``"Chart"``, for the frame name a file gave."""
    if not isinstance(name, str) or any(ord(char) < 32 or ord(char) == 127 for char in name):
        return "Chart"
    return name[:NAME_LENGTH].strip() or "Chart"


class _Skip(Exception):
    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


def _plot_type(plots):
    """The native type of a draft's single plot, and whether it was a 3D or doughnut variant."""
    if not plots:
        raise _Skip("type")
    kinds = []
    for item in plots:
        kind = item.get("kind")
        if kind == "bar":
            kind = {"col": "column", "bar": "bar"}.get(item.get("direction"))
        elif kind == "doughnut":
            kind = "pie"
        if kind not in charts.TYPES:
            raise _Skip("type")
        kinds.append(kind)
    if len(plots) > 1:
        raise _Skip("combination")
    return kinds[0]


def _extent(ref):
    return (ref[3] - ref[1] + 1) * (ref[4] - ref[2] + 1)


class _Converter:
    def __init__(self, draft, sheet_names, report):
        self.draft, self.report = draft, report
        self.folded = [fold_name(name) for name in sheet_names]
        self.sheet = sheet_names[draft["sheet"]]
        self.label = draft.get("name") if isinstance(draft.get("name"), str) else "Chart"

    def note(self, detail, severity="converted", category=CONVERTED, cell=None):
        self.report.add(category, severity, detail, sheet=self.sheet, cell=(self.label if cell is None else cell)[:32])

    def position(self, ref):
        folded = fold_name(ref[0])
        return self.folded.index(folded) if folded in self.folded else None

    def source(self, ref, role):
        """The range source for a bound reference; skips the chart when the reference cannot be bound exactly."""
        if not isinstance(ref, tuple) or self.position(ref) is None:
            raise _Skip("source")
        if role != "name" and ref[1] != ref[3] and ref[2] != ref[4]:
            raise _Skip("source")
        return {"kind": "range", "sheet": self.position(ref), "ref": _a1(ref)}, _extent(ref)

    def convert(self):
        draft = self.draft
        plot = draft["plots"][0] if len(draft["plots"]) == 1 else None
        kind = _plot_type(draft["plots"])
        if any(item.get("secondary") for item in draft["plots"]):
            raise _Skip("secondary_axis")
        series = draft["series"]
        if not series:
            raise _Skip("source")
        if kind == "pie" and len(series) != 1:
            raise _Skip("pie_series")
        total = 0
        counts = []

        def bound(ref, role):
            nonlocal total
            source, count = self.source(ref, role)
            counts.append((role, count))
            total += count
            return {"source": source}

        categories = None
        if kind != "scatter":
            first = next((item["categories"] for item in series if item.get("categories") is not None), None)
            if first is not None:
                categories = bound(first, "categories")
                if any(item.get("categories") is not None and item["categories"] != first for item in series):
                    self.note("categories_differ")
        out_series = []
        for number, item in enumerate(series, 1):
            name = item.get("name")
            if isinstance(name, dict) and isinstance(name.get("text"), str):
                out_name = {"text": name["text"]}
            else:
                out_name = {"text": f"Series {number}"}
                ref = name.get("ref") if isinstance(name, dict) else None
                if isinstance(ref, tuple) and self.position(ref) is not None and _extent(ref) == 1:
                    out_name = bound(ref, "name")
                elif name is not None:
                    self.note("series_name")
            values = bound(item.get("values"), "values")
            x = bound(item.get("x"), "x") if kind == "scatter" else None
            out_series.append({"name": out_name, "values": values, "x": x})
        if len(series) > LIMIT_SERIES or total > charts.LIMIT_CELLS:
            raise _Skip("limit")
        pie_limit = kind == "pie"
        for role, count in counts:
            if role != "name" and count > (LIMIT_PIE_POINTS if pie_limit and role in ("values", "categories") else LIMIT_POINTS):
                raise _Skip("limit")
        place = draft["anchor"]
        if not (1 <= place["row"] <= MAX_ROWS and 1 <= place["col"] <= MAX_COLS):
            raise _Skip("off_grid")

        # Nothing below can skip the chart.
        if plot is not None and plot.get("kind") == "doughnut":
            self.note("doughnut")
        if any(item.get("three_d") for item in draft["plots"]):
            self.note("three_d")
        grouping = None
        if kind in charts.GROUPED_TYPES:
            grouping = "stacked" if plot.get("grouping") in ("stacked", "percentStacked") else "clustered"
            if plot.get("grouping") == "percentStacked":
                self.note("percent_stacked")
        elif plot is not None and plot.get("grouping") in ("stacked", "percentStacked"):
            self.note("grouping")
        for item in draft.get("styled") or ():
            self.note(f"styling: {item}")
        title = self.text(draft["title"], cut=charts.TITLE_LENGTH)
        axis = draft["axis_titles"]
        low, high = draft.get("value_min"), draft.get("value_max")
        if not all(charts._is_number(value) for value in (low, high) if value is not None) or (
                low is not None and high is not None and low > high):
            low = high = None
        legend = draft.get("legend")
        if legend in ("top", "left"):
            self.note("legend_position")
        position = legend if legend in ("right", "bottom") else "none" if legend is None else "right"
        if draft.get("approximate_size"):
            self.note("size_approximate")
        if draft.get("absolute_anchor"):
            self.note("anchor_absolute")
        name = _clean_name(draft.get("name"))
        if name != draft.get("name"):
            self.note("name")
        size, anchor, offset = draft["size"], draft["anchor"], draft["offset"]
        return {
            "sheet": draft["sheet"], "name": name,
            "anchor": {"row": _clamp(anchor["row"], 1, MAX_ROWS), "col": _clamp(anchor["col"], 1, MAX_COLS)},
            "offset": {"x": _clamp(offset["x"], 0, OFFSET_LIMIT), "y": _clamp(offset["y"], 0, OFFSET_LIMIT)},
            "size": {"width": _clamp(size["width"], *SIZE_LIMITS[0]), "height": _clamp(size["height"], *SIZE_LIMITS[1])},
            "spec": {"spec_version": charts.SPEC_VERSION, "type": kind, "grouping": grouping,
                     "title": {"text": title}, "categories": categories, "series": out_series,
                     "axes": {"category": {"title": self.text(axis["category"])},
                              "value": {"title": self.text(axis["value"]), "min": low, "max": high}},
                     "legend": {"position": position}}}

    def text(self, item, cut=None):
        text = item.get("text")
        if text is None:
            return None
        if item.get("from_cell"):
            self.note("title_from_cell")
        if cut is not None and len(text) > cut:
            self.note("title_cut")
            return text[:cut]
        return text


def convert(draft, sheet_names, report):
    """The record for a reader's draft, or ``None`` (with a ``chart.skipped`` report entry) when it cannot be imported."""
    worker = _Converter(draft, sheet_names, report)
    try:
        return worker.convert()
    except _Skip as skip:
        worker.note(skip.detail, "omitted", SKIPPED)
        return None


# Records.

def _argument(message):
    return EngineError("INVALID_ARGUMENT", message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _record_sources(spec):
    """Every source of a record's specification, in place; refuses a spec of the wrong shape."""
    if not isinstance(spec, dict) or not isinstance(spec.get("series"), list):
        raise _argument("A chart record's spec must hold a series list")
    parts = [spec.get("categories")]
    for item in spec["series"]:
        if not isinstance(item, dict):
            raise _argument("A chart series must be an object")
        parts.extend(item.get(role) for role in ("name", "values", "x"))
    sources = []
    for part in parts:
        if part is None or (isinstance(part, dict) and set(part) == {"text"}):
            continue
        if not isinstance(part, dict) or not isinstance(part.get("source"), dict):
            raise _argument("A chart source must be an object")
        sources.append(part["source"])
    return sources


def _check_record_shape(item):
    name = item["name"]
    if not isinstance(name, str) or not 1 <= len(name) <= NAME_LENGTH:
        raise _argument(f"A chart record's name needs 1-{NAME_LENGTH} characters")
    for key, limits in (("anchor", {"row": (1, MAX_ROWS), "col": (1, MAX_COLS)}),
                        ("offset", {"x": (0, OFFSET_LIMIT), "y": (0, OFFSET_LIMIT)}),
                        ("size", {"width": SIZE_LIMITS[0], "height": SIZE_LIMITS[1]})):
        value = item[key]
        if not isinstance(value, dict) or set(value) != set(limits):
            raise _argument(f"A chart record's {key} needs: {', '.join(limits)}")
        for part, (low, high) in limits.items():
            if not _is_int(value[part]) or not low <= value[part] <= high:
                raise _argument(f"A chart record's {key} {part} must be an integer from {low} to {high}")


def _check_ref(ref):
    parts = ref.split(":") if isinstance(ref, str) else []
    try:
        corners = [split_ref(part) for part in parts] if 1 <= len(parts) <= 2 else None
    except FormulaError:
        corners = None
    if corners is None or any(corner[0] is not None or not 1 <= corner[1] <= MAX_ROWS or not 1 <= corner[2] <= MAX_COLS
                              for corner in corners):
        raise _argument("A chart source ref must be one cell or one rectangle such as A2:A13")


def check_records(records, sheet_count):
    """Check chart records from a client; raises ``INVALID_ARGUMENT``.  Specification details are checked at creation."""
    if not isinstance(records, list):
        raise _argument("charts must be a list")
    if len(records) > charts.LIMIT_CHARTS:
        raise _argument(f"charts holds more than {charts.LIMIT_CHARTS} records")
    for item in records:
        if not isinstance(item, dict):
            raise _argument("A chart record must be an object")
        unknown = sorted(set(item) - set(RECORD_KEYS))
        if unknown:
            raise _argument(f"A chart record has unknown keys: {', '.join(map(str, unknown))}")
        if not _is_int(item.get("sheet")) or not 0 <= item["sheet"] < sheet_count:
            raise _argument("A chart record's sheet is outside the imported sheets")
        if set(item) != set(RECORD_KEYS):
            raise _argument(f"A chart record needs: {', '.join(RECORD_KEYS)}")
        _check_record_shape(item)
        if set(item["spec"]) - set(charts.SPEC_KEYS) if isinstance(item["spec"], dict) else True:
            raise _argument("A chart record's spec must be an object with specification keys only")
        for source in _record_sources(item["spec"]):
            if source.get("kind") != "range" or not _is_int(source.get("sheet")) or not 0 <= source["sheet"] < sheet_count:
                raise _argument("A chart source must be a range on one of the imported sheets")
            _check_ref(source.get("ref"))
    return records


def for_sheet(records, position, row, col, sheet_name, report):
    """Records of one worksheet rebased to sheet 0 and shifted for a paste at ``row``, ``col``."""
    down, right = row - 1, col - 1
    kept = []
    for item in records:
        if item["sheet"] != position:
            continue
        chart = copy.deepcopy(item)
        where = {"sheet": sheet_name, "cell": chart["name"][:32]}
        sources = _record_sources(chart["spec"])
        if any(source["sheet"] != position for source in sources):
            report.add(SKIPPED, "omitted", "source", **where)
            continue
        chart["sheet"] = 0
        chart["anchor"] = {"row": chart["anchor"]["row"] + down, "col": chart["anchor"]["col"] + right}
        fits = chart["anchor"]["row"] <= MAX_ROWS and chart["anchor"]["col"] <= MAX_COLS
        for source in sources:
            source["sheet"] = 0
            corners = [split_ref(part) for part in source["ref"].split(":")]
            rows = [corner[1] + down for corner in corners]
            cols = [corner[2] + right for corner in corners]
            fits = fits and max(rows) <= MAX_ROWS and max(cols) <= MAX_COLS
            first, last = column_name(cols[0]) + str(rows[0]), column_name(cols[-1]) + str(rows[-1])
            source["ref"] = first if len(corners) == 1 else f"{first}:{last}"
        if fits:
            kept.append(chart)
        else:
            report.add(SKIPPED, "omitted", "off_grid", **where)
    return kept


def summary(records, sheet_names):
    """What a review lists for each chart record."""
    return [{"name": item["name"], "sheet": sheet_names[item["sheet"]], "type": item["spec"]["type"]} for item in records]


def _unique(name, taken):
    if name not in taken:
        return name
    number = 2
    while True:
        suffix = f" ({number})"
        candidate = name[:NAME_LENGTH - len(suffix)].rstrip() + suffix
        if candidate not in taken:
            return candidate
        number += 1


def create(book, records, sheet_ids):
    """Store each record as a chart on ``sheet_ids[record['sheet']]``; call inside an open engine operation.

    Charts past the workbook limit are left out and named in ``left_out``."""
    stored = charts.objects(book)
    count = len(stored)
    taken = {}
    for owner, _, payload in stored:
        taken.setdefault(owner, set()).add(payload["name"])
    created, left_out = 0, []
    for item in records:
        if count >= charts.LIMIT_CHARTS:
            left_out.append(item["name"])
            continue
        sheet_id = sheet_ids[item["sheet"]]
        request = copy.deepcopy(item)
        del request["sheet"]
        request["name"] = _unique(item["name"], taken.setdefault(sheet_id, set()))
        for source in _record_sources(request["spec"]):
            source["sheet"] = sheet_ids[source["sheet"]]
        charts.create_chart(book, sheet_id, request)
        taken[sheet_id].add(request["name"])
        created += 1
        count += 1
    return {"created": created, "left_out": left_out}
