# SPDX-License-Identifier: GPL-3.0-only
"""Small, deliberately bounded spreadsheet parser. Never evaluates Python source."""

from decimal import Context, Decimal, InvalidOperation, localcontext, ROUND_HALF_UP, ROUND_UP, ROUND_DOWN
import math
import re
import time
from datetime import date, timedelta

from .functions import FUNCTIONS
from .temporal import Temporal, from_serial, utc_clock, temporal_key, temporal_arithmetic

SUPPORTED_FUNCTIONS = frozenset(FUNCTIONS)
CELL_ERRORS = {"#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#NUM!", "#N/A", "#NULL!"}
NUMBER_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")

MAX_ROWS, MAX_COLS = 1_048_576, 16_384
REF = r"(?:(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z_0-9]*)!)?\$?[A-Za-z]{1,3}\$?[1-9][0-9]*"
TOKEN = re.compile(
    rf'\s*(?:(?P<ref>{REF})(?![A-Za-z_0-9]|\s*\()|'
    r'(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)|'
    r'(?P<string>"(?:[^"]|"")*")|(?P<error>\#REF!)|'
    r'(?P<name>[A-Za-z_][A-Za-z_0-9]*)|(?P<op><=|>=|<>|[+*/^&(),:=<>-]))'
)
ADDRESS = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)([1-9][0-9]*)\Z")
QUOTED_TOKEN = re.compile(r''' "(?:[^"]|"")*" | '(?:[^']|'')*' ''', re.VERBOSE)
ERROR_LITERAL = re.compile(r"#(?:REF!|DIV/0!|VALUE!|NAME\?|NUM!|N/A|NULL!|CYCLE!|LIMIT!)", re.IGNORECASE)


class FormulaError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


class Unmaintainable(FormulaError):
    """Neither the strict parser nor the lenient scanner can find this formula's references."""
    def __init__(self, message="Cannot maintain references in unsupported formula syntax"):
        super().__init__("#VALUE!", message)


# The typed value of an `unmaintained` cell: a formula whose references a
# structural edit met as Unmaintainable. It is never evaluated.
UNMAINTAINED_MESSAGE = ("References in this formula were not maintained through a structural edit. "
                        "Re-enter the formula.")


def column_name(col):
    out = ""
    while col:
        col, digit = divmod(col - 1, 26)
        out = chr(65 + digit) + out
    return out


def address(row, col):
    return f"{column_name(col)}{row}"


def coordinates(source):
    match = ADDRESS.fullmatch(source)
    if not match:
        raise FormulaError("#REF!", "Expected an A1 address")
    col = 0
    for char in match[2].upper():
        col = col * 26 + ord(char) - 64
    if len(match[4]) > len(str(MAX_ROWS)):
        raise FormulaError("#REF!", "Address exceeds the supported grid")
    row = int(match[4])
    if row > MAX_ROWS or col > MAX_COLS:
        raise FormulaError("#REF!", "Address exceeds the supported grid")
    return row, col


def split_ref(source):
    if "!" in source:
        sheet, source = source.rsplit("!", 1)
        if sheet.startswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
    else:
        sheet = None
    return sheet, *coordinates(source)


def has_sheet_reference(source):
    """Ignore error literals and quotes, retaining a quoted sheet's separator.

    Structural edits use it for formulas no transform can read: one on a sheet
    the edit does not touch can refer to that sheet only through a `!`, so a
    False result leaves it unmarked. An incomplete quoted token stays
    conservative, so an unclassified ! counts. Intentionally not scanner-based:
    `=Other!#REF!` must stay True.
    """
    pos = 1
    while pos < len(source):
        if source[pos] == "!":
            return True
        if source[pos] == "#" and (token := ERROR_LITERAL.match(source, pos)):
            pos = token.end()
        elif source[pos] in ("\"", "'"):
            token = QUOTED_TOKEN.match(source, pos)
            if not token:
                return "!" in source[pos:]
            pos = token.end()
        else:
            pos += 1
    return False


def translate(source, rows, cols):
    """Copy/fill offsets affect relative components; string literals are untouched."""
    if not source.startswith("="):
        return source
    if _strict_parse(source) is None:
        return _scanned_translate(source, rows, cols)
    out, pos = ["="], 1
    while pos < len(source):
        if source[pos:].isspace():
            out.append(source[pos:])
            break
        match = TOKEN.match(source, pos)
        if not match:
            # Unreachable after a successful parse; never corrupt the source.
            raise FormulaError("#VALUE!", "Cannot translate unsupported formula syntax")
        token = match.group(0)
        if match.lastgroup == "ref":
            ref = match["ref"]
            prefix, cell = ref.rsplit("!", 1) if "!" in ref else (None, ref)
            parts = ADDRESS.fullmatch(cell)
            row, col = coordinates(cell)
            row += 0 if parts[3] else rows
            col += 0 if parts[1] else cols
            if not (1 <= row <= MAX_ROWS and 1 <= col <= MAX_COLS):
                new = "#REF!"
            else:
                new = f"{parts[1]}{column_name(col)}{parts[3]}{row}"
                if prefix:
                    new = prefix + "!" + new
            token = token[:len(token) - len(ref)] + new
        out.append(token)
        pos = match.end()
    return "".join(out)


def rename_sheet_references(source, old_name, new_name, *, target_id=None, new_id=None):
    """Retarget qualified names; copies can also retarget explicit self UUIDs."""
    if (parsed := _strict_parse(source)) is None:
        return _scanned_rename(source, old_name, new_name, target_id, new_id)
    parser, pending = parsed[0], [parsed[1]]
    fold = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    edits = []
    while pending:
        node = pending.pop()
        if node[0] == "call":
            pending.extend(node[2])
        elif node[0] in ("binary", "range"):
            pending.extend(node[2:])
        elif node[0] == "unary":
            pending.append(node[2])
        elif node[0] == "ref":
            name, _, _ = split_ref(node[1])
            replacement_name = None
            if name is not None:
                if name == target_id:
                    replacement_name = new_id  # Rename preserves identity; copy creates one.
                elif name.translate(fold) == old_name.translate(fold):
                    replacement_name = new_name
            if replacement_name is not None:
                _, cell = node[1].rsplit("!", 1)
                begin, end = parser.spans[id(node)]
                edits.append((begin+1, end+1, "'"+replacement_name.replace("'", "''")+"'!"+cell))
    out, pos = [], 0
    for begin, end, replacement in sorted(edits):
        out.extend((source[pos:begin], replacement))
        pos = end
    out.append(source[pos:])
    return "".join(out)


def delete_sheet_references(source, local_sheet, target_sheet, *, target_id=None):
    """Make deleted-sheet references permanent errors, including inherited ranges."""
    if (parsed := _strict_parse(source)) is None:
        return _scanned_delete(source, _sheet_matcher(local_sheet, target_sheet, target_id))
    parser, pending = parsed[0], [parsed[1]]
    fold = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    def targets(name):
        return (target_id is not None and name == target_id or
                (local_sheet if name is None else name).translate(fold) == target_sheet.translate(fold))
    edits = []
    while pending:
        node = pending.pop()
        if node[0] == "call":
            pending.extend(node[2])
        elif node[0] == "binary":
            pending.extend(node[2:])
        elif node[0] == "unary":
            pending.append(node[2])
        elif node[0] in ("ref","range"):
            if node[0] == "range":
                first, last = node[2:]
                if first[0] != "ref" or last[0] != "ref":
                    raise FormulaError("#REF!", "Range endpoints must be cell references")
                name, _, _ = split_ref(first[1])
                last_name, _, _ = split_ref(last[1])
                if last_name is None:
                    last_name = name
                if targets(name) != targets(last_name):
                    raise FormulaError("#REF!", "Cannot transform a range spanning different sheets")
            else:
                name, _, _ = split_ref(node[1])
            if targets(name):
                begin, end = parser.spans[id(node)]
                edits.append((begin+1,end+1,"#REF!"))
    out, pos = [], 0
    for begin, end, replacement in sorted(edits):
        out.extend((source[pos:begin],replacement))
        pos = end
    out.append(source[pos:])
    return "".join(out), len(edits)


