# SPDX-License-Identifier: GPL-3.0-only
"""Regenerate the GUI fixture with one formula DataHorizon cannot calculate (build-only openpyxl writer)."""
from pathlib import Path
from openpyxl import Workbook
book=Workbook(); sheet=book.active; sheet.title='Data'
sheet['A1']=5; sheet['B1']='=INFO("osversion")'
book.save(Path(__file__).with_name('unsupported.xlsx'))
