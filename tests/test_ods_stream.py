# SPDX-License-Identifier: GPL-3.0-only
"""The large ODS path: the streaming reader and writer, and the ODS upload, stage and export-job routes."""
import base64
from contextlib import contextmanager
import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ods_builder import content_xml, manifest_document, styles_document, workbook_bytes
from test_ods_degrade import FEATURE_TABLE, LAYOUT_STYLES, SPREADSHEET_AFTER, SPREADSHEET_BEFORE, feature_content, settings
from xlsx_builder import workbook_bytes as xlsx_bytes

from datahorizon import ods_stream
from datahorizon import service as service_module
from datahorizon import xlsx_stream
from datahorizon.client import APIError
from datahorizon.conversion_report import ConversionReport
from datahorizon.engine import EngineError, require
from datahorizon.ods import MIME, O, T, WORKBOOK_IMPORT_LINE, native_to_ods, ods_export, ods_preview, ods_to_native, q
from datahorizon.service import Service, openapi
from datahorizon.temporal import Temporal

try:
    from odf.opendocument import load as odf_load
except ImportError:
    odf_load = None
try:
    from openapi_schema_validator import OAS30Validator
except ImportError:
    OAS30Validator = None

FIXTURE = Path(__file__).resolve().parent/'fixtures'/'fidelity.ods'
ODS_TYPE = 'application/vnd.oasis.opendocument.spreadsheet'
XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
MODES = ('formulas', 'mixed', 'values')


def b64(data): return base64.b64encode(data).decode()


def row(*cells, repeat=None, style=None):
    attributes = (f' table:number-rows-repeated="{repeat}"' if repeat else '')+(f' table:style-name="{style}"' if style else '')
    return f'<table:table-row{attributes}>' + ''.join(cells) + '</table:table-row>'


def cell(value=None, kind='float', style=None, repeat=None, formula=None, text=None, extra=''):
    attributes = ''
    if kind is not None and value is not None:
        attribute = {'float': 'office:value', 'percentage': 'office:value', 'currency': 'office:value', 'string': 'office:string-value',
                     'boolean': 'office:boolean-value', 'date': 'office:date-value', 'time': 'office:time-value'}.get(kind, 'office:value')
        attributes += f' office:value-type="{kind}" {attribute}="{value}"'
    elif kind is not None and kind != 'float':
        attributes += f' office:value-type="{kind}"'
    if style: attributes += f' table:style-name="{style}"'
    if repeat: attributes += f' table:number-columns-repeated="{repeat}"'
    if formula: attributes += ' table:formula="'+formula.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')+'"'
    attributes += extra
    return f'<table:table-cell{attributes}>{text}</table:table-cell>' if text is not None else f'<table:table-cell{attributes}/>'


def empty(repeat=None, style=None):
    return cell(kind=None, repeat=repeat, style=style)


STYLES = ('<number:percentage-style style:name="P1"><number:number number:decimal-places="1" number:min-integer-digits="1"/>'
          '<number:text>%</number:text></number:percentage-style>'
          '<number:currency-style style:name="C1"><number:number number:decimal-places="2" number:min-integer-digits="1" number:grouping="true"/>'
          '<number:text> </number:text><number:currency-symbol>EUR</number:currency-symbol></number:currency-style>'
          '<number:date-style style:name="D1"><number:year number:style="long"/><number:text>-</number:text><number:month number:style="long"/>'
          '<number:text>-</number:text><number:day number:style="long"/></number:date-style>'
          '<number:number-style style:name="Odd"><number:scientific-number number:decimal-places="2"/></number:number-style>'
          '<style:style style:name="bold" style:family="table-cell"><style:text-properties fo:font-weight="bold"/></style:style>'
          '<style:style style:name="pct" style:family="table-cell" style:data-style-name="P1"/>'
          '<style:style style:name="cur" style:family="table-cell" style:data-style-name="C1"/>'
          '<style:style style:name="day" style:family="table-cell" style:data-style-name="D1"/>'
          '<style:style style:name="odd" style:family="table-cell" style:data-style-name="Odd"><style:text-properties fo:font-size="9.5pt"/></style:style>'
          '<style:style style:name="child" style:family="table-cell" style:parent-style-name="Base"><style:paragraph-properties fo:text-align="end"/></style:style>'
          '<style:style style:name="lost" style:family="table-cell"><style:table-cell-properties fo:background-color="red" fo:border="0.06pt dashed #ff0000"/></style:style>'
          '<style:style style:name="ro1" style:family="table-row"/>')
COMMON = ('<style:style style:name="Base" style:family="table-cell"><style:text-properties fo:font-style="italic" fo:color="#3366FF"/>'
          '<style:table-cell-properties fo:background-color="#FFEE00" fo:wrap-option="wrap"/></style:style>')
DATA = (row(cell(2), cell('hello  world', 'string', text='<text:p>hello<text:s text:c="2"/>world</text:p>'), cell('true', 'boolean'),
            cell('2024-02-29', 'date', 'day'), cell('PT01:02:03', 'time'), cell('0.25', 'percentage', 'pct'), cell('3.5', 'currency', 'cur')),
        row(cell(1, style='bold', repeat=2), empty(repeat=5), '<table:covered-table-cell table:number-columns-repeated="2"/>',
            cell('x', 'string', text='<text:p>x</text:p>'), repeat=3, style='ro1')
        + '<table:table-row-group>'
        + row(cell(4, formula='of:=[.A1]*2'), cell('big', 'string', formula='of:=IFS([.A1]>1;"big")', text='<text:p>big</text:p>'),
              cell(10, formula='of:=SUM([$Calc.A1:.A2])'), cell(8, formula='of:=[.A1]^3'), cell(kind=None, formula='of:=[.A1]+1'),
              cell('#N/A', 'string', formula='of:=NA()', text='<text:p>#N/A</text:p>'))
        + '<table:table-header-rows>' + row(cell(kind='unknown'), cell(7, style='odd'), cell(1, style='child'), empty(style='lost', repeat=3)) + '</table:table-header-rows>'
        + '</table:table-row-group>'
        + row(empty(repeat=1024), repeat=100)
        + row(cell('two\nlines', 'string', text='<text:p>two</text:p><text:p>lines</text:p>'), empty(style='bold'))
        + row(empty(repeat=1024), repeat=1048576-112))
CALC = row(cell(10), cell(kind=None, formula='of:=[$Data.A1]*[.A1]')) + row(cell(5, repeat=3), repeat=2)
SHEETS = {'Data': DATA, 'Calc': CALC, 'Empty': ''}


