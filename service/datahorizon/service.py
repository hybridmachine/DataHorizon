"""Authenticated loopback-only development REST service; no third-party runtime."""

import argparse
from contextlib import contextmanager
import hmac
from http.client import LineTooLong
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
from pathlib import Path
import secrets
from socketserver import TCPServer
import sqlite3
import sys
import threading
import time
import traceback
from urllib.parse import unquote, urlsplit
import uuid

from .engine import EngineError, MAX_CELLS, Workbook, boolean, encoded, require
from .formula import MAX_COLS, MAX_ROWS, SUPPORTED_FUNCTIONS
from .csv_import import COLUMN_TYPES, ENCODINGS, preview_csv
from .jobs import PreparationJobs
from .interchange import xlsx_preview, xlsx_export, preparation_scope
from .ods import ods_preview, ods_export
from .recovery import inspect_native

CAPABILITIES = {
    "api_version": 1, "format_version": 1, "max_operation_cells": MAX_CELLS,
    "max_summary_cells":MAX_CELLS, "max_viewport_cells": 4096, "max_clipboard_cells":MAX_CELLS, "max_request_bytes": 4 * 1024 * 1024,
    "formulas": ["arithmetic", "references", *sorted(SUPPORTED_FUNCTIONS)],
    "operations": ["workbook_import", "edit", "paste", "move", "fill", "replace_all", "undo", "redo", "sheet_add", "sheet_rename", "sheet_duplicate", "sheet_reorder", "sheet_delete", "rows_insert", "rows_delete", "columns_insert", "columns_delete"],
    "interchange": ["csv", "xlsx", "ods"], "managed_sql": False, "remote_binding": False,
    "follow_up_work": False, "chunked_jobs": False, "preparation_jobs": True,
    "max_preparation_jobs":4, "preparation_job_seconds":10, "preparation_retention_seconds":300,
    "max_open_workbooks":8, "max_open_handles":128,
    "max_concurrent_requests":16, "max_header_bytes":65536, "request_read_timeout_seconds":10,
}

ROUTES = {
    "/v1/capabilities": ("get", "Preview capabilities"),
    "/v1/open": ("post", "Create or open a server-local native workbook"),
    "/v1/recovery/inspect": ("post", "Read-only native-file inspection without code activation or repair"),
    "/v1/import/csv/preview": ("post", "Decode CSV and review explicit column conversions without mutation"),
    "/v1/import/csv/jobs": ("post", "Start a cancellable CSV preparation; never writes workbook data"),
    "/v1/import/ods/jobs": ("post", "Prepare bounded ODS selected-sheet or whole-workbook inputs with conversion review"),
    "/v1/workbooks/{workbook}/export/ods": ("post", "Bounded ODS selection or whole-workbook snapshot and conversion report"),
    "/v1/import/xlsx/jobs": ("post", "Prepare bounded selected-sheet or whole-workbook XLSX with conversion review"),
    "/v1/workbooks/{workbook}/export/xlsx": ("post", "Bounded XLSX selection or whole-workbook snapshot and conversion report"),
    "/v1/jobs/{job}": ("get", "Ephemeral preparation status, progress and reviewed result"),
    "/v1/jobs/{job}/cancel": ("post", "Cancel preparation or discard its completed result"),
    "/v1/jobs/{job}/release": ("post", "Release terminal preparation storage"),
    "/v1/workbooks/{workbook}": ("get", "Workbook state and sheets"),
    "/v1/workbooks/{workbook}/range": ("post", "Bounded consistent viewport read"),
    "/v1/workbooks/{workbook}/clipboard": ("post", "Consistent full-source clipboard read, at most 10,000 cells and 4 MiB"),
    "/v1/workbooks/{workbook}/list": ("post", "Explicit full-scope sorted/filtered list projection"),
    "/v1/workbooks/{workbook}/summary": ("post", "Count and numeric summary of one bounded stored-data snapshot"),
    "/v1/workbooks/{workbook}/find": ("post", "Find in a declared source or display scope"),
    "/v1/workbooks/{workbook}/operations": ("post", "Atomic managed operation"),
    "/v1/workbooks/{workbook}/operations/{operation}": ("get", "Durable outcome lookup"),
    "/v1/workbooks/{workbook}/snapshot": ("post", "Exclusive consistent native copy"),
    "/v1/workbooks/{workbook}/close": ("post", "Release this client handle; other client handles remain valid"),
    "/v1/workbooks/{workbook}/export/csv": ("post", "Consistent values-only CSV export"),
}


