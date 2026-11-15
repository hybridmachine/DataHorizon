# SPDX-License-Identifier: GPL-3.0-only
"""Builds XLSX and ODS packages with drawings and charts for tests, on top of the xlsx_builder and ods_builder
workbook_bytes."""
import re
from xml.sax.saxutils import escape, quoteattr

from datahorizon import ods_features
from ods_builder import NAMESPACES, workbook_bytes as ods_workbook_bytes
from xlsx_builder import STRICT, TRANSITIONAL, workbook_bytes

C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
CX = "http://schemas.microsoft.com/office/drawing/2014/chartex"
CHARTEX_REL = "http://schemas.microsoft.com/office/2014/relationships/chartEx"


def _series_xml(number, item, kind):
    scatter = kind in ("scatter", "bubble")
    parts = [f'<c:idx val="{number}"/><c:order val="{item.get("order", number)}"/>']
    if item.get("name_ref"):
        parts.append(f'<c:tx><c:strRef><c:f>{escape(item["name_ref"])}</c:f></c:strRef></c:tx>')
    elif item.get("name_text") is not None:
        parts.append(f'<c:tx><c:v>{escape(item["name_text"])}</c:v></c:tx>')
    parts.append(item.get("sppr", ""))
    for key, tag, container in (("cat", "cat", "strRef"), ("x", "xVal", "numRef"), ("val", "yVal" if scatter else "val", "numRef")):
        value = item.get(key)
        if value is None:
            continue
        inner = value if value.startswith("<") else f"<c:{container}><c:f>{escape(value)}</c:f></c:{container}>"
        parts.append(f"<c:{tag}>{inner}</c:{tag}>")
    return f"<c:ser>{''.join(parts)}</c:ser>"


def plot_xml(kind="bar", direction="col", grouping="clustered", series=(), extras="", axes=("1", "2")):
    """One chart element of a plot area: ``kind`` is the element name without ``Chart`` (``bar``, ``bar3D``, ``line``...)."""
    head = ""
    if kind.startswith("bar"):
        head += f'<c:barDir val="{direction}"/>'
    if grouping and not kind.startswith(("pie", "doughnut", "scatter", "radar")):
        head += f'<c:grouping val="{grouping}"/>'
    sers = "".join(_series_xml(number, item, kind) for number, item in enumerate(series))
    ids = "".join(f'<c:axId val="{item}"/>' for item in axes) if kind not in ("pie", "pie3D", "doughnut") else ""
    return f"<c:{kind}Chart>{head}{sers}{extras}{ids}</c:{kind}Chart>"


def axes_xml(kind="bar", category="", value="", ids=("1", "2"), crosses="autoZero"):
    """The axes a plot of ``kind`` needs; ``category`` and ``value`` are raw child XML (titles, scaling...)."""
    if kind in ("pie", "pie3D", "doughnut"):
        return ""
    cat_id, val_id = ids
    if kind == "scatter":
        return (f'<c:valAx><c:axId val="{cat_id}"/><c:axPos val="b"/>{category}<c:crossAx val="{val_id}"/></c:valAx>'
                f'<c:valAx><c:axId val="{val_id}"/><c:axPos val="l"/>{value}<c:crossAx val="{cat_id}"/>'
                f'<c:crosses val="{crosses}"/></c:valAx>')
    return (f'<c:catAx><c:axId val="{cat_id}"/><c:axPos val="b"/>{category}<c:crossAx val="{val_id}"/></c:catAx>'
            f'<c:valAx><c:axId val="{val_id}"/><c:axPos val="l"/>{value}<c:crossAx val="{cat_id}"/>'
            f'<c:crosses val="{crosses}"/></c:valAx>')


