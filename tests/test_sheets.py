"""Atomic sheet catalog changes and reference preservation."""
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from datahorizon.engine import EngineError, Workbook
from datahorizon.client import LocalService
from datahorizon.formula import rename_sheet_references, delete_sheet_references


class SheetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"sheets.dhwb"
        self.book = Workbook(self.path, create=True)

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def apply(self, action="edit", **args):
        return self.book.mutate({"action":action,"sheet":"Sheet1", "operation_id":str(uuid.uuid4()),
                                 "expected_state":self.book.state(), **args})

    def stored(self):
        return (self.book.db.execute("SELECT * FROM sheets ORDER BY id").fetchall(),
                self.book.db.execute("SELECT * FROM cells ORDER BY sheet_id,row,col").fetchall())

    def assertRollback(self, action, code, **args):
        before, history = self.stored(), list(self.book.undo)
        with self.assertRaises(EngineError) as failure:
            self.apply(action, **args)
        self.assertEqual(failure.exception.body["code"],code)
        self.assertEqual(failure.exception.body["outcome"],"rolled_back")
        self.assertEqual(self.book.operation(failure.exception.body["operation_id"]),failure.exception.body)
        self.assertEqual(self.stored(),before)
        self.assertEqual(self.book.undo,history)

    def test_rename_preserves_spelling_strings_ranges_absolute_and_uuid_references(self):
        target = self.book.sheet("Sheet1")
        self.apply(cells=[{"row":2,"col":1,"source":"7"}])
        self.apply("sheet_add",name="Other")
        source = f'= SUM(sHeEt1!$A$2:A3) + \'{target}\'!A2 + IF(TRUE,0,"Sheet1!A2\0") '
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"source":source,"style":{"bold":True}},
                                       {"row":2,"col":1,"source":"=Sheet1!A2","kind":"text"}])
        result = self.apply("sheet_rename",name="O'Brien")
        self.assertEqual(result["changed_cells"],1)
        self.assertEqual(self.book.sheet("O'Brien"),target)
        cells = self.book.read("Other",{"r2":2})["cells"]
        self.assertEqual(cells[0]["source"],source.replace("sHeEt1!","'O''Brien'!"))
        self.assertEqual(cells[0]["value"],14)
        self.assertEqual(cells[0]["style"],{"bold":True})
        self.assertEqual(cells[1]["source"],"=Sheet1!A2")
        self.assertIsNone(self.book.info()["undo"])
        self.book.close(); self.book=Workbook(self.path)
        self.assertEqual(self.book.read("Other",{})["cells"][0]["value"],14)

    def test_rename_matches_sqlite_ascii_case_rules_and_leaves_unqualified_refs(self):
        self.assertEqual(rename_sheet_references('=Main!a$2 + A1 + "Main!A2"',"main","New"),
                         '=\'New\'!a$2 + A1 + "Main!A2"')
        self.assertEqual(rename_sheet_references("='ä'!A1", "Ä", "New"),"='ä'!A1")

    def test_rename_unsupported_syntax_and_hook_reject_without_catalog_change(self):
        self.apply(cells=[{"row":1,"col":1,"source":"=Sheet1!A2&\"x\""}])
        self.assertRollback("sheet_rename","REFERENCE_TRANSFORM_UNAVAILABLE",name="New")
        self.book.handler="required.py"
        self.assertRollback("sheet_rename","UNSUPPORTED_HOOK_SCOPE",name="New")

    def test_rename_collision_and_write_failure_roll_back_formulas_and_catalog(self):
        self.apply(cells=[{"row":1,"col":1,"source":"=Sheet1!A2"},
                          {"row":1,"col":2,"source":"=Sheet1!A3"}])
        self.apply("sheet_add",name="Other")
        self.assertRollback("sheet_rename","OPERATION_FAILED",name="Other")
        original = self.book.put
        def fail(*args):
            original(*args)
            raise EngineError("INJECTED","write failed")
        with patch.object(self.book,"put",side_effect=fail):
            self.assertRollback("sheet_rename","INJECTED",name="New")

    def test_rename_scan_budget_and_expanded_source_limit_are_atomic(self):
        self.apply(cells=[{"row":1,"col":1,"source":"=Sheet1!A2"},
                          {"row":1,"col":2,"source":"=Sheet1!A3"}])
        with patch("datahorizon.engine.MAX_CELLS",1):
            self.assertRollback("sheet_rename","LIMIT",name="New")
        self.apply(cells=[{"row":1,"col":2,"clear":True},
                          {"row":1,"col":1,"source":"=Sheet1!A2"+" "*65520}])
        self.assertRollback("sheet_rename","INVALID_INPUT",name="a"*31)

    def test_rename_receipt_replay_does_not_run_transform_again(self):
        self.apply(cells=[{"row":1,"col":1,"source":"=Sheet1!A2"}])
        request={"action":"sheet_rename","sheet":"Sheet1","name":"Renamed",
                 "operation_id":"rename-once","expected_state":self.book.state()}
        receipt=self.book.mutate(request)
        self.assertEqual(self.book.mutate(request),receipt)
        self.assertEqual(self.book.read("Renamed",{})["cells"][0]["source"],"='Renamed'!A2")

    def test_rename_through_sdk_rest_preserves_shared_reference_behavior(self):
        with LocalService() as client, client.open(Path(self.temp.name)/"rest.dhwb",create=True) as book:
            book.set(1,1,"=Sheet1!A2")
            book.set(2,1,"7")
            target=book.info["sheets"][0]["id"]
            book.apply("sheet_rename",sheet=target,name="Renamed")
            self.assertEqual(book.info["sheets"][0]["id"],target)
            self.assertEqual(book.read("Renamed")["cells"][0]["value"],7)

    def test_duplicate_retargets_local_name_and_uuid_self_references_only_in_copy(self):
        target=self.book.sheet("Sheet1")
        self.apply("sheet_add",name="Other")
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"source":"3"}])
        source=f'= A2 + SUM(sHeEt1!$A$2:A3) + \'{target}\'!A2 + Other!A1'
        self.apply(cells=[{"row":1,"col":1,"source":source,"style":{"wrap":True}},
                          {"row":2,"col":1,"source":"7"},
                          {"row":3,"col":2,"source":"=Sheet1!A2","kind":"text"}])
        before=self.book.db.execute("SELECT row,col,source,kind,style FROM cells WHERE sheet_id=?",(target,)).fetchall()
        result=self.apply("sheet_duplicate",name="O'Brien")
        copied=self.book.sheet("O'Brien")
        self.assertNotEqual(copied,target)
        self.assertEqual(result["changed_cells"],3)
        self.assertEqual(self.book.db.execute("SELECT row,col,source,kind,style FROM cells WHERE sheet_id=?",(target,)).fetchall(),before)
        first=self.book.read("O'Brien",{})["cells"][0]
        self.assertEqual(first["source"],source.replace("sHeEt1!","'O''Brien'!").replace(target,copied))
        self.assertEqual(first["value"],24)
        self.assertEqual(first["style"],{"wrap":True})
        self.apply(sheet="O'Brien",cells=[{"row":2,"col":1,"source":"11"}])
        self.assertEqual(self.book.read("O'Brien",{})["cells"][0]["value"],36)
        self.assertEqual(self.book.read("Sheet1",{})["cells"][0]["value"],24)
        self.assertEqual(self.book.read("O'Brien",{"r1":3,"r2":3,"c1":2,"c2":2})["cells"][0]["source"],"=Sheet1!A2")
        self.book.close(); self.book=Workbook(self.path)
        self.assertEqual(self.book.read("O'Brien",{})["cells"][0]["value"],36)

    def test_duplicate_ignores_unsupported_formulas_on_other_sheets(self):
        self.apply("sheet_add",name="Other")
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"source":"=Sheet1!A1&\"x\""}])
        self.apply(cells=[{"row":1,"col":1,"source":"7"}])
        self.apply("sheet_duplicate",name="Copy")
        self.assertEqual(self.book.read("Copy",{})["cells"][0]["value"],7)

    def test_duplicate_limits_hooks_unsupported_syntax_and_failure_roll_back_catalog(self):
        self.apply(cells=[{"row":1,"col":1,"source":"=Sheet1!A2"},
                          {"row":2,"col":1,"source":"7"}])
        with patch("datahorizon.engine.MAX_CELLS",1):
            self.assertRollback("sheet_duplicate","LIMIT",name="Copy")
        self.assertRollback("sheet_duplicate","OPERATION_FAILED",name="Sheet1")
        original=self.book.put
        def fail(*args):
            original(*args)
            raise EngineError("INJECTED","copy failed")
        with patch.object(self.book,"put",side_effect=fail):
            self.assertRollback("sheet_duplicate","INJECTED",name="Copy")
        self.apply(cells=[{"row":1,"col":1,"source":"=Sheet1!A2"+" "*65520}])
        self.assertRollback("sheet_duplicate","INVALID_INPUT",name="a"*31)
        self.apply(cells=[{"row":1,"col":1,"source":"=Sheet1!A2&\"x\""}])
        self.assertRollback("sheet_duplicate","REFERENCE_TRANSFORM_UNAVAILABLE",name="Copy")
        self.book.handler="required.py"
        self.assertRollback("sheet_duplicate","UNSUPPORTED_HOOK_SCOPE",name="Copy")

    def test_duplicate_rest_replay_retains_the_single_new_identity(self):
        with LocalService() as client, client.open(Path(self.temp.name)/"copy-rest.dhwb",create=True) as book:
            book.set(1,1,"=Sheet1!A2"); book.set(2,1,"7")
            old_state=book.state
            receipt=book.apply("sheet_duplicate",sheet="Sheet1",name="Copy",operation_id="copy-once")
            book.refresh()
            copied=book.info["sheets"][1]["id"]
            self.assertEqual(client.request(book.base+"/operations",{
                "action":"sheet_duplicate","sheet":"Sheet1","name":"Copy",
                "operation_id":"copy-once","expected_state":old_state}),receipt)
            self.assertEqual(book.read("Copy")["cells"][0]["value"],7)
            self.assertEqual(book.info["sheets"][1]["id"],copied)

    def test_delete_breaks_name_uuid_and_inherited_ranges_permanently(self):
        target=self.book.sheet("Sheet1")
        self.apply(cells=[{"row":2,"col":1,"source":"7"}])
        self.apply("sheet_add",name="Other")
        source=f'=SUM((sHeEt1!$A$2):(A3))+\'{target}\'!A2+IF(TRUE,0,"Sheet1!A2")'
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"source":source,"style":{"bold":True}},
                                       {"row":2,"col":1,"source":"=Other!A3"},
                                       {"row":3,"col":1,"source":"=Sheet1!A2","kind":"text"}])
        receipt=self.apply("sheet_delete")
        self.assertEqual(receipt["changed_cells"],2)
        self.assertEqual(receipt["reference_warnings"],2)
        self.assertEqual(receipt["reference_issues"],[{"sheet":"Other","row":1,"col":1}])
        cells=self.book.read("Other",{"r2":3})["cells"]
        self.assertEqual(cells[0]["source"],'=SUM(#REF!)+#REF!+IF(TRUE,0,"Sheet1!A2")')
        self.assertEqual(cells[0]["error"],"#REF!")
        self.assertEqual(cells[0]["style"],{"bold":True})
        self.assertEqual(cells[1]["value"],"=Sheet1!A2")
        self.apply("sheet_add",name="Sheet1")
        self.apply(cells=[{"row":2,"col":1,"source":"100"}])
        self.assertEqual(self.book.read("Other",{})["cells"][0]["error"],"#REF!")
        self.book.close(); self.book=Workbook(self.path)
        self.assertEqual(self.book.read("Other",{})["cells"][0]["error"],"#REF!")

    def test_delete_ignores_deleted_formulas_but_rejects_unsupported_survivors_and_hooks(self):
        self.apply(cells=[{"row":1,"col":1,"source":"=Sheet1!A2&\"x\""}])
        self.apply("sheet_add",name="Other")
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"source":"=Sheet1!A2&\"x\""}])
        self.assertRollback("sheet_delete","REFERENCE_TRANSFORM_UNAVAILABLE")
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"clear":True}])
        self.book.handler="required.py"
        self.assertRollback("sheet_delete","UNSUPPORTED_HOOK_SCOPE")
        self.book.handler=None
        self.apply("sheet_delete")
        self.assertRollback("sheet_delete","LAST_SHEET",sheet="Other")

    def test_delete_affected_cell_budget_and_write_failure_preserve_catalog_and_history(self):
        self.apply(cells=[{"row":1,"col":1,"source":"7"},{"row":2,"col":1,"source":"8"}])
        self.apply("sheet_add",name="Other")
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"source":"=Sheet1!A1"}])
        with patch("datahorizon.engine.MAX_CELLS",2):
            self.assertRollback("sheet_delete","LIMIT")
        original=self.book.put
        def fail(*args):
            original(*args)
            if args[-1] is None:
                raise EngineError("INJECTED","delete failed")
        with patch.object(self.book,"put",side_effect=fail):
            self.assertRollback("sheet_delete","INJECTED")

    def test_delete_reports_first_hundred_formula_locations_and_replays_receipt(self):
        self.apply("sheet_add",name="Other")
        self.apply(sheet="Other",cells=[{"row":row,"col":1,"source":"=Sheet1!A2"} for row in range(1,102)])
        request={"action":"sheet_delete","sheet":"Sheet1","expected_state":self.book.state(),"operation_id":"delete-once"}
        receipt=self.book.mutate(request)
        self.assertEqual(receipt["reference_warnings"],101)
        self.assertEqual(len(receipt["reference_issues"]),100)
        self.assertEqual(self.book.mutate(request),receipt)

    def test_delete_rejects_mixed_sheet_ranges_and_uses_ascii_name_matching(self):
        self.assertEqual(delete_sheet_references("=SUM('O''Brien'!A1:A2)+Other!A1","Main","O'Brien"),
                         ("=SUM(#REF!)+Other!A1",1))
        self.assertEqual(delete_sheet_references("='ä'!A1","Main","Ä"),("='ä'!A1",0))
        self.apply("sheet_add",name="Other")
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"source":"=SUM(Sheet1!A1:Other!A2)"}])
        self.assertRollback("sheet_delete","REFERENCE_TRANSFORM_UNAVAILABLE")

    def test_delete_over_rest_returns_broken_reference_receipt(self):
        with LocalService() as client, client.open(Path(self.temp.name)/"delete-rest.dhwb",create=True) as book:
            book.apply("sheet_add",name="Other")
            book.set(1,1,"=Sheet1!A2",sheet="Other")
            result=book.apply("sheet_delete",sheet="Sheet1")
            self.assertEqual(result["reference_warnings"],1)
            self.assertEqual(book.read("Other")["cells"][0]["source"],"=#REF!")

    def test_delete_reference_scan_deadline_rolls_back_with_a_durable_receipt(self):
        self.apply(cells=[{"row":1,"col":1,"source":"7"}])
        self.apply("sheet_add",name="Other")
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"source":"=Sheet1!A1"}])
        clock=[0]
        def expire(sql):
            if sql.startswith("SELECT c.sheet_id"):
                clock[0]=6
        self.book.db.set_trace_callback(expire)
        try:
            with patch("datahorizon.engine.time.monotonic",side_effect=lambda:clock[0]):
                self.assertRollback("sheet_delete","TIMEOUT")
        finally:
            self.book.db.set_trace_callback(None)
