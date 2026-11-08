"""Invariant numeric display and the deliberately small portable format grammar."""
from decimal import Context, Decimal, localcontext
import re
from .temporal import Temporal, from_serial

TEMPORAL_FORMATS = {"date_iso":"yyyy-mm-dd", "date_mdy":"mm/dd/yyyy", "date_dmy":"dd/mm/yyyy",
                    "time24":"hh:mm:ss", "datetime_iso":"yyyy-mm-dd hh:mm:ss"}


def temporal_style(code):
    aliases={"mm-dd-yy":"date_iso","d-mmm-yy":"date_iso","d-mmm":"date_iso","mmm-yy":"date_iso",
             "m/d/yy":"date_mdy","mm/dd/yy":"date_mdy","d/m/yy":"date_dmy","dd/mm/yy":"date_dmy",
             "h:mm":"time24","h:mm:ss":"time24","h:mm am/pm":"time24","h:mm:ss am/pm":"time24",
             "m/d/yy h:mm":"datetime_iso","yyyy-mm-dd h:mm:ss":"datetime_iso"}
    folded=code.lower()
    mode=next((mode for mode,pattern in TEMPORAL_FORMATS.items() if folded==pattern),aliases.get(folded))
    if mode is None and re.fullmatch(r"hh?:mm:ss\.0{1,6}",folded):mode="time24"
    if mode is None and re.fullmatch(r"yyyy-mm-dd hh?:mm:ss\.0{1,6}",folded):mode="datetime_iso"
    return {"format":mode} if mode else None


def temporal_display(value, style):
    mode = style.get("format")
    if mode not in TEMPORAL_FORMATS: return str(value)
    if not isinstance(value,Temporal):
        try: value=from_serial(value,"time" if mode=="time24" else "datetime")
        except ValueError: return "####"
    parsed=value.parsed()
    if mode=="time24":
        return "00:00:00" if value.kind=="date" else parsed.strftime("%H:%M:%S")
    if value.kind=="time": return str(value)
    day=parsed.date() if value.kind=="datetime" else parsed
    if mode=="date_mdy":return f"{day.month:02}/{day.day:02}/{day.year:04}"
    if mode=="date_dmy":return f"{day.day:02}/{day.month:02}/{day.year:04}"
    if mode=="datetime_iso":return day.isoformat()+" "+("00:00:00" if value.kind=="date" else parsed.strftime("%H:%M:%S"))
    return day.isoformat()


def numeric_spec(style):
    mode=style.get("format", "number" if "decimals" in style or "grouping" in style else "general")
    if mode not in ("fixed2","percent2","number","percent","currency"): return None
    return ("percent" if mode in ("percent","percent2") else "currency" if mode=="currency" else "number",
            2 if mode in ("fixed2","percent2") else style.get("decimals",2), style.get("grouping",False), style.get("currency","USD") if mode=="currency" else None)


def numeric_display(value,style):
    spec=numeric_spec(style)
    if spec is None: return format(value,".15g")
    mode,places,grouped,currency=spec
    separator="," if grouped else ""
    if mode=="percent":
        with localcontext(Context(prec=340)):
            return f"{Decimal(str(value))*100:{separator}.{places}f}%"
    return f"{value:{separator}.{places}f}"+(" "+currency if currency else "")


def number_format_code(style):
    if style.get("format") in TEMPORAL_FORMATS:return TEMPORAL_FORMATS[style["format"]]
    spec=numeric_spec(style)
    if spec is None: return "@" if style.get("format")=="text" else "General"
    mode,places,grouped,currency=spec
    return ("#,##0" if grouped else "0")+("."+"0"*places if places else "")+("%" if mode=="percent" else '" '+currency+'"' if currency else "")


def simple_number_style(code):
    temporal=temporal_style(code)
    if temporal:return temporal
    match=re.fullmatch(r'(#,##)?0(?:\.(0{1,15}))?(%|" [A-Z]{3}")?',code)
    if not match: return None
    places=len(match[2] or "");suffix=match[3] or ""
    currency=suffix[2:-1] if suffix.startswith('" ') else None
    mode="currency" if currency else "percent" if suffix=="%" else "number"
    if places==2 and not match[1] and not currency: return {"format":"percent2" if suffix=="%" else "fixed2"}
    return {"format":mode,"decimals":places,**({"grouping":True} if match[1] else {}),**({"currency":currency} if currency else {})}
