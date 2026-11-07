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
