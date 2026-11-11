# SPDX-License-Identifier: GPL-3.0-only
"""Writes the hand-built feature workbooks of the interchange corpus (standard library only).

Each workbook exercises one spreadsheet feature area so the scorecard shows how it imports today. The XML is
written by hand through tests/xlsx_builder.py and tests/ods_builder.py as an approximation of what Excel or
LibreOffice would save; the files are meant to be replaced or supplemented by application-saved ones. Output is
byte-for-byte reproducible (fixed ZIP times). Run: python3 tests/fixtures/corpus/generate_feature_workbooks.py
[--check] [--out DIRECTORY]; --check exits non-zero if a file on disk differs from what this script produces."""
import argparse
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TESTS)); sys.path.insert(0, str(TESTS.parent / "service"))
import ods_builder  # noqa: E402
import xlsx_builder  # noqa: E402

STAMP = (2026, 11, 11, 0, 0, 0)
RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
CT = {"workbook": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
      "sheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
      "styles": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"}


def n(ref, value, style=None):
    return f'<c r="{ref}"' + (f' s="{style}"' if style is not None else "") + f"><v>{value}</v></c>"


def s(ref, text, style=None):
    return f'<c r="{ref}" t="inlineStr"' + (f' s="{style}"' if style is not None else "") + f"><is><t>{text}</t></is></c>"


def f(ref, formula, value=None, style=None, extra=""):
    return (f'<c r="{ref}"' + (f' s="{style}"' if style is not None else "") + f"><f{extra}>{formula}</f>"
            + (f"<v>{value}</v>" if value is not None else "") + "</c>")


def rows(*lines, attributes=None):
    attributes = attributes or {}
    return "".join(f'<row r="{i}"{attributes.get(i, "")}>{"".join(cells)}</row>' for i, cells in enumerate(lines, 1))


def content_types(overrides, strict=False):
    entries = {"/xl/workbook.xml": CT["workbook"], **overrides}
    return ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            + "".join(f'<Override PartName="{p}" ContentType="{t}"/>' for p, t in entries.items()) + "</Types>")


