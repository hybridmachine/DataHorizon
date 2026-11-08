"""Optional independent readers verify formula spelling and typed values exports."""
import base64
from contextlib import closing
from pathlib import Path
import tempfile
from datahorizon.engine import Workbook
from datahorizon.interchange import xlsx_export,native_formula_to_xlsx
from datahorizon.ods import ods_export
from openpyxl import load_workbook
from odf.opendocument import load
from odf.table import Table,TableRow,TableCell
from odf import teletype

CASES=[('=LEN(TRIM(A1))',5),('=TRIM(A1)','00123'),('=UPPER("a")','A'),('=LOWER("B")','b'),('=LEFT(TRIM(A1),2)','00'),('=RIGHT(TRIM(A1),2)','23'),('=MID(TRIM(A1),2,3)','012'),('=A1&"X"',' 00123  X'),('=CONCAT(TRIM(A1),TRUE,0)','00123TRUE0'),('=COUNTIF(A1,"*00123*")',1),('=SUMIF(C1:C2,">0")',5),('=XLOOKUP(2,C1:C2,A1:A2)',' 00123  '),('=XLOOKUP(9,C1:C2,A1:A2,"missing")','missing')]
CASES += [('=ISNUMBER(A1)',False),('=ISTEXT(A1)',True),('=ISERROR(1/0)',True)]
CASES += [('=IFNA(XLOOKUP(9,C1:C2,A1:A2),"missing")',"missing")]
with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'formulas.dhwb',create=True)) as book:
    book.mutate({'action':'edit','operation_id':'seed','expected_state':book.state(),'cells':[{'row':1,'col':1,'source':' 00123  ','kind':'text'},{'row':1,'col':3,'source':'2'},{'row':2,'col':3,'source':'3'},*[{'row':r,'col':2,'source':formula} for r,(formula,result) in enumerate(CASES,1)]]})
    snapshot=book.read('Sheet1',{'r2':len(CASES),'c2':3})
    for format,export in (('xlsx',xlsx_export),('ods',ods_export)):
        for mode in ('formulas','values'):
            result=export(snapshot,{'mode':mode});path=Path(temp)/(mode+'.'+format);path.write_bytes(base64.b64decode(result['file_base64']))
            if format=='xlsx':
                wb=load_workbook(path,data_only=False);sheet=wb.active
                for r,(formula,value) in enumerate(CASES,1):assert sheet.cell(r,2).value==(native_formula_to_xlsx(formula) if mode=='formulas' else value)
                if mode=='values':assert sheet['B2'].data_type=='s'
                wb.close()
            else:
                doc=load(str(path));rows=doc.spreadsheet.getElementsByType(Table)[0].getElementsByType(TableRow)
                for row,(formula,value) in zip(rows,CASES):
                    cell=row.getElementsByType(TableCell)[1]
                    if mode=='formulas':assert cell.getAttribute('formula')
                    elif type(value) is str:assert cell.getAttribute('valuetype')=='string' and teletype.extractText(cell)==value
                    elif type(value) is bool:assert cell.getAttribute('valuetype')=='boolean' and cell.getAttribute('booleanvalue')==str(value).lower()
                    else:assert float(cell.getAttribute('value'))==value
            print(format+' '+mode+': independent reader accepted '+str(len(CASES))+' formulas/typed values; no target recalculation claim')
