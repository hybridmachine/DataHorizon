# SPDX-License-Identifier: GPL-3.0-only
"""Read a stored chart's sources through the engine, and suggest a chart from a range or a table.

``resolve`` turns a stored chart payload into the plain values
``chart_render.render`` draws.  ``suggest`` proposes an input-form
specification for a selected rectangle or a table.  Neither writes anything.
"""
import math

from . import chart_sources, charts, relational
from .chart_render import LIMIT_PIE_POINTS, LIMIT_POINTS, LIMIT_SERIES
from .engine import MAX_CELLS, EngineError, rectangle, require
from .formula import column_name

GAP_CAUSES = ("empty", "text", "error")
# Each Workbook.read has a fixed two-second calculation deadline, so sources are read in slices.
SLICE_CELLS = 1_000


def _check_sources(book, sources):
    """Raise SOURCE_BROKEN for a source that names no data, or a sheet, table column or saved query that is gone."""
    for source in sources:
        if charts.is_broken(source):
            raise EngineError("SOURCE_BROKEN", "A chart source no longer names its data", 409)
    with book.lock:
        existing = {row[0] for row in book.db.execute("SELECT id FROM sheets")}
        for source in sources:
            kind = charts.source_kind(source)
            if kind == "range" and source["sheet_id"] not in existing:
                raise EngineError("SOURCE_BROKEN", "A chart source names a sheet that no longer exists", 409)
            if kind == "table":
                chart_sources.find(book, source)
            if kind == "query":
                chart_sources.find_query(book, source)


def _slices(source):
    """Split a one-row or one-column source into rectangles of at most SLICE_CELLS cells."""
    if source["row1"] == source["row2"]:
        low, high, axis = source["col1"], source["col2"], ("c1", "c2")
        fixed = {"r1": source["row1"], "r2": source["row2"]}
    else:
        low, high, axis = source["row1"], source["row2"], ("r1", "r2")
        fixed = {"c1": source["col1"], "c2": source["col2"]}
    for start in range(low, high + 1, SLICE_CELLS):
        yield {**fixed, axis[0]: start, axis[1]: min(start + SLICE_CELLS - 1, high)}


def _read(book, source, state, checkpoint=None):
    """Read one source slice by slice, each a bounded call; return its cells by position.

    ``checkpoint(cells)`` is called after each slice with the cells read so far.
    """
    cells = {}
    done = 0
    for part in _slices(source):
        try:
            result = book.read(source["sheet_id"], {**part, "calculation": "fresh"}, limit=MAX_CELLS)
        except EngineError as error:
            if error.body["code"] == "TIMEOUT":
                raise EngineError("CALCULATION_FAILED", "The chart's sources could not be calculated in time", 409) from error
            raise
        if result["state"] != state:
            raise EngineError("STALE_STATE", "Workbook changed while reading chart sources", 409)
        cells.update(((cell["row"], cell["col"]), cell) for cell in result["cells"])
        done += (part["r2"] - part["r1"] + 1) * (part["c2"] - part["c1"] + 1)
        if checkpoint is not None:
            checkpoint(done)
    return cells


def _positions(source):
    return [(row, col) for row in range(source["row1"], source["row2"] + 1)
            for col in range(source["col1"], source["col2"] + 1)]


def _cells(snapshot, source, state, role, chart_type, progress, query_result):
    """Return the cells of one source in order, ``None`` for an empty one.

    ``progress(cells)`` is called with the number of cells read so far in this source.
    ``query_result(source)`` returns the saved query and its result for a query source.
    """
    kind = charts.source_kind(source)
    pie = chart_type == "pie" and role in ("values", "categories")
    limit = LIMIT_PIE_POINTS if pie else LIMIT_POINTS
    if kind == "table":
        cells = chart_sources.read_table(snapshot, source, state, limit)
        progress(len(cells))
        return cells
    if kind == "query":
        item, result = query_result(source)
        cells = chart_sources.read_query(item, result, source["column"], limit)
        progress(len(cells))
        return cells
    read = _read(snapshot, source, state, progress)
    return [read.get(position) for position in _positions(source)]


def _text(cells):
    return [cell["display"] if cell is not None else "" for cell in cells]


def _classify(cell):
    """Return ``(number, None)`` for a point, else ``(None, cause)`` for a gap."""
    if cell is None or (cell.get("calculation") != "error" and cell.get("value") is None):
        return None, "empty"
    if cell.get("calculation") == "error":
        return None, "error"
    value = cell["value"]
    if isinstance(value, bool) or cell.get("value_kind") or not isinstance(value, (int, float)):
        return None, "text"
    if not math.isfinite(value):
        return None, "text"
    return float(value), None


