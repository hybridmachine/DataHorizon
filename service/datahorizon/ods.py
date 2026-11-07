"""Bounded ODF 1.3 sheet interchange with explicit local formula/style translation."""
import base64
import io
import re
import zipfile
import xml.etree.ElementTree as ET

from .engine import EngineError, MAX_CELLS, encoded, integer, require, typed
from .formula import Parser, split_ref
from .interchange import import_edits, local_formula, mode, node, package, read_xml, xml, preparation_scope, prepare_workbook, validate_sheet_names, workbook_report

O="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
T="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
X="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
Y="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
F="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
N="urn:oasis:names:tc:opendocument:xmlns:datastyle:1.0"
M="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
OF="urn:oasis:names:tc:opendocument:xmlns:of:1.2"
MIME="application/vnd.oasis.opendocument.spreadsheet"
for prefix,uri in (("office",O),("table",T),("text",X),("style",Y),("fo",F),("number",N),("manifest",M)):
    ET.register_namespace(prefix,uri)


def q(uri,name): return f"{{{uri}}}{name}"


def repeat(element,name,maximum):
    value=element.get(q(T,name),"1")
    require(re.fullmatch(r"[1-9][0-9]{0,6}",value) is not None and int(value)<=maximum,
            "LIMIT","Invalid or excessive repeated spreadsheet extent")
    return int(value)


def ods_to_native(source,sheet_names=None):
    require(source.startswith("of:="),"UNSUPPORTED_FORMULA","Only the of:= OpenFormula prefix is supported")
    def endpoint(value):
        match=re.fullmatch(r"(\$?(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z_0-9]*))?\.(\$?[A-Za-z]{1,3}\$?[1-9][0-9]*)",value)
        require(match is not None,"UNSUPPORTED_FORMULA","Only bounded A1 OpenFormula references are supported")
        name=match[1]
        if name is None: return match[2]
        name=name.removeprefix("$")
        return name+"!"+match[2]
    parts=[]; pos=0
    for match in re.finditer(r'"(?:[^"]|"")*"|\[[^\]]*\]|[^"\[]+',source[4:]):
        require(match.start()==pos,"UNSUPPORTED_FORMULA","Invalid OpenFormula quoting")
        part=match[0]; pos=match.end()
        if part.startswith('"'): parts.append(part)
        elif part.startswith("["):
            refs=part[1:-1].split(":")
            require(1<=len(refs)<=2,"UNSUPPORTED_FORMULA","Unsupported OpenFormula range")
            parts.append(":".join(endpoint(value) for value in refs))
        else:
            require("^" not in part,"UNSUPPORTED_FORMULA","ODS power precedence is outside this formula tier")
            part=re.sub(r"\b(TRUE|FALSE)\(\)",lambda m:m[1].upper(),part,flags=re.I)
            parts.append(part.replace(";",","))
    require(pos==len(source)-4,"UNSUPPORTED_FORMULA","Invalid OpenFormula syntax")
    return local_formula("="+"".join(parts),sheet_names=sheet_names)


def native_to_ods(source,bounds=None,sheet_names=None):
    local_formula(source,bounds,sheet_names)
    def reference(value,inherited=None):
        name,_,_=split_ref(value)
        cell=value.rsplit("!",1)[-1]
        name=name if name is not None else inherited
        return ("$'"+name.replace("'","''")+"'" if name is not None else "")+"."+cell
    def render(item):
        kind=item[0]
        if kind=="literal":
            value=item[1]
            if isinstance(value,str): return '"'+value.replace('"','""')+'"'
            if type(value) is bool: return "TRUE()" if value else "FALSE()"
            return repr(value)
        if kind=="ref": return "["+reference(item[1])+"]"
        if kind=="call": return item[1]+"("+";".join(map(render,item[2]))+")"
        if kind=="binary":
            require(item[1]!="^","UNSUPPORTED_FORMULA","ODS power precedence is outside this formula tier")
            return "("+render(item[2])+item[1]+render(item[3])+")"
        if kind=="unary": return "("+item[1]+render(item[2])+")"
        if kind=="range":
            require(item[2][0]==item[3][0]=="ref","UNSUPPORTED_FORMULA","Only cell-reference range endpoints are supported")
            inherited=split_ref(item[2][1])[0]
            return "["+reference(item[2][1])+":"+reference(item[3][1],inherited)+"]"
        raise EngineError("UNSUPPORTED_FORMULA","Formula errors are outside ODS formula export")
    return "of:="+render(Parser(source[1:]).parse())


