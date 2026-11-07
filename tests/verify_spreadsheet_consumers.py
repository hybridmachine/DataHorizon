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
