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

from . import relational
from .chart_render import LIMIT_PIE_POINTS, LIMIT_POINTS, LIMIT_SERIES
from .engine import MAX_COLS, MAX_ROWS, EngineError, encoded, require
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
        sheet, ref = source["sheet_id"], source["ref"]
        source.clear()
        source.update(kind="range", sheet=sheet, ref=ref)
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
    if action == "chart_update":
        return _update(book, request)
    sheet_id, _ = find(book, request.get("chart_id"))
    store(book, sheet_id, request["chart_id"], None)
    return {"chart_id": request["chart_id"]}