def ods_text(cell):
    if q(O,"string-value") in cell.attrib: return cell.get(q(O,"string-value"))
    def walk(item):
        yield False,item.text or ""
        for child in item:
            if child.tag==q(X,"s"):
                value=child.get(q(X,"c"),"1")
                require(re.fullmatch(r"[1-9][0-9]{0,4}",value) and int(value)<=65536,"LIMIT","Text space repetition exceeds a cell")
                yield True," "*int(value)
            elif child.tag==q(X,"tab"): yield True,"\t"
            elif child.tag==q(X,"line-break"): yield True,"\n"
            elif child.tag in (q(X,"span"),q(X,"a"),q(X,"meta"),q(X,"meta-field"),q(X,"ruby-base")):
                yield from walk(child)
            elif child.tag==q(X,"ruby"):
                for base in child.findall(q(X,"ruby-base")): yield from walk(base)
            yield False,child.tail or ""
    paragraphs=[]; total=0
    for paragraph in cell.findall(q(X,"p")):
        parts=[]; pending=False; started=False
        for explicit,value in walk(paragraph):
            total+=len(value.encode())
            require(total<=65536,"LIMIT","ODS text exceeds 64 KiB before whitespace normalization")
            if explicit:
                if pending and started: parts.append(" ")
                pending=False; parts.append(value); started=True
            else:
                for token in re.findall(r"[ \t\r\n]+|[^ \t\r\n]+",value):
                    if token[0] in " \t\r\n": pending=True
                    else:
                        if pending and started: parts.append(" ")
                        pending=False; parts.append(token); started=True
        paragraphs.append("".join(parts))
    value="\n".join(paragraphs)
    require(len(value.encode())<=65536,"LIMIT","ODS text exceeds 64 KiB")
    return value


