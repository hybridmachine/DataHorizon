# SPDX-License-Identifier: GPL-3.0-only
"""Regenerate the GUI fixtures with one formula DataHorizon cannot calculate and a markup sheet name (build-only openpyxl writer)."""
from pathlib import Path
from openpyxl import Workbook
book=Workbook(); sheet=book.active; sheet.title='Data'
sheet['A1']=5; sheet['B1']='=INFO("osversion")'
book.save(Path(__file__).with_name('unsupported.xlsx'))
# A sheet name with markup characters (sheet names cannot contain "/"); it must show literally everywhere.
book=Workbook(); sheet=book.active; sheet.title='<b>x<b>'
sheet['A1']=1
book.save(Path(__file__).with_name('markup.xlsx'))
