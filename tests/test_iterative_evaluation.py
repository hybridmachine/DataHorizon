# SPDX-License-Identifier: GPL-3.0-only
"""Calculator.cell evaluates a missing formula in place, by recursion, up to IN_PLACE_DEPTH nested cells, and
follows a deeper chain over an explicit stack (calculation stage 2, Task 2 and the redesign that removed speculation)."""
from contextlib import closing
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

from calculation_corpus import CASES, make
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


def random_cells(seed, rows=6, cols=4):
    """A small workbook of inputs and formulas over ranges, criteria, lookups, IF, IFERROR and whatever cycles arise."""
    R = random.Random(seed)
    ref = lambda: "ABCD"[R.randrange(cols)] + str(R.randint(1, rows))

    def block():
        r1, c1 = R.randint(1, rows), R.randrange(cols)
        return f"{'ABCD'[c1]}{r1}:{'ABCD'[R.randint(c1, cols-1)]}{R.randint(r1, rows)}"

    def vectors(count):
        height = R.randint(1, rows)
        top = R.randint(1, rows-height+1)
        return [f"{letter}{top}:{letter}{top+height-1}" for letter in (R.choice("ABCD") for _ in range(count))]
    criterion = lambda: R.choice(['">0"', '""', '"<>x"', '"x"', "2", '"<3"', ref()])
    value = lambda: R.choice(["1", "2", '"x"', ref()])

    def formula():
        kind = R.randrange(14)
        if kind == 0: return f"={ref()}"
        if kind == 1: return f"={ref()}+{ref()}"
        if kind == 2: return R.choice(["=1/0", "=2", '="x"', "=1"])
        if kind == 3: return f"=IF({ref()}>0,{ref()},{ref()})"
        if kind == 4: return f"=IFERROR({ref()},{ref()})"
        if kind in (5, 6): return f"={R.choice(['SUM', 'AVERAGE', 'MIN', 'COUNT', 'COUNTA', 'CONCAT'])}({block()})"
        if kind == 7: return f"=COUNTIF({block()},{criterion()})"
        if kind == 8: return "=SUMIF({0},{2},{1})".format(*vectors(2), criterion())
        if kind == 9: return "=SUMIFS({0},{1},{3},{2},{4})".format(*vectors(3), criterion(), criterion())
        if kind == 10: return "=COUNTIFS({0},{2},{1},{3})".format(*vectors(2), criterion(), criterion())
        if kind == 11: return "=XLOOKUP({2},{0},{1}{3})".format(*vectors(2), value(), R.choice(["", ",0"]))
        if kind == 12: return f"=MATCH({value()},{vectors(1)[0]},0)"
        return f'=TEXTJOIN(",",{R.choice(["TRUE", "FALSE"])},{block()})'
    cells = {}
    for row in range(1, rows+1):
        for letter in "ABCD"[:cols]:
            chance = R.random()
            if chance < 0.6:
                cells[f"{letter}{row}"] = formula()
            elif chance < 0.85:
                cells[f"{letter}{row}"] = R.choice(["1", "2", "0", "x", "3"])
    return cells


