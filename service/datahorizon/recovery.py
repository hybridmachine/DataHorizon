# SPDX-License-Identifier: GPL-3.0-only
"""Bounded read-only inspection; never repairs data, evaluates formulas or runs code."""
import json
import sqlite3
import time

from .engine import EngineError, Workbook, require, sql_deadline, typed
from .formula import FormulaError
from .formula_admission import status as formula_status
from .formula_refs import scan

INSPECTION_CELLS = 10_000
INSPECTION_BYTES = 8*1024*1024
INSPECTION_SAMPLES = 100


def inspect_inputs(book):
    """Check stored inputs without evaluating them. A formula the calculator can evaluate is valid; one it cannot, but
    whose references the lenient scanner reads (as imports store by design), and a cell of kind unmaintained are valid
    inputs counted under unevaluable_formulas and unmaintained_formulas; a formula source that cannot even be scanned
    is an issue."""
    result={"checked_cells":0,"checked_bytes":0,"complete":True,"issue_count":0,"issues":[],
            "unevaluable_formulas":{"count":0,"samples":[]},"unmaintained_formulas":{"count":0,"samples":[]},
            "max_cells":INSPECTION_CELLS,"max_bytes":INSPECTION_BYTES,"formula_check":"syntax and evaluability only; no evaluation"}
    def issue(code,message,location=None):
        result["issue_count"]+=1
        if len(result["issues"])<100:
            result["issues"].append({**(location or {}),"code":code,"message":str(message)[:200]})
    def counted(key,location,**detail):
        found=result[key]; found["count"]+=1
        if len(found["samples"])<INSPECTION_SAMPLES: found["samples"].append({**location,**detail})
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
                # Stored formulas name sheets by identity; either spelling is a sheet of this workbook.
                sheet_names=[value for row in book.db.execute("SELECT id,name FROM sheets") for value in row]
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
                        if value["kind"]=="unmaintained": counted("unmaintained_formulas",location)
                        elif value["kind"]=="formula" and (problem:=formula_status(source,sheet_names)) is not None:
                            if scan(source) is None: raise FormulaError("Formula source cannot be read: "+(problem[1] or problem[0]))
                            counted("unevaluable_formulas",location,reason=problem[0])
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
                          f'{inputs["unevaluable_formulas"]["count"]} stored formulas DataHorizon cannot evaluate and {inputs["unmaintained_formulas"]["count"]} '
                          "with unmaintained references are valid inputs (they calculate to an error naming the reason), not issues.",
                          "Input inspection is limited to 10,000 cells / 8 MiB / five seconds, with a 1-MiB SQLite record bound. Formula syntax is checked without evaluation or export-compatibility promises.",
                          "No code, change handlers, recalculation or repairs were run. Inspection opens no persistent client handle. Retain the original and inspect a native copy for further recovery work."]}
    finally:
        book.close()
