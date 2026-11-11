# SPDX-License-Identifier: GPL-3.0-only
import unittest

from datahorizon.formula_refs import Ref, RefRange, rewrite, scan

UNCHANGED = ['="see A1"', "='It''s [1] A1'!B2+1", '={"A1","B2"}', "=Table1[[#This Row],[A1]]", '=LEFT("A1:B2",2)&A1']


class FormulaRefsTest(unittest.TestCase):
    def test_text_that_looks_like_a_reference_is_never_rewritten(self):
        for source in UNCHANGED:
            with self.subTest(source):
                refs = scan(source)
                self.assertIsNotNone(refs)
                self.assertEqual(rewrite(source, lambda item: None), source)
        self.assertEqual(len(scan('="see A1"')), 0)
        self.assertEqual([r.sheet for r in scan("='It''s [1] A1'!B2+1")], ["It's [1] A1"])
        self.assertEqual(len(scan('=LEFT("A1:B2",2)&A1')), 1)

    def test_reference_forms(self):
        (r,) = scan("=VLOOKUP(X,$B:$C,2,0)")
        self.assertIsInstance(r, RefRange)
        self.assertEqual((r.first.kind, r.first.col, r.first.abs_col, r.last.col), ("column", 2, True, 3))
        (r,) = scan("=SUM(Jan:Mar!B2)")
        self.assertEqual((r.sheet, r.sheet_last), ("Jan", "Mar"))
        (r,) = scan("=[1]Data!A1")
        self.assertTrue(r.external)
        (r,) = scan("=A1#")
        self.assertEqual((r.row, r.col), (1, 1))

    def test_untokenizable(self):
        for source in ('="open', "=Table1[open", "=A1 ` B2", "={1,2"):
            with self.subTest(source):
                self.assertIsNone(scan(source))
        self.assertEqual(len(scan("=SUM(A1")), 1)

    def test_spans_and_absolute_flags(self):
        source = "=Sheet1!$B$2+a1"
        r, lower = scan(source)
        self.assertEqual((r.prefix_start, r.start, r.end), (1, 8, 12))
        self.assertEqual(source[r.start:r.end], "$B$2")
        self.assertEqual((r.sheet, r.sheet_last, r.external, r.kind), ("Sheet1", None, False, "cell"))
        self.assertEqual((r.row, r.col, r.abs_row, r.abs_col), (2, 2, True, True))
        self.assertEqual((lower.row, lower.col, lower.abs_row, lower.abs_col), (1, 1, False, False))
        self.assertEqual((lower.prefix_start, lower.start), (lower.start, 13))

    def test_ranges(self):
        (r,) = scan("=A1:B2")
        self.assertEqual((r.first.kind, r.last.kind, r.last.row, r.last.col), ("cell", "cell", 2, 2))
        (r,) = scan("=Sheet1!A1:B2")
        self.assertEqual((r.first.sheet, r.last.sheet), ("Sheet1", None))
        (r,) = scan("=Sheet1!A1:Sheet1!B2")
        self.assertEqual((r.first.sheet, r.last.sheet), ("Sheet1", "Sheet1"))
        (r,) = scan("=A1 : B2")
        self.assertIsInstance(r, RefRange)
        (r,) = scan("=Sheet1!$A:$C")
        self.assertEqual((r.first.sheet, r.first.kind, r.first.col, r.last.col, r.last.abs_col), ("Sheet1", "column", 1, 3, True))
        (r,) = scan("=SUM($1:$5)")
        self.assertEqual((r.first.kind, r.first.row, r.first.abs_row, r.last.row), ("row", 1, True, 5))
        (r,) = scan("=A:A")
        self.assertEqual((r.first.start, r.first.end, r.last.start, r.last.end), (1, 2, 3, 4))

    def test_rewrite_replaces_whole_item_span_only(self):
        source = '=SUM( Sheet1!A1:B2 ) & "A1" + C3'
        seen = []

        def change(item):
            seen.append(item)
            return "X" if isinstance(item, RefRange) else None

        self.assertEqual(rewrite(source, change), '=SUM( X ) & "A1" + C3')
        self.assertEqual([type(i) for i in seen], [RefRange, Ref])
        self.assertEqual(rewrite("=A:A+1:1", lambda item: "Z"), "=Z+Z")

    def test_function_names_and_names_are_not_references(self):
        for source in ("=LOG10(2)", "=A1(2)", "=A1B", "=ABC123X+1", "=_xlfn.CONCAT(1)", "=my.name"):
            with self.subTest(source):
                self.assertEqual(scan(source), [])
        self.assertEqual(len(scan("=LOG10(A1)")), 1)

    def test_spill_and_implicit_intersection(self):
        (r,) = scan("=@A1")
        self.assertEqual((r.row, r.col), (1, 1))
        (r,) = scan("=SUM(B2#)")
        self.assertEqual((r.row, r.col), (2, 2))
        self.assertEqual(rewrite("=A1#", lambda item: "B1"), "=B1#")

    def test_structured_references_and_external_prefixes(self):
        self.assertEqual(scan("=Table1[[#This Row],[A1]]+Table1[Col [x']]]"), [])
        (r,) = scan("=Table1[Col]+B2")
        self.assertEqual((r.row, r.col), (2, 2))
        (r,) = scan("=[Book.xlsx]Sheet1!A1")
        self.assertEqual((r.external, r.sheet, r.prefix_start, r.start), (True, "Sheet1", 1, 19))
        (r,) = scan("='[Book.xlsx]Sheet 1'!A1")
        self.assertEqual((r.external, r.sheet), (True, "Sheet 1"))
        (r,) = scan("='Sheet 1'!A1")
        self.assertFalse(r.external)
        (r,) = scan("=[1]A1")
        self.assertTrue(r.external)

    def test_out_of_grid_coordinates_are_not_references(self):
        for source in ("=XFE1", "=A1048577", "=XFE:XFE", "=1048577:1048577"):
            with self.subTest(source):
                self.assertEqual(scan(source), [])
        self.assertEqual(len(scan("=XFD1048576")), 1)

    def test_number_followed_by_colon_outside_a_reference(self):
        items = scan("=IF(A1,1:1,2)")
        self.assertEqual([type(i) for i in items], [Ref, RefRange])
        self.assertEqual(items[1].first.kind, "row")
        self.assertEqual(scan("=1.5:2"), [])

    def test_errors_and_escapes(self):
        self.assertEqual(scan('=IF(A1,#REF!,"a""B2")'), scan("=IF(A1,0,0)"))
        self.assertEqual(len(scan("=Sheet1!Name+A1")), 1)
        self.assertEqual(len(scan("=\"x\"\"A1\"&B2")), 1)


if __name__ == "__main__":
    unittest.main()