def chart_xml(kind="bar", direction="col", grouping="clustered", series=(), title=None, legend=None, extras="",
              category_axis="", value_axis="", plot_area=None):
    """A chart part.  ``title`` and ``legend`` are raw XML (see ``rich_title``); ``extras`` goes inside the plot
    element after its series; ``plot_area`` replaces the whole plot area content (several plots, own axes)."""
    area = plot_area if plot_area is not None else (
        plot_xml(kind, direction, grouping, series, extras) + axes_xml(kind, category_axis, value_axis))
    legend_xml = "" if legend is None else f'<c:legend><c:legendPos val="{legend}"/></c:legend>'
    return (f'<c:chartSpace xmlns:c="{C}" xmlns:a="{A}"><c:chart>{title or ""}'
            f"<c:plotArea>{area}</c:plotArea>{legend_xml}</c:chart></c:chartSpace>")


def rich_title(text):
    return f'<c:title><c:tx><c:rich><a:p><a:r><a:t>{escape(text)}</a:t></a:r></a:p></c:rich></c:tx></c:title>'


def cell_title(formula, cached):
    return (f"<c:title><c:tx><c:strRef><c:f>{escape(formula)}</c:f><c:strCache><c:ptCount val=\"1\"/>"
            f'<c:pt idx="0"><c:v>{escape(cached)}</c:v></c:pt></c:strCache></c:strRef></c:tx></c:title>')


def _marker(col, col_off, row, row_off):
    return (f"<xdr:col>{col}</xdr:col><xdr:colOff>{col_off}</xdr:colOff>"
            f"<xdr:row>{row}</xdr:row><xdr:rowOff>{row_off}</xdr:rowOff>")


def two_cell(first=(2, 0, 0, 0), last=(9, 0, 15, 0), frame="{frame}"):
    """A twoCellAnchor template; ``first`` and ``last`` are ``(col, col offset, row, row offset)`` in 0-based cells and EMU."""
    return (f"<xdr:twoCellAnchor><xdr:from>{_marker(*first)}</xdr:from><xdr:to>{_marker(*last)}</xdr:to>"
            f"{frame}<xdr:clientData/></xdr:twoCellAnchor>")


def one_cell(first=(2, 0, 0, 0), extent=(4572000, 2743200), frame="{frame}"):
    return (f'<xdr:oneCellAnchor><xdr:from>{_marker(*first)}</xdr:from><xdr:ext cx="{extent[0]}" cy="{extent[1]}"/>'
            f"{frame}<xdr:clientData/></xdr:oneCellAnchor>")


def absolute(position=(1219200, 381000), extent=(4572000, 2743200), frame="{frame}"):
    return (f'<xdr:absoluteAnchor><xdr:pos x="{position[0]}" y="{position[1]}"/><xdr:ext cx="{extent[0]}" cy="{extent[1]}"/>'
            f"{frame}<xdr:clientData/></xdr:absoluteAnchor>")


def frame_xml(rid, name, extended=False):
    element = (f'<cx:chart xmlns:cx="{CX}" r:id="{rid}"/>' if extended
               else f'<c:chart xmlns:c="{C}" r:id="{rid}"/>')
    uri = CX if extended else C
    return (f'<xdr:graphicFrame macro=""><xdr:nvGraphicFramePr><xdr:cNvPr id="2" name="{escape(name)}"/><xdr:cNvGraphicFramePr/>'
            f'</xdr:nvGraphicFramePr><xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm>'
            f'<a:graphic><a:graphicData uri="{uri}">{element}</a:graphicData></a:graphic></xdr:graphicFrame>')