def adjust_interval(first, last, start, count, delete, maximum=None):
    """Retarget an interval after inserting or deleting count members at start.

    Contracts around deleted members and expands for insertions inside it;
    return None when wholly deleted or moved off the grid. Preserve the
    order of first and last.
    """
    end = start + count - 1
    low, high = sorted((first, last))
    if delete:
        if start <= low and high <= end:
            return None
        low = low if low < start else max(low, end+1)-count
        high = high-count if high > end else min(high, start-1)
    else:
        low += count if low >= start else 0
        high += count if high >= start else 0
    if not (1 <= low <= high <= (MAX_ROWS if maximum is None else maximum)):
        return None
    return (low, high) if first <= last else (high, low)


def transform_structure(source, local_sheet, target_sheet, axis, start, count, delete=False, *, target_id=None,
                        sheet_order=None):
    """Retarget A1 references after a whole-row/column edit; preserve token spelling.

    Absolute markers control copying, not structural edits. A range contracts
    around deleted members and expands for insertions inside its endpoints.
    Return the source and number of newly broken reference expressions.
    sheet_order (display order of sheet names) places 3D references; without
    it a 3D reference is edited only when the edited sheet is one of its ends.
    """
    if (parsed := _strict_parse(source)) is None:
        matcher = _sheet_matcher(local_sheet, target_sheet, target_id, sheet_order)
        return _scanned_structure(source, matcher, axis, start, count, delete)
    parser, pending = parsed[0], [parsed[1]]  # Use the same range semantics as the calculator.
    fold = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    def targets(name):
        if target_id is not None and name == target_id:
            return True  # The calculator also accepts an exact sheet UUID.
        return (local_sheet if name is None else name).translate(fold) == target_sheet.translate(fold)
    def coordinate(ref):
        name, row, col = split_ref(ref)
        return name, row if axis == "row" else col
    maximum = MAX_ROWS if axis == "row" else MAX_COLS
    def interval(first, last):
        return adjust_interval(first, last, start, count, delete, maximum)
    def render(ref, value):
        prefix, cell = ref.rsplit("!", 1) if "!" in ref else (None, ref)
        parts = ADDRESS.fullmatch(cell)
        row, col = coordinates(cell)
        if axis == "row": row = value
        else: col = value
        result = f"{parts[1]}{column_name(col)}{parts[3]}{row}"
        return prefix+"!"+result if prefix is not None else result
    edits, broken = [], 0
    while pending:
        node = pending.pop()
        if node[0] == "call":
            pending.extend(reversed(node[2]))
            continue
        if node[0] == "binary":
            pending.extend((node[3],node[2]))
            continue
        if node[0] == "unary":
            pending.append(node[2])
            continue
        if node[0] not in ("ref", "range"):
            continue
        first = last = node
        if node[0] == "range":
            first, last = node[2:]
            if first[0] != "ref" or last[0] != "ref":
                raise FormulaError("#REF!", "Range endpoints must be cell references")
        name, value = coordinate(first[1])
        last_name, last_value = coordinate(last[1])
        if last is not first and last_name is None:
            last_name = name  # Sheet!A1:A3 inherits the first endpoint's sheet.
        if last is not first and targets(name) != targets(last_name):
            raise FormulaError("#REF!", "Cannot transform a range spanning different sheets")
        if targets(name):
            values = interval(value, last_value)
            if values is None:
                begin, finish = parser.spans[id(node)]
                edits.append((begin+1, finish+1, "#REF!"))
                broken += 1
            else:
                endpoints = ((first,value,values[0]),) if last is first else ((first,value,values[0]),(last,last_value,values[1]))
                for endpoint, old, new in endpoints:
                    if old != new:
                        begin, finish = parser.spans[id(endpoint)]
                        edits.append((begin+1,finish+1,render(endpoint[1],new)))
    out, pos = [], 0
    for begin, end, replacement in sorted(edits):
        out.extend((source[pos:begin], replacement))
        pos = end
    out.append(source[pos:])
    return "".join(out), broken


def move_references(source, local_sheet, target_sheet, rectangle, rows, cols, *, target_id=None, sheet_order=None):
    """Same-sheet cut: referenced targets follow the moved rectangle, even $ refs.

    Discontiguous partial range moves cannot be represented in the scalar tier.
    Moved formulas keep their original external targets, unlike Copy/Fill.
    """
    edits=[]
    if (parsed:=_strict_parse(source)) is None:
        matcher=_sheet_matcher(local_sheet,target_sheet,target_id,sheet_order)
        return _scanned_move(source,matcher,rectangle,rows,cols)
    parser,pending=parsed[0],[parsed[1]]
    fold=str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")
    r1,c1,r2,c2=rectangle
    def targets(name):
        return (target_id is not None and name==target_id or
                (local_sheet if name is None else name).translate(fold)==target_sheet.translate(fold))
    def render(ref):
        prefix,cell=ref.rsplit("!",1) if "!" in ref else (None,ref)
        parts=ADDRESS.fullmatch(cell);row,col=coordinates(cell)
        result=f"{parts[1]}{column_name(col+cols)}{parts[3]}{row+rows}"
        return prefix+"!"+result if prefix is not None else result
    while pending:
        node=pending.pop()
        if node[0]=="call":pending.extend(node[2]);continue
        if node[0]=="binary":pending.extend(node[2:]);continue
        if node[0]=="unary":pending.append(node[2]);continue
        if node[0] not in ("ref","range"):continue
        first=last=node
        if node[0]=="range":
            first,last=node[2:]
            if first[0]!="ref" or last[0]!="ref":raise FormulaError("#REF!","Move requires cell reference range endpoints")
        name,row,col=split_ref(first[1]);last_name,end_row,end_col=split_ref(last[1])
        if last is not first and last_name is None:last_name=name
        if targets(name)!=targets(last_name):raise FormulaError("#REF!","Cannot move a range spanning different sheets")
        if not targets(name):continue
        low_row,high_row=sorted((row,end_row));low_col,high_col=sorted((col,end_col))
        if high_row<r1 or low_row>r2 or high_col<c1 or low_col>c2:continue
        if not (r1<=low_row<=high_row<=r2 and c1<=low_col<=high_col<=c2):
            raise FormulaError("#REF!","Cut partially intersects a formula range; move its entire referenced range or use Copy")
        for endpoint in (first,) if last is first else (first,last):
            begin,end=parser.spans[id(endpoint)];edits.append((begin+1,end+1,render(endpoint[1])))
    out=[];pos=0
    for begin,end,replacement in sorted(edits):out.extend((source[pos:begin],replacement));pos=end
    out.append(source[pos:]);return "".join(out)


# Fallbacks for formulas the strict parser rejects (unknown functions, whole
# rows/columns, 3D and external references). They apply the strict rules to
# the references formula_refs.scan finds and keep all other text verbatim.

def _fold(name):
    # SQLite NOCASE is ASCII-only, unlike str.casefold().
    return name.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def _strict_parse(source):
    """Return (parser, tree) for the strict transforms, or None when only the scanner reads it right.

    Besides syntax the parser rejects, that is a `2:3` whole-row range (parsed
    as two numbers) and a quoted prefix holding a `:` or `[`, which sheet names
    cannot contain: a 3D or external reference.
    """
    try:
        parser = Parser(source[1:], track_spans=True)
        tree = parser.parse()
    except FormulaError:
        return None
    pending = [tree]
    while pending:
        node = pending.pop()
        if node[0] == "call":
            pending.extend(node[2])
        elif node[0] in ("binary", "range"):
            if node[0] == "range" and all(end[0] == "literal" and type(end[1]) is float for end in node[2:]):
                return None
            pending.extend(node[2:])
        elif node[0] == "unary":
            pending.append(node[2])
        elif node[0] == "ref" and "!" in node[1] and any(char in node[1].rsplit("!", 1)[0] for char in ":["):
            return None
    return parser, tree


def _scanned(source):
    from .formula_refs import scan
    items = scan(source)
    if items is None:
        raise Unmaintainable()
    return items


def _rewrite(source, change):
    from .formula_refs import rewrite
    return rewrite(source, change)


