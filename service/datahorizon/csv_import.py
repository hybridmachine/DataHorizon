"""Bounded CSV decoding and review, shared by every managed client."""
import base64
import binascii
import csv
import io

from .engine import EngineError, MAX_CELLS, encoded, require

ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1")
MAX_SOURCE_BYTES = 3*1024*1024


def preview_csv(request):
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
    require(len(encoded(result).encode())<=4*1024*1024,"READ_LIMIT","CSV preview exceeds 4 MiB; select a smaller file")
    return result
