# SPDX-License-Identifier: GPL-3.0-only
"""Disposable bounded dependency graph and nonvolatile result reuse."""
from collections import defaultdict
from .formula import Calculator, Parser, FormulaError, CELL_ERRORS


INVALIDATION_CHECKS=100_000  # rectangle checks one invalidate may spend: two edited cells against a cache at its edge cap


class DependencyIndex:
    """edges[key] holds the rectangles (sheet, r1, c1, r2, c2) the formula at key reads, a single cell as a rectangle of
    one. Single cells are found again through reverse[cell]; larger rectangles through ranges[sheet][(r1, c1, r2, c2)],
    each checked for containment (counted in checks). uncacheable holds the cells whose result must not be kept: the
    volatile and unsafe formulas, those that read one of them (found when their result was saved), and those whose
    evaluation ended in an error that is not kept, so that a reader of a cell whose status is unknown is not kept.
    Every cell in uncacheable has edges, so the node cap bounds it; rows[sheet][row] and cols[sheet][col] hold the same
    cells by line, to tell whether a rectangle holds one without walking them all (mark, unmark, marked)."""
    def __init__(self):
        self.checks=0
        self.clear()

    def clear(self):
        self.edges={};self.edge_count=0;self.reverse=defaultdict(set);self.ranges=defaultdict(dict)
        self.volatile=set();self.unsafe=set();self.uncacheable=set();self.rows={};self.cols={}
        self.values={};self.sizes={};self.bytes=0;self.hits=0;self.invalidated=0

    def link(self,key,rectangles):
        self.edge_count+=len(rectangles)-len(self.edges.get(key,()))
        self.unlink(key);self.edges[key]=rectangles
        for sheet,r1,c1,r2,c2 in rectangles:
            if r1==r2 and c1==c2:self.reverse[sheet,r1,c1].add(key)
            else:self.ranges[sheet].setdefault((r1,c1,r2,c2),set()).add(key)

    def unlink(self,key):
        for sheet,r1,c1,r2,c2 in self.edges.pop(key,()):
            readers,target=(self.reverse,(sheet,r1,c1)) if r1==r2 and c1==c2 else (self.ranges[sheet],(r1,c1,r2,c2))
            readers[target].discard(key)
            if not readers[target]:readers.pop(target)
            if not self.ranges.get(sheet,True):self.ranges.pop(sheet)

    def forget(self,key):
        self.bytes-=self.sizes.pop(key,0);self.values.pop(key,None)
        self.edge_count-=len(self.edges.get(key,()));self.unlink(key)
        self.volatile.discard(key);self.unsafe.discard(key);self.unmark(key)

    def mark(self,key):
        if key in self.uncacheable:return
        sheet,row,col=key;self.uncacheable.add(key)
        self.rows.setdefault(sheet,{}).setdefault(row,set()).add(col);self.cols.setdefault(sheet,{}).setdefault(col,set()).add(row)

    def unmark(self,key):
        if key not in self.uncacheable:return
        sheet,row,col=key;self.uncacheable.discard(key)
        for lines,line,other in ((self.rows,row,col),(self.cols,col,row)):
            lines[sheet][line].discard(other)
            if not lines[sheet][line]:del lines[sheet][line]
            if not lines[sheet]:del lines[sheet]

    def marked(self,sheet,r1,c1,r2,c2):
        """Whether an uncacheable cell lies in the rectangle. Looks along rows or along columns, whichever has fewer
        lines to try: the rectangle's own lines, or the sheet's lines that hold an uncacheable cell if those are fewer;
        and within a line, the rectangle's width or the line's cells, whichever is fewer. A column of volatile cells
        beside a range, in its rows or not, costs one or two checks."""
        rows=self.rows.get(sheet)
        if not rows:return False
        cols=self.cols[sheet]
        if min(r2-r1+1,len(rows))<=min(c2-c1+1,len(cols)):lines,a,b,x,y=rows,r1,r2,c1,c2
        else:lines,a,b,x,y=cols,c1,c2,r1,r2
        for line in (range(a,b+1) if b-a+1<=len(lines) else lines):
            self.checks+=1;others=lines.get(line) if a<=line<=b else None
            if not others:continue
            if y-x+1<=len(others):
                for other in range(x,y+1):
                    self.checks+=1
                    if other in others:return True
            else:
                for other in others:
                    self.checks+=1
                    if x<=other<=y:return True
        return False

    def invalidate(self,keys):
        """Forget keys, the formulas that read them and so on, and return all of those. Readers through a single-cell
        edge are followed first, at no cost in checks. The cells gathered that way are then looked up together, sheet
        by sheet: each against every rectangle on its sheet while the call has spent no more than INVALIDATION_CHECKS
        checks; past that, their bounding box against each rectangle, once, so that every reader whose rectangle meets
        the box is forgotten. That is exact for one cell and for a filled block, and forgets more than it must for
        scattered cells, which is always safe. The readers found are followed in the same way. A call that is past the
        budget after a step (formulas chained through ranges) clears the whole index."""
        affected=set(keys);frontier=list(affected);spent=0
        while frontier:
            sheets=defaultdict(list)
            while frontier:
                key=frontier.pop()
                if key[0] in self.ranges:sheets[key[0]].append(key)
                for reader in self.reverse.get(key,()):
                    if reader not in affected:affected.add(reader);frontier.append(reader)
            found=set()
            for sheet,cells in sheets.items():
                rectangles=self.ranges[sheet];cost=len(cells)*len(rectangles)
                if spent+cost<=INVALIDATION_CHECKS:
                    spent+=cost
                    for (r1,c1,r2,c2),readers in rectangles.items():
                        for _,row,col in cells:
                            self.checks+=1
                            if r1<=row<=r2 and c1<=col<=c2:found|=readers;break
                else:
                    spent+=len(rectangles);self.checks+=len(rectangles)
                    top=min(cell[1] for cell in cells);bottom=max(cell[1] for cell in cells)
                    left=min(cell[2] for cell in cells);right=max(cell[2] for cell in cells)
                    for (r1,c1,r2,c2),readers in rectangles.items():
                        if r1<=bottom and top<=r2 and c1<=right and left<=c2:found|=readers
            if spent>INVALIDATION_CHECKS:
                affected|=self.edges.keys();self.clear();return affected
            frontier=list(found-affected);affected.update(frontier)
        for key in affected:self.forget(key)
        self.invalidated+=len(affected)
        return affected

    def cacheable(self,key):
        """Whether key's result may be kept: key is neither volatile nor unsafe and no cell it reads directly, alone or
        inside one of its rectangles, is uncacheable. One level suffices: a reader is saved only after every formula it
        read was saved (its own direct reads checked the same way) or marked uncacheable, or came from values."""
        if key in self.volatile or key in self.unsafe:return False
        for sheet,r1,c1,r2,c2 in self.edges.get(key,()):
            self.checks+=1
            if (sheet,r1,c1) in self.uncacheable if r1==r2 and c1==c2 else self.marked(sheet,r1,c1,r2,c2):return False
        return True

    def drop(self,key):
        """Mark key's result as one never to keep, keeping its edges so that edits still reach its readers."""
        self.bytes-=self.sizes.pop(key,0);self.values.pop(key,None);self.mark(key)

    def calculator(self,read,resolve,seconds,clock=None,*,scan=None,decode=None):
        index=self
        class IndexedCalculator(Calculator):
            def __init__(self):
                super().__init__(self.input,resolve,seconds,clock=clock,scan=scan,decode=decode)
                self.cache=dict(index.values);self.disabled=False

            def input(self,sheet,row,col):
                item=read(sheet,row,col);key=(sheet,row,col);targets=set();volatile=unsafe=False
                if item and item['kind']=='formula':
                    try:
                        pending=[Parser(item['source'][1:]).parse()]
                        while pending:
                            self.check();node=pending.pop()
                            if node[0] in ('ref','range'):targets.add(self.range_geometry(node,sheet))  # one rectangle per reference
                            elif node[0]=='call':
                                volatile=volatile or node[1] in ('TODAY','NOW')
                                pending.extend(node[2])
                            elif node[0]=='binary':pending.extend(node[2:])
                            elif node[0]=='unary':pending.append(node[2])
                    except FormulaError:unsafe=True
                if not self.disabled:
                    if len(index.edges)>=4096 or index.edge_count+len(targets)>50000:
                        index.clear();self.disabled=True
                    else:
                        # Nothing is recorded once disabled: a mark without edges would outlive the read that made it.
                        index.link(key,targets)
                        if volatile:index.volatile.add(key)
                        if unsafe:index.unsafe.add(key)
                        if volatile or unsafe:index.mark(key)
                return item

            def cell(self,sheet,row,col):
                # A result already held was decided when it was saved; one completed now is saved by remember.
                if (sheet,row,col) in self.cache:index.hits+=1
                return super().cell(sheet,row,col)

            def remember(self,key,result):
                # Formulas evaluated on the way to the one read no longer pass through cell().
                super().remember(key,result)
                if not isinstance(result,FormulaError) or result.code in CELL_ERRORS:self.save(key,result)
                elif not self.disabled:index.drop(key)  # #LIMIT!, #CYCLE!: what reads it is not kept either

            def save(self,key,result):
                if self.disabled:return
                index.unmark(key)  # its status is decided again from its direct reads
                if not index.cacheable(key):index.drop(key);return
                size=64+len(str(result).encode('utf-8'))
                if key not in index.values and (len(index.values)>=4096 or index.bytes+size>4*1024*1024):
                    index.clear();self.disabled=True;return
                index.bytes+=size-index.sizes.get(key,0);index.sizes[key]=size;index.values[key]=result
        return IndexedCalculator()

    def info(self):
        return dict(nodes=len(self.edges),edges=sum(map(len,self.edges.values())),cached=len(self.values),bytes=self.bytes,hits=self.hits,invalidated=self.invalidated)
