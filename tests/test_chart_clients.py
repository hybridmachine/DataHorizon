# SPDX-License-Identifier: GPL-3.0-only
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from datahorizon.client import APIError, LocalService

CLI = [sys.executable, "-m", "datahorizon.cli"]
TYPES = ("column", "bar", "line", "area", "pie", "scatter")


def fill(book):
    cells = [{"row": 1, "col": 1, "source": "Month"}, {"row": 1, "col": 2, "source": "Sales"}, {"row": 1, "col": 3, "source": "Cost"}]
    for row in range(2, 8):
        cells += [{"row": row, "col": 1, "source": f"M{row}"}, {"row": row, "col": 2, "source": str(row * 10)},
                  {"row": row, "col": 3, "source": str(row * 4)}]
    book.apply("edit", "Sheet1", cells=cells)


def parses(svg):
    return ET.fromstring(svg).tag.endswith("svg")


def cli(path, *arguments, data=None):
    return subprocess.run([*CLI, str(path), *arguments], input=data, capture_output=True, text=True)


class ChartClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.service = LocalService()
        cls.client = cls.service.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.service.__exit__(None, None, None)
        cls.temp.cleanup()

    def open_book(self, name):
        book = self.client.open(Path(self.temp.name) / name, create=True)
        self.addCleanup(book.close)
        fill(book)
        return book

    def test_sdk_round_trip_for_all_six_types(self):
        book = self.open_book("types.dhwb")
        for chart_type in TYPES:
            with self.subTest(chart_type):
                spec = book.suggest_chart("Sheet1", r1=1, c1=1, r2=7, c2=3, type=chart_type)["spec"]
                chart_id = book.create_chart(spec)["chart_id"]
                self.assertTrue(parses(book.render_chart(chart_id, 320, 200)["svg"]))
                book.update_chart(chart_id, spec_patch={"title": {"text": "Patched"}})
                self.assertEqual(book.charts(chart_id=chart_id)[0]["spec"]["title"]["text"], "Patched")
                self.assertTrue(parses(book.render_chart(chart_id, 320, 200)["svg"]))
                book.apply("undo")
                self.assertNotEqual(book.charts(chart_id=chart_id)[0]["spec"]["title"]["text"], "Patched")
                book.delete_chart(chart_id)
                self.assertEqual(book.charts(), [])
        self.assertEqual(book.resolve_chart(book.create_chart(spec)["chart_id"])["state"], book.state)

    def test_resolve_chart_raises_on_stale(self):
        book = self.open_book("stale.dhwb")
        chart_id = book.create_chart(book.suggest_chart("Sheet1", r1=1, c1=1, r2=7, c2=2)["spec"])["chart_id"]
        real = book.chart_job

        def moved(*arguments, **keywords):
            started = real(*arguments, **keywords)
            book.set(20, 20, "1")  # the workbook changes after the job starts, before any status read
            return {**started, "status": "running"}

        with patch.object(book, "chart_job", moved), self.assertRaises(APIError) as caught:
            book.resolve_chart(chart_id)
        self.assertEqual((caught.exception.status, caught.exception.body["code"]), (409, "STALE_STATE"))

    def test_failed_job_raises_with_its_error_body(self):
        book = self.open_book("failed.dhwb")
        chart_id = book.create_chart(book.suggest_chart("Sheet1", r1=1, c1=1, r2=7, c2=2)["spec"])["chart_id"]
        book.apply("rows_delete", "Sheet1", start=1, count=20)
        with self.assertRaises(APIError) as caught:
            book.resolve_chart(chart_id)
        self.assertEqual(caught.exception.body["code"], "SOURCE_BROKEN")

    def test_failed_job_raises_with_its_http_status(self):
        book = self.open_book("render-limit.dhwb")
        wide = {"spec_version": 1, "type": "line", "legend": {"position": "none"},
                "series": [{"name": {"text": name}, "values": {"source": {"kind": "range", "sheet": "Sheet1", "ref": "B2:B5002"}}}
                           for name in ("A", "B")]}
        chart_id = book.create_chart(wide)["chart_id"]  # 2 x 5,001 values: over the 10,000-value render limit
        with self.assertRaises(APIError) as caught:
            book.render_chart(chart_id, 320, 200)
        self.assertEqual((caught.exception.status, caught.exception.body["code"]), (400, "LIMIT_RENDER_POINTS"))
        book.apply("rows_delete", "Sheet1", start=1, count=6000)
        with self.assertRaises(APIError) as caught:
            book.resolve_chart(chart_id)
        self.assertEqual((caught.exception.status, caught.exception.body["code"]), (409, "SOURCE_BROKEN"))

    def test_timeout_cancels_the_job(self):
        book = self.open_book("timeout.dhwb")
        chart_id = book.create_chart(book.suggest_chart("Sheet1", r1=1, c1=1, r2=7, c2=2)["spec"])["chart_id"]
        seen = []
        real = book.chart_job_status

        def running(job_id, action=None):
            seen.append(action)
            return {"status": "running"} if action is None else real(job_id, action)

        with patch.object(book, "chart_job_status", running), self.assertRaises(TimeoutError):
            book.resolve_chart(chart_id, timeout=.2)
        self.assertIn("cancel", seen)
        self.assertIn("release", seen)

    def test_timeout_survives_a_failing_cancel(self):
        book = self.open_book("cancel.dhwb")
        chart_id = book.create_chart(book.suggest_chart("Sheet1", r1=1, c1=1, r2=7, c2=2)["spec"])["chart_id"]
        seen = []
        real = book.chart_job_status

        def failing(job_id, action=None):
            seen.append(action)
            if action == "cancel":
                raise OSError("connection lost")
            return {"status": "running"} if action is None else real(job_id, action)

        with patch.object(book, "chart_job_status", failing), self.assertRaises(TimeoutError):
            book.resolve_chart(chart_id, timeout=.2)
        self.assertIn("release", seen)

    def test_timed_out_job_is_released_only_after_it_has_stopped(self):
        book = self.open_book("slowcancel.dhwb")
        seen, after_cancel = [], []

        def slow(job_id, action=None):
            seen.append(action)
            if action == "release":
                if after_cancel.count(None) < 3:
                    raise APIError(409, {"code": "JOB_RUNNING", "message": "still running", "outcome": "rejected"})
                return {"released": True}
            if "cancel" in seen:
                after_cancel.append(action)
            return {"status": "cancelled" if after_cancel.count(None) >= 3 else "running"}

        with patch.object(book, "chart_job_status", slow), self.assertRaises(TimeoutError):
            book._chart_result({"job_id": "j", "status": "running"}, .1)
        self.assertEqual(seen.count("release"), 1)
        self.assertGreaterEqual(after_cancel.count(None), 3)

    def test_svg_out_never_replaces_the_workbook_or_an_existing_file_unasked(self):
        path = Path(self.temp.name) / "guard.dhwb"
        with LocalService() as client, client.open(path, create=True) as book:
            fill(book)
            chart_id = book.create_chart(book.suggest_chart("Sheet1", r1=1, c1=1, r2=7, c2=2)["spec"])["chart_id"]
        before = path.read_bytes()
        symlink, hardlink = Path(self.temp.name) / "alias-symlink", Path(self.temp.name) / "alias-hardlink"
        symlink.symlink_to(path)
        os.link(path, hardlink)

        def render(target, *extra):
            return cli(path, "charts", "render", chart_id, "--width", "320", "--height", "200", "--svg-out", str(target), *extra)

        for target in (path, symlink, hardlink):
            for extra in ((), ("--overwrite",)):
                refused = render(target, *extra)
                self.assertNotEqual(refused.returncode, 0, (target, extra))
                self.assertEqual(refused.stdout, "")
                self.assertEqual(path.read_bytes(), before, (target, extra))
        other = Path(self.temp.name) / "existing.svg"
        other.write_text("keep", encoding="utf-8")
        refused = render(other)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(other.read_text(encoding="utf-8"), "keep")
        replaced = render(other, "--overwrite")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertTrue(parses(other.read_text(encoding="utf-8")))
        self.assertEqual(path.read_bytes(), before)
        with LocalService() as client, client.open(path) as book:
            self.assertEqual(len(book.charts()), 1)

    def test_sdk_rest_and_cli_agree(self):
        path = Path(self.temp.name) / "agree.dhwb"
        with LocalService() as client, client.open(path, create=True) as book:
            fill(book)
            spec = book.suggest_chart("Sheet1", r1=1, c1=1, r2=7, c2=3)["spec"]
            receipt = book.create_chart(spec, name="Sales", anchor={"row": 3, "col": 5})
            sdk = book.charts()
            raw = client.request(book.base + "/charts", {"expected_state": book.state})["charts"]
            self.assertEqual(sdk, raw)
            self.assertEqual((sdk[0]["id"], sdk[0]["name"]), (receipt["chart_id"], "Sales"))
        listed = cli(path, "charts", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["charts"], sdk)

    def test_render_cli_writes_svg_file(self):
        path = Path(self.temp.name) / "cli.dhwb"
        with LocalService() as client, client.open(path, create=True) as book:
            fill(book)
        suggested = cli(path, "charts", "suggest", "--sheet", "Sheet1", "--r1", "1", "--c1", "1", "--r2", "7", "--c2", "2", "--type", "line")
        self.assertEqual(suggested.returncode, 0, suggested.stderr)
        spec = json.loads(suggested.stdout)["spec"]
        created = cli(path, "charts", "create", "--sheet", "Sheet1", data=json.dumps({"spec": spec, "name": "Trend"}))
        self.assertEqual(created.returncode, 0, created.stderr)
        chart_id = json.loads(created.stdout)["chart_id"]
        updated = cli(path, "charts", "update", chart_id, data=json.dumps({"spec_patch": {"title": {"text": "Hi"}}}))
        self.assertEqual(updated.returncode, 0, updated.stderr)
        resolved = cli(path, "charts", "resolve", chart_id)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertIn("resolved", json.loads(resolved.stdout))
        target = Path(self.temp.name) / "out.svg"
        rendered = cli(path, "charts", "render", chart_id, "--width", "320", "--height", "200", "--svg-out", str(target))
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        body = json.loads(rendered.stdout)
        self.assertEqual(body["svg_path"], str(target))
        self.assertNotIn("svg", body)
        self.assertTrue(parses(target.read_text(encoding="utf-8")))
        inline = json.loads(cli(path, "charts", "render", chart_id, "--width", "320", "--height", "200").stdout)
        self.assertTrue(parses(inline["svg"]))
        deleted = cli(path, "charts", "delete", chart_id)
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(json.loads(cli(path, "charts", "list").stdout)["charts"], [])

    def test_export_reports_charts_as_native_only(self):
        book = self.open_book("export.dhwb")
        spec = book.suggest_chart("Sheet1", r1=1, c1=1, r2=7, c2=2)["spec"]
        book.create_chart(spec, name="Sales")
        book.apply("sheet_add", name="Summary")
        book.create_chart(spec, sheet="Summary")
        charts_line = "Charts are native-only and omitted from this export: Sales on Sheet1, Chart 1 on Summary"
        for form in ("xlsx", "ods"):
            with self.subTest(form=form):
                whole = book.export_spreadsheet(form, scope="workbook")["report"]
                self.assertEqual([line for line in whole if line.startswith("Charts ")], [charts_line])
                selection = book.export_spreadsheet(form, sheet="Sheet1", r1=1, c1=1, r2=3, c2=3)["report"]
                self.assertIn(charts_line, selection)
        csv = self.client.request(book.base + "/export/csv", dict(sheet="Sheet1", r1=1, c1=1, r2=3, c2=3))["report"]
        self.assertFalse([line for line in csv if "Chart" in line])
        empty = self.open_book("no-charts.dhwb")
        for form in ("xlsx", "ods"):
            self.assertFalse([line for line in empty.export_spreadsheet(form, scope="workbook")["report"] if line.startswith("Charts ")])

    def test_cli_error_is_structured_json_on_stderr_with_exit_1(self):
        path = Path(self.temp.name) / "error.dhwb"
        with LocalService() as client, client.open(path, create=True):
            pass
        result = cli(path, "charts", "resolve", "no-such-chart")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["code"], "CHART_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
