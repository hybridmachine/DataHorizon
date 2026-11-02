import json
import csv
import io
import itertools
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from types import SimpleNamespace

from datahorizon.engine import EngineError, Workbook, encoded
from datahorizon.formula import coordinates, has_sheet_reference, translate

FIXTURES = Path(__file__).parent / "fixtures"


class EngineTests(unittest.TestCase):
    def test_formula_preflight_sql_obeys_the_operation_deadline(self):
        sheet=self.book.info()["sheets"][0]["id"]
        with closing(sqlite3.connect(self.path)) as direct,direct:
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source) VALUES(?,?,1,'7')",
                               ((sheet,row) for row in range(1,100001)))
        self.book.info()
        for action,args in (("sheet_rename",{"name":"Renamed"}),
                            ("move",{"r1":1,"c1":1,"row":100001,"col":1,"cut_state":self.book.state()})):
            clock=itertools.count()
            with self.subTest(action=action):
                with patch("datahorizon.engine.time.monotonic",side_effect=lambda:next(clock)):
                    with self.assertRaises(EngineError) as failure: self.apply(action,sheet=sheet,**args)
                self.assertEqual(failure.exception.body["code"],"TIMEOUT")
                self.assertEqual(failure.exception.body["outcome"],"rolled_back")
                self.assertEqual(self.book.operation(failure.exception.body["operation_id"]),failure.exception.body)
        self.assertEqual(self.book.info()["sheets"][0]["name"],"Sheet1")
        self.assertEqual(self.book.db.execute("SELECT row FROM cells ORDER BY row DESC LIMIT 1").fetchone()[0],100000)

    def test_native_catalog_and_foreign_keys_are_validated(self):
        for sql,args in (("INSERT INTO cells(sheet_id,row,col) VALUES('missing',1,1)",()),
                         ("UPDATE sheets SET id=NULL",()),
                         ("UPDATE sheets SET name=?",("bad/name",)),
                         ("UPDATE sheets SET name=?",(b"invalid text",)),
                         ("UPDATE sheets SET position=-1",()),
                         ("UPDATE dh_meta SET workbook_id='invalid'",())):
            with self.subTest(sql=sql),closing(sqlite3.connect(self.path)) as direct:
                direct.execute("BEGIN"); direct.execute(sql,args); direct.commit()
                with self.assertRaises(EngineError) as failure:
                    with closing(Workbook(self.path)): pass
                self.assertEqual(failure.exception.body["code"],"INVALID_FORMAT")
                # Restore the exact initial catalog for the next invalid fixture.
                direct.execute("DELETE FROM cells"); direct.execute("DELETE FROM sheets")
                direct.execute("INSERT INTO sheets VALUES(?,?,0)",(str(uuid.uuid4()),"Sheet1"))
                direct.execute("UPDATE dh_meta SET workbook_id=?",(self.book.id,)); direct.commit()
        with closing(sqlite3.connect(self.path)) as direct,direct:
            direct.execute("UPDATE dh_meta SET workbook_id=?",(str(uuid.uuid4()),))
        with self.assertRaises(EngineError) as failure: self.book.info()
        self.assertEqual(failure.exception.body["code"],"INVALID_FORMAT")

    def test_snapshot_validates_the_copied_catalog_and_schema(self):
        destination=Path(self.temp.name)/"incompatible-copy.dhwb"; original=self.book.db
        class ChangedDuringBackup:
            def backup(self,target,**kwargs):
                original.backup(target,**kwargs)
                target.execute("CREATE TRIGGER injected AFTER UPDATE ON cells BEGIN SELECT 1; END")
        with patch.object(self.book,"db",ChangedDuringBackup()):
            with self.assertRaises(EngineError) as failure: self.book.snapshot(destination)
        self.assertEqual(failure.exception.body["code"],"SCHEMA_CHANGED")
        self.assertTrue(destination.is_file())
        self.assertEqual(self.book.info()["sheets"][0]["name"],"Sheet1")

    def test_snapshot_busy_wait_has_a_deadline_and_preserves_its_destination(self):
        destination=Path(self.temp.name)/"busy-copy.dhwb"
        direct=sqlite3.connect(self.path,check_same_thread=False,isolation_level=None)
        direct.execute("BEGIN EXCLUSIVE")
        release=threading.Timer(0.4,lambda:direct.execute("ROLLBACK")); release.start()
        try:
            with patch("datahorizon.engine.SNAPSHOT_SECONDS",0.05,create=True):
                with self.assertRaises(EngineError) as failure: self.book.snapshot(destination)
            self.assertEqual(failure.exception.body["code"],"TIMEOUT")
            self.assertTrue(destination.is_file())
        finally:
            release.join(); direct.close()

    def test_projected_read_column_defaults_match_rectangular_reads(self):
        self.edit((7,20,"target"),(7,21,"other"))
        for args in ({"rows":[7],"c1":20},{"rows":[7],"c1":20,"c2":20}):
            result=self.book.read("Sheet1",args)
            self.assertEqual(result["range"],[7,20,7,20])
            self.assertEqual([cell["value"] for cell in result["cells"]],["target"])
        self.assertEqual(self.book.read("Sheet1",{"rows":[7]})["range"],[7,1,7,1])

    def test_alternate_numeric_spellings_cannot_bypass_integer_precision(self):
        for source in ("9007199254740993.0","9007199254740993e0"," 9007199254740993 ","9_007_199_254_740_993"):
            with self.subTest(source=source):
                with self.assertRaises(EngineError) as failure:
                    self.apply(cells=[{"row":1,"col":1,"source":source,"kind":"number"}])
                self.assertIn(failure.exception.body["code"],("PRECISION","INVALID_INPUT"))
                self.assertEqual(failure.exception.body["outcome"],"rolled_back")
        self.assertNotIn((1,1),self.cells())

    def test_history_budget_includes_labels_through_undo_and_redo(self):
        with patch("datahorizon.engine.MAX_HISTORY_BYTES",1024,create=True):
            for value in ("7","9"):
                self.apply(cells=[{"row":1,"col":1,"source":value}],label=value*700)
            self.apply("undo")
            self.assertEqual(self.cells()[(1,1)]["value"],7)
            self.assertEqual(self.book.info()["redo"],"9"*700)
            with self.assertRaises(EngineError) as failure: self.apply("undo")
            self.assertEqual(failure.exception.body["code"],"NO_HISTORY")
            self.apply("redo")
            self.assertEqual(self.cells()[(1,1)]["value"],9)
            self.assertEqual(self.book.info()["undo"],"9"*700)

    def test_oversized_sheet_duplication_rejects_with_bounded_database_work(self):
        sheet=self.book.info()["sheets"][0]["id"]
        with closing(sqlite3.connect(self.path)) as direct,direct:
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source) VALUES(?,?,1,'7')",
                               ((sheet,row) for row in range(1,100001)))
        self.book.info()  # Complete external validation before measuring duplication.
        steps=0
        def work_limit():
            nonlocal steps
            steps+=1000
            # Interrupt only once so rollback and receipt recording can finish.
            return int(steps==120000)
        self.book.db.set_progress_handler(work_limit,1000)
        try:
            with self.assertRaises(EngineError) as failure: self.apply("sheet_duplicate",name="Copy")
        finally:
            self.book.db.set_progress_handler(None,0)
        self.assertEqual(failure.exception.body["code"],"LIMIT")
        self.assertEqual(failure.exception.body["outcome"],"rolled_back")
        self.assertLess(steps,120000)
        self.assertEqual(self.book.operation(failure.exception.body["operation_id"]),failure.exception.body)
        self.assertEqual([s["name"] for s in self.book.info()["sheets"]],["Sheet1"])
        self.assertEqual(self.book.db.execute("SELECT count(*) FROM cells").fetchone()[0],100000)

    def test_create_rejects_replacement_after_initialization(self):
        path=Path(self.temp.name)/"new-path.dhwb"; moved=Path(self.temp.name)/"created.dhwb"
        data_version=Workbook.data_version
        def replace_after_loading(book):
            version=data_version(book); book.db.close(); path.rename(moved); path.write_bytes(b"foreign replacement")
            return version
        with patch.object(Workbook,"data_version",new=replace_after_loading):
            with self.assertRaises(EngineError) as failure: Workbook(path,create=True)
        self.assertEqual(failure.exception.body["code"],"FILE_CHANGED")
        self.assertEqual(path.read_bytes(),b"foreign replacement")
        with closing(Workbook(moved)) as created:
            self.assertEqual(created.info()["sheets"][0]["name"],"Sheet1")

    def test_unidentifiable_snapshot_destination_fails_before_backup(self):
        self.edit((1,1,"7")); destination=Path(self.temp.name)/"unidentified.dhwb"
        with patch("datahorizon.engine.os.fstat",return_value=SimpleNamespace(st_ino=0,st_dev=0)):
            with self.assertRaises(EngineError) as failure: self.book.snapshot(destination)
        self.assertEqual(failure.exception.body["code"],"FILE_IDENTITY_UNAVAILABLE")
        self.assertEqual(destination.read_bytes(),b"")
        self.assertEqual(self.cells()[(1,1)]["value"],7)

    def test_failed_snapshot_preserves_another_writers_replacement(self):
        self.edit((1,1,"7")); destination=Path(self.temp.name)/"snapshot.dhwb"; moved=Path(self.temp.name)/"owned.dhwb"
        class FailingBackup:
            def backup(self,target,**kwargs):
                target.close(); destination.rename(moved); destination.write_bytes(b"another writer's file")
                raise OSError("Injected backup failure")
        with patch.object(self.book,"db",FailingBackup()):
            with self.assertRaises(OSError): self.book.snapshot(destination)
        self.assertEqual(destination.read_bytes(),b"another writer's file")
        self.assertEqual(self.cells()[(1,1)]["value"],7)

    def test_snapshot_rejects_replacement_before_backup_and_preserves_foreign_data(self):
        self.edit((1,1,"7")); destination=Path(self.temp.name)/"snapshot.dhwb"; moved=Path(self.temp.name)/"reserved.dhwb"
        connect=sqlite3.connect
        def replace_then_connect(path,*args,**kwargs):
            destination.rename(moved)
            with closing(connect(destination)) as foreign,foreign:
                foreign.execute("CREATE TABLE foreign_data(value)"); foreign.execute("INSERT INTO foreign_data VALUES('preserve')")
            return connect(path,*args,**kwargs)
        with patch("datahorizon.engine.sqlite3.connect",side_effect=replace_then_connect):
            with self.assertRaises(EngineError) as failure: self.book.snapshot(destination)
        self.assertEqual(failure.exception.body["code"],"FILE_CHANGED")
        with closing(connect(destination)) as foreign:
            self.assertEqual(foreign.execute("SELECT value FROM foreign_data").fetchall(),[("preserve",)])

    def test_snapshot_rejects_replacement_after_backup_without_removing_it(self):
        self.edit((1,1,"7")); destination=Path(self.temp.name)/"snapshot.dhwb"; moved=Path(self.temp.name)/"completed.dhwb"
        original=self.book.db
        class ReplacingBackup:
            def backup(self,target,**kwargs):
                original.backup(target,**kwargs); target.close(); destination.rename(moved); destination.write_bytes(b"replacement")
        with patch.object(self.book,"db",ReplacingBackup()):
            with self.assertRaises(EngineError) as failure: self.book.snapshot(destination)
        self.assertEqual(failure.exception.body["code"],"FILE_CHANGED")
        self.assertEqual(destination.read_bytes(),b"replacement")
        with closing(Workbook(moved)) as copied:
            self.assertEqual(copied.read("Sheet1",{})["cells"][0]["value"],7)

    def test_sqlite_like_user_trigger_names_cannot_bypass_schema_validation(self):
        self.edit((1,1,"7"))
        with closing(sqlite3.connect(self.path)) as direct,direct:
            direct.execute("CREATE TRIGGER sqliteXhidden AFTER UPDATE ON cells BEGIN INSERT OR REPLACE INTO cells(sheet_id,row,col,source) VALUES(new.sheet_id,2,1,'bypass'); END")
        with self.assertRaises(EngineError) as failure: self.edit((1,1,"9"))
        self.assertEqual(failure.exception.body["code"],"SCHEMA_CHANGED")
        with closing(sqlite3.connect(self.path)) as direct:
            self.assertEqual(direct.execute("SELECT row,source FROM cells ORDER BY row").fetchall(),[(1,"7")])
        with self.assertRaises(EngineError) as reopened: Workbook(self.path)
        self.assertEqual(reopened.exception.body["code"],"SCHEMA_CHANGED")

    def test_failed_create_preserves_a_replacement_file(self):
        path=Path(self.temp.name)/"new.dhwb"; moved=Path(self.temp.name)/"original.dhwb"
        def replace_then_fail(book):
            book.db.close()  # Permit replacement on Windows too.
            path.rename(moved); path.write_bytes(b"replacement belongs to another writer")
            raise EngineError("TEST_FAILURE","Injected creation failure")
        with patch.object(Workbook,"validate",new=replace_then_fail):
            with self.assertRaises(EngineError): Workbook(path,create=True)
        self.assertEqual(path.read_bytes(),b"replacement belongs to another writer")
        path.unlink()
        with patch.object(Workbook,"validate",side_effect=EngineError("TEST_FAILURE","Injected failure")):
            with self.assertRaises(EngineError): Workbook(path,create=True)
        self.assertTrue(path.is_file())
        with closing(Workbook(path)) as preserved:
            self.assertEqual(preserved.info()["sheets"][0]["name"],"Sheet1")

    def test_revision_exhaustion_rolls_back_without_corrupting_the_native_format(self):
        self.edit((1,1,"7"))
        with closing(sqlite3.connect(self.path)) as direct,direct:
            direct.execute("UPDATE dh_meta SET revision=?",(2**63-1,))
        with self.assertRaises(EngineError) as failure: self.edit((1,1,"9"))
        self.assertEqual(failure.exception.body["code"],"REVISION_LIMIT")
        self.assertEqual(failure.exception.body["outcome"],"rolled_back")
        self.assertEqual(self.book.operation(failure.exception.body["operation_id"]),failure.exception.body)
        self.assertEqual(self.cells()[(1,1)]["value"],7)
        self.book.close(); self.book=Workbook(self.path)
        self.assertEqual(self.book.db.execute("SELECT revision FROM dh_meta").fetchone()[0],2**63-1)

    def test_list_criteria_members_are_validated_before_access(self):
        self.edit((1,1,"7"))
        for field in ("filters","sort"):
            for item in (None,"bad",7,[],True):
                with self.subTest(field=field,item=item):
                    with self.assertRaises(EngineError) as failure:
                        self.book.list_view("Sheet1",{field:[item]})
                    self.assertEqual(failure.exception.body["code"],"INVALID_ARGUMENT")
        self.assertEqual(self.book.list_view("Sheet1",{})["rows"],[1])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.dhwb"
        self.book = Workbook(self.path, create=True)

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def apply(self, action="edit", **args):
        return self.book.mutate({"action": action, "sheet": "Sheet1", "operation_id": str(uuid.uuid4()),
                                 "expected_state": self.book.info()["state"], **args})

    def edit(self, *cells):
        return self.apply(cells=[{"row": row, "col": col, "source": source} for row, col, source in cells])

    def cells(self, **args):
        return {(c["row"], c["col"]): c for c in self.book.read("Sheet1", {"r2": 8, "c2": 8, **args})["cells"]}

    def test_supplies_and_reopen(self):
        self.edit((2, 2, "2"), (2, 3, "3.5"), (2, 4, "=B2*C2"),
                  (3, 2, "4"), (3, 3, "1.25"), (4, 2, "3"), (4, 3, "2"), (6, 4, "=SUM(D2:D4)"))
        self.apply("fill", r1=2, c1=4, r2=4, c2=4)
        cells = self.cells()
        self.assertEqual([cells[(r, 4)]["value"] for r in (2, 3, 4, 6)], [7, 5, 6, 18])
        self.edit((3, 2, "6"))
        self.assertEqual(self.cells()[(6, 4)]["value"], 20.5)
        self.book.close()
        self.book = Workbook(self.path)
        self.assertEqual(self.cells()[(6, 4)]["value"], 20.5)
        self.assertEqual(self.cells()[(3, 4)]["source"], "=B3*C3")

    def test_runtime_formula_recursion_is_a_limit_error(self):
        self.edit(*[(row,1,"="+"+"*20+f"A{row+1}") for row in range(1,61)],(61,1,"1"))
        previous=sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(400)
            result=self.book.read("Sheet1",{"r2":61,"c2":1})
        finally:
            sys.setrecursionlimit(previous)
        self.assertEqual(result["cells"][0]["display"],"#LIMIT!")
        self.assertEqual(result["cells"][-1]["value"],1)

    def test_formula_errors_and_lazy_if(self):
        self.edit((1, 1, "=SUUM(B1:B2)"), (1, 2, "=IF(TRUE,7,1/0)"),
                  (2, 1, "=A2"), (3, 1, "=1/0"), (4, 1, "=-2^2"), (5, 1, '=SUM("text",2,3)'))
        cells = self.cells()
        self.assertEqual(cells[(1, 1)]["display"], "#NAME?")
        self.assertEqual(cells[(1, 2)]["value"], 7)
        self.assertEqual(cells[(2, 1)]["display"], "#CYCLE!")
        self.assertEqual(cells[(3, 1)]["display"], "#DIV/0!")
        self.assertEqual(cells[(4, 1)]["value"], -4)
        self.assertEqual(cells[(5, 1)]["value"], 5)
        self.edit((1, 1, "=SUM(B1:B2)"))
        self.assertEqual(self.cells()[(1, 1)]["value"], 7)

    def test_unary_boolean_arithmetic_uses_numeric_zero_and_one(self):
        self.edit((1,1,"TRUE"),(1,2,"=+TRUE"),(1,3,"=+FALSE"),(1,4,"=SUM(+TRUE,+A1)"),
                  (2,2,"=-FALSE"),(2,3,"=SUM(TRUE,A1)"))
        cells=self.cells()
        self.assertEqual(cells[(1,2)]["value"],1)
        self.assertIs(type(cells[(1,2)]["value"]),int)
        self.assertEqual(cells[(1,2)]["display"],"1")
        self.assertEqual(cells[(1,3)]["display"],"0")
        self.assertEqual(cells[(1,4)]["value"],2)
        self.assertEqual(cells[(2,2)]["display"],"0")
        self.assertEqual(cells[(2,3)]["value"],0)

    def test_cross_sheet_absolute_and_text_references(self):
        self.apply("sheet_add", name="Other sheet")
        self.book.mutate({"action": "edit", "sheet": "Other sheet", "operation_id": str(uuid.uuid4()),
                          "expected_state": self.book.state(), "cells": [{"row": 1, "col": 1, "source": "9"}]})
        self.edit((1, 1, "='Other sheet'!$A$1+1"))
        self.assertEqual(self.cells()[(1, 1)]["value"], 10)
        self.assertEqual(translate('=A1+$B1+C$1+$D$1+IF(TRUE,"A1",0)', 2, 3),
                         '=D3+$B3+F$1+$D$1+IF(TRUE,"A1",0)')
        with self.assertRaises(EngineError):
            self.apply("sheet_rename", name="Renamed")

    def test_sheet_reference_detection_ignores_formula_strings_and_preserves_quoted_names(self):
        self.edit((1,1,'="Not a reference!"'),(2,1,'="Escaped ""!"" text"'))
        self.apply("sheet_rename",name='A"B')
        self.apply("sheet_duplicate",sheet='A"B',name="Copy")
        self.apply("sheet_delete",sheet="Copy")
        self.assertEqual(self.book.read('A"B',{"r2":1,"c2":1})["cells"][0]["value"],"Not a reference!")
        self.apply("sheet_add",name="Other")
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"source":'''='A"B'!A1+"tail"'''}])
        with self.assertRaises(EngineError) as failure:
            self.apply("sheet_rename",sheet='A"B',name="Renamed")
        self.assertEqual(failure.exception.body["code"],"REFERENCE_TRANSFORM_UNAVAILABLE")

    def test_copied_broken_reference_does_not_block_sheet_operations(self):
        self.edit((2,1,"=A1"))
        self.apply("paste",row=1,col=1,origin_row=2,origin_col=1,
                   rows=[[self.cells()[(2,1)]]])
        self.assertEqual(self.cells()[(1,1)]["source"],"=#REF!")
        self.assertEqual(self.cells()[(1,1)]["display"],"#REF!")
        self.apply("sheet_rename",name="Renamed")
        self.apply("sheet_duplicate",sheet="Renamed",name="Copy")
        self.apply("sheet_delete",sheet="Renamed")
        copied=self.book.read("Copy",{"r2":1,"c2":1})["cells"][0]
        self.assertEqual(copied["source"],"=#REF!")
        self.assertEqual(copied["display"],"#REF!")

    def test_sheet_reference_detection_distinguishes_error_literals_and_separators(self):
        for error in ("#REF!","#DIV/0!","#VALUE!","#NAME?","#NUM!","#N/A",
                      "#NULL!","#CYCLE!","#LIMIT!"):
            with self.subTest(error=error):
                self.assertFalse(has_sheet_reference("="+error))
                self.assertFalse(has_sheet_reference("=IF(TRUE,1,"+error+")"))
                self.assertTrue(has_sheet_reference("="+error+"+Other!A1"))
        for source in ("='Other sheet'!A1+#REF!","=Other!#REF!","=Unknown!",
                       '=IF(TRUE,"text!",Other!A1)',"='Incomplete!"):
            with self.subTest(source=source):
                self.assertTrue(has_sheet_reference(source))

    def test_edit_boolean_flags_reject_truthy_strings_and_numbers_atomically(self):
        self.edit((1,1,"7"))
        before=self.book.read("Sheet1",{"r2":2,"c2":1})["cells"]
        for flag in ("clear","clear_input","replace_style"):
            for invalid in ("false",0,1,None,[],{}):
                with self.subTest(flag=flag,value=invalid):
                    with self.assertRaises(EngineError) as failure:
                        self.apply(cells=[{"row":2,"col":1,"source":"9"},
                                          {"row":1,"col":1,flag:invalid,"style":{"italic":True}}])
                    self.assertEqual(failure.exception.body["code"],"INVALID_ARGUMENT")
                    self.assertEqual(failure.exception.body["outcome"],"rolled_back")
                    self.assertEqual(self.book.read("Sheet1",{"r2":2,"c2":1})["cells"],before)

    def test_atomic_validation_and_undo_redo(self):
        with self.assertRaises(EngineError):
            self.apply(cells=[{"row": 1, "col": 1, "source": "7"},
                              {"row": 1, "col": 2, "source": "nan", "kind": "number"}])
        self.assertEqual(self.cells(), {})
        self.edit((1, 1, "7"), (2, 1, "8"))
        self.apply("undo")
        self.assertEqual(self.cells(), {})
        self.apply("redo")
        self.assertEqual(self.cells()[(2, 1)]["value"], 8)
        self.apply("undo")
        self.edit((3, 1, "9"))
        with self.assertRaises(EngineError):
            self.apply("redo")

    def test_history_uses_recorded_sheet_after_default_sheet_is_renamed_or_deleted(self):
        self.apply("sheet_rename",name="Renamed")
        self.apply(sheet="Renamed",cells=[{"row":1,"col":1,"source":"7"}])
        self.apply("undo")
        self.assertEqual(self.book.read("Renamed",{"r2":1,"c2":1})["cells"],[])
        self.apply("redo")
        self.assertEqual(self.book.read("Renamed",{"r2":1,"c2":1})["cells"][0]["value"],7)
        self.apply("sheet_add",name="Other")
        self.apply("sheet_delete",sheet="Renamed")
        self.apply(sheet="Other",cells=[{"row":1,"col":1,"source":"9"}])
        self.apply("undo")
        self.assertEqual(self.book.read("Other",{"r2":1,"c2":1})["cells"],[])
        self.apply("redo")
        self.assertEqual(self.book.read("Other",{"r2":1,"c2":1})["cells"][0]["value"],9)

    def test_digit_bearing_function_names_are_not_translated_as_cell_references(self):
        self.edit((1,1,"=LOG10(B1)"),(2,1,"=LOG10 (B1)"),(3,1,"=IF(FALSE,LOG10(B1),7)"))
        cells=self.cells()
        self.assertEqual([cells[(row,1)]["display"] for row in (1,2)],["#NAME?"]*2)
        self.assertEqual(cells[(3,1)]["value"],7)
        self.assertEqual(translate("=LOG10(A1)+LOG10",1,0),"=LOG10(A2)+LOG11")
        self.assertEqual(translate("=LOG10 (A1)",1,0),"=LOG10 (A2)")

    def test_idempotency_and_status(self):
        request = {"action": "edit", "sheet": "Sheet1", "operation_id": "lost-response",
                   "expected_state": self.book.state(), "cells": [{"row": 1, "col": 1, "source": "7"}]}
        first = self.book.mutate(request)
        self.assertEqual(self.book.mutate(request), first)
        self.assertEqual(self.book.operation("lost-response"), first)
        request["cells"][0]["source"] = "8"
        with self.assertRaises(EngineError) as raised:
            self.book.mutate(request)
        self.assertEqual(raised.exception.body["code"], "OPERATION_ID_REUSED")
        self.assertEqual(self.cells()[(1, 1)]["value"], 7)

    def test_unconfirmed_admission_releases_its_transaction_and_keeps_the_id(self):
        recorded=self.book.recorded_operation
        def fail_inside_admission(operation,fingerprint):
            if self.book.db.in_transaction:
                raise ValueError("Injected failure after acquiring the writer lock")
            return recorded(operation,fingerprint)
        self.book.recorded_operation=fail_inside_admission
        try:
            with self.assertRaises(EngineError) as failure:
                self.apply(operation_id="unconfirmed-admission",cells=[{"row":1,"col":1,"source":"7"}])
        finally:
            self.book.recorded_operation=recorded
        self.assertEqual(failure.exception.body["outcome"],"unknown")
        self.assertEqual(failure.exception.body["operation_id"],"unconfirmed-admission")
        self.assertFalse(self.book.db.in_transaction)
        self.assertEqual(self.cells(),{})
        with self.assertRaises(EngineError) as missing:
            self.book.operation("unconfirmed-admission")
        self.assertEqual(missing.exception.body["outcome"],"unknown")
        self.edit((2,1,"9"))
        self.assertEqual(self.cells()[(2,1)]["value"],9)

    def test_unexpected_failure_has_a_durable_rollback_receipt_and_is_not_reexecuted(self):
        self.edit((1,1,"before"))
        before=self.cells()
        apply_edit=self.book.apply_edit
        request={"action":"edit","operation_id":"unexpected-failure","expected_state":self.book.info()["state"],
                 "cells":[{"row":1,"col":1,"source":"after"},{"row":2,"col":1,"source":"partial"}]}
        def fail_after_edits(*args,**kwargs):
            apply_edit(*args,**kwargs)
            raise RuntimeError("Injected failure after data changes")
        self.book.apply_edit=fail_after_edits
        try:
            with self.assertLogs("datahorizon.engine",level="ERROR"):
                with self.assertRaises(EngineError) as failure: self.book.mutate(request)
        finally:
            self.book.apply_edit=apply_edit
        self.assertEqual(failure.exception.body["code"],"INTERNAL_ERROR")
        self.assertEqual(failure.exception.body["outcome"],"rolled_back")
        self.assertEqual(failure.exception.body,self.book.operation("unexpected-failure"))
        self.assertFalse(self.book.db.in_transaction)
        self.assertEqual(self.cells(),before)
        with self.assertRaises(EngineError) as replay: self.book.mutate(request)
        self.assertEqual(replay.exception.body,failure.exception.body)
        self.assertEqual(self.cells(),before)

    def test_long_integer_inputs_keep_the_exact_numeric_boundary(self):
        zeros="0"*5000
        self.edit((1,1,zeros+"7"),(1,2,"=A1+1"),(2,1,zeros+"9007199254740991"))
        cells=self.cells()
        self.assertEqual(cells[(1,1)]["value"],7)
        self.assertEqual(cells[(1,2)]["value"],8)
        self.assertEqual(cells[(2,1)]["value"],9007199254740991)
        with self.assertRaises(EngineError) as failure: self.edit((3,1,zeros+"9007199254740992"))
        self.assertEqual(failure.exception.body["code"],"PRECISION")
        self.assertEqual(failure.exception.body["outcome"],"rolled_back")

    def test_direct_edit_stale_write_and_history_gap(self):
        self.edit((1, 1, "7"), (1, 2, "=A1*2"))
        state = self.book.state()
        with closing(sqlite3.connect(self.path)) as direct, direct:
            direct.execute("UPDATE cells SET source='9' WHERE row=1 AND col=1")
        with self.assertRaises(EngineError) as raised:
            self.book.mutate({"action": "edit", "sheet": "Sheet1", "operation_id": "stale", "expected_state": state,
                              "cells": [{"row": 1, "col": 1, "source": "8"}]})
        self.assertEqual(raised.exception.body["code"], "STALE_STATE")
        self.assertEqual(self.cells()[(1, 2)]["value"], 18)
        self.assertIsNone(self.book.info()["undo"])

    def test_readonly_schema_and_backup(self):
        self.edit((1, 1, "7"))
        copy = Path(self.temp.name) / "copy.dhwb"
        self.book.snapshot(copy)
        with self.assertRaises(FileExistsError):
            self.book.snapshot(copy)
        read = Workbook(copy, readonly=True)
        try:
            self.assertEqual(read.read("Sheet1", {})["cells"][0]["value"], 7)
            with self.assertRaises(EngineError):
                read.mutate({"action": "edit", "operation_id": "readonly"})
        finally:
            read.close()
        with closing(sqlite3.connect(self.path)) as direct, direct:
            direct.execute("ALTER TABLE cells ADD COLUMN bad TEXT")
        with self.assertRaises(EngineError) as raised:
            self.book.info()
        self.assertEqual(raised.exception.body["code"], "SCHEMA_CHANGED")

    def test_typed_blank_null_empty_text_and_format(self):
        self.apply(cells=[{"row": 1, "col": 1, "source": "00123", "kind": "text"},
                          {"row": 1, "col": 2, "source": "", "kind": "text"},
                          {"row": 1, "col": 3, "source": "", "kind": "null"},
                          {"row": 1, "col": 4, "source": "7", "style": {"format": "fixed2", "bold": True}}])
        cells = self.cells()
        self.assertEqual(cells[(1, 1)]["value"], "00123")
        self.assertEqual(cells[(1, 2)]["value"], "")
        self.assertIsNone(cells[(1, 3)]["value"])
        self.assertNotIn((1, 5), cells)
        self.assertEqual(cells[(1, 4)]["display"], "7.00")
        self.edit((2, 1, "=1+2"))
        self.apply(cells=[{"row":2,"col":1,"style":{"format":"text"}}])
        self.assertEqual(self.cells()[(2,1)]["value"],3)
        self.assertEqual(self.cells()[(2,1)]["source"],"=1+2")
        self.apply(cells=[{"row":3,"col":1,"style":{"format":"text"}}])
        self.edit((3,1,"00123"))
        self.assertEqual(self.cells()[(3,1)]["value"],"00123")
        with self.assertRaises(EngineError):
            self.edit((4, 1, "9007199254740993"))

    def test_bounded_reads_and_operations(self):
        with self.assertRaises(EngineError):
            self.book.read("Sheet1", {"r2": 4097})
        with self.assertRaises(EngineError):
            self.apply("fill", r2=10001)
        self.assertEqual(coordinates("XFD1048576"), (1048576, 16384))
        self.edit((1_048_576, 16_384, "far"))
        self.assertEqual(self.cells(r1=1_048_576, r2=1_048_576, c1=16_384, c2=16_384)[(1_048_576,16_384)]["value"], "far")

    def test_hooks_commit_rollback_direct_and_undo(self):
        self.book.handler = FIXTURES / "validation_hook.py"
        self.edit((1, 1, "7"))
        self.assertEqual(self.cells()[(1, 2)]["value"], 14)
        with self.assertRaises(EngineError) as raised:
            self.edit((1, 1, "-1"), (3, 1, "99"))
        self.assertEqual(raised.exception.body["outcome"], "rolled_back")
        self.assertEqual(self.cells()[(1, 1)]["value"], 7)
        self.assertNotIn((3, 1), self.cells())
        with closing(sqlite3.connect(self.path)) as direct, direct:
            direct.execute("UPDATE cells SET source='10' WHERE row=1 AND col=1")
        self.assertEqual(self.cells()[(1, 2)]["value"], 14)

    def test_missing_and_timeout_hook_fail_closed(self):
        self.book.handler = FIXTURES / "missing.py"
        with self.assertRaises(EngineError):
            self.edit((1, 1, "7"))
        self.assertEqual(self.cells(), {})
        self.book.handler = FIXTURES / "timeout_hook.py"
        started = time.monotonic()
        with self.assertRaises(EngineError):
            self.edit((1, 1, "7"))
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual(self.cells(), {})

    def test_csv_snapshot_and_formula_error_block(self):
        self.edit((1, 1, "'00123"), (1, 2, "=7+13.5"))
        result = self.book.csv_export("Sheet1", {"r2": 1, "c2": 2})
        self.assertEqual(result["csv"], "00123,20.5\r\n")
        self.edit((1, 2, "=SUUM(A1)"))
        with self.assertRaises(EngineError):
            self.book.csv_export("Sheet1", {"c2": 2})

    def test_paste_relative_references_values_and_atomicity(self):
        self.edit((1, 1, "3"))
        self.apply("paste", row=2, col=2, origin_row=1, origin_col=2,
                   rows=[[{"source": "=A1+$A$1", "input_kind": "auto", "value": 6}]])
        self.assertEqual(self.cells()[(2,2)]["source"], "=A2+$A$1")
        self.apply("paste", row=3, col=2, mode="values", rows=[[{"source": "=A1", "value": 3}]])
        self.assertEqual(self.cells()[(3,2)]["value"], 3)
        with self.assertRaises(EngineError):
            self.apply("paste", row=1_048_576, col=1, rows=[[{"source": "x"}], [{"source": "y"}]])
        self.assertNotIn((1_048_576,1), self.cells(r1=1_048_576, r2=1_048_576))

    def test_full_scope_list_projection_and_source_identity(self):
        self.edit((1,1,"Item"), (1,2,"Quantity"), (2,1,"Notebook"), (2,2,"2"),
                  (3,1,"Pen"), (3,2,"6"), (4,1,"Folder"), (4,2,"3"))
        scope = {"r1":1, "c1":1, "r2":4, "c2":2, "header":True,
                 "filters":[{"col":2, "op":">", "value":2}], "sort":[{"col":2, "descending":True}]}
        result = self.book.list_view("Sheet1", scope)
        self.assertEqual(result["rows"], [1,3,4])
        self.assertEqual((result["matching"], result["total"]), (2,3))
        self.assertEqual(self.cells()[(2,1)]["value"], "Notebook")
        self.edit((3,2,"1"))
        self.assertEqual(self.book.list_view("Sheet1", scope)["rows"], [1,4])
        scope["filters"] = []
        self.assertEqual(self.book.list_view("Sheet1", scope)["rows"], [1,4,2,3])
        projected = self.book.read("Sheet1", {"rows":[4,2], "c1":1, "c2":2})
        self.assertEqual(len(projected["cells"]), 4)

    def test_find_replace_scope_source_and_rollback(self):
        self.edit((1,1,"Pen"), (2,1,"Pen"), (3,1,"=SUM(1,2)"))
        args = {"r1":1,"r2":1,"c1":1,"c2":1,"needle":"Pen"}
        self.assertEqual(self.book.find("Sheet1", args)["count"], 1)
        self.apply("replace_all", **args, replacement="Pencil")
        self.assertEqual(self.cells()[(1,1)]["value"], "Pencil")
        self.assertEqual(self.cells()[(2,1)]["value"], "Pen")
        with self.assertRaises(EngineError):
            self.apply("replace_all", r1=3, r2=3, c1=1, c2=1, needle="3", field="display", replacement="4")
        self.assertEqual(self.cells()[(3,1)]["source"], "=SUM(1,2)")

    def test_cut_overlap_series_and_failed_destination(self):
        self.edit((1,3,"original"))
        self.book.handler = FIXTURES / "validation_hook.py"
        with self.assertRaises(EngineError):
            self.apply("move", r1=1,c1=3,r2=1,c2=3,row=1,col=1,cut_state=self.book.state())
        self.assertEqual(self.cells()[(1,3)]["value"], "original")
        self.assertNotIn((1,1), self.cells())
        self.book.handler = None
        self.edit((2,1,"3"))
        self.apply("fill",r1=2,c1=1,r2=2,c2=3,mode="series",step=2)
        self.assertEqual([self.cells()[(2,c)]["value"] for c in (1,2,3)],[3,5,7])
        self.apply("move",r1=2,c1=1,r2=2,c2=3,row=2,col=2,cut_state=self.book.state())
        self.assertNotIn((2,1),self.cells())
        self.assertEqual([self.cells()[(2,c)]["value"] for c in (2,3,4)],[3,5,7])
        self.apply("undo")
        self.assertEqual([self.cells()[(2,c)]["value"] for c in (1,2,3)],[3,5,7])

    def test_sheet_duplicate_order_and_coalesced_noop(self):
        self.edit((2,1,"3"),(2,2,"=A2*2"))
        self.apply("sheet_duplicate",name="Copy")
        self.assertEqual(self.book.read("Copy",{"r2":2,"c2":2})["cells"][1]["value"],6)
        ids=[s["id"] for s in self.book.info()["sheets"]]
        self.apply("sheet_reorder",order=list(reversed(ids)))
        self.assertEqual(self.book.info()["sheets"][0]["name"],"Copy")
        self.book.handler = FIXTURES / "missing.py"
        # Coalescing back to the original value must not invoke a change handler.
        self.apply(cells=[{"row":2,"col":1,"source":"4"},{"row":2,"col":1,"source":"3"}])
        self.assertEqual(self.cells()[(2,1)]["value"],3)

    def test_unsupported_sql_and_trigger_schema_fail_closed(self):
        with self.assertRaises(EngineError) as error:
            self.apply("sql",sql="UPDATE cells SET source='bypass'")
        self.assertEqual(error.exception.body["code"],"UNSUPPORTED_OPERATION")
        with closing(sqlite3.connect(self.path)) as direct, direct:
            direct.execute("INSERT INTO cells(sheet_id,row,col,source) SELECT id,1,1,'external' FROM sheets")
        self.assertEqual(self.cells()[(1,1)]["value"],"external")
        with closing(sqlite3.connect(self.path)) as direct, direct:
            direct.execute("CREATE TRIGGER unsupported AFTER UPDATE ON cells BEGIN SELECT 1; END")
        with self.assertRaises(EngineError) as error:
            self.edit((1,1,"managed"))
        self.assertEqual(error.exception.body["code"],"SCHEMA_CHANGED")

    def test_long_text_viewport_and_export_are_bounded(self):
        sheet=self.book.info()["sheets"][0]["id"]
        with closing(sqlite3.connect(self.path)) as direct, direct:
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'text')",
                               ((sheet,row,"x"*64000) for row in range(1,140)))
        with self.assertRaises(EngineError):
            self.book.read("Sheet1",{"r2":70})
        with self.assertRaises(EngineError):
            self.book.csv_export("Sheet1",{"r2":70})
        with self.assertRaises(EngineError):
            self.book.find("Sheet1",{"r2":139,"needle":"x"})
        self.edit((1,2,"=SUM(A1:A139)"))
        self.assertEqual(self.cells()[(1,2)]["display"],"#LIMIT!")

    def test_failed_receipt_reconciles_without_reexecuting_code(self):
        self.book.handler=FIXTURES/"validation_hook.py"
        request={"action":"edit","sheet":"Sheet1","operation_id":"failed-response","expected_state":self.book.state(),
                 "cells":[{"row":1,"col":1,"source":"-1"}]}
        with self.assertRaises(EngineError):
            self.book.mutate(request)
        receipt=self.book.operation("failed-response")
        self.assertEqual(receipt["outcome"],"rolled_back")
        self.book.handler=None
        with self.assertRaises(EngineError) as failure:
            self.book.mutate(request)
        self.assertEqual(failure.exception.body,receipt)
        self.assertEqual(self.cells(),{})

    def test_styled_blank_and_explicit_empty_text_remain_distinct(self):
        self.edit((1,2,"=A1+1"))
        self.book.handler=FIXTURES/"missing.py"
        self.apply(cells=[{"row":1,"col":1,"style":{"bold":True,"format":"fixed2"}}])
        self.assertEqual(self.cells()[(1,1)]["kind"],"blank")
        self.assertEqual(self.cells()[(1,2)]["value"],1)
        self.book.handler=None
        self.apply(cells=[{"row":1,"col":1,"source":"","kind":"text"}])
        self.assertEqual(self.cells()[(1,2)]["display"],"#VALUE!")
        self.apply(cells=[{"row":1,"col":1,"source":"","style":{"format":"text"}}])
        self.assertEqual(self.cells()[(1,2)]["value"],1)

    def test_coalesced_input_with_style_change_does_not_dispatch_handler(self):
        self.edit((1,1,"3"))
        self.book.handler=FIXTURES/"missing.py"
        self.apply(cells=[{"row":1,"col":1,"source":"4","style":{"bold":True}},
                          {"row":1,"col":1,"source":"3"}])
        self.assertEqual(self.cells()[(1,1)]["value"],3)
        self.assertTrue(self.cells()[(1,1)]["style"]["bold"])
        self.apply("undo")
        self.assertEqual(self.cells()[(1,1)]["style"],{})

    def test_absent_clipboard_cells_clear_inputs_without_creating_text(self):
        self.apply(cells=[{"row":1,"col":1,"source":"5","style":{"format":"text","bold":True}},
                          {"row":1,"col":2,"source":"=A1+1"}])
        self.apply("paste",row=1,col=1,mode="values",rows=[[{}]])
        self.assertEqual(self.cells()[(1,2)]["value"],1)
        self.assertEqual(self.cells()[(1,1)]["style"],{"format":"text","bold":True})
        self.apply("paste",row=1,col=1,rows=[[{}]])
        self.assertNotIn((1,1),self.cells())
        self.apply("paste",row=1,col=1,mode="values",rows=[[{"source":"","input_kind":"text","value":""}]])
        self.assertEqual(self.cells()[(1,1)]["kind"],"text")
        self.assertEqual(self.cells()[(1,2)]["display"],"#VALUE!")

    def test_values_paste_rejects_formula_errors_atomically(self):
        self.edit((1,1,"5"),(1,2,"=1/0"))
        error=self.cells()[(1,2)]
        with self.assertRaises(EngineError):
            self.apply("paste",row=1,col=1,mode="values",rows=[[{"value":7},error]])
        self.assertEqual(self.cells()[(1,1)]["value"],5)
        self.assertEqual(self.cells()[(1,2)]["display"],"#DIV/0!")

    def test_cut_rejects_special_paste_before_mutating(self):
        self.edit((1,1,"source"),(1,2,"destination"))
        for mode in ("values","formats"):
            with self.subTest(mode=mode), self.assertRaises(EngineError):
                self.apply("move",r1=1,c1=1,r2=1,c2=1,row=1,col=2,mode=mode,cut_state=self.book.state())
        self.assertEqual([self.cells()[(1,c)]["value"] for c in (1,2)],["source","destination"])

    def test_csv_numbers_round_trip_without_display_rounding(self):
        values=[9007199254740991,1.2345678901234567,1e100]
        self.edit(*[(1,i+1,str(value)) for i,value in enumerate(values)])
        exported=self.book.csv_export("Sheet1",{"c2":3})["csv"]
        self.assertEqual([float(value) for value in next(csv.reader(io.StringIO(exported)))],values)

    def test_blank_comparisons_never_fail_the_viewport(self):
        for index,(op,expected) in enumerate((("<",False),("<=",True),(">",False),(">=",True),("=",True),("<>",False)),1):
            self.edit((2,index,f"=A1{op}B1"))
            self.assertEqual(self.cells()[(2,index)]["value"],expected)
        self.edit((3,1,'=A1<"text"'),(3,2,"7"))
        self.assertEqual(self.cells()[(3,1)]["display"],"#VALUE!")
        self.assertEqual(self.cells()[(3,2)]["value"],7)

    def test_display_replacement_preserves_text_and_rejects_numeric_conversion(self):
        self.apply(cells=[{"row":1,"col":1,"source":"'00123"},
                          {"row":2,"col":1,"source":"1.2345","style":{"format":"fixed2"}},
                          {"row":3,"col":1,"source":"item3"}])
        self.apply("replace_all",r1=1,r2=1,c1=1,c2=1,field="display",needle="3",replacement="4")
        self.assertEqual(self.cells()[(1,1)]["source"],"'00124")
        self.assertEqual(self.cells()[(1,1)]["value"],"00124")
        receipt=self.apply("replace_all",r1=2,r2=2,c1=1,c2=1,field="display",needle="1",replacement="1")
        self.assertEqual(receipt["changed_cells"],0)
        self.assertEqual(self.cells()[(2,1)]["source"],"1.2345")
        with self.assertRaises(EngineError) as failure:
            self.apply("replace_all",r1=1,r2=3,c1=1,c2=1,field="display",needle="1",replacement="9")
        self.assertEqual(failure.exception.body["code"],"DISPLAY_REPLACEMENT_UNSUPPORTED")
        self.assertEqual(self.cells()[(1,1)]["value"],"00124")
        self.assertEqual(self.cells()[(2,1)]["value"],1.2345)
        self.apply("replace_all",r1=3,r2=3,c1=1,c2=1,field="display",needle="item",replacement="")
        self.assertEqual(self.cells()[(3,1)]["kind"],"text")
        self.assertEqual(self.cells()[(3,1)]["value"],"3")

    def test_large_sheet_delete_rejects_before_cascade(self):
        self.apply("sheet_add",name="Large")
        sheet=self.book.sheet("Large")
        with closing(sqlite3.connect(self.path)) as direct, direct:
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source) VALUES(?,?,1,'data')",((sheet,row) for row in range(1,10002)))
        with self.assertRaises(EngineError) as failure:
            self.apply("sheet_delete",sheet="Large")
        self.assertEqual(failure.exception.body["code"],"LIMIT")
        self.assertEqual(self.book.db.execute("SELECT count(*) FROM cells WHERE sheet_id=?",(sheet,)).fetchone()[0],10001)
        self.assertEqual(self.book.sheet("Large"),sheet)

    def test_extreme_references_and_range_comparisons_are_cell_errors(self):
        self.edit((1,2,"1"),(1,3,"=A"+"9"*5000),(2,3,"=(A1:A2)<(B1:B2)"),(3,3,"7"))
        cells=self.cells()
        self.assertEqual(cells[(1,3)]["display"],"#REF!")
        self.assertEqual(cells[(2,3)]["display"],"#VALUE!")
        self.assertEqual(cells[(3,3)]["value"],7)

    def test_large_text_viewports_are_bounded_previews_with_full_cell_reads(self):
        source="x"*64000
        self.apply(cells=[{"row":row,"col":1,"source":source,"kind":"text"} for row in range(1,33)])
        with self.assertRaises(EngineError) as failure:
            self.book.read("Sheet1",{"r1":1,"r2":32,"c1":1,"c2":16})
        self.assertEqual(failure.exception.body["code"],"READ_LIMIT")
        preview=self.book.read("Sheet1",{"r1":1,"r2":32,"c1":1,"c2":16,"preview":True})
        self.assertLess(len(json.dumps(preview)),100000)
        self.assertTrue(all(cell["source_omitted"] and cell["value_omitted"] for cell in preview["cells"]))
        full=self.book.read("Sheet1",{"r1":32,"r2":32,"c1":1,"c2":1})
        self.assertEqual(full["state"],preview["state"])
        self.assertEqual(full["cells"][0]["source"],source)
        for mode in ("all","values"):
            with self.subTest(mode=mode),self.assertRaises(EngineError):
                self.apply("paste",row=40,col=1,mode=mode,rows=[[preview["cells"][0]]])

    def test_receipt_rechecked_after_writer_lock_without_reclassifying_outcome(self):
        for outcome in ("committed","rolled_back","reused"):
            with self.subTest(outcome=outcome),closing(Workbook(self.path)) as retry:
                request={"action":"edit","sheet":"Sheet1","operation_id":"race-"+outcome,
                         "expected_state":self.book.info()["state"],
                         "cells":[{"row":1,"col":1,"source":"invalid" if outcome=="rolled_back" else outcome,
                                   "kind":"number" if outcome=="rolled_back" else "text"}]}
                retry_request={**request,"label":"different"} if outcome=="reused" else request
                recorded=[]
                def commit_between_lookup_and_lock(sql):
                    if sql=="BEGIN IMMEDIATE" and not recorded:
                        try:
                            recorded.append(self.book.mutate(request))
                        except EngineError as error:
                            recorded.append(error.body)
                retry.db.set_trace_callback(commit_between_lookup_and_lock)
                if outcome=="committed":
                    self.assertEqual(retry.mutate(retry_request),recorded[0])
                    self.assertEqual(recorded[0]["outcome"],"committed")
                else:
                    with self.assertRaises(EngineError) as failure:
                        retry.mutate(retry_request)
                    self.assertEqual(failure.exception.body["code"],"OPERATION_ID_REUSED" if outcome=="reused" else "INVALID_INPUT")
                    self.assertEqual(failure.exception.body["outcome"],"unknown" if outcome=="reused" else "rolled_back")
                self.assertFalse(retry.db.in_transaction)
                self.assertEqual(retry.operation(request["operation_id"]),recorded[0])

    def test_sheet_add_and_duplicate_append_after_deleting_middle_sheet(self):
        for name in ("Middle","Last"):
            self.apply("sheet_add",name=name)
        self.apply("sheet_delete",sheet="Middle")
        self.apply("sheet_add",name="Added")
        self.apply("sheet_duplicate",sheet="Sheet1",name="Copy")
        self.assertEqual([s["name"] for s in self.book.info()["sheets"]],["Sheet1","Last","Added","Copy"])
        positions=[row[0] for row in self.book.db.execute("SELECT position FROM sheets")]
        self.assertEqual(len(positions),len(set(positions)))

    def test_snapshot_reads_refresh_after_external_commit_at_begin(self):
        for method in ("list_view","find","csv_export"):
            with self.subTest(method=method):
                self.edit((1,1,"old"))
                old_state=self.book.info()["state"]
                changed=[]
                def external_commit(sql):
                    if sql=="BEGIN" and not changed:
                        with closing(sqlite3.connect(self.path)) as direct,direct:
                            direct.execute("UPDATE cells SET source='new' WHERE row=1 AND col=1")
                        changed.append(True)
                self.book.db.set_trace_callback(external_commit)
                try:
                    result=getattr(self.book,method)("Sheet1",{"r2":1,"c2":1,"needle":"new",
                        "filters":[{"col":1,"op":"=","value":"new"}]})
                finally:
                    self.book.db.set_trace_callback(None)
                self.assertTrue(changed)
                self.assertNotEqual(result["state"],old_state)
                self.assertEqual(result["state"],self.book.info()["state"])
                self.assertEqual(self.book.undo,[])
                if method=="list_view": self.assertEqual(result["rows"],[1])
                elif method=="find": self.assertEqual(result["count"],1)
                else: self.assertEqual(result["csv"],"new\r\n")

    def test_csv_export_limits_complete_serialized_json_response(self):
        sheet=self.book.sheet("Sheet1")
        source="é"*30000
        with closing(sqlite3.connect(self.path)) as direct,direct:
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'text')",
                               ((sheet,row,source) for row in range(1,65)))
        with self.assertRaises(EngineError) as failure:
            self.book.csv_export("Sheet1",{"r2":64})
        self.assertEqual(failure.exception.body["code"],"LIMIT")
        result=self.book.csv_export("Sheet1",{"r2":8})
        self.assertLessEqual(len(json.dumps(result).encode()),4*1024*1024)
        self.assertEqual(result["csv"],(source+"\r\n")*8)

    def test_read_response_limit_includes_envelope_and_array_separators(self):
        source="x"*21750
        self.apply(cells=[{"row":row,"col":1,"source":source,"kind":"text"} for row in range(1,65)])
        args={"r2":64,"c2":1}
        baseline=self.book.read("Sheet1",args)
        limit=4*1024*1024
        size=len(encoded(baseline).encode())
        self.assertLessEqual(size,limit)
        extra=(limit-size)//3+1  # Full Text replies carry source, value and display.
        self.apply(cells=[{"row":1,"col":1,"source":source+"x"*extra,"kind":"text"}])
        for cell_limit in (4096,10000):
            with self.subTest(limit=cell_limit),self.assertRaises(EngineError) as failure:
                self.book.read("Sheet1",args,limit=cell_limit)
            self.assertEqual(failure.exception.body["code"],"READ_LIMIT")
        self.assertLess(len(encoded(self.book.read("Sheet1",{**args,"preview":True})).encode()),limit)

    def test_nonfinite_literals_are_errors_before_scalar_operations(self):
        self.edit((1,1,"=1e9999>0"),(2,1,"=IF(1e9999,7,0)"),(3,1,"=1e9999^0"),
                  (4,1,"=IF(FALSE,1e9999,7)"),(5,1,"7"))
        cells=self.cells()
        self.assertEqual([cells[(row,1)]["display"] for row in range(1,4)],["#NUM!"]*3)
        self.assertEqual([cells[(row,1)]["value"] for row in (4,5)],[7,7])

    def test_sum_overflow_cannot_be_hidden_by_comparison_or_condition(self):
        self.edit((1,1,"=SUM(1e308,1e308)>0"),(2,1,"=IF(SUM(1e308,1e308),7,0)"),
                  (3,1,"=SUM(1e308,1e308,-1e308)"),(4,1,"=IF(FALSE,SUM(1e308,1e308),7)"))
        cells=self.cells()
        self.assertEqual([cells[(row,1)]["display"] for row in range(1,4)],["#NUM!"]*3)
        self.assertEqual(cells[(4,1)]["value"],7)

    def test_full_paste_preserves_inputs_independently_of_text_format(self):
        self.apply(cells=[{"row":1,"col":1,"source":"7"},{"row":2,"col":1,"source":"'00123"},
                          {"row":3,"col":1,"source":"=A1+1"},{"row":4,"col":1,"source":"0001","kind":"text"}])
        self.apply(cells=[{"row":row,"col":1,"style":{"format":"text"}} for row in range(1,5)])
        original=self.book.read("Sheet1",{"r2":4,"c2":1})["cells"]
        self.apply("paste",row=1,col=2,origin_row=1,origin_col=1,rows=[[cell] for cell in original])
        copied=[c for c in self.book.read("Sheet1",{"r2":4,"c2":2})["cells"] if c["col"]==2]
        self.assertEqual([c["value"] for c in copied],[7,"00123",8,"0001"])
        self.assertEqual([c["input_kind"] for c in copied],[c["input_kind"] for c in original])
        self.assertEqual([c["style"] for c in copied],[c["style"] for c in original])
        self.assertEqual(copied[1]["source"],"'00123")
        self.assertEqual(copied[2]["source"],"=B1+1")

    def test_failed_receipt_keeps_writer_lock_until_outcome_is_recorded(self):
        operation="rollback-receipt-race"
        with closing(Workbook(self.path)) as contender:
            contender.db.execute("PRAGMA busy_timeout=0")
            other={"action":"edit","sheet":"Sheet1","operation_id":operation,"expected_state":contender.info()["state"],
                   "cells":[{"row":1,"col":1,"source":"9"}]}
            attempts=[]
            def race(sql):
                if sql.startswith("INSERT INTO operations") and not attempts:
                    attempts.append("started")
                    try: attempts.append(contender.mutate(other)["outcome"])
                    except EngineError as error: attempts.append(error.body)
            self.book.db.set_trace_callback(race)
            try:
                with self.assertRaises(EngineError) as failure:
                    self.book.mutate({**other,"expected_state":self.book.info()["state"],
                                      "cells":[{"row":1,"col":1,"source":"invalid","kind":"number"}]})
            finally:
                self.book.db.set_trace_callback(None)
            self.assertEqual(len(attempts),2)
            self.assertEqual(attempts[0],"started")
            self.assertEqual(attempts[1]["outcome"],"unknown")
            self.assertEqual(attempts[1]["operation_id"],operation)
            self.assertIn("locked",attempts[1]["message"])
            self.assertEqual(failure.exception.body,self.book.operation(operation))
            self.assertEqual(failure.exception.body["outcome"],"rolled_back")
            self.assertEqual(self.cells(),{})
            with self.assertRaises(EngineError) as reused:
                contender.mutate(other)
            self.assertEqual(reused.exception.body["code"],"OPERATION_ID_REUSED")

    def test_receipt_storage_failure_retains_unknown_outcome_and_releases_transaction(self):
        self.edit((1,1,"7"))
        self.book.db.set_authorizer(lambda action,table,*args: sqlite3.SQLITE_DENY
                                   if action==sqlite3.SQLITE_INSERT and table=="operations" else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(EngineError) as failure:
                self.edit((1,1,"9"))
        finally:
            self.book.db.set_authorizer(None)
        self.assertEqual(failure.exception.body["outcome"],"unknown")
        self.assertEqual(failure.exception.body["code"],"OUTCOME_UNKNOWN")
        self.assertFalse(self.book.db.in_transaction)
        self.assertEqual(self.cells()[(1,1)]["value"],7)
        self.edit((1,1,"8"))
        self.assertEqual(self.cells()[(1,1)]["value"],8)


if __name__ == "__main__":
    unittest.main()
