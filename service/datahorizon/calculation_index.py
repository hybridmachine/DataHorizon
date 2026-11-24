# SPDX-License-Identifier: GPL-3.0-only
"""Compact in-memory dependents index with centered interval-tree range lookup."""

from collections import defaultdict

from .calculation_store import BLOCK_ROWS

STORED_FORMULA_LIMIT = 140_000  # provisional; fixed by measurement in Task 11
_WIDE_COLS = 16  # ranges narrower than this are filed per column; wider ones per sheet

_COL_MASK = 0x7FFF
_ROW_SHIFT = 15
_ROW_MASK = 0x1FFFFF
_SHEET_SHIFT = 36


def _pack(snum: int, row: int, col: int) -> int:
    return (snum << _SHEET_SHIFT) | (row << _ROW_SHIFT) | col


def _unpack(packed: int) -> tuple[int, int, int]:
    return packed >> _SHEET_SHIFT, (packed >> _ROW_SHIFT) & _ROW_MASK, packed & _COL_MASK


class _IntervalNode:
    __slots__ = ("mid", "by_start", "by_end", "left", "right")

    def __init__(self, mid: int, by_start: list, by_end: list, left, right):
        self.mid = mid
        self.by_start = by_start
        self.by_end = by_end
        self.left = left
        self.right = right


