# SPDX-License-Identifier: GPL-3.0-only
"""Read a ``draw:frame`` chart object of an ODS worksheet into a format-neutral chart draft (see import_charts.convert).

The frame names an object directory of the package; its ``content.xml`` holds ``office:chart`` with one
``chart:chart``.  Packages are untrusted: the object's part is read only through the ``read_part`` callable the
caller passes (so the caller's budget applies), the object's path must stay inside the package, and a chart that
cannot be read is reported and omitted, never raised.  A frame that holds something else (an image, a formula, an
embedded document) is not a chart and is passed over without a report.
"""
import math
import re
import xml.etree.ElementTree as ET

from .conversion_report import ConversionReport
from .engine import MAX_COLS, MAX_ROWS
from .import_charts import SKIPPED, STYLED, parse_reference
from .ods import O, T, X, Y, q
from .ods_features import CHART, DRAW, SVG, XLINK
from .xlsx_charts import DEFAULT_COLUMN, DEFAULT_ROW, _locate

MAX_STEPS = 20_000  # cell sizes asked of the caller, in total for one frame
MAX_PIXELS = 1 << 30
PIXELS = {"in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4, "pt": 96 / 72, "pc": 16.0, "px": 1.0}
LENGTH = re.compile(r"\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*(in|cm|mm|pt|pc|px)\s*")
LEGEND_POSITIONS = {"end": "right", "start": "left", "top": "top", "bottom": "bottom", "top-start": "top",
                    "top-end": "top", "bottom-start": "bottom", "bottom-end": "bottom"}
KINDS = {"circle": "pie", "ring": "doughnut"}
DEFAULT_GROUPING = {"bar": "clustered", "line": "standard", "area": "standard"}
EMPTY_TITLE = {"text": None, "from_cell": False}

NAME = q(DRAW, "name")
HREF = q(XLINK, "href")
STYLE_NAME, CLASS = q(CHART, "style-name"), q(CHART, "class")
STYLE_FAMILY = q(Y, "family")
BODY, DOCUMENT = q(O, "body"), q(O, "document-content")
SERIES_STYLED = frozenset(q(CHART, name) for name in ("chart", "plot-area", "axis", "series", "data-point"))


class _Unreadable(Exception):
    pass


class _NotChart(Exception):
    pass


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _kids(element, uri, name):
    return [child for child in element if child.tag == q(uri, name)]


def _kid(element, uri, name):
    if element is not None:
        for child in element:
            if child.tag == q(uri, name):
                return child
    return None


# Package access.

def _object_content(href, names):
    """The ``content.xml`` of the object directory ``href`` names, or ``None`` if it leaves the package or is absent."""
    if not isinstance(href, str) or "\\" in href or "\0" in href:
        return None
    path = (href[2:] if href.startswith("./") else href).rstrip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        return None
    content = path + "/content.xml"
    return content if content in names else None


# Placement.

def _pixels(text, default=0):
    """Pixels (96 DPI) of an ODF length; ``default`` for an absent one; raises ``_Unreadable`` for a malformed one."""
    if text is None:
        return default
    found = LENGTH.fullmatch(text)
    if found is None:
        raise _Unreadable
    return max(0, min(int(float(found.group(1)) * PIXELS[found.group(2)] + 0.5), MAX_PIXELS))


class _Steps:
    """What is left of a frame's budget of per-cell size lookups, and whether it ever ran out."""
    def __init__(self):
        self.left = MAX_STEPS
        self.exhausted = False

    def take(self):
        if self.left <= 0:
            self.exhausted = True
            return False
        self.left -= 1
        return True


def _placement(frame, cell, widths):
    """``(row, col), (x, y), (width, height), absolute, approximate`` of a frame."""
    x, y = _pixels(frame.get(q(SVG, "x"))), _pixels(frame.get(q(SVG, "y")))
    size = (_pixels(frame.get(q(SVG, "width"))), _pixels(frame.get(q(SVG, "height"))))
    if cell is not None:
        return (cell[0], cell[1]), (x, y), size, False, False
    column, row = widths if widths is not None else (None, None)
    steps = _Steps()
    col, offset_x = _locate(x, column, DEFAULT_COLUMN, MAX_COLS, steps)
    number, offset_y = _locate(y, row, DEFAULT_ROW, MAX_ROWS, steps)
    return (number, col), (offset_x, offset_y), size, True, steps.exhausted


# The chart.

def _styles(root):
    """``{style name: {attribute: value}}`` over the property elements of the automatic chart styles."""
    found = {}
    for styles in _kids(root, O, "automatic-styles"):
        for style in _kids(styles, Y, "style"):
            merged = found.setdefault(style.get(q(Y, "name")), {})
            for properties in style:
                merged.update(properties.attrib)
    return found


def _text(element):
    """The paragraphs of a ``chart:title`` joined by newlines, or ``None``."""
    if element is None:
        return dict(EMPTY_TITLE)
    paragraphs = ["".join(paragraph.itertext()) for paragraph in element.iter(q(X, "p"))]
    return {"text": "\n".join(paragraphs) or None, "from_cell": False}


def _flag(properties, name):
    return properties.get(q(CHART, name)) == "true"


def _limit(properties, name):
    try:
        number = float(properties.get(q(CHART, name)))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _reference(element, uri, name):
    text = None if element is None else element.get(q(uri, name))
    return None if text is None else parse_reference(text, "ods")


def _kind(name):
    name = (name or "").rpartition(":")[2]
    return KINDS.get(name, name)


def _plot(kind, properties, secondary):
    grouping = DEFAULT_GROUPING.get(kind)
    if grouping is not None:
        grouping = "percentStacked" if _flag(properties, "percentage") else "stacked" if _flag(properties, "stacked") else grouping
    return {"kind": kind, "direction": ("bar" if _flag(properties, "vertical") else "col") if kind == "bar" else None,
            "grouping": grouping, "three_d": _flag(properties, "three-dimensional"), "secondary": secondary}


def _styled(chart, styles):
    """The dropped styling a chart carries, in the order of ``STYLED``."""
    found = set()
    if any(element.tag == q(CHART, "grid") for element in chart.iter()):
        found.add("gridlines")
    for element in chart.iter():
        if element.tag not in SERIES_STYLED:
            continue
        properties = styles.get(element.get(STYLE_NAME), {})
        if q(DRAW, "fill-color") in properties and properties.get(q(DRAW, "fill")) != "none":
            found.add("colours")
        if properties.get(q(CHART, "data-label-number"), "none") != "none":
            found.add("data labels")
        if q(CHART, "gap-width") in properties or q(CHART, "overlap") in properties:
            found.add("gap width")
        if properties.get(q(CHART, "symbol-type"), "none") != "none":
            found.add("markers")
        if properties.get(q(CHART, "interpolation"), "none") != "none":
            found.add("smoothing")
    return [item for item in STYLED if item in found]


def _read_chart(root):
    """The chart-specific part of a draft from an object's ``content.xml`` root.

    Raises ``_NotChart`` for another kind of object and ``_Unreadable`` for a part that is neither."""
    if _local(root.tag) == "math":
        raise _NotChart
    body = _kid(root, O, "body") if root.tag == DOCUMENT else None
    if body is None or len(body) == 0:
        raise _Unreadable
    holder = body[0]
    if holder.tag != q(O, "chart"):
        raise _NotChart
    chart = _kid(holder, CHART, "chart")
    area = _kid(chart, CHART, "plot-area")
    if area is None:
        raise _Unreadable
    styles = _styles(root)
    own = styles.get(area.get(STYLE_NAME), {})
    axes = _kids(area, CHART, "axis")
    horizontal = next((item for item in axes if item.get(q(CHART, "dimension")) == "x"), None)
    values = [item for item in axes if item.get(q(CHART, "dimension")) == "y"]
    names = [item.get(q(CHART, "name")) for item in values]
    primary = "primary-y" if "primary-y" in names else (names[0] if names else None)
    vertical = next((item for item in values if item.get(q(CHART, "name")) == primary), None)
    limits = styles.get(vertical.get(STYLE_NAME), {}) if vertical is not None else {}
    categories = _kid(horizontal, CHART, "categories")
    if categories is None:
        categories = next(area.iter(q(CHART, "categories")), None)
    shared = _reference(categories, T, "cell-range-address")
    chart_kind = _kind(chart.get(CLASS))
    plots, series = [], []
    for element in _kids(area, CHART, "series"):
        kind = _kind(element.get(CLASS)) or chart_kind
        attached = element.get(q(CHART, "attached-axis"))
        secondary = attached is not None and attached in names and attached != primary
        if (kind, secondary) not in [(item["kind"], item["secondary"]) for item in plots]:
            plots.append(_plot(kind, own, secondary))
        scatter = kind in ("scatter", "bubble")
        label = _reference(element, CHART, "label-cell-address")
        series.append({"name": None if label is None else {"ref": label},
                       "values": _reference(element, CHART, "values-cell-range-address"),
                       "categories": None if scatter else shared,
                       "x": _reference(_kid(element, CHART, "domain"), T, "cell-range-address") if scatter else None})
    legend = _kid(chart, CHART, "legend")
    return {"plots": plots or [_plot(chart_kind, own, False)],
            "title": _text(_kid(chart, CHART, "title")),
            "axis_titles": {"category": _text(_kid(horizontal, CHART, "title")), "value": _text(_kid(vertical, CHART, "title"))},
            "value_min": _limit(limits, "minimum"), "value_max": _limit(limits, "maximum"),
            "legend": None if legend is None else LEGEND_POSITIONS.get(legend.get(q(CHART, "legend-position"), "end"), "right"),
            "series": series, "styled": _styled(chart, styles)}


def read_frame_draft(archive, frame: ET.Element, position: int, sheet_name: str, order: int, read_part,
                     report: ConversionReport, cell, widths=None):
    """The draft of the chart a ``draw:frame`` holds, or ``None``.

    ``None`` without a report: the frame is not a chart object.  ``None`` with a ``chart.skipped`` entry
    (``unreadable``): the object cannot be read.  ``read_part(archive, name)`` returns the parsed root of a package
    part under the caller's budget.  ``cell`` is the 1-based ``(row, col)`` of the cell holding the frame, or
    ``None`` for a frame in ``table:shapes``, which is placed by its absolute position.  ``widths`` is an optional
    ``(column_width_px(col), row_height_px(row))`` pair of callables over 1-based indexes; without it the default
    cell sizes, 64 and 20 pixels, are used.  The callables are asked at most ``MAX_STEPS`` times for a frame; past
    that the default sizes are used and the draft is marked ``approximate_size``."""
    label = frame.get(NAME) or f"Chart {order + 1}"
    try:
        element = _kid(frame, DRAW, "object")
        if element is None:
            return None
        part = _object_content(element.get(HREF), set(archive.namelist()))
        root = read_part(archive, part) if part is not None else None
        if root is None:
            raise _Unreadable
        body = _read_chart(root)
        (row, col), (x, y), (width, height), absolute, approximate = _placement(frame, cell, widths)
        return {"sheet": position, "name": label, "order": order, **body,
                "anchor": {"row": row, "col": col}, "offset": {"x": x, "y": y},
                "size": {"width": width, "height": height}, "approximate_size": approximate, "absolute_anchor": absolute}
    except _NotChart:
        return None
    except Exception:  # an untrusted chart never fails the import, whatever is wrong with it
        report.add(SKIPPED, "omitted", "unreadable", sheet=sheet_name, cell=label[:32], count=1)
        return None