def _numbers(cells, source):
    """Return ``(values, causes)``: a number or ``None`` per cell, and why each ``None`` is a gap."""
    values, causes = [], []
    positions = _positions(source) if charts.source_kind(source) == "range" else [None] * len(cells)
    for position, cell in zip(positions, cells):
        # #LIMIT! means the engine could not compute the value: the resolve cannot finish, it is not a gap.
        if cell is not None and cell.get("calculation") == "error" and cell.get("display") == "#LIMIT!":
            row, col = position
            raise EngineError("CALCULATION_FAILED", f"{column_name(col)}{row} could not be calculated: {cell.get('error')}", 409)
        number, cause = _classify(cell)
        values.append(number)
        causes.append(cause)
    return values, causes


def resolve(snapshot, payload, state, checkpoint=None, run_query=None):
    """Return the renderer's input for a stored chart, with point and gap counts.

    ``run_query(query_id)`` runs a saved query and returns ``{"columns", "rows"}``; it is needed only
    by a chart with a saved-query source. Each saved query runs once per resolve, outside any lock.
    """
    issue = charts.validate_stored(payload)
    if issue is not None:
        status = 409 if issue == "UNSUPPORTED_SPEC_VERSION" else 400
        raise EngineError(issue, f"The chart specification cannot be read ({issue})", status)
    spec = payload["spec"]
    limit = charts.limit_error(spec)
    if limit is not None:
        raise limit
    sources = charts._sources(spec)
    if run_query is None and any(charts.source_kind(source) == "query" for source in sources):
        raise EngineError("INVALID_ARGUMENT", "Saved-query chart sources need a query runner")
    _check_sources(snapshot, sources)

    cells_read = 0
    query_results = {}

    def query_result(source):
        query_id = source["query_id"]
        if query_id not in query_results:
            with snapshot.lock:
                item = chart_sources.find_query(snapshot, source)
            if checkpoint is not None:
                checkpoint(cells_read, "Running saved query")
            query_results[query_id] = (item, chart_sources.run_query(run_query, item))
        return query_results[query_id]

    def fetch(source, role):
        nonlocal cells_read
        before = cells_read

        def progress(done):
            nonlocal cells_read
            cells_read = before + done
            if checkpoint is not None:
                checkpoint(cells_read, "Reading sources")
        cells = _cells(snapshot, source, state, role, spec["type"], progress, query_result)
        cells_read = before + len(cells)
        if cells_read > charts.LIMIT_CELLS:
            raise EngineError("LIMIT_CELLS", f"The chart's sources hold more than {charts.LIMIT_CELLS} cells")
        return cells

    categories = None
    if spec["categories"] is not None:
        categories = _text(fetch(spec["categories"]["source"], "categories"))
    gaps = dict.fromkeys(GAP_CAUSES, 0)
    series = []
    points = 0
    for item in spec["series"]:
        if "source" in item["name"]:
            source = item["name"]["source"]
            name = _text(fetch(source, "name"))[0]
        else:
            name = item["name"]["text"]
        source = item["values"]["source"]
        values, causes = _numbers(fetch(source, "values"), source)
        x = None
        if item["x"] is not None:
            # A scatter point draws only with both coordinates: a usable value whose x is missing
            # or unusable is a gap too, classified by what is wrong with the x cell.
            source = item["x"]["source"]
            x, x_causes = _numbers(fetch(source, "x"), source)
            x_causes += ["empty"] * (len(causes) - len(x_causes))
            causes = [cause or x_cause for cause, x_cause in zip(causes, x_causes)]
        for cause in causes:
            if cause is None:
                points += 1
            else:
                gaps[cause] += 1
        series.append({"name": name, "values": values, "x": x})
    if spec["type"] == "pie" and any(v is not None and v < 0 for item in series for v in item["values"]):
        raise EngineError("NEGATIVE_IN_PIE", "A pie chart cannot show negative values")
    resolved = {"type": spec["type"], "grouping": spec["grouping"], "title": spec["title"]["text"],
                "categories": categories, "series": series, "axes": spec["axes"], "legend": spec["legend"]}
    return {"resolved": resolved, "counts": {"points": points, "gaps": gaps}, "state": state}


def _holds_number(cell):
    return cell is not None and cell.get("calculation") != "error" and _classify(cell)[0] is not None


