# SPDX-License-Identifier: GPL-3.0-only
"""XLSX mapping for the existing native style tier; unsupported display is reported."""
import re

from .engine import encoded, require, validate_style, EngineError
from .borders import SIDES, BORDER_STYLES, border_edges
from .formats import number_format_code, simple_number_style
from .conversion_report import ConversionReport
from .interchange import S,R,node,read_xml


def enabled(item):
    return item is not None and item.get("val","1") not in ("0","false")


class XlsxStyles:
    def __init__(self,archive,path="xl/styles.xml",report=None):
        self.cache={}
        self.recorded={}
        self.report=report if report is not None else ConversionReport()
        self._where=(None,None)
        self._current=[]
        self.fonts=[]; self.fills=[]; self.borders=[]; self.formats={}; self.xfs=[]
        if path not in archive.namelist(): return
        root=read_xml(archive,path)
        require(root.tag==f"{{{S}}}styleSheet","INVALID_SPREADSHEET","Invalid stylesheet")
        for attr,tag in (("fonts","fonts"),("fills","fills"),("borders","borders"),("xfs","cellXfs")):
            setattr(self,attr,root.findall(f"{{{S}}}{tag}/*"))
            require(len(getattr(self,attr))<=4096,"LIMIT","Stylesheet table exceeds 4,096 entries")
        self.formats={item.get("numFmtId"):item.get("formatCode","") for item in root.findall(f"{{{S}}}numFmts/{{{S}}}numFmt")}

    def loss(self,category,detail,severity="omitted"):
        """Record one style loss for the cell being resolved (and for later cells sharing the style)."""
        self._current.append((category,severity,detail))
        self.report.add(category,severity,detail,sheet=self._where[0],cell=self._where[1])

    @staticmethod
    def index(value,table):
        require(isinstance(value,str) and re.fullmatch(r"[0-9]{1,8}",value) is not None and int(value)<len(table),
                "INVALID_SPREADSHEET","Invalid stylesheet index")
        return table[int(value)]

    def style(self,index="0",*,sheet=None,cell=None):
        """The native style for an xf index; its losses are reported once per cell that uses it."""
        if not self.xfs:
            require(index=="0","INVALID_SPREADSHEET","Cell references a missing stylesheet")
            return {}
        if index in self.cache:
            for category,severity,detail in self.recorded[index]: self.report.add(category,severity,detail,sheet=sheet,cell=cell)
            return self.cache[index]
        self._where=(sheet,cell); self._current=[]
        self.cache[index]=result=self._resolve(index)
        self.recorded[index]=self._current
        return result

    def _resolve(self,index):
        xf=self.index(index,self.xfs); result={}
        if xf.get("xfId","0")!="0": self.loss("style.named_style","Named style inheritance is omitted; direct cell formatting is mapped.")
        fmt=xf.get("numFmtId","0")
        custom=self.formats.get(fmt)
        if fmt=="49" or custom=="@": result["format"]="text"
        else:
            code=custom if custom is not None else {"0":"General","1":"0","2":"0.00","3":"#,##0","4":"#,##0.00","9":"0%","10":"0.00%","14":"mm-dd-yy","15":"d-mmm-yy","16":"d-mmm","17":"mmm-yy","18":"h:mm AM/PM","19":"h:mm:ss AM/PM","20":"h:mm","21":"h:mm:ss","22":"m/d/yy h:mm"}.get(fmt,"")
            mapped=simple_number_style(code)
            if mapped:
                result.update(mapped)
                if mapped.get("format") in ("date_iso","date_mdy","date_dmy","time24","datetime_iso") and code!=number_format_code(mapped):self.loss("style.number_format","Date/time display is normalized to the supported invariant format; source date/time values are retained.","approximated")
            elif code!="General": self.loss("style.number_format",f"Number format {fmt} ({custom or 'built-in'}) is omitted; numeric inputs remain numbers, including date/time serials.")
        font=self.index(xf.get("fontId","0"),self.fonts)
        if enabled(font.find(f"{{{S}}}b")): result["bold"]=True
        if enabled(font.find(f"{{{S}}}i")): result["italic"]=True
        for key,tag in (("font_family","name"),("font_size","sz"),("text_color","color")):
            element=font.find(f"{{{S}}}{tag}")
            if element is None:continue
            value=element.get("val","")
            if key=="font_size":value=int(float(value)) if re.fullmatch(r"[0-9]{1,3}(?:\.0{1,8})?",value) else None
            elif key=="text_color":
                rgb=element.get("rgb","");value="#"+rgb[2:].lower() if re.fullmatch(r"[0-9a-fA-F]{8}",rgb) and element.get("tint","0") in ("0","0.0") else None
            try:validate_style({key:value})
            except EngineError:self.loss("style.font",f"Unsupported {key} (including theme/tinted colors or fractional/out-of-range sizes) is omitted.")
            else:result[key]=value
        fill=self.index(xf.get("fillId","0"),self.fills)
        pattern=fill.find(f"{{{S}}}patternFill")
        if pattern is not None and pattern.get("patternType") not in (None,"none"):
            color=pattern.find(f"{{{S}}}fgColor")
            rgb=color.get("rgb","") if color is not None else ""
            if pattern.get("patternType")=="solid" and re.fullmatch(r"[0-9a-fA-F]{8}",rgb) and color.get("tint","0") in ("0","0.0"): result["background"]="#"+rgb[2:].lower()
            else: self.loss("style.fill","Non-solid, tinted, indexed or theme fills are omitted.")
        border=self.index(xf.get("borderId","0"),self.borders)
        sides=[border.find(f"{{{S}}}{side}") for side in ("left","right","top","bottom")]
        styles=[side.get("style") if side is not None else None for side in sides]
        if all(style=="thin" for style in styles):result["border"]="box"
        else:
            for side,style in zip(SIDES,styles):
                if style in BORDER_STYLES and style!="none":result["border_"+side]=style
                elif style not in (None,"none"):self.loss("style.border","Partial mapping: unsupported border line styles are omitted.")
        if any(style in BORDER_STYLES and style!="none" for style in styles):self.loss("style.border","Border colors map to the native default color.","approximated")
        if any(edge.tag.rsplit("}",1)[-1] not in ("left","right","top","bottom") and edge.get("style") for edge in border):
            self.loss("style.border","Diagonal and additional border edges are omitted.")
        alignment=xf.find(f"{{{S}}}alignment")
        if alignment is not None:
            horizontal=alignment.get("horizontal")
            if horizontal in ("left","center","right"): result["align"]=horizontal
            elif horizontal not in (None,"general"): self.loss("style.alignment","Unsupported horizontal alignment is omitted.")
            if alignment.get("wrapText") in ("1","true"): result["wrap"]=True
            if any(key not in ("horizontal","wrapText") for key in alignment.attrib): self.loss("style.alignment","Vertical alignment, rotation, indent and shrink-to-fit are omitted.")
        return result


