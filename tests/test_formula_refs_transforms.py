# SPDX-License-Identifier: GPL-3.0-only
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from datahorizon.engine import Workbook

from datahorizon.formula import (MAX_ROWS, FormulaError, Unmaintainable, delete_sheet_references, move_references,
                                 rename_sheet_references, transform_structure, translate)
from datahorizon.formula_admission import status

# Text that looks like references inside a string, structured reference and array constant.
OPAQUE = '"A1 Sheet1!A1",Table1[[#This Row],[A1]],{"A1",1}'
MIXED = "=IFS(A1," + OPAQUE + ",Sheet1!C3)"


class StructureFallbackTests(unittest.TestCase):
    def test_row_insert_adjusts_references_in_unevaluable_formula(self):
        source = '=VLOOKUP(A5,Data!$B$2:$C$9,2,0)&" A5"'
        out, broken = transform_structure(source, "Sheet1", "Sheet1", "row", 3, 2)
        self.assertEqual((out, broken), ('=VLOOKUP(A7,Data!$B$2:$C$9,2,0)&" A5"', 0))

    def test_column_insert_adjusts_whole_column_range(self):
        out, _ = transform_structure("=VLOOKUP(A1,B:C,2,0)", "Sheet1", "Sheet1", "column", 2, 1)
        self.assertEqual(out, "=VLOOKUP(A1,C:D,2,0)")

    def test_untokenizable_formula_raises_unmaintainable(self):
        with self.assertRaises(Unmaintainable):
            transform_structure('=A1 ` B2', "Sheet1", "Sheet1", "row", 1, 1)

    def test_whole_column_range_contracts_and_breaks_on_column_delete(self):
        self.assertEqual(transform_structure("=SUM(B:D)", "Sheet1", "Sheet1", "col", 3, 1, True), ("=SUM(B:C)", 0))
        self.assertEqual(transform_structure("=SUM($B:$D)", "Sheet1", "Sheet1", "col", 1, 1, True), ("=SUM($A:$C)", 0))
        self.assertEqual(transform_structure("=SUM(B:C)+1", "Sheet1", "Sheet1", "col", 2, 2, True), ("=SUM(#REF!)+1", 1))
        self.assertEqual(transform_structure("=SUM(Sheet1!B:B)", "Sheet1", "Sheet1", "col", 2, 1, True), ("=SUM(#REF!)", 1))

    def test_full_span_whole_references_clamp_at_the_grid_edge_on_insert(self):
        self.assertEqual(transform_structure("=SUM(A:XFD)", "Sheet1", "Sheet1", "col", 1, 1), ("=SUM(A:XFD)", 0))
        self.assertEqual(transform_structure("=SUM(1:1048576)", "Sheet1", "Sheet1", "row", 1, 3), ("=SUM(1:1048576)", 0))
        self.assertEqual(transform_structure("=SUM(B:XFC)", "Sheet1", "Sheet1", "col", 2, 5), ("=SUM(G:XFD)", 0))
        self.assertEqual(transform_structure("=SUM(XFD:XFD)", "Sheet1", "Sheet1", "col", 1, 1), ("=SUM(#REF!)", 1))
        self.assertEqual(transform_structure("=SUM(A1:XFD1)+A1", "Sheet1", "Sheet1", "col", 1, 1), ("=SUM(#REF!)+B1", 1))

    def test_whole_column_and_row_references_follow_only_their_axis(self):
        self.assertEqual(transform_structure("=SUM(B:C)", "Sheet1", "Sheet1", "row", 1, 5), ("=SUM(B:C)", 0))
        self.assertEqual(transform_structure("=SUM(B:C)", "Sheet1", "Sheet1", "row", 1, 5, True), ("=SUM(B:C)", 0))
        self.assertEqual(transform_structure("=SUM(2:3)", "Sheet1", "Sheet1", "row", 1, 2), ("=SUM(4:5)", 0))
        self.assertEqual(transform_structure("=SUM(2:3)", "Sheet1", "Sheet1", "col", 1, 2), ("=SUM(2:3)", 0))
        self.assertEqual(transform_structure("=SUM(2:4)", "Sheet1", "Sheet1", "row", 3, 1, True), ("=SUM(2:3)", 0))

    def test_second_endpoint_inherits_the_first_endpoints_sheet(self):
        source = "=IFS(Other!A1:B2,1)"
        self.assertEqual(transform_structure(source, "Sheet1", "Other", "row", 2, 1), ("=IFS(Other!A1:B3,1)", 0))
        self.assertEqual(transform_structure(source, "Sheet1", "Sheet1", "row", 2, 1), (source, 0))
        self.assertEqual(transform_structure(source, "Sheet1", "other", "row", 1, 2, True), ("=IFS(#REF!,1)", 1))
        self.assertEqual(transform_structure("=IFS(A1:B2,1)", "Other", "Other", "row", 2, 1), ("=IFS(A1:B3,1)", 0))

    def test_target_identity_matches_like_the_strict_path(self):
        out, _ = transform_structure("=IFS('sheet-id'!A1,1)", "Sheet1", "Other", "row", 1, 1, target_id="sheet-id")
        self.assertEqual(out, "=IFS('sheet-id'!A2,1)")

    def test_range_spanning_different_sheets_is_refused(self):
        with self.assertRaises(FormulaError) as caught:
            transform_structure("=IFS(A1:Other!B2,1)", "Sheet1", "Sheet1", "row", 1, 1)
        self.assertNotIsInstance(caught.exception, Unmaintainable)

    def test_external_references_are_never_adjusted(self):
        source = "=IFS([1]Sheet1!A1,A1,'[Book.xlsx]Sheet1'!B:B)"
        self.assertEqual(transform_structure(source, "Sheet1", "Sheet1", "row", 1, 1),
                         ("=IFS([1]Sheet1!A1,A2,'[Book.xlsx]Sheet1'!B:B)", 0))
        self.assertEqual(transform_structure(source, "Sheet1", "Sheet1", "col", 1, 1),
                         ("=IFS([1]Sheet1!A1,B1,'[Book.xlsx]Sheet1'!B:B)", 0))

    def test_three_dimensional_references_by_display_position(self):
        source = "=SUM(Jan:Mar!B2)"
        order = ["Jan", "Feb", "Mar", "Apr"]
        self.assertEqual(transform_structure(source, "Sheet1", "Feb", "row", 1, 1, sheet_order=order)[0], "=SUM(Jan:Mar!B3)")
        self.assertEqual(transform_structure(source, "Sheet1", "Apr", "row", 1, 1, sheet_order=order)[0], source)
        self.assertEqual(transform_structure("=SUM(Mar:Jan!B2)", "Sheet1", "feb", "row", 1, 1, sheet_order=order)[0],
                         "=SUM(Mar:Jan!B3)")
        # Without the order only the named endpoints are known to lie in the span.
        self.assertEqual(transform_structure(source, "Sheet1", "Feb", "row", 1, 1)[0], source)
        self.assertEqual(transform_structure(source, "Sheet1", "Jan", "row", 1, 1)[0], "=SUM(Jan:Mar!B3)")
        self.assertEqual(transform_structure(source, "Sheet1", "Mar", "row", 1, 1)[0], "=SUM(Jan:Mar!B3)")
        self.assertEqual(transform_structure("='Jan 1:Mar 3'!A1:B2", "Sheet1", "Mar 3", "col", 1, 1)[0], "='Jan 1:Mar 3'!B1:C2")

    def test_opaque_text_is_untouched(self):
        out, broken = transform_structure(MIXED, "Sheet1", "Sheet1", "row", 1, 1)
        self.assertEqual((out, broken), ("=IFS(A2," + OPAQUE + ",Sheet1!C4)", 0))

    def test_strict_path_still_handles_parseable_formulas(self):
        cases = [("=SUM(A1:A3)+Sheet2!B2", ("row", 2, 1, False), ("=SUM(A1:A4)+Sheet2!B2", 0)),
                 ("=a1+A3", ("row", 2, 1, False), ("=a1+A4", 0)),
                 ("=SUM(A2:A3)", ("row", 2, 2, True), ("=SUM(#REF!)", 1))]
        with mock.patch("datahorizon.formula_refs.scan", side_effect=AssertionError("fallback used")):
            for source, (axis, start, count, delete), expected in cases:
                with self.subTest(source):
                    self.assertEqual(transform_structure(source, "Sheet1", "Sheet1", axis, start, count, delete), expected)


