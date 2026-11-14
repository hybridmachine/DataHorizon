# SPDX-License-Identifier: GPL-3.0-only
"""Chart payloads: validation, storage and the managed chart operations.

A specification is accepted in an input form (sources named by sheet and A1
text, optional keys defaulted) and kept in a stored form (sources as sheet id
and rectangle, every key present).  Charts are ``chart`` rows in the
``dh_objects`` catalog (native format 8); ``store`` is the only writer.
"""
import copy
import json
import math
import uuid

from . import query_views, relational
from .chart_render import LIMIT_PIE_POINTS, LIMIT_POINTS, LIMIT_SERIES
from .engine import MAX_COLS, MAX_ROWS, EngineError, encoded, require
from .formula import FormulaError, adjust_interval, column_name, split_ref
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


def _check_extent(row1, col1, row2, col2, role, chart_type, limits=True):
    """Check a rectangle's shape and, with ``limits``, its size; return its cell count."""
    if not (1 <= row1 <= row2 <= MAX_ROWS and 1 <= col1 <= col2 <= MAX_COLS):
        raise _invalid(f"{role} is outside the grid")
    count = (row2 - row1 + 1) * (col2 - col1 + 1)
    if role == "name":
        if count != 1:
            raise _invalid("a series name source must be one cell")
        return count
    if row1 != row2 and col1 != col2:
        raise _invalid(f"a {role} source must be one row or one column")
    if not limits:
        return count
    pie = chart_type == "pie" and role in ("values", "categories")
    limit = LIMIT_PIE_POINTS if pie else LIMIT_POINTS
    if count > limit:
        raise _invalid(f"a {role} source has more than {limit} cells", "LIMIT_PIE_POINTS" if pie else "LIMIT_POINTS")
    return count


def source_kind(source):
    """``"range"``, ``"table"`` or ``"query"`` for a stored source."""
    return source["kind"]


def is_broken(source):
    """Whether a stored source no longer names its data; reads the stored form only."""
    kind = source_kind(source)
    if kind == "table":
        return source["table_id"] is None or source["column_id"] is None
    if kind == "query":
        return source["query_id"] is None
    return source["sheet_id"] is None or any(source[key] is None for key in RECTANGLE_KEYS)


def _input_table_source(book, value, role):
    value = _object(value, f"{role} source", ("kind", "table", "column"), ("kind", "table", "column"))
    if not isinstance(value["table"], str) or not isinstance(value["column"], str):
        raise _invalid("a table source names its table and column as text")
    found = relational.table(book.db, value["table"])
    column = next((item for item in found["columns"] if item["id"] == value["column"]), None)
    if column is None:
        column = next((item for item in found["columns"] if item["name"].casefold() == value["column"].casefold()), None)
    if column is None:
        raise EngineError("COLUMN_NOT_FOUND", f"Table {found['name']} has no column {value['column']}")
    return {"kind": "table", "table_id": found["id"], "column_id": column["id"]}, 0


def _input_query_source(book, value, role):
    value = _object(value, f"{role} source", ("kind", "query", "column"), ("kind", "query", "column"))
    if not isinstance(value["query"], str) or not isinstance(value["column"], str):
        raise _invalid("a query source names its saved query and column as text")
    found = query_views.query(book.db, value["query"])
    if [item["name"] for item in found["shape"]].count(value["column"]) != 1:
        raise EngineError("COLUMN_NOT_FOUND", f"Saved query {found['name']} has no single result column {value['column']}")
    return {"kind": "query", "query_id": found["id"], "column": value["column"]}, 0


def _input_source(book, value, role, chart_type):
    kind = value.get("kind") if isinstance(value, dict) else None
    if kind in ("table", "query") and role == "name":
        raise _invalid("a series name source must be a range; use {'text': ...} for a fixed name")
    if kind == "table":
        return _input_table_source(book, value, role)
    if kind == "query":
        return _input_query_source(book, value, role)
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


def _stored_ids(value, role, keys, ids):
    value = _object(value, f"{role} source", keys, keys)
    if role == "name":
        raise _invalid("a series name source must be a range")
    if not all(value[key] is None or isinstance(value[key], str) for key in ids):
        raise _invalid("a source id must be text or null")
    return value


