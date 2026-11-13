# SPDX-License-Identifier: GPL-3.0-only
"""Chart payloads: validation of specifications and stored charts.

Everything here is pure: nothing writes to the workbook.  A specification is
accepted in an input form (sources named by sheet and A1 text, optional keys
defaulted) and kept in a stored form (sources as sheet id and rectangle,
every key present).
"""
import copy
import math

from .chart_render import LIMIT_PIE_POINTS, LIMIT_POINTS, LIMIT_SERIES
from .engine import MAX_COLS, MAX_ROWS, EngineError
from .formula import FormulaError, column_name, split_ref
from .metadata import name as metadata_name

SPEC_VERSION = 1
TYPES = ("column", "bar", "line", "area", "pie", "scatter")
GROUPED_TYPES = ("column", "bar", "area")
LIMIT_CELLS = 100_000
LIMIT_CHARTS = 64

TITLE_LENGTH = 255
LEGEND_POSITIONS = ("right", "bottom", "none")
SPEC_KEYS = ("spec_version", "type", "grouping", "title", "categories", "series", "axes", "legend")
REQUIRED_KEYS = ("spec_version", "type", "series")
SERIES_KEYS = ("name", "values", "x")
RECTANGLE_KEYS = ("row1", "col1", "row2", "col2")
STORED_KEYS = ("name", "anchor", "offset", "size", "z", "spec_version", "spec")


