# SPDX-License-Identifier: GPL-3.0-only
"""Prototypes for the calculation stage 1 experiment: input digests, dependency data, ordered evaluation.

Isolated test code (docs/design/calculation-stage-1-plan.md). Nothing here is used by the service.
"""
import hashlib

HASHES = ("blake2b16", "sha256")
MODES = ("sum", "stream")
_NEW = {"blake2b16": lambda data=b"": hashlib.blake2b(data, digest_size=16), "sha256": hashlib.sha256}
_SIZE = {"blake2b16": 16, "sha256": 32}


# ---- Input digests -------------------------------------------------------------------------------

def _encoded(row, col, source, kind):
    # Length-prefixed, so no two different cells share an encoding.
    source, kind = source.encode("utf-8", "surrogatepass"), kind.encode("utf-8", "surrogatepass")
    return b"%d:%d:%d:%d:" % (row, col, len(source), len(kind)) + source + kind


def cell_hash(hash_name, row, col, source, kind):
    return int.from_bytes(_NEW[hash_name](_encoded(row, col, source, kind)).digest(), "big")


def scan_digests(db, band, hash_name, mode):
    """Digest of every block of stored inputs, keyed by (sheet_id, (row - 1) // band). Style is not read."""
    new, size, digests = _NEW[hash_name], _SIZE[hash_name], {}
    rows = db.execute("SELECT sheet_id,row,col,source,kind FROM cells ORDER BY sheet_id,row,col")
    if mode == "sum":
        modulus, from_bytes = 1 << (8 * size), int.from_bytes
        for sheet, row, col, source, kind in rows:
            key = sheet, (row - 1) // band
            digests[key] = digests.get(key, 0) + from_bytes(new(_encoded(row, col, source, kind)).digest(), "big")
        return {key: (total % modulus).to_bytes(size, "big") for key, total in digests.items()}
    current, hasher = None, None
    for sheet, row, col, source, kind in rows:
        key = sheet, (row - 1) // band
        if key != current:
            if hasher is not None:
                digests[current] = hasher.digest()
            current, hasher = key, new()
        hasher.update(_encoded(row, col, source, kind))
    if hasher is not None:
        digests[current] = hasher.digest()
    return digests


def updated_digest(digest, hash_name, old, new):
    """A sum-mode block digest after one cell changed from old to new; each is (row, col, source, kind) or None."""
    size = _SIZE[hash_name]
    total = int.from_bytes(digest, "big")
    if old is not None:
        total -= cell_hash(hash_name, *old)
    if new is not None:
        total += cell_hash(hash_name, *new)
    return (total % (1 << (8 * size))).to_bytes(size, "big")


def sheets_digest(db, hash_name):
    size, total = _SIZE[hash_name], 0
    for sheet, name in db.execute("SELECT id,name FROM sheets"):
        sheet, name = sheet.encode(), name.encode("utf-8", "surrogatepass")
        total += int.from_bytes(_NEW[hash_name](b"%d:%d:" % (len(sheet), len(name)) + sheet + name).digest(), "big")
    return (total % (1 << (8 * size))).to_bytes(size, "big")


# ---- Dependency data -----------------------------------------------------------------------------

from bisect import bisect_right
from collections import defaultdict, deque

from datahorizon.formula import FormulaError, Parser, _fold, split_ref

WIDE = 256  # A range wider than this many columns is kept in a per-sheet list and scanned.
FORMULA_CELLS = "SELECT sheet_id,row,col,source FROM cells WHERE kind='formula' OR (kind='auto' AND source LIKE '=%')"


def extract_precedents(source, sheet_id, resolve):
    """What a formula reads, as rectangles (sheet_id, r1, c1, r2, c2), with its volatile and unsafe flags.

    A reference to an unknown sheet gives no rectangle: the formula is #REF! whatever any cell holds.
    """
    def target(ref, default):
        name, row, col = split_ref(ref)
        return (default if name is None else resolve(name)), row, col

    rectangles, volatile = {}, False
    try:
        pending = [Parser(source[1:]).parse()]
        while pending:
            node = pending.pop()
            kind = node[0]
            if kind == "ref":
                sheet, row, col = target(node[1], sheet_id)
                if sheet is not None:
                    rectangles[(sheet, row, col, row, col)] = None
            elif kind == "range":
                first, last = node[2], node[3]
                if first[0] == "ref" and last[0] == "ref":
                    sheet, r1, c1 = target(first[1], sheet_id)
                    last_sheet, r2, c2 = target(last[1], sheet if split_ref(last[1])[0] is None else sheet_id)
                    if sheet is not None and sheet == last_sheet:
                        rectangles[(sheet, min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2))] = None
                else:
                    pending.extend((last, first))
            elif kind == "call":
                volatile = volatile or node[1] in ("NOW", "TODAY")
                pending.extend(reversed(node[2]))
            elif kind == "binary":
                pending.extend((node[3], node[2]))
            elif kind == "unary":
                pending.append(node[2])
    except (FormulaError, RecursionError):
        return [], False, True
    return list(rectangles), volatile, False


