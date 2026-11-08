"""Optional independent openpyxl/odfpy producers and consumers; no target recalc claim."""
import base64
from contextlib import closing
from datetime import date,datetime,time
import io
from pathlib import Path
import tempfile
from unittest.mock import patch

from openpyxl import Workbook as ExcelWorkbook,load_workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904,CALENDAR_WINDOWS_1900
from odf.opendocument import OpenDocumentSpreadsheet,load
from odf.table import Table,TableRow,TableCell

from datahorizon.engine import Workbook
from datahorizon.interchange import xlsx_export,xlsx_preview
from datahorizon.ods import ods_export,ods_preview


def request(data):return dict(file_base64=base64.b64encode(data).decode())


with tempfile.TemporaryDirectory() as temp:
    for epoch in (CALENDAR_WINDOWS_1900,CALENDAR_MAC_1904):
        for iso in (False,True):
            producer=ExcelWorkbook(iso_dates=iso);producer.epoch=epoch
            for r,value in enumerate((date(2024,2,29),time(12,34,56,123000),datetime(2024,2,29,12,34,56,123000),"2024-02-29"),1):producer.active.cell(r,1,value)
            output=io.BytesIO();producer.save(output)
            reviewed=xlsx_preview(request(output.getvalue()));assert reviewed['can_import'],reviewed
            assert [c['kind'] for c in reviewed['cells']]==['date','time','datetime','text'],reviewed
            assert reviewed['cells'][2]['source']=='2024-02-29T12:34:56.123',reviewed
            print('openpyxl producer:',epoch.year,'ISO' if iso else 'serial','native temporal types retained')
    producer=OpenDocumentSpreadsheet();table=Table(name='Dates');producer.spreadsheet.addElement(table)
    for kind,attribute,value in (('date','datevalue','2024-02-29'),('time','timevalue','PT12H34M56.123456S'),('date','datevalue','2024-02-29T12:34:56.123456')):
        row=TableRow();row.addElement(TableCell(valuetype=kind,**{attribute:value}));table.addElement(row)
    output=io.BytesIO();producer.write(output)
    reviewed=ods_preview(request(output.getvalue()));assert reviewed['can_import'],reviewed
    assert [c['kind'] for c in reviewed['cells']]==['date','time','datetime']
    print('odfpy producer: ISO dates and microsecond clock durations retained')

    with closing(Workbook(Path(temp)/'temporal.dhwb',create=True)) as book:
        edits=[dict(row=1,col=1,source='2024-02-29',kind='date'),dict(row=2,col=1,source='12:34:56.123456',kind='time'),dict(row=3,col=1,source='2024-02-29T12:34:56.123456',kind='datetime')]
        formulas=['=DATE(2024,2,29)','=YEAR(A1)','=MONTH(A1)','=DAY(A1)','=TIME(12,34,56)','=HOUR(A2)','=MINUTE(A2)','=SECOND(A2)','=TODAY()','=NOW()']
        edits += [dict(row=r,col=2,source=formula) for r,formula in enumerate(formulas,1)]
        book.mutate(dict(action='edit',operation_id='seed',expected_state=book.state(),cells=edits))
        with patch('datahorizon.formula.utc_clock',return_value=datetime(2024,2,29,12,34,56,123456)):snapshot=book.read('Sheet1',dict(r2=10,c2=2))
        for mode in ('formulas','values'):
            exported=xlsx_export(snapshot,dict(mode=mode));consumer=load_workbook(io.BytesIO(base64.b64decode(exported['file_base64'])),data_only=False)
            assert consumer.active['A1'].value==date(2024,2,29)
            # openpyxl's ISO parser retains milliseconds; native XML retains six digits.
            assert consumer.active['A2'].value==time(12,34,56,123000)
            assert consumer.active['A3'].value==datetime(2024,2,29,12,34,56,123000)
            if mode=='formulas':
                assert [consumer.active.cell(r,2).value for r in range(1,11)]==formulas
            else:
                assert consumer.active['B1'].value==date(2024,2,29)
                assert consumer.active['B5'].value==time(12,34,56)
            consumer.close()
            exported=ods_export(snapshot,dict(mode=mode));path=Path(temp)/(mode+'.ods');path.write_bytes(base64.b64decode(exported['file_base64']))
            consumer=load(str(path));rows=consumer.spreadsheet.getElementsByType(Table)[0].getElementsByType(TableRow)
            assert rows[0].getElementsByType(TableCell)[0].getAttribute('datevalue')=='2024-02-29'
            assert rows[1].getElementsByType(TableCell)[0].getAttribute('timevalue')=='PT12H34M56.123456S'
            assert rows[2].getElementsByType(TableCell)[0].getAttribute('datevalue')=='2024-02-29T12:34:56.123456'
            if mode=='formulas':assert all(rows[r-1].getElementsByType(TableCell)[1].getAttribute('formula') for r in range(1,11))
            print('Independent XLSX/ODS consumers:',mode,'accepted all ten function sources/typed results; native subsecond precision verified separately; no target recalculation claim')