class OdsStyles:
    def __init__(self,roots):
        self.styles={}; self.formats={}; self.losses=set(); self.cache={}
        for root in roots:
            for item in root.iter():
                if item.tag==q(Y,"style") and item.get(q(Y,"family"))=="table-cell": self.styles[item.get(q(Y,"name"))]=item
                if item.tag in (q(N,"number-style"),q(N,"text-style"),q(N,"date-style"),q(N,"time-style"),q(N,"currency-style"),q(N,"percentage-style")):
                    self.formats[item.get(q(Y,"name"))]=item
        require(len(self.styles)<=4096 and len(self.formats)<=4096,"LIMIT","ODS styles exceed 4,096 entries")

    def loss(self,message):
        if len(self.losses)<100: self.losses.add(message)

    def resolved(self,name):
        chain=[]; seen=set()
        while name is not None:
            require(name not in seen and len(chain)<32,"INVALID_SPREADSHEET","ODS style inheritance is cyclic or exceeds 32 levels")
            seen.add(name)
            item=self.styles.get(name)
            require(item is not None,"INVALID_SPREADSHEET","Missing ODS cell or parent style")
            chain.append(item); name=item.get(q(Y,"parent-style-name"))
        result=ET.Element(q(Y,"style")); props={}
        for item in reversed(chain):
            result.attrib.update(item.attrib)
            for child in item:
                if child.tag not in props: props[child.tag]=ET.SubElement(result,child.tag)
                props[child.tag].attrib.update(child.attrib)
        return result

    def style(self,name):
        if name is None: return {}
        if name in self.cache: return self.cache[name]
        item=self.resolved(name)
        result={}
        data_name=item.get(q(Y,"data-style-name"))
        if data_name:
            fmt=self.formats.get(data_name)
            require(fmt is not None,"INVALID_SPREADSHEET","Missing ODS number style")
            number=fmt.find(q(N,"number"))
            simple_number=number is not None and number.get(q(N,"decimal-places"))=="2" and number.get(q(N,"grouping"),"false")=="false" and number.get(q(N,"min-integer-digits"),"1")=="1" and set(number.attrib)<={q(N,"decimal-places"),q(N,"min-integer-digits"),q(N,"grouping")}
            if fmt.tag==q(N,"text-style") and len(fmt)==1 and fmt[0].tag==q(N,"text-content"): result["format"]="text"
            elif fmt.tag==q(N,"number-style") and len(fmt)==1 and simple_number: result["format"]="fixed2"
            elif fmt.tag==q(N,"percentage-style") and len(fmt)==2 and simple_number and fmt[0].tag==q(N,"number") and fmt[1].tag==q(N,"text") and fmt[1].text=="%": result["format"]="percent2"
            else: self.loss("Unsupported ODS number/date/currency display formats are omitted; typed values are retained.")
        for prop in item:
            if prop.tag==q(Y,"text-properties"):
                if prop.get(q(F,"font-weight"))=="bold": result["bold"]=True
                if prop.get(q(F,"font-style"))=="italic": result["italic"]=True
            elif prop.tag==q(Y,"paragraph-properties"):
                align=prop.get(q(F,"text-align"))
                if align in ("left","center","right","start","end"): result["align"]={"start":"left","end":"right"}.get(align,align)
                elif align: self.loss("Unsupported ODS alignment is omitted.")
            elif prop.tag==q(Y,"table-cell-properties"):
                color=prop.get(q(F,"background-color"))
                if color and re.fullmatch(r"#[0-9a-fA-F]{6}",color): result["background"]=color.lower()
                elif color not in (None,"transparent"): self.loss("Unsupported ODS fill is omitted.")
                if prop.get(q(F,"wrap-option"))=="wrap": result["wrap"]=True
                border=prop.get(q(F,"border"))
                if border and re.fullmatch(r"(?:0\.75pt|1px) solid #[0-9a-fA-F]{6}",border): result["border"]="box"
                elif border not in (None,"none"): self.loss("Unsupported ODS border thickness/pattern is omitted.")
                if any(key.rsplit("}",1)[-1].startswith("border-") for key in prop.attrib): self.loss("Partial ODS borders are omitted.")
        self.cache[name]=result
        return result