def rich_package():
    return workbook_bytes(SHEETS, styles=STYLES, styles_xml=styles_document(COMMON), manifest=manifest_document())


def rebuild(data, replace=None, *, first=None, stored=True):
    """Copy a package, replacing parts by name; `first` names the entry to write first."""
    replace = replace or {}
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, 'w') as target:
        names = source.namelist()
        if first is not None: names = [first]+[n for n in names if n != first]
        for name in names:
            payload = replace.get(name, source.read(name))
            method = zipfile.ZIP_STORED if (name == 'mimetype' and stored) else zipfile.ZIP_DEFLATED
            target.writestr(name, payload, compress_type=method)
        for name, payload in replace.items():
            if name not in names: target.writestr(name, payload, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


class ListSink:
    """Keeps every cell; like the on-disk stage, it rejects a duplicate coordinate in a sheet."""
    def __init__(self):
        self.sheets = []; self.batches = []; self.strings = None; self._seen = set()

    def sheet(self, name):
        self.sheets.append((name, [])); self._seen = set()

    def cells(self, batch):
        assert 0 < len(batch) <= 1000, len(batch)
        for item in batch:
            require((item['row'], item['col']) not in self._seen, 'INVALID_SPREADSHEET', 'Duplicate cell coordinates')
            self._seen.add((item['row'], item['col']))
        self.sheets[-1][1].extend(batch); self.batches.append(len(batch))


class CountingSink:
    def __init__(self): self.count = 0; self.names = []
    def sheet(self, name): self.names.append(name)
    def cells(self, batch): self.count += len(batch)


def sorted_entries(entries):
    return [{**e, 'samples': sorted(e['samples'], key=lambda s: (s['sheet'] or '', s['cell'] or ''))} for e in entries]


class StreamingReaderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name); self.files = 0

    def write(self, data):
        self.files += 1
        path = self.directory/f'book{self.files}.ods'
        path.write_bytes(data)
        return path

    def stream(self, data, mode='formulas', sink=None, checkpoint=lambda *_: None, **request):
        sink = sink or ListSink()
        report = ods_stream.read_workbook(self.write(data), {'mode': mode, **request}, sink, checkpoint)
        return sink, report

    def rejects(self, code, call, *args, **kwargs):
        with self.assertRaises(EngineError) as raised: call(*args, **kwargs)
        self.assertEqual(raised.exception.body['code'], code, raised.exception.body)
        return raised.exception

    def assert_agree(self, data, mode):
        tree = ods_preview({'file_base64': b64(data), 'scope': 'workbook', 'mode': mode})
        sink, report = self.stream(data, mode)
        self.assertEqual(sink.sheets, [(s['name'], s['cells']) for s in tree['workbook_sheets']])
        self.assertEqual(sorted_entries(report.entries()), sorted_entries(tree['report_entries']))
        self.assertEqual(report.totals(), tree['totals'])
        self.assertEqual(sorted(ods_stream.scope_lines({'mode': mode})+report.lines()), sorted(tree['report']))
        return sink, report

    def test_agrees_with_the_tree_reader_on_the_fixture(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                sink, _ = self.assert_agree(FIXTURE.read_bytes(), mode)
                self.assertEqual([name for name, _ in sink.sheets], ['Inputs', 'Summary', 'Empty'])

    def test_agrees_with_the_tree_reader_on_a_rich_package(self):
        data = rich_package()
        for mode in MODES:
            with self.subTest(mode=mode):
                sink, report = self.assert_agree(data, mode)
                cells = dict(sink.sheets)
                # Repeated styled cells expand in all three rows; the covered and empty runs store nothing.
                self.assertEqual([(c['row'], c['col']) for c in cells['Data'] if c['row'] in (2, 3, 4)],
                                 [(r, c) for r in (2, 3, 4) for c in (1, 2, 10)])
                # The formula without a cache is omitted in values mode.
                self.assertEqual(len(cells['Calc']), 2+6 if mode != 'values' else 1+6)
                self.assertEqual(cells['Empty'], [])
                self.assertGreater(report.totals()['omitted'], 0)

    def test_agrees_with_the_tree_reader_on_small_hand_built_packages(self):
        packages = {
            'quoted sheet references': {"O'Brien": row(cell(7)), 'Sum': row(cell(14, formula="of:=[$'O''Brien'.A1]*2"),
                                                                         cell(9, formula='of:=SUM([$Missing.A1])'))},
            'matrix and errors': {'S': row(cell(3, formula='of:=SUM([.A2:.A3])', extra=' table:number-matrix-columns-spanned="1" table:number-matrix-rows-spanned="1"'),
                                           cell('#DIV/0!', 'string', formula='of:=1/0', text='<text:p>#DIV/0!</text:p>'),
                                           cell('x', 'string', extra=' calcext:value-type="error"', text='<text:p>#VALUE!</text:p>'))
                                       + row(cell(1), cell(kind='unknown'), cell('nope', 'boolean'), cell('2024-13-01', 'date'))},
            'offset timestamps': {'S': row(cell('2024-02-29T10:00:00+02:00', 'date'), cell('2024-02-29T10:00:00', 'date'))},
            'merged spans and text': {'S': row(cell('a', 'string', extra=' table:number-columns-spanned="2"', text='<text:p>a<text:tab/>b<text:line-break/>c</text:p>'),
                                               '<table:covered-table-cell/>', cell(kind=None, text='<text:p> lead <text:span>span</text:span></text:p>')),
                                      'T': row(empty(repeat=3), cell(5), repeat=4)+row(cell(6))},
        }
        for name, sheets in packages.items():
            for mode in MODES:
                for policy in ('reject', 'utc'):
                    with self.subTest(name, mode=mode, policy=policy):
                        data = workbook_bytes(sheets)
                        tree = ods_preview({'file_base64': b64(data), 'scope': 'workbook', 'mode': mode, 'timezone_policy': policy})
                        sink, report = self.stream(data, mode, timezone_policy=policy)
                        self.assertEqual(sink.sheets, [(s['name'], s['cells']) for s in tree['workbook_sheets']])
                        self.assertEqual(sorted_entries(report.entries()), sorted_entries(tree['report_entries']))

    def test_feature_entries_agree_with_the_tree_reader(self):
        content = feature_content(FEATURE_TABLE + FEATURE_TABLE.replace('table:name="Sheet1"', 'table:name="Two"')
                                  + '<table:table table:name="Hidden" table:style-name="ta2"/>',
                                  before=SPREADSHEET_BEFORE, after=SPREADSHEET_AFTER, spreadsheet_attributes=' table:structure-protected="true"')
        data = workbook_bytes({}, content=content, extra_parts={"settings.xml": settings(("Two", 2, 2)), "Object 1/content.xml": "<x/>"})
        for mode in MODES:
            with self.subTest(mode=mode):
                _, report = self.assert_agree(data, mode)
                found = {(e['category'], e['detail']): e for e in report.entries() if e['category'].endswith('.feature')}
                self.assertEqual(found[('sheet.feature', 'merged_cells')]['samples'], [{'sheet': 'Sheet1', 'cell': None}, {'sheet': 'Two', 'cell': None}])
                self.assertEqual(found[('sheet.feature', 'frozen_panes')]['samples'], [{'sheet': 'Two', 'cell': None}])
                self.assertEqual(found[('sheet.feature', 'auto_filter')]['samples'], [{'sheet': 'Sheet1', 'cell': None}])
                for key in (('workbook.feature', 'defined_names'), ('workbook.feature', 'hidden_sheets'), ('workbook.feature', 'embedded_objects'),
                            ('sheet.feature', 'comments'), ('sheet.feature', 'hyperlinks'), ('sheet.feature', 'conditional_formatting'),
                            ('sheet.feature', 'column_widths'), ('sheet.feature', 'row_heights')):
                    self.assertIn(key, found)

    def test_layout_styles_after_the_body_still_agree(self):
        late = feature_content(FEATURE_TABLE, styles="").replace(
            '</office:body>', f'</office:body><office:automatic-styles>{LAYOUT_STYLES}</office:automatic-styles>')
        _, report = self.assert_agree(workbook_bytes({}, content=late), 'formulas')
        self.assertIn('row_heights', {e['detail'] for e in report.entries()})

    def test_style_definitions_after_the_body_still_agree(self):
        late = content_xml({'S': row(cell(1, style='late'))}).replace(
            '</office:body>', '</office:body><office:automatic-styles><style:style style:name="late" style:family="table-cell">'
            '<style:text-properties fo:font-weight="bold"/></style:style></office:automatic-styles>')
        sink, _ = self.assert_agree(workbook_bytes({}, content=late), 'formulas')
        self.assertEqual(sink.sheets[0][1][0]['style'], {'bold': True})

    def test_trailing_million_row_repeat_adds_no_cells(self):
        data = workbook_bytes({'S': row(cell(1), cell(2)) + row(empty(repeat=16384), repeat=1048576)})
        started = time.monotonic()
        sink, report = self.stream(data)
        self.assertLess(time.monotonic()-started, 2)
        self.assertEqual([(c['row'], c['col']) for c in sink.sheets[0][1]], [(1, 1), (1, 2)])
        self.assertEqual(report.totals(), {'converted': 0, 'approximated': 0, 'omitted': 0, 'unevaluable': 0})

    def test_styled_repeat_beyond_the_cell_limit_is_rejected_before_expansion(self):
        # 1,000 rows of a styled cell repeated 2,000 times is 2,000,000 stored cells.
        data = workbook_bytes({'S': row(empty(style='bold', repeat=2000), repeat=1000)}, styles=STYLES)
        sink = CountingSink()
        tracemalloc.start()
        started = time.monotonic()
        try:
            error = self.rejects('LIMIT', self.stream, data, sink=sink)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(time.monotonic()-started, 2)
        self.assertIn('1,000,000', error.body['message'])
        self.assertEqual(sink.count, 0, 'nothing is materialised before the limit is known to be exceeded')
        self.assertLess(peak, 8*1024*1024)
        # Exactly at the limit the same repeat is accepted and expanded.
        with patch.object(xlsx_stream, 'LARGE_CELLS', 2000*1000):
            self.stream(data, sink=(counted := CountingSink()))
        self.assertEqual(counted.count, 2_000_000)

    def test_a_styled_repeat_over_two_rows_is_counted_against_the_limit(self):
        data = workbook_bytes({'S': row(cell(1), empty(style='bold', repeat=6), repeat=2)}, styles=STYLES)
        with patch.object(xlsx_stream, 'LARGE_CELLS', 14): self.stream(data)
        with patch.object(xlsx_stream, 'LARGE_CELLS', 13): self.rejects('LIMIT', self.stream, data)

    def test_five_million_empty_repeated_cells_read_quickly_and_still_checkpoint(self):
        # 50,000 rows of 100 empty repeated cells, in 100,000 elements: nothing is stored.
        data = workbook_bytes({'S': row(empty(repeat=99), empty())*50_000})
        calls = []
        started = time.monotonic()
        sink, _ = self.stream(data, sink=CountingSink(), checkpoint=lambda done, phase: calls.append((done, phase)))
        self.assertLess(time.monotonic()-started, 5)
        self.assertEqual(sink.count, 0)
        self.assertGreaterEqual(sum(1 for _, phase in calls if phase == 'Reading cells'), 15)

    def test_cancellation_stops_a_sheet_of_empty_cells(self):
        data = workbook_bytes({'S': row(empty(repeat=99), empty())*50_000})
        seen = []
        def checkpoint(done, phase):
            seen.append(phase)
            if seen.count('Reading cells') == 3: raise EngineError('CANCELLED', 'stop')
        self.rejects('CANCELLED', self.stream, data, sink=CountingSink(), checkpoint=checkpoint)

    def test_checkpoints_at_least_every_thousand_stored_cells(self):
        data = workbook_bytes({'S': row(*(cell(n) for n in range(1, 51)))*100})
        calls = []
        sink, _ = self.stream(data, checkpoint=lambda done, phase: calls.append(done))
        self.assertEqual(sum(len(c) for _, c in sink.sheets), 5000)
        self.assertTrue(set(range(1000, 5001, 1000)) <= set(calls), calls)
        self.assertLessEqual(max(sink.batches), 1000)

    def test_two_mebibyte_cell_is_limit_with_bounded_memory(self):
        spans = '<text:span>'+'x'*1000+'</text:span>'
        data = workbook_bytes({'S': row(cell('big', 'string', text='<text:p>'+spans*2100+'</text:p>'))})
        tracemalloc.start()
        try:
            error = self.rejects('LIMIT', self.stream, data)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertIn('single cell', error.body['message'])
        self.assertLess(peak, 16*1024*1024)

    def test_unsafe_or_foreign_packages_reject(self):
        good = rich_package()
        doctype = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]>'+content_xml({'S': ''})
        entity = content_xml({'S': row(cell('x', 'string', text='<text:p>&a;</text:p>'))}).replace('<office:document-content', '<!ENTITY a "b"><office:document-content')
        encrypted = manifest_document('<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml">'
                                      '<manifest:encryption-data manifest:checksum-type="SHA1" manifest:checksum="AAAA"/></manifest:file-entry>')
        cases = {'doctype': rebuild(good, {'content.xml': doctype}),
                 'entity': rebuild(good, {'content.xml': entity}),
                 'encrypted manifest': rebuild(good, {'META-INF/manifest.xml': encrypted}),
                 'wrong mimetype': rebuild(good, {'mimetype': b'application/vnd.oasis.opendocument.text'}),
                 'mimetype not first': rebuild(good, first='content.xml'),
                 'compressed mimetype': rebuild(good, stored=False),
                 'missing content': self.without(good, 'content.xml'),
                 'missing mimetype': self.without(good, 'mimetype'),
                 'utf-16 declaration': rebuild(good, {'content.xml': '<?xml version="1.0" encoding="UTF-16"?>'+content_xml({'S': ''})}),
                 'not a zip': b'PK not really',
                 'wrong root': rebuild(good, {'content.xml': f'<office:document-styles xmlns:office="{O}"/>'})}
        for name, data in cases.items():
            with self.subTest(name):
                self.rejects('INVALID_SPREADSHEET', self.stream, data)

    @staticmethod
    def without(data, name):
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, 'w') as target:
            for item in source.infolist():
                if item.filename != name: target.writestr(item, source.read(item.filename))
        return output.getvalue()

    def test_cells_outside_the_grid_are_limit(self):
        for rows in (row(empty(repeat=16383), cell(1, repeat=2)),
                     row(empty(repeat=16384), repeat=1048575)+row(cell(1), repeat=2),
                     row(cell(1), repeat=1048577)):
            with self.subTest(rows=rows[:60]):
                self.rejects('LIMIT', self.stream, workbook_bytes({'S': rows}))

    def test_style_definitions_are_bounded(self):
        data = workbook_bytes({'S': row(cell(1))}, styles=STYLES)
        # Four data styles and seven cell styles; the row style is not kept.
        with patch.object(ods_stream, 'MAX_STYLE_DEFINITIONS', 11): self.stream(data)
        with patch.object(ods_stream, 'MAX_STYLE_DEFINITIONS', 10): self.rejects('LIMIT', self.stream, data)
        with patch.object(ods_stream, 'STYLE_BYTES', 64): self.rejects('LIMIT', self.stream, data)

    def test_a_repeated_row_is_held_within_its_byte_bound(self):
        data = workbook_bytes({'S': row(*(cell('y'*100, 'string', text='<text:p>'+'y'*100+'</text:p>') for _ in range(20)), repeat=3)})
        sink, _ = self.stream(data)
        self.assertEqual(len(sink.sheets[0][1]), 60)
        with patch.object(ods_stream, 'REPEATED_ROW_BYTES', 1000): self.rejects('LIMIT', self.stream, data)

    def test_more_sheets_than_the_tree_reader_allows(self):
        data = workbook_bytes({f'S{n}': row(cell(n)) for n in range(1, 101)})
        sink, _ = self.stream(data)
        self.assertEqual(len(sink.sheets), 100)
        with patch.object(xlsx_stream, 'LARGE_SHEETS', 99): self.rejects('LIMIT', self.stream, data)

    def test_request_checks(self):
        data = workbook_bytes({'S': row(cell(1))})
        self.rejects('INVALID_ARGUMENT', self.stream, data, mode='sideways')
        self.rejects('INVALID_ARGUMENT', self.stream, data, timezone_policy='local')
        self.rejects('INVALID_ARGUMENT', self.stream, data, row=2)

    def test_reads_an_open_file_from_its_start(self):
        with open(self.write(FIXTURE.read_bytes()), 'rb') as package:
            package.read(7)
            sink = ListSink()
            ods_stream.read_workbook(package, {}, sink, lambda *_: None)
            self.assertFalse(package.closed)
        self.assertEqual(len(sink.sheets), 3)