def xlsx_with_charts(sheets, charts, **builder_options):
    """A workbook of ``sheets`` ({name: sheetData XML}) with ``charts`` drawn on them.

    Each chart is ``{"sheet", "xml", "anchor", "name"}``: ``anchor`` is an anchor template whose ``{frame}`` is
    replaced by the graphic frame (None gives ``two_cell()``).  Optional keys: ``extended`` (a ``cx:chart`` frame
    whose part is a chartEx), ``missing`` (the part is not written) and ``target`` (the relationship target)."""
    strict = builder_options.get("strict", False)
    rel = (STRICT if strict else TRANSITIONAL)["rel"]
    pkg = TRANSITIONAL["pkg"]
    parts, sheet_after = {}, dict(builder_options.pop("sheet_after", None) or {})
    names = list(sheets)
    chart_number = 0
    for number, name in enumerate(names, 1):
        mine = [item for item in charts if item["sheet"] == name]
        if not mine:
            continue
        anchors, links = [], []
        for index, item in enumerate(mine, 1):
            chart_number += 1
            rid = f"rId{index}"
            extended = item.get("extended", False)
            part = f"xl/charts/{'chartEx' if extended else 'chart'}{chart_number}.xml"
            target = item.get("target", f"../charts/{part.rsplit('/', 1)[1]}")
            if not item.get("missing"):
                parts[part] = item["xml"]
            kind = CHARTEX_REL if extended else f"{rel}/chart"
            links.append(f'<Relationship Id="{rid}" Type="{kind}" Target="{target}"/>')
            anchors.append((item.get("anchor") or two_cell()).replace(
                "{frame}", frame_xml(rid, item.get("name", f"Chart {index}"), extended)))
        parts[f"xl/drawings/drawing{number}.xml"] = (
            f'<xdr:wsDr xmlns:xdr="{XDR}" xmlns:a="{A}" xmlns:r="{rel}" xmlns:c="{C}" xmlns:mc="{MC}">{"".join(anchors)}</xdr:wsDr>')
        parts[f"xl/drawings/_rels/drawing{number}.xml.rels"] = f'<Relationships xmlns="{pkg}">{"".join(links)}</Relationships>'
        parts[f"xl/worksheets/_rels/sheet{names.index(name) + 1}.xml.rels"] = (
            f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" Type="{rel}/drawing" '
            f'Target="../drawings/drawing{number}.xml"/></Relationships>')
        sheet_after[name] = sheet_after.get(name, "") + '<drawing r:id="rId1"/>'
    parts.update(builder_options.pop("extra_parts", None) or {})
    return workbook_bytes(sheets, extra_parts=parts, sheet_after=sheet_after, **builder_options)


def _rows(header, count=7, width=2):
    cells = "".join(f'<c r="{chr(65 + col)}1" t="inlineStr"><is><t>{escape(text)}</t></is></c>' for col, text in enumerate(header))
    rows = [f'<row r="1">{cells}</row>']
    for row in range(2, count + 2):
        values = "".join(f'<c r="{chr(66 + col)}{row}"><v>{row * (col + 1)}</v></c>' for col in range(width - 1))
        rows.append(f'<row r="{row}"><c r="A{row}" t="inlineStr"><is><t>Division {row}</t></is></c>{values}</row>')
    return "".join(rows)


# The sample workbook's first sheet: division and sales, rows 2 to 8.
SAMPLE_SALES = [("Sports", 400586), ("Arts", 353095), ("Electronics", 804585), ("Office", 410434),
                ("Celebrations", 99699), ("Food", 25539), ("Garden", 25156)]
SAMPLE_SHEETS = ("Column Chart", "Stacked Column")


def _sales_rows():
    rows = ['<row r="1"><c r="A1" t="inlineStr"><is><t>Division</t></is></c><c r="B1" t="inlineStr"><is><t>Sales</t></is></c></row>']
    for row, (division, sales) in enumerate(SAMPLE_SALES, 2):
        rows.append(f'<row r="{row}"><c r="A{row}" t="inlineStr"><is><t>{division}</t></is></c><c r="B{row}"><v>{sales}</v></c></row>')
    return "".join(rows)


def _sample_series():
    first, second = SAMPLE_SHEETS
    one = [{"name_ref": f"'{first}'!$B$1", "cat": f"'{first}'!$A$2:$A$8", "val": f"'{first}'!$B$2:$B$8"}]
    four = [{"name_ref": f"'{second}'!${chr(66 + n)}$1", "cat": f"'{second}'!$A$2:$A$8",
             "val": f"'{second}'!${chr(66 + n)}$2:${chr(66 + n)}$8"} for n in range(4)]
    return one, four


