# SPDX-License-Identifier: GPL-3.0-only
"""Regenerate charts-two-sheets.xlsx (and charts-one-sheet.xlsx, its first sheet alone): the standard-library two-sheet chart fixture (a column chart on each sheet) with fixed ZIP times,
so every run writes the same bytes. Run: python3 tests/fixtures/generate_chart_workbook.py"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "service"), str(ROOT / "tests")]
from chart_fixtures import sample_like

TIMESTAMP = (2026, 1, 1, 0, 0, 0)
OUTPUT = Path(__file__).with_name("charts-two-sheets.xlsx")
SINGLE = Path(__file__).with_name("charts-one-sheet.xlsx")


def build(sheets=2):
    return sample_like(sheets, timestamp=TIMESTAMP)


if __name__ == "__main__":
    OUTPUT.write_bytes(build())
    SINGLE.write_bytes(build(1))