def _endpoints(item):
    """[(ref, sheet, sheet_last, external)], resolving a second endpoint's inherited prefix."""
    from .formula_refs import RefRange
    if not isinstance(item, RefRange):
        return [(item, item.sheet, item.sheet_last, item.external)]
    first, last = item.first, item.last
    resolved = [(first, first.sheet, first.sheet_last, first.external)]
    if last.prefix_start == last.start:  # Sheet1!A1:B2 inherits Sheet1 (and a 3D span or workbook)
        resolved.append((last, first.sheet, first.sheet_last, first.external))
    else:
        resolved.append((last, last.sheet, last.sheet_last, last.external))
    return resolved


def _sheet_matcher(local_sheet, target_sheet, target_id=None, sheet_order=None):
    """Return matches(sheet, sheet_last): does a reference with this prefix point at the target sheet?"""
    target = _fold(target_sheet)
    positions = {_fold(name): index for index, name in enumerate(sheet_order or ())}
    def named(name):
        return target_id is not None and name == target_id or _fold(name) == target
    def matches(sheet, sheet_last=None):
        if sheet is None:
            return _fold(local_sheet) == target
        if sheet_last is None:
            return named(sheet)
        if named(sheet) or named(sheet_last):
            return True
        low, high, edited = positions.get(_fold(sheet)), positions.get(_fold(sheet_last)), positions.get(target)
        return None not in (low, high, edited) and min(low, high) <= edited <= max(low, high)
    matches.named = named
    return matches


def _body(ref, row, col):
    column = "" if ref.kind == "row" else ("$" if ref.abs_col else "") + column_name(col)
    number = "" if ref.kind == "column" else ("$" if ref.abs_row else "") + str(row)
    return column + number


def _item_text(source, endpoints, bodies=None, prefixes=None):
    """Rebuild an item's text, replacing the given endpoint bodies/prefixes by index."""
    bodies, prefixes = bodies or {}, prefixes or {}
    out = []
    for index, (ref, *_ignored) in enumerate(endpoints):
        if index:
            out.append(source[endpoints[0][0].end:ref.prefix_start])
        out.append(prefixes.get(index, source[ref.prefix_start:ref.start]))
        out.append(bodies.get(index, source[ref.start:ref.end]))
    return "".join(out)


def _target_endpoints(item, matches, action):
    """Endpoints of a non-external item on the target sheet, or None; a split range is refused."""
    endpoints = _endpoints(item)
    if any(external for *_ignored, external in endpoints):
        return None
    hits = {matches(sheet, sheet_last) for _ref, sheet, sheet_last, _external in endpoints}
    if len(hits) > 1:
        raise FormulaError("#REF!", f"Cannot {action} a range spanning different sheets")
    return endpoints if hits.pop() else None


def _scanned_translate(source, rows, cols):
    _scanned(source)
    def change(item):
        # Copy/fill shifts relative parts of external references too; only their prefix stays verbatim.
        endpoints = _endpoints(item)
        bodies = {}
        for index, (ref, *_ignored) in enumerate(endpoints):
            row = ref.row if ref.row is None or ref.abs_row else ref.row + rows
            col = ref.col if ref.col is None or ref.abs_col else ref.col + cols
            if (row is not None and not 1 <= row <= MAX_ROWS) or (col is not None and not 1 <= col <= MAX_COLS):
                return "#REF!"
            if (row, col) != (ref.row, ref.col):
                bodies[index] = _body(ref, row, col)
        return _item_text(source, endpoints, bodies) if bodies else None
    return _rewrite(source, change)


def _scanned_structure(source, matches, axis, start, count, delete):
    _scanned(source)
    field, maximum = ("row", MAX_ROWS) if axis == "row" else ("col", MAX_COLS)
    broken = 0
    def change(item):
        nonlocal broken
        endpoints = _target_endpoints(item, matches, "transform")
        if endpoints is None:
            return None
        refs = [ref for ref, *_ignored in endpoints]
        old = [getattr(ref, field) for ref in refs]
        if old[0] is None:
            return None  # a whole column under a row edit, or a whole row under a column edit
        if refs[0].kind != "cell" and not delete:
            # A range already spanning the whole axis (A:XFD, 1:1048576) stays; otherwise an insert
            # pushing its far end past the grid edge clamps it there instead of breaking it.
            shifted = [value + count if value >= start else value for value in old]
            if sorted(old) == [1, maximum]:
                shifted = old
            new = None if min(shifted) > maximum else [min(value, maximum) for value in shifted]
        else:
            new = adjust_interval(old[0], old[-1], start, count, delete, maximum)
        if (new is None or any(before != after for before, after in zip(old, new))) and any(sheet_last is not None for _ref, _sheet, sheet_last, _external in endpoints):
            # One rectangle stands for every sheet of the span, and only the edited sheet's cells moved:
            # no rewrite keeps pointing at the same cells on all of them.
            raise Unmaintainable("A structural edit on one sheet of a 3D reference cannot be followed")
        if new is None:
            broken += 1
            return "#REF!"
        bodies = {}
        for index, (ref, before, after) in enumerate(zip(refs, old, new)):
            if before != after:
                bodies[index] = _body(ref, after if field == "row" else ref.row, after if field == "col" else ref.col)
        return _item_text(source, endpoints, bodies) if bodies else None
    return _rewrite(source, change), broken


def _scanned_move(source, matches, rectangle, rows, cols):
    _scanned(source)
    r1, c1, r2, c2 = rectangle
    def change(item):
        endpoints = _target_endpoints(item, matches, "move")
        if endpoints is None:
            return None
        refs = [ref for ref, *_ignored in endpoints]
        low_row, high_row = (1, MAX_ROWS) if refs[0].row is None else sorted((refs[0].row, refs[-1].row))
        low_col, high_col = (1, MAX_COLS) if refs[0].col is None else sorted((refs[0].col, refs[-1].col))
        if high_row < r1 or low_row > r2 or high_col < c1 or low_col > c2:
            return None
        if any(sheet_last is not None for _ref, _sheet, sheet_last, _external in endpoints):
            raise FormulaError("#REF!", "Cut intersects a 3D reference; a move cannot retarget one sheet of it")
        if source[:refs[0].prefix_start].rstrip().endswith(":") or source[refs[-1].end:].lstrip().startswith(":"):
            # One end of a range the scanner cannot pair, such as A1:Sheet2!B:B: its extent is unknown.
            raise FormulaError("#REF!", "Cut intersects a range whose extent cannot be followed; use Copy")
        if not (r1 <= low_row <= high_row <= r2 and c1 <= low_col <= high_col <= c2):
            raise FormulaError("#REF!", "Cut partially intersects a formula range; move its entire referenced range or use Copy")
        bodies = {}
        for index, ref in enumerate(refs):
            row = None if ref.row is None else ref.row + rows
            col = None if ref.col is None else ref.col + cols
            if (row is not None and not 1 <= row <= MAX_ROWS) or (col is not None and not 1 <= col <= MAX_COLS):
                raise FormulaError("#REF!", "Move would place a reference outside the grid")
            if ref.kind == "column" and rows or ref.kind == "row" and cols:
                raise FormulaError("#REF!", "Move would place a reference outside the grid")
            bodies[index] = _body(ref, row, col)
        return _item_text(source, endpoints, bodies)
    return _rewrite(source, change)


def _scanned_rename(source, old_name, new_name, target_id, new_id):
    _scanned(source)
    def renamed(name):
        if name == target_id:
            return new_id  # Rename preserves identity; copy creates one.
        return new_name if _fold(name) == _fold(old_name) else None
    def quoted(name):
        return name.replace("'", "''")
    def change(item):
        endpoints = _endpoints(item)
        if any(external for *_ignored, external in endpoints):
            return None
        prefixes = {}
        for index, (ref, *_ignored) in enumerate(endpoints):
            if ref.sheet is None or ref.prefix_start == ref.start:
                continue  # unqualified, or inheriting the first endpoint's prefix
            first = renamed(ref.sheet)
            last = None if ref.sheet_last is None else renamed(ref.sheet_last)
            if first is None and last is None:
                continue
            name = quoted(ref.sheet if first is None else first)
            if ref.sheet_last is not None:
                name += ":" + quoted(ref.sheet_last if last is None else last)
            prefixes[index] = "'" + name + "'!"
        return _item_text(source, endpoints, prefixes=prefixes) if prefixes else None
    return _rewrite(source, change)


