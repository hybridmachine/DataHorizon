"""Regenerate the GUI fixture with the build-only independent openpyxl writer."""
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
book=Workbook(); sheet=book.active
sheet['A1']=7; sheet['B1']='=A1*2'; sheet['C1']='00123'
cell=sheet['B1']; cell.number_format='0.00'
cell.font=Font(bold=True,italic=True)
cell.fill=PatternFill('solid',fgColor='FF12AB34')
cell.alignment=Alignment(horizontal='center',wrap_text=True)
side=Side(style='thin',color='FF000000')
cell.border=Border(left=side,right=side,top=side,bottom=side)
book.save(Path(__file__).with_name('inputs.xlsx'))
