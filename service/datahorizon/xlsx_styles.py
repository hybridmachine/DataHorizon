"""XLSX mapping for the existing native style tier; unsupported display is reported."""
import re

from .engine import encoded, require
from .interchange import S,R,node,read_xml


def enabled(item):
    return item is not None and item.get("val","1") not in ("0","false")


class XlsxStyles:
    def __init__(self,archive,path="xl/styles.xml"):
        self.cache={}
        self.losses=set()
        self.fonts=[]; self.fills=[]; self.borders=[]; self.formats={}; self.xfs=[]
        if path not in archive.namelist(): return
        root=read_xml(archive,path)
        require(root.tag==f"{{{S}}}styleSheet","INVALID_SPREADSHEET","Invalid stylesheet")
        for attr,tag in (("fonts","fonts"),("fills","fills"),("borders","borders"),("xfs","cellXfs")):
            setattr(self,attr,root.findall(f"{{{S}}}{tag}/*"))
            require(len(getattr(self,attr))<=4096,"LIMIT","Stylesheet table exceeds 4,096 entries")
        self.formats={item.get("numFmtId"):item.get("formatCode","") for item in root.findall(f"{{{S}}}numFmts/{{{S}}}numFmt")}

    def loss(self,message):
        if len(self.losses)<100: self.losses.add(message[:160])

    @staticmethod
    def index(value,table):
        require(isinstance(value,str) and re.fullmatch(r"[0-9]{1,8}",value) is not None and int(value)<len(table),
                "INVALID_SPREADSHEET","Invalid stylesheet index")
        return table[int(value)]

    def style(self,index="0"):
        if not self.xfs:
            require(index=="0","INVALID_SPREADSHEET","Cell references a missing stylesheet")
            return {}
        if index in self.cache: return self.cache[index]
        xf=self.index(index,self.xfs); result={}
        if xf.get("xfId","0")!="0": self.loss("Named style inheritance is omitted; direct cell formatting is mapped.")
        fmt=xf.get("numFmtId","0")
        custom=self.formats.get(fmt)
        if fmt=="49" or custom=="@": result["format"]="text"
        elif fmt=="2" or custom=="0.00": result["format"]="fixed2"
        elif fmt=="10" or custom=="0.00%": result["format"]="percent2"
        elif fmt!="0" and custom!="General": self.loss(f"Number format {fmt} ({custom or 'built-in'}) is omitted; numeric inputs remain numbers, including date/time serials.")
        font=self.index(xf.get("fontId","0"),self.fonts)
        if enabled(font.find(f"{{{S}}}b")): result["bold"]=True
        if enabled(font.find(f"{{{S}}}i")): result["italic"]=True
        fill=self.index(xf.get("fillId","0"),self.fills)
        pattern=fill.find(f"{{{S}}}patternFill")
        if pattern is not None and pattern.get("patternType") not in (None,"none"):
            color=pattern.find(f"{{{S}}}fgColor")
            rgb=color.get("rgb","") if color is not None else ""
            if pattern.get("patternType")=="solid" and re.fullmatch(r"[0-9a-fA-F]{8}",rgb) and color.get("tint","0") in ("0","0.0"): result["background"]="#"+rgb[2:].lower()
            else: self.loss("Non-solid, tinted, indexed or theme fills are omitted.")
        border=self.index(xf.get("borderId","0"),self.borders)
        sides=[border.find(f"{{{S}}}{side}") for side in ("left","right","top","bottom")]
        styles=[side.get("style") if side is not None else None for side in sides]
        if all(style=="thin" for style in styles):
            result["border"]="box"
            self.loss("Border colors map to the native default color.")
        elif any(styles): self.loss("Partial or non-thin borders are omitted.")
        if any(edge.tag.rsplit("}",1)[-1] not in ("left","right","top","bottom") and edge.get("style") for edge in border):
            self.loss("Diagonal and additional border edges are omitted.")
        alignment=xf.find(f"{{{S}}}alignment")
        if alignment is not None:
            horizontal=alignment.get("horizontal")
            if horizontal in ("left","center","right"): result["align"]=horizontal
            elif horizontal not in (None,"general"): self.loss("Unsupported horizontal alignment is omitted.")
            if alignment.get("wrapText") in ("1","true"): result["wrap"]=True
            if any(key not in ("horizontal","wrapText") for key in alignment.attrib): self.loss("Vertical alignment, rotation, indent and shrink-to-fit are omitted.")
        self.cache[index]=result
        return result

    def report(self):
        return ["Mapped direct cell styles replace destination styles: general/Text/fixed2/percent2, bold/italic, alignment, wrapping, solid RGB fill and thin box borders.",
                "Font family/size/color, rich-text runs, named-style inheritance, conditional formatting, row/column defaults and dimensions are omitted.",*sorted(self.losses)]


def export_styles(cells):
    import xml.etree.ElementTree as ET
    styles=[{}]; mapping={encoded({}):0}
    for cell in cells:
        key=encoded(cell.get("style",{}))
        if key not in mapping:
            require(len(styles)<1024,"LIMIT","XLSX export exceeds 1,024 distinct native styles")
            mapping[key]=len(styles); styles.append(cell["style"])
    root=ET.Element("styleSheet",xmlns=S)
    fonts=node(root,"fonts",count=str(len(styles)))
    fills=node(root,"fills",count=str(len(styles)+2))
    node(node(fills,"fill"),"patternFill",patternType="none")
    node(node(fills,"fill"),"patternFill",patternType="gray125")
    borders=node(root,"borders",count=str(len(styles)))
    for style in styles:
        font=node(fonts,"font"); node(font,"sz",val="11"); node(font,"name",val="Calibri")
        if style.get("bold"): node(font,"b")
        if style.get("italic"): node(font,"i")
        fill=node(fills,"fill")
        pattern=node(fill,"patternFill",patternType="solid" if "background" in style else "none")
        if "background" in style:
            node(pattern,"fgColor",rgb="FF"+style["background"][1:].upper()); node(pattern,"bgColor",indexed="64")
        border=node(borders,"border")
        for side in ("left","right","top","bottom","diagonal"):
            edge=node(border,side)
            if side!="diagonal" and style.get("border")=="box":
                edge.set("style","thin"); node(edge,"color",rgb="FF000000")
    bases=node(root,"cellStyleXfs",count="1"); node(bases,"xf",numFmtId="0",fontId="0",fillId="0",borderId="0")
    xfs=node(root,"cellXfs",count=str(len(styles)))
    for index,style in enumerate(styles):
        xf=node(xfs,"xf",numFmtId={"general":"0","text":"49","fixed2":"2","percent2":"10"}[style.get("format","general")],
                fontId=str(index),fillId=str(index+2) if "background" in style else "0",borderId=str(index),xfId="0",
                applyFont="1",applyFill="1",applyBorder="1",applyNumberFormat="1",applyAlignment="1")
        alignment={}
        if "align" in style: alignment["horizontal"]=style["align"]
        if style.get("wrap"): alignment["wrapText"]="1"
        if alignment: node(xf,"alignment",**alignment)
    names=node(root,"cellStyles",count="1"); node(names,"cellStyle",name="Normal",xfId="0",builtinId="0")
    return root,mapping
