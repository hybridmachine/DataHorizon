# SPDX-License-Identifier: GPL-3.0-only
"""Read a worksheet's drawing and chart parts into format-neutral chart drafts (see import_charts.convert).

Path: worksheet part -> its relationships -> the ``drawing`` relationship -> drawing part -> each anchor's
graphicFrame -> ``r:id`` -> chart part.  Elements are matched by local name, so Transitional and Strict
packages read alike.  Packages are untrusted: every part is read through the package byte budget, relationship
targets must stay inside the package, and a chart that cannot be read is reported and omitted, never raised.
"""
import math
import posixpath

from .conversion_report import ConversionReport
from .engine import MAX_COLS, MAX_ROWS, EngineError
from .import_charts import (DEFAULT_COLUMN_PX, DEFAULT_GROUPING, DEFAULT_ROW_PX, EMPTY_TITLE, MAX_DRAFT_SERIES, MAX_STEPS,
                            SKIPPED, STYLED, UNUSABLE, Steps, locate, parse_reference)
from .xlsx_features import local, relationships_path
from .xlsx_stream import _read_whole

EMU_PER_PIXEL = 9525
MAX_FRAMES = 256  # graphic frames met in one worksheet drawing
MAX_EMU = 1 << 40
CHARTEX = "chartex"
ANCHORS = ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor")
LEGEND_POSITIONS = {"r": "right", "b": "bottom", "t": "top", "l": "left", "tr": "right"}
FILLS = ("solidFill", "gradFill", "pattFill", "blipFill")
LABEL_FLAGS = ("showLegendKey", "showVal", "showCatName", "showSerName", "showPercent", "showBubbleSize")


class _Unreadable(Exception):
    pass


def _kids(element, name):
    return [child for child in element if local(child.tag) == name]


def _kid(element, name):
    if element is not None:
        for child in element:
            if local(child.tag) == name:
                return child
    return None


def _val(element, name, default=None):
    child = _kid(element, name)
    return default if child is None else child.get("val", default)


def _true(text, default=True):
    return default if text is None else text.strip().lower() in ("1", "true")


def _namespace(tag):
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


# Package access.

def _read(archive, name, budget):
    """The root of a package part, or ``None`` when it is missing, malformed or would exceed the byte budget.

    A part that would not fit is not charged, so the budget keeps behaving as it would without this reader."""
    try:
        size = archive.getinfo(name).file_size
    except (KeyError, ValueError):
        return None
    if budget.used + size > budget.limit:
        return None
    try:
        return _read_whole(archive, name, budget)
    except EngineError:
        return None


def _resolve(base, target, names):
    """The package part a relationship of ``base`` names, or ``None`` if it leaves the package or is absent."""
    if not isinstance(target, str) or "\\" in target or "\0" in target:
        return None
    path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(base), target))
    if path.startswith("..") or path.startswith("/") or path not in names:
        return None
    return path


def _relationships(archive, part, budget, names):
    """``{id: (kind, target)}`` for the internal relationships of ``part``; ``None`` when unreadable, ``{}`` when absent."""
    path = relationships_path(part)
    if path not in names:
        return {}
    root = _read(archive, path, budget)
    if root is None:
        return None
    found = {}
    for item in _kids(root, "Relationship"):
        if item.get("TargetMode") != "External" and item.get("Id") is not None:
            found[item.get("Id")] = (item.get("Type", "").rstrip("/").rsplit("/", 1)[-1], item.get("Target", ""))
    return found


def _relationship_id(element):
    for key, value in element.attrib.items():
        if key.startswith("{") and key.endswith("}id") and "relationships" in key:
            return value
    return None


# Anchors.

def _pixels(emu):
    return int(emu / EMU_PER_PIXEL + 0.5)


def _number(text):
    try:
        return max(0, min(int((text or "0").strip()), MAX_EMU))
    except ValueError as error:
        raise _Unreadable from error


def _corner(element):
    """``(col, col offset px, row, row offset px)`` of an ``xdr:from`` or ``xdr:to`` element (0-based cells)."""
    if element is None:
        raise _Unreadable
    values = {name: _number(getattr(_kid(element, name), "text", None)) for name in ("col", "colOff", "row", "rowOff")}
    return values["col"], _pixels(values["colOff"]), values["row"], _pixels(values["rowOff"])


def _attribute_pair(element, first, second):
    if element is None:
        raise _Unreadable
    return _pixels(_number(element.get(first))), _pixels(_number(element.get(second)))


def _distance(first, offset_first, last, offset_last, size, default, steps):
    """Pixels from ``offset_first`` into 0-based cell ``first`` to ``offset_last`` into cell ``last``."""
    total = offset_last - offset_first
    index = first
    while size is not None and index < last and steps.take():
        total += size(index + 1)
        index += 1
    return total + max(last - index, 0) * default


