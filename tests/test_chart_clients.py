# SPDX-License-Identifier: GPL-3.0-only
import json
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
