# SPDX-License-Identifier: GPL-3.0-only
"""Bounded spreadsheet packages; no extraction, external resolution or code activation."""
import base64
import binascii
import heapq
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


@dataclass(frozen=True)
class Namespaces:
    """The OOXML namespace set of one workbook: transitional or strict (ISO 29500)."""
    main: str      # SpreadsheetML main
    rel: str       # r:id attribute namespace and base of relationship Type URIs
    doc_rel: str   # officeDocument relationships (identical to rel in both variants)


TRANSITIONAL = Namespaces(S, R, R)
STRICT = Namespaces("http://purl.oclc.org/ooxml/spreadsheetml/main",
                    "http://purl.oclc.org/ooxml/officeDocument/relationships",
                    "http://purl.oclc.org/ooxml/officeDocument/relationships")


def namespaces(workbook_root):
    """The namespace set named by the workbook root's tag; anything else is not a workbook."""
    for ns in (TRANSITIONAL, STRICT):
        if workbook_root.tag == f"{{{ns.main}}}workbook": return ns
    raise EngineError("INVALID_SPREADSHEET", "Workbook root is not a SpreadsheetML workbook (transitional or strict)")
MAX_PACKAGE = 3*1024*1024
MAX_EXPANDED = 16*1024*1024
MAX_SOURCE_BYTES = 65_536  # typed() limit on a managed cell source
# A selection's sheet_index names any sheet of the package's workbook list (a bounded package holds at most 128
# parts); only worksheets count toward whole-workbook scope's 64-sheet limit.
MAX_SHEET_INDEX = 4096


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


def related_part(relationships,kind,ns=TRANSITIONAL):
    candidates=[item for item in relationships if item.get("Type")==ns.rel+"/"+kind]
    require(len(candidates)<=1,"INVALID_SPREADSHEET","Duplicate workbook part relationships")
    if not candidates: return None
    item=candidates[0]
    require(item.get("TargetMode")!="External","INVALID_SPREADSHEET","Workbook data parts must be local")
    target=item.get("Target","")
    path=posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/"+target)
    require(path.startswith("xl/") and "\\" not in path,"INVALID_SPREADSHEET","Invalid workbook part target")
    return path


def date_system(workbook,ns):
    properties=workbook.find(f"{{{ns.main}}}workbookPr")
    epoch=properties.get("date1904","0") if properties is not None else "0"
    require(epoch in ("0","1","false","true"),"INVALID_SPREADSHEET","Invalid XLSX date1904 flag")
    return "1904" if epoch in ("1","true") else "1900"


def worksheet_path(mapping,sheet,ns):
    """The local part of a workbook <sheet>, given the workbook relationships keyed by Id."""
    rel=mapping.get(sheet.get(f"{{{ns.rel}}}id"))
    require(rel is not None and rel.get("TargetMode")!="External" and rel.get("Type")==ns.rel+"/worksheet",
            "INVALID_SPREADSHEET","Selected sheet must have a local worksheet relationship")
    target=rel.get("Target","")
    path=posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/"+target)
    require(path.startswith("xl/") and "\\" not in path,"INVALID_SPREADSHEET","Invalid worksheet target")
    return path


def shared_strings_path(archive,relationships,ns):
    return related_part(relationships,"sharedStrings",ns) or ("xl/sharedStrings.xml" if "xl/sharedStrings.xml" in archive.namelist() else None)


def styles_path(archive,relationships,ns):
    path=related_part(relationships,"styles",ns)
    require(path is None or path in archive.namelist(),"INVALID_SPREADSHEET","Missing related stylesheet")
    return path or "xl/styles.xml"


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


UNMAINTAINED_DETAIL="references were not maintained through a structural edit; the source is written unchanged"
# Whole-workbook export writes every stored formula. One entry per formula_admission category counts those
# DataHorizon cannot evaluate; one counts those whose references cannot even be read (the target judges them).
EXPORT_UNEVALUABLE_DETAIL="written as stored; DataHorizon cannot evaluate it"
EXPORT_UNTOKENIZABLE_DETAIL="written as stored; its references cannot be read, so the target application judges it"


