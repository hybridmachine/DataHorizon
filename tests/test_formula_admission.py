# SPDX-License-Identifier: GPL-3.0-only
import unittest

from datahorizon.formula_admission import status
from datahorizon.xlsx_future_functions import FUTURE, PREFIX, strip_prefix

CASES = [("=SUM(A1:A3)", None),
         ("=VLOOKUP(A1,B:C,2,0)", ("formula.unsupported_function", "VLOOKUP")),
         ("=[1]Sheet1!A1", ("formula.external_reference", "")),
         ("=Other!A1", ("formula.missing_sheet", "Other")),
         ("=Rate*2", ("formula.name", "Rate")),
         ("={1,2}", ("formula.array", "")),
         ("=SUM(A1:A3", ("formula.syntax", "")),
         ("=STDEV.S(A1:A3)", ("formula.unsupported_function", "STDEV.S")),
         ('=IF(A1="[x]{y}",1,2)', None),
         ("=sheet1!A1+SUM(A1)", None),
         ("=Sheet1!A1:Sheet2!B2", ("formula.syntax", "range spans different sheets")),
         ("=TRUE", None)]


class FormulaAdmissionTests(unittest.TestCase):
    def test_status(self):
        for source, expected in CASES:
            with self.subTest(source):
                self.assertEqual(status(source, ["Sheet1"]), expected)

    def test_no_sheets_makes_every_sheet_reference_missing(self):
        self.assertEqual(status("=Sheet1!A1", None), ("formula.missing_sheet", "Sheet1"))

    def test_future_function_table(self):
        required = {"TEXTJOIN", "IFNA", "CONCAT", "XLOOKUP", "IFS", "SWITCH", "MAXIFS", "MINIFS", "LET", "LAMBDA",
                    "SEQUENCE", "UNIQUE", "XMATCH", "FILTER", "SORT", "SORTBY"}
        self.assertLessEqual(required, FUTURE)
        self.assertEqual(PREFIX["XLOOKUP"], "_xlfn.XLOOKUP")
        self.assertEqual(PREFIX["FILTER"], "_xlfn._xlws.FILTER")

    def test_prefix_removal_leaves_strings_alone(self):
        self.assertEqual(strip_prefix("'_xlfn.x'!A1+_xlfn.IFS(1)"),"'_xlfn.x'!A1+IFS(1)")
        self.assertLessEqual({"COMBINA","ENCODEURL","DETECTLANGUAGE","TRANSLATE"},FUTURE)
        self.assertEqual(strip_prefix('_xlfn.IFS(A1,"_xlfn.x")+_xlfn._xlws.SORT(A1)'), 'IFS(A1,"_xlfn.x")+SORT(A1)')


if __name__ == "__main__":
    unittest.main()
