# SPDX-License-Identifier: GPL-3.0-only
"""Workbook fixture for the calculation experiment, with independently computed expected values.

Sheet1: columns A-I are stored inputs, column J is a chained running total of column A,
and column K (optional) is the prefix form of the same total. Summary holds three formulas.
"""
import uuid

from datahorizon.engine import Workbook

DATA_COLUMNS = 9


def _running_total(row):
    full, rest = divmod(row, 97)
    return full * (96 * 97 // 2) + rest * (rest + 1) // 2


def expected_value(rows, sheet, row, col):
    if sheet == "Summary":
        return {1: _running_total(rows), 2: _running_total(rows), 3: 0}.get(row) if col == 1 else None
    if not 1 <= row <= rows:
        return None
    if col == 1:
        return row % 97
    if col < DATA_COLUMNS:
        return row * col
    if col == DATA_COLUMNS:
        return f"item-{row}"
    return _running_total(row) if col in (10, 11) else None


def _cells(sheet, rows, prefix_rows):
    for row in range(1, rows + 1):
        yield sheet, row, 1, str(row % 97), "number"
        for col in range(2, DATA_COLUMNS):
            yield sheet, row, col, str(row * col), "number"
        yield sheet, row, DATA_COLUMNS, f"item-{row}", "text"
        yield sheet, row, 10, "=A1" if row == 1 else f"=J{row - 1}+A{row}", "formula"
        if row <= prefix_rows:
            yield sheet, row, 11, f"=SUM(A$1:A{row})", "formula"


def create_fixture(path, rows=100_000, prefix_rows=0):
    book = Workbook(path, create=True)
    try:
        book.mutate(dict(action="sheet_add", name="Summary", expected_state=book.state(), operation_id=uuid.uuid4().hex))
        data, summary = book.sheet("Sheet1"), book.sheet("Summary")
        insert = "INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,?,?,?)"
        book.db.execute("BEGIN")
        book.db.executemany(insert, _cells(data, rows, prefix_rows))
        book.db.executemany(insert, [(summary, 1, 1, f"=SUM(Sheet1!A1:A{rows})", "formula"),
                                     (summary, 2, 1, f"=Sheet1!J{rows}", "formula"),
                                     (summary, 3, 1, "=A1-A2", "formula")])
        book.db.execute("COMMIT")
        return {"data": data, "summary": summary}
    finally:
        book.close()