class TranslateFallbackTests(unittest.TestCase):
    def test_shared_formula_member_translates_through_unevaluable_master(self):
        self.assertEqual(translate("=IFS(A1>1,B:B)", 1, 0), "=IFS(A2>1,B:B)")

    def test_whole_column_and_row_follow_their_own_offset_only(self):
        self.assertEqual(translate("=SUM(B:B,$C:$C)", 5, 1), "=SUM(C:C,$C:$C)")
        self.assertEqual(translate("=SUM(2:2,$3:$3)", 1, 5), "=SUM(3:3,$3:$3)")
        self.assertEqual(translate("=SUM(B:C)", 3, 0), "=SUM(B:C)")
        self.assertEqual(translate("=SUM(A:B)", 0, -1), "=SUM(#REF!)")
        self.assertEqual(translate("=SUM($A:B)", 0, -1), "=SUM($A:A)")
        self.assertEqual(translate("=SUM(Data!1:2)", -1, 0), "=SUM(#REF!)")
        # The strict parser reads an unqualified 2:3 as two numbers; it is a whole-row range.
        self.assertEqual(translate("=SUM(2:3)", 1, 4), "=SUM(3:4)")

    def test_cells_absolute_markers_external_and_opaque_text(self):
        self.assertEqual(translate("=IFS($A1,A$1,Data!B2:C3)", 1, 1), "=IFS($A2,B$1,Data!C3:D4)")
        # Copy/fill shifts relative parts of external references too, keeping the prefix verbatim.
        self.assertEqual(translate("=IFS([1]Sheet1!A1,A1)", 1, 0), "=IFS([1]Sheet1!A2,A2)")
        self.assertEqual(translate("=IFS([1]Sheet1!$A$1,'[Book.xlsx]S 1'!B:B)", 1, 1), "=IFS([1]Sheet1!$A$1,'[Book.xlsx]S 1'!C:C)")
        self.assertEqual(translate("=IFS([1]Sheet1!A1:B2)", -1, 0), "=IFS(#REF!)")
        self.assertEqual(translate(MIXED, 1, 1), "=IFS(B2," + OPAQUE + ",Sheet1!D4)")
        self.assertEqual(translate("=SUM(Jan:Mar!B2)", 1, 0), "=SUM(Jan:Mar!B3)")

    def test_untokenizable_formula_raises_unmaintainable(self):
        with self.assertRaises(Unmaintainable) as caught:
            translate("=A1 ` B2", 1, 0)
        self.assertIsInstance(caught.exception, FormulaError)

    def test_strict_path_still_handles_parseable_formulas(self):
        with mock.patch("datahorizon.formula_refs.scan", side_effect=AssertionError("fallback used")):
            self.assertEqual(translate("=a1+$B$2", 1, 1), "=B2+$B$2")
            self.assertEqual(translate("=a1", 0, 0), "=A1")  # the strict path normalizes spelling
        self.assertEqual(translate("=IFS(a1,B:B)", 0, 0), "=IFS(a1,B:B)")  # the fallback leaves unchanged text alone


