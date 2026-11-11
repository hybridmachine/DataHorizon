# SPDX-License-Identifier: GPL-3.0-only
"""Bounded ODF 1.3 sheet interchange with explicit local formula/style translation."""
import base64
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .engine import EngineError, MAX_CELLS, encoded, integer, require, typed, validate_style
from .formula import Parser, FormulaError, address, split_ref, translate
from .borders import SIDES, border_edges, parse_ods_border, ODS_BORDERS
from .formats import numeric_spec, simple_number_style
from .temporal import Temporal, from_duration, to_duration, export_temporal_styles, import_temporal
from .conversion_report import ConversionReport
from .formula_admission import status as formula_status
from .interchange import MAX_SOURCE_BYTES, map_formula_functions, import_edits, local_formula, mode, native_only_report, node, package, read_xml, xml, preparation_scope, prepare_workbook, validate_sheet_names, workbook_report

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


TEMPORAL_PARTS={
    "date_iso":("year","-","month","-","day"),
    "date_mdy":("month","/","day","/","year"),
    "date_dmy":("day","/","month","/","year"),
    "time24":("hours",":","minutes",":","seconds"),
    "datetime_iso":("year","-","month","-","day"," ","hours",":","minutes",":","seconds"),
}


def temporal_format(fmt):
    if fmt.tag not in (q(N,"date-style"),q(N,"time-style")):return None
    parts=tuple(child.text if child.tag==q(N,"text") else child.tag.removeprefix("{"+N+"}") for child in fmt)
    if any(child.get(q(N,"textual"))=="true" for child in fmt):return None
    return next((mode for mode,pattern in TEMPORAL_PARTS.items() if parts==pattern),None)


def repeat(element,name,maximum):
    value=element.get(q(T,name),"1")
    require(re.fullmatch(r"[1-9][0-9]{0,6}",value) is not None and int(value)<=maximum,
            "LIMIT","Invalid or excessive repeated spreadsheet extent")
    return int(value)


