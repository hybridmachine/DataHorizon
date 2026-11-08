"""Invariant numeric display and the deliberately small portable format grammar."""
from decimal import Context, Decimal, localcontext
import re


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
    spec=numeric_spec(style)
    if spec is None: return "@" if style.get("format")=="text" else "General"
    mode,places,grouped,currency=spec
    return ("#,##0" if grouped else "0")+("."+"0"*places if places else "")+("%" if mode=="percent" else '" '+currency+'"' if currency else "")


def simple_number_style(code):
    match=re.fullmatch(r'(#,##)?0(?:\.(0{1,15}))?(%|" [A-Z]{3}")?',code)
    if not match: return None
    places=len(match[2] or "");suffix=match[3] or ""
    currency=suffix[2:-1] if suffix.startswith('" ') else None
    mode="currency" if currency else "percent" if suffix=="%" else "number"
    if places==2 and not match[1] and not currency: return {"format":"percent2" if suffix=="%" else "fixed2"}
    return {"format":mode,"decimals":places,**({"grouping":True} if match[1] else {}),**({"currency":currency} if currency else {})}
