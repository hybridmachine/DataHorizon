# SPDX-License-Identifier: GPL-3.0-only
"""Calculators that collect storable formula results and serve stored results."""

from .formula import CELL_ERRORS, Calculator, FormulaError, Parser


class CollectingCalculator(Calculator):
    """Calculator that extracts precedents and collects storable formula results in completion order.

    A formula may be stored when its result is a value or a CELL_ERRORS error, it is neither
    volatile (NOW/TODAY) nor unparseable, and no precedent rectangle of it contains a cell
    that this calculator evaluated and found not storable.
    """

    def __init__(self, read, resolve_sheet, seconds=0.25, *, clock=None, scan=None, decode=None):
        self._base_read = read
        self._formulas: set[tuple] = set()
        self._precedents: dict[tuple, tuple] = {}
        self._unstorable: set[tuple] = set()
        self._unstorable_rows: dict[str, dict[int, set[int]]] = {}
        self._unstorable_cols: dict[str, dict[int, set[int]]] = {}
        self.batch: list[tuple] = []
        super().__init__(self._input, resolve_sheet, seconds, clock=clock, scan=scan, decode=decode)

    def _mark_unstorable(self, key: tuple) -> None:
        if key in self._unstorable:
            return
        sheet, row, col = key
        self._unstorable.add(key)
        self._unstorable_rows.setdefault(sheet, {}).setdefault(row, set()).add(col)
        self._unstorable_cols.setdefault(sheet, {}).setdefault(col, set()).add(row)

    def _has_unstorable_in(self, sheet: str, r1: int, c1: int, r2: int, c2: int) -> bool:
        rows = self._unstorable_rows.get(sheet)
        if not rows:
            return False
        cols = self._unstorable_cols[sheet]
        if min(r2 - r1 + 1, len(rows)) <= min(c2 - c1 + 1, len(cols)):
            lines, a, b, x, y = rows, r1, r2, c1, c2
        else:
            lines, a, b, x, y = cols, c1, c2, r1, r2
        for line in (range(a, b + 1) if b - a + 1 <= len(lines) else lines):
            others = lines.get(line) if a <= line <= b else None
            if not others:
                continue
            if y - x + 1 <= len(others):
                for other in range(x, y + 1):
                    if other in others:
                        return True
            else:
                for other in others:
                    if x <= other <= y:
                        return True
        return False

    def _input(self, sheet, row, col):
        item = self._base_read(sheet, row, col)
        key = (sheet, row, col)
        if item and item["kind"] == "formula":
            self._formulas.add(key)
            targets = set()
            volatile = False
            unsafe = False
            try:
                pending = [Parser(item["source"][1:]).parse()]
                while pending:
                    self.check()
                    node = pending.pop()
                    kind = node[0]
                    if kind in ("ref", "range"):
                        targets.add(self.range_geometry(node, sheet))
                    elif kind == "call":
                        volatile = volatile or node[1] in ("TODAY", "NOW")
                        pending.extend(node[2])
                    elif kind == "binary":
                        pending.extend(node[2:])
                    elif kind == "unary":
                        pending.append(node[2])
            except (FormulaError, RecursionError):
                unsafe = True
            self._precedents[key] = tuple(targets)
            if volatile or unsafe:
                self._mark_unstorable(key)
        return item

    def remember(self, key, result):
        super().remember(key, result)
        if key not in self._formulas:
            return
        if isinstance(result, FormulaError) and result.code not in CELL_ERRORS:
            self._mark_unstorable(key)
            return
        if key in self._unstorable:
            return
        rects = self._precedents.get(key, ())
        for sheet, r1, c1, r2, c2 in rects:
            if r1 == r2 and c1 == c2:
                if (sheet, r1, c1) in self._unstorable:
                    self._mark_unstorable(key)
                    return
            elif self._has_unstorable_in(sheet, r1, c1, r2, c2):
                self._mark_unstorable(key)
                return
        self.batch.append((key, result, rects))


class StoreCalculator(CollectingCalculator):
    """CollectingCalculator that serves already-stored results from book.store.

    Streamed ranges fetch stored formula results of each 1,024-row band in one results_in call.
    Results taken from the store are never put into batch.
    """

    _PREFETCH_CAP = 16_384

    def __init__(self, book, seconds=0.25, clock=None):
        from .engine import EngineError, stored_item, typed

        self._book = book
        self._store = book.store
        self._unserved = book.unserved
        self._prefetched: dict[tuple, object] = {}

        def resolve(name):
            try:
                return book.sheet(name)
            except EngineError as error:
                raise FormulaError("#REF!", "Referenced sheet does not exist") from error

        read = lambda s, r, c: typed(book.raw(s, r, c))
        base_scan = book.scan

        def scan_with_store(target, r1, c1, r2, c2):
            if self._store is not None and not self._book.distrusted:
                stored_rows = self._store.results_in((target, r1, c1, r2, c2))
                if stored_rows:
                    if len(self._prefetched) + len(stored_rows) > self._PREFETCH_CAP:
                        self._prefetched.clear()
                    self._prefetched.update(stored_rows)
            yield from base_scan(target, r1, c1, r2, c2)

        super().__init__(read, resolve, seconds, clock=clock, scan=scan_with_store, decode=stored_item)

    def _can_serve(self, key: tuple) -> bool:
        return self._store is not None and not self._book.distrusted and key not in self._unserved

    def result(self, key):
        if key in self.active:
            return FormulaError("#CYCLE!", "Circular reference")
        if key in self.cache:
            return self.cache[key]
        if self._can_serve(key):
            if key in self._prefetched:
                return self._prefetched[key]
            found, value = self._store.result(key)
            if found:
                self.cache[key] = value
                return value
        return super().result(key)