class MoveFallbackTests(unittest.TestCase):
    def test_move_retargets_references_in_unevaluable_formula(self):
        out = move_references("=IFS(A1>0,B2,Sheet1!B2:B2)", "Sheet1", "Sheet1", (2, 2, 2, 2), 1, 1)
        self.assertEqual(out, "=IFS(A1>0,C3,Sheet1!C3:C3)")

    def test_whole_columns_follow_only_a_full_height_cut(self):
        self.assertEqual(move_references("=SUM(B:B)", "Sheet1", "Sheet1", (1, 2, MAX_ROWS, 2), 0, 2), "=SUM(D:D)")
        with self.assertRaises(FormulaError):
            move_references("=SUM(B:B)", "Sheet1", "Sheet1", (1, 2, 10, 2), 0, 2)
        with self.assertRaises(FormulaError):  # an unpaired range end: the range's extent is unknown
            move_references("=SUM(A1:Sheet1!B:B)", "Sheet1", "Sheet1", (1, 1, 1, 1), 1, 0)
        self.assertEqual(move_references("=SUM(B:B)", "Sheet1", "Sheet1", (1, 3, 10, 3), 0, 2), "=SUM(B:B)")

    def test_three_dimensional_reference_into_the_cut_is_refused(self):
        with self.assertRaises(FormulaError):
            move_references("=SUM(Jan:Mar!B2)", "Sheet1", "Feb", (2, 2, 2, 2), 1, 0, sheet_order=["Jan", "Feb", "Mar"])
        self.assertEqual(move_references("=SUM(Jan:Mar!B2)", "Sheet1", "Apr", (2, 2, 2, 2), 1, 0,
                                         sheet_order=["Jan", "Feb", "Mar", "Apr"]), "=SUM(Jan:Mar!B2)")

    def test_external_and_opaque_text_untouched(self):
        self.assertEqual(move_references("=IFS([1]Sheet1!A1,A1)", "Sheet1", "Sheet1", (1, 1, 1, 1), 1, 0), "=IFS([1]Sheet1!A1,A2)")
        self.assertEqual(move_references(MIXED, "Sheet1", "Sheet1", (1, 1, 3, 3), 1, 0), "=IFS(A2," + OPAQUE + ",Sheet1!C4)")

    def test_postfix_percent_no_longer_blocks_a_move(self):
        self.assertEqual(move_references("=A1%+1", "Sheet1", "Sheet1", (1, 1, 2, 2), 3, 0), "=A4%+1")

    def test_untokenizable_formula_raises_unmaintainable(self):
        with self.assertRaises(Unmaintainable):
            move_references("=A1 ` B2", "Sheet1", "Sheet1", (1, 1, 1, 1), 1, 0)


