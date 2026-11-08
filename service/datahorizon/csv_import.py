"""Bounded CSV decoding and review, shared by every managed client."""
import base64
import binascii
import csv
from datetime import date
import io
import re

from .engine import EngineError, MAX_CELLS, boolean, encoded, require, typed
from .temporal import Temporal
from .csv_types import temporal_schema
from .conversion import convert,PROFILES
from .temporal import import_temporal

ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1")
COLUMN_TYPES = ("text", "number", "boolean", "date_iso", "date_mdy", "date_dmy", "time_iso", "datetime_iso", "date_locale", "datetime_locale", "timestamp_utc")
COLUMN_LABELS = {"text":"Text","number":"Number","boolean":"Boolean",
                 "date_iso":"Date YYYY-MM-DD","date_mdy":"Date M/D/YYYY","date_dmy":"Date D/M/YYYY", "time_iso":"Time ISO", "datetime_iso":"Datetime ISO", "date_locale":"Date in selected locale", "datetime_locale":"Datetime in selected locale", "timestamp_utc":"Timestamp normalized to UTC"}
MAX_SOURCE_BYTES = 3*1024*1024


def preview_csv(request, checkpoint=lambda completed, phase: None):
    checkpoint(0, "Decoding")
    require(("csv" in request) != ("csv_base64" in request), "INVALID_CSV",
            "Provide exactly one of csv text or csv_base64 bytes")
    delimiter = request.get("delimiter", ",")
    require(isinstance(delimiter,str) and len(delimiter)==1 and delimiter not in '\r\n\0"',
            "INVALID_CSV", "Delimiter must be one character other than a quote, NUL or line ending")
    encoding = "unicode"
    if "csv_base64" in request:
        encoding = request.get("encoding","utf-8-sig")
        require(isinstance(encoding,str) and encoding in ENCODINGS, "INVALID_CSV", "Unsupported CSV encoding")
        data = request["csv_base64"]
        require(isinstance(data,str) and len(data)<=4*1024*1024, "INVALID_CSV", "Provide at most 3 MiB of base64-encoded CSV bytes")
        try:
            raw = base64.b64decode(data,validate=True)
        except (ValueError,binascii.Error) as error:
            raise EngineError("INVALID_CSV","Invalid base64 CSV data") from error
        require(len(raw)<=MAX_SOURCE_BYTES,"LIMIT","CSV source exceeds 3 MiB")
        require(encoding!="utf-16" or raw.startswith((b"\xff\xfe",b"\xfe\xff")),
                "INVALID_CSV_ENCODING","UTF-16 CSV requires a byte-order mark")
        try:
            text = raw.decode(encoding,errors="strict")
        except UnicodeError as error:
            raise EngineError("INVALID_CSV_ENCODING",f"CSV bytes are invalid for {encoding}; choose the correct encoding") from error
    else:
        require("encoding" not in request,"INVALID_CSV","Encoding applies to csv_base64 bytes; csv text is already Unicode")
        text = request["csv"]
        require(isinstance(text,str),"INVALID_CSV","Provide CSV text")
    try:
        text_bytes = len(text.encode("utf-8"))
        delimiter.encode("utf-8")
    except UnicodeEncodeError as error:
        raise EngineError("INVALID_CSV","CSV text and delimiter must contain valid Unicode") from error
    require(text_bytes<=MAX_SOURCE_BYTES,"LIMIT","Decoded CSV text exceeds 3 MiB of UTF-8")
    rows, count = [], 0
    try:
        for row in csv.reader(io.StringIO(text,newline=""),delimiter=delimiter,strict=True):
            checkpoint(count, "Parsing")
            require(len(rows)<MAX_CELLS,"LIMIT","CSV preview/import exceeds 10,000 records")
            count += len(row)
            require(count<=MAX_CELLS,"LIMIT","CSV preview/import exceeds 10,000 cells")
            require(all(len(value.encode("utf-8"))<=65536 for value in row),
                    "INVALID_INPUT","CSV cell source exceeds 64 KiB of UTF-8")
            rows.append(row)
    except csv.Error as error:
        raise EngineError("INVALID_CSV",str(error)) from error
    result = {"rows":rows,"cells":count,"default_kind":"text","encoding":encoding,"delimiter":delimiter,
              "report":[f"Encoding: {encoding}; delimiter: {delimiter!r}.",
                        "All columns remain Text, preserving leading zeros and formula-like text."]}
    convert_columns(request,result,checkpoint)
    if "temporal_schema" in request:
        schema=request["temporal_schema"]
        require(not request.get("column_types") and not request.get("header"),"INVALID_CSV_TYPES","Companion types cannot be combined with column conversions or header overrides")
        require(isinstance(schema,dict) and set(schema)=={"version","sha256","cells"} and type(schema["version"]) is int and schema["version"]==1,
                "INVALID_CSV_TYPES","Unsupported temporal CSV companion")
        require(schema["sha256"]==temporal_schema(text,[])["sha256"],"INVALID_CSV_TYPES","CSV companion hash does not match the decoded CSV")
        entries=schema["cells"]
        require(isinstance(entries,list) and len(entries)<=MAX_CELLS,"INVALID_CSV_TYPES","Temporal companion exceeds 10,000 cells")
        inputs=[[{"source":value,"kind":"text"} for value in row] for row in rows];seen=set()
        for item in entries:
            require(isinstance(item,dict) and set(item)=={"row","col","kind"},"INVALID_CSV_TYPES","Invalid temporal companion entry")
            row,col,kind=item["row"],item["col"],item["kind"]
            require(type(row) is int and 1<=row<=len(rows) and type(col) is int and 1<=col<=len(rows[row-1]) and isinstance(kind,str) and kind in ("date","time","datetime") and (row,col) not in seen,
                    "INVALID_CSV_TYPES","Invalid temporal companion coordinates or kind")
            seen.add((row,col))
            try:Temporal(rows[row-1][col-1],kind)
            except ValueError as error:raise EngineError("INVALID_CSV_TYPES",str(error)) from error
            inputs[row-1][col-1]["kind"]=kind
        result.update(inputs=inputs,converted_cells=len(entries))
        result["report"].append(f"Restored {len(entries)} native date/time kinds from the hash-checked companion; other fields remain Text.")
    require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","CSV preview exceeds 4 MiB; select a smaller file")
    checkpoint(count, "Ready for review")
    return result