def _suggest_table(book, identity, chart_type):
    """Suggest a specification over a table's columns, given by id, reading the schema only."""
    with book.lock:
        found = relational.table(book.db, identity)

    def source(column):
        return {"kind": "table", "table": found["id"], "column": column["id"]}

    columns = found["columns"]
    data = [column for column in columns if column["type"] in ("integer", "number")]
    require(data, "INVALID_SPEC", "The table holds no numeric data column")
    label = next((column for column in columns if column["type"] not in ("integer", "number", "boolean")), None)
    x_column = None
    if chart_type == "scatter":
        x_column, data = data[0], data[1:]
        require(data, "INVALID_SPEC", "A scatter chart needs an x column and a value column")
    elif chart_type == "pie":
        data = data[:1]
    require(len(data) <= LIMIT_SERIES, "LIMIT_SERIES", f"The table would make more than {LIMIT_SERIES} series")
    series = []
    for column in data:
        item = {"name": {"text": column["name"]}, "values": {"source": source(column)}}
        if x_column is not None:
            item["x"] = {"source": source(x_column)}
        series.append(item)
    spec = {"spec_version": 1, "type": chart_type, "title": {"text": None}, "series": series}
    if chart_type != "scatter":
        spec["categories"] = {"source": source(label)} if label is not None else None
    with book.lock:
        charts.normalise_spec(book, spec)
    return {"spec": spec}


def suggest(book, args):
    """Suggest an input-form specification for the rectangle, or the table, in ``args``."""
    require(args.get("expected_state") == book.state(), "STALE_STATE", "Workbook changed. Select the range again", 409)
    chart_type = args.get("type", "column")
    require(chart_type in charts.TYPES, "INVALID_ARGUMENT", f"type must be one of {', '.join(charts.TYPES)}")
    if "table" in args:
        clash = [key for key in ("sheet", "r1", "c1", "r2", "c2") if key in args]
        require(not clash, "INVALID_ARGUMENT", f"table cannot be combined with {', '.join(clash)}")
        return _suggest_table(book, args["table"], chart_type)
    r1, c1, r2, c2 = rectangle(args, MAX_CELLS)
    result = book.read(args.get("sheet"), {"r1": r1, "c1": c1, "r2": r2, "c2": c2, "calculation": "fresh"}, limit=MAX_CELLS)
    require(result["state"] == args["expected_state"], "STALE_STATE", "Workbook changed. Select the range again", 409)
    cells = {(cell["row"], cell["col"]): cell for cell in result["cells"]}
    with book.lock:
        sheet_name = book.db.execute("SELECT name FROM sheets WHERE id=?", (result["sheet"],)).fetchone()[0]

    def number_in(rows, cols):
        return any(_holds_number(cells.get((row, col))) for row in rows for col in cols)

    columns = range(c1, c2 + 1)
    header = r2 > r1 and not number_in([r1], columns) and number_in(range(r1 + 1, r2 + 1), columns)
    first = r1 + 1 if header else r1
    rows = range(first, r2 + 1)
    has_categories = c2 > c1 and not number_in(rows, [c1]) and number_in(rows, range(c1 + 1, c2 + 1))
    data = list(range(c1 + 1 if has_categories else c1, c2 + 1))
    require(number_in(rows, data), "INVALID_SPEC", "The selection holds no numeric data column")

    def source(row1, col1, row2=None, col2=None):
        row2, col2 = row1 if row2 is None else row2, col1 if col2 is None else col2
        first_ref, last_ref = f"{column_name(col1)}{row1}", f"{column_name(col2)}{row2}"
        return {"kind": "range", "sheet": sheet_name, "ref": first_ref if first_ref == last_ref else f"{first_ref}:{last_ref}"}

    x_column = None
    if chart_type == "scatter":
        x_column, data = data[0], data[1:]
        require(data, "INVALID_SPEC", "A scatter chart needs an x column and a value column")
    elif chart_type == "pie":
        data = data[:1]
    require(len(data) <= LIMIT_SERIES, "LIMIT_SERIES", f"The selection would make more than {LIMIT_SERIES} series")

    series = []
    for position, col in enumerate(data, start=1):
        name = {"source": source(r1, col)} if header else {"text": f"Series {position}"}
        item = {"name": name, "values": {"source": source(first, col, r2, col)}}
        if x_column is not None:
            item["x"] = {"source": source(first, x_column, r2, x_column)}
        series.append(item)
    spec = {"spec_version": 1, "type": chart_type, "title": {"text": None}, "series": series}
    if chart_type != "scatter":
        spec["categories"] = {"source": source(first, c1, r2, c1)} if has_categories else None
    with book.lock:
        charts.normalise_spec(book, spec)  # a suggestion must be creatable as it stands; refuse here, by name
    return {"spec": spec}