def root_rels():
    return (f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{RELS}/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>')


def sheet_rels(*items):
    return f'<Relationships xmlns="{PKG}">{"".join(items)}</Relationships>'


def book(sheets, *, overrides=None, parts=None, **options):
    """A package with [Content_Types].xml and the package relationships Excel writes."""
    types = {f"/xl/worksheets/sheet{i}.xml": CT["sheet"] for i in range(1, len(sheets) + 1)}
    if options.get("styles_xml") is not None: types["/xl/styles.xml"] = CT["styles"]
    types.update(overrides or {})
    extra = {"[Content_Types].xml": content_types(types), "_rels/.rels": root_rels(), **(parts or {})}
    return xlsx_builder.workbook_bytes(sheets, extra_parts=extra, timestamp=STAMP, **options)


def styles(*, numfmts="", fonts='<font><sz val="11"/><name val="Calibri"/></font>',
           fills='<fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>',
           xfs='<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>', cell_styles="", dxfs="",
           style_xfs='<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'):
    return (f'<styleSheet xmlns="{MAIN}">'
            + (f'<numFmts count="{numfmts.count("<numFmt ")}">{numfmts}</numFmts>' if numfmts else "")
            + f'<fonts count="{fonts.count("<font>")}">{fonts}</fonts>'
            + f'<fills count="{fills.count("<fill>")}">{fills}</fills>'
            + '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            + f'<cellStyleXfs count="{style_xfs.count("<xf ")}">{style_xfs}</cellStyleXfs>'
            + f'<cellXfs count="{xfs.count("<xf ")}">{xfs}</cellXfs>'
            + (f'<cellStyles count="{cell_styles.count("<cellStyle ")}">{cell_styles}</cellStyles>' if cell_styles else "")
            + (f'<dxfs count="{dxfs.count("<dxf>")}">{dxfs}</dxfs>' if dxfs else "") + "</styleSheet>")


def merged_cells():
    cells = rows([s("A1", "Quarterly report")], [s("A2", "Region"), n("B2", 10)], [n("B3", 20)], [s("A4", "Total"), f("B4", "SUM(B2:B3)", 30)])
    after = '<mergeCells count="2"><mergeCell ref="A1:C1"/><mergeCell ref="A2:A3"/></mergeCells>'
    return book({"Merged": cells}, sheet_after={"Merged": after})


def layout():
    cols = ('<cols><col min="1" max="1" width="24.5" customWidth="1"/><col min="2" max="2" width="9.140625" hidden="1" customWidth="1"/>'
            '<col min="3" max="4" width="14" customWidth="1"/></cols>')
    view = ('<sheetViews><sheetView workbookViewId="0"><pane xSplit="1" ySplit="1" topLeftCell="B2" activePane="bottomRight" state="frozen"/>'
            '<selection pane="topRight"/><selection pane="bottomLeft"/><selection pane="bottomRight" activeCell="B2" sqref="B2"/></sheetView></sheetViews>'
            '<sheetFormatPr defaultRowHeight="15"/>')
    cells = rows([s("A1", "Item"), s("B1", "Hidden column"), s("C1", "Qty"), s("D1", "Price")],
                 [s("A2", "Tall row"), n("B2", 1), n("C2", 2), n("D2", 3)],
                 [s("A3", "Hidden row"), n("B3", 4), n("C3", 5), n("D3", 6)],
                 [s("A4", "Short row"), n("B4", 7), n("C4", 8), n("D4", 9)],
                 attributes={2: ' ht="42" customHeight="1"', 3: ' hidden="1"', 4: ' ht="9.75" customHeight="1"'})
    return book({"Layout": cells}, sheet_before={"Layout": view + cols})


def number_formats():
    numfmts = ('<numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/>'
               '<numFmt numFmtId="165" formatCode="#,##0.00;[Red]\\-#,##0.00"/>'
               '<numFmt numFmtId="166" formatCode="# ?/?"/>'
               '<numFmt numFmtId="167" formatCode="&quot;EUR&quot;\\ #,##0.00"/>')
    xfs = ('<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           + "".join(f'<xf numFmtId="{i}" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' for i in (164, 165, 166, 167, 10)))
    cells = rows([s("A1", "Date"), n("B1", 46000, 1)],
                 [s("A2", "Negative in red"), n("B2", -1234.5, 2)],
                 [s("A3", "Fraction"), n("B3", 0.75, 3)],
                 [s("A4", "Currency text"), n("B4", 99.5, 4)],
                 [s("A5", "Percent"), n("B5", 0.1234, 5)])
    return book({"Formats": cells}, styles_xml=styles(numfmts=numfmts, xfs=xfs))


def conditional_formatting():
    dxfs = '<dxf><font><b/><color rgb="FF9C0006"/></font><fill><patternFill><bgColor rgb="FFFFC7CE"/></patternFill></fill></dxf>'
    cells = rows([n("A1", 5), n("B1", 1)], [n("A2", 50), n("B2", 2)], [n("A3", 500), n("B3", 3)])
    after = ('<conditionalFormatting sqref="A1:A3"><cfRule type="cellIs" dxfId="0" priority="1" operator="greaterThan"><formula>100</formula></cfRule></conditionalFormatting>'
             '<conditionalFormatting sqref="B1:B3"><cfRule type="colorScale" priority="2"><colorScale><cfvo type="min"/><cfvo type="max"/>'
             '<color rgb="FFFFFFFF"/><color rgb="FF63BE7B"/></colorScale></cfRule></conditionalFormatting>')
    return book({"Rules": cells}, styles_xml=styles(dxfs=dxfs), sheet_after={"Rules": after})


def data_validation():
    cells = rows([s("A1", "Status"), s("B1", "Count")], [s("A2", "Open"), n("B2", 3)])
    after = ('<dataValidations count="2">'
             '<dataValidation type="list" allowBlank="1" showErrorMessage="1" errorTitle="Status" error="Pick one." sqref="A2:A10"><formula1>"Open,Closed,Pending"</formula1></dataValidation>'
             '<dataValidation type="whole" operator="between" allowBlank="1" showInputMessage="1" promptTitle="Count" prompt="0 to 100" sqref="B2:B10"><formula1>0</formula1><formula2>100</formula2></dataValidation>'
             '</dataValidations>')
    return book({"Validation": cells}, sheet_after={"Validation": after})


def hyperlink_and_comment():
    cells = rows([s("A1", "Project site"), n("B1", 1)])
    after = ('<hyperlinks><hyperlink ref="A1" r:id="rId1" tooltip="Example"/></hyperlinks>'
             '<legacyDrawing r:id="rId2"/>')
    rels = sheet_rels(f'<Relationship Id="rId1" Type="{RELS}/hyperlink" Target="https://example.com/" TargetMode="External"/>',
                      f'<Relationship Id="rId2" Type="{RELS}/vmlDrawing" Target="../drawings/vmlDrawing1.vml"/>',
                      f'<Relationship Id="rId3" Type="{RELS}/comments" Target="../comments1.xml"/>')
    comments = (f'<comments xmlns="{MAIN}"><authors><author>Test author</author></authors><commentList>'
                '<comment ref="B1" authorId="0"><text><r><t>Check this value.</t></r></text></comment></commentList></comments>')
    vml = ('<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel">'
           '<v:shape id="_x0000_s1025" type="#_x0000_t202" style="position:absolute;margin-left:59.25pt;margin-top:1.5pt;width:108pt;height:59.25pt;visibility:hidden">'
           '<x:ClientData ObjectType="Note"><x:Row>0</x:Row><x:Column>1</x:Column></x:ClientData></v:shape></xml>')
    parts = {"xl/worksheets/_rels/sheet1.xml.rels": rels, "xl/comments1.xml": comments, "xl/drawings/vmlDrawing1.vml": vml}
    return book({"Links": cells}, sheet_after={"Links": after}, parts=parts,
                overrides={"/xl/comments1.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml"})


def defined_names():
    cells = rows([s("A1", "Net"), n("B1", 200)], [s("A2", "Rate"), n("B2", 0.2)],
                 [s("A3", "Tax"), f("B3", "Net*TaxRate", 40)], [s("A4", "Local"), f("B4", "LocalOffset+1", 6)])
    names = ('<definedNames><definedName name="Net">Names!$B$1</definedName><definedName name="TaxRate">Names!$B$2</definedName>'
             '<definedName name="LocalOffset" localSheetId="0">5</definedName></definedNames>')
    return book({"Names": cells}, workbook_after_sheets=names)


def array_and_data_table():
    cells = rows([n("A1", 1), n("B1", 2), f("C1", "A1:A3*B1:B3", 2, extra=' t="array" ref="C1:C3"')],
                 [n("A2", 3), n("B2", 4), n("C2", 12)], [n("A3", 5), n("B3", 6), n("C3", 30)],
                 [s("A5", "Input"), n("B5", 10)], [s("A6", "Result"), f("B6", "B5*2", 20)],
                 [n("A8", 1), f("B8", "", 2, extra=' t="dataTable" ref="B8:B9" dt2D="0" dtr="0" r1="B5"')],
                 [n("A9", 2), n("B9", 4)])
    return book({"Arrays": cells})


def excel_table():
    cells = rows([s("A1", "Item"), s("B1", "Amount")], [s("A2", "Pens"), n("B2", 3)], [s("A3", "Ink"), n("B3", 9)],
                 [s("D1", "Total"), f("D2", "SUM(Sales[Amount])", 12)])
    table = (f'<table xmlns="{MAIN}" id="1" name="Sales" displayName="Sales" ref="A1:B3" totalsRowShown="0">'
             '<autoFilter ref="A1:B3"/><tableColumns count="2"><tableColumn id="1" name="Item"/><tableColumn id="2" name="Amount"/></tableColumns>'
             '<tableStyleInfo name="TableStyleMedium2" showFirstColumn="0" showLastColumn="0" showRowStripes="1" showColumnStripes="0"/></table>')
    rels = sheet_rels(f'<Relationship Id="rId1" Type="{RELS}/table" Target="../tables/table1.xml"/>')
    return book({"Tables": cells}, sheet_after={"Tables": '<tableParts count="1"><tablePart r:id="rId1"/></tableParts>'},
                parts={"xl/worksheets/_rels/sheet1.xml.rels": rels, "xl/tables/table1.xml": table},
                overrides={"/xl/tables/table1.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"})


def theme_colours():
    scheme = "".join(f'<a:{tag}><a:srgbClr val="{value}"/></a:{tag}>' for tag, value in (
        ("dk1", "000000"), ("lt1", "FFFFFF"), ("dk2", "44546A"), ("lt2", "E7E6E6"), ("accent1", "4472C4"), ("accent2", "ED7D31"),
        ("accent3", "A5A5A5"), ("accent4", "FFC000"), ("accent5", "5B9BD5"), ("accent6", "70AD47"), ("hlink", "0563C1"), ("folHlink", "954F72")))
    theme = ('<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme"><a:themeElements>'
             f'<a:clrScheme name="Office">{scheme}</a:clrScheme></a:themeElements></a:theme>')
    fonts = ('<font><sz val="11"/><name val="Calibri"/></font>'
             '<font><b/><sz val="11"/><color theme="4"/><name val="Calibri"/></font>'
             '<font><sz val="11"/><color indexed="10"/><name val="Calibri"/></font>'
             '<font><sz val="11"/><color theme="5" tint="-0.249977111117893"/><name val="Calibri"/></font>')
    fills = ('<fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
             '<fill><patternFill patternType="solid"><fgColor theme="4" tint="0.79998168889431442"/><bgColor indexed="64"/></patternFill></fill>'
             '<fill><patternFill patternType="solid"><fgColor indexed="13"/><bgColor indexed="64"/></patternFill></fill>')
    xfs = ('<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="1" applyFont="1" applyFill="1"/>'
           '<xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
           '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>')
    style_xfs = ('<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
                 '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" applyFont="1" applyFill="1"/>')
    cell_styles = '<cellStyle name="Normal" xfId="0" builtinId="0"/><cellStyle name="Accent Note" xfId="1"/>'
    cells = rows([s("A1", "Named style", 1), s("B1", "Indexed colours", 2), s("C1", "Tinted theme font", 3)])
    rel = f'<Relationship Id="rIdTheme" Type="{RELS}/theme" Target="theme/theme1.xml"/>'
    return book({"Colours": cells}, styles_xml=styles(fonts=fonts, fills=fills, xfs=xfs, style_xfs=style_xfs, cell_styles=cell_styles),
                workbook_rels=[rel], parts={"xl/theme/theme1.xml": theme},
                overrides={"/xl/theme/theme1.xml": "application/vnd.openxmlformats-officedocument.theme+xml"})


def strict_namespace():
    cells = rows([n("A1", 2), n("B1", 3), f("C1", "A1*B1", 6)], [s("A2", "Strict")])
    return xlsx_builder.workbook_bytes({"Strict": cells}, strict=True, timestamp=STAMP,
                                       extra_parts={"[Content_Types].xml": content_types({"/xl/worksheets/sheet1.xml": CT["sheet"]}),
                                                    "_rels/.rels": root_rels()})


def shared_formulas():
    cells = rows([n("A1", 1), n("B1", 10), f("C1", "A1+B1", 11, extra=' t="shared" ref="C1:C3" si="0"')],
                 [n("A2", 2), n("B2", 20), '<c r="C2"><f t="shared" si="0"/><v>22</v></c>'],
                 [n("A3", 3), n("B3", 30), '<c r="C3"><f t="shared" si="0"/><v>33</v></c>'])
    return book({"Shared": cells})


def error_cells():
    errors = ("#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!", "#REF!", "#VALUE!")
    lines = [[f'<c r="A{i}" t="e"><v>{e}</v></c>'] for i, e in enumerate(errors, 1)]
    lines[0].append('<c r="B1" t="e"><f>1/0</f><v>#DIV/0!</v></c>')
    lines[1].append(f("B2", "B1+1", 2).replace('<c r="B2">', '<c r="B2" t="e">').replace("<v>2</v>", "<v>#DIV/0!</v>"))
    cells = rows(*lines)
    return book({"Errors": cells})


def external_and_3d_references():
    first = rows([n("A1", 1), n("B1", 2)], [f("A3", "SUM(First:Last!A1)", 3)], [f("A4", "[1]Budget!$A$1", 100)])
    middle = rows([n("A1", 2)])
    last = rows([n("A1", 0)])
    link = (f'<externalLink xmlns="{MAIN}" xmlns:r="{RELS}"><externalBook r:id="rId1"><sheetNames><sheetName val="Budget"/></sheetNames>'
            '<sheetDataSet><sheetData sheetId="0"><row r="1"><cell r="A1"><v>100</v></cell></row></sheetData></sheetDataSet></externalBook></externalLink>')
    link_rels = sheet_rels(f'<Relationship Id="rId1" Type="{RELS}/externalLinkPath" Target="Budget.xlsx" TargetMode="External"/>')
    workbook_links = '<externalReferences><externalReference r:id="rIdLink"/></externalReferences>'
    return book({"First": first, "Middle": middle, "Last": last}, workbook_after_sheets=workbook_links,
                workbook_rels=[f'<Relationship Id="rIdLink" Type="{RELS}/externalLink" Target="externalLinks/externalLink1.xml"/>'],
                parts={"xl/externalLinks/externalLink1.xml": link, "xl/externalLinks/_rels/externalLink1.xml.rels": link_rels},
                overrides={"/xl/externalLinks/externalLink1.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.externalLink+xml"})


def ods_cell(value, style=""):
    return f'<table:table-cell office:value-type="float" office:value="{value}"{style}/>'


def ods_text(text, style=""):
    return f'<table:table-cell office:value-type="string"{style}><text:p>{text}</text:p></table:table-cell>'


def ods_merged():
    rows_ = ('<table:table-row><table:table-cell table:number-columns-spanned="3" table:number-rows-spanned="1" office:value-type="string"><text:p>Quarterly report</text:p></table:table-cell>'
             '<table:covered-table-cell/><table:covered-table-cell/></table:table-row>'
             '<table:table-row>' + ods_text("Region") + ods_cell(10) + '</table:table-row>')
    return ods_builder.workbook_bytes({"Merged": rows_}, timestamp=STAMP)


def ods_number_format():
    styles_ = ('<number:number-style style:name="NegRed"><number:text>-</number:text><number:number number:decimal-places="2" number:min-integer-digits="1" number:grouping="true"/></number:number-style>'
               '<style:style style:name="Money" style:family="table-cell" style:data-style-name="NegRed"/>')
    rows_ = '<table:table-row>' + ods_cell("-1234.5", ' table:style-name="Money"') + ods_cell(3) + '</table:table-row>'
    return ods_builder.workbook_bytes({"Formats": rows_}, styles=styles_, timestamp=STAMP)


def ods_validation():
    validations = ('<table:content-validations><table:content-validation table:name="val1" table:condition="of:cell-content-is-between(0;100)" '
                   'table:allow-empty-cell="true" table:base-cell-address="Validation.A1"><table:error-message table:display="true" table:message-type="stop" table:title="Range"><text:p>0 to 100</text:p></table:error-message>'
                   '</table:content-validation></table:content-validations>')
    rows_ = '<table:table-row>' + ods_cell(5, ' table:content-validation-name="val1"') + '</table:table-row>'
    content = (f'<office:document-content {ods_builder.NAMESPACES}><office:automatic-styles/><office:body><office:spreadsheet>{validations}'
               f'<table:table table:name="Validation">{rows_}</table:table></office:spreadsheet></office:body></office:document-content>')
    return ods_builder.workbook_bytes({}, content=content, timestamp=STAMP)


FILES = {
    "merged-cells.xlsx": merged_cells, "layout-hidden-frozen.xlsx": layout, "number-formats.xlsx": number_formats,
    "conditional-formatting.xlsx": conditional_formatting, "data-validation.xlsx": data_validation,
    "hyperlink-comment.xlsx": hyperlink_and_comment, "defined-names.xlsx": defined_names,
    "array-data-table.xlsx": array_and_data_table, "excel-table.xlsx": excel_table, "theme-colours.xlsx": theme_colours,
    "strict-namespace.xlsx": strict_namespace, "shared-formulas.xlsx": shared_formulas, "error-cells.xlsx": error_cells,
    "external-3d-references.xlsx": external_and_3d_references,
    "merged-cells.ods": ods_merged, "number-format.ods": ods_number_format, "data-validation.ods": ods_validation}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent), help="directory to write the workbooks into")
    parser.add_argument("--check", action="store_true", help="compare with the files on disk instead of writing")
    arguments = parser.parse_args(argv)
    target, stale = Path(arguments.out), []
    for name, build in FILES.items():
        data = build()
        path = target / name
        if arguments.check:
            if not path.is_file() or path.read_bytes() != data: stale.append(name)
        else:
            path.write_bytes(data)
    if stale:
        print("Differs from the generator: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
