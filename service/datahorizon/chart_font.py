# SPDX-License-Identifier: GPL-3.0-only
"""The font bundled for chart text and the advance widths the renderer lays out from."""

import functools
import json
import pathlib

FAMILY = "DejaVu Sans"
_FONTS = pathlib.Path(__file__).resolve().parent / "fonts"


def font_path() -> pathlib.Path:
    """Path of the bundled TrueType file."""
    return _FONTS / "DejaVuSans.ttf"


@functools.lru_cache(maxsize=1)
def default_metrics() -> dict[str, float]:
    """Character -> advance in ems for the bundled font."""
    return json.loads((_FONTS / "DejaVuSans-metrics.json").read_text(encoding="utf-8"))["advances"]