def native_cells():
    """Native cells as Workbook.stream_workbook yields them: formulas carry no value or calculation."""
    return [
        {'row': 1, 'col': 1, 'source': 'a  b\tc', 'kind': 'auto', 'style': {'bold': True}, 'value': 'a  b\tc', 'calculation': 'current'},
        {'row': 1, 'col': 3, 'source': '42', 'kind': 'number', 'style': {'format': 'percent', 'decimals': 1}, 'value': 42, 'calculation': 'current'},
        {'row': 3, 'col': 2, 'source': '2024-02-29', 'kind': 'date', 'style': {}, 'value': Temporal('2024-02-29', 'date'), 'value_kind': 'date', 'calculation': 'current'},
        {'row': 3, 'col': 4, 'source': 'TRUE', 'kind': 'boolean', 'style': {'format': 'currency', 'currency': 'EUR', 'border': 'box'}, 'value': True, 'calculation': 'current'},
        {'row': 6, 'col': 1, 'source': '', 'kind': 'null', 'style': {'background': '#ff0000'}, 'value': None, 'calculation': 'current'}]


class StreamingWriterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def write(self, sheets, name='out.ods', checkpoint=lambda *_: None, scratch=None):
        report = ConversionReport()
        written = ods_stream.write_workbook(self.directory/name, iter(sheets), report, checkpoint, scratch=scratch)
        return written, report, (self.directory/name).read_bytes()

    def test_values_are_written_exactly_as_the_bounded_export_writes_them(self):
        sheets = [('Sheet1', native_cells()), ('Data', [{**native_cells()[1], 'row': 2}]), ('Empty', [])]
        written, report, data = self.write([(name, iter(cells)) for name, cells in sheets])
        bounded = base64.b64decode(ods_export({'state': 's', 'sheets': [{'name': n, 'cells': c} for n, c in sheets]}, {'scope': 'workbook'})['file_base64'])
        with zipfile.ZipFile(io.BytesIO(data)) as left, zipfile.ZipFile(io.BytesIO(bounded)) as right:
            self.assertEqual(left.namelist(), right.namelist())
            self.assertEqual(left.infolist()[0].compress_type, zipfile.ZIP_STORED)
            for name in ('mimetype', 'styles.xml', 'META-INF/manifest.xml'):
                self.assertEqual(left.read(name), right.read(name), name)
            streamed, expected = left.read('content.xml'), right.read('content.xml')
        # One style table and one cell writer: everything after the root start tag is identical.
        self.assertEqual(streamed[streamed.index(b'<office:automatic-styles'):], expected[expected.index(b'<office:automatic-styles'):])
        self.assertEqual((written['bytes'], written['sha256']), (len(data), hashlib.sha256(data).hexdigest()))
        self.assertEqual((written['sheets'], written['cells'], written['formulas']), (3, 6, 0))
        self.assertEqual(report.entries(), [])

    def test_formulas_are_written_without_caches_and_reported(self):
        cells = [{'row': 1, 'col': 1, 'source': '5', 'kind': 'number', 'style': {}, 'value': 5, 'calculation': 'current'},
                 {'row': 1, 'col': 2, 'source': '=A1*2', 'kind': 'formula', 'style': {}},
                 {'row': 1, 'col': 3, 'source': '=CONCAT(Other!A1,"x")', 'kind': 'formula', 'style': {'format': 'date_iso'}}]
        written, report, data = self.write([('Sheet1', iter(cells)), ('Other', iter([]))])
        self.assertEqual((written['formulas'], written['unevaluated_formulas'], written['modern_functions']), (2, 1, True))
        self.assertEqual([(e['category'], e['severity'], e['count'], e['samples']) for e in report.entries()],
                         [(ods_stream.CACHE_CATEGORY, 'approximated', 2, [{'sheet': 'Sheet1', 'cell': 'B1'}, {'sheet': 'Sheet1', 'cell': 'C1'}]),
                          (xlsx_stream.FORMULA_DISPLAY_CATEGORY, 'approximated', 1, [{'sheet': 'Sheet1', 'cell': 'B1'}])])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root = ET.fromstring(archive.read('content.xml'))
        formulas = [c for c in root.iter(q(T, 'table-cell')) if c.get(q(T, 'formula'))]
        self.assertEqual([c.get(q(T, 'formula')) for c in formulas], ['of:=([.A1]*2.0)', "of:=COM.MICROSOFT.CONCAT([$'Other'.A1];\"x\")"])
        self.assertTrue(all(c.get(q(O, 'value-type')) is None for c in formulas), 'no cached values')
        lines = ods_stream.export_lines({}, written['modern_functions'], written['formulas'], written['unevaluated_formulas'])
        self.assertIn(ods_stream.FORMULA_LINE, lines)
        self.assertIn(xlsx_stream.FORMULA_DISPLAY_LINE, lines)
        # The tree reader reads the package back.
        tree = ods_preview({'file_base64': b64(data), 'scope': 'workbook'})
        self.assertEqual([c['source'] for c in tree['workbook_sheets'][0]['cells']], ['5', '=(A1*2.0)', "=CONCAT('Other'!A1,\"x\")"])

    def test_limits_remove_the_partial_file_and_scratch(self):
        cells = [{'row': n, 'col': 1, 'source': str(n), 'kind': 'number', 'style': {}, 'value': n, 'calculation': 'current'} for n in range(1, 7)]
        scratch = self.directory/'scratch.part'
        def reserve():
            scratch.touch(); return scratch
        for name, value in (('LARGE_CELLS', 5), ('LARGE_EXPANDED', 1024), ('LARGE_SHEETS', 0)):
            with self.subTest(name), patch.object(xlsx_stream, name, value):
                with self.assertRaises(EngineError) as raised: self.write([('S', iter(cells))], scratch=reserve)
                self.assertEqual(raised.exception.body['code'], 'LIMIT')
                self.assertFalse((self.directory/'out.ods').exists())
                self.assertEqual(scratch.stat().st_size if scratch.exists() else 0, 0, 'the body scratch is emptied')
        with self.assertRaises(EngineError) as raised:
            self.write([('S', iter([{'row': 1, 'col': 1, 'source': 'a\x01', 'kind': 'text', 'style': {}, 'value': 'a\x01', 'calculation': 'current'}]))])
        self.assertEqual(raised.exception.body['code'], 'UNSUPPORTED_CELL')
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['scratch.part'])

    def test_cancellation_while_writing_removes_the_file(self):
        cells = [{'row': n, 'col': 1, 'source': str(n), 'kind': 'number', 'style': {}, 'value': n, 'calculation': 'current'} for n in range(1, 3001)]
        def checkpoint(done, phase):
            if done >= 2000: raise EngineError('CANCELLED', 'stop')
        with self.assertRaises(EngineError): self.write([('S', iter(cells))], checkpoint=checkpoint)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_streamed_package_reads_back_through_both_readers(self):
        cells = native_cells()
        _, _, data = self.write([('Sheet1', iter(cells)), ('Empty', iter([]))])
        tree = ods_preview({'file_base64': b64(data), 'scope': 'workbook'})
        path = self.directory/'again.ods'; path.write_bytes(data)
        sink = ListSink()
        ods_stream.read_workbook(path, {}, sink, lambda *_: None)
        self.assertEqual(sink.sheets, [(s['name'], s['cells']) for s in tree['workbook_sheets']])
        self.assertEqual([c['source'] for c in tree['workbook_sheets'][0]['cells']], ['a  b\tc', '42', '2024-02-29', 'TRUE', ''])

    @unittest.skipIf(odf_load is None, 'odfpy is not installed')
    def test_odfpy_reads_the_streamed_package(self):
        from odf.table import Table, TableCell
        _, _, data = self.write([('Sheet1', iter(native_cells())), ('Empty', iter([]))])
        document = odf_load(io.BytesIO(data))
        self.assertEqual([t.getAttribute('name') for t in document.spreadsheet.getElementsByType(Table)], ['Sheet1', 'Empty'])
        self.assertEqual(document.spreadsheet.getElementsByType(TableCell)[0].getAttribute('valuetype'), 'string')


