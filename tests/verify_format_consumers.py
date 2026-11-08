"""Optional independent package readers check the portable formatting tier."""
import base64
from io import BytesIO
from pathlib import Path
import tempfile
from openpyxl import load_workbook
from odf.opendocument import load
from odf.table import TableCell
from odf.number import NumberStyle,PercentageStyle,Number
from datahorizon.formats import number_format_code
from datahorizon.interchange import xlsx_export
from datahorizon.ods import ods_export

styles=[{'format':mode,'decimals':places} for mode in ('number','percent') for places in (0,1,2,4,15)]
snapshot={'state':'s','range':[1,1,len(styles),1],'cells':[{'row':r,'col':1,'source':'0.125','kind':'number','value':.125,'calculation':'current','style':style} for r,style in enumerate(styles,1)]}
with tempfile.TemporaryDirectory() as temp:
    wb=load_workbook(BytesIO(base64.b64decode(xlsx_export(snapshot,{})['file_base64'])))
    for r,style in enumerate(styles,1):
        cell=wb.active.cell(r,1);assert cell.value==.125 and cell.data_type=='n';assert cell.number_format==number_format_code(style)
    wb.close()
    path=Path(temp)/'styles.ods';path.write_bytes(base64.b64decode(ods_export(snapshot,{})['file_base64']));doc=load(str(path))
    formats={f.getAttribute('name'):f for kind in (NumberStyle,PercentageStyle) for f in doc.automaticstyles.getElementsByType(kind)}
    from odf.style import Style
    mapped={s.getAttribute('name'):s.getAttribute('datastylename') for s in doc.automaticstyles.getElementsByType(Style)}
    for cell,style in zip(doc.spreadsheet.getElementsByType(TableCell),styles):
        assert float(cell.getAttribute('value'))==.125;assert cell.getAttribute('valuetype')==('percentage' if style['format']=='percent' else 'float')
        fmt=formats[mapped[cell.getAttribute('stylename')]];assert int(fmt.getElementsByType(Number)[0].getAttribute('decimalplaces'))==style['decimals']
    print('openpyxl and odfpy accepted',len(styles),'numeric style cases; typed values and precision retained')
