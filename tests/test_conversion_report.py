# SPDX-License-Identifier: GPL-3.0-only
import json
import unittest
from datahorizon.conversion_report import ConversionReport, SEVERITIES


class ConversionReportTests(unittest.TestCase):
    def test_repeated_problem_is_one_entry_with_bounded_samples(self):
        report=ConversionReport()
        for row in range(1,500001):
            report.add("formula.unsupported_function","unevaluable","VLOOKUP",sheet="Data",cell=f"B{row}")
        (entry,)=report.entries()
        self.assertEqual((entry["count"],len(entry["samples"])),(500000,100))
        self.assertEqual(entry["samples"][0],{"sheet":"Data","cell":"B1"})
        self.assertLess(len(json.dumps(report.entries())),16384)

    def test_entry_cap_folds_overflow_into_one_entry(self):
        report=ConversionReport()
        for index in range(250): report.add("style.number_format","omitted",f"code {index}")
        entries=report.entries()
        self.assertEqual(len(entries),200)
        self.assertEqual(entries[-1]["category"],"report.truncated")
        self.assertEqual(entries[-1]["severity"],"omitted")
        self.assertEqual(entries[-1]["count"],51)
        self.assertEqual(sum(e["count"] for e in entries),250)

    def test_unknown_severity_is_a_programming_error(self):
        with self.assertRaises(ValueError): ConversionReport().add("x","lost")

    def test_sample_needs_a_cell_and_sheet_may_be_missing(self):
        report=ConversionReport()
        report.add("a","omitted","d",sheet="S")
        report.add("a","omitted","d",cell="C3")
        (entry,)=report.entries()
        self.assertEqual(entry["count"],2)
        self.assertEqual(entry["samples"],[{"sheet":None,"cell":"C3"}])

    def test_detail_is_truncated_and_distinguishes_entries(self):
        report=ConversionReport()
        report.add("a","omitted","x"*500)
        report.add("a","omitted","y")
        self.assertEqual(sorted(len(e["detail"]) for e in report.entries()),[1,160])

    def test_entries_are_sorted_unevaluable_first_then_count_then_category(self):
        report=ConversionReport()
        report.add("z","converted",count=9)
        report.add("b","omitted",count=2)
        report.add("a","omitted",count=2)
        report.add("c","omitted",count=5)
        report.add("d","unevaluable")
        self.assertEqual([e["category"] for e in report.entries()],["d","c","a","b","z"])

    def test_entries_are_copies(self):
        report=ConversionReport()
        report.add("a","omitted",cell="A1")
        report.entries()[0]["samples"].clear()
        self.assertEqual(len(report.entries()[0]["samples"]),1)

    def test_merge_adds_counts_and_bounds_samples(self):
        left,right=ConversionReport(),ConversionReport()
        for row in range(80):
            left.add("f","unevaluable","VLOOKUP",sheet="A",cell=f"A{row}")
            right.add("f","unevaluable","VLOOKUP",sheet="B",cell=f"A{row}")
        right.add("g","omitted")
        left.merge(right)
        by_category={e["category"]:e for e in left.entries()}
        self.assertEqual(by_category["f"]["count"],160)
        self.assertEqual(len(by_category["f"]["samples"]),100)
        self.assertEqual(by_category["f"]["samples"][80],{"sheet":"B","cell":"A0"})
        self.assertEqual(by_category["g"]["count"],1)

    def test_merge_respects_entry_cap_and_carries_overflow(self):
        left,right=ConversionReport(),ConversionReport()
        for index in range(150): left.add("l","omitted",str(index))
        for index in range(150): right.add("r","omitted",str(index))
        for index in range(250): right.add("o","omitted",str(index))
        left.merge(right)
        entries=left.entries()
        self.assertEqual(len(entries),200)
        self.assertEqual(entries[-1]["category"],"report.truncated")
        self.assertEqual(sum(e["count"] for e in entries),150+150+250)

    def test_totals_include_every_severity(self):
        report=ConversionReport()
        report.add("a","unevaluable",count=3)
        report.add("b","omitted")
        report.add("c","omitted",count=4)
        self.assertEqual(report.totals(),{"converted":0,"approximated":0,"omitted":5,"unevaluable":3})
        self.assertEqual(tuple(report.totals()),SEVERITIES)

    def test_lines_are_human_sentences(self):
        report=ConversionReport()
        for row in (3,4):
            report.add("formula.unsupported_function","unevaluable","VLOOKUP is not available",sheet="Sheet1",cell=f"B{row}")
        report.add("style.fill","omitted","pattern fill",sheet="Sheet1",cell="A1")
        report.add("sheet.hidden","approximated")
        self.assertEqual(report.lines(),[
            "2 cells: VLOOKUP is not available (unevaluable); first at Sheet1!B3",
            "1 cell: pattern fill (omitted); first at Sheet1!A1",
            "1 cell: sheet.hidden (approximated)"])

    def test_line_for_sample_without_sheet(self):
        report=ConversionReport()
        report.add("a","omitted","gone",cell="C3")
        self.assertEqual(report.lines(),["1 cell: gone (omitted); first at C3"])


if __name__ == "__main__":
    unittest.main()
