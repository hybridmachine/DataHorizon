# SPDX-License-Identifier: GPL-3.0-only
"""Bounded read-only inspection; never repairs data, evaluates formulas or runs code."""
import json
import sqlite3
import time

from .engine import EngineError, Workbook, require, sql_deadline, typed
from .formula import FormulaError, Parser

INSPECTION_CELLS = 10_000
INSPECTION_BYTES = 8*1024*1024


def inspect_inputs(book):
    result={"checked_cells":0,"checked_bytes":0,"complete":True,"issue_count":0,"issues":[],
            "max_cells":INSPECTION_CELLS,"max_bytes":INSPECTION_BYTES,"formula_check":"syntax only; no evaluation"}
    def issue(code,message,location=None):
        result["issue_count"]+=1
        if len(result["issues"])<100:
            result["issues"].append({**(location or {}),"code":code,"message":str(message)[:200]})
    with book.lock:
        deadline=time.monotonic()+5
        book.refresh(deadline=deadline)
        book.db.execute("BEGIN")
        result["state"]=book.state()
        previous=book.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,1024*1024)
        try:
            book.refresh(deadline=deadline)
            result["state"]=book.state()
            with sql_deadline(book.db,deadline):
                rows=book.db.execute("SELECT s.name,c.row,c.col,c.source,c.kind,c.style FROM cells c JOIN sheets s ON s.id=c.sheet_id ORDER BY c.sheet_id,c.row,c.col LIMIT ?",(INSPECTION_CELLS+1,))
                for name,row,col,source,kind,style in rows:
                    if result["checked_cells"]>=INSPECTION_CELLS:
                        result.update(complete=False,stop_reason="cell_limit"); break
                    require(time.monotonic()<=deadline,"TIMEOUT","Input inspection exceeded five seconds")
                    size=sum(len(v.encode()) if isinstance(v,str) else len(v) if isinstance(v,bytes) else 8 for v in (source,kind,style))
                    if result["checked_bytes"]+size>INSPECTION_BYTES:
                        result.update(complete=False,stop_reason="byte_limit"); break
                    result["checked_cells"]+=1; result["checked_bytes"]+=size
                    location={"sheet":name,"row":row,"col":col}
                    try:
                        require(isinstance(style,str) and len(style.encode())<=65536,"INVALID_STYLE","Style JSON must be text no larger than 64 KiB")
                        value=typed({"source":source,"kind":kind,"style":json.loads(style)})
                        if value["kind"]=="formula": Parser(source[1:]).parse()
                    except (EngineError,FormulaError,ValueError,TypeError,UnicodeError,RecursionError) as error:
                        issue(error.body["code"] if isinstance(error,EngineError) else "INVALID_FORMULA" if isinstance(error,FormulaError) else "INVALID_INPUT",error,location)
            book.verify_file()
        except EngineError as error:
            if error.body["code"]!="TIMEOUT": raise
            result.update(complete=False,stop_reason="time_limit")
        except sqlite3.Error as error:
            result.update(complete=False,stop_reason="storage_read_limit_or_error")
            issue("INPUT_SCAN_FAILED",error)
        finally:
            book.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,previous)
            book.db.execute("ROLLBACK")
    result["inputs_valid"] = result["complete"] and not result["issue_count"]
    return result


def inspect_native(path):
    book=Workbook(path,readonly=True)
    try:
        # Tie the input scan to the state whose opening integrity checks passed.
        initial_state=book.state()
        inputs=inspect_inputs(book)
        result=book.info()
        # Metadata cannot silently be presented as belonging to a newer state.
        require(result["state"]==inputs["state"]==initial_state,"STALE_STATE","Workbook changed during inspection; inspect again",409)
        return {"path":result["path"],"workbook_id":result["id"],"format_version":result["format_version"],
                "sheets":result["sheets"],"sqlite_integrity":"ok","readonly":True,"inputs":inputs,
                "report":["SQLite quick_check and native schema/catalog/coordinate validation passed within the opening budget.",
                          f'Input inspection checked {inputs["checked_cells"]} cells; {inputs["issue_count"]} issues; '+("complete." if inputs["complete"] else "incomplete: "+inputs["stop_reason"]+". Unchecked inputs are not validated."),
                          "Input inspection is limited to 10,000 cells / 8 MiB / five seconds, with a 1-MiB SQLite record bound. Formula syntax is checked without evaluation or export-compatibility promises.",
                          "No code, change handlers, recalculation or repairs were run. Inspection opens no persistent client handle. Retain the original and inspect a native copy for further recovery work."]}
    finally:
        book.close()
