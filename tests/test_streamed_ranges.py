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


class RangeCase(unittest.TestCase):
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

    @staticmethod
    def passes(scans, sheet, col):
        """How many times a range in column col starting at row 1 was scanned from its start: once per pass."""
        return sum(1 for scan in scans if scan[:3] == (sheet, 1, col) and scan[4] == col)


class StreamedRangeTests(RangeCase):
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

    def test_the_dependency_cache_records_a_large_range_without_expanding_it(self):
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
        self.assertIn((book.sheet(S), 1, 3), book.dependencies.values)  # one rectangle (Task 6), so the reader is cached


class StreamedCriteriaAndLookupTests(RangeCase):
    """COUNTIF, SUMIF, COUNTIFS, SUMIFS, XLOOKUP and MATCH read their ranges as aligned scans (Task 5)."""
    ROWS = 20_000

    def counted(self, book):
        """A calculator whose calls to read, scan and _evaluate_cell are recorded in self.reads, .scans, .evaluations."""
        calculator = self.calculator(book)
        self.reads, self.scans, self.evaluations = [], [], []
        read, scan, evaluate_cell = calculator.read, calculator.scan, calculator._evaluate_cell
        calculator.read = lambda *key: self.reads.append(key) or read(*key)
        calculator.scan = lambda *rectangle: self.scans.append(rectangle) or scan(*rectangle)
        calculator._evaluate_cell = lambda key: self.evaluations.append(key) or evaluate_cell(key)
        return calculator

    def results(self, cells, refs, *, scan=True):
        book = self.book(cells)
        calculator = self.counted(book)
        if not scan:
            calculator.scan = None
        return {ref: self.outcome(calculator, book, ref) for ref in refs}

    def test_criteria_functions_over_20000_rows(self):
        rows = self.ROWS
        cells = {**column("A", [("x", "y")[n % 2] for n in range(rows)]), **column("B", [str(n % 7) for n in range(rows)]),
                 **column("C", [str(n % 3) for n in range(rows)]),
                 "E1": f'=COUNTIF(A1:A{rows},"x")', "E2": f'=SUMIF(A1:A{rows},"y",B1:B{rows})', "E3": f'=SUMIF(B1:B{rows},">3")',
                 "E4": f'=COUNTIFS(A1:A{rows},"x")', "E5": f'=SUMIFS(B1:B{rows},A1:A{rows},"y")',
                 "E6": f'=COUNTIFS(A1:A{rows},"x",B1:B{rows},">2",C1:C{rows},1)',
                 "E7": f'=SUMIFS(B1:B{rows},A1:A{rows},"y",B1:B{rows},">2",C1:C{rows},"<>0")'}
        a, b, c = [("x", "y")[n % 2] for n in range(rows)], [n % 7 for n in range(rows)], [n % 3 for n in range(rows)]
        expected = {"E1": a.count("x"), "E2": sum(b[n] for n in range(rows) if a[n] == "y"), "E3": sum(n for n in b if n > 3),
                    "E4": a.count("x"), "E5": sum(b[n] for n in range(rows) if a[n] == "y"),
                    "E6": sum(1 for n in range(rows) if a[n] == "x" and b[n] > 2 and c[n] == 1),
                    "E7": sum(b[n] for n in range(rows) if a[n] == "y" and b[n] > 2 and c[n] != 0)}
        book = self.book(cells)
        for ref, value in expected.items():
            with self.subTest(formula=ref):
                calculator = self.counted(book)
                self.assertEqual(self.outcome(calculator, book, ref), value)
                self.assertEqual(len(self.reads), 1)  # the formula itself: its ranges are scanned

    def test_blank_matching_criteria_count_the_empty_positions_from_the_area(self):
        rows = self.ROWS
        stored = {f"A{r}": ("x" if r == 500 else "a") for r in (1, 2, 3, 500, 4000, 9000, 12000, 15000, 19000, rows)}
        book = self.book({**stored, "E1": f'=COUNTIF(A1:A{rows},"")', "E2": f'=COUNTIF(A1:A{rows},"<>x")',
                          "E3": f'=COUNTIFS(A1:A{rows},"<>",B1:B{rows},"")', "E4": f'=COUNTIF(A1:A{rows},"=")'})
        matches = formula.Criterion.matches
        for ref, value in (("E1", rows-10), ("E2", rows-1), ("E3", 10), ("E4", rows-10)):
            with self.subTest(formula=ref):
                calls = []
                calculator = self.counted(book)
                with patch.object(formula.Criterion, "matches", lambda criterion, left: calls.append(left) or matches(criterion, left)):
                    self.assertEqual(self.outcome(calculator, book, ref), value)
                self.assertEqual(len(self.reads), 1)
                self.assertLessEqual(len(calls), 2*10+2)  # the stored cells and the empty positions once

    def test_an_empty_criteria_cell_beside_a_stored_sum_cell(self):
        rows = self.ROWS
        cells = {"B1": "10", "B700": "20", "A9000": "1", f"B{rows}": "5",
                 "E1": f"=SUMIFS(B1:B{rows},A1:A{rows},D1)", "E2": f'=SUMIFS(B1:B{rows},A1:A{rows},"")',
                 "E3": f"=SUMIF(A1:A{rows},D1,B1:B{rows})", "E4": f'=SUMIFS(B1:B{rows},A1:A{rows},"<>")',
                 "E5": f"=SUMIFS(B1:B{rows},A1:A{rows},1)", "E6": f"=COUNTIFS(B1:B{rows},\"\",A1:A{rows},1)"}
        self.assertEqual(self.results(cells, ["E1", "E2", "E3", "E4", "E5", "E6"]), {"E1": 35, "E2": 35, "E3": 35, "E4": 0, "E5": 0, "E6": 1})
        self.assertLessEqual(len(self.reads), 7)  # the formulas and D1

    def test_lookups_over_20000_rows(self):
        rows = self.ROWS
        keys = column("A", [f"k{n}" for n in range(1, rows+1)])
        returns = column("B", [str(n) for n in range(1, rows+1)])
        cells = {**keys, **returns, "E1": f'=XLOOKUP("k{rows}",A1:A{rows},B1:B{rows})', "E2": f'=MATCH("k{rows}",A1:A{rows},0)',
                 "E3": f'=XLOOKUP("none",A1:A{rows},B1:B{rows})', "E4": f'=MATCH("none",A1:A{rows},0)',
                 "E5": f'=XLOOKUP("none",A1:A{rows},B1:B{rows},"fallback")'}
        book = self.book(cells)
        for ref, value in (("E1", rows), ("E2", rows), ("E3", "#N/A"), ("E4", "#N/A"), ("E5", "fallback")):
            with self.subTest(formula=ref):
                calculator = self.counted(book)
                self.assertEqual(self.outcome(calculator, book, ref), value)
                self.assertEqual(len(self.reads), 1)

    def test_a_blank_lookup_key_matches_the_first_gap(self):
        rows = self.ROWS
        returns = column("B", [str(n) for n in range(1, rows+1)])
        cases = {"gap inside": ({**column("A", ["a"]*1500), "A1502": "b", **returns}, 1501, 1501),
                 "gap before the first stored cell": ({"A3000": "a", **returns}, 1, 1),
                 "gap where the return range is empty": ({**column("A", ["a"]*7000), "B7002": "9"}, 0, 7001),
                 "no stored cell": ({}, 0, 1),
                 "no gap": (column("A", ["a"]*rows), "#N/A", "#N/A")}
        for label, (cells, value, position) in cases.items():
            for key in ("Z1", '""'):
                with self.subTest(case=label, key=key):
                    found = self.results({**cells, "E1": f"=XLOOKUP({key},A1:A{rows},B1:B{rows})", "E2": f"=MATCH({key},A1:A{rows},0)"},
                                         ["E1", "E2"])
                    # Text "" is not blank: it matches no empty coordinate.
                    self.assertEqual(found, {"E1": value, "E2": position} if key == "Z1" else {"E1": "#N/A", "E2": "#N/A"})
                    self.assertLessEqual(len(self.reads), 3)  # the formulas and Z1

    def test_a_lookup_stops_scanning_at_its_match(self):
        rows = 5*formula.SCAN_ROWS
        book = self.book({**column("A", [f"k{n}" for n in range(1, rows+1)]), "B9": "9", f"B{rows}": "last",
                          "E1": f'=XLOOKUP("k3",A1:A{rows},B1:B{rows})', "E2": f'=MATCH("k3",A1:A{rows},0)',
                          "E3": f"=XLOOKUP(Z1,C1:C{rows},B1:B{rows})", "E4": f"=MATCH(Z1,C1:C{rows},0)",
                          "E5": f"=XLOOKUP(Z1,C1:C{rows},D1:D{rows})"})
        sheet = book.sheet(S)
        for ref, value in (("E1", 0), ("E2", 3), ("E3", 0), ("E4", 1), ("E5", 0)):
            with self.subTest(formula=ref):
                calculator = self.counted(book)
                self.assertEqual(self.outcome(calculator, book, ref), value)
                self.assertTrue(self.scans)
                self.assertTrue(all(scan[1] == 1 for scan in self.scans if scan[0] == sheet), self.scans)  # the first band only

    def test_cell_errors_in_criteria_and_sum_ranges(self):
        rows = self.ROWS
        cells = {**column("A", ["x"]*rows), **column("B", ["1"]*rows), "A700": "=1/0", "B9000": "=Nowhere!A1",
                 "A9500": "y", "B9500": "=1/0",
                 "E1": f'=COUNTIF(A1:A{rows},"x")', "E2": f'=SUMIF(A1:A{rows},"x",B1:B{rows})',
                 "E3": f'=COUNTIFS(A1:A{rows},"<>y",B1:B{rows},">0")', "E4": f'=SUMIFS(B1:B{rows},A1:A{rows},"y")',
                 "E5": f'=SUMIFS(B1:B9499,A1:A9499,"<>y")', "E6": f'=SUMIFS(B1:B{rows},A1:A{rows},"x",B1:B{rows},"<>")'}
        found = self.results(cells, ["E1", "E2", "E3", "E4", "E5", "E6"])
        self.assertEqual(found, {"E1": rows-2, "E2": "#REF!", "E3": rows-3, "E4": "#DIV/0!", "E5": "#REF!", "E6": rows-3})

    def test_a_cycle_in_a_criteria_or_sum_range_raises(self):
        rows = self.ROWS
        cells = {**column("A", ["x"]*rows), **column("B", ["1"]*rows), "A9000": "y"}
        for ref, source in (("A600", f'=COUNTIF(A1:A{rows},"x")'), ("B600", f'=SUMIF(A1:A{rows},"x",B1:B{rows})'),
                            ("A601", f'=SUMIFS(B1:B{rows},A1:A{rows},"x")'), ("B602", f'=SUMIFS(B1:B{rows},A1:A{rows},"x")'),
                            ("B601", f'=COUNTIFS(A1:A{rows},"x",B1:B{rows},1)')):
            with self.subTest(formula=source, at=ref):
                self.assertEqual(self.results({**cells, ref: source}, [ref]), {ref: "#CYCLE!"})
        # The sum cell under a row that does not match is not read.
        self.assertEqual(self.results({**cells, "B9000": f'=SUMIF(A1:A{rows},"x",B1:B{rows})'}, ["B9000"]), {"B9000": rows-1})
        # A lookup reads its return range at its match only, and its lookup range up to it.
        self.assertEqual(self.results({**cells, "B9000": f'=XLOOKUP("x",A1:A{rows},B1:B{rows})'}, ["B9000"]), {"B9000": 1})
        self.assertEqual(self.results({**cells, "A9001": f'=MATCH("x",A1:A{rows},0)'}, ["A9001"]), {"A9001": 1})
        self.assertEqual(self.results({**cells, "B9000": f'=XLOOKUP("y",A1:A{rows},B1:B{rows})'}, ["B9000"]), {"B9000": "#CYCLE!"})
        self.assertEqual(self.results({**cells, "A9001": f'=MATCH("z",A1:A{rows},0)'}, ["A9001"]), {"A9001": "#CYCLE!"})

    def test_a_sum_cell_that_reads_its_reader_under_a_row_that_does_not_match(self):
        # The old evaluator never read B2: it is not evaluated on E1's behalf, where it would meet E1 on the stack.
        cells = {"A1": "x", "A2": "y", "B1": "=3", "B2": "=E1+1", "E1": '=SUMIF(A1:A2,"x",B1:B2)'}
        for first in ("E1", "B2"):
            with self.subTest(first=first):
                book = self.book(cells)
                calculator = self.calculator(book)
                second = "B2" if first == "E1" else "E1"
                self.assertEqual((self.outcome(calculator, book, first), self.outcome(calculator, book, second)),
                                 (3, 4) if first == "E1" else (4, 3))
        self.assertEqual(self.together(self.book(cells), ["E1", "B2"]), {"E1": 3, "B2": 4})

    def test_criteria_over_uncalculated_formulas_are_evaluated_a_constant_number_of_times(self):
        rows = 2_000
        cells = {**{f"A{r}": f'="{("y", "x")[r % 2 == 0]}"' for r in range(1, rows+1)}, **{f"B{r}": f"={r}*1" for r in range(1, rows+1)}}
        for source, value in ((f'=COUNTIF(A1:A{rows},"x")', rows//2), (f'=SUMIF(A1:A{rows},"x",B1:B{rows})', sum(range(2, rows+1, 2))),
                              (f'=COUNTIFS(A1:A{rows},"y",B1:B{rows},">10")', (rows-10)//2),
                              (f'=SUMIFS(B1:B{rows},A1:A{rows},"y",B1:B{rows},"<100")', sum(range(1, 100, 2)))):
            with self.subTest(formula=source):
                book = self.book({**cells, "E1": source})
                calculator = self.counted(book)
                self.assertEqual(self.outcome(calculator, book, "E1"), value)
                self.assertLessEqual(self.evaluations.count((book.sheet(S), 1, 5)), 3)
                self.assertLessEqual(len(self.evaluations), 2*rows+3)

    def test_lookups_over_uncalculated_formulas(self):
        rows = 2_000
        cells = {**{f"A{r}": f'="k"&{r}' for r in range(1, rows+1)}, **{f"B{r}": f"={r}*10" for r in range(1, rows+1)}}
        for source, value in ((f'=XLOOKUP("k{rows}",A1:A{rows},B1:B{rows})', rows*10), (f'=MATCH("k{rows}",A1:A{rows},0)', rows),
                              (f'=XLOOKUP("none",A1:A{rows},B1:B{rows},-1)', -1)):
            with self.subTest(formula=source):
                book = self.book({**cells, "E1": source})
                calculator = self.counted(book)
                self.assertEqual(self.outcome(calculator, book, "E1"), value)
                self.assertLessEqual(self.evaluations.count((book.sheet(S), 1, 5)), 3)
                self.assertLessEqual(len(self.evaluations), rows+4)  # one return cell, not the whole return range
        # A calculated match bounds the cells evaluated: none after it is evaluated on the lookup's behalf.
        book = self.book({**cells, "A50": "k50", "E1": f'=XLOOKUP("k50",A1:A{rows},B1:B{rows})'})
        calculator = self.counted(book)
        self.assertEqual(self.outcome(calculator, book, "E1"), 500)
        self.assertFalse(any(key[1] > 50 for key in self.evaluations), sorted(self.evaluations)[-3:])
        self.assertTrue(self.scans)
        self.assertTrue(all(scan[1] == 1 for scan in self.scans if scan[2] == 1), self.scans)

    def limited(self, cells, ref, limit, *, scan=True):
        with patch.object(formula, "FORMULA_CELL_LIMIT", limit):
            return self.results(cells, [ref], scan=scan)[ref]

    def test_the_limit_counts_only_the_cells_a_function_reads(self):
        sumifs = '=SUMIFS(B1:B40,A1:A40,"x")'
        # Uncalculated criteria and a matching sum error: each pass counts what it reads, not what it scans to collect.
        cells = {**{f"A{r}": '="x"' for r in range(1, 41)}, "B1": "=1/0", "E1": sumifs}
        # A1 (one cell and one text unit) and B1: what the old evaluator read before it raised.
        for limit, expected in ((2, "#LIMIT!"), (3, "#DIV/0!"), (60, "#DIV/0!"), (100, "#DIV/0!")):
            with self.subTest(case="error under a match", limit=limit):
                self.assertEqual(self.limited(cells, "E1", limit), expected)
        # No row matches: the 40 criteria cells are read (a cell and a text unit each), the sum cells never.
        for criteria in ("y", '="y"'):
            cells = {**column("A", [criteria]*40), **column("B", ["1"]*40), "E1": sumifs}
            for limit, expected in ((79, "#LIMIT!"), (80, 0), (81, 0), (100, 0)):
                for scan in (True, False):
                    with self.subTest(case="no match", criteria=criteria, limit=limit, scan=scan):
                        self.assertEqual(self.limited(cells, "E1", limit, scan=scan), expected)
        # A lookup counts its key's text unit, its vector up to the match (a cell and a text unit each) and the return
        # cell there; MATCH only the key and the vector. Nothing after the match counts.
        cells = {**column("A", [f"k{n}" for n in range(1, 1001)]), **column("B", ["7"]*1000),
                 "E1": '=XLOOKUP("k3",A1:A1000,B1:B1000)', "E2": '=MATCH("k3",A1:A1000,0)'}
        for ref, enough, value in (("E1", 8, 7), ("E2", 7, 3)):
            for scan in (True, False):
                with self.subTest(case="lookup", formula=ref, scan=scan):
                    self.assertEqual(self.limited(cells, ref, enough, scan=scan), value)
                    self.assertEqual(self.limited(cells, ref, enough-1, scan=scan), "#LIMIT!")

    def test_two_column_criteria_ranges_with_different_gaps(self):
        rows = 3_000
        first = {(5, 1): "x", (10, 2): "a", (2000, 1): "y", (2999, 2): "x", (3000, 1): "x"}  # A1:B3000
        second = {(5, 1): "q", (7, 2): "q", (10, 2): "x", (1500, 1): "z"}  # C1:D3000
        numbers = {(5, 1): 1, (7, 2): 2, (10, 2): 4, (11, 1): 8, (2999, 2): 16, (1, 1): 32}  # E1:F3000
        cells = {}
        for grid, letters in ((first, "AB"), (second, "CD"), (numbers, "EF")):
            cells.update({f"{letters[col-1]}{row}": str(value) for (row, col), value in grid.items()})
        cells.update({"H1": f'=COUNTIFS(A1:B{rows},"",C1:D{rows},"")', "H2": f'=COUNTIFS(A1:B{rows},"<>x",C1:D{rows},"")',
                      "H3": f'=SUMIFS(E1:F{rows},A1:B{rows},"",C1:D{rows},"<>x")', "H4": f'=COUNTIF(A1:B{rows},"")',
                      "H5": f'=COUNTIF(C1:D{rows},"<>x")', "H6": f'=SUMIF(C1:D{rows},"",E1:F{rows})'})
        positions = [(row, col) for row in range(1, rows+1) for col in (1, 2)]
        expected = {
            "H1": sum(1 for p in positions if p not in first and p not in second),
            "H2": sum(1 for p in positions if first.get(p) != "x" and p not in second),
            "H3": sum(numbers.get(p, 0) for p in positions if p not in first and second.get(p) != "x"),
            "H4": sum(1 for p in positions if p not in first),
            "H5": sum(1 for p in positions if second.get(p) != "x"),
            "H6": sum(numbers.get(p, 0) for p in positions if p not in second)}
        refs = list(expected)
        self.assertEqual(self.results(cells, refs), expected)
        self.assertEqual(self.results(cells, refs, scan=False), expected)

    def test_without_scan_the_functions_read_each_coordinate(self):
        cells = {"A1": "x", "A2": "=1/0", "A4": "x", "B1": "2", "B3": "=A1", "B4": "5",
                 "E1": '=COUNTIF(A1:A4,"x")', "E2": '=SUMIF(A1:A4,"x",B1:B4)', "E3": '=COUNTIFS(A1:A4,"",B1:B4,"<>")',
                 "E4": '=SUMIFS(A1:A4,B1:B4,"<>5")', "E5": '=XLOOKUP(D1,A1:A4,B1:B4)', "E6": '=MATCH("x",A4:A1,0)',
                 "E7": '=COUNTIF(A1:A9,"")', "E8": '=XLOOKUP("x",A1:A4,B1:B4)'}
        refs = [f"E{n}" for n in range(1, 9)]
        expected = {"E1": 2, "E2": 7, "E3": 1, "E4": "#DIV/0!", "E5": "#DIV/0!", "E6": 1, "E7": 6, "E8": 2}
        self.assertEqual(self.results(cells, refs), expected)
        self.assertEqual(self.results(cells, refs, scan=False), expected)


if __name__ == "__main__":
    unittest.main()