def convert_columns(request,result,checkpoint=lambda completed, phase: None):
    profile=request.get("locale","invariant");require(isinstance(profile,str) and profile in PROFILES,"INVALID_CONVERSION","Choose an explicit locale profile")
    rows = result["rows"]
    width = max(map(len,rows),default=0)
    types = request.get("column_types",{})
    header = boolean(request.get("header",False),"header")
    require(isinstance(types,dict) and len(types)<=width,"INVALID_CSV_TYPES","Column types must map CSV column numbers to explicit types")
    for column,kind in types.items():
        require(isinstance(column,str) and re.fullmatch(r"[1-9][0-9]{0,4}",column) is not None
                and int(column)<=width and isinstance(kind,str) and kind in COLUMN_TYPES,
                "INVALID_CSV_TYPES","Column types require existing 1-based CSV columns and a supported type")
    result.update(columns=width,column_types=types,header=header,can_import=bool(result["cells"]),
                  conversion_errors=0,conversion_issues=[],converted_cells=0,ambiguous_dates=0,date_examples=[])
    if not types:
        return
    inputs, errors, issues, converted, ambiguous, examples = [], 0, [], 0, 0, []
    for row_number,row in enumerate(rows,1):
        checkpoint(row_number-1, "Converting rows")
        items=[]
        for column,value in enumerate(row,1):
            kind = "text" if header and row_number==1 else types.get(str(column),"text")
            item={"source":value,"kind":"text"}
            try:
                if kind!="text" and value=="":
                    item={"source":"","kind":"auto"}
                elif kind in ('date_locale','datetime_locale','timestamp_utc') or (kind=='number' and profile!='invariant'):
                    result_kind={'date_locale':'date','datetime_locale':'datetime'}.get(kind,kind)
                    conversion=convert(dict(source=value,kind=result_kind,locale=profile));item=conversion['cell'];ambiguous+=int(conversion['ambiguous_date']);converted+=1
                elif kind in ("number","boolean"):
                    item["kind"]=kind
                    typed({**item,"style":{}})
                    converted+=1
                elif kind in ("time_iso","datetime_iso"):
                    item["kind"]="time" if kind=="time_iso" else "datetime"
                    item["source"]=str(import_temporal(value,item["kind"]))
                    converted+=1
                elif kind.startswith("date_"):
                    if kind=="date_iso":
                        require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",value) is not None,
                                "INVALID_DATE","Expected YYYY-MM-DD")
                        parsed=date.fromisoformat(value)
                    else:
                        match=re.fullmatch(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})",value)
                        require(match is not None,"INVALID_DATE","Expected M/D/YYYY or D/M/YYYY in the selected order")
                        first,second,year=map(int,match.groups())
                        month,day=(first,second) if kind=="date_mdy" else (second,first)
                        parsed=date(year,month,day)
                        ambiguous+=int(first<=12 and second<=12 and first!=second)
                    item["source"]=parsed.isoformat()
                    item["kind"]="date"
                    converted+=1
                    if len(examples)<20:
                        examples.append({"row":row_number,"col":column,"source":value,"result":item["source"]})
            except (EngineError,ValueError) as error:
                errors+=1
                if len(issues)<100:
                    issues.append({"row":row_number,"col":column,"source":value[:120],
                                   "code":error.body["code"] if isinstance(error,EngineError) else "INVALID_DATE",
                                   "message":str(error)})
                item=None
            items.append(item)
        inputs.append(items)
    result.update(conversion_errors=errors,conversion_issues=issues,converted_cells=converted,
                  ambiguous_dates=ambiguous,date_examples=examples,can_import=bool(result["cells"]) and errors==0)
    if errors==0:
        result["inputs"]=inputs  # A failed conversion never supplies an applicable typed payload.
    selected=sorted(types.items(),key=lambda entry:int(entry[0]))
    result["report"]=[result["report"][0],
        f"Explicit locale profile: {profile}. "+f"{len(types)} explicit column types (first 20): "+", ".join(f"{column}={COLUMN_LABELS[kind]}" for column,kind in selected[:20])+"; unspecified columns remain Text.",
        f"{converted} typed/date values; {errors} conversion errors. First record stays Text: {'yes' if header else 'no'}.",
        f"{ambiguous} ambiguous slash dates use the explicitly selected order. Dates/times use native ISO kinds. Empty converted fields become blank."]
