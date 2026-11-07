"""Optional manual verification with independent file producers/consumers.

Run with PYTHONPATH=service build/interchange-venv/bin/python
 tests/verify_spreadsheet_consumers.py. These libraries are build-only tools.
"""
import base64
import io
from openpyxl import load_workbook, Workbook
from datahorizon.interchange import xlsx_export,xlsx_preview

source=Workbook(); sheet=source.active
sheet['A1']='00123';sheet['B1']=7;sheet['C1']='=B1*2';sheet['D1']=True
out=io.BytesIO();source.save(out)
p=xlsx_preview({'file_base64':base64.b64encode(out.getvalue()).decode()})
assert p['can_import'],p
snapshot={'cells':[dict(row=1,col=1,source='00123',kind='text',value='00123',calculation='current',style={}),
                   dict(row=1,col=2,source='7',kind='number',value=7,calculation='current',style={}),
                   dict(row=1,col=3,source='=B1*2',kind='formula',value=14,calculation='current',style={})],
          'range':[1,1,1,3],'state':'independent'}
result=xlsx_export(snapshot,{})
book=load_workbook(io.BytesIO(base64.b64decode(result['file_base64'])))
assert [book.active.cell(1,c).value for c in (1,2,3)]==['00123',7,'=B1*2']
print('Independent openpyxl producer/consumer: Text, numbers, formulas passed.')

from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
sheet['B1'].font=Font(bold=True,italic=True)
sheet['B1'].fill=PatternFill('solid',fgColor='FF12AB34')
sheet['B1'].alignment=Alignment(horizontal='center',wrap_text=True)
side=Side(style='thin',color='FF000000')
sheet['B1'].border=Border(left=side,right=side,top=side,bottom=side)
sheet['B1'].number_format='0.00'
out=io.BytesIO();source.save(out)
reviewed=xlsx_preview({'file_base64':base64.b64encode(out.getvalue()).decode()})
expected={'format':'fixed2','bold':True,'italic':True,'background':'#12ab34','align':'center','wrap':True,'border':'box'}
assert reviewed['can_import'] and reviewed['cells'][1]['style']==expected,reviewed
snapshot['cells'][1]['style']=expected
exported=xlsx_export(snapshot,{})
parsed=load_workbook(io.BytesIO(base64.b64decode(exported['file_base64'])))
cell=parsed.active['B1']
assert cell.font.bold and cell.font.italic and cell.fill.fgColor.rgb=='FF12AB34'
assert cell.alignment.horizontal=='center' and cell.alignment.wrap_text
assert cell.number_format=='0.00' and cell.border.bottom.style=='thin'
print('Independent openpyxl producer/consumer: all native style properties passed.')
