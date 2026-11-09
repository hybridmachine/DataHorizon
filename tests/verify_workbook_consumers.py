# SPDX-License-Identifier: GPL-3.0-only
"""Build-only independent XLSX/ODS workbook consumers, including values snapshots."""
import argparse
import base64
from contextlib import closing
from importlib.metadata import version
import io
import json
from pathlib import Path
import tempfile
from openpyxl import load_workbook
from odf.opendocument import load
from odf.table import Table,TableCell
from datahorizon.engine import Workbook
from datahorizon.interchange import xlsx_preview,xlsx_export
from datahorizon.ods import ods_preview,ods_export


def verify():
    checks=[]
    for format,parse,export in (("xlsx",xlsx_preview,xlsx_export),("ods",ods_preview,ods_export)):
        source=Path(__file__).parent/'fixtures'/('fidelity.'+format)
        reviewed=parse({'file_base64':base64.b64encode(source.read_bytes()).decode(),'scope':'workbook'})
        assert reviewed['can_import'],reviewed
        with tempfile.TemporaryDirectory() as temp,closing(Workbook(Path(temp)/'book.dhwb',create=True)) as book:
            book.mutate({'action':'workbook_import','sheets':reviewed['workbook_sheets'],'operation_id':'import','expected_state':book.state()})
            snapshot=book.read_workbook()
            for mode in ('formulas','values'):
                data=base64.b64decode(export(snapshot,{'mode':mode})['file_base64'])
                if format=='xlsx':
                    target=load_workbook(io.BytesIO(data),data_only=False)
                    assert target.sheetnames==['Inputs','Summary','Empty']
                    for row in range(1,17):
                        cell=target['Inputs'].cell(row,2)
                        assert (cell.data_type=='f')==(mode=='formulas')
                        if mode=='values': assert cell.value==14
                        assert cell.number_format=='0.00' and cell.font.bold and cell.font.italic
                        assert cell.alignment.wrap_text and cell.alignment.horizontal=='center'
                        assert cell.border.bottom.style=='thin' and cell.fill.fgColor.rgb=='FF12AB34'
                    if mode=='values': assert target['Summary']['A1'].value==224
                else:
                    target=load(io.BytesIO(data)); tables=target.spreadsheet.getElementsByType(Table)
                    assert [t.getAttribute('name') for t in tables]==['Inputs','Summary','Empty']
                    cells=tables[0].getElementsByType(TableCell)
                    for row in range(16):
                        cell=cells[row*3+1]
                        assert bool(cell.getAttribute('formula'))==(mode=='formulas')
                        assert float(cell.getAttribute('value'))==14
                        assert cell.getAttribute('stylename')
                    assert float(tables[1].getElementsByType(TableCell)[0].getAttribute('value'))==224
                checks.append({'format':format,'mode':mode,'sheets':3,'input_rows':16,'summary':224,'passed':True})
    return {'producers_consumers':{n:version(n) for n in ('openpyxl','odfpy')},'checks':checks,
            'native_excel_libreoffice_acceptance':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--output',type=Path)
    args=parser.parse_args(); result=verify(); text=json.dumps(result,indent=2)+'\n'
    if args.output: args.output.write_text(text)
    print(text,end='')
