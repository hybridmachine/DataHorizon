"""Authenticated loopback-only development REST service; no third-party runtime."""

import argparse
import csv
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
from socketserver import TCPServer
import sqlite3
import sys
import threading
import traceback
from urllib.parse import unquote, urlsplit
import uuid

from .engine import EngineError, MAX_CELLS, Workbook, boolean, encoded, require
from .formula import MAX_COLS, MAX_ROWS

CAPABILITIES = {
    "api_version": 1, "format_version": 1, "max_operation_cells": MAX_CELLS,
    "max_viewport_cells": 4096, "max_clipboard_cells":MAX_CELLS, "max_request_bytes": 4 * 1024 * 1024,
    "formulas": ["arithmetic", "references", "SUM", "IF"],
    "operations": ["edit", "paste", "move", "fill", "replace_all", "undo", "redo", "sheet_add", "sheet_rename", "sheet_duplicate", "sheet_reorder", "sheet_delete"],
    "interchange": ["csv"], "managed_sql": False, "remote_binding": False,
    "follow_up_work": False, "chunked_jobs": False,
    "max_open_workbooks":8, "max_open_handles":128,
}

ROUTES = {
    "/v1/capabilities": ("get", "Preview capabilities"),
    "/v1/open": ("post", "Create or open a server-local native workbook"),
    "/v1/import/csv/preview": ("post", "Parse CSV as text for explicit import review"),
    "/v1/workbooks/{workbook}": ("get", "Workbook state and sheets"),
    "/v1/workbooks/{workbook}/range": ("post", "Bounded consistent viewport read"),
    "/v1/workbooks/{workbook}/clipboard": ("post", "Consistent full-source clipboard read, at most 10,000 cells and 4 MiB"),
    "/v1/workbooks/{workbook}/list": ("post", "Explicit full-scope sorted/filtered list projection"),
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
            "format":{"type":"string","enum":["general","text","fixed2"]},
            "bold":{"type":"boolean"},"italic":{"type":"boolean"},
            "align":{"type":"string","enum":["left","center","right"]},
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
            "operation_id":{"type":"string"},"outcome":{"type":"string","enum":["committed"]},"state":{"type":"string"},"changed_cells":{"type":"integer"}}},
    }
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
        "fill": ([],{"sheet":sheet,**rectangle,"mode":{"type":"string","enum":["copy","series"],"default":"copy"},"step":{"type":"number","default":1}},
                 {"r1":1,"c1":1,"r2":2,"c2":1,"mode":"series","step":2}),
        "move": (["row","col","cut_state"],{"sheet":sheet,**rectangle,"row":row,"col":col,"cut_state":{"type":"string","description":"State captured by Cut; must still match"},"mode":{"type":"string","enum":["all"],"default":"all"}},
                 {"r1":1,"c1":1,"r2":1,"c2":1,"row":3,"col":1,"cut_state":"copy-from-workbook-info"}),
        "replace_all": (["needle","replacement"],{"sheet":sheet,**rectangle,**search,"replacement":{"type":"string"}},
                        {"r1":1,"c1":1,"r2":3,"c2":1,"needle":"7","replacement":"9"}),
        "undo": ([],{},{}), "redo": ([],{},{}),
        "sheet_add": ([],{"name":{**name,"default":"Sheet"}},{"name":"Other"}),
        "sheet_rename": (["sheet","name"],{"sheet":sheet,"name":name},{"sheet":"Other","name":"Renamed"}),
        "sheet_duplicate": (["sheet","name"],{"sheet":sheet,"name":name},{"sheet":"Renamed","name":"Copy"}),
        "sheet_reorder": (["order"],{"order":{"type":"array","minItems":1,"uniqueItems":True,"items":{"type":"string"},"description":"Every current sheet ID exactly once, in desired display order"}},{"order":["replace-with-all-current-sheet-ids"]}),
        "sheet_delete": (["sheet"],{"sheet":sheet},{"sheet":"Copy"}),
    }
    mapping = {}
    for action, (required, properties, example) in actions.items():
        schema_name = "".join(word.title() for word in action.split("_"))+"Operation"
        mapping[action] = "#/components/schemas/"+schema_name
        schemas[schema_name] = {"type":"object","required":["operation_id","expected_state","action",*required],
            "properties":{**common,"action":{"type":"string","enum":[action]},**properties},
            "example":{"operation_id":"generate-a-uuid","expected_state":"copy-from-workbook-info","action":action,**example}}
    schemas["OperationRequest"] = {"oneOf":[{"$ref":ref} for ref in mapping.values()],
        "discriminator":{"propertyName":"action","mapping":mapping},
        "description":"Atomic operations: at most 10,000 affected cells and a 4 MiB request. Range endpoints are inclusive and must be ordered. Copy fill uses the top-left input; series requires a numeric seed. Move requires no formula sources in the workbook. Sheet rename/duplicate/delete reject cross-sheet references; duplicate/delete also reject activated required hooks. Structure changes clear history. No managed SQL or chunked commits.",
        "example":schemas["EditOperation"]["example"]}
    schemas.update({
        "CsvPreviewRequest":{"type":"object","required":["csv"],"properties":{"csv":{"type":"string"},"delimiter":{"type":"string","minLength":1,"maxLength":1,"default":","}},"example":{"csv":"ID,Value\n00123,7"}},
        "SnapshotRequest":{"type":"object","required":["path"],"properties":{"path":{"type":"string","description":"New server-local destination; never overwrite an existing file"}},"example":{"path":"/tmp/copy.dhwb"}},
        "CloseRequest":{"type":"object","example":{}},
        "FindRequest":{"type":"object","required":["needle"],"properties":{"sheet":sheet,**rectangle,**search},"example":{"sheet":"Sheet1","r2":10,"c2":4,"needle":"Pen"}},
        "CsvExportRequest":{"type":"object","properties":{"sheet":sheet,**rectangle},"example":{"sheet":"Sheet1","r2":10,"c2":4}},
        "ListRequest":{"type":"object","properties":{"sheet":sheet,**rectangle,"header":{"type":"boolean","default":False},
            "sort":{"type":"array","maxItems":8,"items":{"type":"object","required":["col"],"properties":{"col":col,"descending":{"type":"boolean","default":False}}}},
            "filters":{"type":"array","maxItems":8,"items":{"type":"object","required":["col","op"],"properties":{"col":col,"op":{"type":"string","enum":["=","<>",">","<",">=","<=","contains"]},"value":scalar}}}},
            "example":{"sheet":"Sheet1","r2":10,"c2":4,"header":True,"sort":[{"col":2,"descending":True}]}}
    })
    paths = {}
    for path, (method, summary) in ROUTES.items():
        parameters = [{"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
                      for name in ("workbook", "operation") if "{" + name + "}" in path]
        operation = {"summary": summary, "parameters": parameters,
                     "responses": {"200": {"description": "JSON result"}, **{str(status):{"description":"Structured error; inspect outcome and operation_id before deciding whether a mutation finished"} for status in (400,401,403,404,409,413,500)}},
                     "security": [{"localToken": []}]}
        if method == "post":
            name = "OpenRequest" if path == "/v1/open" else "CsvPreviewRequest" if path == "/v1/import/csv/preview" else "RangeRequest" if path.endswith("/range") else "ClipboardRequest" if path.endswith("/clipboard") else "OperationRequest" if path.endswith("/operations") else {
                "list":"ListRequest","find":"FindRequest","snapshot":"SnapshotRequest","close":"CloseRequest","csv":"CsvExportRequest"}[path.rsplit("/",1)[-1]]
            schema = {"$ref":"#/components/schemas/"+name}
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": schema}}}
        for status in ("400","401","403","404","409","413","500"):
            operation["responses"][status]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/Error"}}}
        if path.endswith("/operations"):
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

    def __init__(self, port=0, token=None, handler=None):
        self.token, self.handler = token or secrets.token_urlsafe(32), handler
        self.workbooks, self.registry_lock = {}, threading.RLock()
        super().__init__(("127.0.0.1", port), RequestHandler)

    def server_bind(self):
        # HTTPServer resolves a hostname here; a numeric loopback service has
        # no use for reverse DNS, which can block startup on macOS runners.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]

    def server_close(self):
        super().server_close()
        for book in set(self.workbooks.values()):
            with book.lock:
                book.close()

    def dispatch(self, method, path, body):
        if method == "GET" and path == "/v1/capabilities":
            return CAPABILITIES
        if method == "POST" and path == "/v1/open":
            require(isinstance(body.get("path"), str) and bool(body["path"]), "INVALID_PATH", "A server-local workbook path is required")
            create, readonly = boolean(body.get("create", False), "create"), boolean(body.get("readonly", False), "readonly")
            path = Path(body["path"]).expanduser().resolve()
            with self.registry_lock:
                require(len(self.workbooks)<128,"HANDLE_LIMIT","Release unused workbook handles before opening more",409)
                for book in list(self.workbooks.values()):
                    if book.path == path:
                        require(not create, "FILE_EXISTS", "Workbook already exists", 409)
                        require(book.readonly == readonly, "OPEN_MODE_CONFLICT", "Workbook is already open in a different mode", 409)
                        info=book.info()
                        handle=str(uuid.uuid4()); self.workbooks[handle]=book
                        return {**info, "id": handle, "workbook_id": book.id}
                require(len(set(self.workbooks.values()))<8,"WORKBOOK_LIMIT","Close unused workbooks before opening more",409)
                book = Workbook(path, create=create, readonly=readonly, handler=self.handler)
                try:
                    info=book.info()
                except BaseException:
                    book.close()
                    raise
                handle = str(uuid.uuid4())
                self.workbooks[handle] = book
                return {**info, "id": handle, "workbook_id": book.id}
        if method == "POST" and path == "/v1/import/csv/preview":
            text = body.get("csv")
            delimiter = body.get("delimiter", ",")
            require(isinstance(text, str) and isinstance(delimiter, str) and len(delimiter) == 1,
                    "INVALID_CSV", "Provide CSV text and a one-character delimiter")
            rows, count = [], 0
            try:
                for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True):
                    require(len(rows) < MAX_CELLS, "LIMIT", "CSV preview/import exceeds 10,000 records")
                    count += len(row)
                    require(count <= MAX_CELLS, "LIMIT", "CSV preview/import exceeds 10,000 cells")
                    rows.append(row)
            except csv.Error as error:
                raise EngineError("INVALID_CSV", str(error)) from error
            return {"rows": rows, "cells": count, "default_kind": "text",
                    "report": ["All columns remain text, including leading zeros and formula-like text. Choose types explicitly before applying."]}
        parts = path.strip("/").split("/")
        require(len(parts) >= 3 and parts[:2] == ["v1", "workbooks"], "NOT_FOUND", "Unknown API resource", 404)
        with self.registry_lock:
            book = self.workbooks.get(parts[2])
        require(book is not None, "NOT_FOUND", "Workbook is not open in this service", 404)
        suffix = parts[3:]
        if method == "POST" and suffix == ["close"]:
            with self.registry_lock:
                self.workbooks.pop(parts[2],None)
                if book not in self.workbooks.values():
                    with book.lock:
                        book.close()
            return {"closed":True}
        if method == "GET" and not suffix:
            return {**book.info(), "id": parts[2], "workbook_id": book.id}
        if method == "POST" and suffix == ["range"]:
            return book.read(body.get("sheet", "Sheet1"), body)
        if method == "POST" and suffix == ["clipboard"]:
            return book.read(body.get("sheet", "Sheet1"), {**body,"preview":False},limit=MAX_CELLS)
        if method == "POST" and suffix == ["list"]:
            return book.list_view(body.get("sheet", "Sheet1"), body)
        if method == "POST" and suffix == ["find"]:
            return book.find(body.get("sheet", "Sheet1"), body)
        if method == "POST" and suffix == ["operations"]:
            return book.mutate(body)
        if method == "GET" and len(suffix) == 2 and suffix[0] == "operations":
            return book.operation(unquote(suffix[1]))
        if method == "POST" and suffix == ["snapshot"]:
            require(isinstance(body.get("path"), str), "INVALID_PATH", "Snapshot path is required")
            return book.snapshot(body["path"])
        if method == "POST" and suffix == ["export", "csv"]:
            return book.csv_export(body.get("sheet", "Sheet1"), body)
        raise EngineError("NOT_FOUND", "Unknown API resource", 404)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "DataHorizon/0.1"

    def log_message(self, *_):
        pass  # Avoid logging credentials, inputs, or local file paths.

    def respond(self, status, result, content_type="application/json"):
        data = encoded(result).encode() if content_type == "application/json" else result.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
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
                self.connection.settimeout(10)
                raw = self.rfile.read(length)
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
