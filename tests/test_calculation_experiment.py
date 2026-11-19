# SPDX-License-Identifier: GPL-3.0-only
"""Small-input tests for the calculation stage 1 experiment (docs/design/calculation-stage-1-plan.md)."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from datahorizon.engine import Workbook

from calculation_fixture import create_fixture, expected_value
from calculation_experiment import HASHES, MODES, cell_hash, scan_digests, sheets_digest, updated_digest
from calculation_experiment import DependencyData, extract_precedents, sql_dependents
from calculation_experiment import evaluate, stream_sum
from datahorizon.formula import FormulaError


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "calc.dhwb"

    def connect(self, path=None):
        db = sqlite3.connect(path or self.path)
        self.addCleanup(db.close)
        return db

    def book(self, cells, name="book.dhwb"):
        """A workbook holding {(sheet name, row, col): source}; returns its connection and {sheet name: id}."""
        import uuid
        path = Path(self.temp.name) / name
        with closing(Workbook(path, create=True)) as book:
            for sheet in sorted({key[0] for key in cells} - {"Sheet1"}):
                book.mutate(dict(action="sheet_add", name=sheet, expected_state=book.state(), operation_id=uuid.uuid4().hex))
            ids = {sheet: book.sheet(sheet) for sheet in {key[0] for key in cells} | {"Sheet1"}}
        db = self.connect(path)
        db.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,?,?,?)",
                       [(ids[sheet], row, col, source, "formula" if source.startswith("=") else "auto")
                        for (sheet, row, col), source in cells.items()])
        db.commit()
        return db, ids


class FixtureTests(Case):
    def test_shape_and_expected_values(self):
        ids = create_fixture(self.path, rows=200, prefix_rows=50)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM cells WHERE sheet_id=?", (ids["data"],)).fetchone()[0], 200*10+50)
            self.assertEqual(db.execute("SELECT count(*) FROM cells WHERE kind='formula'").fetchone()[0], 200+50+3)
            self.assertEqual(db.execute("SELECT source FROM cells WHERE sheet_id=? AND row=2 AND col=10", (ids["data"],)).fetchone()[0], "=J1+A2")
        self.assertEqual(expected_value(200, "Sheet1", 200, 10), sum(r % 97 for r in range(1, 201)))
        self.assertEqual(expected_value(200, "Summary", 3, 1), 0)

    def test_fixture_opens_and_engine_agrees_on_a_short_chain(self):
        create_fixture(self.path, rows=100)
        with closing(Workbook(self.path)) as book:
            cell = book.read("Sheet1", dict(r1=100, r2=100, c1=10, c2=10))["cells"][0]
        self.assertEqual(cell["value"], expected_value(100, "Sheet1", 100, 10))


class DigestTests(Case):
    def setUp(self):
        super().setUp()
        self.data = create_fixture(self.path, rows=300)["data"]
        self.db = self.connect()

    def digests(self, mode="sum", hash_name="blake2b16"):
        return scan_digests(self.db, 64, hash_name, mode)

    def test_field_boundaries_are_unambiguous(self):
        for name in HASHES:
            self.assertNotEqual(cell_hash(name, 1, 1, "ab", "c"), cell_hash(name, 1, 1, "a", "bc"))
            self.assertNotEqual(cell_hash(name, 1, 11, "x", "text"), cell_hash(name, 11, 1, "x", "text"))

    def test_each_kind_of_change_alters_only_its_block(self):
        for mode in MODES:
            before = self.digests(mode)
            for sql in ("UPDATE cells SET source='5' WHERE row=70 AND col=1",
                        "UPDATE cells SET kind='text' WHERE row=70 AND col=2",
                        "DELETE FROM cells WHERE row=70 AND col=3",
                        "INSERT INTO cells(sheet_id,row,col,source,kind) SELECT id,70,40,'new','text' FROM sheets WHERE name='Sheet1'"):
                with self.subTest(mode=mode, sql=sql):
                    self.db.execute(sql)
                    after = self.digests(mode)
                    self.assertEqual([k for k in before.keys() | after.keys() if before.get(k) != after.get(k)], [(self.data, 1)])
                    before = after
            self.db.rollback()

    def test_style_is_not_covered(self):
        before = self.digests()
        self.db.execute("""UPDATE cells SET style='{"bold":true}' WHERE row=5""")
        self.assertEqual(self.digests(), before)

    def test_sum_update_equals_rescan(self):
        for name in HASHES:
            before = self.digests(hash_name=name)
            old = self.db.execute("SELECT row,col,source,kind FROM cells WHERE row=70 AND col=1").fetchone()
            self.db.execute("UPDATE cells SET source='99' WHERE row=70 AND col=1")
            block = updated_digest(before[(self.data, 1)], name, old, (70, 1, "99", old[3]))
            self.assertEqual(block, self.digests(hash_name=name)[(self.data, 1)])
            self.db.execute("DELETE FROM cells WHERE row=70 AND col=1")
            self.assertEqual(updated_digest(block, name, (70, 1, "99", old[3]), None), self.digests(hash_name=name)[(self.data, 1)])
            self.db.rollback()

    def test_awkward_sources(self):
        for mode in MODES:
            for source in ("", "\u00e9" * 10, "a\x00b", "x" * 65536):
                with self.subTest(mode=mode, source=source[:8]):
                    self.db.execute("UPDATE cells SET source=? WHERE row=1 AND col=9", (source,))
                    first = self.digests(mode)
                    self.db.execute("UPDATE cells SET source=? WHERE row=1 AND col=9", (source + "!",))
                    self.assertNotEqual(self.digests(mode)[(self.data, 0)], first[(self.data, 0)])

    def test_empty_workbook_and_sheet_rename(self):
        empty = Path(self.temp.name) / "empty.dhwb"
        Workbook(empty, create=True).close()
        for mode in MODES:
            self.assertEqual(scan_digests(self.connect(empty), 64, "sha256", mode), {})
        blocks, sheets = self.digests(), sheets_digest(self.db, "sha256")
        self.db.execute("UPDATE sheets SET name='Renamed' WHERE name='Summary'")
        self.assertNotEqual(sheets_digest(self.db, "sha256"), sheets)
        self.assertEqual(self.digests(), blocks)


class DependencyDataTests(Case):
    def test_extraction(self):
        r = lambda name: {"Sheet1": "S1", "Other": "S2"}.get(name)
        self.assertEqual(extract_precedents("=A1+Other!B2", "S1", r), ([("S1",1,1,1,1), ("S2",2,2,2,2)], False, False))
        self.assertEqual(extract_precedents("=SUM(A1:A1048576)", "S1", r), ([("S1",1,1,1048576,1)], False, False))
        self.assertEqual(extract_precedents("=SUM(Other!B$2:C3,A1)", "S1", r), ([("S2",2,2,3,3), ("S1",1,1,1,1)], False, False))
        self.assertEqual(extract_precedents("=TODAY()+A1", "S1", r)[1:], (True, False))
        self.assertEqual(extract_precedents("=Missing!A1", "S1", r), ([], False, False))
        self.assertEqual(extract_precedents("=A1 +* 2", "S1", r), ([], False, True))

    def test_dependents_through_cells_ranges_and_sheets(self):
        db, ids = self.book({("Sheet1",1,1):"1", ("Sheet1",1,2):"=A1", ("Sheet1",1,3):"=SUM(A1:B5)",
                             ("Other",1,1):"=Sheet1!C1", ("Sheet1",9,9):"=7"})
        data = DependencyData.build(db); data.save(db)
        for find in (data.dependents, lambda c: sql_dependents(db, c)):
            self.assertEqual(find([(ids["Sheet1"],1,1)]), {(ids["Sheet1"],1,2), (ids["Sheet1"],1,3), (ids["Other"],1,1)})
            self.assertEqual(find([(ids["Sheet1"],5,2)]), {(ids["Sheet1"],1,3), (ids["Other"],1,1)})
            self.assertEqual(find([(ids["Sheet1"],6,1)]), set())

    def test_whole_column_range_is_one_rectangle(self):
        db, ids = self.book({("Sheet1",1,2):"=SUM(A1:A1048576)"})
        data = DependencyData.build(db); data.save(db)
        self.assertEqual(db.execute("SELECT count(*) FROM x_precedents").fetchone()[0], 1)
        self.assertEqual(data.dependents([(ids["Sheet1"],1048576,1)]), {(ids["Sheet1"],1,2)})
        self.assertEqual(sql_dependents(db, [(ids["Sheet1"],1048576,1)]), {(ids["Sheet1"],1,2)})

    def test_save_and_load_round_trip(self):
        ids = create_fixture(self.path, rows=300, prefix_rows=20)
        db = self.connect(); built = DependencyData.build(db); built.save(db); loaded = DependencyData.load(db)
        self.assertEqual(len(loaded.formulas()), 300+20+3)
        self.assertEqual(set(loaded.formulas()), set(built.formulas()))
        for cell in ((ids["data"],1,1), (ids["data"],150,1), (ids["data"],300,10)):
            self.assertEqual(loaded.dependents([cell]), built.dependents([cell]))
            self.assertEqual(sql_dependents(db, [cell]), built.dependents([cell]))
        a150 = built.dependents([(ids["data"],150,1)])
        self.assertEqual(len(a150), (300-150+1) + 3)
        self.assertNotIn((ids["data"],20,11), a150)
        self.assertEqual(len(built.dependents([(ids["data"],1,1)])), 300+20+3)

    def test_volatile_and_unsafe_flags_survive_the_round_trip(self):
        db, ids = self.book({("Sheet1",1,1):"=NOW()", ("Sheet1",2,1):"=A1 +* 2", ("Sheet1",3,1):"=Missing!A1+A1"})
        DependencyData.build(db).save(db); data = DependencyData.load(db); s = ids["Sheet1"]
        self.assertEqual((data.volatile, data.unsafe), ({(s,1,1)}, {(s,2,1)}))
        self.assertEqual(data.dependents([(s,1,1)]), {(s,3,1)})

    def test_order_and_cycles(self):
        db, ids = self.book({("Sheet1",1,1):"=B1", ("Sheet1",1,2):"=SUM(A1:A2)", ("Sheet1",1,3):"=A1+1",
                             ("Sheet1",2,1):"=A2", ("Sheet1",3,1):"1", ("Sheet1",3,2):"=A3", ("Sheet1",3,3):"=B3"})
        data = DependencyData.build(db); s = ids["Sheet1"]
        ordered, cyclic = data.order({(s,1,1),(s,1,2),(s,1,3),(s,2,1),(s,3,2),(s,3,3)})
        self.assertEqual(cyclic, {(s,1,1),(s,1,2),(s,1,3),(s,2,1)})
        self.assertEqual(set(ordered), {(s,3,2),(s,3,3)})
        self.assertLess(ordered.index((s,3,2)), ordered.index((s,3,3)))

    def test_order_handles_a_chain_deeper_than_the_recursion_limit(self):
        ids = create_fixture(self.path, rows=5000)
        data = DependencyData.build(self.connect())
        ordered, cyclic = data.order(set(data.formulas()))
        self.assertEqual((len(ordered), cyclic), (5003, set()))
        position = {key: index for index, key in enumerate(ordered)}
        self.assertLess(position[(ids["data"],4999,10)], position[(ids["data"],5000,10)])
        self.assertLess(position[(ids["data"],5000,10)], position[(ids["summary"],2,1)])


class EvaluationTests(Case):
    def everything(self, db, **options):
        data = DependencyData.build(db)
        return data, evaluate(db, data, set(data.formulas()), {}, **options)

    def test_chain_deeper_than_128_and_range_wider_than_10000(self):
        ids = create_fixture(self.path, rows=12_000)
        data, results = self.everything(self.connect())
        self.assertEqual(results[(ids["data"], 12_000, 10)], expected_value(12_000, "Sheet1", 12_000, 10))
        self.assertEqual(results[(ids["summary"], 1, 1)], expected_value(12_000, "Summary", 1, 1))
        self.assertEqual(results[(ids["summary"], 3, 1)], 0)

    def test_agrees_with_the_engine_on_every_formula_of_a_small_workbook(self):
        ids = create_fixture(self.path, rows=100, prefix_rows=40)
        db = self.connect()
        extra = ["=1/0", '=A1&"x"', '=IF(A2>1,"y",NOW())', "=Missing!A1", "=B200", "=M1+1", '=COUNTIFS(A1:A50,">5")',
                 "=XLOOKUP(7,A1:A50,B1:B50)", "=AVERAGE(J1:J10)", "=SUM(M1:M2)", "=A1 +* 2", "=I5", "=Summary!A1*2"]
        db.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,13,?,'formula')",
                       [(ids["data"], row, source) for row, source in enumerate(extra, 1)])
        db.commit()
        data, results = self.everything(db)
        self.assertEqual(len(results), 100 + 40 + 3 + len(extra))
        names = {ids["data"]: "Sheet1", ids["summary"]: "Summary"}
        with closing(Workbook(self.path)) as book:
            for (sheet, row, col), mine in sorted(results.items()):
                with self.subTest(sheet=names[sheet], row=row, col=col):
                    cell = book.read(names[sheet], dict(r1=row, r2=row, c1=col, c2=col))["cells"][0]
                    if cell["calculation"] == "error":
                        self.assertIsInstance(mine, FormulaError)
                        self.assertEqual(mine.code, cell["display"])
                    else:
                        self.assertEqual(mine, cell["value"])

    def test_cycles_get_cycle_errors_and_evaluation_terminates(self):
        db, ids = self.book({("Sheet1",1,1):"=B1", ("Sheet1",1,2):"=SUM(A1:A2)", ("Sheet1",1,3):"=A1+1",
                             ("Sheet1",2,1):"=A2", ("Sheet1",3,1):"1", ("Sheet1",3,2):"=A3", ("Sheet1",3,3):"=B3",
                             ("Sheet1",4,4):"=D4"})
        data, results = self.everything(db); s = ids["Sheet1"]
        for key in ((s,1,1), (s,1,2), (s,1,3), (s,2,1), (s,4,4)):
            self.assertIsInstance(results[key], FormulaError)
            self.assertEqual(results[key].code, "#CYCLE!")
        self.assertEqual((results[(s,3,2)], results[(s,3,3)]), (1, 1))

    def test_cell_limit_is_a_deterministic_count(self):
        ids = create_fixture(self.path, rows=500); db = self.connect(); data = DependencyData.build(db)
        first = evaluate(db, data, {(ids["summary"],1,1)}, {}, cell_limit=499)
        self.assertEqual(first[(ids["summary"],1,1)].code, "#LIMIT!")
        self.assertEqual(evaluate(db, data, {(ids["summary"],1,1)}, {}, cell_limit=500)[(ids["summary"],1,1)], expected_value(500, "Summary", 1, 1))

    def test_edit_recalculates_only_dependents(self):
        ids = create_fixture(self.path, rows=300); db = self.connect()
        data, old = self.everything(db)
        db.execute("UPDATE cells SET source='1000' WHERE sheet_id=? AND row=150 AND col=1", (ids["data"],)); db.commit()
        affected = data.dependents([(ids["data"],150,1)])
        self.assertEqual(affected, {(ids["data"],row,10) for row in range(150, 301)} | {(ids["summary"],row,1) for row in (1,2,3)})
        merged = {**old, **evaluate(db, data, affected, old)}
        self.assertEqual(merged, evaluate(db, data, set(data.formulas()), {}))
        self.assertEqual(merged[(ids["data"],300,10)], expected_value(300, "Sheet1", 300, 10) - 150 % 97 + 1000)

    def test_stream_sum_equals_evaluated_sum_over_inputs_and_formulas(self):
        ids = create_fixture(self.path, rows=2_000); db = self.connect()
        data, results = self.everything(db)
        self.assertEqual(stream_sum(db, results, (ids["data"],1,1,2000,1)), expected_value(2000, "Summary", 1, 1))
        self.assertEqual(stream_sum(db, results, (ids["data"],1,10,2000,10)), sum(expected_value(2000, "Sheet1", row, 10) for row in range(1, 2001)))
        self.assertEqual(stream_sum(db, results, (ids["data"],1,9,2000,9)), 0)


if __name__ == "__main__":
    unittest.main()
