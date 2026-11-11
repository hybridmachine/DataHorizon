# SPDX-License-Identifier: GPL-3.0-only
import base64
import io
import struct
import tempfile
import tracemalloc
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from datahorizon import xlsx_stream
from datahorizon.engine import EngineError, require
from datahorizon.interchange import STRICT, TRANSITIONAL, workbook_report, xlsx_preview
from datahorizon.xlsx_stream import ByteBudget, open_package, read_part, read_workbook, scope_lines
from xlsx_builder import workbook_bytes

FIXTURE = Path(__file__).parent/"fixtures"/"fidelity.xlsx"
CHUNK = 65536
SHEET_OPEN = f'<worksheet xmlns="{TRANSITIONAL.main}"><sheetData>'
SHEET_CLOSE = "</sheetData></worksheet>"
STYLES = ('<styleSheet xmlns="{ns}"><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/></numFmts>'
          '<fonts count="2"><font/><font><b/></font></fonts>'
          '<fills count="2"><fill/><fill><patternFill patternType="solid"><fgColor theme="5"/></patternFill></fill></fills>'
          '<borders count="1"><border/></borders>'
          '<cellXfs count="4"><xf fontId="0"/><xf fontId="1"/><xf numFmtId="164"/><xf fillId="1" xfId="1"/></cellXfs></styleSheet>')
DATA = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s" s="1"><v>1</v></c><c r="C1"><v>3.5</v></c>'
        '<c r="D1" t="b"><v>1</v></c><c r="E1" t="inlineStr"><is><r><t>rich</t></r><r><t> text</t></r></is></c></row>'
        '<row r="2"><c r="A2" s="2"><v>45200</v></c><c r="B2" t="e"><v>#DIV/0!</v></c><c r="C2" t="str"><v>x_x0041_</v></c>'
        '<c r="D2" s="3"><v>7</v></c><c r="E2" t="d"><v>2026-11-11</v></c></row>'
        '<row r="3"><c r="A3"><f t="shared" ref="A3:A5" si="0">C1*2</f><v>7</v></c><c r="B3"><f>WEBSERVICE("x")</f><v>9</v></c>'
        '<c r="C3"><f t="array" ref="C3">SUM(C1:C2)</f><v>3</v></c><c r="D3" t="b"><v>2</v></c></row>'
        '<row r="4"><c r="A4"><f t="shared" si="0"/><v>8</v></c></row>'
        '<row r="5"><c r="A5"><f t="shared" si="0"/></c><c r="B5"><f>Data!A1&amp;"!"</f><v>q</v></c></row>')
CALC = '<row r="1"><c r="A1"><f>SUM(Data!C1:C2)</f><v>3.5</v></c><c r="B1" t="s"><v>2</v></c></row>'


def b64(data): return base64.b64encode(data).decode()


def rich_package(strict=False):
    ns = STRICT if strict else TRANSITIONAL
    return workbook_bytes({"Data": DATA, "Calc": CALC, "Blank": ""}, strict=strict,
                          styles_xml=STYLES.format(ns=ns.main), shared_strings=["alpha", "beta", "gamma"])


