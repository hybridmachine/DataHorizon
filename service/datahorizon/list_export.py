# SPDX-License-Identifier: GPL-3.0-only
"""Values-only list exports reuse one evaluated projection snapshot."""
import csv
import io
from .engine import encoded,interchange_text,require
from .interchange import xlsx_export
from .ods import ods_export
from .csv_types import temporal_schema


def export_list(book,format,request):
    require(format in ('csv','xlsx','ods'),'INVALID_ARGUMENT','Unsupported list export format')
    require(request.get('mode','values')=='values','INVALID_ARGUMENT','List export supports calculated values only')
    values=book.list_values(request.get('sheet','Sheet1'),request)
    rows,columns=values['rows'],values['source_columns']
    require(bool(rows) and bool(columns),'NO_RESULTS','No visible list values to export')
    report=[f"Visible list values in displayed order, compressed to A1: {len(rows)} rows × {len(columns)} columns.",
            f"Explicit header follows the list criteria; {values['excluded_rows']} locally hidden rows and {values['excluded_columns']} columns excluded.",
            f"{values['formulas_as_values']} formula inputs converted to calculated values; no formulas are exported.",
            "Native workbook, Python code, queries, relationships, history and view preferences remain in the source."]
    if format=='csv':
        output=io.StringIO(newline='');writer=csv.writer(output)
        for row in rows:writer.writerow([interchange_text(cell.get('value')) for cell in row])
        result={'csv':output.getvalue(),'state':values['state'],'report':report+[
            'CSV uses canonical, unrounded values and omits styles/types. Blank, NULL and empty Text become empty fields.']}
        entries=[dict(row=r,col=c,kind=cell['value_kind']) for r,row in enumerate(rows,1) for c,cell in enumerate(row,1) if cell.get('value_kind') in ('date','time','datetime')]
        if entries:
            result['temporal_schema']=temporal_schema(result['csv'],entries)
            result['report'].append('Keep the temporal companion to restore native date/time kinds; CSV alone is untyped.')
    else:
        cells=[{**cell,'row':r,'col':c} for r,row in enumerate(rows,1) for c,cell in enumerate(row,1) if cell]
        snapshot={'cells':cells,'state':values['state'],'sheet':values['sheet'],'range':[1,1,len(rows),len(columns)]}
        result=(xlsx_export if format=='xlsx' else ods_export)(snapshot,{'mode':'values'})
        result['report']=report+result['report'][1:]
        result['scope_lines']=report+result['scope_lines'][1:]
    result.update(source_rows=values['source_rows'],source_columns=columns,rows=len(rows),columns=len(columns))
    require(len(encoded(result).encode())<=4*1024*1024,'READ_LIMIT','List export response exceeds 4 MiB')
    return result
