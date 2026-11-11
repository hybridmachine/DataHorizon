# SPDX-License-Identifier: GPL-3.0-only
"""Builds minimal ODS packages for tests from the inner XML of each <table:table>."""
import io
import zipfile

from datahorizon.ods import F, M, MIME, N, O, OF, T, X, Y

CALCEXT = "urn:org:documentfoundation:names:experimental:calc:xmlns:calcext:1.0"
NAMESPACES = (f'xmlns:office="{O}" xmlns:table="{T}" xmlns:text="{X}" xmlns:style="{Y}" '
              f'xmlns:fo="{F}" xmlns:number="{N}" xmlns:of="{OF}" xmlns:calcext="{CALCEXT}"')


def _attribute(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def content_xml(sheets, *, styles=""):
    tables = "".join(f'<table:table table:name="{_attribute(name)}">{rows}</table:table>' for name, rows in sheets.items())
    return (f'<office:document-content {NAMESPACES}>'
            f'<office:automatic-styles>{styles}</office:automatic-styles>'
            f'<office:body><office:spreadsheet>{tables}</office:spreadsheet></office:body></office:document-content>')


def styles_document(styles):
    """A styles.xml part whose office:styles holds `styles`."""
    return f'<office:document-styles {NAMESPACES}><office:styles>{styles}</office:styles></office:document-styles>'


def manifest_document(entries=""):
    return (f'<manifest:manifest xmlns:manifest="{M}" manifest:version="1.3">'
            f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{MIME}"/>{entries}</manifest:manifest>')


def workbook_bytes(sheets, *, styles="", styles_xml=None, manifest=None, content=None):
    """`content` replaces the generated content.xml; styles_xml and manifest add those parts."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", MIME)
        archive.writestr("content.xml", content if content is not None else content_xml(sheets, styles=styles),
                         compress_type=zipfile.ZIP_DEFLATED)
        if styles_xml is not None: archive.writestr("styles.xml", styles_xml, compress_type=zipfile.ZIP_DEFLATED)
        if manifest is not None: archive.writestr("META-INF/manifest.xml", manifest, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()