def sheet_resolver(db):
    """Sheet name to identifier, or None, matching names as the service's SQLite NOCASE does (ASCII only)."""
    sheets = {_fold(name): sheet for sheet, name in db.execute("SELECT id,name FROM sheets")}
    return lambda name: sheets.get(_fold(name))


class DependencyData:
    """Precedent rectangles per formula, with a lookup from a changed cell to the formulas that read it.

    Single-cell precedents are a dictionary from the cell read to its readers. A range is filed under each
    column it covers, sorted by first row, and found by bisection; ranges wider than WIDE columns are
    kept per sheet and scanned.
    """

    def __init__(self, precedents, volatile=(), unsafe=()):
        self.precedents, self.volatile, self.unsafe = precedents, set(volatile), set(unsafe)
        self.single, columns, self.wide = defaultdict(list), defaultdict(list), defaultdict(list)
        for key, rectangles in precedents.items():
            for sheet, r1, c1, r2, c2 in rectangles:
                if r1 == r2 and c1 == c2:
                    self.single[(sheet, r1, c1)].append(key)
                elif c2 - c1 >= WIDE:
                    self.wide[sheet].append((r1, c1, r2, c2, key))
                else:
                    for col in range(c1, c2 + 1):
                        columns[(sheet, col)].append((r1, r2, key))
        self.columns = {}
        for place, ranges in columns.items():
            ranges.sort(key=lambda item: item[0])
            self.columns[place] = [item[0] for item in ranges], ranges

    @classmethod
    def build(cls, db):
        resolve = sheet_resolver(db)
        precedents, volatile, unsafe = {}, set(), set()
        for sheet, row, col, source in db.execute(FORMULA_CELLS):
            key = sheet, row, col
            precedents[key], is_volatile, is_unsafe = extract_precedents(source, sheet, resolve)
            if is_volatile:
                volatile.add(key)
            if is_unsafe:
                unsafe.add(key)
        return cls(precedents, volatile, unsafe)

    def save(self, db):
        db.executescript("""
            DROP TABLE IF EXISTS x_precedents; DROP TABLE IF EXISTS x_flags;
            CREATE TABLE x_precedents(sheet_id TEXT NOT NULL,row INTEGER NOT NULL,col INTEGER NOT NULL,
                p_sheet TEXT NOT NULL,r1 INTEGER NOT NULL,c1 INTEGER NOT NULL,r2 INTEGER NOT NULL,c2 INTEGER NOT NULL);
            CREATE TABLE x_flags(sheet_id TEXT NOT NULL,row INTEGER NOT NULL,col INTEGER NOT NULL,
                volatile INTEGER NOT NULL,unsafe INTEGER NOT NULL,PRIMARY KEY(sheet_id,row,col)) WITHOUT ROWID;""")
        db.executemany("INSERT INTO x_precedents VALUES(?,?,?,?,?,?,?,?)",
                       ((*key, *rectangle) for key, rectangles in self.precedents.items() for rectangle in rectangles))
        db.executemany("INSERT INTO x_flags VALUES(?,?,?,?,?)",
                       ((*key, key in self.volatile, key in self.unsafe) for key in self.precedents))
        db.executescript("""
            CREATE INDEX x_single ON x_precedents(p_sheet,r1,c1) WHERE r1=r2 AND c1=c2;
            CREATE INDEX x_range ON x_precedents(p_sheet,c1,r1) WHERE NOT (r1=r2 AND c1=c2);""")
        db.commit()

    @classmethod
    def load(cls, db):
        precedents, volatile, unsafe = {}, set(), set()
        for sheet, row, col, is_volatile, is_unsafe in db.execute("SELECT sheet_id,row,col,volatile,unsafe FROM x_flags"):
            key = sheet, row, col
            precedents[key] = []
            if is_volatile:
                volatile.add(key)
            if is_unsafe:
                unsafe.add(key)
        for sheet, row, col, *rectangle in db.execute("SELECT * FROM x_precedents"):
            precedents[(sheet, row, col)].append(tuple(rectangle))
        return cls(precedents, volatile, unsafe)

    def formulas(self):
        return self.precedents.keys()

    def readers(self, cell):
        """The formulas that read one cell directly."""
        sheet, row, col = cell
        yield from self.single.get(cell, ())
        column = self.columns.get((sheet, col))
        if column:
            starts, ranges = column
            for index in range(bisect_right(starts, row)):
                if ranges[index][1] >= row:
                    yield ranges[index][2]
        for r1, c1, r2, c2, key in self.wide.get(sheet, ()):
            if r1 <= row <= r2 and c1 <= col <= c2:
                yield key

    def dependents(self, cells):
        affected, pending = set(), list(cells)
        while pending:
            for key in self.readers(pending.pop()):
                if key not in affected:
                    affected.add(key)
                    pending.append(key)
        return affected

    def order(self, cells):
        """A dependency order of the formulas in cells, and those in or downstream of a cycle among them."""
        rows = defaultdict(lambda: defaultdict(list))
        for sheet, row, col in cells:
            rows[sheet][col].append(row)
        for by_column in rows.values():
            for values in by_column.values():
                values.sort()
        waiting, readers = {}, defaultdict(list)
        for key in cells:
            count = 0
            for sheet, r1, c1, r2, c2 in self.precedents.get(key, ()):
                if r1 == r2 and c1 == c2:
                    found = ((sheet, r1, c1),) if (sheet, r1, c1) in cells else ()
                else:
                    found = [(sheet, row, col) for col, values in rows.get(sheet, {}).items() if c1 <= col <= c2
                             for row in values[bisect_right(values, r1 - 1):bisect_right(values, r2)]]
                for precedent in found:
                    readers[precedent].append(key)
                    count += 1
            waiting[key] = count
        ready = deque(key for key, count in waiting.items() if not count)
        ordered = []
        while ready:
            key = ready.popleft()
            ordered.append(key)
            for reader in readers.get(key, ()):
                waiting[reader] -= 1
                if not waiting[reader]:
                    ready.append(reader)
        return ordered, set(cells) - set(ordered)