def _placement(anchor, widths, steps):
    """``(row, col), (x, y), (width, height), absolute`` of a drawing anchor element."""
    column, row = widths if widths is not None else (None, None)
    kind = local(anchor.tag)
    if kind == "twoCellAnchor":
        col1, x1, row1, y1 = _corner(_kid(anchor, "from"))
        col2, x2, row2, y2 = _corner(_kid(anchor, "to"))
        size = (max(_distance(col1, x1, col2, x2, column, DEFAULT_COLUMN_PX, steps), 0),
                max(_distance(row1, y1, row2, y2, row, DEFAULT_ROW_PX, steps), 0))
        return (row1 + 1, col1 + 1), (x1, y1), size, False
    size = _attribute_pair(_kid(anchor, "ext"), "cx", "cy")
    if kind == "oneCellAnchor":
        col1, x1, row1, y1 = _corner(_kid(anchor, "from"))
        return (row1 + 1, col1 + 1), (x1, y1), size, False
    x, y = _attribute_pair(_kid(anchor, "pos"), "x", "y")
    col, offset_x = locate(x, column, DEFAULT_COLUMN_PX, MAX_COLS, steps)
    number, offset_y = locate(y, row, DEFAULT_ROW_PX, MAX_ROWS, steps)
    return (number, col), (offset_x, offset_y), size, True


# Chart parts.

def _title(element):
    """``{"text", "from_cell"}`` of a ``c:title`` (``None`` for an absent one)."""
    text, from_cell = None, False
    tx = _kid(element, "tx")
    rich, reference = _kid(tx, "rich"), _kid(tx, "strRef")
    if rich is not None:
        paragraphs = ["".join(item.text or "" for item in paragraph.iter() if local(item.tag) == "t")
                      for paragraph in rich.iter() if local(paragraph.tag) == "p"]
        text = "\n".join(paragraphs) or None
    elif reference is not None:
        from_cell = True
        cached = next((item for item in reference.iter() if local(item.tag) == "v"), None)
        text = (cached.text or None) if cached is not None else None
    return {"text": text, "from_cell": from_cell} if element is not None else dict(EMPTY_TITLE)


def _source(container):
    """``None`` for an absent source, ``UNUSABLE`` for literal or unparsable data, else the reference."""
    if container is None:
        return None
    for kind in ("numRef", "strRef", "multiLvlStrRef"):
        reference = _kid(container, kind)
        if reference is not None:
            formula = getattr(_kid(reference, "f"), "text", None)
            return parse_reference(formula, "xlsx") if formula else UNUSABLE
    return UNUSABLE if any(local(item.tag) in ("numLit", "strLit") for item in container) else None


def _series_name(element):
    tx = _kid(element, "tx")
    if tx is None:
        return None
    if _kid(tx, "strRef") is not None:
        return {"ref": _source(tx)}
    literal = getattr(_kid(tx, "v"), "text", None)
    return {"text": literal} if literal else None


def _series(plot, kind, room):
    """The first ``room`` series of a plot, in series order (``room`` bounds the work done on an oversized chart)."""
    items = []
    for number, element in enumerate(_kids(plot, "ser")):
        try:
            order = int(_val(element, "order", number))
        except ValueError:
            order = number
        items.append((order, number, element))
    scatter = kind in ("scatter", "bubble")
    return [{"name": _series_name(element), "values": _source(_kid(element, "yVal" if scatter else "val")),
             "categories": None if scatter else _source(_kid(element, "cat")),
             "x": _source(_kid(element, "xVal")) if scatter else None}
            for _, _, element in sorted(items, key=lambda item: item[:2])[:room]]


def _plots(area):
    """``(element, plot dict, axis ids)`` for each chart element of a plot area."""
    plots = []
    for element in area:
        name = local(element.tag)
        if not name.endswith("Chart") or name == "Chart":
            continue
        kind = name[:-len("Chart")]
        three_d = kind.endswith("3D")
        kind = kind[:-2] if three_d else kind
        plots.append((element, {"kind": kind, "direction": (_val(element, "barDir") or "col") if kind == "bar" else None,
                                "grouping": _val(element, "grouping") or DEFAULT_GROUPING.get(kind),
                                "three_d": three_d, "secondary": False},
                      {item.get("val") for item in _kids(element, "axId")}))
    return plots


def _mark_secondary(plots, area):
    """A plot is secondary when its axes differ from the first plot's, or it uses a value axis crossing at the far side."""
    values = _kids(area, "valAx")
    far = {_val(axis, "axId") for axis in values if _val(axis, "crosses") == "max"} if len(values) > 1 else set()
    for number, (_, plot, axes) in enumerate(plots):
        plot["secondary"] = (number > 0 and axes != plots[0][2]) or bool(far & axes)