def ods_to_native(source,sheet_names=None,*,strict=True):
    """Translate OpenFormula syntax; strict also demands functions and sheets DataHorizon can evaluate."""
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
    native=map_formula_functions("="+"".join(parts),{"COM.MICROSOFT.CONCAT":"CONCAT","COM.MICROSOFT.XLOOKUP":"XLOOKUP","COM.MICROSOFT.TEXTJOIN":"TEXTJOIN"})
    return local_formula(native,sheet_names=sheet_names) if strict else native


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
        if kind=="call": return {"CONCAT":"COM.MICROSOFT.CONCAT","XLOOKUP":"COM.MICROSOFT.XLOOKUP","TEXTJOIN":"COM.MICROSOFT.TEXTJOIN"}.get(item[1],item[1])+"("+";".join(map(render,item[2]))+")"
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
    def __init__(self,roots,report=None):
        self.styles={}; self.formats={};self.fonts={}; self.cache={}; self.recorded={}
        self.report=report if report is not None else ConversionReport()
        self._where=(None,None); self._count=1; self._current=[]
        for root in roots:
            for item in root.iter():
                if item.tag==q(Y,"font-face"):self.fonts[item.get(q(Y,"name"))]=item.get("{urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0}font-family","")
                if item.tag==q(Y,"style") and item.get(q(Y,"family"))=="table-cell": self.styles[item.get(q(Y,"name"))]=item
                if item.tag in (q(N,"number-style"),q(N,"text-style"),q(N,"date-style"),q(N,"time-style"),q(N,"currency-style"),q(N,"percentage-style")):
                    self.formats[item.get(q(Y,"name"))]=item
        require(len(self.styles)<=4096 and len(self.formats)<=4096 and len(self.fonts)<=4096,"LIMIT","ODS styles exceed 4,096 entries")

    def loss(self,category,detail,severity="omitted"):
        """Record one style loss for the cell being resolved (and for later cells sharing the style)."""
        if (category,severity,detail) in self._current: return
        self._current.append((category,severity,detail))
        self.report.add(category,severity,detail,sheet=self._where[0],cell=self._where[1],count=self._count)

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

    def style(self,name,*,sheet=None,cell=None,count=1):
        """The native style for a cell style name; its losses are reported once per cell that uses it."""
        if name is None: return {}
        if name in self.cache:
            for category,severity,detail in self.recorded[name]: self.report.add(category,severity,detail,sheet=sheet,cell=cell,count=count)
            return self.cache[name]
        self._where=(sheet,cell); self._count=count; self._current=[]
        self.cache[name]=result=self._resolve(name)
        self.recorded[name]=self._current
        return result

    def _resolve(self,name):
        item=self.resolved(name)
        result={}
        data_name=item.get(q(Y,"data-style-name"))
        if data_name:
            fmt=self.formats.get(data_name)
            require(fmt is not None,"INVALID_SPREADSHEET","Missing ODS number style")
            number=fmt.find(q(N,"number"))
            simple_number=number is not None and re.fullmatch(r"[0-9]|1[0-5]",number.get(q(N,"decimal-places"),"")) is not None and number.get(q(N,"grouping"),"false") in ("false","true") and number.get(q(N,"min-integer-digits"),"1")=="1" and set(number.attrib)<={q(N,"decimal-places"),q(N,"min-integer-digits"),q(N,"grouping")}
            if temporal_format(fmt):result["format"]=temporal_format(fmt)
            elif fmt.tag==q(N,"text-style") and len(fmt)==1 and fmt[0].tag==q(N,"text-content"): result["format"]="text"
            elif fmt.tag==q(N,"number-style") and len(fmt)==1 and simple_number: result.update(simple_number_style(("#,##0" if number.get(q(N,"grouping"))=="true" else "0")+("."+"0"*int(number.get(q(N,"decimal-places"))) if int(number.get(q(N,"decimal-places"))) else "")))
            elif fmt.tag==q(N,"percentage-style") and len(fmt)==2 and simple_number and fmt[0].tag==q(N,"number") and fmt[1].tag==q(N,"text") and fmt[1].text=="%": result.update(simple_number_style(("#,##0" if number.get(q(N,"grouping"))=="true" else "0")+("."+"0"*int(number.get(q(N,"decimal-places"))) if int(number.get(q(N,"decimal-places"))) else "")+"%"))
            elif fmt.tag==q(N,"currency-style") and len(fmt)==3 and simple_number and fmt[0].tag==q(N,"number") and fmt[1].tag==q(N,"text") and fmt[1].text==" " and fmt[2].tag==q(N,"currency-symbol") and re.fullmatch(r"[A-Z]{3}",fmt[2].text or ""):
                result.update(simple_number_style(("#,##0" if number.get(q(N,"grouping"))=="true" else "0")+("."+"0"*int(number.get(q(N,"decimal-places"))) if int(number.get(q(N,"decimal-places"))) else "")+'" '+fmt[2].text+'"'))
            else: self.loss("style.number_format","Unsupported ODS number/date/currency display formats are omitted; typed values are retained.")
        for prop in item:
            if prop.tag==q(Y,"text-properties"):
                if prop.get(q(F,"font-weight"))=="bold": result["bold"]=True
                if prop.get(q(F,"font-style"))=="italic": result["italic"]=True
                family=prop.get(q(F,"font-family"))
                if prop.get(q(Y,"font-name")) is not None:family=self.fonts.get(prop.get(q(Y,"font-name")),"")
                if family and len(family)>=2 and family[0]==family[-1] and family[0] in "\"'":family=family[1:-1]
                size=prop.get(q(F,"font-size"));color=prop.get(q(F,"color"))
                for key,value in (("font_family",family),("font_size",size),("text_color",color)):
                    if value is None:continue
                    if key=="font_size":value=int(float(value[:-2])) if re.fullmatch(r"[0-9]{1,3}(?:\.0{1,8})?pt",value) else None
                    try:validate_style({key:value})
                    except EngineError:self.loss("style.font",f"Unsupported ODS {key} is omitted; only a single portable family, integer 6–72pt and RGB color map.")
                    else:result[key]=value.lower() if key=="text_color" else value
            elif prop.tag==q(Y,"paragraph-properties"):
                align=prop.get(q(F,"text-align"))
                if align in ("left","center","right","start","end"): result["align"]={"start":"left","end":"right"}.get(align,align)
                elif align: self.loss("style.alignment","Unsupported ODS alignment is omitted.")
            elif prop.tag==q(Y,"table-cell-properties"):
                color=prop.get(q(F,"background-color"))
                if color and re.fullmatch(r"#[0-9a-fA-F]{6}",color): result["background"]=color.lower()
                elif color not in (None,"transparent"): self.loss("style.fill","Unsupported ODS fill is omitted.")
                if prop.get(q(F,"wrap-option"))=="wrap": result["wrap"]=True
                base=prop.get(q(F,"border"));base_style=parse_ods_border(base);edges={side:base_style or "none" for side in SIDES};has_border=base is not None
                if base is not None and base_style is None:self.loss("style.border","Unsupported ODS border thickness/pattern is omitted.")
                for side in SIDES:
                    value=prop.get(q(F,"border-"+side))
                    if value is None:continue
                    has_border=True;mapped=parse_ods_border(value)
                    if mapped is None:self.loss("style.border","Unsupported ODS border thickness/pattern is omitted.")
                    else:edges[side]=mapped
                if all(value=="thin" for value in edges.values()):result["border"]="box"
                else:
                    for side,value in edges.items():
                        if value!="none":result["border_"+side]=value
                if has_border and any(v!="none" for v in edges.values()):self.loss("style.border","ODS border colors map to the native default color.","approximated")
        return result