def _stored_source(book, value, role, chart_type):
    kind = value.get("kind") if isinstance(value, dict) else None
    if kind == "table":
        value = _stored_ids(value, role, ("kind", "table_id", "column_id"), ("table_id", "column_id"))
        if (value["table_id"] is None) != (value["column_id"] is None):
            raise _invalid("a table source has both ids or neither")
        return dict(value), 0
    if kind == "query":
        value = _stored_ids(value, role, ("kind", "query_id", "column"), ("query_id",))
        if not isinstance(value["column"], str) or not value["column"]:
            raise _invalid("a query source column must be non-empty text")
        return dict(value), 0
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
    return dict(value), _check_extent(*coordinates, role, chart_type, limits=False)


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
    if not stored and total > LIMIT_CELLS:
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
    cannot be read: ``UNSUPPORTED_SPEC_VERSION`` or ``INVALID_SPEC``.  Size
    limits are not readability: an edit can push a stored chart past one
    (see ``limit_error``).
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


def _sized_sources(spec):
    """``(role, source)`` for each source of a readable stored specification, in validation order."""
    if spec["categories"] is not None:
        yield "categories", spec["categories"]["source"]
    for item in spec["series"]:
        for role in ("name", "values", "x"):
            part = item[role]
            if part is not None and "source" in part:
                yield role, part["source"]


def limit_error(spec):
    """The size-limit refusal for a readable stored specification, or ``None`` when it is within its limits."""
    total = 0
    for role, source in _sized_sources(spec):
        if source_kind(source) != "range" or source["row1"] is None:
            continue
        count = (source["row2"] - source["row1"] + 1) * (source["col2"] - source["col1"] + 1)
        total += count
        if role == "name":
            continue
        pie = spec["type"] == "pie" and role in ("values", "categories")
        limit = LIMIT_PIE_POINTS if pie else LIMIT_POINTS
        if count > limit:
            return EngineError("LIMIT_PIE_POINTS" if pie else "LIMIT_POINTS",
                               f"The chart's {role} source now holds {count} cells; the limit is {limit}")
    if total > LIMIT_CELLS:
        return EngineError("LIMIT_CELLS", f"The chart's sources now hold {total} cells; the limit is {LIMIT_CELLS}")
    return None


def over_limit(payload):
    """The limit code a readable stored chart now exceeds, or ``None``."""
    if validate_stored(payload) is not None:
        return None
    error = limit_error(payload["spec"])
    return None if error is None else error.body["code"]


def _describe_table_source(db, source):
    source["table"] = source["column"] = None
    found = None if source["table_id"] is None else next(
        (item for item in relational.tables(db) if item["id"] == source["table_id"]), None)
    column = None if found is None or source["column_id"] is None else next(
        (item for item in found["columns"] if item["id"] == source["column_id"]), None)
    source["broken"] = column is None
    if column is not None:
        source["table"], source["column"] = found["name"], column["name"]


def _describe_query_source(db, source):
    found = None if source["query_id"] is None else next(
        (item for item in query_views.queries(db) if item["id"] == source["query_id"]), None)
    source["query"] = None if found is None else found["name"]
    source["broken"] = found is None or [item["name"] for item in found["shape"]].count(source["column"]) != 1


def _describe_source(db, source):
    if source_kind(source) == "table":
        return _describe_table_source(db, source)
    if source_kind(source) == "query":
        return _describe_query_source(db, source)
    source["broken"] = source["sheet_id"] is None or source["row1"] is None or db.execute(
        "SELECT 1 FROM sheets WHERE id=?", (source["sheet_id"],)).fetchone() is None
    source["ref"] = None
    if not source["broken"]:
        first = column_name(source["col1"]) + str(source["row1"])
        last = column_name(source["col2"]) + str(source["row2"])
        source["ref"] = first if first == last else f"{first}:{last}"


def _sources(spec):
    """Every source object of a readable stored specification, in place."""
    sources = [spec["categories"]["source"]] if spec["categories"] else []
    for item in spec["series"]:
        for part in (item["name"], item["values"], item["x"]):
            if part is not None and "source" in part:
                sources.append(part["source"])
    return sources


def describe(db, sheet_id, chart_id, payload):
    """Return a deep copy of ``payload`` with its id, sheet, issue and source refs."""
    out = copy.deepcopy(payload)
    out["id"] = chart_id
    out["sheet_id"] = sheet_id
    out["issue"] = validate_stored(payload)
    out["over_limit"] = over_limit(payload)
    if out["issue"] is None:
        for source in _sources(out["spec"]):
            _describe_source(db, source)
    return out


# Storage.

