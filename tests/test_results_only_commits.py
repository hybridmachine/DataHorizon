# SPDX-License-Identifier: GPL-3.0-only
"""A session that only stores calculation results must not look like an external change to other sessions."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from datahorizon.engine import EngineError, Workbook
from datahorizon.formula import Calculator


class ResultsOnlyCommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "shared.dhwb"
        self.first = self.open(create=True)
        self.edit(self.first, [dict(row=1, col=1, source="2"), dict(row=1, col=2, source="=A1*3"),
                               dict(row=1, col=3, source="=B1+1")])
        self.second = self.open()

    def open(self, **kwargs):
        book = Workbook(self.path, **kwargs)
        self.addCleanup(book.close)
        return book

    def edit(self, book, cells, state=None):
        if state is None:
            book.info()
            state = book.state()
        return book.mutate(dict(action="edit", sheet="Sheet1", operation_id=str(uuid.uuid4()),
                                expected_state=state, cells=cells))

    def value(self, book, row, col):
        return book.read("Sheet1", dict(r1=row, r2=row, c1=col, c2=col))["cells"][0]["value"]

    def direct(self, statement, *parameters):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(statement, parameters)
            db.commit()

    def test_a_read_elsewhere_keeps_state_token_and_undo(self):
        self.edit(self.second, [dict(row=5, col=5, source="kept")])
        state, undo = self.second.state(), len(self.second.undo)
        self.assertEqual(self.value(self.first, 1, 3), 7)  # stores B1 and C1 in the file
        self.assertGreater(self.first.store.formula_count(), 0)
        self.second.info()
        self.assertEqual(self.second.state(), state)
        self.assertEqual(len(self.second.undo), undo)
        # The token held from before the other session's read still admits an edit.
        self.edit(self.second, [dict(row=6, col=5, source="after")], state=state)

    def test_results_stored_elsewhere_are_served_and_stay_correct(self):
        self.second.info()
        self.assertEqual(self.value(self.first, 1, 3), 7)
        with patch.object(Calculator, "_evaluate_cell", side_effect=AssertionError("evaluated")):
            self.assertEqual(self.value(self.second, 1, 3), 7)
        self.edit(self.second, [dict(row=1, col=1, source="10")])
        self.assertEqual(self.value(self.first, 1, 3), 31)
        self.assertEqual(self.value(self.second, 1, 3), 31)

    def test_repeated_reads_in_both_sessions_never_rotate(self):
        self.first.info(); self.second.info()
        states = self.first.state(), self.second.state()
        for row in range(2, 8):
            self.edit(self.first, [dict(row=row, col=2, source=f"=A1+{row}")], state=self.first.state())
            states = self.first.state(), states[1]
            self.second.info()
            states = states[0], self.second.state()  # the managed edit rotates the other session, as before
            self.assertEqual(self.value(self.second, row, 2), 2 + row)
            self.assertEqual(self.value(self.first, row, 2), 2 + row)
            self.first.info(); self.second.info()
            self.assertEqual((self.first.state(), self.second.state()), states)

    def test_a_direct_edit_still_rotates(self):
        self.second.info()
        state = self.second.state()
        sheet = self.second.sheet("Sheet1")
        self.direct("UPDATE cells SET source='5' WHERE sheet_id=? AND row=1 AND col=1", sheet)
        self.second.info()
        self.assertNotEqual(self.second.state(), state)
        self.assertEqual(self.value(self.second, 1, 3), 16)

    def test_a_direct_edit_beside_a_stored_result_still_rotates(self):
        self.second.info()
        state = self.second.state()
        sheet = self.second.sheet("Sheet1")
        self.assertEqual(self.value(self.first, 1, 3), 7)
        self.direct("UPDATE cells SET source='5' WHERE sheet_id=? AND row=1 AND col=1", sheet)
        self.assertEqual(self.value(self.first, 1, 3), 16)
        self.second.info()
        self.assertNotEqual(self.second.state(), state)
        self.assertEqual(self.value(self.second, 1, 3), 16)

    def test_a_direct_edit_that_forges_nothing_is_not_hidden_by_a_later_result(self):
        # A direct edit to a cell no stored formula reads, then a results-only commit: still an external change.
        self.second.info()
        state = self.second.state()
        sheet = self.second.sheet("Sheet1")
        self.direct("INSERT INTO cells VALUES(?,900,1,'x','auto','{}')", sheet)
        self.assertEqual(self.value(self.first, 1, 3), 7)
        self.second.info()
        self.assertNotEqual(self.second.state(), state)

    def test_a_managed_edit_elsewhere_still_rotates(self):
        self.second.info()
        state = self.second.state()
        self.edit(self.first, [dict(row=9, col=9, source="1")])
        self.second.info()
        self.assertNotEqual(self.second.state(), state)
        with self.assertRaises(EngineError) as raised:
            self.edit(self.second, [dict(row=9, col=9, source="2")], state=state)
        self.assertEqual(raised.exception.body["code"], "STALE_STATE")

    def test_a_read_only_session_keeps_its_state_token(self):
        reader = self.open(readonly=True)
        self.assertEqual(self.value(reader, 1, 3), 7)
        state = reader.state()
        self.assertEqual(self.value(self.first, 1, 3), 7)
        reader.info()
        self.assertEqual(reader.state(), state)
        self.assertEqual(self.value(reader, 1, 3), 7)

    def test_a_file_in_another_journal_format_is_treated_as_changed(self):
        self.second.info()
        with patch.object(Workbook, "_calculation_marks", return_value=None):
            self.second._marks_at = None
            self.second.info()
            state = self.second.state()
            self.assertEqual(self.value(self.first, 1, 3), 7)
            self.second.info()
        self.assertNotEqual(self.second.state(), state)


if __name__ == "__main__":
    unittest.main()