def _ods_preview(request,checkpoint=lambda completed,phase:None,*,sheet_names=None):
    selected=integer(request.get("sheet_index",1),1,64,"sheet_index"); selected_mode=mode(request)
    report=["Selected sheet stored cells only; absent cells leave the destination unchanged. Explicit input types and mapped direct/inherited cell styles replace destination inputs/styles.",
            "Other sheets, code, charts, comments, merged presentation, names, validation, conditional/default/row/column formatting, dimensions and advanced fonts are omitted; code never activates.",
            "Percentage/currency retain numeric magnitude. Supported two-decimal percentage styles map; currency codes and other unsupported display are omitted. Dates and durations become ISO Text. Formula syntax is translated canonically; only the published native function tier and arithmetic/comparison formulas without powers are supported."]
    if selected_mode=="values": report.append("Formula caches become values with unverified freshness; missing/error caches block Apply.")
    with package(request) as archive:
        try: require(archive.read("mimetype")==MIME.encode(),"INVALID_SPREADSHEET","Package is not an ODS spreadsheet")
        except KeyError as error: raise EngineError("INVALID_SPREADSHEET","Missing ODS mimetype") from error
        checkpoint(0,"Reading ODS")
        root=read_xml(archive,"content.xml")
        require(root.tag==q(O,"document-content"),"INVALID_SPREADSHEET","Invalid ODS content")
        sheets=root.findall(f'{q(O,"body")}/{q(O,"spreadsheet")}/{q(T,"table")}')
        require(0<len(sheets)<=64 and selected<=len(sheets),"INVALID_SPREADSHEET","Choose an existing ODS sheet")
        roots=([read_xml(archive,"styles.xml")] if "styles.xml" in archive.namelist() else [])+[root]
        styles=OdsStyles(roots)
        edits=[]; issues=[]; row=1; processed=0; staged_bytes=0
        def rows(parent):
            for item in parent:
                if item.tag==q(T,"table-row"): yield item
                elif item.tag in (q(T,"table-row-group"),q(T,"table-header-rows"),q(T,"table-rows")): yield from rows(item)
        for record in rows(sheets[selected-1]):
            row_count=repeat(record,"number-rows-repeated",1048576)
            require(row+row_count-1<=1048576,"LIMIT","ODS rows exceed the native grid")
            col=1; template=[]
            for cell in record:
                if cell.tag not in (q(T,"table-cell"),q(T,"covered-table-cell")): continue
                count=repeat(cell,"number-columns-repeated",16384)
                require(col+count-1<=16384,"LIMIT","ODS columns exceed the native grid")
                processed+=1; checkpoint(processed,"Reading ODS cells")
                kind=cell.get(q(O,"value-type")); formula=cell.get(q(T,"formula")); style_name=cell.get(q(T,"style-name"))
                try:
                    if cell.tag==q(T,"covered-table-cell"): col+=count; continue
                    meaningful=kind is not None or formula is not None or style_name is not None or bool(cell.findall(q(X,"p")))
                    if not meaningful: col+=count; continue
                    if formula is not None and selected_mode=="formulas":
                        require(not any("matrix" in key for key in cell.attrib),"UNSUPPORTED_FORMULA","ODS matrix formulas are unavailable")
                        item={"source":ods_to_native(formula,sheet_names),"kind":"formula"}
                    elif kind in ("float","percentage","currency"):
                        item={"source":cell.get(q(O,"value"),""),"kind":"number"}
                    elif kind=="boolean":
                        value=cell.get(q(O,"boolean-value"))
                        require(value in ("true","false"),"INVALID_SPREADSHEET","Invalid ODS Boolean")
                        item={"source":value.upper(),"kind":"boolean"}
                    elif kind in ("date","time"):
                        value=cell.get(q(O,"date-value") if kind=="date" else q(O,"time-value"))
                        require(value is not None,"INVALID_SPREADSHEET","Missing date/duration value")
                        item={"source":value,"kind":"text"}
                    elif kind=="string" or cell.findall(q(X,"p")):
                        item={"source":ods_text(cell),"kind":"text"}
                    else:
                        require(kind is None,"UNSUPPORTED_CELL","Unsupported ODS value type")
                        require(formula is None,"MISSING_CACHE","ODS formula has no supported cache")
                        item={"source":"","kind":"auto"}
                    if formula is not None and selected_mode=="values":
                        require(not any(key.rsplit("}",1)[-1]=="value-type" and value=="error" for key,value in cell.attrib.items())
                                and item["source"] not in ("#N/A","#VALUE!","#REF!","#DIV/0!","#NUM!","#NAME?","#NULL!"),
                                "MISSING_CACHE","ODS formula has an error cache")
                    item.update(style=styles.style(style_name),replace_style=True)
                    typed(item)
                    require(len(edits)+row_count*(len(template)+count)<=MAX_CELLS,"LIMIT","Repeated stored cells exceed 10,000")
                    staged_bytes+=row_count*count*(len(encoded(item).encode())+40)
                    require(staged_bytes<=3*1024*1024,"READ_LIMIT","Repeated ODS input payload exceeds 3 MiB")
                    template.extend({"col":col+offset,**item} for offset in range(count))
                except EngineError as error:
                    if error.body["code"] in ("LIMIT","READ_LIMIT"): raise
                    if len(issues)<100: issues.append({"cell":f"row {row}, column {col}","message":str(error)[:200]})
                col+=count
            for offset in range(row_count if template else 0): edits.extend({"row":row+offset,**item} for item in template)
            row+=row_count
        report.extend(sorted(styles.losses))
        result={"cells":edits if not issues else [],"stored_cells":len(edits),"can_import":bool(edits) and not issues,
                "issues":issues,"sheet":sheets[selected-1].get(q(T,"name"),""),"sheets":[s.get(q(T,"name"),"") for s in sheets],"report":report}
        if result["can_import"]: result["cells"]=import_edits(result,integer(request.get("row",1),1,1048576,"row"),integer(request.get("col",1),1,16384,"col"))
        require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","ODS preview exceeds 4 MiB")
        return result


