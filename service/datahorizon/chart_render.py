# SPDX-License-Identifier: GPL-3.0-only
"""Standard-library SVG chart renderer.

Service code. Turns a resolved chart (plain dicts and lists, no workbook
access) into SVG plus mark metadata. Built in charts stage 1 as an experiment
(docs/design/charts-stage-1-plan.md) and moved into the service in stage 2.
"""

import math

from . import chart_font
from .engine import EngineError

LIMIT_POINTS = 10_000
LIMIT_PIE_POINTS = 1_000
LIMIT_SERIES = 64

_NARROW = frozenset("iIjl.,:;|!' ")
_WIDE = frozenset("mwMW@%")
_ELLIPSIS = "…"


class ChartRenderError(EngineError):
    """A structured refusal; ``code`` is the stable identifier."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(code, message or code, 400)
        self.code = code


def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Ticks on a 1/2/5 x 10^k step that cover [lo, hi].

    Raises ``VALUE_RANGE`` when the step or a tick is not a finite float.
    """
    try:
        ticks = _nice_ticks(lo, hi, target)
    except (OverflowError, ValueError, ZeroDivisionError):
        ticks = []
    if not ticks or not all(math.isfinite(tick) for tick in ticks):
        raise ChartRenderError("VALUE_RANGE", "values are too large or too small to plot")
    return ticks


def _nice_ticks(lo: float, hi: float, target: int) -> list[float]:
    if lo > hi:
        lo, hi = hi, lo
    if lo == hi:
        if lo == 0:
            return [0, 1]
        lo, hi = min(lo, 0), max(hi, 0)
    raw = (hi - lo) / target
    exponent = math.floor(math.log10(raw))
    step = 10.0 ** (exponent + 1)
    for multiple in (1, 2, 5):
        candidate = multiple * 10.0**exponent
        if candidate >= raw * (1 - 1e-12):
            step = candidate
            break
    first = math.floor(lo / step + 1e-9)
    last = math.ceil(hi / step - 1e-9)
    places = max(0, -math.floor(math.log10(step) + 1e-9))
    ticks = []
    for index in range(first, last + 1):
        value = round(index * step, places)
        if not math.isfinite(value):
            return []
        ticks.append(int(value) if value == int(value) else value)
    return ticks


def format_tick(value: float) -> str:
    """Invariant tick label: '.' decimal, no grouping, no float noise."""
    rounded = round(float(value), 10)
    if rounded == int(rounded):
        return str(int(rounded))
    return repr(rounded)


def _advance(char: str, table: dict[str, float] | None) -> float:
    if table is not None and char in table:
        return table[char]
    if char in _NARROW:
        return 0.30
    if char in _WIDE:
        return 0.90
    if not char.isascii():
        return 1.00
    if char.isdigit():
        return 0.60
    if char.isupper():
        return 0.72
    return 0.56


def estimate_text_width(text: str, size_px: float, table: dict[str, float] | None = None) -> float:
    """Estimated advance width in pixels; ``table`` maps a character to ems."""
    return sum(_advance(char, table) for char in text) * size_px


def truncate_to_width(text: str, size_px: float, max_px: float, table: dict[str, float] | None = None) -> str:
    """``text`` unchanged if it fits, else its longest prefix plus an ellipsis."""
    if estimate_text_width(text, size_px, table) <= max_px:
        return text
    budget = max_px - estimate_text_width(_ELLIPSIS, size_px, table)
    used = 0.0
    kept = 0
    for char in text:
        used += _advance(char, table) * size_px
        if used > budget:
            break
        kept += 1
    return text[:kept] + _ELLIPSIS


# ---------------------------------------------------------------- rendering

