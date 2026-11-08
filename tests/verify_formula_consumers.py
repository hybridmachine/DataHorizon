"""Optional independent readers verify formula spelling and typed values exports."""
import base64
from contextlib import closing
from pathlib import Path
import tempfile
from datahorizon.engine import Workbook
from datahorizon.interchange import xlsx_export
from datahorizon.ods import ods_export
from openpyxl import load_workbook
from odf.opendocument import load
from odf.table import Table,TableRow,TableCell
from odf import teletype

CASES=[('=LEN(TRIM(A1))',5),('=TRIM(A1)','00123'),('=UPPER("a")','A'),('=LOWER("B")','b'),('=LEFT(TRIM(A1),2)','00'),('=RIGHT(TRIM(A1),2)','23'),('=MID(TRIM(A1),2,3)','012')]
with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'formulas.dhwb',create=True)) as book:
    book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[{'row':1,'col':1,'source':' 00123  ','kind':'text'},*[{'row':r,'col':2,'source':formula} for r,(formula,result) in enumerate(CASES,1)]]})
    snapshot=book.read('Sheet1',{'r2':len(CASES),'c2':2})
    for format,export in (('xlsx',xlsx_export),('ods',ods_export)):
        for mode in ('formulas','values'):
            result=export(snapshot,{'mode':mode});path=Path(temp)/(mode+'.'+format);path.write_bytes(base64.b64decode(result['file_base64']))
            if format=='xlsx':
                wb=load_workbook(path,data_only=False);sheet=wb.active
                for r,(formula,value) in enumerate(CASES,1):assert sheet.cell(r,2).value==(formula if mode=='formulas' else value)
                if mode=='values':assert sheet['B2'].data_type=='s'
                wb.close()
            else:
                doc=load(str(path));rows=doc.spreadsheet.getElementsByType(Table)[0].getElementsByType(TableRow)
                for row,(formula,value) in zip(rows,CASES):
                    cell=row.getElementsByType(TableCell)[1]
                    if mode=='formulas':assert cell.getAttribute('formula')
                    elif type(value) is str:assert cell.getAttribute('valuetype')=='string' and teletype.extractText(cell)==value
                    else:assert float(cell.getAttribute('value'))==value
            print(format+' '+mode+': independent reader accepted '+str(len(CASES))+' formulas/typed values; no target recalculation claim')
