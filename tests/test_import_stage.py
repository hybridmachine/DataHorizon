# SPDX-License-Identifier: GPL-3.0-only
import json
import os
try:
    import resource
except ImportError:
    resource = None
import sys
import sqlite3
import tempfile
import threading
import time
import tracemalloc
import unittest
import unittest.mock
from contextlib import closing
from pathlib import Path

from datahorizon import xlsx_stream
from datahorizon.conversion_report import ConversionReport
from datahorizon.engine import EngineError
from datahorizon.import_stage import ImportStage, SCHEMA, SUMMARY_LIMIT, open_readonly
from datahorizon.staging import Staging
from test_xlsx_stream import FIXTURE, ListSink

STYLES = [{}, {"bold": True}, {"format": "percent"}, {"italic": True, "align": "center"}]


def cell(row, col, source=None, kind="text", style=None):
    return {"row": row, "col": col, "source": source if source is not None else f"v{row}-{col}", "kind": kind,
            "style": STYLES[(row+col) % 4] if style is None else style}


class Base(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.count = 0

    def path(self):
        self.count += 1
        path = Path(self.directory.name)/f"stage-s{self.count}.stage"
        path.touch(mode=0o600)
        return path

    def rejects(self, code, call, *args):
        with self.assertRaises(EngineError) as raised: call(*args)
        self.assertEqual(raised.exception.body["code"], code, raised.exception.body)

    def build(self, sheets, size=1000, extra=None, report=None, order=None):
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        for name, cells in sheets:
            stage.sheet(name)
            for start in range(0, len(cells), size):
                stage.cells(cells[start:start+size])
        summary = stage.finish(report or ConversionReport(), extra or {})
        return stage, summary


class ImportStageTests(Base):
    def sheets(self):
        return [("One", [cell(r, c) for r in range(1, 30) for c in range(1, 8)]), ("Two", [cell(1, 1, "=A1", "formula")])]

    def test_digest_ignores_batch_size_and_style_interning_order(self):
        a, first = self.build(self.sheets(), size=7)
        b, second = self.build(self.sheets(), size=1000)
        self.assertEqual(first["stage_digest"], second["stage_digest"])
        self.assertEqual(a.digest(), first["stage_digest"])
        # Same content arriving column-descending interns styles in a different order; the digest is unchanged.
        reordered = [("One", [cell(r, c) for r in range(1, 30) for c in reversed(range(1, 8))]), self.sheets()[1]]
        _, third = self.build(reordered)
        self.assertEqual(first["stage_digest"], third["stage_digest"])

    def test_digest_changes_with_one_source(self):
        _, first = self.build(self.sheets())
        changed = self.sheets()
        changed[0][1][5]["source"] = "different"
        _, second = self.build(changed)
        self.assertNotEqual(first["stage_digest"], second["stage_digest"])
        renamed = self.sheets()
        renamed[1] = ("Three", renamed[1][1])
        self.assertNotEqual(first["stage_digest"], self.build(renamed)[1]["stage_digest"])

    def test_sample_is_first_cells_per_sheet_in_row_major_order(self):
        stage, summary = self.build(self.sheets())
        sample = stage.sample()
        self.assertEqual([s["sheet"] for s in sample], ["One", "Two"])
        first = sample[0]["cells"]
        self.assertEqual(len(first), 100)
        self.assertEqual([(c["row"], c["col"]) for c in first], [(r, c) for r in range(1, 30) for c in range(1, 8)][:100])
        self.assertEqual(first[1]["style"], STYLES[(1+2) % 4])
        self.assertEqual(len(stage.sample(3)[0]["cells"]), 3)
        self.assertEqual(summary["sample"][0]["cells"], first)

    def test_duplicate_coordinate_rejected(self):
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        stage.sheet("A")
        self.rejects("INVALID_SPREADSHEET", stage.cells, [cell(1, 1), cell(2, 2), cell(1, 1)])
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        stage.sheet("A")
        stage.cells([cell(1, 1)])
        self.rejects("INVALID_SPREADSHEET", stage.cells, [cell(1, 1)])
        self.rejects("JOB_CONFLICT", stage.sheet, "B")  # a failed stage is dead
        self.rejects("JOB_CONFLICT", stage.cells, [cell(1, 1)])

    def test_invalid_cells_rejected(self):
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        stage.sheet("A")
        self.rejects("INVALID_INPUT", stage.cells, [cell(1, 1, "x", "bogus")])
        self.rejects("UNSUPPORTED_STYLE", stage.cells, [cell(1, 1, style={"nope": 1})])
        self.rejects("INVALID_SPREADSHEET", stage.cells, [cell(0, 1)])
        self.rejects("INVALID_SPREADSHEET", stage.cells, [cell(1, 16385)])

    def test_cells_before_sheet_and_limits(self):
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        self.rejects("INVALID_SPREADSHEET", stage.cells, [cell(1, 1)])
        for index in range(xlsx_stream.LARGE_SHEETS): stage.sheet(f"S{index}")
        self.rejects("LIMIT", stage.sheet, "one more")
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        stage.sheet("A")
        with unittest.mock.patch("datahorizon.import_stage.LARGE_CELLS", 3):
            stage.cells([cell(1, 1), cell(1, 2), cell(1, 3)])
            self.rejects("LIMIT", stage.cells, [cell(1, 4)])

    def test_finish_seals_stage_and_drops_strings(self):
        stage = ImportStage(self.path())
        stage.sheet("A")
        stage.strings.put(0, "zero"); stage.strings.put(1, "one")
        self.assertEqual(stage.strings.get(1), "one")
        stage.cells([cell(1, 1, "a")])
        summary = stage.finish(ConversionReport(), {"source_size": 12})
        self.assertEqual(summary["source_size"], 12)
        self.rejects("JOB_CONFLICT", stage.cells, [cell(5, 5)])
        self.rejects("JOB_CONFLICT", stage.sheet, "B")
        self.rejects("JOB_CONFLICT", stage.strings.put, 2, "x")
        with closing(sqlite3.connect(stage.path)) as db:
            tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("strings", tables)
        self.assertEqual(stage.summary(), summary)
        stage.close()

    def test_extra_must_not_override_summary_keys(self):
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        stage.sheet("A")
        with self.assertRaises(ValueError): stage.finish(ConversionReport(), {"stored_cells": 5})

    def test_summary_shape_and_report(self):
        report = ConversionReport()
        report.add("fmt", "approximated", "approximated format", sheet="One", cell="B2")
        stage, summary = self.build(self.sheets(), report=report, extra={"source_mtime": 5})
        self.assertEqual(set(summary), {"scope", "stage_id", "stage_digest", "sheets", "stored_cells", "report", "scope_lines", "report_entries",
                                        "totals", "sample", "can_import", "source_mtime"})
        self.assertEqual(summary["stage_id"], "s1")
        self.assertEqual(summary["sheets"], [{"name": "One", "stored_cells": 203}, {"name": "Two", "stored_cells": 1}])
        self.assertEqual(summary["stored_cells"], 204)
        self.assertEqual(summary["report"], xlsx_stream.scope_lines({})+report.lines())
        self.assertEqual(summary["totals"], report.totals())
        self.assertTrue(summary["can_import"])
        self.assertEqual(stage.sheets(), ["One", "Two"])
        self.assertEqual(stage.count(), 204)
        self.assertFalse(stage.has_temporal())

    def test_has_temporal(self):
        stage, _ = self.build([("A", [cell(1, 1, "2026-01-01", "date")])])
        self.assertTrue(stage.has_temporal())

    def test_summary_stays_under_one_mebibyte(self):
        report = ConversionReport()
        for index in range(250):
            for sample in range(120):
                report.add(f"category{index}", "approximated", f"detail {index}", sheet="S"*64, cell=f"A{sample}")
        self.assertEqual(len(report.entries()), 200)
        long = "x"*60_000
        sheets = [(f"S{n}", [cell(r, 1, long) for r in range(1, 101)]) for n in range(20)]
        stage, summary = self.build(sheets, report=report)
        self.assertLessEqual(len(json.dumps(summary).encode()), SUMMARY_LIMIT)
        self.assertEqual(summary["stored_cells"], 2000)
        self.assertEqual(summary["totals"], report.totals())
        self.assertEqual(len(summary["report_entries"]), 200)
        self.assertEqual(len(summary["report"]), len(xlsx_stream.scope_lines({}))+200)
        self.assertEqual(summary["sheets"][0]["stored_cells"], 100)
        for item in summary["sample"]:
            for sampled in item["cells"]:
                self.assertLessEqual(len(sampled["source"]), 256)
                self.assertTrue(sampled["truncated"])
        self.assertEqual(stage.summary(), summary)

    def test_readonly_open_from_another_thread(self):
        stage, summary = self.build(self.sheets())
        results = {}

        def work():
            with ImportStage.open_readonly(stage.path) as reader:
                results["summary"] = reader.summary()
                results["digest"] = reader.digest()
                results["cells"] = [row for rows in reader.iter_cells(0, 10) for row in rows]
                results["batches"] = [len(rows) for rows in reader.iter_cells(0, 100)]
                results["count"], results["sheets"] = reader.count(), reader.sheets()
                with self.assertRaises(sqlite3.OperationalError):
                    reader.db.execute("DELETE FROM cells")
        thread = threading.Thread(target=work); thread.start(); thread.join()
        self.assertEqual(results["summary"], summary)
        self.assertEqual(results["digest"], summary["stage_digest"])
        self.assertEqual(results["cells"][0], (1, 1, "v1-1", "text", json.dumps(STYLES[2], separators=(",", ":"))))
        self.assertEqual(results["batches"], [100, 100, 3])
        self.assertEqual((results["count"], results["sheets"]), (204, ["One", "Two"]))

    def test_tampering_changes_recomputed_digest(self):
        stage, summary = self.build(self.sheets())
        stage.close()
        with closing(sqlite3.connect(stage.path)) as db:
            db.execute("UPDATE cells SET source = 'evil' WHERE sheet = 0 AND row = 1 AND col = 1")
            db.commit()
        with open_readonly(stage.path) as reader:
            self.assertEqual(reader.summary()["stage_digest"], summary["stage_digest"])
            self.assertNotEqual(reader.digest(), summary["stage_digest"])

    def test_readonly_refuses_unfinished_and_foreign_files(self):
        path = self.path()
        stage = ImportStage(path)
        stage.sheet("A")
        self.rejects("INVALID_STAGE", open_readonly, path)  # uncommitted: no schema visible yet
        stage.close()
        foreign = Path(self.directory.name)/"foreign.stage"
        foreign.write_bytes(b"not a database at all, definitely not sqlite")
        self.rejects("INVALID_STAGE", open_readonly, foreign)
        self.rejects("JOB_NOT_FOUND", open_readonly, Path(self.directory.name)/"missing")

    def test_file_mode_when_created_by_staging(self):
        with Staging() as staging:
            path = staging.new("stage", "abc")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            stage = ImportStage(path)
            stage.sheet("A"); stage.cells([cell(1, 1)]); stage.finish(ConversionReport(), {})
            stage.close()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_and_foreign_files_raise_invalid_stage(self):
        foreign = self.path()
        foreign.write_bytes(b"this is not sqlite, but it is not empty either"*10)
        self.rejects("INVALID_STAGE", ImportStage, foreign)
        corrupt = self.path()
        with closing(sqlite3.connect(corrupt)) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO meta VALUES (1, '{not json')")
            db.commit()
        self.rejects("INVALID_STAGE", ImportStage, corrupt)
        self.rejects("INVALID_STAGE", open_readonly, corrupt)

    def test_failed_finish_leaves_strings_and_marks_failed(self):
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        stage.sheet("A")
        stage.cells([cell(1, 1)])
        with self.assertRaises(ValueError): stage.finish(ConversionReport(), {"sample": 1})
        self.rejects("JOB_CONFLICT", stage.cells, [cell(2, 2)])
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        stage.sheet("A")
        self.rejects("LIMIT", stage.finish, ConversionReport(), {"blob": "x"*70_000})
        # nothing was dropped or sealed
        self.assertEqual(stage.db.execute("SELECT count(*) FROM sqlite_master WHERE name = 'strings'").fetchone()[0], 1)
        self.assertIsNone(stage._meta())
        self.rejects("JOB_CONFLICT", stage.sheet, "B")

    @unittest.skipIf(resource is None, "resource module unavailable")
    def test_finish_memory_growth_is_bounded(self):
        def peak():
            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return value if sys.platform == "darwin" else value*1024
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        stage.sheet("Big")
        before, text = peak(), "y"*2000
        for start in range(0, 50_000, 1000):
            stage.cells([cell(row, 1, text+str(row)) for row in range(start+1, start+1001)])
        stage.finish(ConversionReport(), {})
        self.assertLess(peak()-before, 128*1024*1024)

    def test_stages_200000_cells_quickly_in_bounded_memory(self):
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        stage.sheet("Big")
        batches = ([cell(r, c) for c in range(1, 21)] for r in range(1, 10_001))
        started = time.monotonic()
        tracemalloc.start()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            pending = []
            for rows in batches:
                pending.extend(rows)
                if len(pending) >= 1000:
                    stage.cells(pending); pending = []
            summary = stage.finish(ConversionReport(), {})
            peak = tracemalloc.get_traced_memory()[1]-baseline
        finally:
            tracemalloc.stop()
        self.assertEqual(summary["stored_cells"], 200_000)
        self.assertLess(time.monotonic()-started, 30)
        self.assertLess(peak, 64*1024*1024)

    def test_integration_with_streaming_reader(self):
        listed = ListSink()
        xlsx_stream.read_workbook(FIXTURE, {"mode": "mixed"}, listed, lambda *_: None)
        stage = ImportStage(self.path())
        self.addCleanup(stage.close)
        report = xlsx_stream.read_workbook(FIXTURE, {"mode": "mixed"}, stage, lambda *_: None)
        summary = stage.finish(report, {})
        with open_readonly(stage.path) as reader:
            self.assertEqual(reader.sheets(), [name for name, _ in listed.sheets])
            for position, (name, cells) in enumerate(listed.sheets):
                staged = [row for rows in reader.iter_cells(position) for row in rows]
                expected = sorted(((c["row"], c["col"], c["source"], c["kind"], json.dumps(c["style"], sort_keys=True, separators=(",", ":")))
                                   for c in cells), key=lambda t: t[:2])
                self.assertEqual(staged, expected, name)
            self.assertEqual(reader.digest(), summary["stage_digest"])
        self.assertEqual(summary["stored_cells"], sum(len(c) for _, c in listed.sheets))
        self.assertEqual(summary["report_entries"], report.entries())


class StagingTests(Base):
    def test_supplied_root_is_validated_not_chmodded(self):
        root = Path(self.directory.name)/"open"
        root.mkdir()
        os.chmod(root, 0o775)
        with self.assertRaises(ValueError): Staging(root)
        os.chmod(root, 0o757)
        with self.assertRaises(ValueError): Staging(root)
        self.assertEqual(root.stat().st_mode & 0o777, 0o757)
        os.chmod(root, 0o755)
        Staging(root).close()
        self.assertEqual(root.stat().st_mode & 0o777, 0o755)
        self.assertTrue(root.exists())

    def test_close_removes_only_own_entries_in_supplied_root(self):
        root = Path(self.directory.name)/"shared"
        root.mkdir(mode=0o700)
        other = Staging(root)
        theirs = other.new("stage", "theirs")
        mine = Staging(root)
        ours = mine.new("stage", "ours")
        mine.close()
        self.assertFalse(ours.exists())
        self.assertTrue(theirs.exists())
        self.assertTrue(root.exists())
        other.close()

    def test_path_and_touch_refresh_retention(self):
        with Staging(Path(self.directory.name)/"s", retention=100) as staging:
            used = staging.new("stage", "used"); touched = staging.new("stage", "touched"); idle = staging.new("stage", "idle")
            stale = time.time()-1000
            for item in (used, touched, idle): os.utime(item, (stale, stale))
            staging.path("stage", "used")
            staging.touch("stage", "touched")
            self.assertEqual(staging.sweep(), 1)
            self.assertFalse(idle.exists())
            self.rejects("JOB_NOT_FOUND", staging.touch, "stage", "idle")

    def test_directory_is_private_and_removed(self):
        staging = Staging()
        root = staging.root
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertTrue(root.name.startswith(f"datahorizon-{os.getpid()}-"))
        staging.close()
        self.assertFalse(root.exists())
        staging.close()

    def test_given_root_is_private(self):
        root = Path(self.directory.name)/"nested"/"stage"
        with Staging(root) as staging:
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            staging.new("upload", "a")
        self.assertFalse(root.exists())

    def test_capacity_is_per_kind_and_release_frees_it(self):
        with Staging(Path(self.directory.name)/"s", capacity=2) as staging:
            staging.new("stage", "a"); staging.new("stage", "b")
            self.rejects("JOB_LIMIT", staging.new, "stage", "c")
            staging.new("upload", "a"); staging.new("export", "a")
            staging.release("stage", "a")
            staging.new("stage", "c")
            staging.release("stage", "missing")  # harmless
            self.rejects("JOB_NOT_FOUND", staging.path, "stage", "a")
            self.assertTrue(staging.path("stage", "c").is_file())

    def test_conflict_not_found_and_identifier_syntax(self):
        with Staging(Path(self.directory.name)/"s") as staging:
            staging.new("stage", "a")
            self.rejects("JOB_CONFLICT", staging.new, "stage", "a")
            self.rejects("JOB_NOT_FOUND", staging.path, "export", "a")
            for bad in ("", "../x", "a/b", "a.b", "x"*65, "a b", "é", None, 5):
                self.rejects("INVALID_JOB", staging.new, "stage", bad)
                self.rejects("INVALID_JOB", staging.path, "stage", bad)
                self.rejects("INVALID_JOB", staging.release, "stage", bad)
            staging.new("stage", "x"*64)
            self.rejects("INVALID_ARGUMENT", staging.new, "other", "a")
            self.assertEqual(sorted(p.name for p in staging.root.iterdir()), ["stage-a.stage", "stage-"+"x"*64+".stage"])

    def test_sweep_removes_only_expired_entries(self):
        with Staging(Path(self.directory.name)/"s", retention=900) as staging:
            old, fresh = staging.new("stage", "old"), staging.new("upload", "fresh")
            stale = time.time()-1000
            os.utime(old, (stale, stale))
            self.assertEqual(staging.sweep(), 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())

    def test_new_sweeps_first_so_expired_entries_free_capacity(self):
        with Staging(Path(self.directory.name)/"s", capacity=1, retention=10) as staging:
            old = staging.new("stage", "a")
            stale = time.time()-100
            os.utime(old, (stale, stale))
            staging.new("stage", "b")
            self.assertFalse(old.exists())

    def test_concurrent_new_respects_capacity(self):
        with Staging(Path(self.directory.name)/"s", capacity=3) as staging:
            outcomes = []

            def attempt(index):
                try: staging.new("stage", f"id{index}"); outcomes.append("ok")
                except EngineError as error: outcomes.append(error.body["code"])
            threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(outcomes.count("ok"), 3)
            self.assertEqual(outcomes.count("JOB_LIMIT"), 9)


if __name__ == "__main__":
    unittest.main()