def _axes(area):
    """The category (or horizontal) axis and the value axis of a plot area, either possibly ``None``."""
    categories = _kids(area, "catAx") + _kids(area, "dateAx")
    values = _kids(area, "valAx")
    if categories:
        return categories[0], (values[0] if values else None)
    horizontal = [item for item in values if _val(item, "axPos") in ("b", "t")]
    first = horizontal[0] if horizontal else (values[0] if values else None)
    rest = [item for item in values if item is not first]
    return first, (rest[0] if rest else None)


def _limit(axis, name):
    try:
        number = float(_val(_kid(axis, "scaling"), name))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _filled(element):
    return element is not None and any(local(child.tag) in FILLS for child in element)


def _styled(root):
    """The dropped styling a chart carries, in the order of ``STYLED``."""
    found = set()
    if _filled(_kid(root, "spPr")):
        found.add("colours")
    for element in root.iter():
        name = local(element.tag)
        if name in ("ser", "dPt") and _filled(_kid(element, "spPr")):
            found.add("colours")
        elif name == "dLbls" and any(local(child.tag) in LABEL_FLAGS and _true(child.get("val")) for child in element):
            found.add("data labels")
        elif name in ("gapWidth", "overlap"):
            found.add("gap width")
        elif name == "marker" and _val(element, "symbol", "none") != "none":
            found.add("markers")
        elif name == "smooth" and _true(element.get("val")):
            found.add("smoothing")
        elif name in ("majorGridlines", "minorGridlines"):
            found.add("gridlines")
        elif name == "numFmt" and not _true(element.get("sourceLinked"), False):
            found.add("number formats")
        elif name == "txPr":
            found.add("fonts")
    return [item for item in STYLED if item in found]


def _all_series(plots):
    """The series of every plot, stopping at ``MAX_DRAFT_SERIES``: one past the limit is enough for ``convert`` to skip."""
    found = []
    for element, plot, _ in plots:
        found += _series(element, plot["kind"], MAX_DRAFT_SERIES - len(found))
    return found


def _read_chart(root):
    """The chart-specific part of a draft from a ``c:chartSpace`` root; raises ``_Unreadable``."""
    chart = _kid(root, "chart") if local(root.tag) == "chartSpace" else None
    if chart is None:
        raise _Unreadable
    area = _kid(chart, "plotArea")
    plots = _plots(area) if area is not None else []
    if area is not None:
        _mark_secondary(plots, area)
    category, value = _axes(area) if area is not None else (None, None)
    legend = _kid(chart, "legend")
    return {"plots": [plot for _, plot, _ in plots],
            "title": _title(_kid(chart, "title")),
            "axis_titles": {"category": _title(_kid(category, "title")), "value": _title(_kid(value, "title"))},
            "value_min": _limit(value, "min"), "value_max": _limit(value, "max"),
            "legend": None if legend is None else LEGEND_POSITIONS.get(_val(legend, "legendPos", "r")),
            "series": _all_series(plots),
            "styled": _styled(root)}


def _extended():
    """The draft body of a ``cx:chart`` graphic: its part is not read, ``convert`` skips the kind."""
    return {"plots": [{"kind": "extended", "direction": None, "grouping": None, "three_d": False, "secondary": False}],
            "title": dict(EMPTY_TITLE), "axis_titles": {"category": dict(EMPTY_TITLE), "value": dict(EMPTY_TITLE)},
            "value_min": None, "value_max": None, "legend": None, "series": [], "styled": []}


# Drawings.

def _chart_element(frame):
    data = next((item for item in frame.iter() if local(item.tag) == "graphicData"), None)
    return None if data is None else next((child for child in data if local(child.tag) == "chart"), None)


def _choose(content):
    """The graphicFrame of an ``mc:AlternateContent`` to read: a regular chart first, else an extended one."""
    frames = [frame for branch in content for frame in _kids(branch, "graphicFrame") if _chart_element(frame) is not None]
    regular = [frame for frame in frames if CHARTEX not in _namespace(_chart_element(frame).tag)]
    return (regular or frames or [None])[0]


def _frames(anchor):
    """``(graphicFrame, grouped)`` for each chart-bearing frame of an anchor, in document order."""
    found = []

    def visit(element, grouped):
        for child in element:
            name = local(child.tag)
            if name == "graphicFrame" and _chart_element(child) is not None:
                found.append((child, grouped))
            elif name == "grpSp":
                visit(child, True)
            elif name == "AlternateContent":
                chosen = _choose(_kids(child, "Choice") + _kids(child, "Fallback"))
                if chosen is not None:
                    found.append((chosen, grouped))
    visit(anchor, False)
    return found


