# SPDX-License-Identifier: GPL-3.0-only
"""Crash-recovery tests killing a child Workbook process with SIGKILL at four store boundaries."""

from contextlib import closing
from datetime import datetime
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "service") not in sys.path:
    sys.path.insert(0, str(ROOT / "service"))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from datahorizon import engine
from datahorizon.calculation_store import CalculationStore
from datahorizon.engine import Workbook
from test_calculation_differential import assert_book_matches_oracle


def _seed_crash_workbook(path: Path) -> str:
    """Create a workbook with chains, ranges, cross-sheet refs, and stored results."""
    book = Workbook(path, create=True)
    try:
        def apply(action="edit", **args):
            return book.mutate(dict(action=action, operation_id=str(uuid.uuid4()), expected_state=book.state(), **args))

        apply("sheet_add", name="Sheet2")
        apply(sheet="Sheet1", cells=[
            dict(row=1, col=1, source="2"),
            dict(row=2, col=1, source="3"),
            dict(row=3, col=1, source="4"),
            *[dict(row=row, col=2, source=f"=A1+{row}") for row in range(1, 11)],
            dict(row=1, col=3, source="=SUM(B1:B10)"),
            dict(row=65, col=1, source="=7+8"),
        ])
        apply(sheet="Sheet2", cells=[
            dict(row=1, col=1, source="=Sheet1!C1*2"),
            dict(row=2, col=1, source="=Sheet1!A65+1"),
        ])
        book.read("Sheet1", dict(r1=1, c1=1, r2=65, c2=3))
        book.read("Sheet2", dict(r1=1, c1=1, r2=2, c2=1))
        return book.sheet("Sheet1")
    finally:
        book.close()


def _run_child(point: str, path: Path) -> None:
    """Execute one crash point inside a child process and SIGKILL self at the named boundary."""
    assert hasattr(signal, "SIGKILL")
    sigkill = signal.SIGKILL

    if point == "before_commit":
        book = Workbook(path)
        real_verify = book.verify_file
        calls = 0

        def kill_before_commit():
            nonlocal calls
            real_verify()
            calls += 1
            # mutate calls verify_file at start, inside refresh, and right before COMMIT.
            # Trigger on the call when db is in an active write transaction with changes written.
            if book.db.in_transaction and book.changes:
                os.kill(os.getpid(), sigkill)

        book.verify_file = kill_before_commit
        book.mutate(dict(
            action="edit",
            operation_id=str(uuid.uuid4()),
            expected_state=book.state(),
            sheet="Sheet1",
            cells=[dict(row=1, col=1, source="99")],
        ))

    elif point == "during_result_write":
        book = Workbook(path)
        book.mutate(dict(
            action="edit",
            operation_id=str(uuid.uuid4()),
            expected_state=book.state(),
            sheet="Sheet1",
            cells=[dict(row=1, col=1, source="11")],
        ))
        real_write = CalculationStore.write

        def kill_during_write(store_self, items):
            real_write(store_self, items)
            os.kill(os.getpid(), sigkill)

        with patch.object(CalculationStore, "write", kill_during_write):
            book.read("Sheet1", dict(r1=1, c1=1, r2=10, c2=3))

    elif point == "between_slices":
        book = Workbook(path)
        with patch.object(engine, "DELETE_SLICE", 2):
            book.mutate(dict(
                action="edit",
                operation_id=str(uuid.uuid4()),
                expected_state=book.state(),
                sheet="Sheet1",
                cells=[dict(row=1, col=1, source="25")],
            ))
            # First slice committed (2 deleted), root and remaining dependents are on disk.
            # Kill during the second slice right after delete_results and before COMMIT.
            real_delete = CalculationStore.delete_results

            def kill_in_second_slice(store_self, keys):
                real_delete(store_self, keys)
                os.kill(os.getpid(), sigkill)

            with patch.object(CalculationStore, "delete_results", kill_in_second_slice):
                book.info()

    elif point == "during_open_compare":
        # Direct edit happened before this child started; kill mid-repair inside apply_verdict before COMMIT.
        real_apply = engine.apply_verdict

        def kill_in_apply(store, db, verdict):
            real_apply(store, db, verdict)
            os.kill(os.getpid(), sigkill)

        with patch.object(engine, "apply_verdict", kill_in_apply):
            Workbook(path)

    else:
        raise ValueError(f"Unknown crash point: {point}")

    raise RuntimeError(f"Child did not hit crash point {point}")


@unittest.skipUnless(hasattr(signal, "SIGKILL"), "SIGKILL is unavailable on this platform")
class CalculationCrashTests(unittest.TestCase):
    def _exercise_crash(self, point: str, *, pre_direct_edit: bool = False):
        clock_dt = datetime(2025, 6, 15, 12, 0, 0)
        with tempfile.TemporaryDirectory(prefix=f"dh-crash-{point}-") as temp:
            path = Path(temp) / f"{point}.dhwb"
            sheet1 = _seed_crash_workbook(path)
            if pre_direct_edit:
                with closing(sqlite3.connect(path)) as direct, direct:
                    direct.execute("UPDATE cells SET source='77' WHERE sheet_id=? AND row=1 AND col=1", (sheet1,))

            env = {
                **os.environ,
                "PYTHONPATH": f"{ROOT / 'service'}{os.pathsep}{ROOT / 'tests'}",
                "DH_CRASH_POINT": point,
                "DH_CRASH_PATH": str(path),
            }
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--child"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                proc.returncode,
                -signal.SIGKILL,
                f"Expected SIGKILL for {point}, got rc={proc.returncode}, stderr={proc.stderr}",
            )

            reopened = Workbook(path)
            try:
                assert_book_matches_oracle(self, reopened, path, clock_dt, label=f"crash={point}")
            finally:
                reopened.close()

    def test_kill_during_edit_before_commit(self):
        self._exercise_crash("before_commit")

    def test_kill_inside_result_write(self):
        self._exercise_crash("during_result_write")

    def test_kill_between_two_deletion_slices(self):
        self._exercise_crash("between_slices")

    def test_kill_during_opening_comparison(self):
        self._exercise_crash("during_open_compare", pre_direct_edit=True)


if __name__ == "__main__":
    if "--child" in sys.argv:
        _run_child(os.environ["DH_CRASH_POINT"], Path(os.environ["DH_CRASH_PATH"]))
    else:
        unittest.main()