def sql_dependents(db, cells):
    """dependents() answered from x_precedents alone, with no graph in memory."""
    db.execute("CREATE TEMP TABLE IF NOT EXISTS x_roots(s TEXT,r INTEGER,c INTEGER)")
    db.execute("DELETE FROM x_roots")
    db.executemany("INSERT INTO x_roots VALUES(?,?,?)", cells)
    single = ("SELECT p.sheet_id,p.row,p.col FROM {0} a JOIN x_precedents p "
              "ON p.p_sheet=a.s AND p.r1=a.r AND p.c1=a.c WHERE p.r1=p.r2 AND p.c1=p.c2")
    ranges = ("SELECT p.sheet_id,p.row,p.col FROM {0} a JOIN x_precedents p "
              "ON p.p_sheet=a.s AND p.c1<=a.c AND p.c2>=a.c AND p.r1<=a.r AND p.r2>=a.r WHERE NOT (p.r1=p.r2 AND p.c1=p.c2)")
    terms = " UNION ".join(term.format(source) for source in ("x_roots", "affected") for term in (single, ranges))
    return set(db.execute(f"WITH RECURSIVE affected(s,r,c) AS ({terms}) SELECT s,r,c FROM affected"))


# ---- Ordered evaluation --------------------------------------------------------------------------

import math

from datahorizon.engine import typed
from datahorizon.formula import Calculator

CELL = "SELECT source,kind FROM cells WHERE sheet_id=? AND row=? AND col=?"