def stored_rows(engine):
    with engine.lock:
        return engine.db.execute("SELECT sheets.name, row, col, source, kind, style FROM cells JOIN sheets ON sheets.id = cells.sheet_id "
                                 "ORDER BY sheets.position, row, col").fetchall()


class LargeOdsServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.service = Service(staging_root=self.temp/'staging')
        worker = threading.Thread(target=self.service.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        worker.start()
        def stop():
            self.service.shutdown(); worker.join(timeout=2); self.service.server_close()
        self.addCleanup(stop)
        self.book = self.open(self.temp/'book.dhwb', create=True)

    def request(self, method, path, body=None, content_type='application/json'):
        connection = http.client.HTTPConnection('127.0.0.1', self.service.server_port, timeout=120)
        try:
            data = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
            connection.request(method, path, body=data if method != 'GET' else None,
                               headers={'Authorization': 'Bearer '+self.service.token, 'Content-Type': content_type})
            reply = connection.getresponse()
            payload = reply.read()
            return reply.status, (json.loads(payload) if reply.getheader('Content-Type', '').startswith('application/json') else payload), dict(reply.getheaders())
        finally:
            connection.close()

    def call(self, path, body=None, method='POST'):
        status, result, _ = self.request(method, path, body)
        if status != 200: raise APIError(status, result)
        return result

    def refused(self, path, body, status, code):
        got, result, _ = self.request('POST', path, body)
        self.assertEqual((got, result['code']), (status, code), result)
        return result

    def open(self, path, create=False):
        return self.call('/v1/open', {'path': str(path), 'create': create})['id']

    def info(self, book=None):
        return self.call(f'/v1/workbooks/{book or self.book}', method='GET')

    def operate(self, request, book=None):
        request = {'expected_state': self.info(book)['state'], **request}
        request.setdefault('operation_id', 'op-'+str(time.monotonic_ns()))
        return self.call(f'/v1/workbooks/{book or self.book}/operations', request)

    def edit(self, cells, sheet='Sheet1', book=None):
        return self.operate({'action': 'edit', 'sheet': sheet, 'cells': cells}, book)

    def engine(self, book=None):
        return self.service.workbooks[book or self.book]

    def upload(self, identifier, data):
        status, receipt, _ = self.request('PUT', '/v1/uploads/'+identifier, data, 'application/octet-stream')
        self.assertEqual(status, 200, receipt)
        return receipt

    def terminal(self, job_id, seconds=120):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            job = self.call('/v1/jobs/'+job_id, method='GET')
            if job['status'] != 'running': return job
            time.sleep(.01)
        self.fail('Job did not finish')

    def completed(self, job_id):
        job = self.terminal(job_id)
        self.assertEqual(job['status'], 'completed', job)
        return job['result']

    def staged(self, data, job_id, fmt='ods', **request):
        """Upload a package and prepare it into a stage; return the review summary."""
        self.upload(job_id, data)
        self.call(f'/v1/import/{fmt}/jobs', {'job_id': job_id, 'upload_id': job_id, 'scope': 'workbook', **request})
        return self.completed(job_id)

    def apply(self, summary, name):
        target = self.open(self.temp/(name+'.dhwb'), create=True)
        receipt = self.operate({'action': 'workbook_import', 'stage_id': summary['stage_id'], 'stage_digest': summary['stage_digest']}, target)
        self.assertEqual(receipt['outcome'], 'committed')
        return target

    def export(self, job_id='export', book=None, fmt='ods'):
        self.call(f'/v1/workbooks/{book or self.book}/export/{fmt}/jobs', {'job_id': job_id, 'mode': 'formulas'})
        return self.completed(job_id)

    def download(self, export_id):
        status, data, headers = self.request('GET', '/v1/exports/'+export_id)
        self.assertEqual(status, 200, data)
        return data, headers

    def staging_names(self):
        return sorted(p.name for p in self.service.staging.root.iterdir())

    def test_sixty_thousand_cells_prepare_apply_and_equal_the_xlsx_path(self):
        rows = 6000
        ods = workbook_bytes({'Big': ''.join(row(*(cell(r*c) for c in range(1, 11)),
                                               cell(f'r{r}', 'string', text=f'<text:p>r{r}</text:p>')) for r in range(1, rows+1))})
        xlsx = xlsx_bytes({'Big': ''.join(f'<row r="{r}">' + ''.join(f'<c r="{chr(64+c)}{r}"><v>{r*c}</v></c>' for c in range(1, 11))
                                          + f'<c r="K{r}" t="inlineStr"><is><t>r{r}</t></is></c></row>' for r in range(1, rows+1))})
        summary = self.staged(ods, 'odsbig')
        self.assertEqual((summary['format'], summary['stored_cells'], summary['mode']), ('ods', 66_000, 'formulas'))
        self.assertEqual(summary['source']['sha256'], hashlib.sha256(ods).hexdigest())
        self.assertIn(WORKBOOK_IMPORT_LINE, summary['report'])
        self.assertLess(len(json.dumps(summary).encode()), 1024*1024)
        from_ods = stored_rows(self.engine(self.apply(summary, 'fromods')))
        from_xlsx = stored_rows(self.engine(self.apply(self.staged(xlsx, 'xlsxbig', 'xlsx'), 'fromxlsx')))
        self.assertEqual(len(from_ods), 66_000)
        self.assertEqual(from_ods, from_xlsx)

    def test_source_path_and_inline_preparation(self):
        path = self.temp/'fixture.ods'; path.write_bytes(FIXTURE.read_bytes())
        self.call('/v1/import/ods/jobs', {'job_id': 'local', 'source_path': str(path), 'scope': 'workbook', 'mode': 'mixed'})
        summary = self.completed('local')
        self.assertEqual(summary['source']['kind'], 'path')
        self.assertEqual([s['name'] for s in summary['sheets']], ['Inputs', 'Summary', 'Empty'])
        # A file changed after it was reviewed is refused at Apply.
        os.utime(path, ns=(1, 1))
        target = self.open(self.temp/'t.dhwb', create=True)
        status, error, _ = self.request('POST', f'/v1/workbooks/{target}/operations', {
            'operation_id': 'stale', 'expected_state': self.info(target)['state'], 'action': 'workbook_import',
            'stage_id': 'local', 'stage_digest': summary['stage_digest']})
        self.assertEqual((status, error['code']), (409, 'STALE_PLAN'))
        # The inline bounded preview is unchanged and makes no stage.
        self.call('/v1/import/ods/jobs', {'job_id': 'inline', 'file_base64': b64(FIXTURE.read_bytes()), 'scope': 'workbook'})
        inline = self.completed('inline')
        self.assertIn('workbook_sheets', inline); self.assertNotIn('stage_id', inline)
        self.assertEqual([n for n in self.staging_names() if n.startswith('stage-')], ['stage-local.stage'])

    def test_preparation_request_checks(self):
        self.upload('u', FIXTURE.read_bytes())
        for body, status, code in (({'job_id': 'a', 'upload_id': 'u', 'source_path': '/tmp/x', 'scope': 'workbook'}, 400, 'INVALID_ARGUMENT'),
                                   ({'job_id': 'a', 'scope': 'workbook'}, 400, 'INVALID_ARGUMENT'),
                                   ({'job_id': 'a', 'upload_id': 'u'}, 400, 'INVALID_ARGUMENT'),
                                   ({'job_id': 'a', 'upload_id': 'u', 'scope': 'workbook', 'sheet_index': 1}, 400, 'INVALID_ARGUMENT'),
                                   ({'job_id': 'a.b', 'upload_id': 'u', 'scope': 'workbook'}, 400, 'INVALID_JOB'),
                                   ({'job_id': 'a', 'upload_id': 'gone', 'scope': 'workbook'}, 404, 'JOB_NOT_FOUND'),
                                   ({'job_id': 'a', 'source_path': 'relative.ods', 'scope': 'workbook'}, 400, 'INVALID_PATH'),
                                   ({'job_id': 'a', 'source_path': str(self.temp), 'scope': 'workbook'}, 400, 'INVALID_PATH')):
            with self.subTest(body=body):
                self.refused('/v1/import/ods/jobs', body, status, code)
        self.assertEqual([n for n in self.staging_names() if n.startswith('stage-')], [])
        # A malformed package fails the job and deletes its stage.
        self.upload('bad', b'PK not a package')
        self.call('/v1/import/ods/jobs', {'job_id': 'bad', 'upload_id': 'bad', 'scope': 'workbook'})
        job = self.terminal('bad')
        self.assertEqual((job['status'], job['error']['code']), ('failed', 'INVALID_SPREADSHEET'))
        self.assertEqual([n for n in self.staging_names() if n.startswith('stage-')], [])

    def test_cancel_during_read_leaves_no_stage(self):
        self.upload('slow', workbook_bytes({'S': row(*(cell(n) for n in range(1, 11)))*3000}))
        reached, proceed = threading.Event(), threading.Event()
        real = ods_stream.read_workbook
        def gated(package, request, sink, checkpoint):
            def gate(done, phase):
                if phase == 'Reading cells' and not reached.is_set():
                    reached.set(); proceed.wait(30)
                checkpoint(done, phase)
            return real(package, request, sink, gate)
        with patch.object(service_module, 'ods_read_workbook', gated):
            self.call('/v1/import/ods/jobs', {'job_id': 'slow', 'upload_id': 'slow', 'scope': 'workbook'})
            try:
                self.assertTrue(reached.wait(30))
                self.assertIn('stage-slow.stage', self.staging_names())
                self.call('/v1/jobs/slow/cancel')
            finally:
                proceed.set()
            job = self.terminal('slow')
        self.assertEqual((job['status'], job['error']['code']), ('cancelled', 'CANCELLED'))
        self.assertEqual(self.staging_names(), ['upload-slow.upload'])

    def fixture_book(self):
        """Text, numbers, cross-sheet formulas, styles, a date, a boolean and an empty sheet; no formula has a temporal result."""
        self.operate({'action': 'sheet_add', 'name': 'Data'})
        self.operate({'action': 'sheet_add', 'name': 'Empty'})
        self.edit([{'row': 1, 'col': 1, 'source': '5'}, {'row': 2, 'col': 1, 'source': 'second'}], 'Data')
        self.edit([{'row': 1, 'col': 1, 'source': 'hello <&> "world"'},
                   {'row': 2, 'col': 1, 'source': '42'},
                   {'row': 2, 'col': 2, 'source': '=Data!A1*2'},
                   {'row': 3, 'col': 2, 'source': '=SUM(A2,Data!A1)', 'style': {'format': 'fixed2'}},
                   {'row': 4, 'col': 2, 'source': '=CONCAT(A1,"!")'},
                   {'row': 1, 'col': 3, 'source': '7', 'style': {'bold': True, 'background': '#ff0000', 'align': 'center'}},
                   {'row': 2, 'col': 3, 'source': 'TRUE'},
                   {'row': 3, 'col': 3, 'source': '2.5', 'style': {'format': 'percent', 'decimals': 1}},
                   {'row': 4, 'col': 1, 'source': '2024-02-29', 'kind': 'date'},
                   {'row': 5, 'col': 1, 'source': 'tab\tand  spaces', 'kind': 'text'},
                   {'row': 6, 'col': 1, 'source': '', 'kind': 'null', 'style': {'border': 'box'}}])

    def test_export_job_download_and_staged_reimport_preserve_cells(self):
        self.fixture_book()
        before = stored_rows(self.engine())
        state = self.info()['state']
        result = self.export()
        self.assertEqual(set(result), {'export_id', 'bytes', 'sha256', 'state', 'report', 'report_entries', 'totals'})
        self.assertEqual(result['state'], state)
        self.assertIn(ods_stream.FORMULA_LINE, result['report'])
        self.assertIn('COM.MICROSOFT', ' '.join(result['report']))
        self.assertEqual(result['totals'], {'converted': 0, 'approximated': 5, 'omitted': 0, 'unevaluable': 0})
        data, headers = self.download('export')
        self.assertEqual(headers['Content-Type'], ODS_TYPE)
        self.assertEqual(headers['Content-Disposition'], 'attachment; filename="export.ods"')
        self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (result['bytes'], result['sha256']))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(archive.infolist()[0].filename, 'mimetype')
            self.assertEqual(archive.read('mimetype'), MIME.encode())
        # The tree reader accepts the package.
        self.assertTrue(ods_preview({'file_base64': b64(data), 'scope': 'workbook'})['can_import'])
        streamed = stored_rows(self.engine(self.apply(self.staged(data, 'again'), 'again')))
        # The same workbook through the bounded export and the tree reader: the large path adds no difference of its own.
        bounded = base64.b64decode(self.call(f'/v1/workbooks/{self.book}/export/ods', {'scope': 'workbook'})['file_base64'])
        self.assertEqual(streamed, stored_rows(self.engine(self.apply(self.staged(bounded, 'bounded'), 'bounded'))))
        # Against the original, only the bounded exporter's representation differences remain, each named here.
        names = ['Sheet1', 'Data', 'Empty']
        def expected(item):
            sheet, r, c, source, kind, style = item
            if kind == 'formula':
                # 1. Formulas come back in the canonical OpenFormula rendering native_to_ods writes.
                source = ods_to_native(native_to_ods(source, sheet_names=names), names)
            elif kind == 'auto' and source:
                # 2. Automatic text is written as an ODS string cell, which reads back as explicit text;
                #    automatic numbers and booleans read back as their typed kinds.
                kind = {'42': 'number', '5': 'number', '7': 'number', '2.5': 'number', 'TRUE': 'boolean'}.get(source, 'text')
            elif kind == 'null':
                # 3. A styled NULL is written as a styled empty cell, which reads back as a styled empty automatic input.
                kind = 'auto'
            if kind == 'number':
                # 4. A number's value is written as Python float text (42 becomes 42.0).
                source = str(float(source))
            if kind == 'date' and style == '{}':
                # 5. An unformatted date takes the ISO calendar display style its export gives it.
                style = '{"format":"date_iso"}'
            return (sheet, r, c, source, kind, style)
        self.assertEqual(streamed, [expected(item) for item in before])
        self.assertEqual([s['name'] for s in self.info(self.open(self.temp/'again.dhwb'))['sheets']], names)

    @unittest.skipIf(odf_load is None, 'odfpy is not installed')
    def test_odfpy_reads_the_downloaded_export(self):
        from odf.table import Table
        self.fixture_book()
        self.export()
        document = odf_load(io.BytesIO(self.download('export')[0]))
        self.assertEqual([t.getAttribute('name') for t in document.spreadsheet.getElementsByType(Table)], ['Sheet1', 'Data', 'Empty'])

    def test_cancel_during_write_leaves_no_export_snapshot_or_scratch(self):
        self.edit([{'row': n, 'col': 1, 'source': str(n)} for n in range(1, 3001)])
        reached, proceed = threading.Event(), threading.Event()
        real = ods_stream.write_workbook
        def gated(path, sheets, report, checkpoint, **options):
            def gate(done, phase):
                if done >= 1000 and not reached.is_set():
                    reached.set(); proceed.wait(30)
                checkpoint(done, phase)
            return real(path, sheets, report, gate, **options)
        with patch.object(service_module, 'ods_write_workbook', gated):
            self.call(f'/v1/workbooks/{self.book}/export/ods/jobs', {'job_id': 'export'})
            try:
                self.assertTrue(reached.wait(30))
                self.assertEqual([n for n in self.staging_names() if not n.endswith('-journal')],
                                 ['export-export.export', 'part-export.part', 'snapshot-export.snapshot'])
                self.call('/v1/jobs/export/cancel')
            finally:
                proceed.set()
            job = self.terminal('export')
        self.assertEqual((job['status'], job['error']['code']), ('cancelled', 'CANCELLED'))
        self.assertEqual(self.staging_names(), [])
        self.assertEqual(self.request('GET', '/v1/exports/export')[0], 404)

    def test_completed_export_leaves_only_its_file_and_releases(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        self.export()
        self.assertEqual(self.staging_names(), ['export-export.export'])
        self.assertEqual(self.call('/v1/exports/export/release'), {'released': True, 'export_id': 'export'})
        self.assertEqual(self.staging_names(), [])

    def test_values_and_mixed_export_jobs_are_refused(self):
        for mode in ('values', 'mixed'):
            error = self.refused(f'/v1/workbooks/{self.book}/export/ods/jobs', {'job_id': 'e', 'mode': mode}, 400, 'UNSUPPORTED')
            self.assertIn('/export/ods', error['message'])
        self.refused(f'/v1/workbooks/{self.book}/export/ods/jobs', {'job_id': 'e', 'scope': 'workbook'}, 400, 'INVALID_ARGUMENT')
        self.assertEqual(self.staging_names(), [])

    def test_an_export_job_id_names_one_format(self):
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        self.export('both', fmt='xlsx')
        self.refused(f'/v1/workbooks/{self.book}/export/ods/jobs', {'job_id': 'both'}, 409, 'JOB_CONFLICT')
        data, headers = self.download('both')
        self.assertEqual(headers['Content-Type'], XLSX_TYPE)
        self.assertEqual(headers['Content-Disposition'], 'attachment; filename="export.xlsx"')

    def test_capabilities_and_openapi_examples_execute(self):
        capabilities = self.call('/v1/capabilities', method='GET')
        self.assertEqual(capabilities['large_interchange_formats'], ['xlsx', 'ods'])
        spec = openapi(); schemas = spec['components']['schemas']
        route = spec['paths']['/v1/import/ods/jobs']['post']['requestBody']['content']['application/json']['schema']
        self.assertEqual(route, {'$ref': '#/components/schemas/OdsPreparationRequest'})
        self.assertEqual([item['$ref'].rsplit('/', 1)[-1] for item in schemas['OdsPreparationRequest']['oneOf']],
                         ['SpreadsheetPreparation', 'OdsUploadPreparation', 'OdsPathPreparation'])
        export = spec['paths']['/v1/workbooks/{workbook}/export/ods/jobs']['post']
        self.assertEqual(export['requestBody']['content']['application/json']['schema'], {'$ref': '#/components/schemas/OdsExportJobRequest'})
        download = spec['paths']['/v1/exports/{export}']['get']['responses']['200']
        self.assertEqual(set(download['content']), {XLSX_TYPE, ODS_TYPE})
        self.assertIn('attachment; filename="export.ods"', download['headers']['Content-Disposition']['schema']['enum'])
        upload = copy.deepcopy(schemas['OdsUploadPreparation']['example'])
        self.upload(upload['upload_id'], FIXTURE.read_bytes())
        local = copy.deepcopy(schemas['OdsPathPreparation']['example'])
        local['source_path'] = str(FIXTURE)
        for request in (upload, local):
            if OAS30Validator is not None:
                OAS30Validator({**schemas['OdsPreparationRequest'], 'components': spec['components']}).validate(request)
            self.call('/v1/import/ods/jobs', request)
            self.completed(request['job_id'])
            self.call(f"/v1/jobs/{request['job_id']}/release")
        self.edit([{'row': 1, 'col': 1, 'source': 'x'}])
        request = copy.deepcopy(schemas['OdsExportJobRequest']['example'])
        self.call(f'/v1/workbooks/{self.book}/export/ods/jobs', request)
        result = self.completed(request['job_id'])
        self.assertEqual(set(result), set(schemas['XlsxExportJobResult']['required']))
        if OAS30Validator is not None:
            for name, value in (('OdsExportJobRequest', request), ('XlsxExportJobResult', result)):
                OAS30Validator({**schemas[name], 'components': spec['components']}).validate(value)
        data, headers = self.download(request['job_id'])
        self.assertEqual(headers['Content-Type'], ODS_TYPE)
        self.assertEqual(self.call(f"/v1/exports/{request['job_id']}/release"), {'released': True, 'export_id': request['job_id']})


if __name__ == '__main__':
    unittest.main()
