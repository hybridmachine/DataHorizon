# SPDX-License-Identifier: GPL-3.0-only
"""Resolve the reference text workbook code passes to the code context."""
from .engine import require
from . import names

MAX_CELLS=4096


def resolve(book,ref,*,single):
    """Return (sheet name, row1, col1, row2, col2) for an address or a defined name.

    Text containing '!' is a sheet-qualified address; anything else is a name.
    """
    require(isinstance(ref,str) and ref!='','REFERENCE_INVALID','Reference must be nonempty text')
    if '!' in ref:
        target=names.parse_reference(book,ref,missing_sheet=('REFERENCE_NOT_FOUND',404))
    else:
        found=names.find(book.db,ref)
        require(found is not None,'REFERENCE_NOT_FOUND','No defined name matches that reference',404)
        target={key:found[key] for key in ('sheet_id',*names.SPAN)}
        require(target['row1'] is not None and book.db.execute('SELECT 1 FROM sheets WHERE id=?',(target['sheet_id'],)).fetchone() is not None,'NAME_BROKEN','The name no longer refers to a cell or range',409)
    cells=(target['row2']-target['row1']+1)*(target['col2']-target['col1']+1)
    require(not single or cells==1,'REFERENCE_SHAPE','This operation needs a single cell, not a range')
    require(cells<=MAX_CELLS,'REFERENCE_LIMIT','Reference covers more than 4,096 cells')
    sheet=book.db.execute('SELECT name FROM sheets WHERE id=?',(target['sheet_id'],)).fetchone()[0]
    return sheet,target['row1'],target['col1'],target['row2'],target['col2']