class OrderedCalculator(Calculator):
    """The service's functions over results that already exist.

    A formula cell's value comes from the results, so evaluating one formula never descends into another;
    the only limit is the number of cells one formula reads.
    """

    def __init__(self, db, data, results, cell_limit, unordered=()):
        find = sheet_resolver(db)

        def resolve(name):
            sheet = find(name)
            if sheet is None:
                raise FormulaError("#REF!", "Referenced sheet does not exist")
            return sheet
        super().__init__(None, resolve, 0)
        self.db, self.data, self.results, self.cell_limit, self.cells_read = db, data, results, cell_limit, 0
        # Formulas the static order could not place: in, or downstream of, a possible cycle. They are
        # evaluated by following the references a formula really reads, as the service does.
        self.unordered, self.following, self.cycle_hits = set(unordered), [], 0

    def check(self):
        pass

    def cell(self, sheet, row, col):
        self.cells_read += 1
        if self.cells_read > self.cell_limit:
            raise FormulaError("#LIMIT!", f"Formula reads more than {self.cell_limit} cells")
        key = sheet, row, col
        if key in self.unordered:
            result = self.follow(key)
            if isinstance(result, FormulaError):
                raise result
            return result
        if key in self.data.precedents:
            result = self.results[key]
            if isinstance(result, FormulaError):
                raise result
            return result
        stored = self.db.execute(CELL, key).fetchone()
        if stored is None:
            return None
        if stored[1] == "unmaintained":
            raise FormulaError("#REF!", "Unmaintained formula")
        return typed({"source": stored[0], "kind": stored[1], "style": {}})["value"]

    def range_geometry(self, node, sheet):
        # Calculator.range_geometry without its 10,000-cell bound.
        if node[0] == "error":
            raise FormulaError(node[1])
        if node[0] == "ref":
            target = self.reference(node[1], sheet)
            return target[0], target[1], target[2], target[1], target[2]
        if node[0] != "range":
            raise FormulaError("#VALUE!", "Expected a cell or rectangular range")
        if node[2][0] != "ref" or node[3][0] != "ref":
            raise FormulaError("#REF!", "Range endpoints must be cell references")
        first = self.reference(node[2][1], sheet)
        last = self.reference(node[3][1], first[0] if split_ref(node[3][1])[0] is None else sheet)
        if first[0] != last[0]:
            raise FormulaError("#REF!", "3D ranges are unsupported")
        (r1, r2), (c1, c2) = sorted((first[1], last[1])), sorted((first[2], last[2]))
        return first[0], r1, c1, r2, c2

    def follow(self, key):
        """The value of an unordered formula, reached from the formulas in self.following.

        Meeting a formula that is already being followed is the cycle. A result is kept only if nothing
        beneath it met one: a result that did depends on where evaluation entered the cycle.
        """
        if key in self.following:
            self.cycle_hits += 1
            return FormulaError("#CYCLE!", "Circular reference")
        hits = self.cycle_hits
        self.following.append(key)
        try:
            result = self.calculate(key)
        finally:
            self.following.pop()
        if self.cycle_hits == hits:
            self.unordered.discard(key)
            self.results[key] = result
        return result

    def formula(self, key):
        self.cells_read = 0
        if key in self.unordered:
            return self.follow(key)
        return self.calculate(key)

    def calculate(self, key):
        try:
            source = self.db.execute(CELL, key).fetchone()[0]
            result = self.evaluate(Parser(source[1:]).parse(), key[0])
            if isinstance(result, list):
                raise FormulaError("#VALUE!", "Range cannot be a scalar result")
            if isinstance(result, (int, float)) and not math.isfinite(result):
                raise FormulaError("#NUM!", "Non-finite result")
            return result
        except OverflowError:
            return FormulaError("#NUM!", "Numeric overflow")
        except RecursionError:
            return FormulaError("#LIMIT!", "Calculation nesting limit exceeded")
        except FormulaError as error:
            return error


def evaluate(db, data, cells, results, *, cell_limit=2_000_000):
    """Results for the formulas in cells, evaluated in dependency order; each is a value or a FormulaError.

    results holds the values that are already current. Nothing is timed: the only limit is cell_limit.
    """
    ordered, cyclic = data.order(cells)
    new = {}
    calculator = OrderedCalculator(db, data, dict(results) if results else {}, cell_limit, cyclic)
    current = calculator.results
    for key in ordered:
        current[key] = new[key] = calculator.formula(key)
    for key in sorted(cyclic):
        current[key] = new[key] = calculator.formula(key)
    return new


def stream_sum(db, results, rect):
    """SUM over one rectangle from a single range scan, taking formula cells' values from results."""
    sheet, r1, c1, r2, c2 = rect
    total = 0
    for row, col, source, kind in db.execute(
            "SELECT row,col,source,kind FROM cells WHERE sheet_id=? AND row BETWEEN ? AND ? AND col BETWEEN ? AND ? "
            "ORDER BY row,col",
            (sheet, r1, r2, c1, c2)):
        if kind == "number":
            value = float(source)
        elif kind == "formula" or (kind == "auto" and source.startswith("=")):
            value = results[(sheet, row, col)]
            if isinstance(value, FormulaError):
                raise value
        elif kind == "unmaintained":
            raise FormulaError("#REF!", "Unmaintained formula")
        else:
            value = typed({"source": source, "kind": kind, "style": {}})["value"]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += value
            if not math.isfinite(total):
                raise FormulaError("#NUM!", "SUM exceeds the finite numeric range")
    return total
