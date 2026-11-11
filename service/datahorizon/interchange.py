# SPDX-License-Identifier: GPL-3.0-only
"""Bounded spreadsheet packages; no extraction, external resolution or code activation."""
import base64
import binascii
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .engine import EngineError, MAX_CELLS, encoded, integer, require, typed
from .formula import SUPPORTED_FUNCTIONS, FormulaError, Parser, address, coordinates, split_ref, translate
from .temporal import Temporal, from_serial, export_temporal_styles, import_temporal
from .formats import TEMPORAL_FORMATS
from .conversion_report import ConversionReport
from .formula_admission import status as formula_status
from .xlsx_future_functions import PREFIX as FUTURE_PREFIX, strip_prefix

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_PACKAGE = 3*1024*1024
MAX_EXPANDED = 16*1024*1024


def node(parent, tag, text=None, **attrs):
    item=ET.SubElement(parent,tag,attrs)
    item.text=text
    return item


def xml(root):
    return ET.tostring(root,encoding="utf-8",xml_declaration=True)


def mode(request):
    value=request.get("mode","formulas")
    require(value in ("formulas","mixed","values"),"INVALID_ARGUMENT","mode must be formulas, mixed or values")
    return value


def package(request):
    value=request.get("file_base64")
    require(isinstance(value,str) and len(value)<=4*1024*1024,"LIMIT","Spreadsheet package must be at most 3 MiB")
    try:
        data=base64.b64decode(value,validate=True)
        require(len(data)<=MAX_PACKAGE,"LIMIT","Spreadsheet package exceeds 3 MiB")
        archive=zipfile.ZipFile(io.BytesIO(data))
        items=archive.infolist()
        require(len(items)<=128 and len({i.filename for i in items})==len(items),"INVALID_SPREADSHEET","Package has too many or duplicate entries")
        require(sum(i.file_size for i in items)<=MAX_EXPANDED,"LIMIT","Expanded spreadsheet exceeds 16 MiB")
        require(all(not i.flag_bits & 1 and i.compress_type in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED) for i in items),
                "INVALID_SPREADSHEET","Encrypted or unsupported package compression")
        return archive
    except (ValueError,binascii.Error,zipfile.BadZipFile) as error:
        raise EngineError("INVALID_SPREADSHEET","Invalid spreadsheet ZIP/base64 data") from error


def read_xml(archive,path):
    try:
        data=archive.read(path)
        # Deliberately support UTF-8 XML only; never permit declarations/entities.
        require(b"\0" not in data and b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper(),
                "INVALID_SPREADSHEET","UTF-8 XML without DTDs or entities is required")
        parser=ET.XMLPullParser(events=("start","end"))
        root=None; count=0; depth=0
        for offset in range(0,len(data),65536):
            parser.feed(data[offset:offset+65536])
            for event,item in parser.read_events():
                depth+=1 if event=="start" else -1
                if event=="start":
                    count+=1
                    require(count<=100000 and depth<=64,"LIMIT","XML structure exceeds the supported bounds")
                    if root is None: root=item
        parser.close()
        return root
    except (KeyError,ET.ParseError,zipfile.BadZipFile,RuntimeError,NotImplementedError) as error:
        raise EngineError("INVALID_SPREADSHEET",f"Invalid or missing XML part: {path}") from error


def related_part(relationships,kind):
    candidates=[item for item in relationships if item.get("Type")==R+"/"+kind]
    require(len(candidates)<=1,"INVALID_SPREADSHEET","Duplicate workbook part relationships")
    if not candidates: return None
    item=candidates[0]
    require(item.get("TargetMode")!="External","INVALID_SPREADSHEET","Workbook data parts must be local")
    target=item.get("Target","")
    path=posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/"+target)
    require(path.startswith("xl/") and "\\" not in path,"INVALID_SPREADSHEET","Invalid workbook part target")
    return path


