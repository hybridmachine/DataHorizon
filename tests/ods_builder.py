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


def workbook_bytes(sheets, *, styles="", styles_xml=None, manifest=None, content=None, timestamp=None):
    """`content` replaces the generated content.xml; styles_xml and manifest add those parts; timestamp (a 6-tuple)
    fixes the ZIP entry times so the package is reproducible."""
    output = io.BytesIO()
    def put(archive, name, data, compress):
        archive.writestr(zipfile.ZipInfo(name, timestamp) if timestamp else name, data, compress_type=compress)
    with zipfile.ZipFile(output, "w") as archive:
        put(archive, "mimetype", MIME, zipfile.ZIP_STORED)
        put(archive, "content.xml", content if content is not None else content_xml(sheets, styles=styles), zipfile.ZIP_DEFLATED)
        if styles_xml is not None: put(archive, "styles.xml", styles_xml, zipfile.ZIP_DEFLATED)
        if manifest is not None: put(archive, "META-INF/manifest.xml", manifest, zipfile.ZIP_DEFLATED)
    return output.getvalue()
