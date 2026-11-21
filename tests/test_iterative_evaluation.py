# SPDX-License-Identifier: GPL-3.0-only
"""Calculator.cell follows a dependency chain with a loop over an explicit stack (calculation stage 2, Task 2)."""
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from calculation_corpus import make
from datahorizon import formula
from datahorizon.engine import Workbook
from datahorizon.formula import FormulaError

S = "Sheet1"


def shaped(cells):
    """{"A1": source} or {"Other!A1": source} as the corpus's (sheet, row, col) keys."""
    result = {}
    for ref, source in cells.items():
        sheet, _, ref = ref.rpartition("!") if "!" in ref else (S, "", ref)
        letters = ref.rstrip("0123456789")
        col = 0
        for ch in letters:
            col = col*26 + ord(ch)-64
        result[sheet, int(ref[len(letters):]), col] = source
    return result


class IterativeEvaluationTests(unittest.TestCase):
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
        sheet, row, col = next(iter(shaped({ref: None})))
        try:
            return calculator.cell(book.sheet(sheet), row, col)
        except FormulaError as error:
            return error.code

    def chain(self, rows):
        return self.book({"A1": "1", **{f"A{row}": f"=A{row-1}+1" for row in range(2, rows + 1)}})

    def test_a_chain_of_129_gives_its_value(self):
        book = self.chain(129)
        self.assertEqual(self.outcome(self.calculator(book), book, "A129"), 129)

    def test_a_chain_of_20000_gives_its_value_without_recursion(self):
        book = self.chain(20_000)
        calculator, limit = self.calculator(book), sys.getrecursionlimit()
        sys.setrecursionlimit(200)
        try:
            result = self.outcome(calculator, book, "A20000")
        finally:
            sys.setrecursionlimit(limit)
        self.assertEqual(result, 20_000)

    def test_a_formula_is_evaluated_at_most_once_per_missing_precedent(self):
        book = self.book({"A1": "=1", "B1": "=2", "C1": "=3", "D1": "=A1+B1+C1"})
        calculator, reader = self.calculator(book), (book.sheet(S), 1, 4)
        parse, evaluate_cell = formula.Parser.parse, calculator._evaluate_cell
        parsed, evaluated = [], []

        def counted_parse(parser):
            tree = parse(parser)
            parsed.append(tree)
            return tree

        def counted_evaluation(key):
            evaluated.append(key)
            return evaluate_cell(key)
        calculator._evaluate_cell = counted_evaluation
        with patch.object(formula.Parser, "parse", counted_parse):
            self.assertEqual(self.outcome(calculator, book, "D1"), 6)
        reader_tree = formula.Parser("A1+B1+C1").parse()
        self.assertEqual(parsed.count(reader_tree), 1)
        self.assertEqual(len(parsed), 4)
        self.assertLessEqual(evaluated.count(reader), 4)
        self.assertEqual(calculator.following, [])
        self.assertEqual(calculator.active, set())

    def test_cycles_are_decided_by_references_followed(self):
        cases = [
            ({"A1": "=A1"}, {"A1": "#CYCLE!"}),
            ({"A1": "=B1", "B1": "=A1"}, {"A1": "#CYCLE!", "B1": "#CYCLE!"}),
            ({"A1": "=IF(FALSE,A1,42)"}, {"A1": 42}),
            ({"A1": "=IF(TRUE,A1,1)"}, {"A1": "#CYCLE!"}),
            ({"A2": "=B2", "B2": "=A2", "C2": "=IFERROR(B2,5)"}, {"A2": "#CYCLE!", "B2": "#CYCLE!", "C2": "#CYCLE!"}),
            ({"A2": "=IFERROR(B2,5)", "B2": "=A2"}, {"A2": "#CYCLE!", "B2": "#CYCLE!"}),
            ({"A1": "1", "A2": "2", "A3": "=SUM(A1:A3)"}, {"A3": "#CYCLE!"}),
            ({"A1": "1", "A2": '=COUNTIF(A1:A3,">0")', "A3": "2"}, {"A2": "#CYCLE!"}),
            ({"B1": "=SUM(A1:A2)", "A1": "=1/0", "A2": "=B1"}, {"A1": "#DIV/0!", "B1": "#DIV/0!", "A2": "#DIV/0!"}),
            ({"A1": "=A1", "B1": "=A1+1", "C1": "=IFERROR(A1,9)", "D1": "=ISBLANK(A1)"},
             {"A1": "#CYCLE!", "B1": "#CYCLE!", "C1": "#CYCLE!", "D1": "#CYCLE!"}),
            ({"A1": "=B1", "B1": "=A1", "C1": "=B1*2", "D1": "=SUM(A1:B1)"},
             {"A1": "#CYCLE!", "B1": "#CYCLE!", "C1": "#CYCLE!", "D1": "#CYCLE!"}),
            ({"Other!A1": "=Sheet1!A1", "A1": "=Other!A1"}, {"Other!A1": "#CYCLE!", "A1": "#CYCLE!"}),
        ]
        for cells, expected in cases:
            book = self.book(cells)
            for ref, value in expected.items():
                with self.subTest(cells=cells, read=ref, alone=True):
                    self.assertEqual(self.outcome(self.calculator(book), book, ref), value)
            calculator = self.calculator(book)
            for ref, value in expected.items():
                with self.subTest(cells=cells, read=ref, alone=False):
                    self.assertEqual(self.outcome(calculator, book, ref), value)
            self.assertEqual((calculator.following, calculator.active), ([], set()))

    def test_an_error_in_one_precedent_does_not_evaluate_later_ones(self):
        book = self.book({"A1": "=1/0", "B1": "=C1", "C1": "=B1", "D1": "=A1+B1"})
        calculator = self.calculator(book)
        self.assertEqual(self.outcome(calculator, book, "D1"), "#DIV/0!")
        self.assertNotIn((book.sheet(S), 1, 2), calculator.cache)

    def test_every_formula_evaluated_on_the_way_reaches_the_dependency_cache(self):
        book = self.chain(200)
        calculator = book.calculator(seconds=30)
        self.assertEqual(self.outcome(calculator, book, "A200"), 200)
        sheet = book.sheet(S)
        self.assertEqual({row: book.dependencies.values.get((sheet, row, 1)) for row in (2, 100, 199, 200)},
                         {2: 2, 100: 100, 199: 199, 200: 200})

    def test_the_count_is_per_formula(self):
        cells = {"B1": "1", "B2": "2", "B3": "3", "A1": "=B1+B2+B3", "C1": "=A1+B1+B2+B3"}
        book = self.book(cells)
        with patch.object(formula, "FORMULA_CELL_LIMIT", 3):
            calculator = self.calculator(book)
            self.assertEqual(self.outcome(calculator, book, "A1"), 6)
            with self.assertRaises(FormulaError) as error:
                calculator.cell(book.sheet(S), 1, 3)
            self.assertEqual((error.exception.code, error.exception.args[1] if len(error.exception.args) > 1 else str(error.exception)),
                             ("#LIMIT!", "Formula reads more than 3 cells"))
            chain = self.book({"A1": "1", "B1": "1", **{f"A{row}": f"=A{row-1}+B1" for row in range(2, 11)}})
            self.assertEqual(self.outcome(self.calculator(chain), chain, "A10"), 10)

    def test_the_count_limit_message_uses_thousands_separators(self):
        book = self.book({"B1": "1", "A1": "=B1+B1"})
        with patch.object(formula, "FORMULA_CELL_LIMIT", 1):
            with self.assertRaises(FormulaError) as error:
                self.calculator(book).cell(book.sheet(S), 1, 1)
        self.assertIn("Formula reads more than 1 cells", str(error.exception))
        self.assertEqual(f"{formula.FORMULA_CELL_LIMIT:,}", "4,194,304")

    def test_the_count_limit_is_repeatable(self):
        book = self.book({"B1": "1", "B2": "2", "B3": "3", "A1": "=B1+B2+B3", "C1": "=A1+B1+B2+B3"})
        with patch.object(formula, "FORMULA_CELL_LIMIT", 3):
            results = [(self.outcome(self.calculator(book), book, "A1"), self.outcome(self.calculator(book), book, "C1"))
                       for _ in range(10)]
        self.assertEqual(set(results), {(6, "#LIMIT!")})

    def test_a_read_past_its_deadline_is_a_limit_error(self):
        book = self.book({"B1": "1", "A1": "=B1+1"})
        calculator = book.calculator(seconds=0, transactional=True)
        with self.assertRaises(FormulaError) as error:
            calculator.cell(book.sheet(S), 1, 1)
        self.assertEqual(error.exception.code, "#LIMIT!")
        self.assertIn("Calculation budget exceeded", str(error.exception))


if __name__ == "__main__":
    unittest.main()