def _anchors(root):
    for child in root:
        if local(child.tag) in ANCHORS:
            yield child
        elif local(child.tag) == "AlternateContent":
            for branch in _kids(child, "Choice") + _kids(child, "Fallback"):
                inner = [item for item in branch if local(item.tag) in ANCHORS]
                if inner:
                    yield from inner
                    break


def _frame_name(frame, number):
    name = next((item.get("name") for item in frame.iter() if local(item.tag) == "cNvPr"), None)
    return name or f"Chart {number}"


def _draft(archive, drawing, links, placed, frame, order, position, label, budget, names):
    element = _chart_element(frame)
    (cell, offset, size, absolute), approximate = placed
    if CHARTEX in _namespace(element.tag):
        body = _extended()
    else:
        link = links.get(_relationship_id(element))
        part = _resolve(drawing, link[1], names) if link is not None and link[0] == "chart" else None
        root = _read(archive, part, budget) if part is not None else None
        if root is None:
            raise _Unreadable
        body = _read_chart(root)
    return {"sheet": position, "name": label, "order": order, **body,
            "anchor": {"row": cell[0], "col": cell[1]}, "offset": {"x": offset[0], "y": offset[1]},
            "size": {"width": size[0], "height": size[1]}, "approximate_size": approximate, "absolute_anchor": absolute}


def read_sheet_drafts(archive, sheet_part, position, sheet_name, budget, report: ConversionReport, widths=None, *,
                      sizes_unknown=False) -> list:
    """Drafts of the charts in a worksheet's drawing, in drawing order.

    Unreadable and grouped charts are reported (``chart.skipped``) and left out, as are the frames past
    ``MAX_FRAMES``.  ``widths`` is an optional ``(column_width_px(col), row_height_px(row))`` pair of callables
    over 1-based indexes; without it the default cell sizes, 64 and 20 pixels, are used.  The callables are asked
    at most ``MAX_STEPS`` times for the whole worksheet; spans measured after that use the default sizes and
    their drafts are marked ``approximate_size``.  ``sizes_unknown`` says the worksheet declares cell sizes that
    ``widths`` does not supply: every draft placed by measuring cells (all but one-cell anchors) is then marked too."""
    def skip(detail, label, count=1):
        report.add(SKIPPED, "omitted", detail, sheet=sheet_name, cell=label[:32], count=count)

    names = set(archive.namelist())
    related = _relationships(archive, sheet_part, budget, names)
    if related is None:
        skip("unreadable", "drawing")
        return []
    drafts, steps = [], Steps()
    for kind, target in related.values():
        if kind != "drawing":
            continue
        drawing = _resolve(sheet_part, target, names)
        root = _read(archive, drawing, budget) if drawing is not None else None
        links = _relationships(archive, drawing, budget, names) if root is not None else None
        if links is None:
            skip("unreadable", "drawing")
            continue
        order = unread = 0
        for anchor in _anchors(root):
            placed = None  # computed once for the anchor, on its first frame that is read
            for frame, grouped in _frames(anchor):
                label = _frame_name(frame, order + 1)
                if order >= MAX_FRAMES:
                    unread += 1
                elif grouped:
                    skip("grouped", label)
                else:
                    try:
                        if placed is None:
                            steps.exhausted = False
                            measured = sizes_unknown and local(anchor.tag) != "oneCellAnchor"
                            placed = (_placement(anchor, widths, steps), steps.exhausted or measured)
                        drafts.append(_draft(archive, drawing, links, placed, frame, order, position, label, budget, names))
                    except Exception:  # an untrusted chart never fails the import, whatever is wrong with it
                        skip("unreadable", label)
                order += 1
        if unread:
            skip("unreadable", "drawing", unread)
    return drafts


def sizes_declared(features) -> bool:
    """Whether a worksheet's ``SheetFeatures`` met column widths or row heights, which the readers do not keep per cell."""
    return bool(features.width_shares or features.default_width is not None
                or {"row_heights", "sheet_format_defaults"} & set(features.counts))


def worksheet_drafts(archive, sheet_part, position, sheet_name, budget, report: ConversionReport, features) -> list:
    """``read_sheet_drafts`` for a worksheet the whole-workbook readers have read, given its ``SheetFeatures``.

    Only a worksheet with a ``<drawing>`` element shows charts, so no other is read.  Its column widths and row heights
    are not kept per cell, so a sheet that declares any has its measured drafts marked ``approximate_size``."""
    if "drawings" not in features.counts:
        return []
    return read_sheet_drafts(archive, sheet_part, position, sheet_name, budget, report, sizes_unknown=sizes_declared(features))
