# SPDX-License-Identifier: GPL-3.0-only
"""Tests for saved-query chart sources in specifications."""

import copy
import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from datahorizon import chart_resolve, chart_sources, charts
from datahorizon.engine import EngineError, Workbook
from datahorizon.query_views import preview

from test_charts import code, payload, spec


def query_source(name, column):
    return {"kind": "query", "query": name, "column": column}


def source_spec(values, **extra):
    return {"spec_version": 1, "type": "column", "series": [{"name": {"text": "s"}, "values": {"source": values}}],
            **extra}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = Workbook(pathlib.Path(self.tmp.name) / "q.dhwb", create=True)
        self.addCleanup(self.book.close)
        self.apply("table_create", name="Sales", columns=[
            dict(name="Month", type="text"), dict(name="Units", type="integer")])
        self.apply("table_insert", table="Sales", rows=[dict(Month=f"M{n}", Units=n) for n in range(1, 13)])
        self.query_id = self.save("Top", "SELECT Month, Units FROM Sales ORDER BY Units DESC")
        self.dupe_id = self.save("Dupe", "SELECT Units AS x, Units AS x FROM Sales")

    def apply(self, action, **args):
        return self.book.mutate(dict(action=action, operation_id=str(uuid.uuid4()),
                                     expected_state=self.book.state(), **args))

    def save(self, name, sql, **options):
        review = preview(self.book, dict(name=name, sql=sql, expected_state=self.book.state(), **options))
        return self.apply("query_save", **review["source_request"], review_digest=review["review_digest"])["query"]["id"]