def _scanned_delete(source, matches):
    _scanned(source)
    broken = 0
    def deleted(sheet, sheet_last):
        # Removing an inner sheet leaves a 3D span valid; removing an end breaks it. Known limitation:
        # Excel shrinks the span to the neighbouring sheet instead, which needs the display order.
        if sheet_last is None:
            return matches(sheet)
        return matches.named(sheet) or matches.named(sheet_last)
    def change(item):
        nonlocal broken
        endpoints = _endpoints(item)
        if any(external for *_ignored, external in endpoints):
            return None
        hits = {deleted(sheet, sheet_last) for _ref, sheet, sheet_last, _external in endpoints}
        if len(hits) > 1:
            raise FormulaError("#REF!", "Cannot transform a range spanning different sheets")
        if not hits.pop():
            return None
        broken += 1
        return "#REF!"
    return _rewrite(source, change), broken


class Parser:
    def __init__(self, source, *, track_spans=False):
        self.tokens, pos = [], 0
        # Optional reference/range offsets use node identity so repeated A1
        # occurrences stay distinct without changing the calculator's AST.
        self.spans = {} if track_spans else None
        self.token_spans = [] if track_spans else None
        offset = len(source)-len(source.lstrip()) if track_spans else 0
        source = source.strip()
        while pos < len(source):
            match = TOKEN.match(source, pos)
            if not match:
                raise FormulaError("#VALUE!", f"Unsupported syntax near {source[pos:pos+20]}")
            self.tokens.append((match.lastgroup, match[match.lastgroup]))
            if track_spans:
                self.token_spans.append((offset+match.start(match.lastgroup),offset+match.end(match.lastgroup)))
            pos = match.end()
            if len(self.tokens) > 1024:
                raise FormulaError("#LIMIT!", "Formula has too many tokens")
        self.tokens.append(("end", ""))
        self.pos = 0

    def take(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def require(self, value):
        if self.take()[1] != value:
            raise FormulaError("#VALUE!", f"Expected {value}")

    def expression(self, minimum=0, depth=0):
        if depth > 128:
            raise FormulaError("#LIMIT!", "Formula nesting limit exceeded")
        start = self.pos
        kind, value = self.take()
        if kind == "number":
            node = ("literal", float(value))
        elif kind == "string":
            node = ("literal", value[1:-1].replace('""', '"'))
        elif kind == "ref":
            node = ("ref", value)
            if self.spans is not None:
                self.spans[id(node)] = self.token_spans[start]
        elif kind == "error":
            node = ("error", value)
        elif value in ("+", "-"):
            node = ("unary", value, self.expression(30, depth + 1))
        elif value == "(":
            node = self.expression(depth=depth + 1)
            self.require(")")
        elif kind == "name":
            if value.upper() in ("TRUE", "FALSE") and self.tokens[self.pos][1] != "(":
                node = ("literal", value.upper() == "TRUE")
            else:
                self.require("(")
                args = []
                if self.tokens[self.pos][1] != ")":
                    while True:
                        args.append(self.expression(depth=depth + 1))
                        if self.tokens[self.pos][1] != ",":
                            break
                        self.take()
                self.require(")")
                node = ("call", value.upper(), args)
        else:
            raise FormulaError("#VALUE!", "Incomplete formula")
        precedence = {"=": 5, "<>": 5, "<": 5, ">": 5, "<=": 5, ">=": 5,
                      "&": 7, "+": 10, "-": 10, "*": 20, "/": 20, "^": 40, ":": 50}
        while self.tokens[self.pos][1] in precedence:
            op = self.tokens[self.pos][1]
            power = precedence[op]
            if power < minimum:
                break
            self.take()
            right = self.expression(power if op == "^" else power + 1, depth + 1)
            node = ("range" if op == ":" else "binary", op, node, right)
            if self.spans is not None and op == ":":
                # Include endpoint parentheses when replacing a whole range,
                # but retain each leaf reference's own span for ordinary edits.
                self.spans[id(node)] = (self.token_spans[start][0],self.token_spans[self.pos-1][1])
        return node

    def parse(self):
        node = self.expression()
        if self.tokens[self.pos][0] != "end":
            raise FormulaError("#VALUE!", "Unexpected trailing input")
        return node


def numeric(value):
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    raise FormulaError("#VALUE!", "Arithmetic requires numeric inputs")


MAX_TEXT_CHARACTERS=32767
MAX_TEXT_BYTES=65536


def scalar_text(value):
    """Unformatted invariant scalar spelling; never parse Text as a number."""
    if value is None:return ""
    if isinstance(value,Temporal):return str(value)
    if type(value) is str:return value
    if type(value) is bool:return "TRUE" if value else "FALSE"
    if type(value) in (int,float):
        if not math.isfinite(value):raise FormulaError("#NUM!","Non-finite text conversion")
        if value==0:return "0"
        return str(int(value)) if value==int(value) and abs(value)<=9007199254740991 else repr(value)
    raise FormulaError("#VALUE!","Text functions require scalar arguments")


def bounded_text_size(characters,byte_count):
    if characters>MAX_TEXT_CHARACTERS or byte_count>MAX_TEXT_BYTES:raise FormulaError("#LIMIT!","Text result exceeds 32,767 characters or 64 KiB UTF-8")


def bounded_text(value):
    bounded_text_size(len(value),len(value.encode()));return value


class Criterion:
    """Bounded explicit conditional comparison; never writes/coerces stored inputs."""
    def __init__(self,value,calculator):
        self.calculator=calculator;self.op="=";self.pattern=[];self.wildcards=False
        if isinstance(value,list):raise FormulaError("#VALUE!","Criteria must be scalar")
        if value is None:value=0
        if type(value) is str:
            if len(value)>255:raise FormulaError("#LIMIT!","Criteria Text exceeds 255 characters")
            match=re.match(r"^(<=|>=|<>|=|<|>)(.*)\Z",value,re.S)
            if match:self.op,value=match[1],match[2]
            if NUMBER_TEXT.fullmatch(value.strip()):value=float(value.strip())
        self.value=value;self.kind="temporal" if isinstance(value,Temporal) else "boolean" if type(value) is bool else "number" if type(value) in (int,float) else "text"
        if self.kind=="number" and not math.isfinite(value):raise FormulaError("#NUM!","Criteria must be finite")
        if self.kind=="text":
            value=value.casefold();i=0
            while i<len(value):
                ch=value[i]
                if ch=="~" and i+1<len(value) and value[i+1] in "*?~":i+=1;self.pattern.append(("literal",value[i]))
                elif ch in "*?":self.pattern.append((ch,""));self.wildcards=True
                else:self.pattern.append(("literal",ch))
                i+=1
            self.value="".join(ch for kind,ch in self.pattern)
            if self.wildcards and self.op not in ("=","<>"):raise FormulaError("#VALUE!","Wildcard criteria require = or <>")

    def wildcard_match(self,text):
        p=t=0;star=-1;retry=0;steps=0
        while t<len(text):
            steps+=1
            if steps%64==1:self.calculator.check()
            if p<len(self.pattern) and (self.pattern[p][0]=="?" or self.pattern[p]==("literal",text[t])):p+=1;t+=1
            elif p<len(self.pattern) and self.pattern[p][0]=="*":star=p;p+=1;retry=t
            elif star>=0:retry+=1;t=retry;p=star+1
            else:return False
        while p<len(self.pattern) and self.pattern[p][0]=="*":p+=1
        return p==len(self.pattern)

    def matches(self,left):
        self.calculator.check()
        if isinstance(left,FormulaError):return False
        if self.kind=="temporal":
            if not isinstance(left,Temporal) or temporal_key(left)[0]!=temporal_key(self.value)[0]:return self.op=="<>"
            left=temporal_key(left);right=temporal_key(self.value)
            return {"=":lambda:left==right,"<>":lambda:left!=right,"<":lambda:left<right,">":lambda:left>right,"<=":lambda:left<=right,">=":lambda:left>=right}[self.op]()
        if self.kind=="number":
            if left is None:left=0
            elif isinstance(left,str):
                self.calculator.text_work(left)
                if NUMBER_TEXT.fullmatch(left.strip()):left=float(left.strip())
            compatible=type(left) in (int,float) and math.isfinite(left)
        elif self.kind=="boolean":compatible=type(left) is bool
        else:
            if left is None and not self.value and not self.wildcards:left=""
            compatible=type(left) is str
            if compatible:
                self.calculator.text_work(left);left=left.casefold()
                if self.wildcards:
                    matched=self.wildcard_match(left);return matched if self.op=="=" else not matched
        if not compatible:return self.op=="<>"
        right=self.value
        return {"=":lambda:left==right,"<>":lambda:left!=right,"<":lambda:left<right,">":lambda:left>right,"<=":lambda:left<=right,">=":lambda:left>=right}[self.op]()


class Calculator:
    def __init__(self, read, resolve_sheet, seconds=0.25, *, clock=None):
        self.read, self.resolve_sheet = read, resolve_sheet
        self.clock = utc_clock() if clock is None else clock
        self.cache, self.active = {}, set()
        self.deadline, self.count = time.monotonic() + seconds, 0
        self.bytes = 0

    def check(self):
        self.count += 1
        if self.count > 10_000 or time.monotonic() > self.deadline:
            raise FormulaError("#LIMIT!", "Calculation budget exceeded")

    def cell(self, sheet, row, col):
        self.check()
        key = sheet, row, col
        if key in self.active:
            raise FormulaError("#CYCLE!", "Circular reference")
        if len(self.active) >= 128:
            raise FormulaError("#LIMIT!", "Dependency chain exceeds 128 cells")
        if key in self.cache:
            result = self.cache[key]
            if isinstance(result, FormulaError):
                raise result
            return result
        self.active.add(key)
        try:
            item = self.read(sheet, row, col)
            if item:
                self.bytes += len(item["source"].encode())
                if self.bytes > 8*1024*1024:
                    raise FormulaError("#LIMIT!", "Calculation input cache exceeds 8 MiB")
            if item is None:
                result = None
            elif item["kind"] == "unmaintained":
                raise FormulaError("#REF!", UNMAINTAINED_MESSAGE)
            elif item["kind"] == "formula":
                result = self.evaluate(Parser(item["source"][1:]).parse(), sheet)
                if isinstance(result, list):
                    raise FormulaError("#VALUE!", "Range cannot be a scalar result")
            else:
                result = item["value"]
            if isinstance(result, (int, float)) and not math.isfinite(result):
                raise FormulaError("#NUM!", "Non-finite result")
            self.cache[key] = result
            return result
        except OverflowError:
            error = FormulaError("#NUM!", "Numeric overflow")
            self.cache[key] = error
            raise error
        except RecursionError:
            error = FormulaError("#LIMIT!", "Calculation nesting limit exceeded")
            self.cache[key] = error
            raise error
        except FormulaError as error:
            self.cache[key] = error
            raise
        finally:
            self.active.remove(key)

    def reference(self, ref, sheet):
        name, row, col = split_ref(ref)
        return self.resolve_sheet(name) if name is not None else sheet, row, col

    def range_geometry(self,node,sheet):
        if node[0]=="error":raise FormulaError(node[1])
        if node[0]=="ref":
            target=self.reference(node[1],sheet);return target[0],target[1],target[2],target[1],target[2]
        if node[0]!="range":raise FormulaError("#VALUE!","Expected a cell or rectangular range")
        if node[2][0]!="ref" or node[3][0]!="ref":raise FormulaError("#REF!","Range endpoints must be cell references")
        first=self.reference(node[2][1],sheet);last_name,_,_=split_ref(node[3][1]);last=self.reference(node[3][1],first[0] if last_name is None else sheet)
        if first[0]!=last[0]:raise FormulaError("#REF!","3D ranges are unsupported")
        r1,r2=sorted((first[1],last[1]));c1,c2=sorted((first[2],last[2]))
        if (r2-r1+1)*(c2-c1+1)>10000:raise FormulaError("#LIMIT!","Range exceeds 10,000 cells")
        return first[0],r1,c1,r2,c2

    def range_targets(self,node,sheet):
        target,r1,c1,r2,c2=self.range_geometry(node,sheet)
        for row in range(r1,r2+1):
            for col in range(c1,c2+1):yield target,row,col

    def criterion_cell(self,target):
        try:return self.cell(*target)
        except FormulaError as error:
            if error.code not in CELL_ERRORS:raise
            return error

    def scalar_value(self,node,sheet):
        if node[0]=="range":raise FormulaError("#VALUE!","Scalar argument required")
        value=self.evaluate(node,sheet)
        if isinstance(value,list):raise FormulaError("#VALUE!","Array arguments are unavailable")
        return value

    def text_argument(self,node,sheet):
        text=scalar_text(self.scalar_value(node,sheet));self.text_work(text);return text

    def exact_text(self,args,sheet):
        if len(args)!=2:raise FormulaError("#NAME?","EXACT requires two scalars")
        if any(node[0]=="range" for node in args):raise FormulaError("#VALUE!","EXACT requires scalars")
        result=self.text_argument(args[0],sheet)==self.text_argument(args[1],sheet);self.check();return result

    def integer_argument(self,node,sheet,minimum):
        value=numeric(self.scalar_value(node,sheet))
        if not math.isfinite(value):raise FormulaError("#NUM!","Position/count must be finite")
        if value<minimum:raise FormulaError("#VALUE!","Position/count is below its minimum")
        return int(value)

    def calendar_function(self,name,args,sheet):
        if len(args)!=(3 if name=="DATE" else 1):raise FormulaError("#NAME?",name+" has invalid arity")
        values=[self.scalar_value(arg,sheet) for arg in args]
        try:
            if name=="DATE":
                year,month,day=[int(numeric(value)) for value in values]
                if not 0<=year<=9999:raise ValueError("DATE year must be 0–9999")
                if year<1900:year+=1900
                year,month0=divmod(year*12+month-1,12)
                value=date(year,month0+1,1)+timedelta(days=day-1)
                return Temporal(value.isoformat(),"date")
            value=values[0]
            if isinstance(value,Temporal):
                if value.kind=="time":raise FormulaError("#VALUE!","Calendar extraction requires a date")
            else:value=from_serial(numeric(value),"date")
            return getattr(value.parsed(),{"YEAR":"year","MONTH":"month","DAY":"day"}[name])
        except (ValueError,OverflowError) as error:
            raise FormulaError("#NUM!",str(error)) from error

    def clock_function(self,name,args,sheet):
        if len(args)!=(3 if name=="TIME" else 1):raise FormulaError("#NAME?",name+" has invalid arity")
        values=[self.scalar_value(arg,sheet) for arg in args]
        try:
            if name=="TIME":
                hour,minute,second=[int(numeric(value)) for value in values]
                if any(not 0<=part<=32767 for part in (hour,minute,second)):raise ValueError("TIME components must be 0–32767")
                total=(hour*3600+minute*60+second)%86400
                return Temporal(f"{total//3600:02}:{total//60%60:02}:{total%60:02}","time")
            value=values[0]
            if isinstance(value,Temporal):
                if value.kind=="date":return 0
            else:
                serial=numeric(value)
                if serial<0:raise ValueError("Clock serial must be nonnegative")
                value=from_serial(serial,"time")
            return getattr(value.parsed(),{"HOUR":"hour","MINUTE":"minute","SECOND":"second"}[name])
        except (ValueError,OverflowError) as error:
            raise FormulaError("#NUM!",str(error)) from error

    def textjoin(self,args,sheet):
        if not 3<=len(args)<=255:raise FormulaError("#NAME?","TEXTJOIN requires delimiter, ignore_empty and 1–253 values")
        if args[0][0]=="range" or args[1][0]=="range":raise FormulaError("#VALUE!","TEXTJOIN delimiter/flag must be scalar")
        delimiter=self.text_argument(args[0],sheet);flag=self.scalar_value(args[1],sheet)
        if type(flag) not in (bool,int,float,type(None)):raise FormulaError("#VALUE!","ignore_empty must be numeric, Boolean or blank")
        def pieces():
            first=True
            for value,_ in self.argument_values(args[2:],sheet):
                self.check()
                if isinstance(value,FormulaError):raise value
                text=scalar_text(value)
                if flag and not text:continue
                if not first:yield delimiter
                yield text;first=False
        return self.concatenate_values(pieces())

    def wildcard_prefix(self,pattern,text,start):
        p=0;t=start;star=-1;retry=start;steps=0
        while True:
            steps+=1
            if steps%64==1:self.check()
            if p==len(pattern):return True
            kind,value=pattern[p]
            if kind=="*":star=p;retry=t;p+=1
            elif t<len(text) and (kind=="?" or value==text[t]):p+=1;t+=1
            elif star>=0 and retry<len(text):retry+=1;t=retry;p=star+1
            else:return False

    def search_text(self,args,sheet):
        if len(args) not in (2,3):raise FormulaError("#NAME?","SEARCH requires two or three scalars")
        if any(node[0]=="range" for node in args):raise FormulaError("#VALUE!","SEARCH requires scalars")
        needle=self.text_argument(args[0],sheet);text=self.text_argument(args[1],sheet);start=self.integer_argument(args[2],sheet,1) if len(args)==3 else 1
        if start>len(text)+1:raise FormulaError("#VALUE!","Start exceeds Text length plus one")
        if len(needle)>255:raise FormulaError("#LIMIT!","SEARCH pattern exceeds 255 characters")
        needle=needle.casefold();pattern=[];i=0
        while i<len(needle):
            char=needle[i]
            if char=="~" and i+1<len(needle) and needle[i+1] in "*?~":i+=1;pattern.append(("literal",needle[i]))
            else:pattern.append((char,"") if char in "*?" else ("literal",char))
            i+=1
        parts=[];origins=[]
        for position,char in enumerate(text[start-1:],start):
            if (position-start)%256==0:self.check()
            folded=char.casefold();parts.append(folded);origins.extend([position]*len(folded))
        folded="".join(parts);self.text_work(folded)
        if all(kind=="literal" for kind,_ in pattern):
            found=folded.find("".join(value for _,value in pattern));self.check()
            if found>=0:return origins[found] if found<len(origins) else len(text)+1
        else:
            for offset in range(len(folded)+1):
                self.check()
                if self.wildcard_prefix(pattern,folded,offset):return origins[offset] if offset<len(origins) else len(text)+1
        raise FormulaError("#VALUE!","Text was not found")

    def find_text(self,args,sheet):
        if len(args) not in (2,3):raise FormulaError("#NAME?","FIND requires two or three scalars")
        if any(node[0]=="range" for node in args):raise FormulaError("#VALUE!","FIND requires scalars")
        needle=self.text_argument(args[0],sheet);text=self.text_argument(args[1],sheet);start=self.integer_argument(args[2],sheet,1) if len(args)==3 else 1
        if start>len(text)+1:raise FormulaError("#VALUE!","Start exceeds Text length plus one")
        position=text.find(needle,start-1);self.check()
        if position<0:raise FormulaError("#VALUE!","Text was not found")
        return position+1

    def replace_text(self,args,sheet):
        if len(args)!=4:raise FormulaError("#NAME?","REPLACE requires four scalars")
        if any(node[0]=="range" for node in args):raise FormulaError("#VALUE!","REPLACE requires scalars")
        text=self.text_argument(args[0],sheet);start=self.integer_argument(args[1],sheet,1);count=self.integer_argument(args[2],sheet,0);new=self.text_argument(args[3],sheet)
        return self.concatenate_values((text[:start-1],new,text[start-1+count:]))

    def substitute(self,args,sheet):
        if len(args) not in (3,4):raise FormulaError("#NAME?","SUBSTITUTE requires 3 or 4 scalars")
        if any(node[0]=="range" for node in args):raise FormulaError("#VALUE!","SUBSTITUTE requires scalars")
        text,old,new=(self.text_argument(node,sheet) for node in args[:3])
        occurrence=self.integer_argument(args[3],sheet,1) if len(args)==4 else None
        if not old:return bounded_text(text)
        if occurrence is None:
            count=text.count(old);self.check()
            bounded_text_size(len(text)+count*(len(new)-len(old)),len(text.encode())+count*(len(new.encode())-len(old.encode())))
            result=text.replace(old,new);self.check();return result
        begin=0
        for _ in range(occurrence):
            self.check();found=text.find(old,begin)
            if found<0:return bounded_text(text)
            begin=found+len(old)
        return self.concatenate_values((text[:found],new,text[begin:]))

    def inspect_scalar(self,name,args,sheet):
        if len(args)!=1:raise FormulaError("#NAME?",name+" requires one scalar argument")
        if args[0][0]=="range":raise FormulaError("#VALUE!","Inspection requires a scalar")
        try:value=self.evaluate(args[0],sheet)
        except FormulaError as error:
            if error.code not in CELL_ERRORS:raise
            return name=="ISERROR" or name=="ISNA" and error.code=="#N/A"
        if isinstance(value,list):raise FormulaError("#VALUE!","Array inspection is unavailable")
        return {"ISBLANK":value is None,"ISNUMBER":type(value) in (int,float),"ISTEXT":type(value) is str,"ISLOGICAL":type(value) is bool,"ISERROR":False,"ISNA":False}[name]

    def exact_lookup_key(self,value):
        if isinstance(value,Temporal):return temporal_key(value)
        if isinstance(value,list):raise FormulaError("#VALUE!","Lookup value must be scalar")
        if value is None:return "blank",None
        if type(value) is bool:return "boolean",value
        if type(value) in (int,float):return "number",value
        self.text_work(value);return "text",value.casefold()

    def xlookup(self,args,sheet):
        if not 3<=len(args)<=6:raise FormulaError("#NAME?","XLOOKUP requires 3–6 arguments in this tier")
        if args[0][0]=="range":raise FormulaError("#VALUE!","Lookup value must be scalar")
        for index,expected in ((4,0),(5,1)):
            if len(args)>index:
                if args[index][0]=="range":raise FormulaError("#VALUE!","Lookup modes must be scalar")
                mode=self.evaluate(args[index],sheet)
                if type(mode) not in (int,float) or mode!=expected:raise FormulaError("#VALUE!","Only exact match_mode 0 and forward search_mode 1 are supported")
        lookup=self.range_geometry(args[1],sheet);returns=self.range_geometry(args[2],sheet)
        shape=(lookup[3]-lookup[1]+1,lookup[4]-lookup[2]+1)
        if min(shape)!=1 or shape!=(returns[3]-returns[1]+1,returns[4]-returns[2]+1):raise FormulaError("#VALUE!","Lookup/return ranges must be same-shaped vectors")
        key=self.exact_lookup_key(self.evaluate(args[0],sheet))
        for row in range(lookup[1],lookup[3]+1):
            for col in range(lookup[2],lookup[4]+1):
                self.check()
                if self.exact_lookup_key(self.cell(lookup[0],row,col))!=key:continue
                value=self.cell(returns[0],returns[1]+row-lookup[1],returns[2]+col-lookup[2])
                if isinstance(value,str):value=bounded_text(value)
                return 0 if value is None else value
        if len(args)>=4:
            if args[3][0]=="range":raise FormulaError("#VALUE!","Lookup fallback must be scalar")
            value=self.evaluate(args[3],sheet)
            if isinstance(value,list):raise FormulaError("#VALUE!","Lookup fallback must be scalar")
            if isinstance(value,str):value=bounded_text(value)
            return 0 if value is None else value
        raise FormulaError("#N/A","Lookup value was not found")

    def index_value(self,args,sheet):
        if len(args) not in (2,3):raise FormulaError("#NAME?","INDEX requires range, row and optional column")
        scope=self.range_geometry(args[0],sheet);height=scope[3]-scope[1]+1;width=scope[4]-scope[2]+1
        row=self.integer_argument(args[1],sheet,1)
        if len(args)==3:col=self.integer_argument(args[2],sheet,1)
        elif height==1:row,col=1,row
        elif width==1:col=1
        else:raise FormulaError("#VALUE!","Two-dimensional scalar INDEX requires a column")
        if row>height or col>width:raise FormulaError("#REF!","INDEX position exceeds its range")
        value=self.cell(scope[0],scope[1]+row-1,scope[2]+col-1)
        if isinstance(value,str):value=bounded_text(value)
        self.check();return 0 if value is None else value

    def match_exact(self,args,sheet):
        if len(args)!=3:raise FormulaError("#NAME?","This MATCH tier requires explicit match_type 0")
        mode=self.scalar_value(args[2],sheet)
        if type(mode) not in (int,float) or mode!=0:raise FormulaError("#VALUE!","Only exact MATCH mode 0 is supported")
        scope=self.range_geometry(args[1],sheet)
        if scope[1]!=scope[3] and scope[2]!=scope[4]:raise FormulaError("#VALUE!","MATCH requires a single row or column")
        key=self.exact_lookup_key(self.scalar_value(args[0],sheet));position=0
        for row in range(scope[1],scope[3]+1):
            for col in range(scope[2],scope[4]+1):
                self.check();position+=1
                if self.exact_lookup_key(self.cell(scope[0],row,col))==key:return position
        raise FormulaError("#N/A","Lookup value was not found")

    def multiple_criteria(self,name,args,sheet):
        offset=1 if name=="SUMIFS" else 0
        if not 2<=len(args)-offset<=254 or (len(args)-offset)%2:raise FormulaError("#NAME?",name+" requires 1–127 range/criterion pairs")
        pairs=[];shape=None
        for index in range(offset,len(args),2):
            scope=self.range_geometry(args[index],sheet);current=(scope[3]-scope[1]+1,scope[4]-scope[2]+1)
            if shape is not None and current!=shape:raise FormulaError("#VALUE!","Criteria ranges must have the same shape")
            shape=current;pairs.append((scope,Criterion(self.scalar_value(args[index+1],sheet),self)))
        sums=self.range_geometry(args[0],sheet) if offset else None
        if sums and (sums[3]-sums[1]+1,sums[4]-sums[2]+1)!=shape:raise FormulaError("#VALUE!","Sum range must have the same shape")
        total=0
        for row in range(shape[0]):
            for col in range(shape[1]):
                self.check();matched=True
                for scope,criterion in pairs:
                    eligible=criterion.matches(self.criterion_cell((scope[0],scope[1]+row,scope[2]+col)))
                    matched=matched and eligible
                if not matched:continue
                if not sums:total+=1;continue
                value=self.cell(sums[0],sums[1]+row,sums[2]+col)
                if type(value) in (int,float):
                    total+=value
                    if not math.isfinite(total):raise FormulaError("#NUM!","SUMIFS subtotal exceeds the finite range")
        return total

    def conditional_aggregate(self,name,args,sheet):
        if not (len(args)==2 if name=="COUNTIF" else len(args) in (2,3)):raise FormulaError("#NAME?",name+" has an unsupported argument count")
        scope=self.range_geometry(args[0],sheet);sums=self.range_geometry(args[2],sheet) if len(args)==3 else scope
        if (scope[3]-scope[1],scope[4]-scope[2])!=(sums[3]-sums[1],sums[4]-sums[2]):raise FormulaError("#VALUE!","SUMIF ranges require the same shape")
        if args[1][0]=="range":raise FormulaError("#VALUE!","Criteria must be scalar")
        criterion=Criterion(self.evaluate(args[1],sheet),self);result=0
        for row in range(scope[1],scope[3]+1):
            for col in range(scope[2],scope[4]+1):
                self.check()
                if not criterion.matches(self.criterion_cell((scope[0],row,col))):continue
                if name=="COUNTIF":result+=1;continue
                value=self.cell(sums[0],sums[1]+row-scope[1],sums[2]+col-scope[2])
                if type(value) in (int,float):
                    result+=value
                    if not math.isfinite(result):raise FormulaError("#NUM!","SUMIF subtotal exceeds the finite range")
        return result

    def argument_values(self,args,sheet):
        """Keep reference provenance and cell errors for range-aware functions."""
        for arg in args:
            if arg[0]=="range":
                try:
                    for target in self.range_targets(arg,sheet):
                        try: value=self.cell(*target)
                        except FormulaError as error:
                            if error.code not in CELL_ERRORS: raise
                            value=error
                        yield value,True
                except FormulaError as error:
                    if error.code not in CELL_ERRORS: raise
                    yield error,True
            else:
                try: value=self.evaluate(arg,sheet)
                except FormulaError as error:
                    if error.code not in CELL_ERRORS: raise
                    value=error
                if isinstance(value,list):
                    for item in value: yield item,True
                else: yield value,arg[0]=="ref"

    @staticmethod
    def logical_value(value):
        if value is None: return False
        if type(value) in (bool,int,float): return bool(value)
        if isinstance(value,str) and value.upper() in ("TRUE","FALSE"): return value.upper()=="TRUE"
        raise FormulaError("#VALUE!","Expected numeric, Boolean or TRUE/FALSE text")

    def logical(self,name,args,sheet):
        if name=="NOT":
            if len(args)!=1: raise FormulaError("#NAME?","NOT requires one scalar argument")
            return not self.logical_value(self.evaluate(args[0],sheet))
        if not 1<=len(args)<=255: raise FormulaError("#NAME?",name+" requires 1-255 arguments")
        result=name=="AND"; count=0
        for value,referenced in self.argument_values(args,sheet):
            self.check()
            if isinstance(value,FormulaError): raise value
            if value is None or (referenced and isinstance(value,str)): continue
            logical=self.logical_value(value); count+=1
            # Evaluate every argument: Python's any/all would hide later errors.
            result=(result and logical) if name=="AND" else (result or logical)
        if not count: raise FormulaError("#VALUE!","Logical aggregate has no numeric or Boolean inputs")
        return result

    def number_function(self,name,args,sheet):
        required=1 if name=="ABS" else 2
        if len(args)!=required: raise FormulaError("#NAME?",name+f" requires {required} arguments")
        number=numeric(self.evaluate(args[0],sheet))
        if name=="ABS": return abs(number)
        digits=numeric(self.evaluate(args[1],sheet))
        if not -15<=digits<=15: raise FormulaError("#NUM!","Rounding supports -15 through 15 decimal places")
        if int(digits)!=digits: raise FormulaError("#VALUE!","Rounding digits must be an integer")
        rounding={"ROUND":ROUND_HALF_UP,"ROUNDUP":ROUND_UP,"ROUNDDOWN":ROUND_DOWN}[name]
        try:
            with localcontext(Context(prec=340)):
                # Round the shortest decimal spelling of the binary64 value.
                result=float(Decimal(str(number)).quantize(Decimal(1).scaleb(-int(digits)),rounding=rounding))
        except (InvalidOperation,OverflowError,ValueError) as error:
            raise FormulaError("#NUM!","Rounding exceeded the numeric range") from error
        if not math.isfinite(result): raise FormulaError("#NUM!","Rounding exceeded the finite range")
        return 0.0 if result==0 else result

    def text_work(self,text):
        self.count+=max(0,(len(text)+255)//256-1)
        self.check()

    def concatenate_values(self,values):
        parts=[];characters=0;size=0
        for value in values:
            self.check()
            if isinstance(value,FormulaError):raise value
            text=scalar_text(value);self.text_work(text);characters+=len(text);size+=len(text.encode())
            if characters>MAX_TEXT_CHARACTERS or size>MAX_TEXT_BYTES:raise FormulaError("#LIMIT!","Concatenation exceeds 32,767 characters or 64 KiB UTF-8")
            parts.append(text)
        self.check();return "".join(parts)

    def substring(self,name,args,sheet):
        if not (len(args)==3 if name=="MID" else len(args) in (1,2)):raise FormulaError("#NAME?",name+" has an unsupported argument count")
        if any(arg[0]=="range" for arg in args):raise FormulaError("#VALUE!",name+" requires scalar arguments")
        text=scalar_text(self.evaluate(args[0],sheet));self.text_work(text)
        def count(arg,minimum):
            value=numeric(self.evaluate(arg,sheet))
            if not math.isfinite(value):raise FormulaError("#NUM!","Non-finite text position")
            if value<minimum:raise FormulaError("#VALUE!","Text positions/counts are outside their supported range")
            return int(value)
        if name=="MID":
            start=count(args[1],1)-1;length=count(args[2],0);result=text[start:start+length]
        else:
            length=count(args[1],0) if len(args)==2 else 1
            result=text[:length] if name=="LEFT" else text[-length:] if length else ""
        result=bounded_text(result);self.check();return result

    def text_function(self,name,args,sheet):
        if len(args)!=1:raise FormulaError("#NAME?",name+" requires one scalar argument")
        if args[0][0]=="range":raise FormulaError("#VALUE!",name+" requires a scalar argument")
        text=scalar_text(self.evaluate(args[0],sheet));self.text_work(text)
        if name=="LEN":return len(text)
        if name=="TRIM":result=re.sub(" +"," ",text.strip(" "))
        elif name=="UPPER":result=text.upper()
        else:result=text.lower()
        result=bounded_text(result);self.check();return result

    def aggregate(self,name,args,sheet):
        if not 1<=len(args)<=255: raise FormulaError("#NAME?",name+" requires 1-255 arguments")
        count=0; total=0.0; extreme=None
        for value,referenced in self.argument_values(args,sheet):
            self.check()
            if isinstance(value,FormulaError):
                if name=="COUNTA": count+=1; continue
                if name=="COUNT": continue
                raise value
            if name=="COUNTA":
                count+=value is not None
                continue
            if type(value) not in (int,float):
                if referenced or value is None: continue
                if type(value) is bool: value=int(value)
                elif isinstance(value,str) and NUMBER_TEXT.fullmatch(value.strip()): value=float(value.strip())
                elif name=="COUNT": continue
                else: raise FormulaError("#VALUE!",name+" requires numeric direct arguments")
            if not math.isfinite(value):
                if name=="COUNT": continue
                raise FormulaError("#NUM!","Non-finite aggregate input")
            count+=1
            if name=="AVERAGE":
                total+=value
                if not math.isfinite(total): raise FormulaError("#NUM!","AVERAGE subtotal exceeds the finite range")
            elif name in ("MIN","MAX"):
                extreme=value if extreme is None else (min(extreme,value) if name=="MIN" else max(extreme,value))
        if name=="AVERAGE":
            if not count: raise FormulaError("#DIV/0!","AVERAGE has no numeric inputs")
            return total/count
        if name in ("COUNT","COUNTA"): return count
        return extreme if extreme is not None else 0

    def evaluate(self, node, sheet):
        self.check()
        kind = node[0]
        if kind == "literal":
            if isinstance(node[1],(int,float)) and not math.isfinite(node[1]):
                raise FormulaError("#NUM!", "Numeric literal exceeds the finite range")
            return node[1]
        if kind == "error":
            raise FormulaError(node[1])
        if kind == "ref":
            return self.cell(*self.reference(node[1], sheet))
        if kind == "unary":
            value = numeric(self.evaluate(node[2], sheet))
            return -value if node[1] == "-" else value
        if kind == "range":
            return [self.cell(*target) for target in self.range_targets(node,sheet)]
        if kind == "call":
            name, args = node[1:]
            if name in ("TODAY","NOW"):
                if args:raise FormulaError("#NAME?",name+" takes no arguments")
                return Temporal(self.clock.date().isoformat(),"date") if name=="TODAY" else Temporal(self.clock.isoformat(),"datetime")
            if name in ("DATE","YEAR","MONTH","DAY"):return self.calendar_function(name,args,sheet)
            if name in ("TIME","HOUR","MINUTE","SECOND"):return self.clock_function(name,args,sheet)
            if name in ("IFERROR","IFNA"):
                if len(args)!=2: raise FormulaError("#NAME?",name+" requires two arguments")
                if args[0][0]=="range": raise FormulaError("#VALUE!",name+" requires scalar expressions")
                try: value=self.evaluate(args[0],sheet)
                except FormulaError as error:
                    if error.code not in (CELL_ERRORS if name=="IFERROR" else {"#N/A"}): raise
                    value=self.evaluate(args[1],sheet)
                if isinstance(value,list): raise FormulaError("#VALUE!",name+" array results are unavailable")
                return "" if value is None else value
            if name == "IF" and len(args) in (2, 3):
                condition = self.evaluate(args[0], sheet)
                if not isinstance(condition, (int, float, bool, type(None))):
                    raise FormulaError("#VALUE!", "IF condition must be numeric or boolean")
                return self.evaluate(args[1], sheet) if condition else (
                    self.evaluate(args[2], sheet) if len(args) == 3 else False)
            if name in ("ISBLANK","ISNUMBER","ISTEXT","ISLOGICAL","ISERROR","ISNA"):return self.inspect_scalar(name,args,sheet)
            if name=="TEXTJOIN":return self.textjoin(args,sheet)
            if name=="SEARCH":return self.search_text(args,sheet)
            if name=="FIND":return self.find_text(args,sheet)
            if name=="REPLACE":return self.replace_text(args,sheet)
            if name=="SUBSTITUTE":return self.substitute(args,sheet)
            if name=="EXACT":return self.exact_text(args,sheet)
            if name=="XLOOKUP":return self.xlookup(args,sheet)
            if name=="INDEX":return self.index_value(args,sheet)
            if name=="MATCH":return self.match_exact(args,sheet)
            if name in ("COUNTIFS","SUMIFS"):return self.multiple_criteria(name,args,sheet)
            if name in ("COUNTIF","SUMIF"):
                return self.conditional_aggregate(name,args,sheet)
            if name=="CONCAT":
                if not 1<=len(args)<=253:raise FormulaError("#NAME?","CONCAT requires 1–253 arguments")
                return self.concatenate_values(value for value,referenced in self.argument_values(args,sheet))
            if name in ("LEFT","RIGHT","MID"):
                return self.substring(name,args,sheet)
            if name in ("LEN","TRIM","UPPER","LOWER"):
                return self.text_function(name,args,sheet)
            if name in ("ABS","ROUND","ROUNDUP","ROUNDDOWN"):
                return self.number_function(name,args,sheet)
            if name in ("AND","OR","NOT"):
                return self.logical(name,args,sheet)
            if name in ("AVERAGE","MIN","MAX","COUNT","COUNTA"):
                return self.aggregate(name,args,sheet)
            if name == "SUM":
                total = 0
                for arg in args:
                    value = self.evaluate(arg, sheet)
                    for item in value if isinstance(value, list) else [value]:
                        if isinstance(item, (int, float)) and not isinstance(item, bool):
                            total += item
                            if not math.isfinite(total):
                                raise FormulaError("#NUM!", "SUM exceeds the finite numeric range")
                return total
            raise FormulaError("#NAME?", f"Unsupported function or arity: {name}")
        if kind == "binary":
            op = node[1]
            left, right = self.evaluate(node[2], sheet), self.evaluate(node[3], sheet)
            if isinstance(left, list) or isinstance(right, list):
                raise FormulaError("#VALUE!", "Array expressions are unsupported; use a range-aware function")
            if op=="&":return self.concatenate_values((left,right))
            if op in ("=", "<>", "<", ">", "<=", ">="):
                if isinstance(left,Temporal) or isinstance(right,Temporal):
                    if not isinstance(left,Temporal) or not isinstance(right,Temporal) or temporal_key(left)[0]!=temporal_key(right)[0]:raise FormulaError("#VALUE!","Comparison types differ")
                    left,right=temporal_key(left)[1],temporal_key(right)[1]
                if left is None or right is None:
                    # Blank comparisons permit numeric/Boolean coercion only.
                    left, right = numeric(left), numeric(right)
                if type(left) != type(right) and not (
                        type(left) in (float, int) and type(right) in (float, int)):
                    raise FormulaError("#VALUE!", "Comparison types differ")
                return {"=": lambda: left == right, "<>": lambda: left != right,
                        "<": lambda: left < right, ">": lambda: left > right,
                        "<=": lambda: left <= right, ">=": lambda: left >= right}[op]()
            if isinstance(left,Temporal) or isinstance(right,Temporal):
                try:return temporal_arithmetic(left,right,op)
                except TypeError as error:raise FormulaError("#VALUE!",str(error)) from error
                except (ValueError,OverflowError) as error:raise FormulaError("#NUM!",str(error)) from error
            left, right = numeric(left), numeric(right)
            if op == "/" and right == 0:
                raise FormulaError("#DIV/0!")
            if op == "^" and (abs(right) > 1024 or (left == 0 and right < 0)):
                raise FormulaError("#NUM!", "Exponent exceeds the supported numeric range")
            result = {"+": lambda: left + right, "-": lambda: left - right,
                      "*": lambda: left * right, "/": lambda: left / right,
                      "^": lambda: left ** right}[op]()
            if isinstance(result, complex) or not math.isfinite(result):
                raise FormulaError("#NUM!")
            return result
        raise FormulaError("#VALUE!")
