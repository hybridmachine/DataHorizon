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
from datahorizon.interchange import STRICT, TRANSITIONAL, workbook_report, xlsx_preview, xlsx_text
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

    def test_a_deferred_member_on_a_checkpoint_boundary_still_checkpoints(self):
        rows = "".join(f'<row r="{r}"><c r="A{r}"><f t="shared" si="0"/><v>0</v></c></row>' if r % 1000 == 0
                       else f'<row r="{r}"><c r="A{r}"><v>{r}</v></c></row>' for r in range(1, 3001))
        rows += '<row r="3001"><c r="A3001"><f t="shared" ref="A1:A3001" si="0">1+1</f><v>2</v></c></row>'
        calls = []
        sink, _ = self.stream(workbook_bytes({"S": rows}), checkpoint=lambda done, phase: calls.append((done, phase)))
        self.assertEqual(len(sink.sheets[0][1]), 3001)
        for done in (1000, 2000, 3000): self.assertIn((done, "Reading cells"), calls)

    def test_resolving_deferred_members_checkpoints_and_can_be_cancelled(self):
        class Cancelled(Exception): pass
        rows = "".join(f'<row r="{r}"><c r="A{r}"><f t="shared" si="0"/></c></row>' for r in range(1, 2501))
        rows += '<row r="2501"><c r="A2501"><f t="shared" ref="A1:A2501" si="0">1+1</f></c></row>'
        data = workbook_bytes({"S": rows})
        calls = []
        sink, _ = self.stream(data, checkpoint=lambda done, phase: calls.append((done, phase)))
        self.assertEqual(len(sink.sheets[0][1]), 2501)
        self.assertEqual([c for c in calls if c[1] == "Resolving shared formulas"],
                         [(2501, "Resolving shared formulas")]*3)
        sink = ListSink()

        def checkpoint(done, phase):
            if phase == "Resolving shared formulas": raise Cancelled()
        with self.assertRaises(Cancelled): self.stream(data, sink=sink, checkpoint=checkpoint)
        self.assertLess(sum(len(c) for _, c in sink.sheets), 2501)

    def test_deferred_members_keep_only_what_conversion_needs(self):
        sheet = ('<row r="1"><c r="A1" t="inlineStr" s="0"><f t="shared" si="0"/><is><r><t>cached</t></r></is>'
                 '<extLst><ext uri="x"><big>'+"y"*5000+'</big></ext></extLst></c></row>'
                 '<row r="2"><c r="A2"><f t="shared" ref="A1:A2" si="0">WEBSERVICE("u")</f><v>1</v></c></row>')
        data = workbook_bytes({"S": sheet})
        tree = xlsx_preview({"file_base64": b64(data), "scope": "workbook", "mode": "mixed"})
        sink, report = self.stream(data)
        self.assertEqual(sorted(sink.sheets[0][1], key=lambda c: c["row"]), tree["workbook_sheets"][0]["cells"])
        self.assertEqual(report.entries(), tree["report_entries"])
        many = "".join(f'<row r="{r}"><c r="A{r}" t="str"><f t="shared" si="0"/><v>{"z"*1000}</v></c></row>' for r in range(1, 21))
        many += '<row r="21"><c r="A21"><f t="shared" ref="A1:A21" si="0">1</f></c></row>'
        with mock.patch.object(xlsx_stream, "DEFERRED_BYTES", 20*1300): self.stream(workbook_bytes({"S": many}))
        with mock.patch.object(xlsx_stream, "DEFERRED_BYTES", 15*1000): self.rejects("LIMIT", self.stream, workbook_bytes({"S": many}))

    def test_report_entries_from_deferred_members_agree_up_to_sample_order(self):
        sheet = ''.join(f'<row r="{r}"><c r="A{r}"><f t="shared" si="0"/><v>{r}</v></c><c r="B{r}"><f>WEBSERVICE("b")</f><v>{r}</v></c>'
                        f'<c r="C{r}" t="b"><v>7</v></c></row>' for r in range(1, 4))
        sheet += '<row r="4"><c r="A4"><f t="shared" ref="A1:A4" si="0">WEBSERVICE(B4)</f><v>4</v></c><c r="C4" t="b"><v>9</v></c></row>'
        data = workbook_bytes({"S": sheet})
        tree = xlsx_preview({"file_base64": b64(data), "scope": "workbook", "mode": "mixed"})
        sink, report = self.stream(data)

        def normal(entries):
            return [{**e, "samples": sorted(e["samples"], key=lambda x: (x["sheet"], x["cell"]))} for e in entries]
        self.assertNotEqual(report.entries(), tree["report_entries"])  # the samples arrive in another order
        self.assertEqual(normal(report.entries()), normal(tree["report_entries"]))
        self.assertEqual(sorted(sink.sheets[0][1], key=lambda c: (c["row"], c["col"])),
                         sorted(tree["workbook_sheets"][0]["cells"], key=lambda c: (c["row"], c["col"])))

    # Per-record byte bound.
    def assert_bounded_rejection(self, data):
        path = self.write(data)
        tracemalloc.start()
        try:
            with self.assertRaises(EngineError) as raised:
                read_workbook(path, {"mode": "mixed"}, CountingSink(), lambda *_: None)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(raised.exception.body["code"], "LIMIT", raised.exception.body)
        self.assertLess(peak, 16*1024*1024)
        return raised.exception

    def test_one_huge_cell_or_shared_string_rejects_in_bounded_memory(self):
        big = "x"*(2*1024*1024)
        for data in (workbook_bytes({"S": f'<row r="1"><c r="A1"><v>{big}</v></c></row>'}),
                     workbook_bytes({"S": f'<row r="1"><c r="A1" t="inlineStr"><is><t>{big}</t></is></c></row>'}),
                     workbook_bytes({"S": ""}, shared_strings=["a", big])):
            error = self.assert_bounded_rejection(data)
            self.assertEqual(str(error), "A single cell or shared string exceeds 1 MiB of XML")
        self.assert_bounded_rejection(workbook_bytes({"S": f'<row r="1">{big}<c r="A1"><v>1</v></c></row>'}))

    def test_long_cells_within_the_bound_import_as_before(self):
        sheet = f'<row r="1"><c r="A1" t="inlineStr"><is><t>{"a"*60000}</t></is></c><c r="B1" t="str"><v>{"b"*70000}</v></c></row>'
        sink, report = self.assert_agree(workbook_bytes({"S": sheet}))
        self.assertEqual([len(c["source"]) for c in sink.sheets[0][1]], [60000])
        self.assertEqual([e["category"] for e in report.entries()], ["cell.text_too_long"])

    def test_a_row_of_16384_cells_is_not_a_single_record(self):
        from datahorizon.formula import address
        row = '<row r="1">'+"".join(f'<c r="{address(1, c)}"><v>{c}{"0"*60}</v></c>' for c in range(1, 16385))+'</row>'
        sink, _ = self.stream(workbook_bytes({"S": row}))
        self.assertEqual(len(sink.sheets[0][1]), 16384)

    def test_held_element_limit_names_its_cause(self):
        sheet = '<row r="1"><c r="A1"><v>1</v></c></row>'
        data = replace_part(workbook_bytes({"S": sheet}), "xl/worksheets/sheet1.xml",
                            SHEET_OPEN+sheet+'</sheetData><extLst><ext uri="u"><x>'+"<y/>"*50+"</x></ext></extLst></worksheet>")
        with mock.patch.object(xlsx_stream, "MAX_HELD", 40):
            error = self.rejects("LIMIT", self.stream, data)
        self.assertIn("another worksheet block holds too many elements", str(error))
        cell = replace_part(workbook_bytes({"S": ""}), "xl/worksheets/sheet1.xml",
                            SHEET_OPEN+'<row r="1"><c r="A1" t="inlineStr"><is>'+"<r/>"*50+"</is></c></row>"+SHEET_CLOSE)
        strings = workbook_bytes({"S": ""}, shared_strings=["a"])
        strings = replace_part(strings, "xl/sharedStrings.xml", f'<sst xmlns="{TRANSITIONAL.main}"><si>'+"<r/>"*50+"</si></sst>")
        with mock.patch.object(xlsx_stream, "MAX_HELD", 40):
            self.assertIn("a cell holds too many elements", str(self.rejects("LIMIT", self.stream, cell)))
            self.assertIn("a shared string holds too many elements", str(self.rejects("LIMIT", self.stream, strings)))

    def test_deferred_inline_strings_keep_only_their_text(self):
        def sheet(runs, deferred):
            member = (f'<c r="A1" t="inlineStr"><f t="shared" si="0"/><is><t>a_x0042_</t>{runs}<r><t>c</t></r>'
                      '<rPh><t>phonetic</t></rPh></is></c>')
            master = '<c r="A2"><f t="shared" ref="A1:A2" si="0">WEBSERVICE("u")</f><v>1</v></c>'
            return (f'<row r="1">{member}</row><row r="2">{master}</row>' if deferred else
                    f'<row r="2">{master}</row><row r="1">{member}</row>')
        # 90,000 empty runs: the most one cell can hold under MAX_HELD (100,000 attached elements).
        for mode in ("mixed", "values"):
            first, _ = self.stream(workbook_bytes({"S": sheet("<r/>"*90_000, True)}), mode)
            second, _ = self.stream(workbook_bytes({"S": sheet("<r/>"*90_000, False)}), mode)
            key = lambda c: c["row"]
            self.assertEqual(sorted(first.sheets[0][1], key=key), sorted(second.sheets[0][1], key=key))
        cell = xlsx_stream.ET.fromstring(f'<c xmlns="{TRANSITIONAL.main}" r="A1" t="inlineStr"><f t="shared" si="0"/>'
                                         '<is><t>a_x0042_</t>'+"<r/>"*1000+'<r><t>é</t></r><rPh><t>zz</t></rPh></is><extLst/></c>')
        copy, size = xlsx_stream._deferred_copy(cell)
        self.assertEqual(sum(1 for _ in copy.iter()), 4)  # c, f, is, t
        self.assertEqual(size, 256+len("rA1tinlineStr")+len("tsharedsi0")+len("a_x0042_é".encode()))
        main = f"{{{TRANSITIONAL.main}}}"
        self.assertEqual(xlsx_text(copy.find(main+"is")), xlsx_text(cell.find(main+"is")))
        self.assertEqual(xlsx_text(copy.find(main+"is")), "aBé")

    def test_ten_thousand_deferred_members_stay_small(self):
        runs = "<r/>"*20
        rows = "".join(f'<row r="{r}">'+"".join(f'<c r="{col}{r}" t="inlineStr"><f t="shared" si="{si}"/><is><t>x</t>{runs}</is></c>'
                                                for si, col in enumerate("AB"))+"</row>" for r in range(1, 5001))
        rows += ('<row r="5001"><c r="A5001"><f t="shared" ref="A1:A5001" si="0">1</f></c>'
                 '<c r="B5001"><f t="shared" ref="B1:B5001" si="1">2</f></c></row>')
        path = self.write(workbook_bytes({"S": rows}))
        with mock.patch.object(xlsx_stream, "DEFERRED_MEMBERS", 10_000):
            tracemalloc.start()
            try:
                read_workbook(path, {"mode": "mixed"}, CountingSink(), lambda *_: None)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        self.assertLess(peak, 64*1024*1024)
        each = 256+len("rA1tinlineStr")+len("tsharedsi0")+1  # at least this per member
        with mock.patch.object(xlsx_stream, "DEFERRED_BYTES", each*9_000):
            self.rejects("LIMIT", read_workbook, path, {"mode": "mixed"}, ListSink(), lambda *_: None)

    def test_whole_read_parts_must_be_utf8(self):
        base = rich_package()
        for part in ("xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"):
            with zipfile.ZipFile(io.BytesIO(base)) as archive: text = archive.read(part)
            data = replace_part(base, part, b'<?xml version="1.0" encoding="ISO-8859-1"?>'+text)
            with self.subTest(part=part): self.rejects("INVALID_SPREADSHEET", self.stream, data)
        self.stream(replace_part(base, "xl/styles.xml", b'<?xml version="1.0" encoding="UTF-8"?>'+STYLES.format(ns=TRANSITIONAL.main).encode()))

    def test_missing_package_file_is_invalid_without_echoing_the_path(self):
        missing = Path(self.directory.name)/"secret-folder"/"absent.xlsx"
        error = self.rejects("INVALID_SPREADSHEET", open_package, missing)
        self.assertNotIn("secret-folder", str(error))
        self.assertNotIn("secret-folder", str(error.body))

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