def ods_export(snapshot,request):
    selected_mode=mode(request)
    report=["One selection at original coordinates from a consistent native state. Native direct styles map to ODS; formulas translate canonically and use current snapshot values as caches.",
            "Other sheets, code, queries, relationships, history, dimensions and view state are omitted. NULL becomes empty. Target formula/coercion compatibility can differ; retain the native workbook."]
    whole="sheets" in snapshot
    snapshots=snapshot["sheets"] if whole else [{**snapshot,"name":"Sheet1"}]
    names=validate_sheet_names([s["name"] for s in snapshots])
    if whole: report=workbook_report()+["OpenFormula uses canonical named sheet references and current snapshot caches; sheet-relative movement semantics are not retained. NULL becomes empty."]
    root=ET.Element(q(O,"document-content"),{q(O,"version"):"1.3","xmlns:of":OF})
    automatic=node(root,q(O,"automatic-styles"))
    number=node(automatic,q(N,"number-style"),**{q(Y,"name"):"N2"}); node(number,q(N,"number"),**{q(N,"decimal-places"):"2",q(N,"min-integer-digits"):"1"})
    textstyle=node(automatic,q(N,"text-style"),**{q(Y,"name"):"NT"}); node(textstyle,q(N,"text-content"))
    percentage=node(automatic,q(N,"percentage-style"),**{q(Y,"name"):"NP"})
    node(percentage,q(N,"number"),**{q(N,"decimal-places"):"2",q(N,"min-integer-digits"):"1"}); node(percentage,q(N,"text"),"%")
    style_ids={}
    for cell in [c for s in snapshots for c in s["cells"]]:
        style=cell.get("style",{}); key=encoded(style)
        if key in style_ids: continue
        require(len(style_ids)<1024,"LIMIT","ODS export exceeds 1,024 styles")
        name="S"+str(len(style_ids)); style_ids[key]=name
        attrs={q(Y,"name"):name,q(Y,"family"):"table-cell"}
        if style.get("format") in ("fixed2","text","percent2"): attrs[q(Y,"data-style-name")]={"fixed2":"N2","text":"NT","percent2":"NP"}[style["format"]]
        out=node(automatic,q(Y,"style"),**attrs)
        text={}; props={}; paragraph={}
        if style.get("bold"): text[q(F,"font-weight")]="bold"
        if style.get("italic"): text[q(F,"font-style")]="italic"
        if "align" in style: paragraph[q(F,"text-align")]=style["align"]
        if style.get("wrap"): props[q(F,"wrap-option")]="wrap"
        if "background" in style: props[q(F,"background-color")]=style["background"]
        if style.get("border")=="box": props[q(F,"border")]="0.75pt solid #000000"
        if props: node(out,q(Y,"table-cell-properties"),**props)
        if paragraph: node(out,q(Y,"paragraph-properties"),**paragraph)
        if text: node(out,q(Y,"text-properties"),**text)
    body=node(root,q(O,"body")); spreadsheet=node(body,q(O,"spreadsheet")); text_nodes=0
    for sheet in snapshots:
        table=node(spreadsheet,q(T,"table"),**{q(T,"name"):sheet["name"]})
        previous_row=0; previous_col=0; row_node=None
        for cell in sheet["cells"]:
            row,col=cell["row"],cell["col"]
            if row!=previous_row:
                if row-previous_row>1: node(table,q(T,"table-row"),**{q(T,"number-rows-repeated"):str(row-previous_row-1)})
                row_node=node(table,q(T,"table-row")); previous_row=row; previous_col=0
            if col-previous_col>1: node(row_node,q(T,"table-cell"),**{q(T,"number-columns-repeated"):str(col-previous_col-1)})
            previous_col=col
            out=node(row_node,q(T,"table-cell"),**{q(T,"style-name"):style_ids[encoded(cell.get("style",{}))]})
            require(cell.get("calculation")=="current","CALCULATION_ERROR","ODS export requires error-free current values")
            if cell["kind"]=="formula" and selected_mode=="formulas": out.set(q(T,"formula"),native_to_ods(cell["source"],None if whole else sheet["range"],names if whole else None))
            value=cell.get("value")
            if type(value) is bool: out.set(q(O,"value-type"),"boolean"); out.set(q(O,"boolean-value"),str(value).lower())
            elif type(value) in (float,int): out.set(q(O,"value-type"),"percentage" if cell.get("style",{}).get("format")=="percent2" else "float"); out.set(q(O,"value"),str(value))
            elif value is not None:
                require(not any((ord(c)<32 and c not in "\t\n\r") or ord(c) in (65534,65535) for c in value),"UNSUPPORTED_CELL","ODS cannot represent this cell's XML control characters")
                out.set(q(O,"value-type"),"string"); out.set(q(O,"string-value"),value)
                paragraph=node(out,q(X,"p"))
                # Explicit whitespace elements preserve presentation as well as the typed value.
                for segment in re.split(r"([ \t\r\n]+)",value):
                    if not segment: continue
                    if segment[0] in " \t\r\n":
                        for run in re.findall(r" +|[^ ]",segment):
                            text_nodes+=1
                            require(text_nodes<=50000,"LIMIT","ODS text structure exceeds 50,000 whitespace nodes")
                            if run[0]==" ": node(paragraph,q(X,"s"),**{q(X,"c"):str(len(run))})
                            elif run=="\t": node(paragraph,q(X,"tab"))
                            else: node(paragraph,q(X,"line-break"))
                    else:
                        if len(paragraph): paragraph[-1].tail=(paragraph[-1].tail or "")+segment
                        else: paragraph.text=(paragraph.text or "")+segment
    styles=ET.Element(q(O,"document-styles"),{q(O,"version"):"1.3"}); node(styles,q(O,"styles"))
    manifest=ET.Element(q(M,"manifest"),{q(M,"version"):"1.3"})
    for name,mime in (("/",MIME),("content.xml","text/xml"),("styles.xml","text/xml")):
        node(manifest,q(M,"file-entry"),**{q(M,"full-path"):name,q(M,"media-type"):mime})
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype",MIME,compress_type=zipfile.ZIP_STORED)
        parts=[(name,xml(part)) for name,part in (("content.xml",root),("styles.xml",styles),("META-INF/manifest.xml",manifest))]
        require(sum(len(data) for _,data in parts)<=16*1024*1024,"LIMIT","Expanded ODS export exceeds 16 MiB")
        for name,data in parts: archive.writestr(name,data)
    result={"file_base64":base64.b64encode(output.getvalue()).decode(),"state":snapshot["state"],"report":report}
    require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","ODS export exceeds 4 MiB")
    return result


def ods_preview(request,checkpoint=lambda completed,phase:None):
    if preparation_scope(request)!="workbook": return _ods_preview(request,checkpoint)
    with package(request) as archive:
        root=read_xml(archive,"content.xml")
        names=[s.get(q(T,"name"),"") for s in root.findall(f'{q(O,"body")}/{q(O,"spreadsheet")}/{q(T,"table")}')]
    result=prepare_workbook(request,checkpoint,names,_ods_preview)
    result["report"].append("Date/duration values become ISO Text. Percentage/currency retain magnitudes; two-decimal percentage styles map. Currency codes and other unsupported display are omitted. Named sheet references replace sheet-relative movement semantics.")
    return result