ODS_ERRORS=("#N/A","#VALUE!","#REF!","#DIV/0!","#NUM!","#NAME?","#NULL!")
NUMERIC_TYPES=("float","percentage","currency")


class _Unusable(Exception):
    """A stored value that cannot become a native input; carries the report entry."""
    def __init__(self,category,detail=""):
        super().__init__(category)
        self.category,self.detail=category,detail


@dataclass
class OdsCellContext:
    """Everything one cell conversion needs; row, col and count place the cell being converted."""
    mode: str
    styles: object
    sheet_names: object
    sheet: str
    report: ConversionReport
    timezone_policy: str = "reject"
    shift: tuple = (0, 0)
    row: int = 1
    col: int = 1
    count: int = 1


def _stored_value(cell,kind,formula,timezone_policy):
    """The native input a cell's own value (or a formula's cache) describes, and whether it is an error value."""
    if kind in NUMERIC_TYPES:
        value=cell.get(q(O,"value"))
        if not value: raise _Unusable("cell.invalid_value","missing value")
        return {"source":value,"kind":"number"},False
    if kind=="boolean":
        value=cell.get(q(O,"boolean-value"))
        if value not in ("true","false"): raise _Unusable("cell.invalid_value","boolean")
        return {"source":value.upper(),"kind":"boolean"},False
    if kind in ("date","time"):
        value=cell.get(q(O,"date-value") if kind=="date" else q(O,"time-value"))
        if value is None: raise _Unusable("cell.invalid_value","missing value")
        try: temporal=from_duration(value) if kind=="time" else import_temporal(value,"datetime" if "T" in value or " " in value else "date",timezone_policy)
        except (ValueError,OverflowError): raise _Unusable("cell.invalid_value","date/time") from None
        return {"source":str(temporal),"kind":temporal.kind},False
    if kind not in (None,"void","string"): raise _Unusable("cell.unsupported_type",kind[:32])
    if kind=="string" or cell.findall(q(X,"p")):
        try: text=ods_text(cell)
        except EngineError as error:
            if error.body["code"]!="LIMIT": raise
            raise _Unusable("cell.text_too_long") from None
        marker=any(key.rsplit("}",1)[-1]=="value-type" and value=="error" for key,value in cell.attrib.items())
        return {"source":text,"kind":"text"},marker or (formula is not None and text in ODS_ERRORS)
    if formula is not None: raise _Unusable("cell.missing_cache")
    return {"source":"","kind":"auto"},False