def single_part(name, payload, *, method=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", method) as archive: archive.writestr(name, payload)
    return output.getvalue()


def replace_part(data, name, payload):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.namelist(): target.writestr(item, payload if item == name else source.read(item))
    return output.getvalue()


def lying_zip(real_bytes, declared):
    """One deflated sheet part whose local and central headers claim `declared` uncompressed bytes."""
    rows = "".join(f'<row r="{i}"/>' for i in range(1, real_bytes//14+2))
    payload = (SHEET_OPEN+rows+SHEET_CLOSE).encode()[:real_bytes]
    data = bytearray(single_part("xl/worksheets/sheet1.xml", payload))
    assert data[:4] == b"PK\x03\x04"
    struct.pack_into("<I", data, 22, declared)
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, central+24, declared)
    return bytes(data)


class ListStrings:
    def __init__(self): self.items = []

    def put(self, index, text):
        assert index == len(self.items)
        self.items.append(text)

    def get(self, index): return self.items[index]


class ListSink:
    """Keeps every cell; like the on-disk stage, it rejects a duplicate coordinate in a sheet."""
    def __init__(self):
        self.sheets = []; self.batches = []; self.strings = ListStrings(); self._seen = set()

    def sheet(self, name):
        self.sheets.append((name, [])); self._seen = set()

    def cells(self, batch):
        assert 0 < len(batch) <= 1000, len(batch)
        for cell in batch:
            require((cell["row"], cell["col"]) not in self._seen, "INVALID_SPREADSHEET", "Duplicate cell coordinates")
            self._seen.add((cell["row"], cell["col"]))
        self.sheets[-1][1].extend(batch); self.batches.append(len(batch))


class CountingSink:
    """Discards cells so memory measurements see only the reader."""
    def __init__(self):
        self.count = 0; self.strings = ListStrings()

    def sheet(self, name): pass

    def cells(self, batch): self.count += len(batch)


class StreamingReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.files = 0

    def write(self, data):
        self.files += 1
        path = Path(self.directory.name)/f"book{self.files}.xlsx"
        path.write_bytes(data)
        return path

    def stream(self, data, mode="mixed", sink=None, checkpoint=lambda *_: None):
        sink = sink or ListSink()
        report = read_workbook(self.write(data), {"mode": mode}, sink, checkpoint)
        return sink, report

    def rejects(self, code, call, *args):
        with self.assertRaises(EngineError) as raised: call(*args)
        self.assertEqual(raised.exception.body["code"], code, raised.exception.body)
        return raised.exception

    def assert_agree(self, data, mode="mixed"):
        tree = xlsx_preview({"file_base64": b64(data), "scope": "workbook", "mode": mode})
        sink, report = self.stream(data, mode)
        self.assertEqual(sink.sheets, [(s["name"], s["cells"]) for s in tree["workbook_sheets"]])
        self.assertEqual(report.entries(), tree["report_entries"])
        self.assertEqual(report.totals(), tree["totals"])
        self.assertEqual(scope_lines({"mode": mode})+report.lines(), tree["report"])
        return sink, report

    def assert_both_reject(self, data, code, mode="mixed"):
        tree = self.rejects(code, xlsx_preview, {"file_base64": b64(data), "scope": "workbook", "mode": mode})
        stream = self.rejects(code, self.stream, data, mode)
        return tree, stream

    # Byte budget and declared sizes.
    def test_declared_size_is_not_trusted(self):
        path = self.write(lying_zip(real_bytes=2*1024*1024, declared=1024))
        budget = ByteBudget(1024*1024)
        with self.assertRaises(EngineError) as raised:
            list(read_part(open_package(path), "xl/worksheets/sheet1.xml", budget))
        # zipfile stops at the declared size and then fails the CRC, so the lie surfaces as a malformed package.
        self.assertIn(raised.exception.body["code"], ("LIMIT", "INVALID_SPREADSHEET"))
        self.assertLessEqual(budget.used, budget.limit+CHUNK)

    def test_actual_bytes_over_budget_reject_with_limit_before_the_declared_size(self):
        path = self.write(lying_zip(real_bytes=2*1024*1024, declared=3*1024*1024//2))
        budget = ByteBudget(1024*1024)
        with open_package(path) as archive:
            self.rejects("LIMIT", lambda: list(read_part(archive, "xl/worksheets/sheet1.xml", budget)))
        self.assertGreater(budget.used, budget.limit)
        self.assertLessEqual(budget.used, budget.limit+CHUNK)

    def test_budget_is_shared_across_parts(self):
        data = workbook_bytes({"A": "", "B": ""})
        with open_package(self.write(data)) as archive:
            size = archive.getinfo("xl/worksheets/sheet1.xml").file_size
            budget = ByteBudget(size+archive.getinfo("xl/worksheets/sheet2.xml").file_size-1)
            list(read_part(archive, "xl/worksheets/sheet1.xml", budget))
            self.assertEqual(budget.used, size)
            self.rejects("LIMIT", lambda: list(read_part(archive, "xl/worksheets/sheet2.xml", budget)))

    def test_workbook_budget_rejects_with_limit(self):
        with mock.patch.object(xlsx_stream, "LARGE_EXPANDED", 1500):
            self.rejects("LIMIT", self.stream, rich_package())

    # Package checks.
    def test_package_checks(self):
        good = workbook_bytes({"S": ""})
        with mock.patch.object(xlsx_stream, "LARGE_PACKAGE", len(good)-1):
            self.rejects("LIMIT", open_package, self.write(good))
        with mock.patch.object(xlsx_stream, "LARGE_PARTS", 2):
            self.rejects("LIMIT", open_package, self.write(good))
        self.rejects("INVALID_SPREADSHEET", open_package, self.write(b"not a zip"))
        output = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("xl/workbook.xml", "<a/>"); archive.writestr("xl/workbook.xml", "<b/>")
        self.rejects("INVALID_SPREADSHEET", open_package, self.write(output.getvalue()))
        self.rejects("INVALID_SPREADSHEET", open_package, self.write(single_part("x.xml", "<a/>", method=zipfile.ZIP_BZIP2)))
        encrypted = bytearray(single_part("x.xml", "<a/>"))
        central = encrypted.index(b"PK\x01\x02"); encrypted[central+8] |= 1
        self.rejects("INVALID_SPREADSHEET", open_package, self.write(bytes(encrypted)))
        declared = lying_zip(real_bytes=4096, declared=xlsx_stream.LARGE_EXPANDED+1)
        self.rejects("LIMIT", open_package, self.write(declared))

    # XML safety in read_part.
    def read_sheet_bytes(self, payload):
        with open_package(self.write(single_part("p.xml", payload))) as archive:
            return list(read_part(archive, "p.xml", ByteBudget(16*1024*1024)))

    def straddling(self, marker, split):
        """A part whose `marker` starts `split` bytes before the first 64-KiB chunk boundary."""
        prefix = b'<?xml version="1.0" encoding="UTF-8"?>'
        padding = CHUNK-split-len(prefix)-len(b"<!---->")
        head = prefix+b"<!--"+b"x"*padding+b"-->"
        self.assertEqual(len(head), CHUNK-split)
        return head+marker+b"<a/>"

    def test_dtd_and_entity_are_rejected_across_chunk_boundaries(self):
        for marker in (b'<!DOCTYPE a>', b'<!-- <!ENTITY x "y"> -->', b'<!doctype a>'):
            for split in (1, 4, 8):
                self.rejects("INVALID_SPREADSHEET", self.read_sheet_bytes, self.straddling(marker, split))
        self.read_sheet_bytes(self.straddling(b"<!-- ok -->", 4))

    def test_nul_bytes_are_rejected_in_any_chunk(self):
        self.rejects("INVALID_SPREADSHEET", self.read_sheet_bytes, b"<a>"+b" "*(CHUNK-3)+b"\0</a>")
        self.rejects("INVALID_SPREADSHEET", self.read_sheet_bytes, b"<a>\0</a>")

    def test_only_utf8_encoding_declarations_are_accepted(self):
        for declaration in (b'<?xml version="1.0" encoding="ISO-8859-1"?>', b"<?xml version='1.0' encoding='windows-1252'?>",
                            b'<?xml version="1.0" encoding="UTF-16"?>'):
            self.rejects("INVALID_SPREADSHEET", self.read_sheet_bytes, declaration+b"<a/>")
        for declaration in (b'<?xml version="1.0" encoding="UTF-8"?>', b"\xef\xbb\xbf<?xml version='1.0' encoding='utf-8'?>",
                            b'<?xml version="1.0"?>', b""):
            self.assertEqual([e for e, _ in self.read_sheet_bytes(declaration+b"<a/>")], ["start", "end"])
        self.rejects("INVALID_SPREADSHEET", self.read_sheet_bytes, "<a>é</a>".encode("latin-1"))

    def test_depth_over_64_is_rejected(self):
        self.assertEqual(len(self.read_sheet_bytes(b"<a>"*64+b"</a>"*64)), 128)
        self.rejects("LIMIT", self.read_sheet_bytes, b"<a>"*65+b"</a>"*65)

    def test_malformed_xml_and_missing_parts_are_invalid(self):
        self.rejects("INVALID_SPREADSHEET", self.read_sheet_bytes, b"<a><b></a>")
        with open_package(self.write(single_part("p.xml", "<a/>"))) as archive:
            self.rejects("INVALID_SPREADSHEET", lambda: list(read_part(archive, "missing.xml", ByteBudget(1024))))

    def test_finished_records_are_released(self):
        payload = SHEET_OPEN+"".join(f'<row r="{i}"><c r="A{i}"><v>{i}</v></c></row>' for i in range(1, 20001))+SHEET_CLOSE
        held = 0
        with open_package(self.write(single_part("p.xml", payload))) as archive:
            for event, item in read_part(archive, "p.xml", ByteBudget(1 << 24), record_depth=4):
                if event == "start" and item.tag.endswith("sheetData"): data = item
                if event == "end" and item.tag.endswith("}row"): held = max(held, len(data))
            self.assertEqual(len(data), 0)
        # Only rows the parser built ahead within one chunk remain attached.
        self.assertLess(held, 3000)

    # Agreement with the tree reader.
    def test_streaming_and_tree_readers_agree(self):
        data = FIXTURE.read_bytes()
        for mode in ("mixed", "formulas", "values"):
            with self.subTest(mode=mode):
                tree = xlsx_preview({"file_base64": b64(data), "scope": "workbook", "mode": mode})
                sink = ListSink(); report = read_workbook(self.write(data), {"mode": mode}, sink, lambda *_: None)
                self.assertEqual(sink.sheets, [(s["name"], s["cells"]) for s in tree["workbook_sheets"]])
                self.assertEqual(report.entries(), tree["report_entries"])

    def test_generated_workbook_agrees_in_every_mode(self):
        for mode in ("mixed", "formulas", "values"):
            with self.subTest(mode=mode):
                sink, report = self.assert_agree(rich_package(), mode)
                self.assertEqual([name for name, _ in sink.sheets], ["Data", "Calc", "Blank"])
                self.assertTrue(report.entries())

    def test_strict_package_agrees_with_its_transitional_twin(self):
        for mode in ("mixed", "values"):
            strict, strict_report = self.stream(rich_package(True), mode)
            transitional, transitional_report = self.stream(rich_package(False), mode)
            self.assertEqual(strict.sheets, transitional.sheets)
            self.assertEqual(strict_report.entries(), transitional_report.entries())
            self.assert_agree(rich_package(True), mode)

    def test_scope_lines_are_the_workbook_scope_lines(self):
        self.assertEqual(scope_lines({"mode": "values"}), workbook_report())

    def test_malformed_packages_are_rejected_by_both_readers(self):
        cases = [
            ("INVALID_SPREADSHEET", workbook_bytes({"S": '<row r="1"><c r="A1"><v>1</v></c><c r="A1"><v>2</v></c></row>'})),
            ("INVALID_SPREADSHEET", workbook_bytes({"S": '<row r="1"><c r="A1" t="s"><v>3</v></c></row>'}, shared_strings=["a"])),
            ("INVALID_SPREADSHEET", workbook_bytes({"S": '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'})),
            ("INVALID_SPREADSHEET", workbook_bytes({"S": '<row><c><v>1</v></c></row>'})),
            ("INVALID_SPREADSHEET", workbook_bytes({"S": '<row r="1"><c r="A1"><f t="shared" si="4"/></c></row>'})),
            ("INVALID_SPREADSHEET", workbook_bytes({"S": '<row r="1"><c r="A1"><f t="bogus">1</f></c></row>'})),
            ("INVALID_SPREADSHEET", workbook_bytes({"S": '<row r="1"><c r="A1" s="9"><v>1</v></c></row>'}, styles_xml=STYLES.format(ns=TRANSITIONAL.main))),
            ("INVALID_SPREADSHEET", replace_part(workbook_bytes({"S": ""}), "xl/worksheets/sheet1.xml", "<!DOCTYPE x><worksheet/>")),
            ("LIMIT", workbook_bytes({f"S{i}": "" for i in range(65)})),
        ]
        for code, data in cases[:-1]:
            with self.subTest(code=code, data=data[-200:]):
                self.assert_both_reject(data, code)
        self.rejects("LIMIT", xlsx_preview, {"file_base64": b64(cases[-1][1]), "scope": "workbook"})
        self.assertEqual(len(self.stream(cases[-1][1])[0].sheets), 65)

    def test_request_is_validated(self):
        data = workbook_bytes({"S": ""})
        self.rejects("INVALID_ARGUMENT", read_workbook, self.write(data), {"mode": "cached"}, ListSink(), lambda *_: None)
        self.rejects("INVALID_ARGUMENT", read_workbook, self.write(data), {"timezone_policy": "local"}, ListSink(), lambda *_: None)
        self.rejects("INVALID_ARGUMENT", read_workbook, self.write(data), {"row": 2}, ListSink(), lambda *_: None)

    # Shared formulas.
    def test_shared_member_before_master_resolves(self):
        early = ('<row r="1"><c r="B1"><f t="shared" si="0"/><v>1</v></c><c r="C1"><v>5</v></c></row>'
                 '<row r="2"><c r="B2"><f t="shared" si="0"/></c></row>'
                 '<row r="3"><c r="B3"><f t="shared" ref="B1:B4" si="0">C3+1</f><v>0</v></c></row>'
                 '<row r="4"><c r="B4"><f t="shared" si="0"/></c></row>')
        sink, report = self.stream(workbook_bytes({"S": early}))
        cells = {(c["row"], c["col"]): c["source"] for c in sink.sheets[0][1]}
        self.assertEqual(cells, {(1, 2): "=C1+1", (1, 3): "5", (2, 2): "=C2+1", (3, 2): "=C3+1", (4, 2): "=C4+1"})
        # Deferred members arrive after the rest of their sheet, in their original order.
        self.assertEqual([(c["row"], c["col"]) for c in sink.sheets[0][1]], [(1, 3), (3, 2), (4, 2), (1, 2), (2, 2)])
        self.assertEqual(report.entries(), [])
        values, _ = self.assert_agree(workbook_bytes({"S": early}), "values")
        self.assertEqual([c["source"] for c in values.sheets[0][1]], ["1", "5", "0"])

    def test_shared_group_without_master_in_the_sheet_rejects(self):
        sheets = {"A": '<row r="1"><c r="A1"><f t="shared" ref="A1:A2" si="0">1</f></c></row>',
                  "B": '<row r="2"><c r="A2"><f t="shared" si="0"/></c></row>'}
        self.assert_both_reject(workbook_bytes(sheets), "INVALID_SPREADSHEET")

    def test_deferred_members_are_bounded(self):
        members = "".join(f'<row r="{r}"><c r="A{r}"><f t="shared" si="0"/></c></row>' for r in range(1, 4))
        sheet = members+'<row r="9"><c r="A9"><f t="shared" ref="A1:A9" si="0">1</f></c></row>'
        with mock.patch.object(xlsx_stream, "DEFERRED_MEMBERS", 3):
            self.assertEqual(len(self.stream(workbook_bytes({"S": sheet}))[0].sheets[0][1]), 4)
        with mock.patch.object(xlsx_stream, "DEFERRED_MEMBERS", 2):
            self.rejects("LIMIT", self.stream, workbook_bytes({"S": sheet}))

    # Limits, batching, checkpoints and cancellation.
    def test_cell_count_over_limit_rejects_with_limit(self):
        sheet = "".join(f'<row r="{r}"><c r="A{r}"><v>{r}</v></c></row>' for r in range(1, 7))
        data = workbook_bytes({"A": sheet, "B": sheet})
        with mock.patch.object(xlsx_stream, "LARGE_CELLS", 12):
            self.assertEqual(sum(len(c) for _, c in self.stream(data)[0].sheets), 12)
        with mock.patch.object(xlsx_stream, "LARGE_CELLS", 10):
            self.rejects("LIMIT", self.stream, data)

    def test_shared_string_count_over_limit_rejects_with_limit(self):
        data = workbook_bytes({"S": ""}, shared_strings=[str(i) for i in range(11)])
        with mock.patch.object(xlsx_stream, "LARGE_CELLS", 11): self.stream(data)
        with mock.patch.object(xlsx_stream, "LARGE_CELLS", 10): self.rejects("LIMIT", self.stream, data)

    def test_sheet_count_over_limit_rejects_with_limit(self):
        data = workbook_bytes({"A": "", "B": "", "C": ""})
        with mock.patch.object(xlsx_stream, "LARGE_SHEETS", 3): self.stream(data)
        with mock.patch.object(xlsx_stream, "LARGE_SHEETS", 2): self.rejects("LIMIT", self.stream, data)

    def test_cells_arrive_in_batches_with_checkpoints_every_thousand_cells(self):
        rows = "".join(f'<row r="{r}"><c r="A{r}"><v>{r}</v></c><c r="B{r}" t="s"><v>{r % 2}</v></c></row>' for r in range(1, 1251))
        calls = []
        sink, _ = self.stream(workbook_bytes({"A": rows, "B": rows}, shared_strings=["x", "y"]),
                              checkpoint=lambda done, phase: calls.append((done, phase)))
        # Pending edits are flushed at every cumulative checkpoint, so the sink is current when a checkpoint cancels.
        self.assertEqual(sink.batches, [1000, 1000, 500, 500, 1000, 1000])
        self.assertEqual([len(c) for _, c in sink.sheets], [2500, 2500])
        cell_counts = [done for done, _ in calls]
        self.assertEqual(cell_counts, sorted(cell_counts))
        self.assertEqual(cell_counts[-1], 5000)
        gaps = [b-a for a, b in zip([0]+cell_counts, cell_counts+[5000])]
        self.assertLessEqual(max(gaps), 1000)
        self.assertIn((0, "Reading sheet A"), calls); self.assertIn((2500, "Reading sheet B"), calls)

    def test_cancel_through_checkpoint_stops_reading(self):
        class Cancelled(Exception): pass
        rows = "".join(f'<row r="{r}"><c r="A{r}"><v>{r}</v></c></row>' for r in range(1, 5001))
        sink = ListSink()

        def checkpoint(done, phase):
            if done >= 2000: raise Cancelled()
        with self.assertRaises(Cancelled):
            self.stream(workbook_bytes({"A": rows}), sink=sink, checkpoint=checkpoint)
        self.assertLessEqual(sum(len(c) for _, c in sink.sheets), 2000)

    def test_fifty_thousand_cells_stream_in_bounded_memory(self):
        path = Path(self.directory.name)/"large.xlsx"
        reference = workbook_bytes({"Big": ""}, shared_strings=["one", "two"])
        with zipfile.ZipFile(io.BytesIO(reference)) as source, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
            for name in source.namelist():
                if name != "xl/worksheets/sheet1.xml": target.writestr(name, source.read(name))
            with target.open("xl/worksheets/sheet1.xml", "w") as part:
                part.write(SHEET_OPEN.encode())
                for r in range(1, 5001):
                    part.write((f'<row r="{r}">'+"".join(f'<c r="{col}{r}"><v>{r*10}</v></c>' for col in "ABCDEFG")
                                + f'<c r="H{r}" t="s"><v>{r % 2}</v></c><c r="I{r}" t="inlineStr"><is><t>t{r}</t></is></c>'
                                + f'<c r="J{r}"><f>A{r}+1</f><v>0</v></c></row>').encode())
                part.write(SHEET_CLOSE.encode())
        sink = CountingSink()
        tracemalloc.start()
        try:
            read_workbook(path, {"mode": "mixed"}, sink, lambda *_: None)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(sink.count, 50_000)
        self.assertLess(peak, 64*1024*1024)


if __name__ == "__main__":
    unittest.main()