class InPlaceEvaluationTests(unittest.TestCase):
    """A missing formula is evaluated where it is read, as the evaluator before stage 2 did; only nesting deeper than
    formula.IN_PLACE_DEPTH (or than Python's recursion limit allows) goes over the explicit stack."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def book(self, cells):
        folder = Path(self.temp.name) / str(len(list(Path(self.temp.name).iterdir())))
        folder.mkdir()
        book = Workbook(make({"name": "case", "cells": cells}, folder))
        self.addCleanup(book.close)
        return book

    def counted(self, book):
        """A calculator, and per cell how many times its evaluation was begun, its result kept, and its input read."""
        calculator = book.calculator(seconds=30, transactional=True)
        evaluated, kept, read = {}, {}, {}
        evaluate_cell, remember, source = calculator._evaluate_cell, calculator.remember, calculator.read

        def counted_evaluation(key):
            evaluated[key] = evaluated.get(key, 0) + 1
            return evaluate_cell(key)

        def counted_remember(key, result):
            kept[key] = kept.get(key, 0) + 1
            return remember(key, result)

        def counted_read(*key):
            item = source(*key)
            read[key] = read.get(key, 0) + 1  # a read that completed: one that ran out of stack is not charged either
            if item and item["kind"] == "formula":
                formulas.add(key)
            return item
        formulas = self.formulas = set()  # of the calculator made last: what assert_bounded counts
        calculator._evaluate_cell, calculator.remember, calculator.read = counted_evaluation, counted_remember, counted_read
        return calculator, evaluated, kept, read

    @staticmethod
    def outcome(calculator, key, room=None):
        """The cell's value or error code; with room, read with only that many Python frames left to recurse into."""
        limit = sys.getrecursionlimit()
        if room:
            depth, frame = 0, sys._getframe()
            while frame:
                depth, frame = depth+1, frame.f_back
            sys.setrecursionlimit(depth+room)
        try:
            return calculator.cell(*key)
        except FormulaError as error:
            return error.code
        finally:
            sys.setrecursionlimit(limit)

    def assert_bounded(self, evaluated):
        """However deep the nesting and however little stack there is: a formula is evaluated in place at most once
        (it completes, or unwinds and is pushed on the stack, once); a pushed formula completes there once; and each
        other evaluation on the stack ended by pushing at least one cell not pushed before. So formulas are evaluated
        at most three times each in all, plus once for each input cell that was itself pushed (the stack ran out
        while it was being read: it is then evaluated twice). Input cells are not counted among the evaluations."""
        formulas = sum(count for key, count in evaluated.items() if key in self.formulas)
        pushed_inputs = sum(1 for key, count in evaluated.items() if key not in self.formulas and count > 1)
        self.assertLessEqual(formulas, 3*len(self.formulas) + pushed_inputs, (formulas, len(self.formulas), pushed_inputs))

    def read_all(self, book, cells, order=None, room=None):
        """Every formula cell read through one calculator: (results by key, evaluations, kept and reads by key)."""
        keys = sorted((book.sheet(sheet), row, col) for (sheet, row, col), source in cells.items()
                      if not isinstance(source, tuple) and source.startswith("="))
        if order:
            order.shuffle(keys)
        calculator, evaluated, kept, read = self.counted(book)
        results = {key: self.outcome(calculator, key, room) for key in keys}
        self.assertEqual((calculator.following, calculator.active, calculator.waiting), ([], set(), {}))
        return results, evaluated, kept, read

    def test_within_the_depth_every_corpus_formula_is_evaluated_once(self):
        for case in CASES:
            with self.subTest(case=case["name"]):
                book = self.book(case["cells"])
                _, evaluated, kept, read = self.read_all(book, case["cells"])
                self.assertEqual({key: count for key, count in evaluated.items() if count != 1}, {})
                self.assertEqual((kept, read), (evaluated, evaluated))
                book.close()

    def test_within_the_depth_every_formula_of_a_random_workbook_is_evaluated_once_in_any_read_order(self):
        for seed in range(60):
            with self.subTest(seed=seed):
                cells = shaped(random_cells(seed))
                book = self.book(cells)
                results, evaluated, kept, read = self.read_all(book, cells)
                self.assertEqual({key: count for key, count in evaluated.items() if count != 1}, {})
                self.assertEqual((kept, read), (evaluated, evaluated))
                for attempt in range(2):
                    shuffled, evaluated, _, _ = self.read_all(book, cells, random.Random(seed*7+attempt))
                    self.assertEqual(shuffled, results)
                    self.assertEqual({key: count for key, count in evaluated.items() if count != 1}, {})
                # With little stack the same cells go over the explicit stack: same results, bounded work.
                for room in (70, 110):
                    cramped, evaluated, kept, _ = self.read_all(book, cells, room=room)
                    self.assertEqual(cramped, results, room)
                    self.assertEqual(set(kept.values()), {1})
                    self.assert_bounded(evaluated)
                book.close()

    NESTED = {  # how A<r> reads A<r-1> (column B holds "x" beside every row), and what A<rows> is when A1 is =1
        "a reference": ("=A{0}+1", lambda rows: rows),
        "SUM of a one-cell range": ("=SUM(A{0}:A{0})+1", lambda rows: rows),
        "AVERAGE": ("=AVERAGE(A{0}:A{0})+1", lambda rows: rows),
        "SUMIFS, the sum cell": ('=SUMIFS(A{0}:A{0},B{0}:B{0},"x")+1', lambda rows: rows),
        "COUNTIF, the criteria cell": ('=COUNTIF(A{0}:A{0},">0")+A{0}', lambda rows: rows),
        "XLOOKUP, the return cell": ('=XLOOKUP("x",B{0}:B{0},A{0}:A{0})+1', lambda rows: rows),
        "MATCH, the lookup cell": ("=MATCH(A{0},A{0}:A{0},0)+A{0}", lambda rows: rows),
        "TEXTJOIN": ('=TEXTJOIN("",TRUE,A{0}:A{0})&"1"', lambda rows: "1"*rows),
    }

    def nested(self, source, rows):
        cells = {"A1": "=1", **{f"A{r}": source.format(r-1) for r in range(2, rows+1)}, **{f"B{r}": "x" for r in range(1, rows+1)}}
        return self.book(shaped(cells))

    def test_128_cells_nested_through_ranges_criteria_and_lookups_give_values(self):
        # What the evaluator before stage 2 could nest (128 cells) still evaluates, however many Python frames one
        # nested cell takes; so does the same nesting past the depth.
        for name, (source, expected) in self.NESTED.items():
            for rows in (128, 129, 700):
                with self.subTest(through=name, rows=rows):
                    book = self.nested(source, rows)
                    calculator, evaluated, kept, read = self.counted(book)
                    sheet = book.sheet(S)
                    self.assertEqual(self.outcome(calculator, (sheet, rows, 1)), expected(rows))
                    self.assertEqual(set(kept.values()), {1})  # each result is kept once, in place or on the stack
                    self.assertEqual({count for key, count in read.items() if key[2] == 1}, {1})
                    self.assert_bounded(evaluated)
                    book.close()

    def test_nesting_deeper_than_the_recursion_limit_allows_goes_over_the_stack(self):
        # With little room on the Python stack no chain reaches IN_PLACE_DEPTH: the RecursionError met in a nested
        # cell hands that cell, and those waiting for it, to the explicit stack instead of becoming its result.
        for name, (source, expected) in self.NESTED.items():
            with self.subTest(through=name):
                book = self.nested(source, 400)
                calculator, evaluated, kept, _ = self.counted(book)
                self.assertEqual(self.outcome(calculator, (book.sheet(S), 400, 1), room=150), expected(400))
                self.assertEqual(set(kept.values()), {1})
                self.assertGreater(max(evaluated.values()), 1)  # some cell did unwind and was evaluated again
                self.assert_bounded(evaluated)
                book.close()

    def test_one_formula_too_deep_for_the_stack_is_a_limit_error_wherever_it_is_read_from(self):
        book = self.book(shaped({"A1": "=" + "+"*128 + "B1", "B1": "1", "A2": "=A1+1", "A3": "=SUM(A1:A2)", "A4": "=B1+1"}))
        for first in (1, 2, 3):
            with self.subTest(first=first):
                calculator, sheet = book.calculator(seconds=30, transactional=True), book.sheet(S)
                self.assertEqual(self.outcome(calculator, (sheet, first, 1), room=100), "#LIMIT!")
                self.assertEqual(str(calculator.cache[sheet, 1, 1]), "Calculation nesting limit exceeded")
                self.assertEqual([self.outcome(calculator, (sheet, row, 1), room=100) for row in (1, 2, 3, 4)],
                                 ["#LIMIT!", "#LIMIT!", "#LIMIT!", 2])

    BUSHY = {  # how A<r> reads A<r+1>; A<depth> sums 1,500 formula cells. Each runs out of Python stack at another depth.
        "AVERAGE": ("=AVERAGE(A{0}:A{0})+1", lambda depth, cells: cells+depth-1),
        "MATCH": ("=MATCH(A{0},A{0}:A{0},0)+A{0}", lambda depth, cells: cells+depth-1),
        "TEXTJOIN": ('=TEXTJOIN("",TRUE,A{0}:A{0})', lambda depth, cells: str(cells)),
        "a reference": ("=A{0}+1", lambda depth, cells: cells+depth-1),
    }

    def bushy(self, source, depth, cells):
        return self.book(shaped({**{f"A{r}": source.format(r+1) for r in range(1, depth)}, f"A{depth}": f"=SUM(C1:C{cells})",
                                 **{f"C{j}": "=1" for j in range(1, cells+1)}}))

    def test_many_formulas_read_where_the_stack_runs_out_are_not_each_paid_for_with_the_path_above_them(self):
        # 124 nested cells, which the evaluator before stage 2 evaluated, the innermost summing 1,500 formulas. If
        # Python's stack runs out exactly there, each of the 1,500 is refused in turn; were the path above them not
        # kept on the explicit stack, each refusal would walk it again (168,110 evaluations, and #LIMIT! from an
        # ordinary read).
        depth, cells = 124, 1_500
        for name, (source, expected) in self.BUSHY.items():
            with self.subTest(through=name):
                book = self.bushy(source, depth, cells)
                calculator, evaluated, kept, _ = self.counted(book)
                self.assertEqual(self.outcome(calculator, (book.sheet(S), 1, 1)), expected(depth, cells))
                self.assertEqual((len(kept), set(kept.values())), (depth+cells, {1}))
                self.assertLessEqual(sum(evaluated.values()), cells + 3*depth)  # the path may unwind; the 1,500 need not
                book.close()
        # Where the stack runs out depends on the caller, and the window is a few frames wide: a smaller workbook is
        # read with every amount of room, so that some read meets it for each function.
        depth, cells = 30, 200
        for name, (source, expected) in self.BUSHY.items():
            book, refused = self.bushy(source, depth, cells), 0
            sheet = book.sheet(S)
            for room in range(40, 400):
                calculator, evaluated, kept, _ = self.counted(book)
                result = self.outcome(calculator, (sheet, 1, 1), room)
                if result == "#LIMIT!":  # not even A1's own expression fits
                    continue
                with self.subTest(through=name, room=room):
                    self.assertEqual(result, expected(depth, cells))
                    self.assertEqual((len(kept), set(kept.values())), (depth+cells, {1}))
                    self.assert_bounded(evaluated)
                    self.assertLessEqual(sum(evaluated.values()), 2*cells + 3*depth)
                refused += max(count for key, count in evaluated.items() if key[2] == 3) > 1
            self.assertGreater(refused, 0, name)  # some read did run out of stack at the 200
            book.close()

    def test_a_cycle_that_closes_beyond_the_depth_is_a_cycle_whichever_cell_is_read_first(self):
        ring = 300
        cells = {**{f"A{r}": f"=A{r+1}+1" for r in range(1, ring)}, f"A{ring}": "=A1", "B1": "=A150*2", "B2": "=SUM(A1:A300)",
                 "B3": "=IFERROR(A7,0)",
                 # a tail that enters a ring far along it: D1 to D200, then the ring D200 to D420 back to D200
                 **{f"D{r}": f"=D{r+1}+1" for r in range(1, 420)}, "D420": "=D200"}
        book = self.book(shaped(cells))
        sheet = book.sheet(S)
        for first in ("A1", "A150", "A300", "B1", "B2", "B3", "D1", "D199", "D200", "D300", "D420"):
            with self.subTest(first=first):
                calculator = book.calculator(seconds=30, transactional=True)
                (_, row, col), = shaped({first: None})
                self.assertEqual(self.outcome(calculator, (sheet, row, col), room=150 if first in ("A150", "D1") else None), "#CYCLE!")
                self.assertEqual((calculator.following, calculator.active, calculator.waiting), ([], set(), {}))
                self.assertEqual({self.outcome(calculator, (sheet, r, 1)) for r in range(1, ring+1)}, {"#CYCLE!"})
                self.assertEqual({self.outcome(calculator, (sheet, r, 4)) for r in range(1, 421)}, {"#CYCLE!"})
                self.assertEqual([self.outcome(calculator, (sheet, r, 2)) for r in (1, 2, 3)], ["#CYCLE!"]*3)

    def test_a_branch_back_to_a_cell_that_is_waiting_is_a_cycle_only_if_it_is_taken(self):
        # A1 waits, on the explicit stack once the chain is deeper than is evaluated in place, while A<n> is
        # evaluated. A<n> names A1 in a branch: untaken, no cell is in a cycle; taken, every cell is. The same at
        # 100 rows, wholly in place, where the evaluator before stage 2 gives the same answers.
        for rows in (100, 129, 400):
            for room in (None, 150):
                with self.subTest(rows=rows, room=room, branch="untaken"):
                    book = self.book(shaped({**{f"A{r}": f"=A{r+1}+1" for r in range(1, rows)}, f"A{rows}": "=IF(FALSE,A1,42)",
                                             "B1": f"=SUM(A1:A{rows})"}))
                    sheet = book.sheet(S)
                    for first in (1, rows//2, rows):
                        calculator, evaluated, _, _ = self.counted(book)
                        self.assertEqual(self.outcome(calculator, (sheet, first, 1), room), 42+rows-first)
                        self.assertEqual([self.outcome(calculator, (sheet, row, 1), room) for row in (1, rows//2, rows)],
                                         [42+rows-1, 42+rows-rows//2, 42])
                        self.assert_bounded(evaluated)
                    calculator = book.calculator(seconds=30, transactional=True)
                    self.assertEqual(self.outcome(calculator, (sheet, 1, 2), room), sum(42+rows-r for r in range(1, rows+1)))
                    book.close()
                with self.subTest(rows=rows, room=room, branch="taken"):
                    book = self.book(shaped({**{f"A{r}": f"=A{r+1}+1" for r in range(1, rows)}, f"A{rows}": "=IF(B2,A1,42)", "B2": "=1",
                                             "B1": f"=SUM(A1:A{rows})"}))
                    sheet = book.sheet(S)
                    for first in ((1, 1), (rows//2, 1), (rows, 1), (1, 2)):
                        calculator = book.calculator(seconds=30, transactional=True)
                        self.assertEqual(self.outcome(calculator, (sheet, *first), room), "#CYCLE!")
                        self.assertEqual({self.outcome(calculator, (sheet, row, 1), room) for row in range(1, rows+1)}, {"#CYCLE!"})
                        self.assertEqual(self.outcome(calculator, (sheet, 2, 2), room), 1)
                    book.close()

    def test_a_deep_chain_reads_charges_and_keeps_each_cell_once(self):
        rows = 1_000
        cells = shaped({"A1": "=1", **{f"A{r}": f"=A{r-1}+B{r}" for r in range(2, rows+1)}, **{f"B{r}": "1" for r in range(2, rows+1)}})
        book = self.book(cells)
        calculator, evaluated, kept, read = self.counted(book)
        self.assertEqual(self.outcome(calculator, (book.sheet(S), rows, 1)), rows)
        self.assertEqual(calculator.bytes, sum(len(source.encode()) for source in cells.values()))
        self.assertEqual((set(read.values()), set(kept.values()), len(kept)), ({1}, {1}, 2*rows-1))
        # A formula is evaluated once in place, where it unwinds, and once on the stack; the one refused for depth
        # in every IN_PLACE_DEPTH, a third time.
        formulas = sum(count for key, count in evaluated.items() if key[2] == 1)
        self.assertLessEqual(formulas, 2*rows + rows//formula.IN_PLACE_DEPTH + 2, formulas)
        self.assertEqual({count for key, count in evaluated.items() if key[2] == 2}, {1})  # the inputs
        self.assert_bounded(evaluated)

    def test_within_the_old_depth_but_beyond_the_python_stack_a_formula_can_be_evaluated_twice(self):
        # 112 cells nested through AVERAGE, read through a total: the evaluator before stage 2 evaluates each formula
        # once (113 evaluations). AVERAGE takes 8 Python frames a cell, so with an ordinary recursion limit the stack
        # can run out before IN_PLACE_DEPTH: the cells that unwind are evaluated once more on the explicit stack
        # (224 evaluations from a short call stack). Accepted: the value is the same and the work stays inside the
        # aggregate bound (3 per formula, less 2), which is what is pinned here, not the exact count.
        rows = 112
        book = self.book(shaped({**{f"A{r}": f"=AVERAGE(A{r+1}:A{r+1})+1" for r in range(1, rows)}, f"A{rows}": "=1",
                                 "B1": f"=SUM(A1:A{rows})"}))
        calculator, evaluated, kept, _ = self.counted(book)
        self.assertEqual(self.outcome(calculator, (book.sheet(S), 1, 2)), rows*(rows+1)//2)
        self.assertEqual((len(kept), set(kept.values())), (rows+1, {1}))
        self.assertLessEqual(sum(evaluated.values()), 3*(rows+1) - 2, sum(evaluated.values()))
        self.assertLessEqual(max(evaluated.values()), 3)

    def test_known_cost_a_total_over_many_deep_rows_is_evaluated_and_rescanned_once_for_each_row(self):
        # NOT A GUARANTEE: a known cost of the fallback, pinned so that a change in either direction is noticed.
        # Each row is a chain 130 cells long to the right, deeper than is evaluated in place, and the total sums the
        # first cell of every row. Each row's chain unwinds the total once, and the total, evaluated again
        # from its start, scans its range again up to that row: rows+1 evaluations of the total and
        # rows*(rows+1)/2 + rows stored cells scanned, which grows with rows x rows (125,750 at 500 rows, 501,500 at
        # 1,000). The evaluator before stage 2 cannot evaluate this shape at all (#LIMIT! past 128 cells).
        rows, links = 150, 130

        def letters(col):
            name = ""
            while col:
                col, digit = divmod(col-1, 26)
                name = chr(65+digit) + name
            return name
        cells = {f"{letters(col)}{r}": f"={letters(col+1)}{r}+1" for r in range(1, rows+1) for col in range(1, links)}
        cells.update({f"{letters(links)}{r}": "=1" for r in range(1, rows+1)})
        cells["A1000"] = f"=SUM(A1:A{rows})"
        book = self.book(shaped(cells))
        calculator, evaluated, kept, _ = self.counted(book)
        scans, scanned, scan, sheet = [], [0], calculator.scan, book.sheet(S)

        def counted_scan(*rectangle):
            scans.append(rectangle)
            for found in scan(*rectangle):
                scanned[0] += 1
                yield found
        calculator.scan = counted_scan
        self.assertEqual(self.outcome(calculator, (sheet, 1000, 1)), rows*links)
        self.assertEqual(set(kept.values()), {1})
        self.assertEqual(evaluated[sheet, 1000, 1], rows + 1)
        self.assertEqual((len(scans), scanned[0]), (rows + 1, rows*(rows+1)//2 + rows))
        self.assert_bounded(evaluated)  # the formulas themselves stay within three evaluations each

    def test_a_chain_read_through_a_range_against_its_direction(self):
        # B1 reads A1 first, and A1 needs the whole column beneath it: in-place evaluation runs out of depth every
        # IN_PLACE_DEPTH cells and what unwound waits on the stack. B1 itself unwinds once, so its range is scanned twice.
        rows = 2_000
        book = self.book(shaped({**{f"A{r}": f"=A{r+1}+1" for r in range(1, rows)}, f"A{rows}": "=1", "B1": f"=SUM(A1:A{rows})"}))
        calculator, evaluated, kept, _ = self.counted(book)
        scans, scan, sheet = [], calculator.scan, book.sheet(S)
        calculator.scan = lambda *rectangle: scans.append(rectangle) or scan(*rectangle)
        self.assertEqual(self.outcome(calculator, (sheet, 1, 2)), rows*(rows+1)//2)
        self.assertEqual(set(kept.values()), {1})
        self.assertLessEqual(evaluated[sheet, 1, 2], 2)
        # A cell is evaluated where it completes and once before, when it unwound; one in every IN_PLACE_DEPTH is
        # also the cell that was refused for depth, which counts as a third attempt.
        self.assertLessEqual(max(evaluated.values()), 3)
        self.assertLessEqual(sum(evaluated.values()), 2*rows + rows//formula.IN_PLACE_DEPTH + 2, sum(evaluated.values()))
        self.assertLessEqual(len(scans), 2 + rows//formula.SCAN_ROWS + 1, scans)  # the first band twice, the others once


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

    def test_a_formula_with_missing_precedents_is_parsed_and_evaluated_once(self):
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
        self.assertEqual(evaluated.count(reader), 1)
        self.assertEqual(len(evaluated), 4)
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
