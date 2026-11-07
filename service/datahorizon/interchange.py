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


def local_formula(source, bounds=None):
    """The interchange tier preserves only implemented, local A1 formulas."""
    try:
        pending=[Parser(source[1:]).parse()]
        while pending:
            item=pending.pop()
            if item[0]=="call":
                require(item[1] in ("SUM","IF"),"UNSUPPORTED_FORMULA",f"Unsupported function: {item[1]}")
                pending.extend(item[2])
            elif item[0] in ("binary","range"): pending.extend(item[2:])
            elif item[0]=="unary": pending.append(item[2])
            elif item[0]=="ref":
                name,row,col=split_ref(item[1])
                require(name is None,"UNSUPPORTED_FORMULA","Cross-sheet references require whole-workbook interchange")
                if bounds is not None:
                    r1,c1,r2,c2=bounds
                    require(r1<=row<=r2 and c1<=col<=c2,"UNSUPPORTED_FORMULA","Formula references cells outside the exported selection; expand it or choose values")
    except FormulaError as error:
        raise EngineError("UNSUPPORTED_FORMULA",str(error)) from error
    return source


def text_decode(value):
    return re.sub(r"_x([0-9A-Fa-f]{4})_",lambda m:chr(int(m[1],16)),value)


def text_encode(value):
    value=re.sub(r"_x[0-9A-Fa-f]{4}_",lambda m:"_x005F_"+m[0][1:],value)
    return "".join(f"_x{ord(c):04X}_" if ord(c)<32 and c not in "\t\n" else c for c in value)


def xlsx_preview(request,checkpoint=lambda completed,phase:None):
    selected=integer(request.get("sheet_index",1),1,64,"sheet_index")
    selected_mode=mode(request)
    report=["Only the selected sheet's stored cells are imported; absent cells do not clear the destination.",
            "Formatting, dates' display formats, dimensions, other sheets and advanced features are omitted in this tier.",
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
        if "xl/sharedStrings.xml" in archive.namelist():
            for item in read_xml(archive,"xl/sharedStrings.xml"):
                shared.append(text_decode("".join(t.text or "" for t in item.iter(f"{{{S}}}t"))))
                require(len(shared)<=MAX_CELLS,"LIMIT","Shared strings exceed 10,000 entries")
        features=sorted({item.tag.rsplit("}",1)[-1] for item in root if item.tag!=f"{{{S}}}sheetData"})
        report.append("Worksheet features outside cell inputs: "+", ".join(features[:40]))
        advanced=[name for name in archive.namelist() if any(part in name.lower() for part in ("vba","externallink","drawing","chart","pivot","table","comment"))]
        if advanced: report.append(f"{len(advanced)} advanced package parts are omitted; no external resources are fetched.")
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
                    item={"source":local_formula("="+(formula.text or "")),"kind":"formula"}
                elif kind=="inlineStr":
                    item={"source":text_decode("".join(t.text or "" for t in cell.findall(f"{{{S}}}is//{{{S}}}t"))),"kind":"text"}
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
                typed({**item,"style":{}})
                edits.append({"row":row,"col":col,**item})
            except (EngineError,FormulaError,UnicodeError) as error:
                if len(issues)<100: issues.append({"cell":location,"message":str(error)[:200]})
        result={"cells":edits if not issues else [],"can_import":bool(edits) and not issues,"issues":issues,
                "sheet":sheets[selected-1].get("name",""),"sheets":[s.get("name","") for s in sheets],"report":report,"stored_cells":count}
        require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","Spreadsheet preview exceeds 4 MiB")
        if result["can_import"]:
            result["cells"]=import_edits(result,integer(request.get("row",1),1,1048576,"row"),integer(request.get("col",1),1,16384,"col"))
        return result


def xlsx_export(snapshot,request):
    selected_mode=mode(request)
    report=["Selection only, at original source coordinates, from one consistent native state.",
            "Formatting, other sheets, code, queries, relationships, history and view preferences are omitted.",
            "Native NULL becomes an empty cell. Formula semantics follow the target application's compatibility; formula caches are omitted and recalculation is requested."]
    root=ET.Element("worksheet",xmlns=S)
    data=node(root,"sheetData")
    current=None
    for cell in snapshot["cells"]:
        if cell["row"]!=current:
            current=cell["row"]; row=node(data,"row",r=str(current))
        out=node(row,"c",r=address(cell["row"],cell["col"]))
        if cell["kind"]=="formula" and selected_mode=="formulas":
            node(out,"f",local_formula(cell["source"],snapshot["range"])[1:])
        else:
            require(cell.get("calculation")=="current","CALCULATION_ERROR","Values export requires current, error-free values")
            value=cell.get("value")
            if type(value) is bool: out.set("t","b"); node(out,"v","1" if value else "0")
            elif type(value) in (int,float): node(out,"v",str(value))
            elif value is not None:
                out.set("t","inlineStr"); inline=node(out,"is")
                node(inline,"t",text_encode(value),**{"xml:space":"preserve"})
    workbook=ET.Element("workbook",xmlns=S,**{"xmlns:r":R})
    sheets=node(workbook,"sheets"); node(sheets,"sheet",name="Sheet1",sheetId="1",**{"r:id":"rId1"})
    node(workbook,"calcPr",fullCalcOnLoad="1",forceFullCalc="1")
    relationships=ET.Element("Relationships",xmlns=P)
    node(relationships,"Relationship",Id="rId1",Type=R+"/worksheet",Target="worksheets/sheet1.xml")
    mainrels=ET.Element("Relationships",xmlns=P)
    node(mainrels,"Relationship",Id="rId1",Type=R+"/officeDocument",Target="xl/workbook.xml")
    types=ET.Element("Types",xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
    node(types,"Default",Extension="rels",ContentType="application/vnd.openxmlformats-package.relationships+xml")
    node(types,"Default",Extension="xml",ContentType="application/xml")
    node(types,"Override",PartName="/xl/workbook.xml",ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
    node(types,"Override",PartName="/xl/worksheets/sheet1.xml",ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        for name,part in (("[Content_Types].xml",types),("_rels/.rels",mainrels),("xl/workbook.xml",workbook),
                          ("xl/_rels/workbook.xml.rels",relationships),("xl/worksheets/sheet1.xml",root)):
            archive.writestr(name,xml(part))
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
