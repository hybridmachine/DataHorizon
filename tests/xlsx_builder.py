# SPDX-License-Identifier: GPL-3.0-only
"""Builds minimal XLSX packages for tests from the inner XML of each <sheetData>."""
import io
import zipfile

TRANSITIONAL = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
                "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
                "pkg": "http://schemas.openxmlformats.org/package/2006/relationships"}
STRICT = {"main": "http://purl.oclc.org/ooxml/spreadsheetml/main",
          "rel": "http://purl.oclc.org/ooxml/officeDocument/relationships",
          "pkg": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def workbook_bytes(sheets, *, strict=False, styles_xml=None, shared_strings=None, extra_parts=None,
                   sheet_before=None, sheet_after=None, workbook_after_sheets="", workbook_rels=(), timestamp=None):
    """Optional parameters (all off by default, so existing callers get the same package):
    sheet_before / sheet_after map a sheet name to XML placed before / after its <sheetData> (view, columns,
    merge cells, rules, hyperlinks...); workbook_after_sheets is XML after <sheets> (external references, defined
    names); workbook_rels are extra Relationship elements; timestamp is a fixed ZIP entry time (a 6-tuple) that
    makes the package byte-for-byte reproducible."""
    ns = STRICT if strict else TRANSITIONAL
    names = list(sheets)
    workbook = (f'<workbook xmlns="{ns["main"]}" xmlns:r="{ns["rel"]}"><sheets>'
                + "".join(f'<sheet name="{_escape(n).replace(chr(34), "&quot;")}" sheetId="{i}" r:id="rId{i}"/>'
                          for i, n in enumerate(names, 1)) + "</sheets>" + workbook_after_sheets + "</workbook>")
    rels = [f'<Relationship Id="rId{i}" Type="{ns["rel"]}/worksheet" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(names) + 1)]
    parts = {"xl/workbook.xml": workbook}
    for i, name in enumerate(names, 1):
        before, after = (sheet_before or {}).get(name, ""), (sheet_after or {}).get(name, "")
        relationships = f' xmlns:r="{ns["rel"]}"' if after else ""
        parts[f"xl/worksheets/sheet{i}.xml"] = (f'<worksheet xmlns="{ns["main"]}"{relationships}>{before}'
                                                f'<sheetData>{sheets[name]}</sheetData>{after}</worksheet>')
    if styles_xml is not None:
        parts["xl/styles.xml"] = styles_xml
        rels.append(f'<Relationship Id="rIdStyles" Type="{ns["rel"]}/styles" Target="styles.xml"/>')
    if shared_strings is not None:
        parts["xl/sharedStrings.xml"] = (f'<sst xmlns="{ns["main"]}">'
                                        + "".join(f"<si><t>{_escape(s)}</t></si>" for s in shared_strings) + "</sst>")
        rels.append(f'<Relationship Id="rIdStrings" Type="{ns["rel"]}/sharedStrings" Target="sharedStrings.xml"/>')
    rels.extend(workbook_rels)
    parts["xl/_rels/workbook.xml.rels"] = f'<Relationships xmlns="{ns["pkg"]}">{"".join(rels)}</Relationships>'
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in {**parts, **(extra_parts or {})}.items():
            archive.writestr(zipfile.ZipInfo(path, timestamp) if timestamp else path, data,
                             **({"compress_type": zipfile.ZIP_DEFLATED} if timestamp else {}))
    return output.getvalue()
