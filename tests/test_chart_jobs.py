# SPDX-License-Identifier: GPL-3.0-only
"""Chart resolve and render jobs over the in-process REST dispatch."""

import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import xml.etree.ElementTree as ET

from datahorizon.engine import EngineError, Workbook
from datahorizon.service import Service, openapi

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ALLOWED = {"svg", "g", "rect", "line", "path", "circle", "text"}


def source(ref, sheet="Sheet1"):
    return {"kind": "range", "sheet": sheet, "ref": ref}


def spec():
    return {"spec_version": 1, "type": "column", "categories": {"source": source("A2:A13")},
            "series": [{"name": {"source": source("B1")}, "values": {"source": source("B2:B13")}}]}


def code(caught):
    return caught.exception.body["code"]


class ChartJobTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.server = self.enterContext(Service())
        info = self.server.dispatch("POST", "/v1/open", {"path": str(pathlib.Path(temp) / "charts.dhwb"), "create": True})
        self.base = f"/v1/workbooks/{info['id']}"
        self.book = self.server.workbooks[info["id"]]
        cells = [{"row": 1, "col": 1, "source": "Month"}, {"row": 1, "col": 2, "source": "Units"}]
        for index, month in enumerate(MONTHS, start=2):
            cells += [{"row": index, "col": 1, "source": month}, {"row": index, "col": 2, "source": f"={index - 1}*2"}]
        self.operate("edit", cells=cells)
        self.chart = self.operate("chart_create", sheet="Sheet1", spec=spec())["chart_id"]

    def state(self):
        return self.server.dispatch("GET", self.base, {})["state"]

    def operate(self, action, **args):
        return self.server.dispatch("POST", self.base + "/operations",
                                    {"action": action, "operation_id": str(uuid.uuid4()), "expected_state": self.state(), **args})

    def edit_cell(self, ref, text):
        col = ord(ref[0]) - 64
        self.operate("edit", cells=[{"row": int(ref[1:]), "col": col, "source": text}])

    def request(self, kind="resolve", **overrides):
        body = {"job_id": uuid.uuid4().hex, "expected_state": self.state(), "chart_id": self.chart, "kind": kind}
        if kind == "render":
            body.update(width=480, height=300)
        body.update(overrides)
        return {key: value for key, value in body.items() if value is not None}

    def start(self, kind="resolve", **overrides):
        return self.server.dispatch("POST", self.base + "/charts/jobs", self.request(kind, **overrides))

    def get(self, job):
        return self.server.dispatch("GET", f"{self.base}/charts/jobs/{job['job_id']}", {})

    def act(self, job, action):
        return self.server.dispatch("POST", f"{self.base}/charts/jobs/{job['job_id']}/{action}", {})

    def wait(self, job, seconds=30):
        deadline = time.monotonic() + seconds
        view = job
        while view["status"] == "running":
            self.assertLess(time.monotonic(), deadline, "chart job did not finish")
            time.sleep(0.01)
            view = self.get(job)
        return view

    def test_resolve_job_completes_with_series_and_state(self):
        state = self.state()
        job = self.start("resolve")
        self.assertEqual((job["chart_id"], job["kind"], job["state"]), (self.chart, "resolve", state))
        view = self.wait(job)
        self.assertEqual(view["status"], "completed", view)
        self.assertEqual((view["chart_id"], view["kind"], view["state"]), (self.chart, "resolve", state))
        result = view["result"]
        self.assertEqual(result["state"], state)
        self.assertEqual(result["resolved"]["categories"], MONTHS)
        self.assertEqual(result["resolved"]["series"], [{"name": "Units", "values": [float(2 * n) for n in range(1, 13)], "x": None}])
        self.assertEqual(result["counts"], {"points": 12, "gaps": {"empty": 0, "text": 0, "error": 0}})
        self.assertNotIn("svg", result)
        self.assertEqual(self.state(), state)

    def test_resolve_calculates_thousands_of_formulas_inside_the_job(self):
        cells = []
        for row in range(1, 3001):
            cells += [{"row": row, "col": 4, "source": str(row)}, {"row": row, "col": 5, "source": f"=D{row}*2+1"}]
        self.operate("edit", cells=cells)
        formulas = {"spec_version": 1, "type": "line", "legend": {"position": "none"},
                    "series": [{"name": {"text": "Derived"}, "values": {"source": source("E1:E3000")}}]}
        self.chart = self.operate("chart_create", sheet="Sheet1", spec=formulas)["chart_id"]
        view = self.wait(self.start("resolve"), seconds=60)
        self.assertEqual(view["status"], "completed", {key: value for key, value in view.items() if key != "result"})
        self.assertEqual(view["result"]["counts"], {"points": 3000, "gaps": {"empty": 0, "text": 0, "error": 0}})
        self.assertEqual(view["result"]["resolved"]["series"][0]["values"][-1], 6001.0)

    def test_render_job_returns_svg_marks_and_size(self):
        view = self.wait(self.start("render", width=640, height=400))
        self.assertEqual(view["status"], "completed", view)
        result = view["result"]
        self.assertEqual((result["width"], result["height"]), (640, 400))
        root = ET.fromstring(result["svg"])
        self.assertLessEqual({element.tag.split("}")[-1] for element in root.iter()}, ALLOWED)
        self.assertEqual((root.get("width"), root.get("height")), ("640", "400"))
        self.assertEqual(len(result["marks"]), 12)
        self.assertEqual(result["resolved"]["series"][0]["name"], "Units")
        self.assertEqual(result["counts"]["points"], 12)

    def test_render_needs_a_size_and_resolve_refuses_one(self):
        bad = [self.request("render", width=None, height=None), self.request("render", height=None),
               self.request("render", width=63), self.request("render", width=4097), self.request("render", height=47),
               self.request("render", height=4097), self.request("render", width=True), self.request("render", width=480.0),
               self.request("render", width="480"), self.request("resolve", width=480, height=300),
               self.request("resolve", width=480), self.request("draw"), self.request(colour="red"),
               self.request(chart_id=7), {key: value for key, value in self.request().items() if key != "kind"},
               {key: value for key, value in self.request().items() if key != "chart_id"}]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(EngineError) as caught:
                self.server.dispatch("POST", self.base + "/charts/jobs", body)
            self.assertEqual(code(caught), "INVALID_ARGUMENT")
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.book.chart_jobs().jobs.entries, {})
        for width, height in ((64, 48), (4096, 4096)):
            view = self.wait(self.start("render", width=width, height=height))
            self.assertEqual(view["status"], "completed", view)
            self.act(view, "release")

    def test_unknown_chart_is_refused_at_once(self):
        with self.assertRaises(EngineError) as caught:
            self.start("resolve", chart_id=str(uuid.uuid4()))
        self.assertEqual((code(caught), caught.exception.status), ("CHART_NOT_FOUND", 404))
        self.assertEqual(self.book.chart_jobs().jobs.entries, {})

    def test_job_is_stale_after_any_managed_edit(self):
        job = self.start("resolve")
        self.assertEqual(self.wait(job)["status"], "completed")
        self.edit_cell("Z99", "1")
        view = self.get(job)
        self.assertEqual(view["status"], "stale")
        self.assertNotIn("result", view)

    def test_job_that_finishes_after_an_edit_is_stale(self):
        entered, resume, read = threading.Event(), threading.Event(), Workbook.read

        def delayed(book, *args, **kwargs):
            if book.readonly:
                entered.set()
                resume.wait(5)
            return read(book, *args, **kwargs)

        with patch.object(Workbook, "read", delayed):
            job = self.start("resolve")
            self.assertTrue(entered.wait(5))
            self.edit_cell("Z99", "1")
            resume.set()
            view = self.wait(job)
        self.assertEqual(view["status"], "stale")
        self.assertNotIn("result", view)

    def test_job_started_against_an_old_state_is_refused(self):
        old = self.state()
        self.edit_cell("Z99", "1")
        with self.assertRaises(EngineError) as caught:
            self.start("resolve", expected_state=old)
        self.assertEqual((code(caught), caught.exception.status), ("STALE_STATE", 409))
        self.assertEqual(self.book.chart_jobs().jobs.entries, {})

    def test_cancel_and_release(self):
        entered, resume, read = threading.Event(), threading.Event(), Workbook.read

        def delayed(book, *args, **kwargs):
            if book.readonly:
                entered.set()
                resume.wait(5)
            return read(book, *args, **kwargs)

        with patch.object(Workbook, "read", delayed):
            job = self.start("render")
            self.assertTrue(entered.wait(5))
            with self.assertRaises(EngineError) as caught:
                self.act(job, "release")
            self.assertEqual((code(caught), caught.exception.status), ("JOB_RUNNING", 409))
            self.assertEqual(self.act(job, "cancel")["kind"], "render")
            resume.set()
            view = self.wait(job)
        self.assertEqual(view["status"], "cancelled")
        self.assertNotIn("result", view)
        self.assertEqual(view["error"]["code"], "CANCELLED")
        self.assertEqual(self.act(job, "release"), {"released": True, "job_id": job["job_id"]})
        with self.assertRaises(EngineError) as caught:
            self.get(job)
        self.assertEqual((code(caught), caught.exception.status), ("JOB_NOT_FOUND", 404))
        completed = self.start("resolve")
        self.assertEqual(self.wait(completed)["status"], "completed")
        discarded = self.act(completed, "cancel")
        self.assertEqual(discarded["status"], "cancelled")
        self.assertNotIn("result", discarded)
        with self.assertRaises(EngineError) as caught:
            self.server.dispatch("POST", f"{self.base}/charts/jobs/{completed['job_id']}/restart", {})
        self.assertEqual((code(caught), caught.exception.status), ("NOT_FOUND", 404))

    def test_same_job_id_with_other_inputs_conflicts(self):
        body = self.request("render")
        first = self.server.dispatch("POST", self.base + "/charts/jobs", body)
        again = self.server.dispatch("POST", self.base + "/charts/jobs", dict(body))
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertEqual(len(self.book.chart_jobs().jobs.entries), 1)
        for change in ({"width": 481}, {"kind": "resolve", "width": None, "height": None}):
            other = {key: value for key, value in {**body, **change}.items() if value is not None}
            with self.subTest(change=change), self.assertRaises(EngineError) as caught:
                self.server.dispatch("POST", self.base + "/charts/jobs", other)
            self.assertEqual((code(caught), caught.exception.status), ("JOB_CONFLICT", 409))
        self.assertEqual(self.wait(first)["status"], "completed")

    def test_failed_job_carries_the_structured_error(self):
        self.operate("rows_delete", sheet="Sheet1", start=1, count=20)
        view = self.wait(self.start("resolve"))
        self.assertEqual(view["status"], "failed")
        self.assertNotIn("result", view)
        self.assertEqual(view["error"]["code"], "SOURCE_BROKEN")
        self.assertEqual(view["error"]["outcome"], "rejected")

    def test_render_over_the_point_limit_is_refused(self):
        self.operate("edit", cells=[{"row": row, "col": 3, "source": str(row % 97 + 1)} for row in range(1, 2001)])
        wide = {"spec_version": 1, "type": "column", "legend": {"position": "none"},
                "series": [{"name": {"text": f"S{index}"}, "values": {"source": source("C1:C2000")}} for index in range(48)]}
        self.chart = self.operate("chart_create", sheet="Sheet1", spec=wide)["chart_id"]
        view = self.wait(self.start("render", width=4096, height=4096), seconds=120)
        self.assertEqual(view["status"], "failed", {key: value for key, value in view.items() if key != "result"})
        self.assertEqual(view["error"]["code"], "LIMIT_RENDER_POINTS")
        self.assertNotIn("result", view)

    def test_resolve_over_the_response_limit_is_refused(self):
        label = "x" * 2_500
        for first in range(1, 2001, 500):
            self.operate("edit", cells=[{"row": row, "col": 4, "source": f"{row} {label}"} for row in range(first, first + 500)])
        long_labels = {"spec_version": 1, "type": "column", "categories": {"source": source("D1:D2000")},
                       "series": [{"name": {"text": "S"}, "values": {"source": source("C1:C2000")}}]}
        self.chart = self.operate("chart_create", sheet="Sheet1", spec=long_labels)["chart_id"]
        view = self.wait(self.start("resolve"), seconds=120)
        self.assertEqual(view["status"], "failed", {key: value for key, value in view.items() if key != "result"})
        self.assertEqual(view["error"]["code"], "READ_LIMIT")
        self.assertNotIn("result", view)

    def test_suggest_route(self):
        out = self.server.dispatch("POST", self.base + "/charts/suggest",
                                   {"expected_state": self.state(), "sheet": "Sheet1", "r1": 1, "c1": 1, "r2": 13, "c2": 2})
        self.assertEqual(out["spec"]["categories"], {"source": source("A2:A13")})
        self.assertEqual(out["spec"]["series"], [{"name": {"source": source("B1")}, "values": {"source": source("B2:B13")}}])
        with self.assertRaises(EngineError) as caught:
            self.server.dispatch("POST", self.base + "/charts/suggest", {"expected_state": "old", "r2": 13, "c2": 2})
        self.assertEqual((code(caught), caught.exception.status), ("STALE_STATE", 409))

    def test_openapi_lists_chart_routes_and_schemas(self):
        document = openapi()
        schemas = document["components"]["schemas"]
        expected = {"/v1/workbooks/{workbook}/charts": "post", "/v1/workbooks/{workbook}/charts/suggest": "post",
                    "/v1/workbooks/{workbook}/charts/jobs": "post", "/v1/workbooks/{workbook}/charts/jobs/{job}": "get",
                    "/v1/workbooks/{workbook}/charts/jobs/{job}/cancel": "post",
                    "/v1/workbooks/{workbook}/charts/jobs/{job}/release": "post"}
        chart_paths = {path: set(methods) for path, methods in document["paths"].items() if "/charts" in path}
        self.assertEqual(chart_paths, {path: {method} for path, method in expected.items()})
        prefix = "#/components/schemas/"
        for path, method in expected.items():
            with self.subTest(path=path):
                operation = document["paths"][path][method]
                refs = [operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]]
                if method == "post":
                    refs.append(operation["requestBody"]["content"]["application/json"]["schema"]["$ref"])
                for ref in refs:
                    self.assertTrue(ref.startswith(prefix))
                    self.assertIn(ref[len(prefix):], schemas)
        for name in ("ChartSpec", "ChartSource", "Chart", "ChartSuggestRequest", "ChartJobRequest", "ChartJob"):
            self.assertIn(name, schemas)

        def refs_in(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "$ref":
                        yield item
                    else:
                        yield from refs_in(item)
            elif isinstance(value, list):
                for item in value:
                    yield from refs_in(item)

        for name in [name for name in schemas if name.startswith("Chart")]:
            for ref in refs_in(schemas[name]):
                with self.subTest(schema=name, ref=ref):
                    self.assertIn(ref[len(prefix):], schemas)


class ChartJobCalculationTests(unittest.TestCase):
    """Formula sources calculated by a real ChartJobs job on a reopened workbook."""

    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = pathlib.Path(temp) / "chain.dhwb"
        self.book = Workbook(self.path, create=True)
        self.addCleanup(lambda: self.book.close())

    def apply(self, action, **args):
        return self.book.mutate({"action": action, "operation_id": str(uuid.uuid4()), "expected_state": self.book.state(), **args})

    def chart_over(self, ref):
        line = {"spec_version": 1, "type": "line", "legend": {"position": "none"},
                "series": [{"name": {"text": "Chain"}, "values": {"source": source(ref)}}]}
        return self.apply("chart_create", sheet="Sheet1", spec=line)["chart_id"]

    def reopen(self):
        self.book.close()
        self.book = Workbook(self.path)

    def resolve(self, chart_id):
        start = {"job_id": uuid.uuid4().hex, "expected_state": self.book.state(), "chart_id": chart_id, "kind": "resolve"}
        job = self.book.chart_jobs().start(start)
        deadline = time.monotonic() + 60
        while job["status"] == "running":
            self.assertLess(time.monotonic(), deadline, "chart job did not finish")
            time.sleep(0.01)
            job = self.book.chart_jobs().access(start["job_id"])
        return job

    def chain(self, rows):
        cells = [{"row": 1, "col": 1, "source": "1"}]
        cells += [{"row": row, "col": 1, "source": f"=A{row - 1}+1"} for row in range(2, rows + 1)]
        self.apply("edit", sheet="Sheet1", cells=cells)

    def test_a_formula_chain_across_slices_resolves_without_gaps(self):
        self.chain(2500)
        chart_id = self.chart_over("A1:A2500")
        self.reopen()
        view = self.resolve(chart_id)
        self.assertEqual(view["status"], "completed", {key: value for key, value in view.items() if key != "result"})
        self.assertEqual(view["result"]["counts"], {"points": 2500, "gaps": {"empty": 0, "text": 0, "error": 0}})
        self.assertEqual(view["result"]["resolved"]["series"][0]["values"][-1], 2500.0)

    def test_a_limit_error_cell_fails_the_resolve(self):
        self.chain(300)
        chart_id = self.chart_over("A300")  # its own chain of 300 cells is deeper than the engine allows
        self.reopen()
        view = self.resolve(chart_id)
        self.assertEqual(view["status"], "failed")
        self.assertNotIn("result", view)
        self.assertEqual(view["error"]["code"], "CALCULATION_FAILED")
        self.assertEqual(view["error"]["message"], "A300 could not be calculated: Dependency chain exceeds 128 cells")

    def test_a_division_by_zero_cell_stays_a_counted_gap(self):
        self.apply("edit", sheet="Sheet1", cells=[{"row": 1, "col": 1, "source": "1"}, {"row": 2, "col": 1, "source": "=1/0"},
                                                  {"row": 3, "col": 1, "source": "=A1+1"}])
        chart_id = self.chart_over("A1:A3")
        self.reopen()
        view = self.resolve(chart_id)
        self.assertEqual(view["status"], "completed", view)
        self.assertEqual(view["result"]["counts"], {"points": 2, "gaps": {"empty": 0, "text": 0, "error": 1}})


class ChartJobAccessorTests(unittest.TestCase):
    def test_concurrent_first_requests_share_one_chart_jobs(self):
        from datahorizon import chart_jobs

        temp = self.enterContext(tempfile.TemporaryDirectory())
        book = Workbook(pathlib.Path(temp) / "jobs.dhwb", create=True)
        self.addCleanup(book.close)
        real = chart_jobs.ChartJobs.__init__

        def slow(self, *args, **kwargs):
            time.sleep(0.05)  # widen the window between the check and the assignment
            real(self, *args, **kwargs)

        gate = threading.Barrier(2)
        seen = []

        def first():
            gate.wait()
            seen.append(book.chart_jobs())

        with patch.object(chart_jobs.ChartJobs, "__init__", slow):
            threads = [threading.Thread(target=first) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0], seen[1])


if __name__ == "__main__":
    unittest.main()
