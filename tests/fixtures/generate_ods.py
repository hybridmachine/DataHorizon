"""Regenerate the GUI fixture using build-only odfpy, independently of the service."""
from pathlib import Path
from odf.opendocument import OpenDocumentSpreadsheet
from odf.table import Table, TableRow, TableCell
from odf.text import P
from odf.style import Style, TableCellProperties, TextProperties, ParagraphProperties
from odf.number import NumberStyle, Number
book=OpenDocumentSpreadsheet()
number=NumberStyle(name='N2');number.addElement(Number(decimalplaces='2',minintegerdigits='1'));book.automaticstyles.addElement(number)
style=Style(name='Native',family='table-cell',datastylename=number)
style.addElement(TableCellProperties(backgroundcolor='#12ab34',wrapoption='wrap',border='0.75pt solid #000000'))
style.addElement(TextProperties(fontweight='bold',fontstyle='italic'))
style.addElement(ParagraphProperties(textalign='center'))
book.automaticstyles.addElement(style)
table=Table(name='Inputs'); row=TableRow()
row.addElement(TableCell(valuetype='float',value='7'))
row.addElement(TableCell(valuetype='float',value='14',formula='of:=[.A1]*2',stylename=style))
cell=TableCell(valuetype='string',stringvalue='00123');cell.addElement(P(text='00123'));row.addElement(cell)
table.addElement(row);book.spreadsheet.addElement(table)
book.save(str(Path(__file__).with_name('inputs.ods')))
