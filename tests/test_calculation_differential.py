# SPDX-License-Identifier: GPL-3.0-only
"""Differential tests comparing the calculation store against a from-scratch plain Calculator."""

from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import random
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from datahorizon import engine
from datahorizon.engine import EVALUATED, Workbook, display, typed
from datahorizon.formula import FormulaError, column_name
from datahorizon.temporal import Temporal


def _oracle_cells(path: Path, clock_dt: datetime) -> dict[str, dict[tuple[int, int], tuple]]:
    """Evaluate every stored cell in a copy of path using store=False and a plain transactional Calculator."""
    with tempfile.TemporaryDirectory(prefix="dh-oracle-") as temp:
        copy_path = Path(temp) / "oracle.dhwb"
        shutil.copyfile(path, copy_path)
        book = Workbook(copy_path, readonly=True, store=False)
        try:
            book.db.execute("BEGIN")
            try:
                calc = book.calculator(seconds=10, clock=clock_dt, transactional=True)
                sheets = list(book.db.execute("SELECT id, name FROM sheets ORDER BY position, id"))
                out: dict[str, dict[tuple[int, int], tuple]] = {}
                for sheet_id, sheet_name in sheets:
                    grid: dict[tuple[int, int], tuple] = {}
                    for row, col, source, kind, style_text in book.db.execute(
                        "SELECT row, col, source, kind, style FROM cells WHERE sheet_id=? ORDER BY row, col",
                        (sheet_id,),
                    ):
                        item = typed({"source": source, "kind": kind, "style": {}})
                        try:
                            value = calc.cell(sheet_id, row, col) if item["kind"] in EVALUATED else item["value"]
                            if isinstance(value, Temporal):
                                grid[(row, col)] = ("current", (value.kind, value.isoformat()), display(value, {}))
                            else:
                                grid[(row, col)] = ("current", value, display(value, {}))
                        except FormulaError as err:
                            grid[(row, col)] = ("error", None, err.code)
                    out[sheet_name] = grid
                return out
            finally:
                book.db.execute("ROLLBACK")
        finally:
            book.close()


def _normalize_cell(cell: dict) -> tuple:
    if cell["calculation"] == "error":
        return ("error", None, cell["display"])
    val = cell["value"]
    if isinstance(val, Temporal):
        return ("current", (val.kind, val.isoformat()), cell["display"])
    return ("current", val, cell["display"])


def assert_book_matches_oracle(test_case: unittest.TestCase, book: Workbook, path: Path, clock_dt: datetime, *, label: str = ""):
    """Assert every cell read through book (cached non-pending and fresh) matches the from-scratch oracle."""
    expected = _oracle_cells(path, clock_dt)
    with patch("datahorizon.formula.utc_clock", return_value=clock_dt):
        info = book.info()
        sheet_names = [s["name"] for s in info["sheets"]]
        test_case.assertEqual(sheet_names, list(expected.keys()), label)

        for name in sheet_names:
            grid = expected[name]
            max_r = max((r for r, _ in grid), default=1)
            max_c = max((c for _, c in grid), default=1)

            # First check cached read: any cell served as current or error must never be stale.
            cached = book.read(name, dict(r1=1, c1=1, r2=max_r, c2=max_c, calculation="cached"))["cells"]
            for cell in cached:
                if cell["calculation"] == "pending":
                    continue
                pos = (cell["row"], cell["col"])
                test_case.assertIn(pos, grid, f"{label} cached unexpected cell {name}!{pos}")
                test_case.assertEqual(
                    _normalize_cell(cell),
                    grid[pos],
                    f"{label} stale cached cell at {name}!{pos} ({cell['source']})",
                )

            # Then check fresh read: every cell must match the oracle exactly.
            fresh = book.read(name, dict(r1=1, c1=1, r2=max_r, c2=max_c, calculation="fresh"))["cells"]
            actual = {(c["row"], c["col"]): _normalize_cell(c) for c in fresh}
            test_case.assertEqual(actual, grid, f"{label} fresh mismatch on {name}")