FUNCTION_SPELLING=re.compile(r'"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'|([A-Za-z_][A-Za-z_0-9.]*)(?=\s*\()')


def map_formula_functions(source,mapping):
    """Map only function identifiers, leaving strings and quoted sheet names intact."""
    return FUNCTION_SPELLING.sub(lambda match:mapping.get(match[1].upper(),match[0]) if match[1] else match[0],source)


def xlsx_formula_to_native(source):
    """Drop Excel's future-function prefixes; whether the calculator can evaluate the result is formula_admission's call."""
    return strip_prefix(source)


def native_formula_to_xlsx(source,bounds=None,sheet_names=None):
    # A function the calculator lacks is stored verbatim, so it still exports; everything else stays strict.
    if (formula_status(source,sheet_names) or ("",))[0]=="formula.unsupported_function":
        try: Parser(source[1:]).parse()
        except FormulaError:
            # A spelling the parser cannot read (e.g. STDEV.S) cannot be bounds-checked: only whole-workbook export passes it through.
            if bounds is None: return map_formula_functions(source,FUTURE_PREFIX)
        return map_formula_functions(local_formula(source,bounds,sheet_names,allow_unsupported_functions=True),FUTURE_PREFIX)
    return map_formula_functions(local_formula(source,bounds,sheet_names),FUTURE_PREFIX)


def local_formula(source, bounds=None, sheet_names=None, *, allow_unsupported_functions=False):
    """Strict check for export: only implemented, local A1 formulas inside the selection."""
    require(not any((ord(c)<32 and c not in "\t\n\r") or ord(c) in (65534,65535) for c in source),
            "UNSUPPORTED_FORMULA","Formula contains characters unavailable in spreadsheet XML")
    try:
        pending=[Parser(source[1:]).parse()]
        while pending:
            item=pending.pop()
            if item[0]=="call":
                require(allow_unsupported_functions or item[1] in SUPPORTED_FUNCTIONS,"UNSUPPORTED_FORMULA",f"Unsupported function: {item[1]}")
                pending.extend(item[2])
            elif item[0] in ("binary","range"):
                if item[0]=="range":
                    require(item[2][0]==item[3][0]=="ref","UNSUPPORTED_FORMULA","Only cell-reference range endpoints are supported")
                    left=split_ref(item[2][1])[0]; right=split_ref(item[3][1])[0]
                    require(right is None or (left is not None and fold_name(left)==fold_name(right)),"UNSUPPORTED_FORMULA","Ranges spanning different sheets are unavailable")
                pending.extend(item[2:])
            elif item[0]=="unary": pending.append(item[2])
            elif item[0]=="ref":
                name,row,col=split_ref(item[1])
                require(name is None or (sheet_names is not None and fold_name(name) in {fold_name(n) for n in sheet_names}),
                        "UNSUPPORTED_FORMULA","Reference requires a sheet in the imported/exported workbook")
                if bounds is not None:
                    r1,c1,r2,c2=bounds
                    require(r1<=row<=r2 and c1<=col<=c2,"UNSUPPORTED_FORMULA","Formula references cells outside the exported selection; expand it or choose values")
    except FormulaError as error:
        raise EngineError("UNSUPPORTED_FORMULA",str(error)) from error
    return source


def text_decode(value):
    text=re.sub(r"_x([0-9A-Fa-f]{4})_",lambda m:chr(int(m[1],16)),value)
    return text.encode("utf-16-le","surrogatepass").decode("utf-16-le")


def text_encode(value):
    value=re.sub(r"_x[0-9A-Fa-f]{4}_",lambda m:"_x005F_"+m[0][1:],value)
    return "".join(f"_x{ord(c):04X}_" if (ord(c)<32 and c not in "\t\n") or ord(c) in (65534,65535) else c for c in value)


def xlsx_text(item):
    if item is None: return ""
    return text_decode("".join((child.text or "") if child.tag==f"{{{S}}}t" else
                       (child.findtext(f"{{{S}}}t") or "") if child.tag==f"{{{S}}}r" else "" for child in item))


class SharedFormulas:
    """Expand only stored shared members, never a group's potentially empty rectangle."""
    def __init__(self,cells):
        self.groups={}
        for cell in cells:
            formula=cell.find(f"{{{S}}}f")
            if formula is None or formula.get("t")!="shared" or not formula.text: continue
            key=formula.get("si","")
            require(re.fullmatch(r"[0-9]{1,8}",key) is not None,"INVALID_SPREADSHEET","Invalid shared formula group")
            require(key not in self.groups,"INVALID_SPREADSHEET","Duplicate shared formula master")
            ends=formula.get("ref","").split(":")
            require(1<=len(ends)<=2,"INVALID_SPREADSHEET","Invalid shared formula extent")
            try:
                r1,c1=coordinates(ends[0]); r2,c2=coordinates(ends[-1]); row,col=coordinates(cell.get("r",""))
            except FormulaError as error: raise EngineError("INVALID_SPREADSHEET","Invalid shared formula coordinates") from error
            require(r1<=row<=r2 and c1<=col<=c2 and (r2-r1+1)*(c2-c1+1)<=MAX_CELLS,
                    "LIMIT","Shared formula extent must contain its master and at most 10,000 cells")
            source="="+formula.text
            self.groups[key]=(row,col,(r1,c1,r2,c2),source)

    def source(self,cell,formula):
        kind=formula.get("t","normal")
        require(formula.get("ca","0") in ("0","1","false","true"),"INVALID_SPREADSHEET","Invalid formula calculation flag")
        if kind=="normal": return "="+(formula.text or "")
        require(kind=="shared","INVALID_SPREADSHEET","Unknown formula type")
        key=formula.get("si","")
        require(key in self.groups,"INVALID_SPREADSHEET","Shared formula has no master")
        origin_row,origin_col,(r1,c1,r2,c2),source=self.groups[key]
        row,col=coordinates(cell.get("r",""))
        require(r1<=row<=r2 and c1<=col<=c2,"INVALID_SPREADSHEET","Shared formula member lies outside its master extent")
        require(formula.text or "ref" not in formula.attrib,"INVALID_SPREADSHEET","Only the shared master may declare an extent")
        return translate(source,row-origin_row,col-origin_col)


STYLE_SCOPE_LINES=[
    "Mapped direct cell styles replace destination styles: general/Text/number/percentage/explicit currency labels with 0–15 decimal places, portable font family/size/RGB color, bold/italic, alignment, wrapping, solid RGB fill and individual solid/dashed/dotted border edges.",
    "Rich-text runs, named-style inheritance, conditional formatting, row/column defaults and dimensions are omitted."]


@dataclass
class XlsxCellContext:
    """Everything one cell conversion needs; a streaming reader builds the same object."""
    mode: str
    shared_strings: list
    styles: object
    date_system: str
    sheet_names: object
    sheet: str
    report: ConversionReport
    shared_formulas: object = None
    timezone_policy: str = "reject"
    shift: tuple = (0, 0)
    seen: set = field(default_factory=set)


def convert_xlsx_cell(cell,*,context):
    """Convert one stored <c> to an edit, or None when it is skipped (with a report entry).

    Raises EngineError only when the package itself is malformed."""
    report,sheet,mode_=context.report,context.sheet,context.mode
    location=cell.get("r","")
    try: row,col=coordinates(location)
    except FormulaError as error: raise EngineError("INVALID_SPREADSHEET","Invalid cell reference") from error
    require((row,col) not in context.seen,"INVALID_SPREADSHEET","Duplicate cell coordinates")
    context.seen.add((row,col))

    def skip(category,detail=""):
        report.add(category,"omitted",detail,sheet=sheet,cell=location)

    formula=cell.find(f"{{{S}}}f")
    value=cell.findtext(f"{{{S}}}v")
    kind=cell.get("t","n")
    cached=value is not None and kind!="e"
    noted=[]
    item=None
    if formula is not None:
        formula_type=formula.get("t","normal")
        require(formula_type in ("normal","shared","array","dataTable"),"INVALID_SPREADSHEET","Unknown formula type")
    if formula is not None and mode_!="values":
        native=None
        if formula_type in ("array","dataTable"):
            problem=("formula.array" if formula_type=="array" else "formula.data_table","")
            if formula.text: native=xlsx_formula_to_native("="+formula.text)
        else:
            try:
                native=xlsx_formula_to_native(context.shared_formulas.source(cell,formula))
                problem=formula_status(native,context.sheet_names)
            except FormulaError: problem=("formula.untranslatable","")
        if cached and (native is None or (mode_=="mixed" and problem is not None)):
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
        if mode_=="values" and formula is not None and value is None and kind!="inlineStr":
            skip("cell.missing_cache"); return None
        try:
            if kind=="e":
                if value is None: skip("cell.invalid_value","error cell without a value"); return None
                item={"source":value,"kind":"text"}
                noted.append(("cell.error_value","converted",""))
            elif kind=="inlineStr": item={"source":xlsx_text(cell.find(f"{{{S}}}is")),"kind":"text"}
            elif kind=="s":
                require(value is not None and re.fullmatch(r"[0-9]+",value) is not None and int(value)<len(context.shared_strings),"INVALID_SPREADSHEET","Invalid shared string index")
                item={"source":context.shared_strings[int(value)],"kind":"text"}
            elif kind=="d":
                if value is None: skip("cell.invalid_value","date cell without a value"); return None
                temporal=import_temporal(value,"datetime" if "T" in value or " " in value else "date" if "-" in value else "time",context.timezone_policy)
                item={"source":str(temporal),"kind":temporal.kind}
            elif kind=="str":
                if value is None: skip("cell.invalid_value","text cell without a value"); return None
                item={"source":text_decode(value),"kind":"text"}
            elif kind=="b":
                if value not in ("0","1"): skip("cell.invalid_value","Boolean cell without 0 or 1"); return None
                item={"source":"TRUE" if value=="1" else "FALSE","kind":"boolean"}
            elif kind=="n": item={"source":value or "","kind":"number" if value else "auto"}
            else: skip("cell.unsupported_type",kind); return None
        except (ValueError,UnicodeError,OverflowError) as error:
            skip("cell.invalid_value",str(error)); return None
    if item["kind"] in ("text","formula"):
        try: typed({**item,"style":{}})
        except EngineError:
            # The engine's managed source limit decides; nothing else can fail for text or a formula.
            skip("cell.text_too_long","formula source" if item["kind"]=="formula" else ""); return None
    elif item["kind"]=="number":
        try: typed({**item,"style":{}})
        except EngineError:
            item={"source":item["source"],"kind":"text"}
            noted.append(("cell.number_out_of_range","converted",""))
    style=context.styles.style(cell.get("s","0"),sheet=sheet,cell=location)
    if item["kind"]=="number" and style.get("format") in TEMPORAL_FORMATS:
        temporal_mode=style["format"]
        try:
            temporal=from_serial(float(item["source"]),"time" if temporal_mode=="time24" else "datetime" if temporal_mode=="datetime_iso" else "date",context.date_system)
            # A date display can conceal a non-midnight clock; retain it.
            if temporal_mode.startswith("date_"):
                complete=from_serial(float(item["source"]),"datetime",context.date_system)
                if complete.parsed().time().isoformat()!="00:00:00":temporal=complete
        except (ValueError,OverflowError):
            style={k:v for k,v in style.items() if k!="format"}
            noted.append(("cell.date_conversion","approximated","date format dropped; the number is kept"))
        else: item.update(source=str(temporal),kind=temporal.kind)
    item.update(style=style,replace_style=True)
    typed(item)
    for category,severity,detail in noted: report.add(category,severity,detail,sheet=sheet,cell=location)
    return {"row":row,"col":col,**item}


def _xlsx_preview(request,checkpoint=lambda completed,phase:None,*,sheet_names=None):
    selected=integer(request.get("sheet_index",1),1,64,"sheet_index")
    selected_mode=mode(request)
    require(request.get("timezone_policy","reject") in ("reject","utc"),"INVALID_ARGUMENT","Choose reject or utc timestamp policy")
    report=["Only the selected sheet's stored cells are imported; absent cells do not clear the destination.",
            "Dimensions, other sheets and advanced features are omitted; unsupported styles are listed below.",
            "Formulas recalculate with DataHorizon's declared compatibility tier; imported code is never activated."]
    if selected_mode=="values": report.append("Formula caches become literal values. Cache freshness cannot be verified; missing/error caches block import.")
    if selected_mode=="mixed": report.append("Formulas DataHorizon can evaluate stay formulas; those it cannot become their cached values when a usable cache exists. Cache freshness cannot be verified.")
    with package(request) as archive:
        checkpoint(0,"Reading workbook")
        workbook=read_xml(archive,"xl/workbook.xml")
        properties=workbook.find(f"{{{S}}}workbookPr")
        epoch=properties.get("date1904","0") if properties is not None else "0"
        require(epoch in ("0","1","false","true"),"INVALID_SPREADSHEET","Invalid XLSX date1904 flag")
        system="1904" if epoch in ("1","true") else "1900"
        report.append("ISO shorthand is normalized to native seconds; timezone offsets require explicit timezone_policy=utc, which stores UTC civil values and omits the original offset.")
        report.append(f"Date/time serials with supported formats use the {system} epoch; serial 60 in the 1900 system is rejected. Formula numeric literals are not epoch-shifted; native formulas use the 1900 system.")
        require(workbook.tag==f"{{{S}}}workbook","INVALID_SPREADSHEET","Only transitional SpreadsheetML XLSX is supported")
        sheets=workbook.findall(f"{{{S}}}sheets/{{{S}}}sheet")
        require(0<len(sheets)<=64 and selected<=len(sheets),"INVALID_SPREADSHEET","Choose an existing sheet (at most 64)")
        relationships=read_xml(archive,"xl/_rels/workbook.xml.rels")
        mapping={item.get("Id"):item for item in relationships}
        rel=mapping.get(sheets[selected-1].get(f"{{{R}}}id"))
        require(rel is not None and rel.get("TargetMode")!="External" and rel.get("Type")==R+"/worksheet",
                "INVALID_SPREADSHEET","Selected sheet must have a local worksheet relationship")
        target=rel.get("Target","")
        path=posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/"+target)
        require(path.startswith("xl/") and "\\" not in path,"INVALID_SPREADSHEET","Invalid worksheet target")
        root=read_xml(archive,path)
        require(root.tag==f"{{{S}}}worksheet","INVALID_SPREADSHEET","Invalid worksheet")
        shared=[]
        shared_path=related_part(relationships,"sharedStrings") or ("xl/sharedStrings.xml" if "xl/sharedStrings.xml" in archive.namelist() else None)
        if shared_path:
            for item in read_xml(archive,shared_path):
                shared.append(xlsx_text(item))
                require(len(shared)<=MAX_CELLS,"LIMIT","Shared strings exceed 10,000 entries")
        features=sorted({item.tag.rsplit("}",1)[-1] for item in root if item.tag!=f"{{{S}}}sheetData"})
        report.append("Worksheet features outside cell inputs: "+", ".join(features[:40]))
        advanced=[name for name in archive.namelist() if any(part in name.lower() for part in ("vba","externallink","drawing","chart","pivot","table","comment"))]
        if advanced: report.append(f"{len(advanced)} advanced package parts are omitted; no external resources are fetched.")
        from .xlsx_styles import XlsxStyles
        style_path=related_part(relationships,"styles")
        require(style_path is None or style_path in archive.namelist(),"INVALID_SPREADSHEET","Missing related stylesheet")
        conversions=ConversionReport()
        styles=XlsxStyles(archive,style_path or "xl/styles.xml",conversions)
        edits,issues=[],[]
        shift=(integer(request.get("row",1),1,1048576,"row")-1,integer(request.get("col",1),1,16384,"col")-1)
        sheet_label=sheets[selected-1].get("name","")
        count=0
        stored=root.findall(f"{{{S}}}sheetData/{{{S}}}row/{{{S}}}c")
        require(len(stored)<=MAX_CELLS,"LIMIT","Import exceeds 10,000 stored cells")
        # Shared-formula structure is package structure: a broken group rejects the file.
        shared_formulas=SharedFormulas(stored) if selected_mode!="values" else None
        context=XlsxCellContext(mode=selected_mode,shared_strings=shared,styles=styles,date_system=system,
                                sheet_names=sheet_names,sheet=sheet_label,report=conversions,
                                shared_formulas=shared_formulas,timezone_policy=request.get("timezone_policy","reject"),shift=shift)
        for cell in stored:
            count+=1; checkpoint(count,"Reading cells")
            require(count<=MAX_CELLS,"LIMIT","Import exceeds 10,000 stored cells")
            edit=convert_xlsx_cell(cell,context=context)
            if edit is not None: edits.append(edit)
        if shared_formulas and shared_formulas.groups: report.append("Shared formulas expand to explicit per-cell sources with relative/absolute reference translation; caches are discarded.")
        report.extend(STYLE_SCOPE_LINES)
        report.extend(conversions.lines())
        result={"cells":edits,"can_import":bool(edits),"issues":issues,
                "sheet":sheet_label,"sheets":[s.get("name","") for s in sheets],"report":report,"report_entries":conversions.entries(),
                "totals":conversions.totals(),"stored_cells":count}
        require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","Spreadsheet preview exceeds 4 MiB")
        if result["can_import"]:
            result["cells"]=import_edits(result,integer(request.get("row",1),1,1048576,"row"),integer(request.get("col",1),1,16384,"col"))
        return result


def native_only_report(snapshot):
    """Report lines for workbook names and event bindings, which no interchange format carries."""
    report=[]
    if snapshot.get('native_names'):report.append('Workbook names are native-only and omitted from this export: '+', '.join(snapshot['native_names']))
    if snapshot.get('native_bindings'):report.append('Event bindings are native-only and omitted from this export: '+', '.join(snapshot['native_bindings']))
    return report


def xlsx_export(snapshot,request):
    selected_mode=mode(request)
    require(selected_mode!="mixed","INVALID_ARGUMENT","mode mixed applies to import; export formulas or values")
    report=["Selection only, at original source coordinates, from one consistent native state.",
            "Native cell styles are mapped. Other sheets, code, queries, relationships, history, dimensions and view preferences are omitted.",
            "Native NULL becomes an empty cell. Formula semantics follow the target application's compatibility; formula caches are omitted and recalculation is requested."]
    whole="sheets" in snapshot
    snapshots=export_temporal_styles(snapshot["sheets"] if whole else [{**snapshot,"name":"Sheet1"}])
    names=validate_sheet_names([s["name"] for s in snapshots])
    if whole: report=workbook_report()+["Formula caches are omitted; target recalculation is requested."]
    if snapshot.get('native_omissions'):report.append('Native relational objects omitted from this sheet export: '+', '.join(snapshot['native_omissions']))
    report.extend(native_only_report(snapshot))
    report.append("Typed dates/times export as ISO XLSX date cells with native precision. Supported numeric date formats are interpreted as serials on import. Target readers may reduce subsecond precision; volatile functions recalculate using the target clock/timezone. Native civil arithmetic and pre-March-1900/typed comparisons may differ from target serial semantics; choose values to freeze results.")
    if selected_mode=="formulas" and any(map_formula_functions(c["source"],FUTURE_PREFIX)!=c["source"] for sheet in snapshots for c in sheet["cells"] if c["kind"]=="formula"):report.append("Known modern functions use their _xlfn identifiers; target function/version support is required. Choose values for older targets.")
    from .xlsx_styles import export_styles
    styles,style_ids=export_styles([c for s in snapshots for c in s["cells"]])
    worksheets=[]
    for sheet in snapshots:
        root=ET.Element("worksheet",xmlns=S)
        data=node(root,"sheetData")
        current=None
        for cell in sheet["cells"]:
            if cell["row"]!=current:
                current=cell["row"]; row=node(data,"row",r=str(current))
            out=node(row,"c",r=address(cell["row"],cell["col"]),s=str(style_ids[encoded(cell.get("style",{}))]))
            if cell["kind"]=="formula" and selected_mode=="formulas":
                node(out,"f",native_formula_to_xlsx(cell["source"],None if whole else sheet["range"],names if whole else None)[1:])
            else:
                require(cell.get("calculation")=="current","CALCULATION_ERROR","Values export requires current, error-free values")
                value=cell.get("value")
                temporal_kind=cell.get("value_kind",cell.get("kind"))
                if isinstance(value,Temporal) or temporal_kind in ("date","time","datetime"):
                    value=Temporal(str(value),value.kind if isinstance(value,Temporal) else temporal_kind)
                    out.set("t","d");node(out,"v",str(value))
                elif type(value) is bool: out.set("t","b"); node(out,"v","1" if value else "0")
                elif type(value) in (int,float): node(out,"v",str(value))
                elif value is not None:
                    require(len(value.encode("utf-16-le"))//2<=32767,"LIMIT","XLSX text exceeds Excel's 32,767-character cell limit")
                    out.set("t","inlineStr"); inline=node(out,"is")
                    node(inline,"t",text_encode(value),**{"xml:space":"preserve"})
        worksheets.append(root)
    workbook=ET.Element("workbook",xmlns=S,**{"xmlns:r":R})
    sheets=node(workbook,"sheets")
    for index,name in enumerate(names,1): node(sheets,"sheet",name=name,sheetId=str(index),**{"r:id":"rId"+str(index)})
    node(workbook,"calcPr",fullCalcOnLoad="1",forceFullCalc="1")
    relationships=ET.Element("Relationships",xmlns=P)
    for index in range(1,len(names)+1): node(relationships,"Relationship",Id="rId"+str(index),Type=R+"/worksheet",Target=f"worksheets/sheet{index}.xml")
    node(relationships,"Relationship",Id="styles",Type=R+"/styles",Target="styles.xml")
    mainrels=ET.Element("Relationships",xmlns=P)
    node(mainrels,"Relationship",Id="rId1",Type=R+"/officeDocument",Target="xl/workbook.xml")
    types=ET.Element("Types",xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
    node(types,"Default",Extension="rels",ContentType="application/vnd.openxmlformats-package.relationships+xml")
    node(types,"Default",Extension="xml",ContentType="application/xml")
    node(types,"Override",PartName="/xl/workbook.xml",ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
    for index in range(1,len(names)+1): node(types,"Override",PartName=f"/xl/worksheets/sheet{index}.xml",ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
    node(types,"Override",PartName="/xl/styles.xml",ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml")
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        parts=[("[Content_Types].xml",types),("_rels/.rels",mainrels),("xl/workbook.xml",workbook),
               ("xl/_rels/workbook.xml.rels",relationships),("xl/styles.xml",styles)]
        parts.extend((f"xl/worksheets/sheet{index}.xml",part) for index,part in enumerate(worksheets,1))
        payload=[(name,xml(part)) for name,part in parts]
        require(sum(len(data) for _,data in payload)<=MAX_EXPANDED,"LIMIT","Expanded XLSX export exceeds 16 MiB")
        for name,data in payload: archive.writestr(name,data)
    result={"file_base64":base64.b64encode(output.getvalue()).decode(),"state":snapshot["state"],"report":report}
    require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","Export response exceeds 4 MiB")
    return result


def import_edits(preview,row=1,col=1):
    """Offset reviewed sparse inputs once; the caller submits one managed edit."""
    require(preview.get("can_import") is True,"IMPORT_BLOCKED","Correct import issues before Apply")
    edits=[]
    for original in preview["cells"]:
        item={**original,"row":original["row"]+row-1,"col":original["col"]+col-1}
        integer(item["row"],1,1048576,"destination row")
        integer(item["col"],1,16384,"destination column")
        if item["kind"]=="formula" and (row,col)!=(1,1):
            try: item["source"]=translate(item["source"],row-1,col-1)
            except FormulaError as error: raise EngineError("UNSUPPORTED_FORMULA",f"Formula at row {original['row']}, column {original['col']} cannot be moved to another destination: {error}") from error
        typed({**item,"style":{}})
        edits.append(item)
    return edits


def fold_name(name):
    # SQLite NOCASE is ASCII-only, unlike str.casefold().
    return name.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"))


def validate_sheet_names(names):
    from .engine import Workbook
    require(0<len(names)<=64,"LIMIT","Provide 1-64 sheets")
    for name in names:
        Workbook.check_name(name)
        require(not any(ord(c) in (65534,65535) for c in name),"INVALID_NAME","Sheet names contain characters unavailable in spreadsheet XML")
        require(name.strip() and not name.startswith("'") and not name.endswith("'"),"INVALID_NAME","Spreadsheet sheet names cannot be blank or start/end with apostrophes")
    require(len({fold_name(n) for n in names})==len(names),"INVALID_NAME","Duplicate sheet names are unavailable")
    return names


def workbook_report():
    return ["All sheets, names, stored inputs and mapped direct cell styles from one consistent workbook state.",
            "Whole-workbook import replaces sheets only in an empty destination, as one atomic operation; history is cleared. Required handlers reject until structure events are defined.",
            "Code, queries, relationships, history, dimensions, view state and unsupported spreadsheet features are omitted. No code activates; retain the native/source file."]


def preparation_scope(request):
    value=request.get("scope","selection")
    require(value in ("selection","workbook"),"INVALID_ARGUMENT","scope must be selection or workbook")
    return value


def prepare_workbook(request,checkpoint,names,prepare):
    validate_sheet_names(names)
    require(request.get("row",1)==1 and request.get("col",1)==1,"INVALID_ARGUMENT","Whole-workbook import uses original coordinates")
    result={"scope":"workbook","workbook_sheets":[],"stored_cells":0,"issues":[],"report":workbook_report(),"can_import":True}
    size=0
    for index,name in enumerate(names,1):
        checkpoint(index-1,"Reading sheet "+name)
        part=prepare({**request,"scope":"selection","sheet_index":index},checkpoint,sheet_names=names)
        result["stored_cells"]+=part["stored_cells"]
        require(result["stored_cells"]<=MAX_CELLS,"LIMIT","Whole-workbook import exceeds 10,000 stored cells")
        size+=len(encoded(part["cells"]).encode())
        require(size<=3*1024*1024,"READ_LIMIT","Workbook input payload exceeds 3 MiB")
        result["workbook_sheets"].append({"name":name,"cells":part["cells"]})
        result["issues"].extend({**issue,"sheet":name} for issue in part["issues"][:max(0,100-len(result["issues"]))])
        # Keep per-sheet conversion details, removing single-selection scope statements.
        result["report"].extend(name+": "+line for line in part["report"][3:])
    if result["issues"]:
        result.update(can_import=False,workbook_sheets=[])
    require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","Workbook preview exceeds 4 MiB")
    return result


def xlsx_preview(request,checkpoint=lambda completed,phase:None):
    if preparation_scope(request)!="workbook": return _xlsx_preview(request,checkpoint)
    with package(request) as archive:
        root=read_xml(archive,"xl/workbook.xml")
        names=[s.get("name","") for s in root.findall(f"{{{S}}}sheets/{{{S}}}sheet")]
    return prepare_workbook(request,checkpoint,names,_xlsx_preview)
