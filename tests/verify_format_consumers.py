"""Optional independent package readers check the portable formatting tier."""
import base64
from io import BytesIO
from pathlib import Path
import tempfile
from openpyxl import load_workbook
from odf.opendocument import load
from odf.table import TableCell
from odf.number import NumberStyle,PercentageStyle,CurrencyStyle,CurrencySymbol,Number
from datahorizon.formats import number_format_code
from datahorizon.interchange import xlsx_export
from datahorizon.ods import ods_export

styles=[{'format':mode,'decimals':places} for mode in ('number','percent') for places in (0,1,2,4,15)]
styles += [{**s,'grouping':True} for s in styles]
styles += [{'format':'currency','currency':code,'decimals':n,'grouping':g} for code in ('USD','EUR','JPY') for n in (0,2,4) for g in (False,True)]
snapshot={'state':'s','range':[1,1,len(styles),1],'cells':[{'row':r,'col':1,'source':'0.125','kind':'number','value':.125,'calculation':'current','style':style} for r,style in enumerate(styles,1)]}
with tempfile.TemporaryDirectory() as temp:
    wb=load_workbook(BytesIO(base64.b64decode(xlsx_export(snapshot,{})['file_base64'])))
    for r,style in enumerate(styles,1):
        cell=wb.active.cell(r,1);assert cell.value==.125 and cell.data_type=='n';assert cell.number_format==number_format_code(style)
    wb.close()
    path=Path(temp)/'styles.ods';path.write_bytes(base64.b64decode(ods_export(snapshot,{})['file_base64']));doc=load(str(path))
    formats={f.getAttribute('name'):f for kind in (NumberStyle,PercentageStyle,CurrencyStyle) for f in doc.automaticstyles.getElementsByType(kind)}
    from odf.style import Style
    mapped={s.getAttribute('name'):s.getAttribute('datastylename') for s in doc.automaticstyles.getElementsByType(Style)}
    for cell,style in zip(doc.spreadsheet.getElementsByType(TableCell),styles):
        assert float(cell.getAttribute('value'))==.125;assert cell.getAttribute('valuetype')=={'percent':'percentage','currency':'currency'}.get(style['format'],'float')
        if style['format']=='currency':assert cell.getAttribute('currency')==style['currency']
        fmt=formats[mapped[cell.getAttribute('stylename')]];assert int(fmt.getElementsByType(Number)[0].getAttribute('decimalplaces'))==style['decimals'];assert (fmt.getElementsByType(Number)[0].getAttribute('grouping')=='true')==style.get('grouping',False)
        if style['format']=='currency':assert str(fmt.getElementsByType(CurrencySymbol)[0].firstChild)==style['currency']
    print('openpyxl and odfpy accepted',len(styles),'numeric style cases; typed values and precision retained')