FORMAT_VERSION = 8
LIMIT_PAYLOAD = 256 * 1024
ENVELOPE_FIELDS = ("action", "operation_id", "expected_state", "label", "sheet")
FIELDS = {
    "chart_create": ("name", "anchor", "offset", "size", "spec"),
    "chart_update": ("chart_id", "name", "anchor", "offset", "size", "z", "order", "spec", "spec_patch"),
    "chart_delete": ("chart_id",),
}
PLACEMENT = ("name", "anchor", "offset", "size")
DEFAULTS = {"anchor": {"row": 1, "col": 1}, "offset": {"x": 0, "y": 0}, "size": {"width": 480, "height": 300}}


def _has_charts(book):
    return book.db.execute("SELECT format_version FROM dh_meta").fetchone()[0] >= FORMAT_VERSION


def objects(book):
    """Every stored chart as ``(sheet_id, chart_id, payload)``; empty below format 8."""
    if not _has_charts(book):
        return []
    rows = book.db.execute("SELECT sheet_id, id, payload FROM dh_objects WHERE namespace='chart' ORDER BY sheet_id, id")
    return [(sheet_id, chart_id, json.loads(payload)) for sheet_id, chart_id, payload in rows]


def stored(book, sheet_id, chart_id):
    """The stored payload of one chart, or ``None`` when it is absent."""
    if not _has_charts(book):
        return None
    row = book.db.execute("SELECT payload FROM dh_objects WHERE namespace='chart' AND sheet_id=? AND id=?",
                          (sheet_id, chart_id)).fetchone()
    return None if row is None else json.loads(row[0])


def find(book, chart_id):
    """Return ``(sheet_id, payload)`` for a chart id anywhere in the workbook."""
    if isinstance(chart_id, str) and _has_charts(book):
        row = book.db.execute("SELECT sheet_id, payload FROM dh_objects WHERE namespace='chart' AND id=?",
                              (chart_id,)).fetchone()
        if row is not None:
            return row[0], json.loads(row[1])
    raise EngineError("CHART_NOT_FOUND", "Chart does not exist", 404)


def store(book, sheet_id, chart_id, payload):
    """Write (or with ``None`` delete) one chart and record it in the operation's change set."""
    key = ("chart", sheet_id, chart_id)
    before = stored(book, sheet_id, chart_id)
    book.ensure_metadata()
    relational.ensure_version(book, FORMAT_VERSION)
    if payload is None:
        book.db.execute("DELETE FROM dh_objects WHERE namespace='chart' AND sheet_id=? AND id=?", (sheet_id, chart_id))
    else:
        text = encoded(payload)
        require(len(text.encode()) <= LIMIT_PAYLOAD, "LIMIT", "A chart payload exceeds 256 KiB")
        book.db.execute("INSERT INTO dh_objects VALUES('chart',?,?,?) ON CONFLICT(namespace,sheet_id,id) "
                        "DO UPDATE SET payload=excluded.payload", (chart_id, sheet_id, text))
        count = book.db.execute("SELECT count(*) FROM dh_objects WHERE namespace='chart'").fetchone()[0]
        require(count <= LIMIT_CHARTS, "LIMIT_CHARTS", f"A workbook holds at most {LIMIT_CHARTS} charts")
    require(book.db.execute("SELECT count(*)<=256 AND coalesce(sum(length(payload)),0)<=16777216 FROM dh_objects").fetchone()[0],
            "LIMIT", "Native metadata exceeds its budget")
    original = book.changes[key][0] if key in book.changes else before
    book.changes[key] = [original, copy.deepcopy(payload)]


# Following the data.

def _readable_sources(payload):
    """The live source objects of a readable chart, in place; none for an unreadable one."""
    if validate_stored(payload) is not None:
        return []
    return [source for source in _sources(payload["spec"])
            if source_kind(source) == "range" and source["sheet_id"] is not None and source["row1"] is not None]


def _break(source, sheet_id):
    source.update(sheet_id=sheet_id, row1=None, col1=None, row2=None, col2=None)