class RenameFallbackTests(unittest.TestCase):
    def test_quoted_sheet_renamed_but_not_inside_a_string(self):
        source = "=IFS('Old Name'!A1>0,\"'Old Name'!A1\")"
        self.assertEqual(rename_sheet_references(source, "Old Name", "New's"), "=IFS('New''s'!A1>0,\"'Old Name'!A1\")")

    def test_plain_and_three_dimensional_endpoints(self):
        self.assertEqual(rename_sheet_references("=SUM(Jan:Mar!B2,mar!A1:Mar!B2,A1)", "Mar", "Q 1"),
                         "=SUM('Jan:Q 1'!B2,'Q 1'!A1:'Q 1'!B2,A1)")
        self.assertEqual(rename_sheet_references("=SUM(Jan:Mar!B2)", "Feb", "X"), "=SUM(Jan:Mar!B2)")
        self.assertEqual(rename_sheet_references("=IFS('old-id'!A1)", "Old", "New", target_id="old-id", new_id="new-id"),
                         "=IFS('new-id'!A1)")

    def test_external_and_opaque_text_untouched(self):
        self.assertEqual(rename_sheet_references("=IFS([1]Sheet1!A1,Sheet1!A1)", "Sheet1", "S2"), "=IFS([1]Sheet1!A1,'S2'!A1)")
        self.assertEqual(rename_sheet_references(MIXED, "Sheet1", "S2"), "=IFS(A1," + OPAQUE + ",'S2'!C3)")

    def test_untokenizable_formula_raises_unmaintainable(self):
        with self.assertRaises(Unmaintainable):
            rename_sheet_references("=Sheet1!A1 ` B2", "Sheet1", "S2")


