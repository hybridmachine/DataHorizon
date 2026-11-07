"""Build-only openpyxl/odfpy corpus; these are not native Excel/LibreOffice files."""
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from odf.opendocument import OpenDocumentSpreadsheet
from odf.table import Table, TableRow, TableCell
from odf.text import P
from odf.style import Style, TableCellProperties, TextProperties, ParagraphProperties
from odf.number import NumberStyle, Number
root=Path(__file__).parent
book=Workbook(); sheet=book.active; sheet.title='Inputs'
side=Side(style='thin',color='FF000000')
for row in range(1,17):
    sheet.cell(row,1,7); c=sheet.cell(row,2,f'=$A{row}*2'); sheet.cell(row,3,'00123')
    c.font=Font(bold=True,italic=True); c.fill=PatternFill('solid',fgColor='FF12AB34')
    c.alignment=Alignment(horizontal='center',wrap_text=True)
    c.border=Border(left=side,right=side,top=side,bottom=side); c.number_format='0.00'
book.create_sheet('Summary')['A1']='=SUM(Inputs!B1:B16)'; book.create_sheet('Empty')
book.save(root/'fidelity.xlsx')
book=OpenDocumentSpreadsheet()
number=NumberStyle(name='N2'); number.addElement(Number(decimalplaces='2',minintegerdigits='1')); book.automaticstyles.addElement(number)
parent=Style(name='Parent',family='table-cell',datastylename=number)
parent.addElement(TextProperties(fontweight='bold',fontstyle='italic'))
parent.addElement(TableCellProperties(backgroundcolor='#12ab34',wrapoption='wrap',border='0.75pt solid #000000'))
book.styles.addElement(parent)
child=Style(name='Child',family='table-cell',parentstylename=parent)
child.addElement(ParagraphProperties(textalign='center')); book.automaticstyles.addElement(child)
table=Table(name='Inputs')
for i in range(1,17):
    row=TableRow(); row.addElement(TableCell(valuetype='float',value='7'))
    row.addElement(TableCell(valuetype='float',value='14',formula=f'of:=[.$A{i}]*2',stylename=child))
    text=TableCell(valuetype='string',stringvalue='00123'); text.addElement(P(text='00123')); row.addElement(text); table.addElement(row)
book.spreadsheet.addElement(table)
table=Table(name='Summary'); row=TableRow(); row.addElement(TableCell(valuetype='float',value='224',formula='of:=SUM([$Inputs.B1:$Inputs.B16])')); table.addElement(row); book.spreadsheet.addElement(table)
book.spreadsheet.addElement(Table(name='Empty')); book.save(str(root/'fidelity.ods'))
(root/'fidelity-provenance.json').write_text(json.dumps({'producers':{name:version(name) for name in ('openpyxl','odfpy')},'scope':'Build-only producer fixtures; no native application acceptance claim','sha256':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ('fidelity.xlsx','fidelity.ods')}},indent=2)+'\n')