def _invalid(message, code="INVALID_SPEC"):
    return EngineError(code, message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _object(value, what, keys, required=None):
    """Check that ``value`` is an object with only ``keys`` and all ``required`` ones."""
    if not isinstance(value, dict):
        raise _invalid(f"{what} must be an object")
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise _invalid(f"{what} has unknown keys: {', '.join(map(str, unknown))}")
    missing = [key for key in (keys if required is None else required) if key not in value]
    if missing:
        raise _invalid(f"{what} needs: {', '.join(missing)}")
    return value


def _check_extent(row1, col1, row2, col2, role, chart_type):
    """Check a rectangle's shape and size; return its cell count."""
    if not (1 <= row1 <= row2 <= MAX_ROWS and 1 <= col1 <= col2 <= MAX_COLS):
        raise _invalid(f"{role} is outside the grid")
    count = (row2 - row1 + 1) * (col2 - col1 + 1)
    if role == "name":
        if count != 1:
            raise _invalid("a series name source must be one cell")
        return count
    if row1 != row2 and col1 != col2:
        raise _invalid(f"a {role} source must be one row or one column")
    pie = chart_type == "pie" and role in ("values", "categories")
    limit = LIMIT_PIE_POINTS if pie else LIMIT_POINTS
    if count > limit:
        raise _invalid(f"a {role} source has more than {limit} cells", "LIMIT_PIE_POINTS" if pie else "LIMIT_POINTS")
    return count


def _input_source(book, value, role, chart_type):
    value = _object(value, f"{role} source", ("kind", "sheet", "ref"))
    if value["kind"] != "range":
        raise _invalid("a source kind must be 'range'")
    ref = value["ref"]
    if not isinstance(ref, str):
        raise _invalid("a source ref must be text")
    parts = ref.split(":")
    try:
        corners = [split_ref(part) for part in parts] if len(parts) <= 2 else None
    except FormulaError:
        corners = None
    if corners is None or any(corner[0] is not None for corner in corners):
        raise _invalid("a source ref must be one cell or one rectangle such as A2:A13, without a sheet")
    rows = sorted(corner[1] for corner in corners)
    cols = sorted(corner[2] for corner in corners)
    count = _check_extent(rows[0], cols[0], rows[-1], cols[-1], role, chart_type)
    sheet = value["sheet"]
    if not isinstance(sheet, str):
        raise _invalid("a source sheet must be a sheet name or id")
    sheet_id = book.sheet(sheet)
    stored = {"kind": "range", "sheet_id": sheet_id,
              "row1": rows[0], "col1": cols[0], "row2": rows[-1], "col2": cols[-1]}
    return stored, count


def _stored_source(book, value, role, chart_type):
    value = _object(value, f"{role} source", ("kind", "sheet_id") + RECTANGLE_KEYS,
                    ("kind", "sheet_id") + RECTANGLE_KEYS)
    if value["kind"] != "range":
        raise _invalid("a source kind must be 'range'")
    if value["sheet_id"] is not None and not isinstance(value["sheet_id"], str):
        raise _invalid("a source sheet_id must be text or null")
    coordinates = [value[key] for key in RECTANGLE_KEYS]
    if all(item is None for item in coordinates):
        return dict(value), 0
    if not all(_is_int(item) for item in coordinates):
        raise _invalid("a source rectangle is all integers or all null")
    return dict(value), _check_extent(*coordinates, role, chart_type)


def _text_or_null(value, what, limit=None):
    if value is not None and not isinstance(value, str):
        raise _invalid(f"{what} must be text or null")
    if limit is not None and value is not None and len(value) > limit:
        raise _invalid(f"{what} is longer than {limit} characters")
    return value


def _axes(value, stored):
    if value is None and not stored:
        value = {}
    value = _object(value, "axes", ("category", "value"), ("category", "value") if stored else ())
    category = value.get("category", {"title": None})
    _object(category, "category axis", ("title",), ("title",))
    amount = value.get("value", {"title": None, "min": None, "max": None})
    _object(amount, "value axis", ("title", "min", "max"), ("title", "min", "max"))
    _text_or_null(category["title"], "category axis title")
    _text_or_null(amount["title"], "value axis title")
    for key in ("min", "max"):
        if amount[key] is not None and not _is_number(amount[key]):
            raise _invalid(f"value axis {key} must be a finite number or null")
    if amount["min"] is not None and amount["max"] is not None and amount["min"] > amount["max"]:
        raise _invalid("value axis min must not exceed max")
    return {"category": {"title": category["title"]},
            "value": {"title": amount["title"], "min": amount["min"], "max": amount["max"]}}


def _check(book, spec, convert, stored):
    """Validate a specification, returning its stored form.

    ``convert(book, source, role, chart_type)`` turns one source into
    ``(stored_source, cell_count)``.  Errors are ordered: shape and unknown
    keys, version, type-specific rules, then sources and limits.
    """
    _object(spec, "specification", SPEC_KEYS, SPEC_KEYS if stored else REQUIRED_KEYS)
    version = spec["spec_version"]
    if not _is_int(version) or version < 1:
        raise _invalid("spec_version must be a positive integer")
    if version > SPEC_VERSION:
        raise _invalid(f"specification version {version} is not supported", "UNSUPPORTED_SPEC_VERSION")
    chart_type = spec["type"]
    if chart_type not in TYPES:
        raise _invalid(f"type must be one of {', '.join(TYPES)}")

    grouped = chart_type in GROUPED_TYPES
    grouping = spec.get("grouping", "clustered" if grouped else None)
    if grouping not in (("clustered", "stacked") if grouped else (None,)):
        raise _invalid("grouping is 'clustered' or 'stacked' for column, bar and area charts and null otherwise")

    title = spec.get("title", {"text": None})
    _object(title, "title", ("text",), ("text",))
    _text_or_null(title["text"], "title text", TITLE_LENGTH)

    categories = spec.get("categories")
    if categories is not None:
        _object(categories, "categories", ("source",), ("source",))
        if chart_type == "scatter":
            raise _invalid("a scatter chart has no categories")

    items = spec["series"]
    if not isinstance(items, list) or not items:
        raise _invalid("series must be a list of at least one series")
    if len(items) > LIMIT_SERIES:
        raise _invalid(f"more than {LIMIT_SERIES} series", "LIMIT_SERIES")
    if chart_type == "pie" and len(items) != 1:
        raise _invalid("a pie chart has exactly one series")
    for item in items:
        _object(item, "series", SERIES_KEYS, SERIES_KEYS if stored else ("name", "values"))
        name = item["name"]
        if not isinstance(name, dict) or len(name) != 1 or not set(name) <= {"text", "source"}:
            raise _invalid("a series name is {'text': ...} or {'source': ...}")
        if "text" in name and not isinstance(name["text"], str):
            raise _invalid("series name text must be text")
        _object(item["values"], "series values", ("source",), ("source",))
        x = item.get("x")
        if chart_type == "scatter":
            _object(x, "series x", ("source",), ("source",))
        elif x is not None:
            raise _invalid("series x is only for scatter charts")
    axes = _axes(spec.get("axes"), stored)
    legend = spec.get("legend", {"position": "right"})
    _object(legend, "legend", ("position",), ("position",))
    if legend["position"] not in LEGEND_POSITIONS:
        raise _invalid(f"legend position must be one of {', '.join(LEGEND_POSITIONS)}")

    total = 0

    def source(value, role):
        nonlocal total
        result, count = convert(book, value, role, chart_type)
        total += count
        return result

    out_categories = None if categories is None else {"source": source(categories["source"], "categories")}
    out_series = []
    for item in items:
        name = item["name"]
        out_name = name if "text" in name else {"source": source(name["source"], "name")}
        out_values = {"source": source(item["values"]["source"], "values")}
        x = item.get("x")
        out_x = None if x is None else {"source": source(x["source"], "x")}
        out_series.append({"name": copy.deepcopy(out_name), "values": out_values, "x": out_x})
    if total > LIMIT_CELLS:
        raise _invalid(f"sources together hold more than {LIMIT_CELLS} cells", "LIMIT_CELLS")
    return {"spec_version": version, "type": chart_type, "grouping": grouping,
            "title": {"text": title["text"]}, "categories": out_categories, "series": out_series,
            "axes": axes, "legend": {"position": legend["position"]}}


def normalise_spec(book, spec):
    """Validate an input-form specification and return its complete stored form."""
    return _check(book, spec, _input_source, False)


def _bad_envelope(message):
    return EngineError("INVALID_FORMAT", message)


def _stored_object(value, what, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise _bad_envelope(f"{what} must be an object with exactly: {', '.join(keys)}")
    return value


def _bounded(value, what, low, high):
    if not _is_int(value) or not low <= value <= high:
        raise _bad_envelope(f"{what} must be an integer from {low} to {high}")


def validate_stored(payload):
    """Check a stored chart without touching the database.

    Raises ``INVALID_FORMAT`` for a bad envelope.  Returns ``None`` for a
    readable chart, otherwise the issue code naming why its specification
    cannot be read: ``UNSUPPORTED_SPEC_VERSION`` or ``INVALID_SPEC``.
    """
    _stored_object(payload, "chart", STORED_KEYS)
    try:
        metadata_name(payload["name"])
    except EngineError as error:
        raise _bad_envelope("chart name needs 1-64 printable characters") from error
    anchor = _stored_object(payload["anchor"], "anchor", ("row", "col"))
    _bounded(anchor["row"], "anchor row", 1, MAX_ROWS)
    _bounded(anchor["col"], "anchor col", 1, MAX_COLS)
    offset = _stored_object(payload["offset"], "offset", ("x", "y"))
    _bounded(offset["x"], "offset x", 0, 4096)
    _bounded(offset["y"], "offset y", 0, 4096)
    size = _stored_object(payload["size"], "size", ("width", "height"))
    _bounded(size["width"], "size width", 64, 4096)
    _bounded(size["height"], "size height", 48, 4096)
    _bounded(payload["z"], "z", -1_000_000, 1_000_000)
    spec = payload["spec"]
    if not isinstance(spec, dict) or not _is_int(spec.get("spec_version")) \
            or not _is_int(payload["spec_version"]) or spec["spec_version"] != payload["spec_version"]:
        raise _bad_envelope("spec_version must be an integer equal to the specification's")
    if spec["spec_version"] > SPEC_VERSION:
        return "UNSUPPORTED_SPEC_VERSION"
    try:
        _check(None, spec, _stored_source, True)
    except EngineError:
        return "INVALID_SPEC"
    return None


def _describe_source(db, source):
    source["broken"] = source["sheet_id"] is None or source["row1"] is None or db.execute(
        "SELECT 1 FROM sheets WHERE id=?", (source["sheet_id"],)).fetchone() is None
    source["ref"] = None
    if not source["broken"]:
        first = column_name(source["col1"]) + str(source["row1"])
        last = column_name(source["col2"]) + str(source["row2"])
        source["ref"] = first if first == last else f"{first}:{last}"


def describe(db, sheet_id, chart_id, payload):
    """Return a deep copy of ``payload`` with its id, sheet, issue and source refs."""
    out = copy.deepcopy(payload)
    out["id"] = chart_id
    out["sheet_id"] = sheet_id
    out["issue"] = validate_stored(payload)
    if out["issue"] is None:
        spec = out["spec"]
        sources = [spec["categories"]["source"]] if spec["categories"] else []
        for item in spec["series"]:
            for part in (item["name"], item["values"], item["x"]):
                if part is not None and "source" in part:
                    sources.append(part["source"])
        for source in sources:
            _describe_source(db, source)
    return out
