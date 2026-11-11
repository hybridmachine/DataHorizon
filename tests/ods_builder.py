# SPDX-License-Identifier: GPL-3.0-only
"""Builds minimal ODS packages for tests from the inner XML of each <table:table>."""
import io
import zipfile

from datahorizon.ods import F, MIME, N, O, OF, T, X, Y

CALCEXT = "urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0"


def _attribute(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def workbook_bytes(sheets, *, styles=""):
    tables = "".join(f'<table:table table:name="{_attribute(name)}">{rows}</table:table>' for name, rows in sheets.items())
    content = (f'<office:document-content xmlns:office="{O}" xmlns:table="{T}" xmlns:text="{X}" xmlns:style="{Y}" '
               f'xmlns:fo="{F}" xmlns:number="{N}" xmlns:of="{OF}" xmlns:calcext="{CALCEXT}">'
               f'<office:automatic-styles>{styles}</office:automatic-styles>'
               f'<office:body><office:spreadsheet>{tables}</office:spreadsheet></office:body></office:document-content>')
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", MIME)
        archive.writestr("content.xml", content)
    return output.getvalue()
