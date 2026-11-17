# SPDX-License-Identifier: GPL-3.0-only
"""Admission, transactional movement, reference parity and hook behavior."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid

from datahorizon.engine import EngineError, Workbook
from datahorizon.formula import MAX_ROWS
from datahorizon.large_structure import needs_large
from datahorizon.workbook_code import activation, fingerprint
from test_charts import spec


class LargeStructureEngine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book = Workbook(Path(self.temp.name) / "book.dhwb", create=True)
        self.addCleanup(self.book.close)
        self.sheet = self.book.sheet("Sheet1")

    def request(self, action, **args):
        return dict(action=action, expected_state=self.book.state(), operation_id=str(uuid.uuid4()), **args)

    def mutate(self, action, **args):
        return self.book.mutate(self.request(action, **args))

    @staticmethod
    def load(db, sheet, count, source="7"):
        with db:
            db.execute("BEGIN")
            db.executemany(
                "INSERT INTO cells(sheet_id,row,col,source) VALUES(?,?,1,?)",
                ((sheet, row, source) for row in range(1, count + 1)),
            )

    def clone(self, name):
        path = Path(self.temp.name) / (name + ".dhwb")
        with closing(sqlite3.connect(path)) as db:
            self.book.db.backup(db)
        return Workbook(path)

    def admit(self, request):
        with self.book.lock:
            self.book.db.execute("BEGIN")
            try:
                return needs_large(self.book, request)
            finally:
                self.book.db.execute("ROLLBACK")

    def test_disjoint_coordinates_threshold_and_atomic_receipt(self):
        self.load(self.book.db, self.sheet, 5000)
        request = self.request("rows_insert", start=1, count=6000)
        self.assertFalse(self.admit(request))
        self.book.db.execute("INSERT INTO cells(sheet_id,row,col,source) VALUES(?,5001,1,'7')", (self.sheet,))
        self.assertTrue(self.admit(request))
        phases = []
        result = self.book.mutate(request, large_structure_progress=phases.append)
        self.assertEqual(result["changed_cells"], 10002)
        self.assertEqual(phases[0], "planning")
        self.assertEqual(phases[-1], "committing")
        self.assertFalse(self.book.undo)
        self.assertFalse(self.book.redo)
        with closing(Workbook(self.book.path)) as reopened:
            after = reopened.db.execute("SELECT row,col,source FROM cells ORDER BY row").fetchall()
            self.assertEqual(after, [(row, 1, "7") for row in range(6001, 11002)])
            self.assertEqual(reopened.operation(request["operation_id"]), result)
            self.assertEqual(reopened.mutate(request, large_structure_progress=self.fail), result)

    def test_unrelated_formula_scan_threshold(self):
        self.mutate("sheet_add", name="Other")
        other = self.book.sheet("Other")
        self.load(self.book.db, other, 10000, "=$A$1+1")
        request = self.request("rows_insert", start=40)
        self.assertFalse(self.admit(request))
        self.book.db.execute("INSERT INTO cells(sheet_id,row,col,source) VALUES(?,10001,1,'=$A$1+1')", (other,))
        self.assertTrue(self.admit(request))
        receipt = self.book.mutate(request, large_structure_progress=lambda _: None)
        self.assertEqual(receipt["changed_cells"], 0)

    def test_source_and_encoded_history_admission_budgets(self):
        # Each of these 128 inputs uses exactly 64 KiB of the ordinary mover's
        # source estimate, including its 128-byte per-cell overhead and {} style.
        self.load(self.book.db, self.sheet, 128, "x" * (65536 - 130))
        duplicate = self.request("sheet_duplicate", sheet=self.sheet, name="Copy")
        self.assertFalse(self.admit(duplicate))
        self.book.db.execute("UPDATE cells SET source=source||'x' WHERE row=1")
        self.assertTrue(self.admit(duplicate))
        self.book.db.execute("DELETE FROM cells")
        self.load(self.book.db, self.sheet, 58, "Å" * 12000)
        insert = self.request("rows_insert", start=1)
        self.assertFalse(self.admit(insert))
        self.book.db.execute("UPDATE cells SET source=source||? WHERE row=1", ("Å" * 4000,))
        self.assertTrue(self.admit(insert))

    def snapshot(self, book):
        copy_id = book.db.execute("SELECT id FROM sheets WHERE name='Copy'").fetchone()
        replacement = copy_id[0] if copy_id else None

        def normalized(value):
            return value.replace(replacement, "<copy-id>") if replacement and isinstance(value, str) else value

        shape = (
            "SELECT * FROM sheets ORDER BY position,id",
            "SELECT c.sheet_id,c.row,c.col,c.source,c.kind,c.style FROM cells c JOIN sheets s ON c.sheet_id=s.id ORDER BY s.position,c.row,c.col",
            "SELECT o.namespace,o.id,o.sheet_id,o.payload FROM dh_objects o JOIN sheets s ON o.sheet_id=s.id ORDER BY s.position,o.namespace,o.id",
            "SELECT * FROM dh_relational ORDER BY kind,id",
        )
        return [[tuple(map(normalized, row)) for row in book.db.execute(query)] for query in shape]

    def test_all_six_paths_match_on_cells_and_metadata(self):
        self.mutate("sheet_add", name="Other")
        self.mutate("edit", cells=[
            dict(row=3, col=2, source="21", style={"bold": True}),
            dict(row=4, col=3, source="", kind="null"),
            dict(row=5, col=2, source="=A3"),
            dict(row=6, col=4, source=f"='{self.sheet}'!A3"),
            dict(row=7, col=3, source="=Sheet1!A3"),
            dict(row=8, col=2, source="=Sheet1!A3 ` 2"),
        ])
        self.mutate("edit", sheet="Other", cells=[
            dict(row=1, col=1, source="=SUM(Sheet1!A3:B7)"),
            dict(row=1, col=2, source=f"='{self.sheet}'!$A$3"),
            dict(row=2, col=1, source="=Sheet1!B5 & ` 2"),
            dict(row=3, col=1, source="=Sheet1!A1048576"),
        ])
        self.mutate("chart_create", sheet="Sheet1", spec=spec(), anchor={"row": 3, "col": 4})
        self.mutate("chart_create", sheet="Other", spec=spec())
        self.mutate("name_save", name="Area", reference="Sheet1!B3:C5")
        self.mutate("list_declare", id="Records", spec=dict(r1=3, r2=5, c1=1, c2=3))
        self.mutate("metadata_set", namespace="presentation", id="View", payload=dict(frozen_rows=2))
        self.mutate("module_save", name="rules", source="def cell(ctx, changes): raise ValueError('unexpected')\n",
                    fingerprint=fingerprint(self.book.db))
        self.mutate("binding_save", event="cell_change", module="rules", function="cell",
                    scope=dict(kind="range", reference="Sheet1!B3:C5"), fingerprint=fingerprint(self.book.db))

        for action, args in (
            ("rows_insert", dict(start=1, count=4)),
            ("rows_delete", dict(start=2, count=2)),
            ("columns_insert", dict(start=1, count=2)),
            ("columns_delete", dict(start=2)),
            ("sheet_duplicate", dict(name="Copy")),
            ("sheet_delete", {}),
        ):
            with self.subTest(action=action), closing(self.clone(action+"_bounded")) as bounded, closing(self.clone(action+"_staged")) as staged:
                results = [
                    book.mutate(dict(
                        action=action, sheet=self.sheet, operation_id="compare", expected_state=book.state(), **args,
                    ), large_structure_progress=progress)
                    for book, progress in ((bounded, None), (staged, lambda _: None))
                ]
                for receipt in results:
                    receipt.pop("state")
                self.assertEqual(results[0], results[1])
                self.assertEqual(self.snapshot(bounded), self.snapshot(staged))

    def assert_rollback(self, request, code, **kwargs):
        before = self.book.db.execute("SELECT * FROM cells ORDER BY sheet_id,row,col").fetchall()
        undo, redo = self.book.undo[:], self.book.redo[:]
        with self.assertRaises(EngineError) as caught:
            self.book.mutate(request, large_structure_progress=lambda _: None, **kwargs)
        self.assertEqual(caught.exception.body["code"], code)
        self.assertEqual(caught.exception.body["outcome"], "rolled_back")
        self.assertEqual(self.book.operation(request["operation_id"]), caught.exception.body)
        self.assertEqual(self.book.state(), request["expected_state"])
        self.assertEqual(self.book.db.execute("SELECT * FROM cells ORDER BY sheet_id,row,col").fetchall(), before)
        self.assertEqual((self.book.undo, self.book.redo), (undo, redo))
        self.assertEqual(self.book.db.execute("SELECT name FROM sqlite_temp_schema WHERE name LIKE 'dh_structure_%'").fetchall(), [])

    def test_sql_interrupt_after_write_begins_rolls_back_with_receipt(self):
        self.load(self.book.db, self.sheet, 5001)
        self.mutate("edit", cells=[dict(row=1, col=2, source="history")])
        cancel = threading.Event()

        def interrupt(sql):
            if sql.startswith("DELETE FROM main.cells"):
                cancel.set()

        self.book.db.set_trace_callback(interrupt)
        try:
            self.assert_rollback(self.request("rows_insert", start=1, count=6000), "CANCELLED", cancel_event=cancel)
        finally:
            self.book.db.set_trace_callback(None)

    def test_grid_overflow_from_before_hook_rolls_back(self):
        self.book.db.execute("INSERT INTO cells(sheet_id,row,col,source) VALUES(?,?,1,'edge')", (self.sheet, MAX_ROWS))
        request = self.request("rows_insert", start=1)
        with self.assertRaises(EngineError) as caught:
            self.admit(request)
        self.assertEqual(caught.exception.body["code"], "GRID_OVERFLOW")
        self.book.db.execute("DELETE FROM cells")
        handler = Path(self.temp.name) / "edge.py"
        handler.write_text("def on_structure(ctx,event):\n"
                           "    if event['phase']=='before': ctx.set('Sheet1',1048576,1,'edge')\n")
        self.book.handler = handler
        self.assert_rollback(request, "GRID_OVERFLOW")

    def test_invalid_direct_inputs_fail_closed_on_shift_and_duplicate(self):
        self.load(self.book.db, self.sheet, 5001)
        for source, style in (("x" * 65537, "{}"), ("x", "{"), (b"blob", "{}"), ("x", b"{}")):
            self.book.db.execute("UPDATE cells SET source=?,style=? WHERE row=1", (source, style))
            for action, args in (("rows_insert", dict(start=1, count=6000)), ("sheet_duplicate", dict(sheet=self.sheet, name="Copy"))):
                with self.subTest(action=action, source_type=type(source), style=style):
                    self.assert_rollback(self.request(action, **args), "INVALID_INPUT")

    def test_hooks_see_authored_cells_at_final_coordinates_and_can_reject(self):
        self.load(self.book.db, self.sheet, 5001)
        self.mutate("sheet_add", name="Log")
        self.mutate("module_save", name="rules", source="""import json