class DifferentialCalculationTests(unittest.TestCase):
    def _seed_workbook(self, book: Workbook, rng: random.Random) -> None:
        def apply(action="edit", **args):
            return book.mutate(dict(action=action, operation_id=str(uuid.uuid4()), expected_state=book.state(), **args))

        apply("sheet_add", name="Sheet2")

        # ~100 cells per sheet (20 rows x 5 cols = 200 cells total across 2 sheets):
        # col 1-2: numeric/text inputs; col 3: chain; col 4: ranges over inputs and formulas;
        # col 5: cross-sheet refs, IF with untaken self-ref, volatile cell, #DIV/0! and readers.
        s2_cells = []
        for row in range(1, 21):
            s2_cells.append(dict(row=row, col=1, source=str(rng.randint(1, 20))))
            s2_cells.append(dict(row=row, col=2, source=str(rng.randint(-5, 15))))
            if row == 1:
                s2_cells.append(dict(row=row, col=3, source="=A1+B1"))
            else:
                s2_cells.append(dict(row=row, col=3, source=f"=C{row-1}+A{row}"))
            if row % 2 == 0:
                s2_cells.append(dict(row=row, col=4, source=f"=SUM(A1:A{row})"))
            else:
                s2_cells.append(dict(row=row, col=4, source=f"=SUM(C1:C{row})"))
            if row == 1:
                s2_cells.append(dict(row=row, col=5, source="=1/0"))
            elif row == 2:
                s2_cells.append(dict(row=row, col=5, source="=IFERROR(E1,A2)"))
            elif row == 3:
                s2_cells.append(dict(row=row, col=5, source="=IF(FALSE,E3,A3+B3)"))
            elif row == 4:
                s2_cells.append(dict(row=row, col=5, source="=YEAR(NOW())+A4"))
            elif row == 5:
                s2_cells.append(dict(row=row, col=5, source="=E4+1"))
            else:
                s2_cells.append(dict(row=row, col=5, source=f"=D{row}+B{row}"))
        apply(sheet="Sheet2", cells=s2_cells)

        s1_cells = []
        for row in range(1, 21):
            s1_cells.append(dict(row=row, col=1, source=str(rng.randint(1, 30))))
            s1_cells.append(dict(row=row, col=2, source=str(rng.randint(0, 10))))
            if row == 1:
                s1_cells.append(dict(row=row, col=3, source="=A1*2"))
            else:
                s1_cells.append(dict(row=row, col=3, source=f"=C{row-1}+B{row}"))
            if row % 2 == 1:
                s1_cells.append(dict(row=row, col=4, source=f"=SUM(A1:B{row})"))
            else:
                s1_cells.append(dict(row=row, col=4, source=f"=SUM(C1:C{row})"))
            if row == 1:
                s1_cells.append(dict(row=row, col=5, source="=Sheet2!A1+C1"))
            elif row == 2:
                s1_cells.append(dict(row=row, col=5, source="=SUM(Sheet2!A1:A10)"))
            elif row == 3:
                s1_cells.append(dict(row=row, col=5, source="=IF(FALSE,E3,Sheet2!C3+1)"))
            elif row == 4:
                s1_cells.append(dict(row=row, col=5, source="=Sheet2!E4+10"))
            elif row == 5:
                s1_cells.append(dict(row=row, col=5, source="=1/0"))
            elif row == 6:
                s1_cells.append(dict(row=row, col=5, source="=IFERROR(E5,D6)"))
            else:
                ref_row = rng.randint(1, 20)
                s1_cells.append(dict(row=row, col=5, source=f"=Sheet2!C{ref_row}+D{row}"))
        apply(sheet="Sheet1", cells=s1_cells)

    def _run_seed(self, seed: int, steps: int = 60):
        rng = random.Random(seed)
        base_clock = datetime(2025, 1, 1, 12, 0, 0)
        with tempfile.TemporaryDirectory(prefix=f"dh-diff-{seed}-") as temp:
            path = Path(temp) / f"diff_{seed}.dhwb"
            book = Workbook(path, create=True)
            rename_counter = 0
            try:
                self._seed_workbook(book, rng)
                assert_book_matches_oracle(self, book, path, base_clock, label=f"seed={seed} initial")

                actions = [
                    "edit", "edit", "edit",
                    "clear",
                    "undo", "redo",
                    "rows_insert", "rows_delete",
                    "sheet_rename",
                    "direct_edit",
                    "reopen",
                    "fresh_read", "cached_read",
                ]
                for step in range(steps):
                    clock_dt = base_clock + timedelta(days=step)
                    action = rng.choice(actions)
                    info = book.info()
                    sheets = info["sheets"]
                    sheet_entry = rng.choice(sheets)
                    sheet_name = sheet_entry["name"]
                    sheet_id = sheet_entry["id"]

                    def apply(act="edit", **args):
                        return book.mutate(dict(action=act, operation_id=str(uuid.uuid4()), expected_state=book.state(), **args))

                    with patch("datahorizon.formula.utc_clock", return_value=clock_dt):
                        if action == "edit":
                            row = rng.randint(1, 20)
                            col = rng.randint(1, 5)
                            choice = rng.randint(0, 6)
                            if choice <= 2:
                                source = str(rng.randint(-20, 50))
                            elif choice == 3:
                                r_end = rng.randint(row, min(20, row + 5))
                                source = f"=SUM(A{row}:A{r_end})"
                            elif choice == 4:
                                other_name = next(s["name"] for s in sheets if s["id"] != sheet_id)
                                source = f"='{other_name}'!A{rng.randint(1, 15)}+1"
                            elif choice == 5:
                                source = "=YEAR(NOW())+A1"
                            else:
                                source = f"=IF(FALSE,{column_name(col)}{row},A{row}+1)"
                            apply(sheet=sheet_name, cells=[dict(row=row, col=col, source=source)])
                        elif action == "clear":
                            row = rng.randint(1, 20)
                            col = rng.randint(1, 5)
                            apply(sheet=sheet_name, cells=[dict(row=row, col=col, clear=True)])
                        elif action == "undo":
                            if book.undo:
                                apply("undo")
                        elif action == "redo":
                            if book.redo:
                                apply("redo")
                        elif action == "rows_insert":
                            apply("rows_insert", sheet=sheet_name, start=rng.randint(1, 15), count=1)
                        elif action == "rows_delete":
                            apply("rows_delete", sheet=sheet_name, start=rng.randint(1, 15), count=1)
                        elif action == "sheet_rename":
                            rename_counter += 1
                            new_name = f"S{seed}_{rename_counter}"
                            apply("sheet_rename", sheet=sheet_name, name=new_name)
                        elif action == "direct_edit":
                            row = rng.randint(1, 20)
                            col = rng.randint(1, 2)
                            with closing(sqlite3.connect(path)) as direct, direct:
                                if rng.random() < 0.25:
                                    direct.execute("DELETE FROM cells WHERE sheet_id=? AND row=? AND col=?", (sheet_id, row, col))
                                else:
                                    direct.execute(
                                        "INSERT INTO cells(sheet_id,row,col,source,kind,style) VALUES(?,?,?,?,'number','{}') "
                                        "ON CONFLICT(sheet_id,row,col) DO UPDATE SET source=excluded.source,kind=excluded.kind",
                                        (sheet_id, row, col, str(rng.randint(-10, 99))),
                                    )
                        elif action == "reopen":
                            book.close()
                            book = Workbook(path)
                        elif action == "fresh_read":
                            r1 = rng.randint(1, 15)
                            c1 = rng.randint(1, 4)
                            book.read(sheet_name, dict(r1=r1, c1=c1, r2=r1 + 5, c2=c1 + 1, calculation="fresh"))
                        elif action == "cached_read":
                            r1 = rng.randint(1, 15)
                            c1 = rng.randint(1, 4)
                            book.read(sheet_name, dict(r1=r1, c1=c1, r2=r1 + 5, c2=c1 + 1, calculation="cached"))

                    assert_book_matches_oracle(self, book, path, clock_dt, label=f"seed={seed} step={step} ({action})")
            finally:
                book.close()

    def test_differential_seeds_0_to_19(self):
        for seed in range(20):
            with self.subTest(seed=seed):
                self._run_seed(seed, steps=60)

    def test_differential_with_delete_slice_1(self):
        with patch.object(engine, "DELETE_SLICE", 1):
            for seed in range(5):
                with self.subTest(seed=seed):
                    self._run_seed(seed, steps=60)

    def test_differential_with_store_write_seconds_0(self):
        with patch.object(engine, "STORE_WRITE_SECONDS", 0):
            for seed in range(5):
                with self.subTest(seed=seed):
                    self._run_seed(seed, steps=60)


if __name__ == "__main__":
    unittest.main()