class Spec(Fixture):
    def refused(self, value):
        with self.assertRaises(EngineError) as caught:
            charts.normalise_spec(self.book, value)
        return caught.exception

    def test_normalise_stores_query_identity_and_column_name(self):
        out = charts.normalise_spec(self.book, source_spec(query_source("top", "Units")))
        self.assertEqual(out["series"][0]["values"]["source"],
                         {"kind": "query", "query_id": self.query_id, "column": "Units"})
        out = charts.normalise_spec(self.book, source_spec(query_source(self.query_id, "Month")))
        self.assertEqual(out["series"][0]["values"]["source"]["query_id"], self.query_id)
        self.assertIsNone(charts.validate_stored(payload(out)))

    def test_refusals(self):
        error = self.refused(source_spec(query_source("Nope", "Units")))
        self.assertEqual((code(error), error.status), ("QUERY_NOT_FOUND", 404))
        self.assertEqual(code(self.refused(source_spec(query_source("Top", "Nope")))), "COLUMN_NOT_FOUND")
        self.assertEqual(code(self.refused(source_spec(query_source("Dupe", "x")))), "COLUMN_NOT_FOUND")
        self.assertEqual(code(self.refused(source_spec(dict(query_source("Top", "Units"), extra=1)))), "INVALID_SPEC")
        named = source_spec(query_source("Top", "Units"))
        named["series"][0]["name"] = {"source": query_source("Top", "Month")}
        self.assertEqual(code(self.refused(named)), "INVALID_SPEC")

    def test_stored_validation_and_is_broken(self):
        out = charts.normalise_spec(self.book, source_spec(query_source("Top", "Units")))
        source = out["series"][0]["values"]["source"]
        self.assertEqual(charts.source_kind(source), "query")
        self.assertFalse(charts.is_broken(source))
        source["query_id"] = None
        self.assertIsNone(charts.validate_stored(payload(out)))
        self.assertTrue(charts.is_broken(source))
        for bad in ({"kind": "query", "query_id": 3, "column": "Units"},
                    {"kind": "query", "query_id": None, "column": ""},
                    {"kind": "query", "query_id": None, "column": None},
                    {"kind": "query", "query_id": None},
                    {"kind": "query", "query_id": None, "column": "Units", "extra": 1}):
            out["series"][0]["values"]["source"] = bad
            self.assertEqual(charts.validate_stored(payload(out)), "INVALID_SPEC", bad)

    def test_describe_marks_a_missing_column_broken(self):
        out = charts.normalise_spec(self.book, source_spec(query_source("Top", "Units")))

        def described(change=None):
            changed = copy.deepcopy(out)
            if change:
                changed["series"][0]["values"]["source"].update(change)
            return charts.describe(self.book.db, "s", "c", payload(changed))["spec"]["series"][0]["values"]["source"]

        self.assertEqual((described()["query"], described()["broken"]), ("Top", False))
        self.assertTrue(described({"column": "Gone"})["broken"])
        self.assertTrue(described({"query_id": str(uuid.uuid4())})["broken"])
        self.assertEqual((described({"query_id": None})["query"], described({"query_id": None})["broken"]), (None, True))
        self.assertTrue(described({"query_id": self.dupe_id, "column": "x"})["broken"])
        self.apply("query_rename", query=self.query_id, name="Renamed")
        self.assertEqual(described()["query"], "Renamed")

    def test_spec_patch_keeps_a_query_source(self):
        chart_id = self.apply("chart_create", spec=source_spec(query_source("Top", "Units")))["chart_id"]
        self.apply("query_rename", query=self.query_id, name="Renamed")
        self.apply("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})
        stored = charts.find(self.book, chart_id)[1]["spec"]
        self.assertEqual(stored["series"][0]["values"]["source"],
                         {"kind": "query", "query_id": self.query_id, "column": "Units"})

    def test_query_sources_add_nothing_to_size_checks(self):
        out = charts.normalise_spec(self.book, source_spec(query_source("Top", "Units")))
        self.assertIsNone(charts.limit_error(out))

    def test_mixed_kinds_in_one_chart(self):
        scatter = {"spec_version": 1, "type": "scatter", "series": [{
            "name": {"text": "s"},
            "values": {"source": {"kind": "table", "table": "Sales", "column": "Units"}},
            "x": {"source": query_source("Top", "Units")}}]}
        out = charts.normalise_spec(self.book, scatter)
        self.assertEqual([out["series"][0][key]["source"]["kind"] for key in ("values", "x")], ["table", "query"])
        column = spec(categories={"source": {"kind": "range", "sheet": "Sheet1", "ref": "A2:A13"}})
        column["series"] = [{"name": {"text": "s"},
                             "values": {"source": {"kind": "table", "table": "Sales", "column": "Units"}}}]
        out = charts.normalise_spec(self.book, column)
        self.assertEqual((out["categories"]["source"]["kind"], out["series"][0]["values"]["source"]["kind"]),
                         ("range", "table"))
        self.assertIsNone(charts.validate_stored(payload(out)))
        chart_id = self.apply("chart_create", spec=column)["chart_id"]
        self.apply("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})
        self.assertEqual(charts.find(self.book, chart_id)[1]["spec"]["categories"]["source"]["kind"], "range")


    # Following the saved query, and mixed sources.

    def make_chart(self):
        self.chart = self.apply("chart_create", spec=source_spec(query_source("Top", "Units")))["chart_id"]
        return self.chart

    def source(self, chart_id=None):
        return charts.find(self.book, chart_id or self.chart)[1]["spec"]["series"][0]["values"]["source"]

    def test_deleting_the_saved_query_breaks_its_sources(self):
        self.make_chart()
        receipt = self.apply("query_delete", query=self.query_id)
        self.assertEqual(receipt["broken_charts"], [self.chart])
        self.assertEqual(self.source(), {"kind": "query", "query_id": None, "column": "Units"})
        described = charts.read(self.book, dict(expected_state=self.book.state()))["charts"][0]
        self.assertTrue(described["spec"]["series"][0]["values"]["source"]["broken"])

    def test_deleting_another_saved_query_breaks_nothing(self):
        self.make_chart()
        receipt = self.apply("query_delete", query=self.dupe_id)
        self.assertNotIn("broken_charts", receipt)
        self.assertEqual(self.source()["query_id"], self.query_id)

    def test_renaming_the_saved_query_breaks_nothing(self):
        self.make_chart()
        receipt = self.apply("query_rename", query=self.query_id, name="Renamed")
        self.assertNotIn("broken_charts", receipt)
        self.assertEqual(self.source()["query_id"], self.query_id)

    def test_saving_a_query_touches_no_chart(self):
        self.make_chart()
        before = charts.find(self.book, self.chart)[1]
        review = preview(self.book, dict(name="Third", sql="SELECT Units FROM Sales", expected_state=self.book.state()))
        receipt = self.apply("query_save", **review["source_request"], review_digest=review["review_digest"])
        self.assertNotIn("broken_charts", receipt)
        self.assertEqual(charts.find(self.book, self.chart)[1], before)

    def test_a_refused_table_drop_leaves_the_chart_alone(self):
        chart_id = self.apply("chart_create", spec=source_spec(
            {"kind": "table", "table": "Sales", "column": "Units"}))["chart_id"]
        before = charts.find(self.book, chart_id)[1]
        with self.assertRaises(EngineError) as caught:
            self.apply("table_drop", table="Sales", allow_data_loss=True)
        self.assertEqual(code(caught), "DEPENDENCY_BLOCKED")
        self.assertEqual(charts.find(self.book, chart_id)[1], before)

    def test_a_broken_saved_query_source_survives_reopen_and_blocks_spec_patch(self):
        self.make_chart()
        self.apply("query_delete", query=self.query_id)
        path = self.book.path
        self.book.close()
        self.book = Workbook(path)
        self.addCleanup(self.book.close)
        self.assertIsNone(self.source()["query_id"])
        with self.assertRaises(EngineError) as caught:
            self.apply("chart_update", chart_id=self.chart, spec_patch={"title": {"text": "T"}})
        self.assertEqual((code(caught), caught.exception.status), ("SOURCE_BROKEN", 409))

    def test_charts_without_the_query_are_not_rewritten(self):
        self.make_chart()
        other = self.apply("chart_create", spec=spec())["chart_id"]
        before = charts.find(self.book, other)[1]
        receipt = self.apply("query_delete", query=self.query_id)
        self.assertEqual(receipt["changed_charts"], 1)
        self.assertEqual(charts.find(self.book, other)[1], before)

    def test_a_series_name_may_not_be_a_table_or_query_source(self):
        out = charts.normalise_spec(self.book, source_spec(query_source("Top", "Units")))
        for named in (query_source("Top", "Month"), {"kind": "table", "table": "Sales", "column": "Month"}):
            stored = copy.deepcopy(out)
            stored["series"][0]["name"] = {"source": named}
            self.assertEqual(charts.validate_stored(payload(stored)), "INVALID_SPEC")
        stored = copy.deepcopy(out)
        stored["series"][0]["name"] = {"source": {"kind": "query", "query_id": self.query_id, "column": "Month"}}
        self.assertEqual(charts.validate_stored(payload(stored)), "INVALID_SPEC")
        stored["series"][0]["name"] = {"source": {"kind": "table", "table_id": "t", "column_id": "c"}}
        self.assertEqual(charts.validate_stored(payload(stored)), "INVALID_SPEC")

    def test_a_chart_with_a_broken_query_source_refuses_spec_patch(self):
        chart_id = self.make_chart()
        sheet_id, stored = charts.find(self.book, chart_id)
        stored["spec"]["series"][0]["values"]["source"]["query_id"] = None
        self.book.db.execute("BEGIN")
        charts.store(self.book, sheet_id, chart_id, stored)
        self.book.db.execute("COMMIT")
        with self.assertRaises(EngineError) as caught:
            self.apply("chart_update", chart_id=chart_id, spec_patch={"title": {"text": "T"}})
        self.assertEqual((code(caught), caught.exception.status), ("SOURCE_BROKEN", 409))

    def mixed(self, sheet):
        column = spec(categories={"source": {"kind": "range", "sheet": sheet, "ref": "A2:A13"}})
        column["series"] = [
            {"name": {"text": "t"}, "values": {"source": {"kind": "table", "table": "Sales", "column": "Units"}}},
            {"name": {"text": "q"}, "values": {"source": query_source("Top", "Units")}}]
        return self.apply("chart_create", spec=column)["chart_id"]

    def parts(self, chart_id):
        stored = charts.find(self.book, chart_id)[1]["spec"]
        return (stored["categories"]["source"], stored["series"][0]["values"]["source"],
                stored["series"][1]["values"]["source"])

    def test_a_mixed_chart_follows_only_its_range_source(self):
        chart_id = self.mixed("Sheet1")
        _, table_source, query_source_ = self.parts(chart_id)
        self.apply("rows_insert", sheet="Sheet1", start=1, count=2)
        moved, table_after, query_after = self.parts(chart_id)
        self.assertEqual((moved["row1"], moved["row2"]), (4, 15))
        self.assertEqual((table_after, query_after), (table_source, query_source_))
        self.apply("move", sheet="Sheet1", r1=1, c1=1, r2=15, c2=1, row=1, col=6, cut_state=self.book.state())
        moved, table_after, query_after = self.parts(chart_id)
        self.assertEqual((moved["col1"], moved["col2"]), (6, 6))
        self.assertEqual((table_after, query_after), (table_source, query_source_))

    def test_deleting_the_range_sheet_breaks_only_the_range_source(self):
        self.apply("sheet_add", name="Sheet2")
        chart_id = self.mixed("Sheet2")
        _, table_source, query_source_ = self.parts(chart_id)
        receipt = self.apply("sheet_delete", sheet="Sheet2")
        self.assertEqual(receipt["broken_charts"], [chart_id])
        broken, table_after, query_after = self.parts(chart_id)
        self.assertTrue(charts.is_broken(broken))
        self.assertEqual((table_after, query_after), (table_source, query_source_))


ENDLESS = "SELECT max(i) AS top FROM (WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT i FROM n)"


def query_spec(categories, *values, type_="column"):
    out = {"spec_version": 1, "type": type_,
           "series": [{"name": {"text": f"s{n}"}, "values": {"source": value}} for n, value in enumerate(values)]}
    if categories is not None:
        out["categories"] = {"source": categories}
    return out


class Resolving(Fixture):
    def setUp(self):
        super().setUp()
        self.calls = []

    def run_query(self, query_id):
        self.calls.append(query_id)
        return self.book.query_jobs().run_once(query_id, state=self.book.state(), cancel_event=threading.Event(),
                                               seconds=5, max_rows=chart_sources.QUERY_ROWS)

    def payload(self, value):
        return payload(charts.normalise_spec(self.book, value))

    def resolve(self, value=None, stored=None, **kwargs):
        kwargs.setdefault("run_query", self.run_query)
        return chart_resolve.resolve(self.book, stored or self.payload(value), self.book.state(), **kwargs)

    def refused_resolve(self, *args, **kwargs):
        with self.assertRaises(EngineError) as caught:
            self.resolve(*args, **kwargs)
        return caught.exception

    def test_resolves_two_series_and_categories_from_one_query_run(self):
        self.save("Pair", "SELECT Month, Units, Units * 2 AS Twice FROM Sales ORDER BY Units DESC")
        out = self.resolve(query_spec(query_source("Pair", "Month"), query_source("Pair", "Units"),
                                      query_source("Pair", "Twice")))
        self.assertEqual(len(self.calls), 1)
        resolved = out["resolved"]
        self.assertEqual(resolved["categories"], [f"M{n}" for n in range(12, 0, -1)])
        self.assertEqual(resolved["series"][0]["values"], [float(n) for n in range(12, 0, -1)])
        self.assertEqual(resolved["series"][1]["values"], [float(n * 2) for n in range(12, 0, -1)])
        self.assertEqual(out["counts"], {"points": 24, "gaps": {"empty": 0, "text": 0, "error": 0}})

    def test_query_values_are_classified(self):
        self.save("Mixed", "SELECT 'a' AS v UNION ALL SELECT NULL UNION ALL SELECT 1.5 UNION ALL SELECT 9007199254740993")
        out = self.resolve(query_spec(query_source("Mixed", "v"), query_source("Mixed", "v")))
        self.assertEqual(out["counts"], {"points": 2, "gaps": {"empty": 1, "text": 1, "error": 0}})
        self.assertEqual(out["resolved"]["series"][0]["values"], [None, None, 1.5, float(9007199254740993)])
        self.assertEqual(out["resolved"]["categories"], ["a", "", "1.5", "9007199254740993"])

    def test_blob_result_fails_the_job(self):
        self.save("Blob", "SELECT x'0102' AS b")
        error = self.refused_resolve(query_spec(None, query_source("Blob", "b")))
        self.assertEqual((error.body["code"], error.status), ("QUERY_VALUE", 409))

    def test_redefined_query_without_the_column_is_refused(self):
        stored = self.payload(query_spec(None, query_source("Top", "Units")))
        self.save("Top", "SELECT Month, Units * 1.5 AS Cost FROM Sales", query=self.query_id)
        error = self.refused_resolve(stored=stored)
        self.assertEqual((error.body["code"], error.status), ("SOURCE_BROKEN", 409))
        self.assertIn("Units", error.body["message"])
        self.assertIn("Top", error.body["message"])

    def test_ambiguous_column_name_is_refused(self):
        stored = self.payload(query_spec(None, query_source("Top", "Units")))
        self.save("Top", "SELECT Units, Units FROM Sales", query=self.query_id)
        error = self.refused_resolve(stored=stored)
        self.assertEqual((error.body["code"], error.status), ("SOURCE_BROKEN", 409))
        self.assertIn("Units", error.body["message"])
        self.assertIn("Top", error.body["message"])

    def test_result_over_the_point_limit_is_refused_by_name(self):
        counted = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < {}) SELECT i FROM n"
        for name, rows in (("Fits", 10_000), ("Over", 10_001), ("Far", 20_000)):
            self.save(name, counted.format(rows))
        self.assertEqual(self.resolve(query_spec(None, query_source("Fits", "i")))["counts"]["points"], 10_000)
        for name in ("Over", "Far"):
            error = self.refused_resolve(query_spec(None, query_source(name, "i")))
            self.assertEqual(error.body["code"], "LIMIT_POINTS", name)
            self.assertIn(name, error.body["message"])
            self.assertIn("10,000", error.body["message"])
        self.save("Pie", counted.format(1_001))
        error = self.refused_resolve(query_spec(None, query_source("Pie", "i"), type_="pie"))
        self.assertEqual(error.body["code"], "LIMIT_PIE_POINTS")
        self.assertIn("Pie", error.body["message"])
        # A query limit that is not the row count keeps the engine's code and message.
        self.book.query_jobs().run_once_bytes = 1024
        error = self.refused_resolve(query_spec(None, query_source("Fits", "i")))
        self.assertEqual((error.body["code"], error.body["message"]),
                         ("QUERY_LIMIT", "Query result exceeds 16 MiB in memory"))

    def test_parameter_defaults_are_used(self):
        self.save("Min", "SELECT Month, Units FROM Sales WHERE Units >= :minimum ORDER BY Units",
                  parameters=[dict(name="minimum", type="integer")], defaults=dict(minimum=10))
        out = self.resolve(query_spec(query_source("Min", "Month"), query_source("Min", "Units")))
        self.assertEqual(out["resolved"]["categories"], ["M10", "M11", "M12"])
        self.assertEqual(out["resolved"]["series"][0]["values"], [10.0, 11.0, 12.0])

    def test_query_without_run_query_is_invalid_argument(self):
        error = self.refused_resolve(query_spec(None, query_source("Top", "Units")), run_query=None)
        self.assertEqual(error.body["code"], "INVALID_ARGUMENT")
        self.apply("table_create", name="Plain", columns=[dict(name="N", type="integer")])
        self.apply("table_insert", table="Plain", rows=[dict(N=1)])
        out = self.resolve(query_spec(None, {"kind": "table", "table": "Plain", "column": "N"}))
        self.assertEqual(out["resolved"]["series"][0]["values"], [1.0])
        self.assertEqual(self.calls, [])

    def test_broken_query_source_is_refused_before_running(self):
        stored = self.payload(query_spec(None, query_source("Top", "Units")))
        broken = copy.deepcopy(stored)
        broken["spec"]["series"][0]["values"]["source"]["query_id"] = None
        error = self.refused_resolve(stored=broken)
        self.assertEqual((error.body["code"], error.status), ("SOURCE_BROKEN", 409))
        # A query deleted after the chart was read is gone from the snapshot.
        self.apply("query_delete", query=self.query_id)
        error = self.refused_resolve(stored=stored)
        self.assertEqual((error.body["code"], error.status), ("SOURCE_BROKEN", 409))
        self.assertEqual(self.calls, [])


class ThroughChartJobs(Fixture):
    def create(self, value):
        return self.apply("chart_create", spec=value)["chart_id"]

    def start(self, chart_id, kind="resolve"):
        request = {"job_id": uuid.uuid4().hex, "expected_state": self.book.state(), "chart_id": chart_id, "kind": kind}
        if kind == "render":
            request.update(width=480, height=300)
        return self.book.chart_jobs().start(request)

    def wait(self, job, seconds=30):
        deadline = time.monotonic() + seconds
        view = job
        while view["status"] == "running":
            self.assertLess(time.monotonic(), deadline, "chart job did not finish")
            time.sleep(0.01)
            view = self.book.chart_jobs().access(job["job_id"])
        return view

    def until(self, condition, seconds=10):
        deadline = time.monotonic() + seconds
        while not condition():
            self.assertLess(time.monotonic(), deadline, "condition not reached")
            time.sleep(0.005)

    def blocked_snapshot(self):
        """Block the first private snapshot taken until ``release`` is set; return ``(entered, release)``."""
        jobs = self.book.query_jobs()
        original = jobs._snapshot
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def snapshot(entry):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(10))
            return original(entry)

        patcher = patch.object(jobs, "_snapshot", side_effect=snapshot)
        patcher.start()
        self.addCleanup(patcher.stop)
        return entered, release

    def recorded_progress(self):
        """Record every progress value chart jobs report from now on."""
        runner = self.book.chart_jobs().jobs
        original = runner.start
        seen = []

        def start(request, prepare, *args, **kwargs):
            def recorded(payload, checkpoint):
                def recording(completed, phase):
                    seen.append(completed)
                    return checkpoint(completed, phase)

                recording.cancel_event = checkpoint.cancel_event
                recording.deadline = checkpoint.deadline
                return prepare(payload, recording)
            return original(request, recorded, *args, **kwargs)

        patcher = patch.object(runner, "start", side_effect=start)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_a_query_stops_at_the_chart_jobs_deadline(self):
        self.save("Endless", ENDLESS)
        chart_id = self.create(query_spec(None, query_source("Endless", "top")))
        self.book.chart_jobs().jobs.seconds = 1
        view = self.wait(self.start(chart_id), seconds=5)
        self.assertEqual(view["status"], "failed", view)
        self.assertEqual(view["error"]["code"], "CALCULATION_FAILED")
        self.until(lambda: not self.book.query_jobs().runs, seconds=2)

    def test_through_a_real_chart_job(self):
        self.save("Parity", "SELECT CASE Units % 2 WHEN 0 THEN 'even' ELSE 'odd' END AS Kind, sum(Units) AS Total "
                            "FROM Sales GROUP BY Kind ORDER BY Kind")
        chart_id = self.create(query_spec(query_source("Parity", "Kind"), query_source("Parity", "Total")))
        view = self.wait(self.start(chart_id, "render"))
        self.assertEqual(view["status"], "completed", view)
        self.assertEqual(view["result"]["resolved"]["categories"], ["even", "odd"])
        self.assertEqual(view["result"]["resolved"]["series"][0]["values"], [42.0, 36.0])
        self.assertTrue(view["result"]["svg"].startswith("<svg"))
        # Progress never goes backwards from a range source to a query source.
        self.apply("edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "even"},
                                                  {"row": 2, "col": 1, "source": "odd"}])
        mixed = query_spec({"kind": "range", "sheet": "Sheet1", "ref": "A1:A2"}, query_source("Parity", "Total"))
        seen = self.recorded_progress()
        view = self.wait(self.start(self.create(mixed)))
        self.assertEqual(view["status"], "completed", view)
        self.assertEqual(view["result"]["resolved"]["categories"], ["even", "odd"])
        self.assertIn(2, seen)
        self.assertEqual(seen, sorted(seen))

    def test_job_is_stale_when_the_workbook_changes_during_the_snapshot(self):
        chart_id = self.create(query_spec(None, query_source("Top", "Units")))
        entered, release = self.blocked_snapshot()
        job = self.start(chart_id)
        self.assertTrue(entered.wait(10))
        self.apply("table_insert", table="Sales", rows=[dict(Month="late", Units=99)])
        release.set()
        view = self.wait(job)
        self.assertEqual(view["status"], "stale", view)
        self.assertNotIn("result", view)
        # The query's own snapshot caught the change: the job failed rather than completing on old data.
        self.assertEqual(view["error"]["code"], "STALE_STATE")
        self.assertEqual(view["error"]["message"], "Query source changed before snapshot")

    def test_cancelling_the_chart_job_stops_a_slow_query(self):
        self.save("Endless", ENDLESS)
        chart_id = self.create(query_spec(None, query_source("Endless", "top")))
        jobs = self.book.query_jobs()
        job = self.start(chart_id)
        self.until(lambda: jobs.runs)
        self.book.chart_jobs().access(job["job_id"], "cancel")
        view = self.wait(job, seconds=2)
        self.assertEqual(view["status"], "cancelled", view)
        self.until(lambda: not jobs.runs, seconds=2)

    def test_a_user_query_job_can_run_while_a_chart_resolves(self):
        chart_id = self.create(query_spec(query_source("Top", "Month"), query_source("Top", "Units")))
        jobs = self.book.query_jobs()
        entered, release = self.blocked_snapshot()
        job = self.start(chart_id)
        self.assertTrue(entered.wait(10))
        accepted = jobs.start(dict(job_id="user", sql="SELECT 1 AS one", expected_state=self.book.state()))
        self.assertEqual(accepted["status"], "running")
        release.set()
        self.assertEqual(self.wait(job)["status"], "completed")
        self.until(lambda: jobs.access("user")["status"] != "running")
        self.assertEqual(jobs.access("user")["status"], "completed")
        jobs.access("user", "release")
        # The reverse: a running user query job does not stop a chart resolving a query source.
        jobs.start(dict(job_id="slow", sql=ENDLESS, expected_state=self.book.state()))
        self.assertEqual(jobs.access("slow")["status"], "running")
        view = self.wait(self.start(chart_id))
        self.assertEqual(view["status"], "completed", view)
        self.assertEqual(view["result"]["resolved"]["series"][0]["values"], [float(n) for n in range(12, 0, -1)])
        jobs.access("slow", "cancel")
        self.until(lambda: jobs.access("slow")["status"] != "running")


if __name__ == "__main__":
    unittest.main()
