"""Contracts for destructive whole-row/column operations and reference identity."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from datahorizon.engine import EngineError, Workbook
from datahorizon.formula import MAX_ROWS, MAX_COLS, column_name, transform_structure


class ReferenceTransformTests(unittest.TestCase):
    def transform(self, source, **args):
        return transform_structure(source,"Main","Main","row",3,2,**args)

    def test_markers_literals_whitespace_and_qualified_ranges(self):
        source = '= A1 + $A$3 + A$5 + $A7 + IF(TRUE,"A3!","x") '
        self.assertEqual(self.transform(source),( '= A1 + $A$5 + A$7 + $A9 + IF(TRUE,"A3!","x") ',0))
        source = "=SUM('Other sheet'!$A$1:A5)+Main!B3+Other!B3"
        self.assertEqual(transform_structure(source,"Main","Other sheet","row",3,2),
                         ("=SUM('Other sheet'!$A$1:A7)+Main!B3+Other!B3",0))
        self.assertEqual(transform_structure("='O''Brien'!A3+\"\x00A3!\"","Main","O'Brien","row",3,2),
                         ("='O''Brien'!A5+\"\x00A3!\"",0))

    def test_sheet_matching_uses_sqlite_ascii_nocase(self):
        self.assertEqual(transform_structure("=mAiN!A3+'Ä'!A3","Other","Main","row",3,2),
                         ("=mAiN!A5+'Ä'!A3",0))
        self.assertEqual(transform_structure("='ä'!A3","Other","Ä","row",3,2),("='ä'!A3",0))

    def test_deletion_matches_surviving_members_for_both_axes_and_reversed_ranges(self):
        # Enumerate overlap shapes against an independent surviving-cell oracle.
        for axis in ("row","col"):
            def ref(value): return f"$A${value}" if axis=="row" else f"${column_name(value)}$1"
            for first in range(1,8):
                for last in range(1,8):
                    for start in range(1,8):
                        for count in (1,2,3):
                            low, high = sorted((first,last))
                            remaining = [value-(count if value>=start+count else 0) for value in range(low,high+1)
                                         if not start<=value<start+count]
                            ends = (min(remaining),max(remaining)) if remaining else None
                            if ends and first>last: ends=ends[::-1]
                            expected = "=SUM(#REF!)" if ends is None else f"=SUM({ref(ends[0])}:{ref(ends[1])})"
                            result, broken = transform_structure(f"=SUM({ref(first)}:{ref(last)})","Main","Main",axis,start,count,True)
                            self.assertEqual((result,broken),(expected,int(ends is None)),(axis,first,last,start,count))

    def test_insertion_expands_only_inside_range_and_moves_absolute_scalars(self):
        for start, expected in ((1,"A3:A7"),(2,"A1:A7"),(5,"A1:A7"),(6,"A1:A5")):
            self.assertEqual(transform_structure("=SUM(A1:A5)","Main","Main","row",start,2),(f"=SUM({expected})",0))
        self.assertEqual(self.transform("=$A$3+A4+A5",delete=True),("=#REF!+#REF!+A3",2))
        self.assertEqual(transform_structure("=XFD1+A1","Main","Main","col",2,1),("=#REF!+A1",1))


class StructureEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"structure.dhwb"
        self.book=Workbook(self.path,create=True)

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def apply(self, action="edit", **args):
        return self.book.mutate({"action":action,"sheet":"Sheet1","operation_id":str(uuid.uuid4()),
                                 "expected_state":self.book.info()["state"],**args})

    def stored(self):
        return self.book.db.execute("SELECT * FROM cells ORDER BY sheet_id,row,col").fetchall()

    def test_insert_delete_retarget_formulas_preserve_inputs_styles_and_reopen(self):
        self.apply(cells=[{"row":1,"col":1,"source":"1"},{"row":3,"col":1,"source":"2","style":{"bold":True}},
                          {"row":5,"col":1,"source":"3"},{"row":7,"col":2,"source":"=SUM($A$1:A5)"},
                          {"row":3,"col":3,"source":"","kind":"null"}])
        self.apply("sheet_add",name="Other sheet")
        self.apply(sheet="Other sheet",cells=[{"row":1,"col":1,"source":"=SUM(Sheet1!A1:A5)"},
                                               {"row":2,"col":1,"source":"=Sheet1!$A$3"}])
        self.apply("rows_insert",start=3,count=2)
        cells={(cell["row"],cell["col"]):cell for cell in self.book.read("Sheet1",{"r2":9,"c2":3})["cells"]}
        self.assertEqual(cells[(9,2)]["source"],"=SUM($A$1:A7)")
        self.assertEqual(cells[(9,2)]["value"],6)
        self.assertEqual(cells[(5,1)]["style"],{"bold":True})
        self.assertEqual(cells[(5,3)]["input_kind"],"null")
        result=self.apply("rows_delete",start=4,count=2)
        self.assertEqual(result["reference_warnings"],1)
        self.assertEqual(result["reference_issues"],[{"sheet":"Other sheet","row":2,"col":1}])
        other=self.book.read("Other sheet",{"r2":2})["cells"]
        self.assertEqual(other[0]["source"],"=SUM(Sheet1!A1:A5)")
        self.assertEqual(other[0]["value"],4)
        self.assertEqual(other[1]["source"],"=#REF!")
        self.assertEqual(other[1]["error"],"#REF!")
        self.assertFalse(self.book.undo); self.assertFalse(self.book.redo)
        expected=self.stored(); self.book.close(); self.book=Workbook(self.path)
        self.assertEqual(self.stored(),expected)

    def test_column_changes_keep_formula_targets_and_typed_text(self):
        self.apply(cells=[{"row":1,"col":2,"source":"7"},{"row":2,"col":3,"source":"=B1+$B$1"},
                          {"row":3,"col":3,"source":"=B1","kind":"text"}])
        self.apply("columns_insert",start=2,count=2)
        cells=self.book.read("Sheet1",{"r2":3,"c2":5})["cells"]
        self.assertEqual(cells[1]["source"],"=D1+$D$1"); self.assertEqual(cells[1]["value"],14)
        self.assertEqual(cells[2]["source"],"=B1"); self.assertEqual(cells[2]["kind"],"text")
        self.apply("columns_delete",start=1,count=2)
        cells=self.book.read("Sheet1",{"r2":3,"c2":3})["cells"]
        self.assertEqual(cells[1]["source"],"=B1+$B$1"); self.assertEqual(cells[1]["value"],14)

    def test_replay_does_not_shift_cells_twice(self):
        self.apply(cells=[{"row":2,"col":1,"source":"7"}])
        request={"action":"rows_insert","start":2,"count":2,"operation_id":"structure-replay","expected_state":self.book.state()}
        receipt=self.book.mutate(request); expected=self.stored()
        self.assertEqual(self.book.mutate(request),receipt)
        self.assertEqual(self.stored(),expected)

    def test_qualified_sheet_uuid_references_follow_the_calculators_identity_rules(self):
        target=self.book.info()["sheets"][0]["id"]
        self.apply(cells=[{"row":2,"col":1,"source":"7"}])
        self.apply("sheet_add",name="Other")
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"source":f"=SUM('{target}'!A2:A3)"}])
        self.apply("rows_insert",start=2)
        cell=self.book.read("Other",{})["cells"][0]
        self.assertEqual(cell["source"],f"=SUM('{target}'!A3:A4)")
        self.assertEqual(cell["value"],7)

    def assertRollback(self, action, code, **args):
        before=self.stored(); history=list(self.book.undo)
        with self.assertRaises(EngineError) as failure: self.apply(action,**args)
        self.assertEqual(failure.exception.body["code"],code)
        self.assertEqual(failure.exception.body["outcome"],"rolled_back")
        self.assertEqual(self.book.operation(failure.exception.body["operation_id"]),failure.exception.body)
        self.assertEqual(self.stored(),before); self.assertEqual(self.book.undo,history)

    def test_grid_edges_reject_storage_loss_but_report_broken_blank_references(self):
        self.apply(cells=[{"row":MAX_ROWS,"col":1,"source":"edge","style":{"bold":True}}])
        self.assertRollback("rows_insert","GRID_OVERFLOW",start=1)
        self.apply("rows_delete",start=MAX_ROWS)
        self.apply(cells=[{"row":1,"col":MAX_COLS,"source":"","style":{"bold":True}}])
        self.assertRollback("columns_insert","GRID_OVERFLOW",start=MAX_COLS)
        self.apply("columns_delete",start=MAX_COLS)
        self.apply(cells=[{"row":1,"col":1,"source":"=A1048576"}])
        self.assertEqual(self.apply("rows_insert",start=2)["reference_warnings"],1)
        self.assertEqual(self.book.read("Sheet1",{})["cells"][0]["error"],"#REF!")

    def test_unsupported_syntax_and_required_handlers_reject_before_changes(self):
        self.apply(cells=[{"row":1,"col":1,"source":"=A1&\"x\""},{"row":4,"col":1,"source":"7"}])
        self.assertRollback("rows_delete","REFERENCE_TRANSFORM_UNAVAILABLE",start=3)
        self.book.handler=str(Path(__file__).parent/"fixtures"/"validation_hook.py")
        self.assertRollback("rows_insert","UNSUPPORTED_HOOK_SCOPE",start=3)

    def test_invalid_arguments_have_durable_rollback(self):
        self.apply(cells=[{"row":2,"col":1,"source":"7"}])
        for args in ({"start":True},{"start":0},{"start":1.5},{"start":1,"count":0},
                     {"start":MAX_ROWS,"count":2},{"start":1,"count":True}):
            with self.subTest(args=args): self.assertRollback("rows_insert","INVALID_ARGUMENT",**args)

    def test_ordinary_coordinate_limit_preflights_sparse_moves(self):
        sheet=self.book.info()["sheets"][0]["id"]
        with closing(sqlite3.connect(self.path)) as direct,direct:
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source) VALUES(?,?,1,'7')",((sheet,row) for row in range(1,5002)))
        self.book.info()
        self.assertRollback("rows_insert","LIMIT",start=1,count=6000)

    def test_failure_after_first_write_rolls_back_all_coordinates_and_references(self):
        self.apply(cells=[{"row":2,"col":1,"source":"7"},{"row":3,"col":1,"source":"=A2"}])
        original=self.book.put; written=[]
        def fail_after_write(*args):
            original(*args); written.append(args)
            if len(written)==2: raise EngineError("INJECTED","Injected write failure")
        with patch.object(self.book,"put",side_effect=fail_after_write):
            self.assertRollback("rows_insert","INJECTED",start=2)
        self.assertEqual(len(written),2)

    def test_structure_deadline_during_scan_rolls_back_durably(self):
        self.apply(cells=[{"row":2,"col":1,"source":"7"},{"row":3,"col":1,"source":"=A2"}])
        clock=[0]
        def expire(sql):
            if sql.startswith("SELECT c.sheet_id"): clock[0]=6
        self.book.db.set_trace_callback(expire)
        try:
            with patch("datahorizon.engine.time.monotonic",side_effect=lambda:clock[0]):
                self.assertRollback("rows_insert","TIMEOUT",start=2)
        finally: self.book.db.set_trace_callback(None)


if __name__=="__main__":
    unittest.main()
