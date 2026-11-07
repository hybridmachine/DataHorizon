"""Bounded spreadsheet packages; no extraction, external resolution or code activation."""
import base64
import binascii
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

from .engine import EngineError, MAX_CELLS, encoded, integer, require, typed
from .formula import FormulaError, Parser, address, coordinates, split_ref, translate

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
    require(value in ("formulas","values"),"INVALID_ARGUMENT","mode must be formulas or values")
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


def local_formula(source, bounds=None, sheet_names=None):
    """The interchange tier preserves only implemented, local A1 formulas."""
    require(not any((ord(c)<32 and c not in "\t\n\r") or ord(c) in (65534,65535) for c in source),
            "UNSUPPORTED_FORMULA","Formula contains characters unavailable in spreadsheet XML")
    try:
        pending=[Parser(source[1:]).parse()]
        while pending:
            item=pending.pop()
            if item[0]=="call":
                require(item[1] in ("SUM","IF"),"UNSUPPORTED_FORMULA",f"Unsupported function: {item[1]}")
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


def _xlsx_preview(request,checkpoint=lambda completed,phase:None,*,sheet_names=None):
    selected=integer(request.get("sheet_index",1),1,64,"sheet_index")
    selected_mode=mode(request)
    report=["Only the selected sheet's stored cells are imported; absent cells do not clear the destination.",
            "Dimensions, other sheets and advanced features are omitted; unsupported styles are listed below.",
            "Formulas recalculate with DataHorizon's declared compatibility tier; imported code is never activated."]
    if selected_mode=="values": report.append("Formula caches become literal values. Cache freshness cannot be verified; missing/error caches block import.")
    with package(request) as archive:
        checkpoint(0,"Reading workbook")
        workbook=read_xml(archive,"xl/workbook.xml")
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
        styles=XlsxStyles(archive,style_path or "xl/styles.xml")
        edits,issues,seen=[],[],set()
        count=0
        for cell in root.findall(f"{{{S}}}sheetData/{{{S}}}row/{{{S}}}c"):
            count+=1; checkpoint(count,"Reading cells")
            require(count<=MAX_CELLS,"LIMIT","Import exceeds 10,000 stored cells")
            location=cell.get("r","")
            try:
                row,col=coordinates(location)
                require((row,col) not in seen,"INVALID_SPREADSHEET","Duplicate cell coordinates")
                seen.add((row,col))
                formula=cell.find(f"{{{S}}}f")
                value=cell.findtext(f"{{{S}}}v")
                kind=cell.get("t","n")
                if formula is not None and selected_mode=="formulas":
                    require(not formula.attrib,"UNSUPPORTED_FORMULA","Shared, array and data-table formulas are unavailable")
                    item={"source":local_formula("="+(formula.text or ""),sheet_names=sheet_names),"kind":"formula"}
                elif kind=="inlineStr":
                    item={"source":xlsx_text(cell.find(f"{{{S}}}is")),"kind":"text"}
                elif kind=="s":
                    require(value is not None and re.fullmatch(r"[0-9]+",value) is not None and int(value)<len(shared),"INVALID_SPREADSHEET","Invalid shared string index")
                    item={"source":shared[int(value)],"kind":"text"}
                elif kind in ("str","d"):
                    require(value is not None,"INVALID_SPREADSHEET","Missing text/date value")
                    item={"source":text_decode(value),"kind":"text"}
                elif kind=="b":
                    require(value in ("0","1"),"INVALID_SPREADSHEET","Invalid Boolean value")
                    item={"source":"TRUE" if value=="1" else "FALSE","kind":"boolean"}
                elif kind=="n":
                    require(formula is None or value is not None,"MISSING_CACHE","Formula has no cached value")
                    item={"source":value or "","kind":"number" if value else "auto"}
                else: raise EngineError("UNSUPPORTED_CELL",f"Unsupported cell type: {kind}")
                item.update(style=styles.style(cell.get("s","0")),replace_style=True)
                typed(item)
                edits.append({"row":row,"col":col,**item})
            except (EngineError,FormulaError,UnicodeError) as error:
                if len(issues)<100: issues.append({"cell":location,"message":str(error)[:200]})
        report.extend(styles.report())
        result={"cells":edits if not issues else [],"can_import":bool(edits) and not issues,"issues":issues,
                "sheet":sheets[selected-1].get("name",""),"sheets":[s.get("name","") for s in sheets],"report":report,"stored_cells":count}
        require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","Spreadsheet preview exceeds 4 MiB")
        if result["can_import"]:
            result["cells"]=import_edits(result,integer(request.get("row",1),1,1048576,"row"),integer(request.get("col",1),1,16384,"col"))
        return result


def xlsx_export(snapshot,request):
    selected_mode=mode(request)
    report=["Selection only, at original source coordinates, from one consistent native state.",
            "Native cell styles are mapped. Other sheets, code, queries, relationships, history, dimensions and view preferences are omitted.",
            "Native NULL becomes an empty cell. Formula semantics follow the target application's compatibility; formula caches are omitted and recalculation is requested."]
    whole="sheets" in snapshot
    snapshots=snapshot["sheets"] if whole else [{**snapshot,"name":"Sheet1"}]
    names=validate_sheet_names([s["name"] for s in snapshots])
    if whole: report=workbook_report()+["Formula caches are omitted; target recalculation is requested."]
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
                node(out,"f",local_formula(cell["source"],None if whole else sheet["range"],names if whole else None)[1:])
            else:
                require(cell.get("calculation")=="current","CALCULATION_ERROR","Values export requires current, error-free values")
                value=cell.get("value")
                if type(value) is bool: out.set("t","b"); node(out,"v","1" if value else "0")
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
        if item["kind"]=="formula": item["source"]=translate(item["source"],row-1,col-1)
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
