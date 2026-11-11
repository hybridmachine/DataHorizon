# SPDX-License-Identifier: GPL-3.0-only
"""Independent date fixtures plus managed whole-workbook temporal round trips."""
import base64
from contextlib import closing
from datetime import datetime
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from datahorizon.engine import Workbook
from datahorizon.interchange import P,R,S,xlsx_export,xlsx_preview
from datahorizon.ods import ods_export,ods_preview
from test_interchange import fixture as xlsx_fixture
from test_ods import fixture as ods_fixture


def serial_package(value,epoch="0",fmt="14"):
    data=xlsx_fixture(f'<c r="A1" s="0"><v>{value}</v></c>')
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as original,zipfile.ZipFile(output,"w") as archive:
        for name in original.namelist():
            content=original.read(name)
            if name=="xl/workbook.xml":content=content.replace(b"<sheets>",f'<workbookPr date1904="{epoch}"/><sheets>'.encode())
            archive.writestr(name,content)
        archive.writestr("xl/styles.xml",f'<styleSheet xmlns="{S}"><fonts><font/></fonts><fills><fill/></fills><borders><border/></borders><cellXfs><xf numFmtId="{fmt}"/></cellXfs></styleSheet>')
    return dict(file_base64=base64.b64encode(output.getvalue()).decode())


