# SPDX-License-Identifier: GPL-3.0-only
"""Ranges are read as one ordered scan and folded by the functions that read them (calculation stage 2, Task 4)."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calculation_corpus import make
from datahorizon import formula
from datahorizon.engine import Workbook
from datahorizon.formula import FormulaError

S = "Sheet1"
FOLDED = ("SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "AND", "OR", "CONCAT")


def shaped(cells):
    """{"A1": source} as the corpus's (sheet, row, col) keys."""
    result = {}
    for ref, source in cells.items():
        letters = ref.rstrip("0123456789")
        col = 0
        for ch in letters:
            col = col*26 + ord(ch)-64
        result[S, int(ref[len(letters):]), col] = source
    return result


def column(letter, sources, start=1):
    return {f"{letter}{start+i}": source for i, source in enumerate(sources)}


class StreamedRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def book(self, cells):
        folder = Path(self.temp.name) / str(len(list(Path(self.temp.name).iterdir())))
        folder.mkdir()
        book = Workbook(make({"name": "case", "cells": shaped(cells)}, folder))
        self.addCleanup(book.close)
        return book

    @staticmethod
    def calculator(book):
        return book.calculator(seconds=30, transactional=True)

    @staticmethod
    def outcome(calculator, book, ref):
        """The cell's value, or its error code."""
        (_, row, col), = shaped({ref: None})
        try:
            return calculator.cell(book.sheet(S), row, col)
        except FormulaError as error:
            return error.code

    def together(self, book, refs):
        """Each cell's value or error code from one read of the rectangle holding them."""
        keys = [next(iter(shaped({ref: None}))) for ref in refs]
        rows, cols = [key[1] for key in keys], [key[2] for key in keys]
        cells = book.read(S, dict(r1=min(rows), c1=min(cols), r2=max(rows), c2=max(cols)))["cells"]
        found = {(cell["row"], cell["col"]): cell["display"] if cell["calculation"] == "error" else cell["value"] for cell in cells}
        return {ref: found[key[1], key[2]] for ref, key in zip(refs, keys)}

    def test_sum_over_more_than_10000_cells(self):
        numbers = [str(n % 97) for n in range(20_000)]
        book = self.book({**column("A", numbers), "C1": "=SUM(A1:A20000)"})
        self.assertEqual(self.outcome(self.calculator(book), book, "C1"), sum(n % 97 for n in range(20_000)))

    def test_a_range_of_formulas_is_evaluated_in_one_pass(self):
        rows = 5_000
        book = self.book({**{f"A{r}": f"=B{r}*2" for r in range(1, rows+1)}, **column("B", [str(r) for r in range(1, rows+1)]),
                          "C1": f"=SUM(A1:A{rows})"})
        calculator, scans = self.calculator(book), []
        scan = calculator.scan

        def counted(*rectangle):
            scans.append(rectangle)
            return scan(*rectangle)
        calculator.scan = counted
        self.assertEqual(self.outcome(calculator, book, "C1"), rows*(rows+1))
        self.assertLessEqual(self.passes(scans, book.sheet(S), 1), 3)

    @staticmethod
    def passes(scans, sheet, col):
        """How many times a range in column col starting at row 1 was scanned from its start: once per pass."""
        return sum(1 for scan in scans if scan[:3] == (sheet, 1, col) and scan[4] == col)

    def test_a_sum_over_2000_uncalculated_formulas_is_linear(self):
        rows = 2_000
        book = self.book({**{f"B{r}": f"=C{r}*2" for r in range(1, rows+1)}, **column("C", [str(r) for r in range(1, rows+1)]),
                          "A1": f"=SUM(B1:B{rows})"})
        calculator, scans, evaluations = self.calculator(book), [], []
        scan, evaluate_cell, reader = calculator.scan, calculator._evaluate_cell, (book.sheet(S), 1, 1)

        def counted_scan(*rectangle):
            scans.append(rectangle)
            return scan(*rectangle)

        def counted_evaluation(key):
            evaluations.append(key)
            return evaluate_cell(key)
        calculator.scan, calculator._evaluate_cell = counted_scan, counted_evaluation
        self.assertEqual(self.outcome(calculator, book, "A1"), rows*(rows+1))
        self.assertLessEqual(evaluations.count(reader), 3)
        self.assertLessEqual(self.passes(scans, book.sheet(S), 2), 3)

    def test_a_range_is_scanned_in_bands_with_the_deadline_checked_between_them(self):
        rows = 5*formula.SCAN_ROWS
        # Many stored cells beside the range, none in it: every band yields nothing.
        book = self.book({**{f"B{r}": "1" for r in range(1, rows+1)}, **{f"C{r}": "1" for r in range(1, rows+1)},
                          "E1": f"=SUM(A1:A{rows})"})
        calculator, checks, scans = self.calculator(book), [], []
        check, scan = calculator.check, calculator.scan

        def counted_check():
            checks.append(len(scans))
            check()

        def counted_scan(*rectangle):
            scans.append(rectangle)
            return scan(*rectangle)
        calculator.check, calculator.scan = counted_check, counted_scan
        self.assertEqual(self.outcome(calculator, book, "E1"), 0)
        sheet = book.sheet(S)
        self.assertEqual(scans, [(sheet, 1+band*formula.SCAN_ROWS, 1, (band+1)*formula.SCAN_ROWS, 1) for band in range(5)])
        self.assertTrue(all(band in checks for band in range(5)))  # a check before each band
        late = book.calculator(seconds=0, transactional=True)
        self.assertEqual(self.outcome(late, book, "E1"), "#LIMIT!")

    def test_a_scan_stops_reading_bands_when_its_reader_stops(self):
        rows = 5*formula.SCAN_ROWS
        book = self.book(column("A", ["1"]*rows))
        calculator, scans = self.calculator(book), []
        scan = calculator.scan
        calculator.scan = lambda *rectangle: scans.append(rectangle) or scan(*rectangle)
        stream = calculator.stored((book.sheet(S), 1, 1, rows, 1))
        self.assertEqual(next(stream), (1, 1, 1))
        stream.close()
        self.assertEqual(len(scans), 1)

    def test_the_count_belongs_to_one_formula(self):
        # An over-limit formula leaves nothing behind for the next read of the same calculator.
        book = self.book({**column("A", ["1"]*60), "E1": "=SUM(A1:A60)", "E2": "7", "E3": "=A1+1"})
        with patch.object(formula, "FORMULA_CELL_LIMIT", 50):
            calculator = self.calculator(book)
            self.assertEqual([self.outcome(calculator, book, ref) for ref in ("E1", "E2", "E3")], ["#LIMIT!", 7, 2])
        # A formula that waits on a precedent reading just under the limit is not charged that precedent's reads.
        book = self.book({**column("A", ["1"]*49), "B1": "=SUM(A1:A49)", "C1": "=B1+A1"})
        with patch.object(formula, "FORMULA_CELL_LIMIT", 50):
            self.assertEqual(self.outcome(self.calculator(book), book, "C1"), 50)

    def test_review_focus_1(self):
        cells = {"B1": "=SUM(A1:A2)", "A1": "=1/0", "A2": "=B1"}
        expected = {"A1": "#DIV/0!", "A2": "#DIV/0!", "B1": "#DIV/0!"}
        for first in ("B1", "A2"):
            with self.subTest(first=first):
                book = self.book(cells)
                calculator = self.calculator(book)
                reads = [first] + [ref for ref in ("B1", "A1", "A2") if ref != first]
                self.assertEqual({ref: self.outcome(calculator, book, ref) for ref in reads}, expected)
                self.assertEqual((calculator.following, calculator.active), ([], set()))
        self.assertEqual(self.together(self.book(cells), ["A1", "A2", "B1"]), expected)

    def test_a_real_cycle_through_a_range(self):
        cells = {"B1": "=SUM(A1:A2)", "A1": "1", "A2": "=B1"}
        for first in ("B1", "A2"):
            with self.subTest(first=first):
                book = self.book(cells)
                calculator = self.calculator(book)
                second = "A2" if first == "B1" else "B1"
                self.assertEqual((self.outcome(calculator, book, first), self.outcome(calculator, book, second)),
                                 ("#CYCLE!", "#CYCLE!"))
        self.assertEqual(self.together(self.book(cells), ["A2", "B1"]), {"A2": "#CYCLE!", "B1": "#CYCLE!"})

    def test_a_cycle_met_while_evaluating_speculatively_is_not_kept(self):
        # A3 is evaluated ahead of time while B1 is being followed; its own precedent C1 meets B1 and the
        # #CYCLE! it would record depends on that, so neither is kept: both read alone give B1's value.
        cells = {"B1": "=SUM(A1:A3)", "A1": "=1/0", "A2": "=4", "A3": "=C1", "C1": "=B1"}
        book = self.book(cells)
        calculator = self.calculator(book)
        self.assertEqual(self.outcome(calculator, book, "B1"), "#DIV/0!")
        sheet = book.sheet(S)
        self.assertEqual(calculator.cache.get((sheet, 2, 1)), 4)
        self.assertNotIn((sheet, 3, 1), calculator.cache)
        self.assertNotIn((sheet, 1, 3), calculator.cache)
        self.assertEqual({ref: self.outcome(calculator, book, ref) for ref in ("A3", "C1")}, {"A3": "#DIV/0!", "C1": "#DIV/0!"})

    def test_first_error_in_row_then_column_order(self):
        # Row order meets B1 (#DIV/0!) before A2 (#REF!); column order would meet A2 first.
        cells = {"A1": "1", "B1": "=1/0", "A2": "=Nowhere!A1", "B2": "2"}
        expected = {"COUNT": 2, "COUNTA": 4}
        for name in FOLDED:
            for precalculated in (False, True):
                with self.subTest(function=name, precalculated=precalculated):
                    book = self.book({**cells, "E1": f"={name}(A1:B2)"})
                    calculator = self.calculator(book)
                    if precalculated:
                        for ref in ("A2", "B1"):
                            self.outcome(calculator, book, ref)
                    self.assertEqual(self.outcome(calculator, book, "E1"), expected.get(name, "#DIV/0!"))

    def test_cell_error_before_overflow(self):
        # The function's own overflow: numbers past the finite range, or text past 32,767 characters.
        big, long = "1e308", "x"*20_000
        for name, inputs in (("SUM", [big, big]), ("AVERAGE", [big, big]), ("CONCAT", [long, long])):
            with self.subTest(function=name, overflow="own"):
                book = self.book({**column("A", ["=1/0", *inputs]), "E1": f"={name}(A1:A3)"})
                self.assertEqual(self.outcome(self.calculator(book), book, "E1"), "#DIV/0!")
        # SUM reads its whole range before reporting its own overflow; AVERAGE reports it where it happens.
        for name, expected in (("SUM", "#DIV/0!"), ("AVERAGE", "#NUM!")):
            with self.subTest(function=name, overflow="own", error="after"):
                book = self.book({**column("A", [big, big, "=1/0"]), "E1": f"={name}(A1:A3)"})
                self.assertEqual(self.outcome(self.calculator(book), book, "E1"), expected)
        # The per-formula limit: functions that stop at a cell error report it; COUNT and COUNTA go on to the limit.
        for name in FOLDED:
            with self.subTest(function=name, overflow="limit"):
                book = self.book({**column("A", ["=1/0"] + ["1"]*200), "E1": f"={name}(A1:A201)"})
                with patch.object(formula, "FORMULA_CELL_LIMIT", 100):
                    result = self.outcome(self.calculator(book), book, "E1")
                self.assertEqual(result, "#LIMIT!" if name in ("COUNT", "COUNTA") else "#DIV/0!")

    def test_cycle_and_limit_from_a_cell_pass_through_functions_that_tolerate_cell_errors(self):
        for name in ("COUNT", "COUNTA"):
            with self.subTest(function=name, error="#CYCLE!"):
                book = self.book({"A1": "1", "A2": "=E1", "E1": f"={name}(A1:A3)"})
                self.assertEqual(self.outcome(self.calculator(book), book, "E1"), "#CYCLE!")
            with self.subTest(function=name, error="#LIMIT!"):
                book = self.book({"A1": "1", "A2": "=SUM(Z1:Z200)", **column("Z", ["1"]*200), "E1": f"={name}(A1:A3)"})
                with patch.object(formula, "FORMULA_CELL_LIMIT", 100):
                    self.assertEqual(self.outcome(self.calculator(book), book, "E1"), "#LIMIT!")

    def test_order_of_addition(self):
        book = self.book({**column("A", ["1e16", "-1e16", "1"]), "E1": "=SUM(A1:A3)"})
        book.db.execute("PRAGMA reverse_unordered_selects=ON")
        self.assertEqual(self.outcome(self.calculator(book), book, "E1"), 1)
        self.assertEqual([row for row, _, _ in book.scan(book.sheet(S), 1, 1, 3, 1)], [1, 2, 3])

    def test_streamed_cells_are_not_cached(self):
        book = self.book({**column("A", ["1"]*20_000), "C1": "=SUM(A1:A20000)"})
        calculator = self.calculator(book)
        self.assertEqual(self.outcome(calculator, book, "C1"), 20_000)
        self.assertLess(len(calculator.cache), 10)

    def test_a_hook_calculator_sees_uncommitted_cells_in_a_range(self):
        book = self.book({"A1": "1", "C1": "=SUM(A1:A3)"})
        sheet = book.sheet(S)
        book.db.execute("BEGIN")
        try:
            book.db.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'auto')", [(sheet, 2, "10"), (sheet, 3, "100")])
            self.assertEqual(self.outcome(book.calculator(transactional=True), book, "C1"), 111)
        finally:
            book.db.execute("ROLLBACK")
        self.assertEqual(self.outcome(book.calculator(transactional=True), book, "C1"), 1)

    def test_the_limit_counts_stored_cells(self):
        for stored, expected in ((50, 50), (100, 100), (101, "#LIMIT!")):
            with self.subTest(stored=stored):
                book = self.book({**{f"A{r*9}": "1" for r in range(1, stored+1)}, "C1": "=SUM(A1:A1000)"})
                with patch.object(formula, "FORMULA_CELL_LIMIT", 100):
                    self.assertEqual(self.outcome(self.calculator(book), book, "C1"), expected)

    def test_textjoin_keeps_empty_coordinates_unless_told_to_ignore_them(self):
        book = self.book({"A1": "a", "A3": "c", "B2": "=A2", "E1": '=TEXTJOIN(",",FALSE,A1:A4)', "E2": '=TEXTJOIN(",",TRUE,A1:A4)',
                          "E3": '=TEXTJOIN("-",FALSE,A1:B2)', "E4": '=TEXTJOIN(",",FALSE,IF(TRUE,A1:A4,0))'})
        calculator = self.calculator(book)
        self.assertEqual({ref: self.outcome(calculator, book, ref) for ref in ("E1", "E2", "E3", "E4")},
                         {"E1": "a,,c,", "E2": "a,c", "E3": "a---", "E4": "a,,c,"})

    def test_a_range_chosen_by_a_function_is_still_folded(self):
        book = self.book({**column("A", ["1", "2", "x"]), "B1": "=1/0",
                          "E1": "=SUM(IF(TRUE,A1:A3,0))", "E2": "=COUNTA(IF(TRUE,A1:A3,0))", "E3": "=SUM(IF(TRUE,A1:B1,0))",
                          "E4": "=COUNT(IF(TRUE,A1:B1,0))", "E5": "=A1:A3", "E6": "=A1:B1"})
        calculator = self.calculator(book)
        self.assertEqual({ref: self.outcome(calculator, book, ref) for ref in ("E1", "E2", "E3", "E4", "E5", "E6")},
                         {"E1": 3, "E2": 3, "E3": "#DIV/0!", "E4": 0, "E5": "#VALUE!", "E6": "#DIV/0!"})

    def test_a_range_where_a_scalar_is_needed_builds_no_list(self):
        book = self.book({**column("A", ["1"]*20_000), "C1": "=A1:A20000", "C2": "=A1:A20000+1"})
        calculator = self.calculator(book)
        self.assertEqual((self.outcome(calculator, book, "C1"), self.outcome(calculator, book, "C2")), ("#VALUE!", "#VALUE!"))
        self.assertLess(len(calculator.cache), 10)

    def test_the_dependency_cache_does_not_expand_a_large_range(self):
        book = self.book({"A1": "1", "A1048576": "2", "C1": "=SUM(A1:A1048576)"})
        calculator, expanded = book.calculator(seconds=30), []
        range_targets = calculator.range_targets

        def counted(node, sheet):
            for target in range_targets(node, sheet):
                expanded.append(target)
                yield target
        calculator.range_targets = counted
        self.assertEqual(self.outcome(calculator, book, "C1"), 3)
        self.assertEqual(expanded, [])
        self.assertLess(book.dependencies.info()["edges"], 10)
        self.assertNotIn((book.sheet(S), 1, 3), book.dependencies.values)


if __name__ == "__main__":
    unittest.main()
