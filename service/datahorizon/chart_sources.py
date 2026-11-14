# SPDX-License-Identifier: GPL-3.0-only
"""Read the cells of a chart's table-column and saved-query sources."""
import time

from . import query_views, relational
from .chart_render import LIMIT_PIE_POINTS, LIMIT_POINTS, format_tick
from .engine import EngineError, sql_deadline

DEADLINE_SECONDS = 2
TEMPORAL = ("date", "time", "datetime")
# A query run is asked for one row more than a series may hold, so a longer result is refused by its own row limit.
QUERY_ROWS = LIMIT_POINTS + 1


def _broken(message):
    return EngineError("SOURCE_BROKEN", message, 409)


def find(book, source):
    """Return ``(table, column)`` for a table source; raise SOURCE_BROKEN when either is gone."""
    if source["table_id"] is None or source["column_id"] is None:
        raise _broken("A chart source no longer names a table column")
    item = next((found for found in relational.tables(book.db) if found["id"] == source["table_id"]), None)
    if item is None:
        raise _broken("A chart source names a table that no longer exists")
    column = next((found for found in item["columns"] if found["id"] == source["column_id"]), None)
    if column is None:
        raise _broken(f"A chart source names a column that no longer exists in table {item['name']}")
    return item, column


def _display(column, stored):
    if stored is None:
        return ""
    if column["type"] == "boolean":
        return "TRUE" if stored else "FALSE"
    if column["type"] in ("integer", "number"):
        return format_tick(stored)
    return stored


def _cell(column, stored):
    if stored is None:
        return None
    value = relational.from_sql(column, stored)
    cell = {"value": value, "display": _display(column, value), "calculation": "current"}
    if column["type"] in TEMPORAL:
        cell["value_kind"] = column["type"]
    return cell


def read_table(book, source, state, limit):
    """Return one cell per row of a table column, in row order; ``None`` stands for an empty cell.

    Reads at most ``limit + 1`` rows and refuses a table that holds more than ``limit``.
    """
    with book.lock:
        deadline = time.monotonic() + DEADLINE_SECONDS
        book.refresh(deadline=deadline)
        book.db.execute("BEGIN")
        try:
            with sql_deadline(book.db, deadline, cancel_event=book._read_cancel_event):
                book.refresh(deadline=deadline)
                if book.state() != state:
                    raise EngineError("STALE_STATE", "Workbook changed while reading chart sources", 409)
                item, column = find(book, source)
                rows = book.db.execute(
                    f'SELECT {relational.quote(column["name"])} FROM {relational.quote(item["name"])} '
                    'ORDER BY "_dh_row_id" LIMIT ?', (limit + 1,)).fetchall()
        except EngineError as error:
            if error.body["code"] == "TIMEOUT":
                raise EngineError("CALCULATION_FAILED", "The chart's sources could not be calculated in time", 409) from error
            raise
        finally:
            book.db.execute("ROLLBACK")
    if len(rows) > limit:
        raise _limit_error("Table", item["name"], limit)
    return [_cell(column, row[0]) for row in rows]


def _limit_error(kind, name, limit):
    pie = limit == LIMIT_PIE_POINTS
    return EngineError("LIMIT_PIE_POINTS" if pie else "LIMIT_POINTS",
                       f"{kind} {name} holds more than {limit:,} rows; "
                       f"the limit for a {'pie' if pie else 'series'} is {limit:,}")


def find_query(book, source):
    """Return the saved query a query source names; raise SOURCE_BROKEN when it is gone."""
    if source["query_id"] is None:
        raise _broken("A chart source no longer names a saved query")
    item = next((found for found in query_views.queries(book.db) if found["id"] == source["query_id"]), None)
    if item is None:
        raise _broken("A chart source names a saved query that no longer exists")
    return item


def run_query(run, query_id):
    """Run a saved query through ``run``; return its result, or ``None`` when it holds more rows than any series may.

    ``run`` is asked for at most QUERY_ROWS rows. Its row refusal is told from its other QUERY_LIMIT
    refusals (memory, labels, values), which pass through unchanged, by that refusal's exact message.
    """
    try:
        result = run(query_id)
    except EngineError as error:
        code = error.body["code"]
        if code == "QUERY_LIMIT" and error.body["message"] == f"Query exceeds {QUERY_ROWS:,} rows":
            return None
        if code == "TIMEOUT":
            raise EngineError("CALCULATION_FAILED", "The chart's sources could not be calculated in time", 409) from error
        raise
    return result


def read_query(item, result, column, limit):
    """Return one cell per result row of the named column; refuse a result over ``limit`` rows or a column that is not one."""
    if result is None or len(result["rows"]) > limit:
        raise _limit_error("Saved query", item["name"], limit)
    matches = result["columns"].count(column)
    if matches != 1:
        problem = "no result column" if matches == 0 else "more than one result column"
        raise _broken(f"Saved query {item['name']} has {problem} named {column}")
    return query_cells(result, column)


def _query_cell(value):
    if value is None:
        return None
    if isinstance(value, dict):
        # The engine's tag for an integer beyond 2^53.
        return {"value": float(int(value["value"])), "display": value["value"], "calculation": "current"}
    if isinstance(value, (int, float)):
        return {"value": value, "display": format_tick(value), "calculation": "current"}
    return {"value": value, "display": value, "calculation": "current"}


def query_cells(result, column):
    """Return the cells of one result column in row order, in the shape ``read_table`` returns."""
    index = result["columns"].index(column)
    return [_query_cell(row[index]) for row in result["rows"]]