class TemporalInterchangeTests(unittest.TestCase):
    def test_ods_conversion_reports_match_native_temporal_and_currency_results(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'report.dhwb',create=True)) as book:
            inputs=[dict(row=1,col=1,source='2024-02-29',kind='date'),dict(row=2,col=1,source='12:34:56',kind='time'),dict(row=3,col=1,source='2024-02-29T12:34:56',kind='datetime'),dict(row=4,col=1,source='25.75',kind='number',style=dict(format='currency',currency='USD',decimals=4,grouping=True))]
            book.mutate(dict(action='edit',operation_id='report',expected_state=book.state(),cells=inputs))
            package=ods_export(book.read_workbook(),dict(mode='values'))
            for scope in ('selection','workbook'):
                result=ods_preview({**package,'scope':scope});self.assertTrue(result['can_import'],result)
                cells=result['cells'] if scope=='selection' else result['workbook_sheets'][0]['cells']
                self.assertEqual([c['kind'] for c in cells],['date','time','datetime','number'])
                self.assertEqual(cells[-1]['style'],dict(format='currency',currency='USD',decimals=4,grouping=True))
                report=' '.join(result['report']);self.assertIn('native ISO types',report);self.assertIn('three-letter currency',report);self.assertIn('0–15',report);self.assertNotIn('ISO Text',report)
        invalid=ods_fixture('<table:table-row><table:table-cell office:value-type="time" office:time-value="PT24H"/></table:table-row>')
        result=ods_preview(dict(file_base64=base64.b64encode(invalid).decode()));self.assertFalse(result['can_import']);self.assertEqual(result['issues'],[]);self.assertEqual(result['report_entries'][0]['category'],'cell.invalid_value')
    def test_xlsx_serial_epochs_hidden_clocks_and_fake_leap_day(self):
        for serial,epoch,expected in ((1,"0","1900-01-01"),(59,"0","1900-02-28"),(61,"0","1900-03-01"),(0,"1","1904-01-01"),(1.5,"1","1904-01-02T12:00:00")):
            result=xlsx_preview(serial_package(serial,epoch))
            self.assertTrue(result["can_import"],result)
            self.assertEqual(result["cells"][0]["source"],expected)
        for serial in (60,60.5):
            result=xlsx_preview(serial_package(serial))
            self.assertTrue(result["can_import"],result);self.assertEqual(result["cells"][0]["kind"],"number");self.assertNotIn("format",result["cells"][0]["style"])
            self.assertIn("cell.date_conversion",[e["category"] for e in result["report_entries"]])
        for serial in ("NaN","1e999"):
            result=xlsx_preview(serial_package(serial))
            self.assertEqual((result["cells"][0]["kind"],result["cells"][0]["source"]),("text",serial))
            self.assertIn("cell.number_out_of_range",[e["category"] for e in result["report_entries"]])
        self.assertEqual(xlsx_preview(serial_package(.5,fmt="21"))["cells"][0]["source"],"12:00:00")

    def test_independent_iso_and_duration_sources_reject_offsets_and_long_durations(self):
        for source,kind in (("2024-02-29","date"),("12:34:56.123456","time"),("2024-02-29T12:34:56.123456","datetime")):
            result=xlsx_preview(dict(file_base64=base64.b64encode(xlsx_fixture(f'<c r="A1" t="d"><v>{source}</v></c>')).decode()))
            self.assertTrue(result["can_import"],result);self.assertEqual(result["cells"][0]["kind"],kind)
        for source in ("2023-02-29","2024-02-29T00:00:00Z"):
            result=xlsx_preview(dict(file_base64=base64.b64encode(xlsx_fixture(f'<c r="A1" t="d"><v>{source}</v></c>')).decode()))
            self.assertFalse(result["can_import"])
            self.assertEqual([(e["category"],e["detail"],e["severity"]) for e in result["report_entries"]],[("cell.invalid_value","date/time","omitted")])
        for duration,expected in (("PT2H","02:00:00"),("P0DT12H34M56.123456S","12:34:56.123456")):
            data=ods_fixture(f'<table:table-row><table:table-cell office:value-type="time" office:time-value="{duration}"/></table:table-row>')
            result=ods_preview(dict(file_base64=base64.b64encode(data).decode()));self.assertTrue(result["can_import"],result);self.assertEqual(result["cells"][0]["source"],expected)
        for duration in ("PT24H","P1DT1H","-PT1H","P1M","PT0.0000001S"):
            data=ods_fixture(f'<table:table-row><table:table-cell office:value-type="time" office:time-value="{duration}"/></table:table-row>')
            self.assertFalse(ods_preview(dict(file_base64=base64.b64encode(data).decode()))["can_import"])

    def test_whole_workbook_types_styles_formulas_and_values_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/"source.dhwb",create=True)) as book:
            edits=[dict(row=1,col=1,source="2024-02-29",kind="date",style={"format":"date_dmy"}),dict(row=2,col=1,source="12:34:56.123456",kind="time"),dict(row=3,col=1,source="2024-02-29T12:34:56.123456",kind="datetime")]
            formulas=["=DATE(2024,2,29)","=YEAR(A1)","=MONTH(A1)","=DAY(A1)","=TIME(12,34,56)","=HOUR(A2)","=MINUTE(A2)","=SECOND(A2)","=TODAY()","=NOW()","=A1+1","=A1+TIME(12,0,0)"]
            edits += [dict(row=r,col=2,source=source) for r,source in enumerate(formulas,1)]
            book.mutate(dict(action="edit",operation_id="seed",expected_state=book.state(),cells=edits))
            clock=datetime(2024,2,29,12,34,56,123456)
            with patch("datahorizon.formula.utc_clock",return_value=clock):snapshot=book.read_workbook()
            expected={ (c["row"],c["col"]):c["value"] for c in snapshot["sheets"][0]["cells"]}
            for export,preview in ((xlsx_export,xlsx_preview),(ods_export,ods_preview)):
                for mode in ("formulas","values"):
                    package=export(snapshot,dict(mode=mode));reviewed=preview({**package,"scope":"workbook","mode":mode})
                    # XLSX formulas intentionally omit caches; values were frozen on export.
                    if mode=="values":reviewed=preview({**package,"scope":"workbook"})
                    self.assertTrue(reviewed["can_import"],reviewed)
                    with closing(Workbook(Path(temp)/(export.__name__+mode+".dhwb"),create=True)) as restored:
                        restored.mutate(dict(action="workbook_import",operation_id="import",expected_state=restored.state(),sheets=reviewed["workbook_sheets"]))
                        with patch("datahorizon.formula.utc_clock",return_value=clock):actual=restored.read_workbook()["sheets"][0]["cells"]
                        self.assertEqual({(c["row"],c["col"]):c["value"] for c in actual},expected)
                        self.assertEqual(actual[0]["value_kind"],"date");self.assertEqual(actual[0]["style"]["format"],"date_dmy")
                        self.assertEqual(restored.info()["format_version"],2)