def sample_like(sheets=2, **builder_options):
    """Two worksheets, each with a column chart named Chart 1 anchored C1 (+632114, +154565 EMU) to J16, as in the
    sample workbook; the first sheet holds the sample's sales figures. ``sheets=1`` keeps only the first."""
    colours = '<c:spPr><a:solidFill><a:schemeClr val="accent1"/></a:solidFill></c:spPr>'
    labels = '<c:dLbls><c:showLegendKey val="0"/><c:showVal val="1"/><c:showCatName val="0"/></c:dLbls><c:gapWidth val="150"/>'
    first, second = SAMPLE_SHEETS
    one, four = ([{**item, "sppr": colours} for item in part] for part in _sample_series())
    anchor = two_cell((2, 632114, 0, 154565), (9, 0, 15, 0))
    return xlsx_with_charts(
        {first: _sales_rows(), **({second: _rows(["Division", "Q1", "Q2", "Q3", "Q4"], width=5)} if sheets > 1 else {})},
        [{"sheet": first, "name": "Chart 1", "anchor": anchor,
          "xml": chart_xml("bar", "col", "clustered", one, rich_title("Sales by Division"), "r", extras=labels)},
         *([{"sheet": second, "name": "Chart 1", "anchor": anchor,
             "xml": chart_xml("bar", "col", "stacked", four, rich_title("Sales by Quarter"), "r",
                              extras=labels + '<c:overlap val="100"/>')}] if sheets > 1 else [])],
        **builder_options)


# ODS: a chart is an object directory whose content.xml holds an office:chart, drawn by a draw:frame.

ODS_CLASSES = {"pie": "circle", "doughnut": "ring"}
ODS_NAMESPACES = (f'{NAMESPACES} xmlns:draw="{ods_features.DRAW}" xmlns:chart="{ods_features.CHART}" '
                  f'xmlns:svg="{ods_features.SVG}" xmlns:xlink="{ods_features.XLINK}"')


def ods_ref(reference):
    """``'My Sheet'!$B$2:$B$8`` as the ODS ``$'My Sheet'.$B$2:.$B$8``; anything else is returned as it is."""
    found = re.fullmatch(r"('(?:[^']|'')+'|[^'!]+)!(.+)", reference)
    if found is None:
        return reference
    first, _, last = found.group(2).partition(":")
    return f"${found.group(1)}.{first}" + (f":.{last}" if last else "")


def ods_title(text, attributes=""):
    return f"<chart:title {attributes}><text:p>{escape(text)}</text:p></chart:title>"


