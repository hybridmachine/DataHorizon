"""Build-only independent producers/consumers for the percent2 interchange tier."""
import base64
import io
from openpyxl import Workbook,load_workbook
from odf.opendocument import OpenDocumentSpreadsheet,load
from odf.table import Table,TableRow,TableCell
from odf.style import Style
from odf.number import PercentageStyle,Number,Text
from datahorizon.interchange import xlsx_preview,xlsx_export
from datahorizon.ods import ods_preview,ods_export

snapshot={'state':'s','range':[1,1,1,1],'cells':[{'row':1,'col':1,'source':'0.125','kind':'number','value':0.125,'calculation':'current','style':{'format':'percent2'}}]}
book=Workbook();book.active['A1']=0.125;book.active['A1'].number_format='0.00%'
output=io.BytesIO();book.save(output)
reviewed=xlsx_preview({'file_base64':base64.b64encode(output.getvalue()).decode()})
assert reviewed['can_import'] and reviewed['cells'][0]['style']['format']=='percent2',reviewed
assert float(reviewed['cells'][0]['source'])==0.125
external=load_workbook(io.BytesIO(base64.b64decode(xlsx_export(snapshot,{})['file_base64'])))
assert external.active['A1'].number_format=='0.00%' and external.active['A1'].value==0.125
print('openpyxl percentage producer/consumer: style retained, numeric fraction unscaled.')

book=OpenDocumentSpreadsheet();percentage=PercentageStyle(name='P2')
percentage.addElement(Number(decimalplaces='2',minintegerdigits='1'));percentage.addElement(Text(text='%'));book.automaticstyles.addElement(percentage)
style=Style(name='Pct',family='table-cell',datastylename=percentage);book.automaticstyles.addElement(style)
table=Table(name='Inputs');row=TableRow();row.addElement(TableCell(valuetype='percentage',value='0.125',stylename=style));table.addElement(row);book.spreadsheet.addElement(table)
output=io.BytesIO();book.save(output)
reviewed=ods_preview({'file_base64':base64.b64encode(output.getvalue()).decode()})
assert reviewed['can_import'] and reviewed['cells'][0]['style']['format']=='percent2',reviewed
assert float(reviewed['cells'][0]['source'])==0.125
external=load(io.BytesIO(base64.b64decode(ods_export(snapshot,{})['file_base64'])))
cell=external.spreadsheet.getElementsByType(TableCell)[0]
assert cell.getAttribute('valuetype')=='percentage' and float(cell.getAttribute('value'))==0.125
assert external.automaticstyles.getElementsByType(PercentageStyle)
print('odfpy percentage producer/consumer: style retained, numeric fraction unscaled.')
