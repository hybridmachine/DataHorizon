# SPDX-License-Identifier: GPL-3.0-only
"""Builds XLSX packages with drawings and charts for tests, on top of xlsx_builder.workbook_bytes."""
from xml.sax.saxutils import escape

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


def sample_like():
    """Two worksheets, each with a column chart anchored C1 (+632114, +154565 EMU) to J16, as in the sample workbook."""
    colours = '<c:spPr><a:solidFill><a:schemeClr val="accent1"/></a:solidFill></c:spPr>'
    labels = '<c:dLbls><c:showLegendKey val="0"/><c:showVal val="1"/><c:showCatName val="0"/></c:dLbls><c:gapWidth val="150"/>'
    first, second = "Column Chart", "Stacked Column"
    one = [{"name_ref": f"'{first}'!$B$1", "cat": f"'{first}'!$A$2:$A$8", "val": f"'{first}'!$B$2:$B$8", "sppr": colours}]
    four = [{"name_ref": f"'{second}'!${chr(66 + n)}$1", "cat": f"'{second}'!$A$2:$A$8",
             "val": f"'{second}'!${chr(66 + n)}$2:${chr(66 + n)}$8", "sppr": colours} for n in range(4)]
    anchor = two_cell((2, 632114, 0, 154565), (9, 0, 15, 0))
    return xlsx_with_charts(
        {first: _rows(["Division", "Sales"]), second: _rows(["Division", "Q1", "Q2", "Q3", "Q4"], width=5)},
        [{"sheet": first, "name": "Chart 1", "anchor": anchor,
          "xml": chart_xml("bar", "col", "clustered", one, rich_title("Sales by Division"), "r", extras=labels)},
         {"sheet": second, "name": "Chart 2", "anchor": anchor,
          "xml": chart_xml("bar", "col", "stacked", four, rich_title("Sales by Quarter"), "r",
                           extras=labels + '<c:overlap val="100"/>')}])