def whole_formula_to_xlsx(cell,sheet,report,names):
    """The <f> text of a formula cell at whole-workbook scope, where `names` are the exported sheet names.

    A formula the calculator can evaluate is checked and written as native_formula_to_xlsx writes it. Any other is
    written as stored, with _xlfn. prefixes: counted as unevaluable under its formula_admission category when the
    reference scanner reads it, else as an approximated formula.syntax entry. Only characters spreadsheet XML cannot
    hold refuse the export, as for unmaintained cells."""
    from .formula_refs import scan
    source=cell["source"]
    require(not any((ord(c)<32 and c not in "\t\n\r") or ord(c) in (65534,65535) for c in source),
            "UNSUPPORTED_FORMULA","Formula contains characters unavailable in spreadsheet XML")
    problem=formula_status(source,names)
    if problem is None:
        try: return native_formula_to_xlsx(source,None,names)
        except EngineError: problem=("formula.syntax","")
    where={"sheet":sheet,"cell":address(cell["row"],cell["col"])}
    if scan(source) is None: report.add("formula.syntax","approximated",EXPORT_UNTOKENIZABLE_DETAIL,**where)
    else: report.add(problem[0],"unevaluable",EXPORT_UNEVALUABLE_DETAIL,**where)
    return map_formula_functions(source,FUTURE_PREFIX)


def unmaintained_formula(cell,sheet,report,whole):
    """Source text for an unmaintained cell, which no transform can read: write it as is and report the approximation.

    A selection export cannot check its references against the selection, so it refuses the cell.
    """
    source=cell["source"]
    require(whole,"UNSUPPORTED_FORMULA",f"Formula at {address(cell['row'],cell['col'])} cannot be checked against the selection: "
            "its references were not maintained through a structural edit. Export the whole workbook or choose values")
    require(not any((ord(c)<32 and c not in "\t\n\r") or ord(c) in (65534,65535) for c in source),
            "UNSUPPORTED_FORMULA","Formula contains characters unavailable in spreadsheet XML")
    report.add("formula.unmaintained","approximated",UNMAINTAINED_DETAIL,sheet=sheet,cell=address(cell["row"],cell["col"]))
    return source


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


def xlsx_text(item,ns=TRANSITIONAL):
    if item is None: return ""
    return text_decode("".join((child.text or "") if child.tag==f"{{{ns.main}}}t" else
                       (child.findtext(f"{{{ns.main}}}t") or "") if child.tag==f"{{{ns.main}}}r" else "" for child in item))


