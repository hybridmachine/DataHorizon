# SPDX-License-Identifier: GPL-3.0-only
"""Read the cells of a chart's table-column source from a workbook snapshot."""
import time

from . import relational
from .chart_render import LIMIT_PIE_POINTS, LIMIT_POINTS, format_tick
from .engine import EngineError, sql_deadline

DEADLINE_SECONDS = 2
TEMPORAL = ("date", "time", "datetime")


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
        pie = limit == LIMIT_PIE_POINTS
        raise EngineError("LIMIT_PIE_POINTS" if pie else "LIMIT_POINTS",
                          f"Table {item['name']} holds more than {limit:,} rows; "
                          f"the limit for a {'pie' if pie else 'series'} is {limit:,}")
    return [_cell(column, row[0]) for row in rows]
