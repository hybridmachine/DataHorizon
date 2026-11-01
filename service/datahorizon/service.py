"""Authenticated loopback-only development REST service; no third-party runtime."""

import argparse
import csv
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
import traceback
from urllib.parse import unquote, urlsplit
import uuid

from .engine import EngineError, MAX_CELLS, Workbook, encoded, require

CAPABILITIES = {
    "api_version": 1, "format_version": 1, "max_operation_cells": MAX_CELLS,
    "max_viewport_cells": 4096, "max_request_bytes": 4 * 1024 * 1024,
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
    "/v1/workbooks/{workbook}/list": ("post", "Explicit full-scope sorted/filtered list projection"),
    "/v1/workbooks/{workbook}/find": ("post", "Find in a declared source or display scope"),
    "/v1/workbooks/{workbook}/operations": ("post", "Atomic managed operation"),
    "/v1/workbooks/{workbook}/operations/{operation}": ("get", "Durable outcome lookup"),
    "/v1/workbooks/{workbook}/snapshot": ("post", "Exclusive consistent native copy"),
    "/v1/workbooks/{workbook}/close": ("post", "Release this client handle; other client handles remain valid"),
    "/v1/workbooks/{workbook}/export/csv": ("post", "Consistent values-only CSV export"),
}


def openapi():
    scalar = {"type": "integer", "minimum": 1}
    schemas = {
        "Error": {"type":"object", "required":["code","message","outcome"], "properties":{
            "code":{"type":"string"},"message":{"type":"string"},
            "outcome":{"type":"string","enum":["rejected","rolled_back","unknown"]},"operation_id":{"type":"string"}}},
        "OpenRequest": {"type":"object","required":["path"],"properties":{
            "path":{"type":"string","description":"Absolute path on the localhost service host"},
            "create":{"type":"boolean","default":False},"readonly":{"type":"boolean","default":False}},
            "example":{"path":"/tmp/example.dhwb","create":True}},
        "RangeRequest": {"type":"object","properties":{
            "sheet":{"type":"string","default":"Sheet1"},"r1":scalar,"c1":scalar,"r2":scalar,"c2":scalar,
            "rows":{"type":"array","maxItems":256,"items":scalar,"description":"Optional explicit source rows for projections"}},
            "example":{"sheet":"Sheet1","r1":1,"c1":1,"r2":32,"c2":16}},
        "CellEdit": {"type":"object","required":["row","col"],"properties":{
            "row":scalar,"col":scalar,"source":{"type":"string","maxLength":65536},
            "kind":{"type":"string","enum":["auto","formula","text","number","boolean","null"]},
            "style":{"type":"object","description":"Supported preview style properties; see compatibility-v0.md"},
            "clear":{"type":"boolean"}}},
        "OperationRequest": {"type":"object","required":["operation_id","expected_state","action"],"properties":{
            "operation_id":{"type":"string","description":"Generate and preserve before sending; reconcile lost responses by this ID"},
            "expected_state":{"type":"string","description":"State from the reviewed workbook; stale writes roll back"},
            "sheet":{"type":"string"},"action":{"type":"string","enum":CAPABILITIES["operations"]},
            "cells":{"type":"array","maxItems":MAX_CELLS,"items":{"$ref":"#/components/schemas/CellEdit"}},
            "label":{"type":"string"},"r1":scalar,"c1":scalar,"r2":scalar,"c2":scalar},
            "description":"Atomic action-specific arguments are documented in development.md and compatibility-v0.md. No managed SQL or chunked commits.",
            "example":{"operation_id":"generate-a-uuid","expected_state":"copy-from-workbook-info","action":"edit","sheet":"Sheet1","cells":[{"row":1,"col":1,"source":"=SUM(7,13.5)"}]}},
        "Receipt": {"type":"object","required":["operation_id","outcome","state","changed_cells"],"properties":{
            "operation_id":{"type":"string"},"outcome":{"type":"string","enum":["committed"]},"state":{"type":"string"},"changed_cells":{"type":"integer"}}},
    }
    paths = {}
    for path, (method, summary) in ROUTES.items():
        parameters = [{"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
                      for name in ("workbook", "operation") if "{" + name + "}" in path]
        operation = {"summary": summary, "parameters": parameters,
                     "responses": {"200": {"description": "JSON result"}, "400": {"description": "Structured error"},
                                   "409": {"description": "Conflict or rolled-back operation"}},
                     "security": [{"localToken": []}]}
        if method == "post":
            name = "OpenRequest" if path == "/v1/open" else "RangeRequest" if path.endswith("/range") else "OperationRequest" if path.endswith("/operations") else None
            schema = {"$ref":"#/components/schemas/"+name} if name else {"type":"object"}
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": schema}}}
        for status in ("400","409"):
            operation["responses"][status]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/Error"}}}
        if path.endswith("/operations"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/Receipt"}}}
        elif path.endswith("/{operation}"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"oneOf":[{"$ref":"#/components/schemas/Receipt"},{"$ref":"#/components/schemas/Error"}]}}}
        paths[path] = {method: operation}
    return {"openapi": "3.0.3", "info": {"title": "DataHorizon local development API", "version": "0.1.0"},
            "paths": paths, "components": {"schemas":schemas,"securitySchemes": {"localToken": {"type": "http", "scheme": "bearer"}}}}