def convert_ods_cell(cell,*,context):
    """Convert one stored table:table-cell to an edit, or None when it is skipped (with a report entry).

    context.row/col/count say where the cell sits and how many identical cells it stands for.
    Raises EngineError only when the package itself is malformed."""
    report,sheet,mode_,count=context.report,context.sheet,context.mode,context.count
    location=address(context.row,context.col)

    def skip(category,detail=""):
        report.add(category,"omitted",detail,sheet=sheet,cell=location,count=count)

    kind=cell.get(q(O,"value-type")); formula=cell.get(q(T,"formula"))
    try: value,is_error=_stored_value(cell,kind,formula,context.timezone_policy); unusable=None
    except _Unusable as error: value=is_error=None; unusable=error
    cached=formula is not None and value is not None and not is_error
    noted=[]
    item=None
    if formula is not None and mode_!="values":
        names=context.sheet_names
        matrix=any("matrix" in key for key in cell.attrib)
        problem=("formula.array","") if matrix else None
        try: native=ods_to_native(formula,names,strict=False)
        except EngineError:
            native=None
            if not matrix: problem=("formula.unsupported_syntax","")
        if native is not None and not matrix: problem=formula_status(native,names)
        if problem is not None and problem[0]=="formula.unsupported_syntax":
            # An untranslated OpenFormula string is not a native formula: keep it as readable text unless a value is usable.
            if cached and mode_!="formulas": noted.append((problem[0],"converted",problem[1]))
            else:
                item={"source":formula,"kind":"text"}
                noted.append((problem[0],"converted",problem[1]))
        elif cached and (native is None or (mode_=="mixed" and problem is not None)):
            noted.append((problem[0],"converted",problem[1]))
        elif native is None:
            skip(*problem); return None
        else:
            if problem is not None: noted.append((problem[0],"unevaluable",problem[1]))
            if context.shift!=(0,0):
                try: translate(native,*context.shift)
                except FormulaError:
                    # import_edits could not move this source; omit it here, where the report is still open.
                    skip("formula.untranslatable"); return None
            item={"source":native,"kind":"formula"}
    if item is None:
        if value is None: skip(unusable.category,unusable.detail); return None
        item=dict(value)
        if is_error: noted.append(("cell.error_value","converted",""))
    if item["kind"] in ("text","formula"):
        # Same limit typed() enforces on managed cell source.
        if len(item["source"].encode("utf-8","surrogatepass"))>MAX_SOURCE_BYTES:
            skip("cell.text_too_long","formula source" if item["kind"]=="formula" else ""); return None
        try: typed({**item,"style":{}})
        except EngineError: skip("cell.invalid_value","rejected by the engine"); return None
    elif item["kind"]=="number":
        try: typed({**item,"style":{}})
        except EngineError:
            item={"source":item["source"],"kind":"text"}
            noted.append(("cell.number_out_of_range","converted",""))
    item.update(style=context.styles.style(cell.get(q(T,"style-name")),sheet=sheet,cell=location,count=count),replace_style=True)
    typed(item)
    for category,severity,detail in noted: report.add(category,severity,detail,sheet=sheet,cell=location,count=count)
    return {"row":context.row,"col":context.col,**item}


def _ods_preview(request,checkpoint=lambda completed,phase:None,*,sheet_names=None):
    selected=integer(request.get("sheet_index",1),1,64,"sheet_index"); selected_mode=mode(request)
    require(request.get("timezone_policy","reject") in ("reject","utc"),"INVALID_ARGUMENT","Choose reject or utc timestamp policy")
    report=["Selected sheet stored cells only; absent cells leave the destination unchanged. Explicit input types and mapped direct/inherited cell styles replace destination inputs/styles.",
            "Other sheets, code, charts, comments, merged presentation, names, validation, conditional/default/row/column formatting, dimensions and advanced typography are omitted; code never activates.",
            "Percentage/currency retain numeric magnitude. Number/percentage styles with 0–15 decimal places map; explicit three-letter currency suffix styles map; other display formats are omitted. Supported dates/datetimes and sub-day time durations restore native ISO types. Formula syntax is translated canonically; formulas in the published native function tier without powers evaluate, and any other formula is kept, replaced or omitted as listed below."]
    if selected_mode=="values": report.append("Formula caches become literal values with unverified freshness; formulas without a usable cache are omitted.")
    if selected_mode=="mixed": report.append("Formulas DataHorizon can evaluate stay formulas; those it cannot become their cached values when a usable cache exists. Cache freshness cannot be verified.")
    with package(request) as archive:
        try: require(archive.read("mimetype")==MIME.encode(),"INVALID_SPREADSHEET","Package is not an ODS spreadsheet")
        except KeyError as error: raise EngineError("INVALID_SPREADSHEET","Missing ODS mimetype") from error
        checkpoint(0,"Reading ODS")
        root=read_xml(archive,"content.xml")
        require(root.tag==q(O,"document-content"),"INVALID_SPREADSHEET","Invalid ODS content")
        sheets=root.findall(f'{q(O,"body")}/{q(O,"spreadsheet")}/{q(T,"table")}')
        require(0<len(sheets)<=64 and selected<=len(sheets),"INVALID_SPREADSHEET","Choose an existing ODS sheet")
        roots=([read_xml(archive,"styles.xml")] if "styles.xml" in archive.namelist() else [])+[root]
        conversions=ConversionReport()
        styles=OdsStyles(roots,conversions)
        report.append("ISO shorthand normalizes to native seconds; explicit timezone_policy=utc normalizes offset timestamps to UTC civil values and omits the original offset.")
        edits=[]; row=1; processed=0; staged_bytes=0
        shift=(integer(request.get("row",1),1,1048576,"row")-1,integer(request.get("col",1),1,16384,"col")-1)
        sheet_label=sheets[selected-1].get(q(T,"name"),"")
        context=OdsCellContext(mode=selected_mode,styles=styles,sheet_names=sheet_names,sheet=sheet_label,report=conversions,
                               timezone_policy=request.get("timezone_policy","reject"),shift=shift)
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
                meaningful=(cell.get(q(O,"value-type")) is not None or cell.get(q(T,"formula")) is not None
                            or cell.get(q(T,"style-name")) is not None or bool(cell.findall(q(X,"p"))))
                if cell.tag==q(T,"table-cell") and meaningful:
                    context.row,context.col,context.count=row,col,row_count*count
                    edit=convert_ods_cell(cell,context=context)
                    if edit is not None:
                        require(len(edits)+row_count*(len(template)+count)<=MAX_CELLS,"LIMIT","Repeated stored cells exceed 10,000")
                        staged_bytes+=row_count*count*(len(encoded(edit).encode())+40)
                        require(staged_bytes<=3*1024*1024,"READ_LIMIT","Repeated ODS input payload exceeds 3 MiB")
                        template.extend({**edit,"col":col+offset} for offset in range(count))
                col+=count
            for offset in range(row_count if template else 0): edits.extend({**item,"row":row+offset} for item in template)
            row+=row_count
        report.extend(conversions.lines())
        result={"cells":edits,"stored_cells":len(edits),"can_import":bool(edits),"issues":[],"sheet":sheet_label,
                "sheets":[s.get(q(T,"name"),"") for s in sheets],"report":report,"report_entries":conversions.entries(),
                "totals":conversions.totals()}
        if result["can_import"]: result["cells"]=import_edits(result,integer(request.get("row",1),1,1048576,"row"),integer(request.get("col",1),1,16384,"col"))
        require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","ODS preview exceeds 4 MiB")
        if sheet_names is not None: result["_conversions"]=conversions
        return result


