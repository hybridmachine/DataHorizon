# SPDX-License-Identifier: GPL-3.0-only
"""Optional independent consumer verification for filtered values packages."""
import base64
from contextlib import closing
from pathlib import Path
import tempfile
from datahorizon.engine import Workbook
from datahorizon.list_export import export_list
from openpyxl import load_workbook
from odf.opendocument import load
from odf.table import Table,TableRow,TableCell
from odf import teletype

with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'consumer.dhwb',create=True)) as book:
    book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[
        {'row':1,'col':1,'source':'ID'},{'row':1,'col':2,'source':'Value'},
        {'row':2,'col':1,'source':'00123','kind':'text'},{'row':2,'col':2,'source':'=1/4','style':{'format':'percent2'}},
        {'row':3,'col':1,'source':'Z'},{'row':3,'col':2,'source':'TRUE'}]})
    args={'r2':3,'c2':2,'header':True,'sort':[{'col':1,'descending':True}],'expected_state':book.state()}
    for format in ('xlsx','ods'):
        path=Path(temp)/('list.'+format);path.write_bytes(base64.b64decode(export_list(book,format,args)['file_base64']))
        if format=='xlsx':
            wb=load_workbook(path,data_only=False);sheet=wb.active
            assert list(sheet.values)==[('ID','Value'),('Z',True),('00123',.25)]
            assert sheet['A3'].data_type=='s' and sheet['B3'].number_format=='0.00%'
            assert not any(cell.data_type=='f' for row in sheet for cell in row)
            wb.close()
        else:
            doc=load(str(path));rows=doc.spreadsheet.getElementsByType(Table)[0].getElementsByType(TableRow)
            cells=[row.getElementsByType(TableCell) for row in rows]
            assert [teletype.extractText(row[0]) for row in cells]==['ID','Z','00123']
            assert cells[1][1].getAttribute('booleanvalue')=='true'
            assert float(cells[2][1].getAttribute('value'))==.25
            assert not any(cell.getAttribute('formula') for row in cells for cell in row)
        print(format+': independent consumer accepted order, Text identifier, Boolean, raw percentage value, and absence of formulas')

# Independent readers also inspect compressed formula references and typed dates.
from datetime import date
from datahorizon.record_operations import export as export_records
with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'records.dhwb',create=True)) as book:
    book.mutate(dict(action='edit',operation_id='seed',expected_state=book.state(),cells=[
        dict(row=1,col=1,source='Date',kind='text'),dict(row=1,col=2,source='Day',kind='text'),
        dict(row=2,col=1,source='2024-02-29',kind='date'),dict(row=2,col=2,source='=DAY(A2)'),
        dict(row=3,col=1,source='excluded',kind='text'),dict(row=3,col=2,source='0'),
        dict(row=4,col=1,source='2024-03-31',kind='date'),dict(row=4,col=2,source='=DAY(A4)')]))
    book.mutate(dict(action='list_declare',operation_id='declare',expected_state=book.state(),id='Dates',spec=dict(r2=4,c2=2,header=True)))
    request=dict(id='Dates',expected_state=book.state(),filters=[dict(col=2,op='>',value=1)])
    for format in ('xlsx','ods'):
        path=Path(temp)/('records.'+format);path.write_bytes(base64.b64decode(export_records(book,format,request)['file_base64']))
        if format=='xlsx':
            consumer=load_workbook(path,data_only=False);sheet=consumer.active
            assert sheet['A2'].value==date(2024,2,29) and sheet['A3'].value==date(2024,3,31)
            assert sheet['B2'].value=='=DAY(A2)' and sheet['B3'].value=='=DAY(A3)';consumer.close()
            consumer=load_workbook(path,data_only=True);assert consumer.active['B3'].value is None;consumer.close()
        else:
            consumer=load(str(path));rows=consumer.spreadsheet.getElementsByType(Table)[0].getElementsByType(TableRow);cells=[row.getElementsByType(TableCell) for row in rows]
            assert len(rows)==3 and cells[2][0].getAttribute('datevalue')=='2024-03-31'
            assert cells[2][1].getAttribute('formula')=='of:=DAY([.A3])' and float(cells[2][1].getAttribute('value'))==31
        frozen=Path(temp)/('records-values.'+format);frozen.write_bytes(base64.b64decode(export_records(book,format,{**request,'mode':'values'})['file_base64']))
        if format=='xlsx':
            consumer=load_workbook(frozen,data_only=True);assert consumer.active['B3'].value==31;consumer.close()
        else:
            consumer=load(str(frozen));rows=consumer.spreadsheet.getElementsByType(Table)[0].getElementsByType(TableRow);assert float(rows[2].getElementsByType(TableCell)[1].getAttribute('value'))==31
        print(format+': independent reader accepted typed dates, compressed formula referents and frozen values; XLSX formula caches intentionally omitted')