def ods_chart_xml(kind="bar", direction="col", grouping="clustered", series=(), title=None, legend=None, extras="",
                  category_axis="", value_axis="", plot_area=None, value_min=None, value_max=None, axis_extras="",
                  secondary=False, chart_style="", title_style="", legend_style="", axis_style="", axis_attributes="",
                  title_attributes=""):
    """An object's content.xml.  The arguments follow ``chart_xml``: ``kind`` (``bar3D`` for three dimensions),
    ``direction`` and ``grouping`` (``stacked`` and ``percentStacked`` give the plot-area flags), ``series`` (the
    same items, with ``cat``/``val``/``x``/``name_ref`` in the XLSX form of reference, plus ``sppr``, the inner XML of
    the series style, ``cls``, the series' own ``chart:class`` word, and ``axis``, its ``chart:attached-axis``).
    ``title`` is plain text and ``legend`` an ODS position word.  ``extras`` is attribute text added to the plot
    area's chart-properties, ``axis_extras`` to the value axis'; ``category_axis`` and ``value_axis`` are raw child
    XML of the axes; ``plot_area`` replaces the whole plot area content; ``secondary`` adds a second value axis.
    ``chart_style``, ``title_style``, ``legend_style`` and ``axis_style`` are the inner XML of those elements' styles
    (the last is the value axis'), ``axis_attributes`` extra attributes of the value axis' ``style:style`` and
    ``title_attributes`` of the chart's ``chart:title``."""
    three_d = kind.endswith("3D")
    base = kind[:-2] if three_d else kind
    flags = ""
    if direction == "bar" and base == "bar":
        flags += ' chart:vertical="true"'
    if grouping == "stacked":
        flags += ' chart:stacked="true"'
    elif grouping == "percentStacked":
        flags += ' chart:percentage="true"'
    if three_d:
        flags += ' chart:three-dimensional="true"'
    limits = "".join(f' chart:{name}="{value}"' for name, value in (("minimum", value_min), ("maximum", value_max))
                     if value is not None)
    styles = [f'<style:style style:name="plot" style:family="chart"><style:chart-properties{flags} {extras}/></style:style>',
              f'<style:style style:name="axis-y" style:family="chart" {axis_attributes}><style:chart-properties{limits} {axis_extras}/>'
              f'{axis_style}</style:style>',
              f'<style:style style:name="chart" style:family="chart">{chart_style}</style:style>',
              f'<style:style style:name="title" style:family="chart">{title_style}</style:style>',
              f'<style:style style:name="legend" style:family="chart">{legend_style}</style:style>',
              '<style:style style:name="axis-x" style:family="chart"><style:chart-properties/></style:style>']
    members = []
    scatter = base in ("scatter", "bubble")
    categories = next((item["cat"] for item in series if item.get("cat")), None)
    for number, item in enumerate(series):
        styles.append(f'<style:style style:name="s{number}" style:family="chart" {item.get("style_attributes", "")}>'
                      f'{item.get("sppr", "")}</style:style>')
        attributes = f'chart:style-name="s{number}"'
        if item.get("val"):
            attributes += f' chart:values-cell-range-address={quoteattr(ods_ref(item["val"]))}'
        if item.get("name_ref"):
            attributes += f' chart:label-cell-address={quoteattr(ods_ref(item["name_ref"]))}'
        if item.get("cls"):
            attributes += f' chart:class="chart:{item["cls"]}"'
        if item.get("axis"):
            attributes += f' chart:attached-axis="{item["axis"]}"'
        domain = (f'<chart:domain table:cell-range-address={quoteattr(ods_ref(item["x"]))}/>'
                  if scatter and item.get("x") else "")
        members.append(f"<chart:series {attributes}>{domain}</chart:series>")
    x_inner = category_axis + (f'<chart:categories table:cell-range-address={quoteattr(ods_ref(categories))}/>'
                               if categories and not scatter else "")
    area = plot_area if plot_area is not None else (
        f'<chart:axis chart:dimension="x" chart:name="primary-x" chart:style-name="axis-x">{x_inner}</chart:axis>'
        + ("" if base in ("pie", "doughnut") else
           f'<chart:axis chart:dimension="y" chart:name="primary-y" chart:style-name="axis-y">{value_axis}</chart:axis>')
        + ('<chart:axis chart:dimension="y" chart:name="secondary-y"/>' if secondary else "") + "".join(members))
    legend_xml = "" if legend is None else f'<chart:legend chart:style-name="legend" chart:legend-position="{legend}"/>'
    return (f'<office:document-content {ODS_NAMESPACES}><office:automatic-styles>{"".join(styles)}</office:automatic-styles>'
            f'<office:body><office:chart><chart:chart chart:class="chart:{ODS_CLASSES.get(base, base)}" '
            f'chart:style-name="chart" svg:width="12cm" svg:height="8cm">'
            f'{ods_title(title, "chart:style-name=" + chr(34) + "title" + chr(34) + " " + title_attributes) if title else ""}{legend_xml}'
            f'<chart:plot-area chart:style-name="plot">{area}</chart:plot-area></chart:chart></office:chart></office:body>'
            f'</office:document-content>')


def ods_frame(number, name, x="0cm", y="0cm", width="12cm", height="8cm", href=None):
    href = f"./Object {number}" if href is None else href
    return (f'<draw:frame draw:name={quoteattr(name)} svg:x="{x}" svg:y="{y}" svg:width="{width}" svg:height="{height}">'
            f'<draw:object xlink:href={quoteattr(href)} xlink:type="simple"/></draw:frame>')


