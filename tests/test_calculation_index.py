# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the compact in-memory dependents index and its interval lookup."""

import unittest

from datahorizon.calculation_index import DependentsIndex, STORED_FORMULA_LIMIT


class DependentsIndexTests(unittest.TestCase):
    def test_single_cells_ranges_and_transitive_readers(self):
        s = "sheet-1"
        b1, c1, d1, e1 = (s, 1, 2), (s, 1, 3), (s, 1, 4), (s, 1, 5)
        idx = DependentsIndex([
            (b1, (s, 1, 1, 1, 1)),
            (c1, (s, 1, 2, 1, 2)),
            (d1, (s, 1, 1, 10, 1)),
            (e1, (s, 1, 26, 1, 26)),
        ])
        self.assertEqual(len(idx), 4)
        self.assertEqual(idx.affected([(s, 1, 1, 1, 1)]), {b1, c1, d1})
        self.assertEqual(idx.affected([(s, 5, 1, 5, 1)]), {d1})
        self.assertEqual(idx.affected([(s, 1, 26, 1, 26)]), {e1})
        self.assertEqual(idx.affected([(s, 50, 50, 50, 50)]), set())

    def test_rectangle_roots(self):
        s = "sheet-1"
        r64 = (s, 100, 2)
        r65 = (s, 101, 2)
        rrange = (s, 102, 2)
        idx = DependentsIndex([
            (r64, (s, 64, 1, 64, 1)),
            (r65, (s, 65, 1, 65, 1)),
            (rrange, (s, 60, 5, 70, 5)),
        ])
        self.assertEqual(idx.affected([(s, 1, 1, 64, 16384)]), {r64, rrange})
        self.assertEqual(idx.affected([(s, 65, 1, 128, 16384)]), {r65, rrange})

    def test_relink_and_unlink(self):
        s = "sheet-1"
        f1 = (s, 1, 2)
        f2 = (s, 2, 2)
        idx = DependentsIndex()
        idx.link(f1, [(s, 1, 1, 1, 1), (s, 2, 1, 5, 1)])
        idx.link(f2, [(s, 1, 2, 1, 2)])
        self.assertEqual(len(idx), 2)
        self.assertEqual(idx.affected([(s, 1, 1, 1, 1)]), {f1, f2})
        self.assertEqual(idx.affected([(s, 4, 1, 4, 1)]), {f1, f2})

        # Relink f1 to read C1:C10 instead of A1 and A2:A5.
        idx.link(f1, [(s, 1, 3, 10, 3)])
        self.assertEqual(len(idx), 2)
        self.assertEqual(idx.affected([(s, 1, 1, 1, 1)]), set())
        self.assertEqual(idx.affected([(s, 4, 1, 4, 1)]), set())
        self.assertEqual(idx.affected([(s, 5, 3, 5, 3)]), {f1, f2})

        # Unlink f1.
        idx.unlink(f1)
        self.assertEqual(len(idx), 1)
        self.assertEqual(idx.affected([(s, 5, 3, 5, 3)]), set())
        self.assertEqual(idx.affected([(s, 1, 2, 1, 2)]), {f2})

        # Unlink missing key is a no-op.
        idx.unlink(f1)
        self.assertEqual(len(idx), 1)

    def test_prefix_sums_over_a_formula_column_stay_cheap(self):
        s = "sheet-1"
        precedents = []
        expected = set()
        for row in range(2, 2002):
            key = (s, row, 10)
            precedents.append((key, (s, row - 1, 10, row - 1, 10)))
            expected.add(key)
        for row in range(1, 2001):
            key = (s, row, 11)
            precedents.append((key, (s, 1, 10, row, 10)))
            expected.add(key)
        idx = DependentsIndex(precedents)
        self.assertEqual(len(idx), 4000)
        idx.checks = 0
        got = idx.affected([(s, 1, 10, 1, 10)])
        self.assertEqual(got, expected)
        self.assertLess(idx.checks, 200_000)

    def test_other_sheets_are_not_affected(self):
        s1, s2 = "sheet-1", "sheet-2"
        f1 = (s1, 1, 2)
        f2 = (s2, 1, 2)
        cross = (s2, 2, 2)
        idx = DependentsIndex([
            (f1, (s1, 1, 1, 10, 1)),
            (f2, (s2, 1, 1, 10, 1)),
            (cross, (s1, 1, 1, 10, 1)),
        ])
        self.assertEqual(idx.affected([(s1, 5, 1, 5, 1)]), {f1, cross})
        self.assertEqual(idx.affected([(s2, 5, 1, 5, 1)]), {f2})

    def test_wide_ranges_and_formula_with_no_precedents(self):
        s = "sheet-1"
        wide = (s, 1, 1)
        const = (s, 2, 1)
        idx = DependentsIndex()
        idx.link(wide, [(s, 10, 1, 20, 100)])
        idx.link(const, ())
        self.assertEqual(len(idx), 2)
        self.assertEqual(idx.affected([(s, 15, 50, 15, 50)]), {wide})
        self.assertEqual(idx.affected([(s, 15, 101, 15, 101)]), set())
        self.assertEqual(idx.affected([(s, 1, 1, 64, 16384)]), {wide})
        self.assertGreater(STORED_FORMULA_LIMIT, 0)


if __name__ == "__main__":
    unittest.main()