def ods_export(snapshot,request):
    selected_mode=mode(request)
    require(selected_mode!="mixed","INVALID_ARGUMENT","mode mixed applies to import; export formulas or values")
    require(request.get("timezone_policy","reject") in ("reject","utc"),"INVALID_ARGUMENT","Choose reject or utc timestamp policy")
    report=["One selection at original coordinates from a consistent native state. Native direct styles map to ODS; formulas translate canonically and use current snapshot values as caches.",
            "Other sheets, code, queries, relationships, history, dimensions and view state are omitted. NULL becomes empty. Target formula/coercion compatibility can differ; retain the native workbook."]
    whole="sheets" in snapshot
    snapshots=export_temporal_styles(snapshot["sheets"] if whole else [{**snapshot,"name":"Sheet1"}])
    names=validate_sheet_names([s["name"] for s in snapshots])
    if whole: report=workbook_report()+["OpenFormula uses canonical named sheet references and current snapshot caches; sheet-relative movement semantics are not retained. NULL becomes empty."]
    if snapshot.get('native_omissions'):report.append('Native relational objects omitted from this sheet export: '+', '.join(snapshot['native_omissions']))
    report.extend(native_only_report(snapshot))
    report.append("Native dates/datetimes use ISO office:date-value; times use nonnegative sub-day office:time-value durations. Date/time formats map to invariant calendar styles; default temporal displays become ISO styles. Unsupported durations/offsets reject import. Target volatile clocks and serial-date arithmetic/type coercions can differ; choose values to freeze native results.")
    if selected_mode=="formulas" and any(map_formula_functions(c["source"],{"CONCAT":"COM.MICROSOFT.CONCAT","XLOOKUP":"COM.MICROSOFT.XLOOKUP","TEXTJOIN":"COM.MICROSOFT.TEXTJOIN"})!=c["source"] for sheet in snapshots for c in sheet["cells"] if c["kind"]=="formula"):report.append("Known modern functions use COM.MICROSOFT extension identifiers; target support is required. Choose values for other targets.")
    root=ET.Element(q(O,"document-content"),{q(O,"version"):"1.3","xmlns:of":OF})
    automatic=node(root,q(O,"automatic-styles"))
    number=node(automatic,q(N,"number-style"),**{q(Y,"name"):"N2"}); node(number,q(N,"number"),**{q(N,"decimal-places"):"2",q(N,"min-integer-digits"):"1"})
    textstyle=node(automatic,q(N,"text-style"),**{q(Y,"name"):"NT"}); node(textstyle,q(N,"text-content"))
    percentage=node(automatic,q(N,"percentage-style"),**{q(Y,"name"):"NP"})
    node(percentage,q(N,"number"),**{q(N,"decimal-places"):"2",q(N,"min-integer-digits"):"1"}); node(percentage,q(N,"text"),"%")
    style_ids={}; numeric_ids={}
    for cell in [c for s in snapshots for c in s["cells"]]:
        style=cell.get("style",{}); key=encoded(style)
        if key in style_ids: continue
        require(len(style_ids)<1024,"LIMIT","ODS export exceeds 1,024 styles")
        name="S"+str(len(style_ids)); style_ids[key]=name
        attrs={q(Y,"name"):name,q(Y,"family"):"table-cell"}
        temporal_mode=style.get("format")
        if temporal_mode in TEMPORAL_PARTS:
            data_name="DT"+str(len(style_ids));fmt=node(automatic,q(N,"time-style" if temporal_mode=="time24" else "date-style"),**{q(Y,"name"):data_name})
            for part in TEMPORAL_PARTS[temporal_mode]:
                if part in ("-","/",":"," "):node(fmt,q(N,"text"),part)
                else:node(fmt,q(N,part),**{q(N,"style"):"long"})
            attrs[q(Y,"data-style-name")]=data_name
        if style.get("format") in ("fixed2","text","percent2"): attrs[q(Y,"data-style-name")]={"fixed2":"N2","text":"NT","percent2":"NP"}[style["format"]]
        spec=numeric_spec(style)
        if spec:
            if spec not in numeric_ids:
                mode_name,places,grouped,currency=spec; data_name="NF"+str(len(numeric_ids));numeric_ids[spec]=data_name
                fmt=node(automatic,q(N,"percentage-style" if mode_name=="percent" else "currency-style" if mode_name=="currency" else "number-style"),**{q(Y,"name"):data_name})
                node(fmt,q(N,"number"),**{q(N,"decimal-places"):str(places),q(N,"min-integer-digits"):"1",q(N,"grouping"):str(grouped).lower()})
                if mode_name=="percent": node(fmt,q(N,"text"),"%")
                elif currency: node(fmt,q(N,"text")," ");node(fmt,q(N,"currency-symbol"),currency)
            attrs[q(Y,"data-style-name")]=numeric_ids[spec]
        out=node(automatic,q(Y,"style"),**attrs)
        text={}; props={}; paragraph={}
        if "font_family" in style:text[q(F,"font-family")]=style["font_family"]
        if "font_size" in style:text[q(F,"font-size")]=str(style["font_size"])+"pt"
        if "text_color" in style:text[q(F,"color")]=style["text_color"]
        if style.get("bold"): text[q(F,"font-weight")]="bold"
        if style.get("italic"): text[q(F,"font-style")]="italic"
        if "align" in style: paragraph[q(F,"text-align")]=style["align"]
        if style.get("wrap"): props[q(F,"wrap-option")]="wrap"
        if "background" in style: props[q(F,"background-color")]=style["background"]
        if "border" in style or any("border_"+side in style for side in SIDES):
            for side,value in border_edges(style).items():props[q(F,"border-"+side)]=ODS_BORDERS[value]
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
            temporal_kind=cell.get("value_kind",cell.get("kind"))
            if isinstance(value,Temporal) or temporal_kind in ("date","time","datetime"):
                value=Temporal(str(value),value.kind if isinstance(value,Temporal) else temporal_kind)
                clock=value.kind=="time"
                out.set(q(O,"value-type"),"time" if clock else "date");out.set(q(O,"time-value") if clock else q(O,"date-value"),to_duration(value) if clock else str(value))
            elif type(value) is bool: out.set(q(O,"value-type"),"boolean"); out.set(q(O,"boolean-value"),str(value).lower())
            elif type(value) in (float,int):
                spec=numeric_spec(cell.get("style",{}));mode_name=spec[0] if spec else "number"
                out.set(q(O,"value-type"),{"percent":"percentage","currency":"currency"}.get(mode_name,"float"));out.set(q(O,"value"),str(value))
                if mode_name=="currency":out.set(q(O,"currency"),spec[3])
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
    result["report"].append("Supported dates/datetimes and sub-day time durations restore native ISO types. Percentage/currency retain magnitudes; supported 0–15 decimal places, grouping and three-letter currency suffix styles map. Other currency/display metadata is omitted. Named sheet references replace sheet-relative movement semantics.")
    return result