def adjust_structure(book, sheet_id, axis, start, count, delete):
    """Follow a whole-row/column edit; return ids of charts with a newly broken source and of charts newly over a size limit."""
    if not _has_charts(book):
        return [], []
    low, high = (("row1", "row2"), MAX_ROWS) if axis == "row" else (("col1", "col2"), MAX_COLS)
    key = "row" if axis == "row" else "col"
    broken, over = [], []
    for owner, chart_id, payload in objects(book):
        changed = copy.deepcopy(payload)
        if owner == sheet_id:
            position = changed["anchor"][key]
            if delete:
                position = start if start <= position < start + count else position - count if position >= start + count else position
            elif position >= start:
                position = min(position + count, high)
            changed["anchor"][key] = position
        newly_broken = False
        for source in _readable_sources(changed):
            if source["sheet_id"] != sheet_id:
                continue
            moved = adjust_interval(source[low[0]], source[low[1]], start, count, delete, high)
            if moved is None:
                _break(source, sheet_id)
                newly_broken = True
            else:
                source[low[0]], source[low[1]] = moved
        if newly_broken:
            broken.append(chart_id)
        if over_limit(payload) is None and over_limit(changed) is not None:
            over.append(chart_id)
        if changed != payload:
            store(book, owner, chart_id, changed)
    return sorted(broken), sorted(over)


def break_sheet(book, sheet_id):
    """Break sources that read a sheet about to be deleted; its own charts go with the sheet."""
    if not _has_charts(book):
        return []
    broken = []
    for owner, chart_id, payload in objects(book):
        if owner == sheet_id:
            continue
        changed = copy.deepcopy(payload)
        sources = [source for source in _readable_sources(changed) if source["sheet_id"] == sheet_id]
        for source in sources:
            _break(source, None)
        if sources:
            broken.append(chart_id)
            store(book, owner, chart_id, changed)
    return sorted(broken)


def _break_sources(book, selects, breaks):
    """Apply ``breaks`` to every readable source ``selects`` accepts; return the sorted ids of the charts that changed."""
    if not _has_charts(book):
        return []
    broken = []
    for owner, chart_id, payload in objects(book):
        if validate_stored(payload) is not None:
            continue
        changed = copy.deepcopy(payload)
        hit = [source for source in _sources(changed["spec"]) if selects(source)]
        for source in hit:
            breaks(source)
        if hit:
            broken.append(chart_id)
            store(book, owner, chart_id, changed)
    return sorted(broken)


def break_table(book, table_id, kept_column_ids=None):
    """Break table sources on a dropped table, or on columns a schema change no longer keeps."""
    def selects(source):
        return source_kind(source) == "table" and source["table_id"] == table_id and (
            kept_column_ids is None or source["column_id"] not in kept_column_ids)

    return _break_sources(book, selects, lambda source: source.update(table_id=None, column_id=None))


def break_query(book, query_id):
    """Break query sources on a deleted saved query; the result column name stays."""
    return _break_sources(book, lambda source: source_kind(source) == "query" and source["query_id"] == query_id,
                          lambda source: source.update(query_id=None))


def adjust_move(book, sheet_id, rectangle, dest_row, dest_col):
    """Move sources wholly inside a Cut rectangle with it; others stay."""
    if not _has_charts(book):
        return
    r1, c1, r2, c2 = rectangle
    for owner, chart_id, payload in objects(book):
        changed = copy.deepcopy(payload)
        for source in _readable_sources(changed):
            if source["sheet_id"] == sheet_id and r1 <= source["row1"] and source["row2"] <= r2 \
                    and c1 <= source["col1"] and source["col2"] <= c2:
                source["row1"] += dest_row - r1
                source["row2"] += dest_row - r1
                source["col1"] += dest_col - c1
                source["col2"] += dest_col - c1
        if changed != payload:
            store(book, owner, chart_id, changed)


def read(book, args):
    """List charts, optionally for one sheet or one chart, from a state-checked snapshot."""
    require(not set(args) - {"expected_state", "sheet", "chart_id"}, "INVALID_ARGUMENT", "Unsupported chart inspection field")
    with book.lock:
        book.refresh()
        book.db.execute("BEGIN")
        try:
            book.refresh()
            require(args.get("expected_state") == book.state(), "STALE_STATE", "Refresh charts before inspection", 409)
            items = objects(book)
            if "sheet" in args:
                sheet_id = book.sheet(args["sheet"])
                items = [item for item in items if item[0] == sheet_id]
            if "chart_id" in args:
                find(book, args["chart_id"])
                items = [item for item in items if item[1] == args["chart_id"]]
            return {"state": book.state(), "charts": [describe(book.db, *item) for item in items]}
        finally:
            book.db.execute("ROLLBACK")


# Operations.

def _argument(message):
    return EngineError("INVALID_ARGUMENT", message)


def _checked(payload, spec_changed):
    """Check a payload about to be stored; a new specification must be readable."""
    try:
        issue = validate_stored(payload)
    except EngineError as error:
        raise _argument(error.body["message"]) from error
    if spec_changed and issue is not None:
        raise _invalid("the specification cannot be stored")