def observe(ctx, changes):
    def source(cell):
        return cell['source'] if cell else None
    ctx.set('Log!A1', json.dumps([[c['row'], source(c['before']), source(c['after'])] for c in changes]))
""", fingerprint=fingerprint(self.book.db))
        self.mutate("binding_save", event="cell_change", module="rules", function="observe",
                    scope=dict(kind="sheet", sheet=self.sheet), fingerprint=fingerprint(self.book.db))
        self.book.approved_code = frozenset([activation(self.book)["activation_key"]])
        handler = Path(self.temp.name) / "rules.py"
        handler.write_text("""def on_structure(ctx, event):
    if event['phase']=='before':
        ctx.set('Sheet1!A2', 'before write')
    else:
        ctx.set('Sheet1!A6003', 'after write')
        if event['action']=='rows_delete':
            raise ValueError('reject whole edit')

def on_change(ctx, changes):
    for change in changes:
        cell = change['after']
        if cell is None:
            assert change['row'] == 2
        else:
            assert cell['source'] in ('before write', 'after write') or cell['source'].startswith('[')
""")
        self.book.handler = handler
        receipt = self.book.mutate(self.request("rows_insert", start=1, count=6000), large_structure_progress=lambda _: None)
        self.assertEqual(receipt["changed_cells"], 10003)
        self.assertEqual(json.loads(self.book.raw(self.book.sheet("Log"), 1, 1)["source"]),
                         [[2, "7", None], [6002, None, "before write"], [6003, None, "after write"]])
        self.assert_rollback(self.request("rows_delete", start=1, count=6000), "HOOK_FAILED")
