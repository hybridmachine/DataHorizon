# SPDX-License-Identifier: GPL-3.0-only
"""Decides whether the calculator can evaluate an imported formula, and why not."""
import re

from .formula import SUPPORTED_FUNCTIONS, FormulaError, Parser, split_ref
from .formula_refs import RefRange, scan

_STRING = re.compile(r'"(?:[^"]|"")*"')
_CALL = re.compile(r"([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_0-9]+)*)\s*\(")
_BOOLEAN = ("TRUE", "FALSE")


def _fold(name):
    # SQLite NOCASE is ASCII-only, unlike str.casefold().
    return name.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def status(source, sheet_names):
    """None when the calculator can evaluate ``source``; else ``(category, detail)``."""
    if any((ord(c) < 32 and c not in "\t\n\r") or ord(c) in (65534, 65535) for c in source):
        return ("formula.syntax", "")
    body = source[1:] if source.startswith("=") else source
    try:
        tree = Parser(body).parse()
        found = _walk(tree, {_fold(n) for n in (sheet_names or ())})
    except FormulaError:
        return _classify_failure(body)
    if found == ("formula.syntax", "range endpoints must be cell references"):
        # The parser reads a whole-row range such as 2:3 as two numbers.
        return _unsupported_reference("=" + body, scan("=" + body) or []) or found
    return found


def _walk(tree, sheets):
    pending = [tree]
    while pending:
        item = pending.pop()
        kind = item[0]
        if kind == "call":
            if item[1] not in SUPPORTED_FUNCTIONS:
                return ("formula.unsupported_function", item[1])
            pending.extend(reversed(item[2]))
        elif kind == "range":
            if not (item[2][0] == item[3][0] == "ref"):
                return ("formula.syntax", "range endpoints must be cell references")
            left, right = split_ref(item[2][1])[0], split_ref(item[3][1])[0]
            if right is not None and (left is None or _fold(left) != _fold(right)):
                return ("formula.syntax", "range spans different sheets")
            pending.extend((item[3], item[2]))
        elif kind == "binary":
            pending.extend((item[3], item[2]))
        elif kind == "unary":
            pending.append(item[2])
        elif kind == "ref":
            name = split_ref(item[1])[0]
            if name is not None and "[" in name:
                return ("formula.external_reference", "")
            if name is not None and ":" in name:  # sheet names cannot hold ":", so this is a quoted 3D prefix
                return ("formula.unsupported_reference", item[1])
            if name is not None and _fold(name) not in sheets:
                return ("formula.missing_sheet", name)
    return None


def _endpoints(item):
    return (item.first, item.last) if isinstance(item, RefRange) else (item,)


def _unsupported_reference(source, items):
    """A whole-row, whole-column or 3D reference the calculator cannot read, as (category, reference text)."""
    for item in items:
        first, *_ignored = ends = _endpoints(item)
        if any(ref.kind != "cell" or ref.sheet_last is not None for ref in ends):
            return ("formula.unsupported_reference", source[first.prefix_start:ends[-1].end])
    return None


def _classify_failure(body):
    items = scan("=" + body) or []
    if any(ref.external for item in items for ref in _endpoints(item)):
        return ("formula.external_reference", "")
    bare = _STRING.sub('""', body)
    if "{" in bare:
        return ("formula.array", "")
    # The parser cannot read every spelling (dotted names, whole-column ranges),
    # but an unavailable function is still the most useful thing to report.
    for call in _CALL.finditer(bare):
        if call[1].upper() not in SUPPORTED_FUNCTIONS:
            return ("formula.unsupported_function", call[1].upper())
    if (reference := _unsupported_reference("=" + body, items)):
        return reference
    try:
        tokens = Parser(body).tokens
    except FormulaError:
        return ("formula.syntax", "")
    for index, (kind, value) in enumerate(tokens):
        if kind == "name" and value.upper() not in _BOOLEAN and tokens[index + 1][1] != "(":
            return ("formula.name", value)
    return ("formula.syntax", "")