class SharedFormulas:
    """Expand only stored shared members, never a group's potentially empty rectangle.

    Per group only the master's origin, extent and source are kept, so an extent may be any rectangle of the grid that
    holds its master. A streaming reader registers masters as it meets them and passes `limit` (groups) and `budget`
    (UTF-8 bytes of master source kept) to bound the groups one sheet holds open at once. When a new master would exceed
    either, the groups whose extent ends above the highest master row met so far are released first; only groups still
    open beyond the bounds are LIMIT. Every file Excel writes stores rows in ascending order, so no later member lies
    inside a released group: one that names it fails as it did while the group was kept (`ended` remembers released
    keys), and one met in an earlier row than the release, which only out-of-order rows produce, is LIMIT. A group in
    `pinned` (members met before its master) is kept to the end of the sheet. Below the bounds nothing is released."""
    def __init__(self,cells=(),ns=TRANSITIONAL,*,limit=None,budget=None):
        self.ns=ns
        self.groups={}
        self.limit,self.budget,self.kept=limit,budget,0
        self.ends,self.pinned,self.ended,self.high,self.released=[],set(),None,0,0
        for cell in cells: self.register(cell)

    def gone(self,key):
        """Whether the group `key` was released (keys are compared as numbers once released)."""
        if self.ended is None or re.fullmatch(r"[0-9]{1,8}",key) is None: return False
        index=int(key)
        return bool(self.ended[index>>3]>>(index&7)&1)

    def release(self):
        while self.ends and self.ends[0][0]<self.high:
            key=heapq.heappop(self.ends)[1]
            self.kept-=len(self.groups.pop(key)[3].encode("utf-8","surrogatepass"))
            if self.ended is None: self.ended=bytearray(10**8//8)
            index=int(key); self.ended[index>>3]|=1<<(index&7)
            self.released=self.high

    def register(self,cell):
        """Record the group of a shared master; any other cell is ignored."""
        formula=cell.find(f"{{{self.ns.main}}}f")
        if formula is None or formula.get("t")!="shared" or not formula.text: return
        key=formula.get("si","")
        require(re.fullmatch(r"[0-9]{1,8}",key) is not None,"INVALID_SPREADSHEET","Invalid shared formula group")
        require(key not in self.groups and not self.gone(key),"INVALID_SPREADSHEET","Duplicate shared formula master")
        ends=formula.get("ref","").split(":")
        require(1<=len(ends)<=2,"INVALID_SPREADSHEET","Invalid shared formula extent")
        try:
            r1,c1=coordinates(ends[0]); r2,c2=coordinates(ends[-1]); row,col=coordinates(cell.get("r",""))
        except FormulaError as error: raise EngineError("INVALID_SPREADSHEET","Invalid shared formula coordinates") from error
        require(r1<=row<=r2 and c1<=col<=c2,"INVALID_SPREADSHEET","Shared formula extent must contain its master")
        source="="+formula.text
        if self.limit is not None:
            size=len(source.encode("utf-8","surrogatepass"))
            self.high=max(self.high,row)
            if len(self.groups)>=self.limit or self.kept+size>self.budget: self.release()
            self.kept+=size
            require(len(self.groups)<self.limit and self.kept<=self.budget,"LIMIT",
                    f"One sheet holds more than {self.limit:,} shared formula groups or {self.budget//(1024*1024)} MiB of shared formula text whose extents are open at once")
            if key not in self.pinned: heapq.heappush(self.ends,(r2,key))
        self.groups[key]=(row,col,(r1,c1,r2,c2),source)

    def source(self,cell,formula):
        kind=formula.get("t","normal")
        require(formula.get("ca","0") in ("0","1","false","true"),"INVALID_SPREADSHEET","Invalid formula calculation flag")
        if kind=="normal": return "="+(formula.text or "")
        require(kind=="shared","INVALID_SPREADSHEET","Unknown formula type")
        key=formula.get("si","")
        row,col=coordinates(cell.get("r",""))
        if key not in self.groups and self.gone(key):
            require(row>=self.released,"LIMIT",f"Shared formula member at row {row:,} arrives after row {self.released:,}, where groups whose extents had "
                    f"ended were released to keep at most {self.limit:,} open at once; a sheet with more must store its rows in ascending row order")
            raise EngineError("INVALID_SPREADSHEET","Shared formula member lies outside its master extent")
        require(key in self.groups,"INVALID_SPREADSHEET","Shared formula has no master")
        origin_row,origin_col,(r1,c1,r2,c2),source=self.groups[key]
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
    ns: Namespaces = TRANSITIONAL
    seen: set | None = field(default_factory=set)  # None: duplicate coordinates are detected downstream


def convert_xlsx_cell(cell,*,context):
    """Convert one stored <c> to an edit, or None when it is skipped (with a report entry).

    Raises EngineError only when the package itself is malformed."""
    report,sheet,mode_=context.report,context.sheet,context.mode
    location=cell.get("r","")
    try: row,col=coordinates(location)
    except FormulaError as error: raise EngineError("INVALID_SPREADSHEET","Invalid cell reference") from error
    if context.seen is not None:
        require((row,col) not in context.seen,"INVALID_SPREADSHEET","Duplicate cell coordinates")
        context.seen.add((row,col))

    def skip(category,detail=""):
        report.add(category,"omitted",detail,sheet=sheet,cell=location)

    formula=cell.find(f"{{{context.ns.main}}}f")
    value=cell.findtext(f"{{{context.ns.main}}}v")
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
                    # Neither the parser nor the reference scanner can read this source (formula.Unmaintainable),
                    # so import_edits could not move it; omit it here, where the report is still open.
                    skip("formula.untranslatable"); return None
            item={"source":native,"kind":"formula"}
    if item is None:
        if mode_=="values" and formula is not None and value is None and kind!="inlineStr":
            skip("cell.missing_cache"); return None
        try:
            if kind=="e":
                if value is None: skip("cell.invalid_value","missing value"); return None
                item={"source":value,"kind":"text"}
                noted.append(("cell.error_value","converted",""))
            elif kind=="inlineStr": item={"source":xlsx_text(cell.find(f"{{{context.ns.main}}}is"),context.ns),"kind":"text"}
            elif kind=="s":
                require(value is not None and re.fullmatch(r"[0-9]+",value) is not None and int(value)<len(context.shared_strings),"INVALID_SPREADSHEET","Invalid shared string index")
                item={"source":context.shared_strings[int(value)],"kind":"text"}
            elif kind=="d":
                if value is None: skip("cell.invalid_value","missing value"); return None
                temporal=import_temporal(value,"datetime" if "T" in value or " " in value else "date" if "-" in value else "time",context.timezone_policy)
                item={"source":str(temporal),"kind":temporal.kind}
            elif kind=="str":
                if value is None: skip("cell.invalid_value","missing value"); return None
                item={"source":text_decode(value),"kind":"text"}
            elif kind=="b":
                if value not in ("0","1"): skip("cell.invalid_value","boolean"); return None
                item={"source":"TRUE" if value=="1" else "FALSE","kind":"boolean"}
            elif kind=="n": item={"source":value or "","kind":"number" if value else "auto"}
            else: skip("cell.unsupported_type",kind); return None
        except (ValueError,UnicodeError,OverflowError):
            skip("cell.invalid_value","date/time" if kind=="d" else "text encoding"); return None
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


@dataclass
class XlsxBook:
    """The workbook-wide parts of a bounded XLSX package: the workbook, its relationships, every <sheet> with its
    kind (None for a worksheet, else the feature id of a sheet kind that is not imported), the shared strings and
    the workbook.feature report."""
    workbook: object
    ns: Namespaces
    relationships: object
    mapping: dict
    sheets: list
    kinds: list
    shared: list
    report: ConversionReport

    def worksheets(self):
        """(1-based sheet index, name) of each worksheet, in workbook order."""
        return [(index,sheet.get("name","")) for index,(sheet,kind) in enumerate(zip(self.sheets,self.kinds),1) if kind is None]

    def skipped(self):
        return [(sheet.get("name",""),kind) for sheet,kind in zip(self.sheets,self.kinds) if kind is not None]


def read_book(archive):
    from .xlsx_features import StringFeatures, sheet_kinds, workbook_features
    workbook=read_xml(archive,"xl/workbook.xml")
    ns=namespaces(workbook)
    sheets=workbook.findall(f"{{{ns.main}}}sheets/{{{ns.main}}}sheet")
    relationships=read_xml(archive,"xl/_rels/workbook.xml.rels")
    mapping={item.get("Id"):item for item in relationships}
    kinds=sheet_kinds(sheets,mapping,ns)
    shared=[]
    strings=StringFeatures(ns)
    shared_path=shared_strings_path(archive,relationships,ns)
    if shared_path:
        table=read_xml(archive,shared_path)
        require(table.tag==f"{{{ns.main}}}sst","INVALID_SPREADSHEET","Shared strings part does not match the workbook's namespaces")
        for item in table:
            shared.append(xlsx_text(item,ns))
            strings.string(item)
            require(len(shared)<=MAX_CELLS,"LIMIT","Shared strings exceed 10,000 entries")
    book=XlsxBook(workbook,ns,relationships,mapping,sheets,kinds,shared,ConversionReport())
    workbook_features(book.report,workbook,ns,archive.namelist(),strings,book.skipped())
    return book


def _xlsx_preview(request,checkpoint=lambda completed,phase:None,*,sheet_names=None):
    selected=integer(request.get("sheet_index",1),1,MAX_SHEET_INDEX,"sheet_index")
    selected_mode=mode(request)
    require(request.get("timezone_policy","reject") in ("reject","utc"),"INVALID_ARGUMENT","Choose reject or utc timestamp policy")
    report=["Only the selected sheet's stored cells are imported; absent cells do not clear the destination.",
            "Other sheets are omitted. Worksheet and workbook features that are not imported, and unsupported styles, are listed below; no external resources are fetched.",
            "Formulas recalculate with DataHorizon's declared compatibility tier; imported code is never activated."]
    if selected_mode=="values": report.append("Formula caches become literal values. Cache freshness cannot be verified; missing/error caches block import.")
    if selected_mode=="mixed": report.append("Formulas DataHorizon can evaluate stay formulas; those it cannot become their cached values when a usable cache exists. Cache freshness cannot be verified.")
    with package(request) as archive:
        checkpoint(0,"Reading workbook")
        book=read_book(archive)
        workbook,ns,relationships,mapping,sheets,shared=book.workbook,book.ns,book.relationships,book.mapping,book.sheets,book.shared
        system=date_system(workbook,ns)
        report.append("ISO shorthand is normalized to native seconds; timezone offsets require explicit timezone_policy=utc, which stores UTC civil values and omits the original offset.")
        report.append(f"Date/time serials with supported formats use the {system} epoch; serial 60 in the 1900 system is rejected. Formula numeric literals are not epoch-shifted; native formulas use the 1900 system.")
        require(0<len(sheets) and selected<=len(sheets),"INVALID_SPREADSHEET","Choose an existing sheet")
        from .xlsx_features import KIND_NAMES
        kind=book.kinds[selected-1]
        require(kind is None,"INVALID_ARGUMENT",f"Sheet {sheets[selected-1].get('name','')!r} is {KIND_NAMES.get(kind)} and cannot be imported; choose a worksheet")
        root=read_xml(archive,worksheet_path(mapping,sheets[selected-1],ns))
        require(root.tag==f"{{{ns.main}}}worksheet","INVALID_SPREADSHEET","Invalid worksheet")
        from .xlsx_features import SheetFeatures, relationship_summary, scan_tree
        from .xlsx_stream import ByteBudget
        from .xlsx_styles import XlsxStyles
        # Workbook features are reported once: here for a selection, by prepare_workbook for a whole workbook.
        wide=book.report
        conversions=ConversionReport()
        if sheet_names is None: conversions.merge(wide)
        part=worksheet_path(mapping,sheets[selected-1],ns)
        sheet_features=SheetFeatures(ns,relationship_summary(archive,part,ByteBudget(MAX_EXPANDED)))
        styles=XlsxStyles(archive,styles_path(archive,relationships,ns),conversions,ns)
        edits,issues=[],[]
        shift=(integer(request.get("row",1),1,1048576,"row")-1,integer(request.get("col",1),1,16384,"col")-1)
        sheet_label=sheets[selected-1].get("name","")
        count=0
        stored=root.findall(f"{{{ns.main}}}sheetData/{{{ns.main}}}row/{{{ns.main}}}c")
        require(len(stored)<=MAX_CELLS,"LIMIT","Import exceeds 10,000 stored cells")
        # Shared-formula structure is package structure: a broken group rejects the file.
        shared_formulas=SharedFormulas(stored,ns) if selected_mode!="values" else None
        context=XlsxCellContext(mode=selected_mode,shared_strings=shared,styles=styles,date_system=system,
                                sheet_names=sheet_names,sheet=sheet_label,report=conversions,
                                shared_formulas=shared_formulas,timezone_policy=request.get("timezone_policy","reject"),shift=shift,ns=ns)
        for cell in stored:
            count+=1; checkpoint(count,"Reading cells")
            require(count<=MAX_CELLS,"LIMIT","Import exceeds 10,000 stored cells")
            edit=convert_xlsx_cell(cell,context=context)
            if edit is not None: edits.append(edit)
        scan_tree(root,sheet_features)
        sheet_features.report(conversions,sheet_label)
        if shared_formulas and shared_formulas.groups: report.append("Shared formulas expand to explicit per-cell sources with relative/absolute reference translation; caches are discarded.")
        report.extend(STYLE_SCOPE_LINES)
        scope=list(report)
        report.extend(conversions.lines())
        result={"cells":edits,"can_import":bool(edits),"issues":issues,
                "sheet":sheet_label,"sheets":[s.get("name","") for s in sheets],"report":report,"scope_lines":scope,"report_entries":conversions.entries(),
                "totals":conversions.totals(),"stored_cells":count}
        require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","Spreadsheet preview exceeds 4 MiB")
        if result["can_import"]:
            result["cells"]=import_edits(result,integer(request.get("row",1),1,1048576,"row"),integer(request.get("col",1),1,16384,"col"))
        if sheet_names is not None: result["_conversions"]=conversions; result["_workbook"]=wide
        return result


def native_only_report(snapshot):
    """Report lines for workbook names and event bindings, which no interchange format carries."""
    report=[]
    if snapshot.get('native_names'):report.append('Workbook names are native-only and omitted from this export: '+', '.join(snapshot['native_names']))
    if snapshot.get('native_bindings'):report.append('Event bindings are native-only and omitted from this export: '+', '.join(snapshot['native_bindings']))
    return report


WHOLE_EXPORT_CACHES="Formula caches are omitted; target recalculation is requested."
TEMPORAL_EXPORT_LINE=("Typed dates/times export as ISO XLSX date cells with native precision. Supported numeric date formats are interpreted as serials on import. "
                      "Target readers may reduce subsecond precision; volatile functions recalculate using the target clock/timezone. Native civil arithmetic and "
                      "pre-March-1900/typed comparisons may differ from target serial semantics; choose values to freeze results.")
FUTURE_FUNCTIONS_LINE="Known modern functions use their _xlfn identifiers; target function/version support is required. Choose values for older targets."


def uses_future_prefix(source):
    """Whether exporting this formula source writes an _xlfn identifier."""
    return map_formula_functions(source,FUTURE_PREFIX)!=source


def export_report(head,catalog,future_functions):
    """The fixed report lines of an XLSX export after its scope lines `head`; the conversion report's lines follow them."""
    report=list(head)
    if catalog.get('native_omissions'):report.append('Native relational objects omitted from this sheet export: '+', '.join(catalog['native_omissions']))
    report.extend(native_only_report(catalog))
    report.append(TEMPORAL_EXPORT_LINE)
    if future_functions:report.append(FUTURE_FUNCTIONS_LINE)
    return report


def blocked_values(snapshots,whole):
    """Refuse a values export when a cell has no current, error-free value, naming the first 100 such cells."""
    blocked=[(f"{sheet['name']}!" if whole else "")+address(cell["row"],cell["col"])
             for sheet in snapshots for cell in sheet["cells"] if cell.get("calculation")!="current"]
    if blocked:
        shown=", ".join(blocked[:100])+(f" and {len(blocked)-100:,} more" if len(blocked)>100 else "")
        raise EngineError("CALCULATION_ERROR",f"Values export requires current, error-free values; {len(blocked):,} "
                          f"cell{'' if len(blocked)==1 else 's'} have none: {shown}. Correct them or export formulas",
                          cells=blocked[:100],blocked_count=len(blocked))


def export_cell(cell,style,*,sheet,mode,report,whole,bounds=None,names=None):
    """The <c> element of one native cell at style index `style`. Both XLSX exporters write exactly this element.

    `whole` is whole-workbook scope, where `names` are the exported sheet names and every stored formula is written
    (whole_formula_to_xlsx); a selection checks references against `bounds`."""
    out=ET.Element("c",r=address(cell["row"],cell["col"]),s=str(style))
    if cell["kind"]=="unmaintained" and mode=="formulas":
        node(out,"f",unmaintained_formula(cell,sheet,report,whole)[1:])
    elif cell["kind"]=="formula" and mode=="formulas":
        node(out,"f",(whole_formula_to_xlsx(cell,sheet,report,names) if whole else native_formula_to_xlsx(cell["source"],bounds))[1:])
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
    return out


def package_parts(names):
    """The content types, package relationships, workbook and workbook relationships of an export of sheets `names`.

    Worksheets are xl/worksheets/sheet<N>.xml and the stylesheet xl/styles.xml; the workbook requests full recalculation on load."""
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
    return [("[Content_Types].xml",types),("_rels/.rels",mainrels),("xl/workbook.xml",workbook),("xl/_rels/workbook.xml.rels",relationships)]


def xlsx_export(snapshot,request):
    selected_mode=mode(request)
    require(selected_mode!="mixed","INVALID_ARGUMENT","mode mixed applies to import; export formulas or values")
    head=["Selection only, at original source coordinates, from one consistent native state.",
          "Native cell styles are mapped. Other sheets, code, queries, relationships, history, dimensions and view preferences are omitted.",
          "Native NULL becomes an empty cell. Formula semantics follow the target application's compatibility; formula caches are omitted and recalculation is requested."]
    whole="sheets" in snapshot
    snapshots=export_temporal_styles(snapshot["sheets"] if whole else [{**snapshot,"name":"Sheet1"}])
    names=validate_sheet_names([s["name"] for s in snapshots])
    if selected_mode=="values": blocked_values(snapshots,whole)
    if whole: head=workbook_report()+[WHOLE_EXPORT_CACHES]
    report=export_report(head,snapshot,selected_mode=="formulas" and any(uses_future_prefix(c["source"]) for sheet in snapshots for c in sheet["cells"] if c["kind"]=="formula"))
    from .xlsx_styles import export_styles
    styles,style_ids=export_styles([c for s in snapshots for c in s["cells"]])
    worksheets=[];conversions=ConversionReport()
    for sheet in snapshots:
        root=ET.Element("worksheet",xmlns=S)
        data=node(root,"sheetData")
        current=None
        for cell in sheet["cells"]:
            if cell["row"]!=current:
                current=cell["row"]; row=node(data,"row",r=str(current))
            row.append(export_cell(cell,style_ids[encoded(cell.get("style",{}))],sheet=sheet["name"],mode=selected_mode,report=conversions,
                                   whole=whole,bounds=sheet.get("range"),names=names))
        worksheets.append(root)
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        parts=package_parts(names)+[("xl/styles.xml",styles)]
        parts.extend((f"xl/worksheets/sheet{index}.xml",part) for index,part in enumerate(worksheets,1))
        payload=[(name,xml(part)) for name,part in parts]
        require(sum(len(data) for _,data in payload)<=MAX_EXPANDED,"LIMIT","Expanded XLSX export exceeds 16 MiB")
        for name,data in payload: archive.writestr(name,data)
    result={"file_base64":base64.b64encode(output.getvalue()).decode(),"state":snapshot["state"],"report":report+conversions.lines(),
            "scope_lines":list(report),"report_entries":conversions.entries()}
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


def validate_sheet_names(names,limit=64):
    from .engine import Workbook
    require(0<len(names)<=limit,"LIMIT",f"Provide 1-{limit} sheets")
    for name in names:
        Workbook.check_name(name)
        require(not any(ord(c) in (65534,65535) for c in name),"INVALID_NAME","Sheet names contain characters unavailable in spreadsheet XML")
        require(name.strip() and not name.startswith("'") and not name.endswith("'"),"INVALID_NAME","Spreadsheet sheet names cannot be blank or start/end with apostrophes")
    require(len({fold_name(n) for n in names})==len(names),"INVALID_NAME","Duplicate sheet names are unavailable")
    return names


def workbook_report():
    return ["All sheets, names, stored inputs and mapped direct cell styles from one consistent workbook state.",
            "Whole-workbook import replaces sheets only in an empty destination, or adds them after the existing sheets when no name collides, as one atomic operation; history is cleared. Required handlers reject until structure events are defined.",
            "Code, queries, relationships, history, dimensions, view state and unsupported spreadsheet features are omitted. No code activates; retain the native/source file."]


def preparation_scope(request):
    value=request.get("scope","selection")
    require(value in ("selection","workbook"),"INVALID_ARGUMENT","scope must be selection or workbook")
    return value


def prepare_workbook(request,checkpoint,names,prepare,indexes=None,scope=()):
    """`indexes` are the sheet_index of each name (by default 1, 2, ...): a package's sheets that are not imported
    (XLSX chart, dialog and macro sheets) take no name but keep their place in the index. `scope` are the format's
    fixed report lines after workbook_report(); the result's report is those scope lines, then one line per entry."""
    validate_sheet_names(names)
    require(request.get("row",1)==1 and request.get("col",1)==1,"INVALID_ARGUMENT","Whole-workbook import uses original coordinates")
    require(names,"INVALID_SPREADSHEET","Workbook has no sheets")
    result={"scope":"workbook","workbook_sheets":[],"stored_cells":0,"issues":[],"can_import":True}
    conversions=ConversionReport()
    size=0
    for index,name in enumerate(names,1):
        checkpoint(index-1,"Reading sheet "+name)
        part=prepare({**request,"scope":"selection","sheet_index":indexes[index-1] if indexes else index},checkpoint,sheet_names=names)
        # Every sheet's preparation reads the same workbook features; they are reported once, first.
        wide=part.pop("_workbook")
        if index==1: conversions.merge(wide)
        conversions.merge(part.pop("_conversions"))
        result["stored_cells"]+=part["stored_cells"]
        require(result["stored_cells"]<=MAX_CELLS,"LIMIT","Whole-workbook import exceeds 10,000 stored cells")
        size+=len(encoded(part["cells"]).encode())
        require(size<=3*1024*1024,"READ_LIMIT","Workbook input payload exceeds 3 MiB")
        result["workbook_sheets"].append({"name":name,"cells":part["cells"]})
    # Sheets whose cells all degraded or were skipped are still created; their details are itemized here.
    result["scope_lines"]=workbook_report()+list(scope)
    result["report"]=result["scope_lines"]+conversions.lines()
    result["report_entries"]=conversions.entries()
    result["totals"]=conversions.totals()
    require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","Workbook preview exceeds 4 MiB")
    return result


def xlsx_preview(request,checkpoint=lambda completed,phase:None):
    if preparation_scope(request)!="workbook": return _xlsx_preview(request,checkpoint)
    with package(request) as archive:
        book=read_book(archive)
    worksheets=book.worksheets()
    if book.sheets and not worksheets:
        # Only chart, dialog or macro sheets: nothing to import. As for any workbook without sheets, Apply is refused.
        result={"scope":"workbook","workbook_sheets":[],"stored_cells":0,"can_import":False,
                "issues":["The workbook has no worksheets; its other sheets are not imported"],
                "report":workbook_report()+book.report.lines(),"scope_lines":workbook_report(),"report_entries":book.report.entries(),"totals":book.report.totals()}
        return result
    return prepare_workbook(request,checkpoint,[name for _,name in worksheets],_xlsx_preview,[index for index,_ in worksheets])