def openapi():
    row = {"type":"integer","minimum":1,"maximum":MAX_ROWS}
    col = {"type":"integer","minimum":1,"maximum":MAX_COLS}
    rectangle = {"r1":{**row,"default":1},"c1":{**col,"default":1},
                 "r2":{**row,"description":"Inclusive final row; defaults to r1"},
                 "c2":{**col,"description":"Inclusive final column; defaults to c1"}}
    sheet = {"type":"string","minLength":1,"description":"Sheet name or stable sheet ID","default":"Sheet1"}
    kinds = ["auto","formula","text","number","boolean","null"]
    scalar = {"oneOf":[{"type":"string","nullable":True},{"type":"number"},{"type":"boolean"}]}
    name = {"type":"string","minLength":1,"maxLength":31,"pattern":r"^[^\[\]:*?/\\!\x00-\x1f]+$"}
    schemas = {
        "Error": {"type":"object", "required":["code","message","outcome"], "properties":{
            "code":{"type":"string"},"message":{"type":"string"},
            "outcome":{"type":"string","enum":["rejected","rolled_back","unknown"]},"operation_id":{"type":"string"}}},
        "OpenRequest": {"type":"object","required":["path"],"properties":{
            "path":{"type":"string","description":"Absolute path on the localhost service host"},
            "create":{"type":"boolean","default":False},"readonly":{"type":"boolean","default":False}},
            "example":{"path":"/tmp/example.dhwb","create":True}},
        "RangeRequest": {"type":"object","properties":{
            "preview":{"type":"boolean","default":False,"description":"Bounded display/input previews; omitted input/value fields are explicitly flagged and cannot be pasted as full data"},
            "sheet":sheet,**rectangle,
            "rows":{"type":"array","minItems":1,"maxItems":256,"items":row,"description":"Optional explicit source rows for projections; replaces r1/r2"}},
            "description":"At most 4,096 viewport cells and a 4 MiB response. Without coordinates, reads default to A1.",
            "example":{"sheet":"Sheet1","r1":1,"c1":1,"r2":32,"c2":16}},
        "Style":{"type":"object","additionalProperties":False,"properties":{
            "format":{"type":"string","enum":["general","text","fixed2","percent2"]},
            "bold":{"type":"boolean"},"italic":{"type":"boolean"},"wrap":{"type":"boolean"},
            "align":{"type":"string","enum":["left","center","right"]},
            "border":{"type":"string","enum":["none","box"]},
            "background":{"type":"string","pattern":"^#[0-9a-fA-F]{6}$"}}},
        "CellEdit": {"type":"object","required":["row","col"],"properties":{
            "row":row,"col":col,"source":{"type":"string","maxLength":65536,"description":"Stored input, at most 64 KiB of UTF-8"},
            "kind":{"type":"string","enum":kinds,"default":"auto"},
            "style":{"$ref":"#/components/schemas/Style"},
            "clear":{"type":"boolean"},"clear_input":{"type":"boolean"},"replace_style":{"type":"boolean"}}},
        "ClipboardCell":{"type":"object","description":"A full clipboard cell, or {} for an absent cell. Full paste preserves input_kind and translates formula source using the origin coordinates.","properties":{
            "source":{"type":"string"},"input_kind":{"type":"string","enum":kinds,"default":"text"},
            "kind":{"type":"string","enum":[*kinds,"blank"]},"value":scalar,"style":{"$ref":"#/components/schemas/Style"},
            "calculation":{"type":"string","enum":["current","error","pending"]},"error":{"type":"string"},
            "source_omitted":{"type":"boolean"},"value_omitted":{"type":"boolean"}}},
        "Receipt": {"type":"object","required":["operation_id","outcome","state","changed_cells"],"properties":{
            "operation_id":{"type":"string"},"outcome":{"type":"string","enum":["committed"]},"state":{"type":"string"},"changed_cells":{"type":"integer"},
            "reference_warnings":{"type":"integer","description":"Newly broken reference expressions in a row/column operation"},
            "reference_issues":{"type":"array","maxItems":100,"description":"First 100 affected formula locations after the structure change","items":{"type":"object","properties":{"sheet":sheet,"row":row,"col":col}}}}},
    }
    schemas["SummaryRequest"]={"type":"object","additionalProperties":False,"properties":{"sheet":sheet,**rectangle},
        "description":"One rectangle up to 10,000 cells, 4 MiB stored input/style budget and 2 seconds; shared formula budgets apply. No hooks or history writes. Count includes non-NULL values and formula errors, including empty Text. Numeric summaries exclude Text/Booleans and are unavailable on any formula error or nonfinite running subtotal. Numeric values are unformatted (percentage fractions unscaled).",
        "example":{"sheet":"Sheet1","r1":1,"c1":1,"r2":100,"c2":3}}
    schemas["Summary"]={"type":"object","required":["state","sheet","range","selected_cells","count","numeric_count","sum","average","error_count","errors","errors_truncated","numeric_error"],"properties":{
        "state":{"type":"string"},"sheet":sheet,"range":{"type":"array","minItems":4,"maxItems":4,"items":{"type":"integer"}},
        **{key:{"type":"integer","minimum":0} for key in ("selected_cells","count","numeric_count","error_count")},
        "sum":{"type":"number","nullable":True},"average":{"type":"number","nullable":True},
        "numeric_error":{"type":"string","nullable":True,"description":"#NUM! for nonfinite running subtotal"},
        "errors_truncated":{"type":"boolean"},"errors":{"type":"array","maxItems":100,"items":{"type":"object","required":["row","col","code"],"properties":{"row":row,"col":col,"code":{"type":"string"}}}}}}
    schemas["ClipboardRequest"] = {**schemas["RangeRequest"],
        "properties":{key:value for key,value in schemas["RangeRequest"]["properties"].items() if key!="preview"},
        "description":"Full inputs and calculated values, at most 10,000 cells and a 4 MiB response; no viewport previews. Without coordinates, reads default to A1."}
    common = {"operation_id":{"type":"string","minLength":1,"maxLength":100,"description":"Valid Unicode text; preserve before sending and reconcile lost responses by this ID"},
              "expected_state":{"type":"string","description":"State from the reviewed workbook; stale writes roll back"},"label":{"type":"string"}}
    search = {"needle":{"type":"string","minLength":1},"field":{"type":"string","enum":["source","display"],"default":"source"}}
    actions = {
        "edit": (["cells"],{"sheet":sheet,"cells":{"type":"array","minItems":1,"maxItems":MAX_CELLS,"items":{"$ref":"#/components/schemas/CellEdit"}}},
                 {"cells":[{"row":1,"col":1,"source":"7"}]}),
        "paste": (["row","col","rows"],{"sheet":sheet,"row":row,"col":col,"origin_row":row,"origin_col":col,
                  "mode":{"type":"string","enum":["all","values","formats"],"default":"all"},
                  "rows":{"type":"array","minItems":1,"items":{"type":"array","items":{"$ref":"#/components/schemas/ClipboardCell"}},"description":"At most 10,000 total cells. Missing origin coordinates default to destination. Values-only paste rejects formula errors and pending results."}},
                  {"row":2,"col":1,"rows":[[{"source":"8","input_kind":"text"}]]}),
        "fill": ([],{"sheet":sheet,**rectangle,"mode":{"type":"string","enum":["copy","series","down","right"],"default":"copy"},"step":{"type":"number","default":1}},
                 {"r1":1,"c1":1,"r2":2,"c2":1,"mode":"series","step":2}),
        "move": (["row","col","cut_state"],{"sheet":sheet,**rectangle,"row":row,"col":col,"cut_state":{"type":"string","description":"State captured by Cut; must still match. Same-sheet scalar/fully-contained range references follow the move; partial referenced-range intersections reject atomically."},"mode":{"type":"string","enum":["all"],"default":"all"}},
                 {"r1":1,"c1":1,"r2":1,"c2":1,"row":3,"col":1,"cut_state":"copy-from-workbook-info"}),
        "replace_all": (["needle","replacement"],{"sheet":sheet,**rectangle,**search,"replacement":{"type":"string"}},
                        {"r1":1,"c1":1,"r2":3,"c2":1,"needle":"7","replacement":"9"}),
        "undo": ([],{},{}), "redo": ([],{},{}),
        "workbook_import": (["sheets"],{"sheets":{"type":"array","minItems":1,"maxItems":64,"items":{"type":"object","required":["name","cells"],"properties":{"name":name,"cells":{"type":"array","items":{"$ref":"#/components/schemas/CellEdit"}}}}}},{"sheets":[{"name":"Sheet1","cells":[]}]}),
        "sheet_add": ([],{"name":{**name,"default":"Sheet"}},{"name":"Other"}),
        "sheet_rename": (["sheet","name"],{"sheet":sheet,"name":name},{"sheet":"Other","name":"Renamed"}),
        "sheet_duplicate": (["sheet","name"],{"sheet":sheet,"name":name},{"sheet":"Renamed","name":"Copy"}),
        "sheet_reorder": (["order"],{"order":{"type":"array","minItems":1,"uniqueItems":True,"items":{"type":"string"},"description":"Every current sheet ID exactly once, in desired display order"}},{"order":["replace-with-all-current-sheet-ids"]}),
        "sheet_delete": (["sheet"],{"sheet":sheet},{"sheet":"Copy"}),
    }
    mapping = {}
    for axis, maximum in (("rows",MAX_ROWS),("columns",MAX_COLS)):
        for verb in ("insert","delete"):
            actions[axis+"_"+verb] = (["start"],{"sheet":sheet,"start":{"type":"integer","minimum":1,"maximum":maximum},
                "count":{"type":"integer","minimum":1,"maximum":maximum,"default":1,"description":"Must fit between start and the grid edge"}},
                {"sheet":"Sheet1","start":2,"count":1})
    for action, (required, properties, example) in actions.items():
        schema_name = "".join(word.title() for word in action.split("_"))+"Operation"
        mapping[action] = "#/components/schemas/"+schema_name
        schemas[schema_name] = {"type":"object","required":["operation_id","expected_state","action",*required],
            "properties":{**common,"action":{"type":"string","enum":[action]},**properties},
            "example":{"operation_id":"generate-a-uuid","expected_state":"copy-from-workbook-info","action":action,**example}}
    schemas["OperationRequest"] = {"oneOf":[{"$ref":ref} for ref in mapping.values()],
        "discriminator":{"propertyName":"action","mapping":mapping},
        "description":"Atomic operations: at most 10,000 affected cells and a 4 MiB request. Range endpoints are inclusive and must be ordered. Copy fill uses the top-left input; series requires a numeric seed. Move requires no formula sources in the workbook. Sheet rename updates qualified name references while preserving stable UUID references, scanning at most 10,000 formulas/8 MiB. Sheet duplication preserves other-sheet references and retargets copied self-references, including UUIDs; it copies at most 10,000 stored cells/8 MiB. Sheet deletion replaces surviving name/UUID references and inherited ranges with permanent #REF! errors and reports affected locations; deleted and rewritten cells share the 10,000-cell limit. Unsupported syntax and required hooks reject reference-changing structure operations. Row/column changes retarget absolute and relative A1 references across sheets, expand/contract ranges and report newly broken references; required handlers, unsupported formula syntax and grid overflow reject atomically. Structure changes clear history. No managed SQL or chunked commits.",
        "example":schemas["EditOperation"]["example"]}
    schemas.update({
        "CsvPreviewRequest":{"type":"object","oneOf":[{"required":["csv"],"not":{"anyOf":[{"required":["csv_base64"]},{"required":["encoding"]}]}},{"required":["csv_base64"],"not":{"required":["csv"]}}],
            "properties":{"csv":{"type":"string","description":"Already decoded Unicode text; at most 3 MiB of UTF-8"},
                "csv_base64":{"type":"string","format":"byte","description":"Strict base64 of at most 3 MiB of source bytes; total JSON request remains limited to 4 MiB"},
                "encoding":{"type":"string","enum":list(ENCODINGS),"default":"utf-8-sig","description":"Strict byte decoding; UTF-16 requires a BOM"},
                "column_types":{"type":"object","additionalProperties":{"type":"string","enum":list(COLUMN_TYPES)},"description":"1-based CSV column numbers as keys; unspecified columns stay Text. Numbers/booleans use strict native input rules. Dates normalize to ISO Text. Conversion errors set can_import=false and omit inputs; successful typed previews return inputs for one managed edit."},
                "header":{"type":"boolean","default":False,"description":"Keep the first CSV record as Text, including typed columns; do not skip it"},
                "delimiter":{"type":"string","minLength":1,"maxLength":1,"default":",","description":"Cannot be a quote, NUL, CR or LF"}},
            "example":{"csv":"ID,Value\n00123,7"}},
        "SnapshotRequest":{"type":"object","required":["path"],"properties":{"path":{"type":"string","description":"New server-local destination; never overwrite an existing file"}},"example":{"path":"/tmp/copy.dhwb"}},
        "CloseRequest":{"type":"object","example":{}},
        "FindRequest":{"type":"object","required":["needle"],"properties":{"sheet":sheet,**rectangle,**search},"example":{"sheet":"Sheet1","r2":10,"c2":4,"needle":"Pen"}},
        "CsvExportRequest":{"type":"object","properties":{"sheet":sheet,**rectangle},"example":{"sheet":"Sheet1","r2":10,"c2":4}},
        "ListRequest":{"type":"object","properties":{"sheet":sheet,**rectangle,"header":{"type":"boolean","default":False},
            "sort":{"type":"array","maxItems":8,"items":{"type":"object","required":["col"],"properties":{"col":col,"descending":{"type":"boolean","default":False}}}},
            "filters":{"type":"array","maxItems":8,"items":{"type":"object","required":["col","op"],"properties":{"col":col,"op":{"type":"string","enum":["=","<>",">","<",">=","<=","contains"]},"value":scalar}}}},
            "example":{"sheet":"Sheet1","r2":10,"c2":4,"header":True,"sort":[{"col":2,"descending":True}]}}
    })
    schemas["PreparationRequest"] = {**schemas["CsvPreviewRequest"], "required":["job_id"],
        "properties":{**schemas["CsvPreviewRequest"]["properties"],"job_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$"}},
        "description":"Ephemeral, idempotent input preparation: one active job, four retained jobs, ten seconds each, five-minute retention. No mutation or chunked commit. Reuse the same ID and input after a lost acknowledgement. Apply reviewed inputs as one ordinary managed edit."}
    schemas["SpreadsheetPreparation"] = {"type":"object","required":["job_id","file_base64"],"properties":{
        "job_id":schemas["PreparationRequest"]["properties"]["job_id"],
        "file_base64":{"type":"string","format":"byte","description":"At most 3 MiB of ZIP data / 16 MiB expanded, 128 entries"},
        "scope":{"type":"string","enum":["selection","workbook"],"default":"selection"},
        "sheet_index":{"type":"integer","minimum":1,"maximum":64,"default":1},"row":row,"col":col,
        "mode":{"type":"string","enum":["formulas","values"],"default":"formulas"}},
        "description":"Selected sheet or whole-workbook inputs, at most 10,000 stored cells total. Whole-workbook XLSX/ODS requires an empty destination and no required handler, replaces sheets atomically and clears history. Values mode uses unverified formula caches. Issues block the entire applicable payload. Explicit reviewed Apply is one managed edit; no code activation."}
    schemas["SpreadsheetExport"] = {**schemas["CsvExportRequest"],"properties":{**schemas["CsvExportRequest"]["properties"],
        "scope":{"type":"string","enum":["selection","workbook"],"default":"selection"},
        "mode":{"type":"string","enum":["formulas","values"],"default":"formulas"}}}
    schemas["JobAction"] = {"type":"object","properties":{}}
    paths = {}
    for path, (method, summary) in ROUTES.items():
        parameters = [{"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
                      for name in ("workbook", "operation", "job") if "{" + name + "}" in path]
        operation = {"summary": summary, "parameters": parameters,
                     "responses": {"200": {"description": "JSON result"}, **{str(status):{"description":"Structured error; inspect outcome and operation_id before deciding whether a mutation finished"} for status in (400,401,403,404,408,409,413,500,503)},
                                   "431":{"description":"HTTP parser rejected oversized headers before dispatch; no operation receipt is implied"}},
                     "security": [{"localToken": []}]}
        if method == "post":
            name = "SnapshotRequest" if path == "/v1/recovery/inspect" else "SpreadsheetPreparation" if path in ("/v1/import/xlsx/jobs","/v1/import/ods/jobs") else "SpreadsheetExport" if path.endswith(("/export/xlsx","/export/ods")) else "PreparationRequest" if path == "/v1/import/csv/jobs" else "JobAction" if path.startswith("/v1/jobs/") else "OpenRequest" if path == "/v1/open" else "CsvPreviewRequest" if path == "/v1/import/csv/preview" else "RangeRequest" if path.endswith("/range") else "ClipboardRequest" if path.endswith("/clipboard") else "OperationRequest" if path.endswith("/operations") else {
                "summary":"SummaryRequest","list":"ListRequest","find":"FindRequest","snapshot":"SnapshotRequest","close":"CloseRequest","csv":"CsvExportRequest"}[path.rsplit("/",1)[-1]]
            schema = {"$ref":"#/components/schemas/"+name}
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": schema}}}
        for status in ("400","401","403","404","409","413","500","503"):
            operation["responses"][status]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/Error"}}}
        if path.endswith("/summary"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/Summary"}}}
        elif path.endswith("/operations"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/Receipt"}}}
        elif path.endswith("/{operation}"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"oneOf":[{"$ref":"#/components/schemas/Receipt"},{"$ref":"#/components/schemas/Error"}]}}}
        paths[path] = {method: operation}
    return {"openapi": "3.0.3", "info": {"title": "DataHorizon local development API", "version": "0.1.0"},
            "paths": paths, "components": {"schemas":schemas,"securitySchemes": {"localToken": {"type": "http", "scheme": "bearer"}}}}


EXPLORER = """<!doctype html><meta charset="utf-8"><title>DataHorizon API explorer</title>
<style>body{font:16px system-ui;max-width:900px;margin:40px auto;padding:0 20px}input,select,textarea,button{font:inherit;margin:6px 0;padding:8px}textarea,input{box-sizing:border-box;width:100%}pre{white-space:pre-wrap;background:#eee;padding:16px}label{display:block}[hidden]{display:none}</style>
<h1>DataHorizon API explorer</h1><p>Loopback development service. <a href="/openapi.json">OpenAPI schema</a>.</p>
<label>Resource<select id="endpoint"></select></label>
<label hidden>Operation<select id="example"></select></label>
<label>Local bearer token<input id="token" type="password" autocomplete="off"></label>
<label>Method<select id="method"><option>GET</option><option>POST</option></select></label>
<label>Path<input id="path" value="/v1/capabilities"></label>
<label>JSON body<textarea id="body" rows="8">{}</textarea></label><button id="run">Send request</button><pre id="result">Ready</pre>
<script>fetch('/openapi.json').then(r=>r.json()).then(api=>{let select=document.querySelector('#endpoint');
for(let [path,methods] of Object.entries(api.paths))for(let [method,op] of Object.entries(methods)){
let option=document.createElement('option');option.textContent=method.toUpperCase()+' '+path+' — '+op.summary;option.value=JSON.stringify([path,method,op]);select.append(option)}
select.onchange=()=>{let[path,method,op]=JSON.parse(select.value);document.querySelector('#path').value=path;document.querySelector('#method').value=method.toUpperCase();
let ref=op.requestBody?.content?.['application/json']?.schema?.['$ref'],schema=ref?api.components.schemas[ref.split('/').pop()]:{};
let variants=schema.oneOf?schema.oneOf.map(v=>api.components.schemas[v['$ref'].split('/').pop()]):[schema],examples=document.querySelector('#example');examples.replaceChildren();
for(let variant of variants){let option=document.createElement('option');option.textContent=variant.example?.action||'Request';option.value=JSON.stringify(variant.example||{});examples.append(option)}
examples.parentElement.hidden=variants.length<2;examples.onchange=()=>{document.querySelector('#body').value=JSON.stringify(JSON.parse(examples.value),null,2)};examples.onchange()};select.onchange()});
document.querySelector('#run').onclick=async()=>{let out=document.querySelector('#result');try{
let method=document.querySelector('#method').value,path=document.querySelector('#path').value;
if(!path.startsWith('/v1/')||path.startsWith('//'))throw Error('Use a /v1/ path');
let r=await fetch(path,{method,headers:{'Authorization':'Bearer '+document.querySelector('#token').value,'Content-Type':'application/json'},body:method==='POST'?document.querySelector('#body').value:undefined});
out.textContent=r.status+'\\n'+JSON.stringify(await r.json(),null,2)}catch(e){out.textContent=String(e)}};</script>"""


class Service(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, token=None, handler=None, *, max_requests=16, read_seconds=10, max_header_bytes=65536):
        if type(max_requests) is not int or max_requests<1 or type(max_header_bytes) is not int or max_header_bytes<1:
            raise ValueError("Positive request and header limits required")
        if type(read_seconds) not in (int,float) or not math.isfinite(read_seconds) or read_seconds<=0:
            raise ValueError("Positive finite read deadline required")
        self._request_slots=threading.BoundedSemaphore(max_requests)
        self.read_seconds,self.max_header_bytes=read_seconds,max_header_bytes
        self.token, self.handler = token or secrets.token_urlsafe(32), handler
        self.workbooks, self.registry_lock = {}, threading.RLock()
        self._path_locks, self._opening = {}, set()
        self._reserved_handles = 0
        self._stopping = False
        self.jobs = PreparationJobs()
        super().__init__(("127.0.0.1", port), RequestHandler)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            # No request was parsed, so do not attach a possibly unrelated ID or
            # assert a terminal operation outcome. Clients retain pending IDs.
            body=encoded({"code":"SERVICE_BUSY","message":"Service request capacity is full; reconcile pending operations before retrying", "outcome":"unknown"}).encode()
            try:
                request.settimeout(.1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: "+str(len(body)).encode()+b"\r\n\r\n"+body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request,client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request,client_address)
        finally:
            self._request_slots.release()

    def server_bind(self):
        # HTTPServer resolves a hostname here; a numeric loopback service has
        # no use for reverse DNS, which can block startup on macOS runners.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]

    def server_close(self):
        super().server_close()
        self.jobs.close()
        with self.registry_lock:
            self._stopping = True
            books = set(self.workbooks.values())
        for book in books:
            with self._path_access(book.path):
                with book.lock:
                    book.close()
                with self.registry_lock:
                    self.workbooks = {handle:engine for handle,engine in self.workbooks.items() if engine is not book}

    @contextmanager
    def _path_access(self, path):
        with self.registry_lock:
            entry = self._path_locks.setdefault(path,[threading.RLock(),0])
            entry[1] += 1
        try:
            # Never wait for a workbook while holding the shared registry.
            with entry[0]:
                yield
        finally:
            with self.registry_lock:
                entry[1] -= 1
                if not entry[1]:
                    self._path_locks.pop(path)

    def _open(self, body):
        require(isinstance(body.get("path"), str) and bool(body["path"]), "INVALID_PATH", "A server-local workbook path is required")
        create, readonly = boolean(body.get("create", False), "create"), boolean(body.get("readonly", False), "readonly")
        path = Path(body["path"]).expanduser().resolve()
        with self._path_access(path):
            with self.registry_lock:
                require(not self._stopping,"SERVICE_STOPPING","Service is stopping",503)
                require(len(self.workbooks)+self._reserved_handles<128,"HANDLE_LIMIT","Release unused workbook handles before opening more",409)
                book = next((book for book in self.workbooks.values() if book.path==path),None)
                fresh = book is None
                if fresh:
                    require(len(set(self.workbooks.values()))+len(self._opening)<8,"WORKBOOK_LIMIT","Close unused workbooks before opening more",409)
                    self._opening.add(path)
                else:
                    require(not create, "FILE_EXISTS", "Workbook already exists", 409)
                    require(book.readonly == readonly, "OPEN_MODE_CONFLICT", "Workbook is already open in a different mode", 409)
                self._reserved_handles += 1
                reserved = True
            try:
                if fresh:
                    book = Workbook(path, create=create, readonly=readonly, handler=self.handler)
                info = book.info()
                handle = str(uuid.uuid4())
                try:
                    result = {**info,"id":handle,"workbook_id":book.id}
                    # SQLite can store non-text metadata through direct access.
                    # Prepare a serializable response before publishing its lease.
                    encoded(result).encode()
                except (TypeError,ValueError) as error:
                    raise EngineError("INVALID_FORMAT","Workbook metadata cannot be represented by the REST interface",409) from error
                with self.registry_lock:
                    require(not self._stopping,"SERVICE_STOPPING","Service is stopping",503)
                    self.workbooks[handle] = book
                    self._reserved_handles -= 1
                    if fresh:
                        self._opening.discard(path)
                    reserved = False
                return result
            except BaseException:
                if fresh and book is not None:
                    book.close()
                raise
            finally:
                if reserved:
                    with self.registry_lock:
                        self._reserved_handles -= 1
                        if fresh:
                            self._opening.discard(path)

    def dispatch(self, method, path, body):
        if method == "GET" and path == "/v1/capabilities":
            return CAPABILITIES
        if method == "POST" and path == "/v1/open":
            return self._open(body)
        if method == "POST" and path == "/v1/recovery/inspect":
            require(isinstance(body.get("path"),str) and bool(body["path"]),"INVALID_PATH","Native file path is required")
            with self._path_access(Path(body["path"]).expanduser().resolve()):
                return inspect_native(body["path"])
        if method == "POST" and path == "/v1/import/csv/preview":
            return preview_csv(body)
        if method == "POST" and path == "/v1/import/csv/jobs":
            return self.jobs.start(body, preview_csv)
        if method == "POST" and path == "/v1/import/ods/jobs":
            return self.jobs.start(body,ods_preview,"ods")
        if method == "POST" and path == "/v1/import/xlsx/jobs":
            return self.jobs.start(body,xlsx_preview,"xlsx")
        parts = path.strip("/").split("/")
        if len(parts) in (3,4) and parts[:2] == ["v1","jobs"]:
            if method == "GET" and len(parts) == 3:
                return self.jobs.access(parts[2])
            if method == "POST" and len(parts) == 4 and parts[3] in ("cancel","release"):
                return self.jobs.access(parts[2],parts[3])
        require(len(parts) >= 3 and parts[:2] == ["v1", "workbooks"], "NOT_FOUND", "Unknown API resource", 404)
        with self.registry_lock:
            book = self.workbooks.get(parts[2])
        require(book is not None, "NOT_FOUND", "Workbook is not open in this service", 404)
        suffix = parts[3:]
        with self._path_access(book.path):
            with self.registry_lock:
                require(self.workbooks.get(parts[2]) is book,"NOT_FOUND","Workbook handle was released",404)
            return self._dispatch_book(method,parts[2],book,suffix,body)

    def _dispatch_book(self, method, handle, book, suffix, body):
        if method == "POST" and suffix == ["close"]:
            with self.registry_lock:
                last = sum(engine is book for engine in self.workbooks.values())==1
                if not last:
                    self.workbooks.pop(handle)
            if last:
                with book.lock:
                    book.close()
                with self.registry_lock:
                    self.workbooks.pop(handle)
            return {"closed":True}
        if method == "GET" and not suffix:
            return {**book.info(), "id": handle, "workbook_id": book.id}
        if method == "POST" and suffix == ["range"]:
            return book.read(body.get("sheet", "Sheet1"), body)
        if method == "POST" and suffix == ["clipboard"]:
            return book.read(body.get("sheet", "Sheet1"), {**body,"preview":False},limit=MAX_CELLS)
        if method == "POST" and suffix == ["list"]:
            return book.list_view(body.get("sheet", "Sheet1"), body)
        if method == "POST" and suffix == ["summary"]:
            return book.summary(body.get("sheet","Sheet1"),body)
        if method == "POST" and suffix == ["find"]:
            return book.find(body.get("sheet", "Sheet1"), body)
        if method == "POST" and suffix == ["operations"]:
            return book.mutate(body)
        if method == "GET" and len(suffix) == 2 and suffix[0] == "operations":
            return book.operation(unquote(suffix[1]))
        if method == "POST" and suffix == ["snapshot"]:
            require(isinstance(body.get("path"), str), "INVALID_PATH", "Snapshot path is required")
            try: return book.snapshot(body["path"])
            except (EngineError,OSError,sqlite3.Error) as error:
                if not isinstance(error,EngineError):
                    error=EngineError("SNAPSHOT_FAILED",f"Native copy failed: {error}. Retain the source and preserve any destination for inspection",409)
                error.body.update(destination=body["path"],copy_outcome="incomplete",destination_requires_inspection=True)
                raise error
        if method == "POST" and suffix in (["export", "xlsx"],["export","ods"]):
            require("rows" not in body,"INVALID_ARGUMENT","Spreadsheet export requires a contiguous rectangle")
            whole=preparation_scope(body)=="workbook"
            snapshot=book.read_workbook() if whole else book.read(body.get("sheet","Sheet1"),{**body,"preview":False},limit=MAX_CELLS)
            return (xlsx_export if suffix[1]=="xlsx" else ods_export)(snapshot,body)
        if method == "POST" and suffix == ["export", "csv"]:
            return book.csv_export(body.get("sheet", "Sheet1"), body)
        raise EngineError("NOT_FOUND", "Unknown API resource", 404)


class DeadlineSocketInput(io.RawIOBase):
    """A total read budget, including a peer that continually trickles bytes."""
    def __init__(self, connection, seconds):
        super().__init__()
        self.connection,self.deadline=connection,time.monotonic()+seconds

    def readable(self): return True

    def remaining(self):
        remaining=self.deadline-time.monotonic()
        if remaining<=0: raise TimeoutError("Request read deadline exceeded")
        return remaining

    def readinto(self, buffer):
        self.connection.settimeout(self.remaining())
        return self.connection.recv_into(buffer)


class RequestInput(io.BufferedReader):
    header_remaining=None

    def readline(self, size=-1):
        self.raw.remaining()
        if self.header_remaining is not None:
            size=min(size if size>=0 else self.header_remaining+1,self.header_remaining+1)
        line=super().readline(size)
        if self.header_remaining is not None:
            self.header_remaining-=len(line)
            if self.header_remaining<0: raise LineTooLong("aggregate headers")
        return line

    def read(self, size=-1):
        self.raw.remaining()
        return super().read(size)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "DataHorizon/0.1"

    def setup(self):
        super().setup()
        self.rfile.close()
        self.rfile=RequestInput(DeadlineSocketInput(self.connection,self.server.read_seconds),buffer_size=8192)

    def parse_request(self):
        # The stdlib separately bounds the request line. Count headers consumed
        # by its parser, preserving any prefetched body in the same buffer.
        self.rfile.header_remaining=self.server.max_header_bytes
        try:
            return super().parse_request()
        finally:
            self.rfile.header_remaining=None

    def log_message(self, *_):
        pass  # Avoid logging credentials, inputs, or local file paths.

    def respond(self, status, result, content_type="application/json"):
        data = encoded(result).encode() if content_type == "application/json" else result.encode()
        try:
            self.connection.settimeout(5)
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # The durable receipt remains available after a lost response.

    def run_request(self):
        try:
            # Reject browser cross-origin writes and DNS-rebinding Host headers.
            host = self.headers.get("Host", "").split(":")[0]
            require(host in ("127.0.0.1", "localhost"), "INVALID_HOST", "Only loopback Host headers are accepted", 403)
            origin = self.headers.get("Origin")
            require(origin is None or origin in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"),
                    "INVALID_ORIGIN", "Cross-origin access is disabled", 403)
            path = urlsplit(self.path).path
            if self.command == "GET" and path in ("/docs", "/openapi.json"):
                return self.respond(200, EXPLORER if path == "/docs" else openapi(), "text/html" if path == "/docs" else "application/json")
            require(hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.server.token),
                    "UNAUTHORIZED", "A valid local bearer token is required", 401)
            body = {}
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                require(0 < length <= 4 * 1024 * 1024, "REQUEST_LIMIT", "JSON body must be 1 byte to 4 MiB", 413)
                try:
                    raw = self.rfile.read(length)
                except TimeoutError as error:
                    raise EngineError("REQUEST_TIMEOUT","Request read deadline exceeded",408) from error
                require(len(raw) == length, "INVALID_JSON", "Incomplete request body")
                body = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
                require(isinstance(body, dict), "INVALID_JSON", "Request body must be an object")
            result = self.server.dispatch(self.command, path, body)
            self.respond(200, result)
        except EngineError as error:
            self.respond(error.status, error.body)
        except (ValueError, TypeError, KeyError) as error:
            self.respond(400, {"code": "INVALID_ARGUMENT", "message": str(error), "outcome": "rejected"})
        except (OSError, sqlite3.Error) as error:
            self.respond(409, {"code": "STORAGE_ERROR", "message": str(error), "outcome": "rejected"})
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.respond(500, {"code": "INTERNAL_ERROR", "message": "Inspect service diagnostics and reconcile the operation ID", "outcome": "unknown"})

    do_GET = run_request
    do_POST = run_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--handler", type=Path, help="Explicitly activate a trusted service-side on_change Python file")
    args = parser.parse_args()
    server = Service(args.port, handler=args.handler.resolve() if args.handler else None)
    # Only the launching parent receives this credential through its private pipe.
    print(encoded({"url": f"http://127.0.0.1:{server.server_port}", "token": server.token, "api_version": 1}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
