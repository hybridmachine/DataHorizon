# SPDX-License-Identifier: GPL-3.0-only
"""Exercise the six managed structural jobs and verify the reopened SQLite workbooks.

Each case starts with a fresh copy of the same native fixture. RSS is sampled from the
service child. Disk figures distinguish named workbook/journal growth from free-space
change on the volume, which can also include unlinked SQLite temp files and other users.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from itertools import zip_longest
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'service'))
from datahorizon.client import APIError, LocalService
from datahorizon.engine import Workbook

COLUMNS = 20
ACTIONS = ("rows_insert", "rows_delete", "columns_insert", "columns_delete",
           "sheet_duplicate", "sheet_delete")


def shift(index, action, axis):
    if not action.startswith(axis + "_") or index < 2:
        return index
    if action.endswith("_insert"):
        return index + 1
    return index - 1 if index > 2 else None


def expected_cells(rows, action):
    """Map original coordinates to their independently expected destination in order."""
    target_col = shift(3, action, "columns")
    target_letter = chr(ord("A") + target_col - 1)
    for row in range(1, rows + 1):
        dest_row = shift(row, action, "rows")
        if dest_row is None:
            continue
        for col in range(1, COLUMNS + 1):
            dest_col = shift(col, action, "columns")
            if dest_col is None:
                continue
            if col == COLUMNS and row % 100 == 0:
                yield dest_row, dest_col, f"=Other!A1+{target_letter}{dest_row}", "formula", '{"bold":true}'
            else:
                yield dest_row, dest_col, str((row - 1) * COLUMNS + col), "number", "{}"


def create_fixture(path, rows):
    book = Workbook(path, create=True)
    def apply(action, **args):
        return book.mutate(dict(action=action, expected_state=book.state(), operation_id=uuid.uuid4().hex, **args))
    try:
        apply("sheet_add", name="Other")
        data = book.sheet("Sheet1")
        other = book.sheet("Other")
        chart_spec = {"spec_version": 1, "type": "line", "series": [
            {"name": {"text": "Sales"}, "values": {"source": {
                "kind": "range", "sheet": "Sheet1", "ref": "C100:C101"}}}]}
        charts = {s: apply("chart_create", sheet=s, spec=chart_spec, anchor={"row": 100, "col": 3})["chart_id"]
                  for s in ("Sheet1", "Other")}
        book.db.execute("BEGIN")
        book.db.executemany("INSERT INTO cells(sheet_id,row,col,source,kind,style) VALUES(?,?,?,?,?,?)",
                            ((data, *cell) for cell in expected_cells(rows, "")))
        book.db.execute("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,1,1,'1','number')", (other,))
        book.db.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,2,?,'formula')",
                            ((other, row, f"=Sheet1!C{row}") for row in range(100, rows + 1, 100)))
        book.db.execute("COMMIT")
        revision, = book.db.execute("SELECT revision FROM dh_meta").fetchone()
        return dict(data=data, other=other, charts=charts, revision=revision)
    finally:
        book.close()


def verify_reopened(path, rows, original, action):
    """Read persisted inputs, formula text, styles, sheet identity, chart anchors and sources."""
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
        sheet_ids = dict(db.execute("SELECT name,id FROM sheets"))
        expected_names = {"Other"}
        if action != "sheet_delete":
            expected_names.add("Sheet1")
        if action == "sheet_duplicate":
            expected_names.add("Copy")
        assert set(sheet_ids) == expected_names and sheet_ids["Other"] == original["other"]
        for name in sorted(expected_names - {"Other"}):
            assert (sheet_ids[name] == original["data"]) == (name == "Sheet1")
            cells = db.execute("SELECT row,col,source,kind,style FROM cells WHERE sheet_id=? ORDER BY row,col", (sheet_ids[name],))
            for actual, expected in zip_longest(cells, expected_cells(rows, action)):
                if actual != expected:
                    raise AssertionError((action, name, actual, expected))
        other_cells = db.execute("SELECT row,col,source FROM cells WHERE sheet_id=? ORDER BY row,col", (sheet_ids["Other"],))
        assert next(other_cells) == (1, 1, "1")
        col = shift(3, action, "columns")
        for actual, row in zip_longest(other_cells, range(100, rows + 1, 100)):
            ref = "=#REF!" if action == "sheet_delete" else f"=Sheet1!{chr(64+col)}{shift(row, action, 'rows')}"
            assert actual == (row, 2, ref), (action, row, actual, ref)
        chart_rows = {i: (owner, json.loads(p)) for i, owner, p in
                      db.execute("SELECT id,sheet_id,payload FROM dh_objects WHERE namespace='chart'")}
        for name, chart in original["charts"].items():
            if name == "Sheet1" and action == "sheet_delete":
                assert chart not in chart_rows
                continue
            owner, payload = chart_rows.pop(chart)
            assert owner == sheet_ids[name]
            row = shift(100, action, "rows")
            anchor = {"row": row, "col": col} if name == "Sheet1" else {"row": 100, "col": 3}
            assert payload["anchor"] == anchor
            source = payload["spec"]["series"][0]["values"]["source"]
            coords = tuple(source[k] for k in ("sheet_id", "row1", "col1", "row2", "col2"))
            expected = (None,) * 5 if action == "sheet_delete" else (original["data"], row, col, row + 1, col)
            assert coords == expected, (action, coords, expected)
        assert not chart_rows
        revision, = db.execute("SELECT revision FROM dh_meta").fetchone()
        assert revision == original["revision"] + bool(action), (action, revision)


class DiskSampler:
    def __init__(self, path):
        self.path = path
        self.initial_file = path.stat().st_size
        self.initial_free = shutil.disk_usage(path.parent).free
        self.named_growth = self.volume_delta = 0
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.run)

    def sample(self):
        named = 0
        for sibling in self.path.parent.glob(self.path.name + "*"):
            try:
                named += sibling.stat().st_size
            except FileNotFoundError:
                pass
        self.named_growth = max(self.named_growth, named - self.initial_file)
        self.volume_delta = max(self.volume_delta, self.initial_free - shutil.disk_usage(self.path.parent).free)

    def run(self):
        while not self.done.wait(.05):
            self.sample()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.done.set()
        self.thread.join()
        self.sample()


def refusal(client, url, request, expected):
    try:
        client.request(url, request)
    except APIError as error:
        assert error.body["code"] == expected, error.body
        return error.body
    raise AssertionError(f"Expected {expected}")


def run_case(root, template, rows, original, action, mode="complete"):
    from qualify_large_interchange import MeasuredService

    with tempfile.TemporaryDirectory(dir=root) as directory:
        path = Path(directory) / "case.dhwb"
        shutil.copyfile(template, path)
        svc = MeasuredService(path.parent / "tmp", action)
        client = svc.client
        operation = uuid.uuid4().hex
        report = {"action": action, "mode": mode}
        try:
            book = client.open(path)
            url = book.base + "/operations"
            lookup = url + "/" + operation
            args = {"start": 2, "count": 1} if action.startswith(("rows_", "columns_")) else {}
            if action == "sheet_duplicate":
                args["name"] = "Copy"
            command = dict(action=action, sheet="Sheet1", expected_state=book.state, operation_id=operation, **args)
            rejected = refusal(client, url, command, "LARGE_STRUCTURE_CONFIRMATION_REQUIRED")
            assert rejected["operation_id"] == operation and rejected["outcome"] == "rejected"
            confirmed = dict(command, confirm_large_structure=True)
            with svc.measure("confirm_to_terminal") as measured, DiskSampler(path) as disk:
                if mode == "unread_response":
                    # Discard the first response before decoding its job ID, as a caller that lost its acknowledgement.
                    post = Request(client.url + url, data=json.dumps(confirmed).encode(), headers={
                        "Content-Type": "application/json", "Authorization": "Bearer " + client.token})
                    with client._open(post, timeout=15):
                        pass
                else:
                    view = client.request(url, confirmed)
                    assert view["operation_id"] == operation and view["status"] == "running", view
                if mode in ("kill", "cancel"):
                    deadline = time.monotonic() + 120
                    journal = Path(str(path) + "-journal")
                    while not journal.exists() or journal.stat().st_size < 32768:
                        if time.monotonic() >= deadline or client.request(lookup + "/status")["status"] != "running":
                            raise AssertionError("Job ended before an active native journal could be observed")
                        time.sleep(.005)
                    if mode == "kill":
                        svc.local.process.kill()
                        svc.local.process.wait(timeout=10)
                    else:
                        assert client.request(lookup + "/cancel", {})["cancel_requested"]
                if mode != "kill":
                    deadline = time.monotonic() + 330
                    phases = []
                    while True:
                        view = client.request(lookup + "/status")
                        phase = view.get("phase")
                        if phase and (not phases or phases[-1] != phase):
                            phases.append(phase)
                        if view["status"] != "running":
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError(view)
                        time.sleep(.05)
                    assert view["operation_id"] == operation
                    if mode == "cancel":
                        assert view["status"] == "failed" and view["error"]["code"] == "CANCELLED", view
                        assert view["error"]["outcome"] == "rolled_back", view
                    else:
                        receipt = view["receipt"]
                        assert view["status"] == "completed" and receipt["outcome"] == "committed"
                        assert client.request(lookup) == receipt
                        replay = client.request(url, confirmed)
                        if "receipt" in replay:
                            replay = replay["receipt"]
                        assert replay == receipt, replay
                    report["phases_observed"] = phases
            report.update(measured, peak_named_growth_bytes=disk.named_growth,
                          peak_volume_free_space_delta_bytes=disk.volume_delta)
            if mode != "kill":
                book.close()
        finally:
            report["service_lifetime_peak_rss_bytes"] = svc.stop()
        expected_action = "" if mode in ("cancel", "kill") else action
        # A new service uses a fresh connection, performs native validation, and has
        # none of the previous worker's in-memory job state.
        with LocalService() as reopened, reopened.open(path) as fresh:
            recovered = fresh.base + "/operations/" + operation
            if mode == "kill":
                no_receipt = refusal(reopened, recovered, None, "OUTCOME_UNKNOWN")
                assert no_receipt["outcome"] == "unknown"
                report["receipt_after_restart"] = "unknown"
            else:
                persisted = reopened.request(recovered)
                assert persisted["operation_id"] == operation
                assert persisted["outcome"] == ("rolled_back" if mode == "cancel" else "committed")
                report["receipt_after_restart"] = persisted["outcome"]
        verify_reopened(path, rows, original, expected_action)
        report["reopened"] = "before" if not expected_action else "after"
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 600 <= args.rows < 1_048_576:
        parser.error("--rows must be between 600 and 1,048,575")
    with tempfile.TemporaryDirectory() as directory:
        template = Path(directory) / "fixture.dhwb"
        original = create_fixture(template, args.rows)
        report = {"date_utc": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
                  "machine": platform.machine(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                  "base_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                  "source_sha256": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in (Path(__file__).resolve(), *(ROOT / "service/datahorizon" / name for name in
                                              ("engine.py", "large_structure.py", "service.py", "client.py")))},
                  "cells_on_edited_sheet": args.rows * COLUMNS, "columns": COLUMNS, "rows": args.rows,
                  "cross_sheet_formulas": 2 * (args.rows // 100), "initial_native_bytes": template.stat().st_size,
                  "sampling_seconds": .05, "cases": []}
        for action, mode in [(a, "unread_response" if a == "rows_insert" else "complete") for a in ACTIONS] + [
                ("rows_insert", "cancel"), ("rows_insert", "kill")]:
            result = run_case(directory, template, args.rows, original, action, mode)
            report["cases"].append(result)
            print(f"{action} {mode}: {result['seconds']} s, reopened {result['reopened']}", flush=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