def _build_interval_tree(items: list) -> _IntervalNode | None:
    """Build a centered interval tree from items = [(r1, r2, payload), ...]."""
    if not items:
        return None
    # Median of interval endpoints balances the tree.
    endpoints = []
    for r1, r2, _ in items:
        endpoints.append(r1)
        endpoints.append(r2)
    endpoints.sort()
    mid = endpoints[len(endpoints) // 2]

    left_items = []
    right_items = []
    center = []
    for item in items:
        if item[1] < mid:
            left_items.append(item)
        elif item[0] > mid:
            right_items.append(item)
        else:
            center.append(item)

    by_start = sorted(center, key=lambda x: x[0])
    by_end = sorted(center, key=lambda x: x[1], reverse=True)
    return _IntervalNode(
        mid,
        by_start,
        by_end,
        _build_interval_tree(left_items),
        _build_interval_tree(right_items),
    )


class _ColumnTree:
    """Intervals in one (sheet, col): dict[(r1, r2), set[packed_reader]] + lazy centered interval tree."""
    __slots__ = ("ranges", "tree")

    def __init__(self):
        self.ranges: dict[tuple[int, int], set[int]] = {}
        self.tree: _IntervalNode | None = None

    def add(self, r1: int, r2: int, reader: int) -> None:
        span = (r1, r2)
        readers = self.ranges.get(span)
        if readers is None:
            self.ranges[span] = {reader}
            self.tree = None
        else:
            readers.add(reader)

    def remove(self, r1: int, r2: int, reader: int) -> bool:
        span = (r1, r2)
        readers = self.ranges.get(span)
        if readers is not None:
            readers.discard(reader)
            if not readers:
                del self.ranges[span]
                self.tree = None
        return not self.ranges

    def ensure_tree(self) -> _IntervalNode | None:
        if self.tree is None and self.ranges:
            self.tree = _build_interval_tree([(r1, r2, readers) for (r1, r2), readers in self.ranges.items()])
        return self.tree


class _WideTree:
    """Wide rectangles (>= _WIDE_COLS columns) on one sheet, indexed by row interval."""
    __slots__ = ("ranges", "tree")

    def __init__(self):
        self.ranges: dict[tuple[int, int, int, int], set[int]] = {}
        self.tree: _IntervalNode | None = None

    def add(self, r1: int, c1: int, r2: int, c2: int, reader: int) -> None:
        rect = (r1, c1, r2, c2)
        readers = self.ranges.get(rect)
        if readers is None:
            self.ranges[rect] = {reader}
            self.tree = None
        else:
            readers.add(reader)

    def remove(self, r1: int, c1: int, r2: int, c2: int, reader: int) -> bool:
        rect = (r1, c1, r2, c2)
        readers = self.ranges.get(rect)
        if readers is not None:
            readers.discard(reader)
            if not readers:
                del self.ranges[rect]
                self.tree = None
        return not self.ranges

    def ensure_tree(self) -> _IntervalNode | None:
        if self.tree is None and self.ranges:
            self.tree = _build_interval_tree(
                [(r1, r2, (c1, c2, readers)) for (r1, c1, r2, c2), readers in self.ranges.items()]
            )
        return self.tree


class DependentsIndex:
    """In-memory reverse dependency index over integer-packed cell keys and interval trees."""

    def __init__(self, precedents=()):
        self._sheet_to_num: dict[str, int] = {}
        self._num_to_sheet: list[str] = []
        # packed formula key -> tuple of packed single-cell int or (p_snum, r1, c1, r2, c2)
        self._edges: dict[int, tuple] = {}
        # packed target cell -> single packed reader int OR set[int]
        self._single: dict[int, int | set[int]] = {}
        # snum -> block -> set[packed target cell] for rectangle root lookups
        self._single_by_block: dict[int, dict[int, set[int]]] = {}
        # snum -> col -> _ColumnTree
        self._col_trees: dict[int, dict[int, _ColumnTree]] = {}
        # snum -> _WideTree
        self._wide_trees: dict[int, _WideTree] = {}
        self.checks = 0

        if precedents:
            grouped: dict[tuple, list] = defaultdict(list)
            for key, rect in precedents:
                grouped[key].append(rect)
            for key, rects in grouped.items():
                self.link(key, rects)

    def _sheet_num(self, sheet_id: str) -> int:
        num = self._sheet_to_num.get(sheet_id)
        if num is None:
            num = len(self._num_to_sheet)
            self._sheet_to_num[sheet_id] = num
            self._num_to_sheet.append(sheet_id)
        return num

    def _add_single(self, target: int, reader: int, p_snum: int, row: int) -> None:
        existing = self._single.get(target)
        if existing is None:
            self._single[target] = reader
            by_block = self._single_by_block.get(p_snum)
            if by_block is None:
                by_block = {}
                self._single_by_block[p_snum] = by_block
            blk = (row - 1) // BLOCK_ROWS
            bucket = by_block.get(blk)
            if bucket is None:
                by_block[blk] = {target}
            else:
                bucket.add(target)
        elif type(existing) is int:
            if existing != reader:
                self._single[target] = {existing, reader}
        else:
            existing.add(reader)

    def _remove_single(self, target: int, reader: int) -> None:
        existing = self._single.get(target)
        if existing is None:
            return
        if type(existing) is int:
            if existing == reader:
                del self._single[target]
                self._drop_single_block(target)
        else:
            existing.discard(reader)
            if len(existing) == 1:
                self._single[target] = next(iter(existing))
            elif not existing:
                del self._single[target]
                self._drop_single_block(target)

    def _drop_single_block(self, target: int) -> None:
        p_snum, row, _ = _unpack(target)
        by_block = self._single_by_block.get(p_snum)
        if by_block is None:
            return
        blk = (row - 1) // BLOCK_ROWS
        bucket = by_block.get(blk)
        if bucket is not None:
            bucket.discard(target)
            if not bucket:
                del by_block[blk]
                if not by_block:
                    del self._single_by_block[p_snum]

    def link(self, key, rectangles) -> None:
        """Replace key's precedent rectangles."""
        snum = self._sheet_num(key[0])
        packed_key = _pack(snum, key[1], key[2])
        encoded = []
        seen = set()
        for p_sheet, r1, c1, r2, c2 in rectangles:
            p_snum = self._sheet_num(p_sheet)
            if r1 == r2 and c1 == c2:
                edge = _pack(p_snum, r1, c1)
            else:
                edge = (p_snum, r1, c1, r2, c2)
            if edge not in seen:
                seen.add(edge)
                encoded.append(edge)
        new_edges = tuple(encoded)
        old_edges = self._edges.get(packed_key)
        if old_edges is not None:
            if old_edges == new_edges:
                return
            self._unlink_packed(packed_key, old_edges)
        self._edges[packed_key] = new_edges
        for edge in new_edges:
            if type(edge) is int:
                p_snum, row, _ = _unpack(edge)
                self._add_single(edge, packed_key, p_snum, row)
            else:
                p_snum, r1, c1, r2, c2 = edge
                if c2 - c1 < _WIDE_COLS:
                    cols = self._col_trees.get(p_snum)
                    if cols is None:
                        cols = {}
                        self._col_trees[p_snum] = cols
                    for col in range(c1, c2 + 1):
                        ct = cols.get(col)
                        if ct is None:
                            ct = _ColumnTree()
                            cols[col] = ct
                        ct.add(r1, r2, packed_key)
                else:
                    wt = self._wide_trees.get(p_snum)
                    if wt is None:
                        wt = _WideTree()
                        self._wide_trees[p_snum] = wt
                    wt.add(r1, c1, r2, c2, packed_key)

    def _unlink_packed(self, packed_key: int, old_edges: tuple) -> None:
        for edge in old_edges:
            if type(edge) is int:
                self._remove_single(edge, packed_key)
            else:
                p_snum, r1, c1, r2, c2 = edge
                if c2 - c1 < _WIDE_COLS:
                    cols = self._col_trees.get(p_snum)
                    if cols is not None:
                        for col in range(c1, c2 + 1):
                            ct = cols.get(col)
                            if ct is not None and ct.remove(r1, r2, packed_key):
                                del cols[col]
                        if not cols:
                            del self._col_trees[p_snum]
                else:
                    wt = self._wide_trees.get(p_snum)
                    if wt is not None and wt.remove(r1, c1, r2, c2, packed_key):
                        del self._wide_trees[p_snum]

    def unlink(self, key) -> None:
        snum = self._sheet_to_num.get(key[0])
        if snum is None:
            return
        packed_key = _pack(snum, key[1], key[2])
        old_edges = self._edges.pop(packed_key, None)
        if old_edges is not None:
            self._unlink_packed(packed_key, old_edges)

    def __len__(self) -> int:
        return len(self._edges)

    def _query_col_tree(self, root: _IntervalNode | None, q_r1: int, q_r2: int, left_done: dict, right_done: dict, emit) -> None:
        """Query a column's centered interval tree for [q_r1, q_r2], skipping intervals already emitted in this call."""
        stack = [root] if root is not None else []
        checks = 0
        while stack:
            node = stack.pop()
            checks += 1
            mid = node.mid
            if q_r1 <= mid <= q_r2:
                by_start = node.by_start
                start_idx = left_done.get(node, 0)
                n = len(by_start)
                if start_idx < n:
                    for i in range(start_idx, n):
                        checks += 1
                        emit(by_start[i][2])
                    left_done[node] = n
                    right_done[node] = n
                if node.left is not None:
                    stack.append(node.left)
                if node.right is not None:
                    stack.append(node.right)
            elif q_r2 < mid:
                by_start = node.by_start
                idx = left_done.get(node, 0)
                n = len(by_start)
                while idx < n:
                    checks += 1
                    item = by_start[idx]
                    if item[0] > q_r2:
                        break
                    emit(item[2])
                    idx += 1
                left_done[node] = idx
                if node.left is not None:
                    stack.append(node.left)
            else:
                # q_r1 > mid
                by_end = node.by_end
                idx = right_done.get(node, 0)
                n = len(by_end)
                while idx < n:
                    checks += 1
                    item = by_end[idx]
                    if item[1] < q_r1:
                        break
                    emit(item[2])
                    idx += 1
                right_done[node] = idx
                if node.right is not None:
                    stack.append(node.right)
        self.checks += checks

    def _query_wide_tree(self, root: _IntervalNode | None, q_r1: int, q_c1: int, q_r2: int, q_c2: int, emit) -> None:
        stack = [root] if root is not None else []
        checks = 0
        while stack:
            node = stack.pop()
            checks += 1
            mid = node.mid
            if q_r1 <= mid <= q_r2:
                for _, _, (c1, c2, readers) in node.by_start:
                    checks += 1
                    if c1 <= q_c2 and q_c1 <= c2:
                        emit(readers)
                if node.left is not None:
                    stack.append(node.left)
                if node.right is not None:
                    stack.append(node.right)
            elif q_r2 < mid:
                for r1, _, (c1, c2, readers) in node.by_start:
                    checks += 1
                    if r1 > q_r2:
                        break
                    if c1 <= q_c2 and q_c1 <= c2:
                        emit(readers)
                if node.left is not None:
                    stack.append(node.left)
            else:
                for _, r2, (c1, c2, readers) in node.by_end:
                    checks += 1
                    if r2 < q_r1:
                        break
                    if c1 <= q_c2 and q_c1 <= c2:
                        emit(readers)
                if node.right is not None:
                    stack.append(node.right)
        self.checks += checks

    def affected(self, rectangles) -> set:
        """Every formula with a precedent rectangle meeting one of rectangles, and their readers, transitively."""
        affected_packed: set[int] = set()
        frontier: list[int] = []
        left_done: dict[_IntervalNode, int] = {}
        right_done: dict[_IntervalNode, int] = {}

        def emit_readers(readers) -> None:
            if type(readers) is int:
                if readers not in affected_packed:
                    affected_packed.add(readers)
                    frontier.append(readers)
            else:
                for reader in readers:
                    if reader not in affected_packed:
                        affected_packed.add(reader)
                        frontier.append(reader)

        def query_rect(snum: int, r1: int, c1: int, r2: int, c2: int) -> None:
            # 1. Single-cell precedents
            if r1 == r2 and c1 == c2:
                readers = self._single.get(_pack(snum, r1, c1))
                if readers is not None:
                    emit_readers(readers)
            else:
                by_block = self._single_by_block.get(snum)
                if by_block:
                    b1 = (r1 - 1) // BLOCK_ROWS
                    b2 = (r2 - 1) // BLOCK_ROWS
                    if b2 - b1 + 1 <= len(by_block):
                        blks = range(b1, b2 + 1)
                    else:
                        blks = [b for b in by_block if b1 <= b <= b2]
                    for blk in blks:
                        bucket = by_block.get(blk)
                        if not bucket:
                            continue
                        for target in bucket:
                            _, tr, tc = _unpack(target)
                            if r1 <= tr <= r2 and c1 <= tc <= c2:
                                readers = self._single.get(target)
                                if readers is not None:
                                    emit_readers(readers)

            # 2. Narrow per-column range trees
            cols = self._col_trees.get(snum)
            if cols:
                if c1 == c2:
                    ct = cols.get(c1)
                    if ct is not None:
                        self._query_col_tree(ct.ensure_tree(), r1, r2, left_done, right_done, emit_readers)
                else:
                    if c2 - c1 + 1 <= len(cols):
                        col_iter = range(c1, c2 + 1)
                    else:
                        col_iter = [col for col in cols if c1 <= col <= c2]
                    for col in col_iter:
                        ct = cols.get(col)
                        if ct is not None:
                            self._query_col_tree(ct.ensure_tree(), r1, r2, left_done, right_done, emit_readers)

            # 3. Wide per-sheet range tree
            wt = self._wide_trees.get(snum)
            if wt is not None:
                self._query_wide_tree(wt.ensure_tree(), r1, c1, r2, c2, emit_readers)

        for sheet_id, r1, c1, r2, c2 in rectangles:
            snum = self._sheet_to_num.get(sheet_id)
            if snum is None:
                continue
            query_rect(snum, r1, c1, r2, c2)

        single = self._single
        col_trees = self._col_trees
        wide_trees = self._wide_trees
        while frontier:
            cell = frontier.pop()
            readers = single.get(cell)
            if readers is not None:
                emit_readers(readers)
            snum = cell >> _SHEET_SHIFT
            row = (cell >> _ROW_SHIFT) & _ROW_MASK
            col = cell & _COL_MASK
            cols = col_trees.get(snum)
            if cols is not None:
                ct = cols.get(col)
                if ct is not None:
                    self._query_col_tree(ct.ensure_tree(), row, row, left_done, right_done, emit_readers)
            wt = wide_trees.get(snum)
            if wt is not None:
                self._query_wide_tree(wt.ensure_tree(), row, col, row, col, emit_readers)

        num_to_sheet = self._num_to_sheet
        return {
            (num_to_sheet[p >> _SHEET_SHIFT], (p >> _ROW_SHIFT) & _ROW_MASK, p & _COL_MASK)
            for p in affected_packed
        }
