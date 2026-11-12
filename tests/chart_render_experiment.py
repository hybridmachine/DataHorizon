# SPDX-License-Identifier: GPL-3.0-only
"""Charts stage 1 experiment: a standard-library SVG chart renderer.

Isolated experiment code. It is not installed, not exposed through REST and
reads no workbook. See docs/design/charts-stage-1-plan.md.
"""

import math

LIMIT_POINTS = 10_000
LIMIT_SERIES = 64

_NARROW = frozenset("iIjl.,:;|!' ")
_WIDE = frozenset("mwMW@%")
_ELLIPSIS = "…"


class ChartRenderError(ValueError):
    """A structured refusal; ``code`` is the stable identifier."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Ticks on a 1/2/5 x 10^k step that cover [lo, hi]."""
    if lo > hi:
        lo, hi = hi, lo
    if lo == hi:
        if lo == 0:
            return [0, 1]
        lo, hi = min(lo, 0), max(hi, 0)
    raw = (hi - lo) / target
    exponent = math.floor(math.log10(raw))
    step = 10.0 ** (exponent + 1)
    for multiple in (1, 2, 5):
        candidate = multiple * 10.0**exponent
        if candidate >= raw * (1 - 1e-12):
            step = candidate
            break
    first = math.floor(lo / step + 1e-9)
    last = math.ceil(hi / step - 1e-9)
    places = max(0, -math.floor(math.log10(step) + 1e-9))
    ticks = []
    for index in range(first, last + 1):
        value = round(index * step, places)
        ticks.append(int(value) if value == int(value) else value)
    return ticks


def format_tick(value: float) -> str:
    """Invariant tick label: '.' decimal, no grouping, no float noise."""
    rounded = round(float(value), 10)
    if rounded == int(rounded):
        return str(int(rounded))
    return repr(rounded)


def _advance(char: str, table: dict[str, float] | None) -> float:
    if table is not None and char in table:
        return table[char]
    if char in _NARROW:
        return 0.30
    if char in _WIDE:
        return 0.90
    if not char.isascii():
        return 1.00
    if char.isdigit():
        return 0.60
    if char.isupper():
        return 0.72
    return 0.56


def estimate_text_width(text: str, size_px: float, table: dict[str, float] | None = None) -> float:
    """Estimated advance width in pixels; ``table`` maps a character to ems."""
    return sum(_advance(char, table) for char in text) * size_px


def truncate_to_width(text: str, size_px: float, max_px: float, table: dict[str, float] | None = None) -> str:
    """``text`` unchanged if it fits, else its longest prefix plus an ellipsis."""
    if estimate_text_width(text, size_px, table) <= max_px:
        return text
    budget = max_px - estimate_text_width(_ELLIPSIS, size_px, table)
    used = 0.0
    kept = 0
    for char in text:
        used += _advance(char, table) * size_px
        if used > budget:
            break
        kept += 1
    return text[:kept] + _ELLIPSIS
