# SPDX-License-Identifier: GPL-3.0-only
"""Disposable bounded dependency graph and nonvolatile result reuse."""
from collections import defaultdict
from .formula import Calculator, Parser, FormulaError, CELL_ERRORS


class DependencyIndex:
    def __init__(self):
        self.clear()

    def clear(self):
        self.edges={};self.reverse=defaultdict(set);self.volatile=set();self.unsafe=set()
        self.values={};self.sizes={};self.bytes=0;self.hits=0;self.invalidated=0

    def forget(self,key):
        self.bytes-=self.sizes.pop(key,0);self.values.pop(key,None)
        for target in self.edges.pop(key,()):
            self.reverse[target].discard(key)
            if not self.reverse[target]:self.reverse.pop(target,None)
        self.volatile.discard(key);self.unsafe.discard(key)

    def invalidate(self,keys):
        affected=set(keys);pending=list(affected)
        while pending:
            for parent in self.reverse.get(pending.pop(),()):
                if parent not in affected:affected.add(parent);pending.append(parent)
        for key in affected:self.forget(key)
        self.invalidated+=len(affected)
        return affected

    def cacheable(self,key):
        pending=[key];seen=set()
        while pending:
            item=pending.pop()
            if item in seen:continue
            seen.add(item)
            if item in self.volatile or item in self.unsafe:return False
            pending.extend(self.edges.get(item,()))
        return True

    def calculator(self,read,resolve,seconds,clock=None):
        index=self
        class IndexedCalculator(Calculator):
            def __init__(self):
                super().__init__(self.input,resolve,seconds,clock=clock)
                self.cache=dict(index.values);self.disabled=False

            def input(self,sheet,row,col):
                item=read(sheet,row,col);key=(sheet,row,col);targets=set()
                if item and item['kind']=='formula':
                    try:
                        pending=[Parser(item['source'][1:]).parse()]
                        while pending:
                            self.check();node=pending.pop()
                            if node[0]=='ref':targets.add(self.reference(node[1],sheet))
                            elif node[0]=='range':targets.update(self.range_targets(node,sheet))
                            elif node[0]=='call':
                                if node[1] in ('TODAY','NOW'):index.volatile.add(key)
                                pending.extend(node[2])
                            elif node[0]=='binary':pending.extend(node[2:])
                            elif node[0]=='unary':pending.append(node[2])
                    except FormulaError:index.unsafe.add(key)
                if not self.disabled:
                    if len(index.edges)>=4096 or sum(map(len,index.edges.values()))+len(targets)>50000:
                        index.clear();self.disabled=True
                    else:
                        index.edges[key]=targets
                        for target in targets:index.reverse[target].add(key)
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

            def save(self,key,result):
                if self.disabled or not index.cacheable(key):return
                size=64+len(str(result).encode('utf-8'))
                if key not in index.values and (len(index.values)>=4096 or index.bytes+size>4*1024*1024):
                    index.clear();self.disabled=True;return
                index.bytes+=size-index.sizes.get(key,0);index.sizes[key]=size;index.values[key]=result
        return IndexedCalculator()

    def info(self):
        return dict(nodes=len(self.edges),edges=sum(map(len,self.edges.values())),cached=len(self.values),bytes=self.bytes,hits=self.hits,invalidated=self.invalidated)
