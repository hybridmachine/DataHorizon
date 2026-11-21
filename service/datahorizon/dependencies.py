# SPDX-License-Identifier: GPL-3.0-only
"""Disposable bounded dependency graph and nonvolatile result reuse."""
from collections import defaultdict
from .formula import Calculator, Parser, FormulaError, CELL_ERRORS


class DependencyIndex:
    """edges[key] holds the rectangles (sheet, r1, c1, r2, c2) the formula at key reads, a single cell as a rectangle of
    one. Single cells are found again through reverse[cell]; larger rectangles through ranges[sheet][(r1, c1, r2, c2)],
    each checked for containment (counted in checks). uncacheable holds formulas found to read a volatile or
    unparseable one when their result was saved: a reader is saved only after the formulas it read have completed."""
    def __init__(self):
        self.checks=0
        self.clear()

    def clear(self):
        self.edges={};self.edge_count=0;self.reverse=defaultdict(set);self.ranges=defaultdict(dict)
        self.volatile=set();self.unsafe=set();self.uncacheable=set()
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
        self.volatile.discard(key);self.unsafe.discard(key);self.uncacheable.discard(key)

    def readers(self,key):
        """The formulas that read the cell key, through a single-cell edge or a rectangle containing it."""
        sheet,row,col=key;found=set(self.reverse.get(key,()))
        for (r1,c1,r2,c2),keys in self.ranges.get(sheet,{}).items():
            self.checks+=1
            if r1<=row<=r2 and c1<=col<=c2:found|=keys
        return found

    def invalidate(self,keys):
        affected=set(keys);pending=list(affected)
        while pending:
            for parent in self.readers(pending.pop()):
                if parent not in affected:affected.add(parent);pending.append(parent)
        for key in affected:self.forget(key)
        self.invalidated+=len(affected)
        return affected

    def cacheable(self,key):
        pending=[key];seen=set();tainted=self.volatile|self.unsafe|self.uncacheable
        while pending:
            item=pending.pop()
            if item in seen:continue
            seen.add(item)
            if item in tainted:return False
            for sheet,r1,c1,r2,c2 in self.edges.get(item,()):
                if r1==r2 and c1==c2:pending.append((sheet,r1,c1));continue
                for other in tainted:
                    self.checks+=1
                    if other[0]==sheet and r1<=other[1]<=r2 and c1<=other[2]<=c2:return False
        return True

    def calculator(self,read,resolve,seconds,clock=None,*,scan=None):
        index=self
        class IndexedCalculator(Calculator):
            def __init__(self):
                super().__init__(self.input,resolve,seconds,clock=clock,scan=scan)
                self.cache=dict(index.values);self.disabled=False

            def input(self,sheet,row,col):
                item=read(sheet,row,col);key=(sheet,row,col);targets=set()
                if item and item['kind']=='formula':
                    try:
                        pending=[Parser(item['source'][1:]).parse()]
                        while pending:
                            self.check();node=pending.pop()
                            if node[0] in ('ref','range'):targets.add(self.range_geometry(node,sheet))  # one rectangle per reference
                            elif node[0]=='call':
                                if node[1] in ('TODAY','NOW'):index.volatile.add(key)
                                pending.extend(node[2])
                            elif node[0]=='binary':pending.extend(node[2:])
                            elif node[0]=='unary':pending.append(node[2])
                    except FormulaError:index.unsafe.add(key)
                if not self.disabled:
                    if len(index.edges)>=4096 or index.edge_count+len(targets)>50000:
                        index.clear();self.disabled=True
                    else:
                        index.link(key,targets)
                return item

            def cell(self,sheet,row,col):
                key=(sheet,row,col)
                if key in self.cache:index.hits+=1
                try:
                    result=super().cell(sheet,row,col)
                except FormulaError as error:
                    if error.code not in CELL_ERRORS:raise
                    result=error
                    self.save(key,result)
                    raise
                self.save(key,result)
                return result

            def remember(self,key,result):
                # Formulas evaluated on the way to the one read no longer pass through cell().
                super().remember(key,result)
                if not isinstance(result,FormulaError) or result.code in CELL_ERRORS:self.save(key,result)

            def save(self,key,result):
                if self.disabled:return
                if not index.cacheable(key):index.uncacheable.add(key);return
                size=64+len(str(result).encode('utf-8'))
                if key not in index.values and (len(index.values)>=4096 or index.bytes+size>4*1024*1024):
                    index.clear();self.disabled=True;return
                index.bytes+=size-index.sizes.get(key,0);index.sizes[key]=size;index.values[key]=result
        return IndexedCalculator()

    def info(self):
        return dict(nodes=len(self.edges),edges=sum(map(len,self.edges.values())),cached=len(self.values),bytes=self.bytes,hits=self.hits,invalidated=self.invalidated)