def ods_with_charts(sheets, charts, **builder_options):
    """A workbook of ``sheets`` ({name: rows XML}) with chart objects drawn on them.

    Each chart is ``{"sheet", "xml", "name", "x", "y", "width", "height"}`` (lengths as ODS writes them) plus
    optional ``cell`` (``(row, col)``, 1-based: the frame sits in that cell of a sheet whose own rows are empty;
    without it the frame is in ``table:shapes``), ``missing`` (no content.xml is written) and ``target`` (the
    frame's ``xlink:href``)."""
    parts, tables, number = {}, [], 0
    for name, rows in sheets.items():
        shapes, placed = [], ""
        for item in (c for c in charts if c["sheet"] == name):
            number += 1
            frame = ods_frame(number, item.get("name", f"Chart {number}"), item.get("x", "0cm"), item.get("y", "0cm"),
                              item.get("width", "12cm"), item.get("height", "8cm"), item.get("target"))
            if not item.get("missing"):
                parts[f"Object {number}/content.xml"] = item["xml"]
            if item.get("cell"):
                row, col = item["cell"]
                before = f'<table:table-cell table:number-columns-repeated="{col - 1}"/>' if col > 1 else ""
                skip = (f'<table:table-row table:number-rows-repeated="{row - 1}"><table:table-cell/></table:table-row>'
                        if row > 1 else "")
                placed += f"{skip}<table:table-row>{before}<table:table-cell>{frame}</table:table-cell></table:table-row>"
            else:
                shapes.append(frame)
        head = f"<table:shapes>{''.join(shapes)}</table:shapes>" if shapes else ""
        tables.append(f'<table:table table:name={quoteattr(name)}>{head}{rows}{placed}</table:table>')
    content = (f'<office:document-content {ODS_NAMESPACES}><office:automatic-styles/><office:body><office:spreadsheet>'
               f'{"".join(tables)}</office:spreadsheet></office:body></office:document-content>')
    extra = {**parts, **(builder_options.pop("extra_parts", None) or {})}
    return ods_workbook_bytes(sheets, content=content, extra_parts=extra, **builder_options)


def _ods_cell(value, frame=""):
    if value is None:
        return f"<table:table-cell>{frame}</table:table-cell>"
    if isinstance(value, str):
        return f'<table:table-cell office:value-type="string">{frame}<text:p>{escape(value)}</text:p></table:table-cell>'
    return f'<table:table-cell office:value-type="float" office:value="{value}">{frame}<text:p>{value}</text:p></table:table-cell>'


def _ods_rows(rows, frame):
    """Rows of values; ``frame`` is drawn in cell C1 (an empty cell when the first row is shorter)."""
    rows = [list(values) + [None] * (3 - len(values)) if number == 0 else values for number, values in enumerate(rows)]
    return "".join("<table:table-row>" + "".join(_ods_cell(value, frame if (number, col) == (0, 2) else "")
                                                for col, value in enumerate(values)) + "</table:table-row>"
                   for number, values in enumerate(rows))


def ods_sample_like():
    """The ODS counterpart of ``sample_like``: the same cells, and each sheet's Chart 1 drawn in C1, 66 and 16 pixels
    into the cell, at the size the XLSX anchor spans with default cell sizes."""
    first, second = SAMPLE_SHEETS
    one, four = _sample_series()
    quarters = [["Division", "Q1", "Q2", "Q3", "Q4"]] + [[f"Division {row}"] + [row * (col + 1) for col in range(4)]
                                                         for row in range(2, 9)]
    sheets = {first: [["Division", "Sales"]] + [list(item) for item in SAMPLE_SALES], second: quarters}
    charts = {first: ods_chart_xml("bar", "col", "clustered", one, "Sales by Division", "end"),
              second: ods_chart_xml("bar", "col", "stacked", four, "Sales by Quarter", "end")}
    rows = {name: _ods_rows(values, ods_frame(number, "Chart 1", "66px", "16px", "382px", "284px"))
            for number, (name, values) in enumerate(sheets.items(), 1)}
    parts = {f"Object {number}/content.xml": charts[name] for number, name in enumerate(sheets, 1)}
    return ods_with_charts(rows, [], extra_parts=parts)