def merge_patch(target, patch):
    """Apply an RFC 7386 JSON merge patch, returning a new value."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def _input_form(db, sheet_id, chart_id, payload):
    """The input form of a stored chart's specification, refusing unreadable or broken ones."""
    described = describe(db, sheet_id, chart_id, payload)
    issue = described["issue"]
    if issue is not None:
        raise EngineError(issue, "The stored specification cannot be read; replace it with spec",
                          409 if issue == "UNSUPPORTED_SPEC_VERSION" else 400)
    spec = described["spec"]
    for source in _sources(spec):
        if source["broken"]:
            raise EngineError("SOURCE_BROKEN", "A chart source is broken; replace the specification with spec", 409)
        kind = source_kind(source)
        if kind == "table":
            rebuilt = {"kind": "table", "table": source["table_id"], "column": source["column_id"]}
        elif kind == "query":
            rebuilt = {"kind": "query", "query": source["query_id"], "column": source["column"]}
        else:
            rebuilt = {"kind": "range", "sheet": source["sheet_id"], "ref": source["ref"]}
        source.clear()
        source.update(rebuilt)
    return spec


def _create(book, request):
    sheet_id = book.sheet(request.get("sheet", "Sheet1"))
    require("spec" in request, "INVALID_ARGUMENT", "chart_create needs spec")
    spec = normalise_spec(book, request["spec"])
    siblings = [payload for sheet, _, payload in objects(book) if sheet == sheet_id]
    taken = {payload["name"] for payload in siblings}
    number = 1
    while f"Chart {number}" in taken:
        number += 1
    payload = {"name": request.get("name", f"Chart {number}")}
    for key, default in DEFAULTS.items():
        payload[key] = copy.deepcopy(request.get(key, default))
    payload["z"] = max(item["z"] for item in siblings) + 1 if siblings else 0
    payload["spec_version"] = spec["spec_version"]
    payload["spec"] = spec
    _checked(payload, True)
    chart_id = str(uuid.uuid4())
    store(book, sheet_id, chart_id, payload)
    return {"chart_id": chart_id}


def _update(book, request):
    if not any(key in request for key in FIELDS["chart_update"] if key != "chart_id"):
        raise _argument("chart_update needs at least one change")
    if "spec" in request and "spec_patch" in request:
        raise _argument("Give spec or spec_patch, not both")
    if "z" in request and "order" in request:
        raise _argument("Give z or order, not both")
    if "order" in request and request["order"] not in ("front", "back"):
        raise _argument("order must be 'front' or 'back'")
    if "spec_patch" in request and not isinstance(request["spec_patch"], dict):
        raise _argument("spec_patch must be an object")
    chart_id = request["chart_id"]
    sheet_id, before = find(book, chart_id)
    payload = copy.deepcopy(before)
    for key in PLACEMENT:
        if key in request:
            payload[key] = copy.deepcopy(request[key])
    if "z" in request:
        payload["z"] = request["z"]
    if "order" in request:
        others = [item["z"] for sheet, other, item in objects(book) if sheet == sheet_id and other != chart_id]
        if others:
            payload["z"] = max(others) + 1 if request["order"] == "front" else min(others) - 1
    spec_changed = "spec" in request or "spec_patch" in request
    if "spec" in request:
        payload["spec"] = normalise_spec(book, request["spec"])
    elif "spec_patch" in request:
        current = _input_form(book.db, sheet_id, chart_id, before)
        payload["spec"] = normalise_spec(book, merge_patch(current, request["spec_patch"]))
    if spec_changed:
        payload["spec_version"] = payload["spec"]["spec_version"]
    _checked(payload, spec_changed)
    store(book, sheet_id, chart_id, payload)
    return {"chart_id": chart_id}


def mutate(book, request):
    """Run ``chart_create``, ``chart_update`` or ``chart_delete``; return the receipt fields."""
    action = request["action"]
    unknown = sorted(set(request) - set(ENVELOPE_FIELDS) - set(FIELDS[action]))
    if unknown:
        raise _argument(f"Unsupported chart operation field: {', '.join(unknown)}")
    if action == "chart_create":
        return _create(book, request)
    if not isinstance(request.get("chart_id"), str):
        raise _argument(f"{action} needs chart_id as text")
    if action == "chart_update":
        return _update(book, request)
    sheet_id, _ = find(book, request.get("chart_id"))
    store(book, sheet_id, request["chart_id"], None)
    return {"chart_id": request["chart_id"]}
