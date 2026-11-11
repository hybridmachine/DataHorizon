# SPDX-License-Identifier: GPL-3.0-only
"""Lenient formula reference scanner.

Finds A1-style references in formula text without parsing or evaluating it, so
formulas the calculator cannot parse can still follow row/column edits, sheet
renames and copy/fill. Strings, quoted sheet names, error literals, numbers,
names, array constants and structured references are skipped as opaque tokens.
"""

import re
from dataclasses import dataclass
from typing import Callable

from .formula import ADDRESS, ERROR_LITERAL, MAX_COLS, MAX_ROWS


@dataclass(frozen=True)
class Ref:
    start: int  # span of the cell/row/column part in source, excluding any sheet prefix
    end: int
    prefix_start: int  # equals start when there is no sheet prefix
    sheet: "str | None"
    sheet_last: "str | None"  # second sheet of a 3D reference
    external: bool  # a [n] or [Book.xlsx] workbook prefix
    kind: str  # "cell", "column" or "row"
    row: "int | None"
    col: "int | None"
    abs_row: bool
    abs_col: bool


@dataclass(frozen=True)
class RefRange:
    first: Ref
    last: Ref


_NAME = r"[^\W\d][\w.]*"
_PREFIX = re.compile(
    rf"(?P<ext>\[[^\[\]]*\])?"
    rf"(?:(?:'(?P<quoted>(?:[^']|'')+)'|(?P<first>{_NAME})(?::(?P<last>{_NAME}))?)!)?"
)
_AFTER = r"(?![\w.(\[])"
_CELL = re.compile(rf"(\$?)([A-Za-z]{{1,3}})(\$?)([1-9][0-9]*){_AFTER}")
_COLUMN = re.compile(rf"(\$?)([A-Za-z]{{1,3}}){_AFTER}")
_ROW = re.compile(rf"(\$?)([1-9][0-9]*){_AFTER}")
_COLON = re.compile(r"\s*:\s*")
_SPACE = re.compile(r"\s+")
_STRING = re.compile(r'"(?:[^"]|"")*"')
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_IDENTIFIER = re.compile(_NAME)
_OPERATORS = frozenset("+-*/^&=<>,;:%@#()")


def _column_number(letters):
    col = 0
    for char in letters.upper():
        col = col * 26 + ord(char) - 64
    return col


def _skip_brackets(source, pos):
    """Return the offset after the balanced `[...]` starting at pos, or None."""
    depth = 0
    while pos < len(source):
        char = source[pos]
        if char == "'":  # escape inside structured references
            pos += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1
    return None


def _skip_array(source, pos):
    pos += 1
    while pos < len(source):
        string = _STRING.match(source, pos) if source[pos] == '"' else None
        if string:
            pos = string.end()
            continue
        if source[pos] == '"':
            return None
        if source[pos] == "}":
            return pos + 1
        pos += 1
    return None


def _endpoint(source, pos):
    """Parse one prefixed cell, column or row endpoint at pos: (Ref, end) or None."""
    prefix = _PREFIX.match(source, pos)
    sheet = sheet_last = None
    external = bool(prefix["ext"])
    if prefix["quoted"] is not None:
        sheet = prefix["quoted"].replace("''", "'")
        if not external:
            inner = re.match(r"\[[^\]]*\]", sheet)
            if inner:
                external, sheet = True, sheet[inner.end():]
    elif prefix["first"] is not None:
        if prefix["last"] is not None and ADDRESS.fullmatch(prefix["first"]):
            return None  # `A1:Sheet2!B2` is a range endpoint, not a 3D prefix
        sheet, sheet_last = prefix["first"], prefix["last"]
    body = prefix.end()
    for kind, pattern in (("cell", _CELL), ("column", _COLUMN), ("row", _ROW)):
        match = pattern.match(source, body)
        if not match:
            continue
        row = col = None
        abs_row = abs_col = False
        if kind == "cell":
            col, row = _column_number(match[2]), int(match[4])
            abs_col, abs_row = bool(match[1]), bool(match[3])
        elif kind == "column":
            col, abs_col = _column_number(match[2]), bool(match[1])
        else:
            row, abs_row = int(match[2]), bool(match[1])
        if (col is not None and col > MAX_COLS) or (row is not None and row > MAX_ROWS):
            continue
        return Ref(body, match.end(), pos, sheet, sheet_last, external, kind, row, col, abs_row, abs_col), match.end()
    return None


def _reference(source, pos):
    """Parse a Ref or RefRange at pos: (item, end) or None."""
    found = _endpoint(source, pos)
    if not found:
        return None
    first, end = found
    colon = _COLON.match(source, end)
    second = _endpoint(source, colon.end()) if colon else None
    if second and second[0].kind == first.kind:
        return RefRange(first, second[0]), second[1]
    if first.kind != "cell":
        return None
    return first, end


def scan(source):
    """Return the top-level references of a formula, or None if it cannot be tokenized."""
    items = []
    pos = 1 if source.startswith("=") else 0
    while pos < len(source):
        char = source[pos]
        if char in _OPERATORS and not (char == "#" and ERROR_LITERAL.match(source, pos)):
            pos += 1
        elif (match := _SPACE.match(source, pos)):
            pos = match.end()
        elif char == '"':
            match = _STRING.match(source, pos)
            if not match:
                return None
            pos = match.end()
        elif char == "#":
            pos = ERROR_LITERAL.match(source, pos).end()
        elif char == "{":
            pos = _skip_array(source, pos)
            if pos is None:
                return None
        elif (found := _reference(source, pos)):
            items.append(found[0])
            pos = found[1]
        elif char == "[":
            pos = _skip_brackets(source, pos)
            if pos is None:
                return None
        elif (match := _PREFIX.match(source, pos)) and match.end() > pos:
            pos = match.end()  # a prefix with no reference after it, such as `Sheet1!Name`
        elif (match := _NUMBER.match(source, pos)):
            pos = match.end()
        elif (match := _IDENTIFIER.match(source, pos)):
            pos = match.end()
            if source.startswith("[", pos):
                pos = _skip_brackets(source, pos)
                if pos is None:
                    return None
        else:
            return None
    return items


def rewrite(source, change: Callable):
    """Replace each top-level reference with change(item), keeping the rest verbatim."""
    items = scan(source)
    if not items:
        return source
    out = []
    pos = 0
    for item in items:
        first, last = (item.first, item.last) if isinstance(item, RefRange) else (item, item)
        replacement = change(item)
        if replacement is None:
            continue
        out.append(source[pos:first.prefix_start])
        out.append(replacement)
        pos = last.end
    out.append(source[pos:])
    return "".join(out)