def export_styles(cells):
    import xml.etree.ElementTree as ET
    styles=[{}]; mapping={encoded({}):0}
    for cell in cells:
        key=encoded(cell.get("style",{}))
        if key not in mapping:
            require(len(styles)<1024,"LIMIT","XLSX export exceeds 1,024 distinct native styles")
            mapping[key]=len(styles); styles.append(cell["style"])
    root=ET.Element("styleSheet",xmlns=S)
    codes={}
    for style in styles:
        code=number_format_code(style)
        if code not in ("General","@","0.00","0.00%") and code not in codes: codes[code]=str(164+len(codes))
    if codes:
        formats=node(root,"numFmts",count=str(len(codes)))
        for code,identifier in codes.items(): node(formats,"numFmt",numFmtId=identifier,formatCode=code)
    fonts=node(root,"fonts",count=str(len(styles)))
    fills=node(root,"fills",count=str(len(styles)+2))
    node(node(fills,"fill"),"patternFill",patternType="none")
    node(node(fills,"fill"),"patternFill",patternType="gray125")
    borders=node(root,"borders",count=str(len(styles)))
    for style in styles:
        font=node(fonts,"font")
        if "font_size" in style:node(font,"sz",val=str(style["font_size"]))
        if "font_family" in style:node(font,"name",val=style["font_family"])
        if "text_color" in style:node(font,"color",rgb="FF"+style["text_color"][1:].upper())
        if style.get("bold"): node(font,"b")
        if style.get("italic"): node(font,"i")
        fill=node(fills,"fill")
        pattern=node(fill,"patternFill",patternType="solid" if "background" in style else "none")
        if "background" in style:
            node(pattern,"fgColor",rgb="FF"+style["background"][1:].upper()); node(pattern,"bgColor",indexed="64")
        border=node(borders,"border")
        for side in ("left","right","top","bottom","diagonal"):
            edge=node(border,side)
            if side!="diagonal" and border_edges(style)[side]!="none":
                edge.set("style",border_edges(style)[side]); node(edge,"color",rgb="FF000000")
    bases=node(root,"cellStyleXfs",count="1"); node(bases,"xf",numFmtId="0",fontId="0",fillId="0",borderId="0")
    xfs=node(root,"cellXfs",count=str(len(styles)))
    for index,style in enumerate(styles):
        xf=node(xfs,"xf",numFmtId=({"General":"0","@":"49","0.00":"2","0.00%":"10"}.get(number_format_code(style)) or codes[number_format_code(style)]),
                fontId=str(index),fillId=str(index+2) if "background" in style else "0",borderId=str(index),xfId="0",
                applyFont="1",applyFill="1",applyBorder="1",applyNumberFormat="1",applyAlignment="1")
        alignment={}
        if "align" in style: alignment["horizontal"]=style["align"]
        if style.get("wrap"): alignment["wrapText"]="1"
        if alignment: node(xf,"alignment",**alignment)
    names=node(root,"cellStyles",count="1"); node(names,"cellStyle",name="Normal",xfId="0",builtinId="0")
    return root,mapping