MARGIN = 8.0
LABEL_PX = 12.0
TITLE_PX = 14.0
ROW = LABEL_PX * 1.5
MAX_CANVAS = 16_384
MIN_WIDTH, MIN_HEIGHT = 64, 48
# Below these sizes a decoration is dropped rather than drawn over the plot.
TITLE_MIN_HEIGHT = 120
LEGEND_RIGHT_MIN_WIDTH = 240
LEGEND_BOTTOM_MIN_HEIGHT = 160
AXIS_TITLE_MIN = (240, 160)
CARTESIAN = ("column", "bar", "line", "area", "scatter")
# Placeholder palette for the experiment; not a product decision.
PALETTE = ("#3b6fb6", "#d9822b", "#4f9d69", "#b8475f", "#7b61a8", "#8a8f98", "#c2a633", "#2f9aa0")
_TEXT = "#333333"
_GRID = "#dddddd"
_AXIS = "#888888"


def _num(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _xml(text: str) -> str:
    cleaned = "".join(c if c >= " " and c not in "￾￿" and not "\ud800" <= c <= "\udfff" else " " for c in text)
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clean(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


class _Svg:
    def __init__(self, width: int, height: int, metrics):
        self.width, self.height, self.metrics = width, height, metrics
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" version="1.2" baseProfile="tiny" '
            f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        ]
        self.marks: list[dict] = []

    def rect(self, x, y, w, h, fill, opacity=None):
        extra = "" if opacity is None else f' fill-opacity="{opacity}"'
        self.parts.append(f'<rect x="{_num(x)}" y="{_num(y)}" width="{_num(w)}" height="{_num(h)}" fill="{fill}"{extra}/>')

    def line(self, x1, y1, x2, y2, stroke, width=1):
        self.parts.append(f'<line x1="{_num(x1)}" y1="{_num(y1)}" x2="{_num(x2)}" y2="{_num(y2)}" stroke="{stroke}" stroke-width="{width}"/>')

    def path(self, d, stroke="none", fill="none", width=2, opacity=None):
        extra = "" if opacity is None else f' fill-opacity="{opacity}"'
        self.parts.append(f'<path d="{d}" stroke="{stroke}" stroke-width="{width}" fill="{fill}"{extra} stroke-linejoin="round"/>')

    def circle(self, cx, cy, r, fill):
        self.parts.append(f'<circle cx="{_num(cx)}" cy="{_num(cy)}" r="{_num(r)}" fill="{fill}"/>')

    def text(self, x, y, text, size=LABEL_PX, anchor="start", rotate=None):
        if not text:
            return
        turn = "" if rotate is None else f' transform="rotate({rotate} {_num(x)} {_num(y)})"'
        self.parts.append(
            f'<text x="{_num(x)}" y="{_num(y)}" font-family="{chart_font.FAMILY}, sans-serif" font-size="{_num(size)}" '
            f'text-anchor="{anchor}" fill="{_TEXT}"{turn}>{_xml(text)}</text>')

    def row_label(self, row, x, y, text, anchor="middle"):
        """A label on a horizontal row; skipped if it would overlap ``row`` or leave the canvas."""
        width = self.width_of(text)
        lo = x - width / 2 if anchor == "middle" else x
        hi = lo + width
        if lo < 0 or hi > self.width or any(lo < other_hi + 4 and hi > other_lo - 4 for other_lo, other_hi in row):
            return
        row.append((lo, hi))
        self.text(x, y, text, anchor=anchor)

    def width_of(self, text, size=LABEL_PX):
        return estimate_text_width(text, size, self.metrics)

    def fit(self, text, max_px, size=LABEL_PX):
        return truncate_to_width(text, size, max(max_px, 0.0), self.metrics)

    def mark(self, series, point, x, y, w, h, **extra):
        x = min(max(x, 0.0), self.width)
        y = min(max(y, 0.0), self.height)
        w = min(max(w, 0.0), self.width - x)
        h = min(max(h, 0.0), self.height - y)
        self.marks.append({"series": series, "point": point, "x": x, "y": y, "w": w, "h": h, **extra})

    def point_mark(self, series, point, cx, cy):
        self.mark(series, point, cx - 4, cy - 4, 8, 8)

    def result(self) -> dict:
        return {"svg": "".join(self.parts) + "</svg>", "marks": self.marks}


def _validate(resolved, width, height) -> dict:
    for size, least in ((width, MIN_WIDTH), (height, MIN_HEIGHT)):
        if isinstance(size, bool) or not isinstance(size, int) or not least <= size <= MAX_CANVAS:
            raise ChartRenderError(
                "INVALID_SIZE", f"canvas must be {MIN_WIDTH}x{MIN_HEIGHT} to {MAX_CANVAS}x{MAX_CANVAS} pixels")
    if not isinstance(resolved, dict):
        raise ChartRenderError("INVALID_SPEC", "resolved chart must be an object")
    kind = resolved.get("type")
    if kind not in CARTESIAN + ("pie",):
        raise ChartRenderError("UNSUPPORTED_TYPE", f"unsupported chart type {kind!r}")
    raw_series = resolved.get("series")
    if not isinstance(raw_series, list):
        raise ChartRenderError("INVALID_SPEC", "series must be a list")
    if len(raw_series) > LIMIT_SERIES:
        raise ChartRenderError("LIMIT_SERIES", f"more than {LIMIT_SERIES} series")
    series = []
    for item in raw_series:
        if not isinstance(item, dict) or not isinstance(item.get("values"), list):
            raise ChartRenderError("INVALID_SPEC", "each series needs a values list")
        xs = item.get("x")
        if xs is not None and not isinstance(xs, list):
            raise ChartRenderError("INVALID_SPEC", "series x must be a list or null")
        if len(item["values"]) > LIMIT_POINTS or (xs is not None and len(xs) > LIMIT_POINTS):
            raise ChartRenderError("LIMIT_POINTS", f"more than {LIMIT_POINTS} points in a series")
        if kind == "pie" and len(item["values"]) > LIMIT_PIE_POINTS:
            raise ChartRenderError("LIMIT_PIE_POINTS", f"more than {LIMIT_PIE_POINTS} values in a pie")
        name = item.get("name")
        series.append({"name": "" if name is None else str(name),
                       "values": [_clean(v) for v in item["values"]],
                       "x": None if xs is None else [_clean(v) for v in xs]})
    categories = resolved.get("categories")
    if categories is not None and not isinstance(categories, list):
        raise ChartRenderError("INVALID_SPEC", "categories must be a list or null")
    if categories is not None and len(categories) > LIMIT_POINTS:
        raise ChartRenderError("LIMIT_POINTS", f"more than {LIMIT_POINTS} categories")
    grouping = resolved.get("grouping")
    if grouping not in (None, "clustered", "stacked"):
        raise ChartRenderError("INVALID_SPEC", f"unknown grouping {grouping!r}")
    axes = resolved.get("axes") if isinstance(resolved.get("axes"), dict) else {}
    value_axis = axes.get("value") if isinstance(axes.get("value"), dict) else {}
    category_axis = axes.get("category") if isinstance(axes.get("category"), dict) else {}
    legend = resolved.get("legend") if isinstance(resolved.get("legend"), dict) else {}
    position = legend.get("position", "none")
    if position not in ("right", "bottom", "none"):
        raise ChartRenderError("INVALID_SPEC", f"unknown legend position {position!r}")
    title = resolved.get("title")
    return {"type": kind, "grouping": grouping, "series": series,
            "categories": None if categories is None else ["" if c is None else str(c) for c in categories],
            "title": None if title in (None, "") else str(title),
            "value_title": None if value_axis.get("title") in (None, "") else str(value_axis["title"]),
            "category_title": None if category_axis.get("title") in (None, "") else str(category_axis["title"]),
            "value_min": _clean(value_axis.get("min")), "value_max": _clean(value_axis.get("max")),
            "legend": position}


def _stack(chart, count):
    """Per series, per point: (start, end) along the value axis, or None for a gap."""
    stacked = chart["grouping"] == "stacked" and chart["type"] in ("column", "bar", "area")
    positive = [0.0] * count
    negative = [0.0] * count
    spans = []
    for item in chart["series"]:
        row = []
        for index in range(count):
            value = item["values"][index] if index < len(item["values"]) else None
            if value is None:
                row.append(None)
            elif not stacked:
                row.append((0.0, value))
            elif value >= 0:
                row.append((positive[index], positive[index] + value))
                positive[index] += value
            else:
                row.append((negative[index], negative[index] + value))
                negative[index] += value
        spans.append(row)
    return spans


def _scale(lo, hi, start_px, end_px):
    span = (hi - lo) or 1.0
    return lambda value: start_px + (value - lo) / span * (end_px - start_px)


def _check_range(lo, hi):
    if not (math.isfinite(lo) and math.isfinite(hi) and math.isfinite(hi - lo)):
        raise ChartRenderError("VALUE_RANGE", "values span a range too large to plot")


def _axis(lo, hi, forced_min, forced_max):
    """The plotted range and the ticks inside it.

    An explicit limit is the end of the range exactly; an automatic end is the
    outer nice tick. Ticks never widen an explicit limit.
    """
    if forced_min is not None and forced_max is not None and forced_min > forced_max:
        forced_min, forced_max = forced_max, forced_min
    if forced_min is not None:
        lo = forced_min
    if forced_max is not None:
        hi = forced_max
    if lo >= hi and (forced_min is not None or forced_max is not None):
        # the explicit limit excludes all the data, or both limits are equal
        if forced_max is None or forced_min == forced_max:
            hi = lo + (abs(lo) or 1.0)
        else:
            lo = hi - (abs(hi) or 1.0)
        _check_range(lo, hi)
    ticks = nice_ticks(lo, hi)
    low = ticks[0] if forced_min is None else forced_min
    high = ticks[-1] if forced_max is None else forced_max
    _check_range(low, high)
    return low, high, [tick for tick in ticks if low <= tick <= high] or [low]


def _title(svg, chart):
    if chart["title"] is None or svg.height < TITLE_MIN_HEIGHT:
        return MARGIN
    text = svg.fit(chart["title"], svg.width - 2 * MARGIN, TITLE_PX)
    svg.text(svg.width / 2, MARGIN + TITLE_PX, text, TITLE_PX, "middle")
    return MARGIN + TITLE_PX * 1.5 + 4


def _category_labels(svg, labels, start, slot, across, vertical, limit):
    """Thinned, truncated category labels along the category axis."""
    count = len(labels)
    if count == 0 or slot <= 0:
        return
    if vertical:  # labels run left to right under the plot
        widest = min(max(svg.width_of(text) for text in labels), 80.0)
        every = max(1, math.ceil((widest + 6) / slot))
        for index in range(0, count, every):
            room = min(every, count - index) * slot - 4
            if every == 1:
                svg.text(start + (index + 0.5) * slot, across, svg.fit(labels[index], room), anchor="middle")
            elif room >= 16:
                svg.text(start + index * slot + 2, across, svg.fit(labels[index], room))
    else:  # labels run top to bottom at the left of the plot
        every = max(1, math.ceil((LABEL_PX + 2) / slot))
        for index in range(0, count, every):
            svg.text(across, start + (index + 0.5) * slot + LABEL_PX / 3, svg.fit(labels[index], limit), anchor="end")


def _render_cartesian(svg, chart, top, right, bottom_edge):
    kind = chart["type"]
    series = chart["series"]
    scatter = kind == "scatter"
    horizontal = kind == "bar"
    count = max((len(item["values"]) for item in series), default=0)
    if chart["categories"] is not None and not scatter:
        count = max(count, len(chart["categories"]))
    labels = [] if scatter else [(chart["categories"] or [])[i] if i < len(chart["categories"] or []) else "" for i in range(count)]
    spans = _stack(chart, count)

    if scatter:  # a point draws only when it has both coordinates
        for item, row in zip(series, spans):
            xs = item["x"] or []
            for index, span in enumerate(row):
                if span is not None and (index >= len(xs) or xs[index] is None):
                    row[index] = None

    ends = [edge for row in spans for span in row if span is not None for edge in span]
    if kind in ("line", "scatter"):
        ends = [span[1] for row in spans for span in row if span is not None]
    lo, hi = (min(ends), max(ends)) if ends else (0.0, 0.0)
    _check_range(lo, hi)
    if kind in ("column", "bar", "area"):
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    low, high, ticks = _axis(lo, hi, chart["value_min"], chart["value_max"])
    tick_labels = [format_tick(tick) for tick in ticks]

    x_ticks = x_labels = None
    if scatter:
        xs = [item["x"][index] for item, row in zip(series, spans)
              for index, span in enumerate(row) if span is not None]
        if xs:
            _check_range(min(xs), max(xs))
        x_ticks = nice_ticks(min(xs), max(xs)) if xs else [0, 1]
        x_labels = [format_tick(tick) for tick in x_ticks]

    if svg.width < AXIS_TITLE_MIN[0] or svg.height < AXIS_TITLE_MIN[1]:
        chart = {**chart, "value_title": None, "category_title": None}
    left = MARGIN + (ROW if (chart["category_title"] if horizontal else chart["value_title"]) else 0)
    bottom = bottom_edge - MARGIN - ROW - (ROW if (chart["value_title"] if horizontal else chart["category_title"]) else 0)
    label_room = 0.0
    if horizontal:
        label_room = min(max((svg.width_of(text) for text in labels), default=0.0), svg.width * 0.3)
        left += label_room + 6
        right -= svg.width_of(tick_labels[-1]) / 2
    else:
        label_room = min(max(svg.width_of(text) for text in tick_labels), svg.width * 0.4)
        left += label_room + 6
        if scatter:
            right -= svg.width_of(x_labels[-1]) / 2
    right = max(right, left + 1)
    bottom = max(bottom, top + 1)
    plot_w, plot_h = right - left, bottom - top

    # value axis: gridlines and tick labels
    if horizontal:
        to_px = _scale(low, high, left, right)
        row = []
        for tick, text in zip(ticks, tick_labels):
            svg.line(to_px(tick), top, to_px(tick), bottom, _GRID)
            svg.row_label(row, to_px(tick), bottom + LABEL_PX + 3, text)
    else:
        to_px = _scale(low, high, bottom, top)
        last = None
        for tick, text in zip(ticks, tick_labels):
            y = to_px(tick)
            svg.line(left, y, right, y, _GRID)
            if last is None or last - y >= LABEL_PX + 2:  # thin labels that would stack
                svg.text(left - 6, y + LABEL_PX / 3, svg.fit(text, label_room), anchor="end")
                last = y
    if scatter:
        x_px = _scale(x_ticks[0], x_ticks[-1], left, right)
        row = []
        for tick, text in zip(x_ticks, x_labels):
            svg.line(x_px(tick), top, x_px(tick), bottom, _GRID)
            svg.row_label(row, x_px(tick), bottom + LABEL_PX + 3, text)

    # axis titles
    across_title, along_title = (chart["category_title"], chart["value_title"]) if horizontal else (chart["value_title"], chart["category_title"])
    if across_title:
        svg.text(MARGIN + LABEL_PX, top + plot_h / 2, svg.fit(across_title, plot_h), anchor="middle", rotate=-90)
    if along_title:
        svg.text(left + plot_w / 2, bottom_edge - MARGIN - 3, svg.fit(along_title, plot_w), anchor="middle")

    slot = (plot_h if horizontal else plot_w) / count if count else 0.0
    origin = top if horizontal else left
    if not scatter:
        if horizontal:
            _category_labels(svg, labels, top, slot, left - 6, False, label_room)
        else:
            _category_labels(svg, labels, left, slot, bottom + LABEL_PX + 3, True, 0.0)

    stacked = chart["grouping"] == "stacked"

    def clamp(value):
        return min(max(value, low), high)

    for number, (item, row) in enumerate(zip(series, spans)):
        colour = PALETTE[number % len(PALETTE)]
        if kind in ("column", "bar"):
            band = slot * 0.8
            thickness = band if stacked else band / max(len(series), 1)
            offset = slot * 0.1 + (0 if stacked else number * thickness)
            for index, span in enumerate(row):
                if span is None:
                    continue
                start, end = clamp(span[0]), clamp(span[1])
                if start == end and span[0] != span[1]:
                    continue  # wholly outside a forced axis range
                a, b = sorted((to_px(start), to_px(end)))
                along = origin + index * slot + offset
                box = (a, along, b - a, thickness) if horizontal else (along, a, thickness, b - a)
                svg.rect(*box, colour)
                svg.mark(number, index, *box)
        elif scatter:
            xs = item["x"] or []
            for index, span in enumerate(row):
                x = xs[index] if index < len(xs) else None
                if span is None or x is None or not low <= span[1] <= high:
                    continue
                svg.circle(x_px(x), to_px(span[1]), 3, colour)
                svg.point_mark(number, index, x_px(x), to_px(span[1]))
        else:  # line and area: one path segment per run of points without a gap
            # A stacked area also breaks where the value changes sign: positive
            # and negative values stack from different bases, and one polygon
            # across both would cross itself and cover the series below.
            runs, run, side = [], [], None
            for index, span in enumerate(row):
                here = None if span is None else span[1] < span[0]
                if run and (span is None or (kind == "area" and stacked and here != side)):
                    runs.append(run)
                    run = []
                side = here
                if span is None or (kind == "line" and not low <= span[1] <= high):
                    if run:
                        runs.append(run)
                    run = []
                else:
                    run.append((left + (index + 0.5) * slot, to_px(clamp(span[1])), to_px(clamp(span[0])), index))
            if run:
                runs.append(run)
            for run in runs:
                upper = " L".join(f"{_num(x)} {_num(y)}" for x, y, _, _ in run)
                if kind == "area":
                    lower = " L".join(f"{_num(x)} {_num(base)}" for x, _, base, _ in reversed(run))
                    svg.path(f"M{upper} L{lower} Z", fill=colour, opacity=1 if stacked else 0.5)
                if len(run) == 1:
                    svg.circle(run[0][0], run[0][1], 2, colour)
                else:
                    svg.path(f"M{upper}", stroke=colour)
                for x, y, _, index in run:
                    svg.point_mark(number, index, x, y)

    # axis lines on top of the data
    zero = min(max(0, low), high)
    if horizontal:
        svg.line(to_px(zero), top, to_px(zero), bottom, _AXIS)
    else:
        svg.line(left, to_px(zero), right, to_px(zero), _AXIS)
        if scatter:
            svg.line(left, top, left, bottom, _AXIS)


def _legend(svg, entries, position, top, right, bottom_edge):
    """Draw the legend and return the (right, bottom_edge) left for the plot."""
    if position == "none" or not entries:
        return right, bottom_edge
    if position == "right" and svg.width < LEGEND_RIGHT_MIN_WIDTH:
        return right, bottom_edge
    if position == "bottom" and svg.height < LEGEND_BOTTOM_MIN_HEIGHT:
        return right, bottom_edge
    swatch, gap = 10.0, 6.0
    if position == "right":
        widest = max(svg.width_of(name) for name in entries)
        box = min(swatch + gap + widest + MARGIN, svg.width * 0.3)
        x = right - box + MARGIN
        y = top
        for index, name in enumerate(entries):
            if y + ROW > bottom_edge - MARGIN:
                break
            svg.rect(x, y + (ROW - swatch) / 2, swatch, swatch, PALETTE[index % len(PALETTE)])
            svg.text(x + swatch + gap, y + ROW / 2 + LABEL_PX / 3, svg.fit(name, box - MARGIN - swatch - gap))
            y += ROW
        return right - box, bottom_edge
    # bottom: entries flow left to right and wrap, at most three rows
    placed, x, row = [], MARGIN, 0
    for index, name in enumerate(entries):
        label = svg.fit(name, 160.0)
        width = swatch + gap + svg.width_of(label) + 12
        if x + width > right and x > MARGIN:
            x, row = MARGIN, row + 1
        if row >= 3:
            break
        placed.append((index, label, x, row))
        x += width
    rows = max(item[3] for item in placed) + 1
    first = bottom_edge - MARGIN - rows * ROW
    for index, label, x, row in placed:
        y = first + row * ROW
        svg.rect(x, y + (ROW - swatch) / 2, swatch, swatch, PALETTE[index % len(PALETTE)])
        svg.text(x + swatch + gap, y + ROW / 2 + LABEL_PX / 3, svg.fit(label, right - x - swatch - gap))
    return right, first


def _pie_values(chart) -> list[float | None]:
    values = chart["series"][0]["values"] if chart["series"] else []
    if any(value is not None and value < 0 for value in values):
        raise ChartRenderError("NEGATIVE_IN_PIE", "a pie cannot show negative values")
    return values


def _render_pie(svg, values, top, right, bottom_edge):
    total = sum(value for value in values if value)
    _check_range(0.0, total)
    cx = (MARGIN + right) / 2
    cy = (top + bottom_edge - MARGIN) / 2
    radius = max(min(right - MARGIN, bottom_edge - MARGIN - top) / 2 - 4, 1.0)
    if total <= 0:
        return

    def at(degrees):
        angle = math.radians(degrees)
        return cx + radius * math.sin(angle), cy - radius * math.cos(angle)

    start = 0.0
    live = [index for index, value in enumerate(values) if value]
    for index in live:
        end = 360.0 if index == live[-1] else start + values[index] / total * 360.0
        colour = PALETTE[index % len(PALETTE)]
        if end - start >= 360.0 - 1e-9:
            svg.circle(cx, cy, radius, colour)
            corners = [(cx - radius, cy - radius), (cx + radius, cy + radius)]
        else:
            (x0, y0), (x1, y1) = at(start), at(end)
            large = 1 if end - start > 180 else 0
            svg.path(f"M{_num(cx)} {_num(cy)} L{_num(x0)} {_num(y0)} "
                     f"A{_num(radius)} {_num(radius)} 0 {large} 1 {_num(x1)} {_num(y1)} Z",
                     stroke="#ffffff", fill=colour, width=1)
            corners = [(cx, cy), (x0, y0), (x1, y1)]
            corners += [at(quarter) for quarter in (0, 90, 180, 270) if start < quarter < end]
        xs = [x for x, _ in corners]
        ys = [y for _, y in corners]
        svg.mark(0, index, min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys), a0=start, a1=end)
        start = end


def render(resolved: dict, width: int, height: int, metrics: dict[str, float] | None = None) -> dict:
    """Render a resolved chart to ``{"svg": str, "marks": list[dict]}``."""
    chart = _validate(resolved, width, height)
    pie = chart["type"] == "pie"
    values = _pie_values(chart) if pie else []
    svg = _Svg(width, height, chart_font.default_metrics() if metrics is None else metrics)
    top = _title(svg, chart)
    if pie:
        labels = chart["categories"] or []
        entries = [labels[index] if index < len(labels) else "" for index in range(len(values))]
    else:
        entries = [item["name"] for item in chart["series"]]
    right, bottom_edge = _legend(svg, entries, chart["legend"], top, width - MARGIN, float(height))
    if pie:
        _render_pie(svg, values, top, right, bottom_edge)
    else:
        _render_cartesian(svg, chart, top, right, bottom_edge)
    return svg.result()