class DeleteFallbackTests(unittest.TestCase):
    def test_deleted_sheet_references_become_errors(self):
        self.assertEqual(delete_sheet_references("=IFS(Data!A1>0,Sheet1!B:B,Data!A1:B2)", "Sheet1", "Data"),
                         ("=IFS(#REF!>0,Sheet1!B:B,#REF!)", 2))

    def test_three_dimensional_endpoint_breaks_interior_survives(self):
        self.assertEqual(delete_sheet_references("=SUM(Jan:Mar!B2)", "Sheet1", "Mar"), ("=SUM(#REF!)", 1))
        self.assertEqual(delete_sheet_references("=SUM(Jan:Mar!B2)", "Sheet1", "Feb"), ("=SUM(Jan:Mar!B2)", 0))

    def test_external_and_opaque_text_untouched(self):
        self.assertEqual(delete_sheet_references("=IFS([1]Sheet1!A1,Sheet1!A1)", "Other", "Sheet1"), ("=IFS([1]Sheet1!A1,#REF!)", 1))
        self.assertEqual(delete_sheet_references(MIXED, "Other", "Sheet1"), ("=IFS(A1," + OPAQUE + ",#REF!)", 1))

    def test_untokenizable_formula_raises_unmaintainable(self):
        with self.assertRaises(Unmaintainable):
            delete_sheet_references("=Data!A1 ` B2", "Sheet1", "Data")


class AdmissionTests(unittest.TestCase):
    def test_references_the_calculator_cannot_read(self):
        cases = [("=SUM(A:A)", ("formula.unsupported_reference", "A:A")),
                 ("=SUM(Sheet1!$1:$2)", ("formula.unsupported_reference", "Sheet1!$1:$2")),
                 ("=SUM(Jan:Mar!B2)", ("formula.unsupported_reference", "Jan:Mar!B2")),
                 ("=SUM('Jan 1:Mar'!B2)", ("formula.unsupported_reference", "'Jan 1:Mar'!B2")),
                 ("=SUM(2:3)", ("formula.unsupported_reference", "2:3")),
                 ("='[Book.xlsx]Sheet1'!A1", ("formula.external_reference", "")),
                 ("=VLOOKUP(A1,B:C,2,0)", ("formula.unsupported_function", "VLOOKUP")),
                 ("=SUM([1]Sheet1!A1)", ("formula.external_reference", "")),
                 ('=SUM("[1]x")+Table1[Col]', ("formula.syntax", ""))]
        for source, expected in cases:
            with self.subTest(source):
                self.assertEqual(status(source, ["Sheet1"]), expected)


class EngineSheetOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.book = Workbook(Path(self.temp.name) / "test.dhwb", create=True)

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def apply(self, action, **args):
        return self.book.mutate({"action": action, "sheet": "Sheet1", "operation_id": str(uuid.uuid4()),
                                 "expected_state": self.book.info()["state"], **args})

    def test_row_insert_on_an_inner_sheet_adjusts_a_3d_reference(self):
        for name in ("Jan", "Feb", "Mar", "Apr"):
            self.apply("sheet_add", name=name)
        self.apply("edit", cells=[{"row": 1, "col": 1, "source": "=SUM(Jan:Mar!B2)"},
                                  {"row": 2, "col": 1, "source": "=SUM(B:C)"}])
        self.apply("rows_insert", sheet="Apr", start=1)
        self.apply("rows_insert", sheet="Feb", start=1)
        self.apply("columns_insert", start=1)
        sources = [c["source"] for c in self.book.read("Sheet1", {"r2": 2, "c2": 2})["cells"]]
        self.assertEqual(sources, ["=SUM(Jan:Mar!B3)", "=SUM(C:D)"])


if __name__ == "__main__":
    unittest.main()