EXPLORER = """<!doctype html><meta charset="utf-8"><title>DataHorizon API explorer</title>
<style>body{font:16px system-ui;max-width:900px;margin:40px auto;padding:0 20px}input,select,textarea,button{font:inherit;margin:6px 0;padding:8px}textarea,input{box-sizing:border-box;width:100%}pre{white-space:pre-wrap;background:#eee;padding:16px}label{display:block}</style>
<h1>DataHorizon API explorer</h1><p>Loopback development service. <a href="/openapi.json">OpenAPI schema</a>.</p>
<label>Resource<select id="endpoint"></select></label>
<label>Local bearer token<input id="token" type="password" autocomplete="off"></label>
<label>Method<select id="method"><option>GET</option><option>POST</option></select></label>
<label>Path<input id="path" value="/v1/capabilities"></label>
<label>JSON body<textarea id="body" rows="8">{}</textarea></label><button id="run">Send request</button><pre id="result">Ready</pre>
<script>fetch('/openapi.json').then(r=>r.json()).then(api=>{let select=document.querySelector('#endpoint');
for(let [path,methods] of Object.entries(api.paths))for(let [method,op] of Object.entries(methods)){
let option=document.createElement('option');option.textContent=method.toUpperCase()+' '+path+' — '+op.summary;option.value=JSON.stringify([path,method,op]);select.append(option)}
select.onchange=()=>{let[path,method,op]=JSON.parse(select.value);document.querySelector('#path').value=path;document.querySelector('#method').value=method.toUpperCase();
let ref=op.requestBody?.content?.['application/json']?.schema?.['$ref'];document.querySelector('#body').value=JSON.stringify(ref?api.components.schemas[ref.split('/').pop()].example||{}:{},null,2)};select.onchange()});
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
            path = Path(body["path"]).expanduser().resolve()
            with self.registry_lock:
                require(len(self.workbooks)<128,"HANDLE_LIMIT","Release unused workbook handles before opening more",409)
                for book in list(self.workbooks.values()):
                    if book.path == path:
                        require(not body.get("create"), "FILE_EXISTS", "Workbook already exists", 409)
                        require(book.readonly == bool(body.get("readonly")), "OPEN_MODE_CONFLICT", "Workbook is already open in a different mode", 409)
                        handle=str(uuid.uuid4()); self.workbooks[handle]=book
                        return {**book.info(), "id": handle, "workbook_id": book.id}
                require(len(set(self.workbooks.values()))<8,"WORKBOOK_LIMIT","Close unused workbooks before opening more",409)
                book = Workbook(path, create=bool(body.get("create")), readonly=bool(body.get("readonly")), handler=self.handler)
                handle = str(uuid.uuid4())
                self.workbooks[handle] = book
                return {**book.info(), "id": handle, "workbook_id": book.id}
        if method == "POST" and path == "/v1/import/csv/preview":
            text = body.get("csv")
            delimiter = body.get("delimiter", ",")
            require(isinstance(text, str) and isinstance(delimiter, str) and len(delimiter) == 1,
                    "INVALID_CSV", "Provide CSV text and a one-character delimiter")
            rows, count = [], 0
            try:
                for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True):
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
