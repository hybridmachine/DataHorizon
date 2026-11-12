# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the charts stage 1 renderer experiment (not product code)."""

import unittest

import chart_render_experiment as cr


class Ticks(unittest.TestCase):
    def test_nice_steps(self):
        self.assertEqual(cr.nice_ticks(0, 97), [0, 20, 40, 60, 80, 100])
        self.assertEqual(cr.nice_ticks(-3, 7), [-4, -2, 0, 2, 4, 6, 8])
        self.assertEqual(cr.nice_ticks(0, 0.5), [0, 0.1, 0.2, 0.3, 0.4, 0.5])

    def test_degenerate_ranges(self):
        self.assertEqual(cr.nice_ticks(5, 5), [0, 1, 2, 3, 4, 5])
        self.assertEqual(cr.nice_ticks(-5, -5), [-5, -4, -3, -2, -1, 0])
        self.assertEqual(cr.nice_ticks(0, 0), [0, 1])

    def test_format(self):
        self.assertEqual(cr.format_tick(20.0), "20")
        self.assertEqual(cr.format_tick(0.30000000000000004), "0.3")
        self.assertEqual(cr.format_tick(1500000.0), "1500000")


class Text(unittest.TestCase):
    def test_default_classes(self):
        self.assertAlmostEqual(cr.estimate_text_width("il", 10), 6.0)
        self.assertAlmostEqual(cr.estimate_text_width("MW", 10), 18.0)
        self.assertAlmostEqual(cr.estimate_text_width("09", 10), 12.0)
        self.assertAlmostEqual(cr.estimate_text_width("AB", 10), 14.4)
        self.assertAlmostEqual(cr.estimate_text_width("ab", 10), 11.2)
        self.assertAlmostEqual(cr.estimate_text_width("日", 10), 10.0)

    def test_table_overrides_classes(self):
        self.assertAlmostEqual(cr.estimate_text_width("ab", 10, {"a": 0.5, "b": 0.25}), 7.5)

    def test_truncate(self):
        self.assertEqual(cr.truncate_to_width("abc", 10, 1000), "abc")
        out = cr.truncate_to_width("a" * 80, 12, 60)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(cr.estimate_text_width(out, 12), 60)


if __name__ == "__main__":
    unittest.main()
