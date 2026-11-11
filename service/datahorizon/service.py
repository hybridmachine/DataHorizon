# SPDX-License-Identifier: GPL-3.0-only
"""Authenticated loopback-only development REST service; no third-party runtime."""

import argparse
from contextlib import contextmanager
import hashlib
import hmac
from http.client import LineTooLong
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import secrets
import socket
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
from .list_export import export_list
from .functions import function_catalog
from .staging import IDENTIFIER, Staging
from .xlsx_stream import LARGE_CELLS, LARGE_PACKAGE

CAPABILITIES = {
    "api_version": 1, "format_version": 7, "supported_format_versions": [1,2,3,4,5,6,7], "new_file_format_version": 1, "max_operation_cells": MAX_CELLS,
    "workbook_modules":True,"atomic_macros":True,"macro_worker_seconds":2,"macro_activation":"operator-approved workbook UUID:code SHA256; set at service start-up, never over HTTP",
    "structured_tables":True,"table_types":["text","integer","number","boolean","date","time","datetime"],
    "readonly_sql":True,"query_jobs":True,"query_result_rows":100000,"query_result_values":1000000,"query_spool_bytes":64*1024*1024,
    "temporal_types": ["date","time","datetime"], "calculation_timezone": "UTC",
    "max_style_cells":MAX_CELLS, "max_summary_cells":MAX_CELLS, "max_viewport_cells": 4096, "max_clipboard_cells":MAX_CELLS, "max_request_bytes": 4 * 1024 * 1024,
    "formulas": ["arithmetic", "references", "concatenation", *sorted(SUPPORTED_FUNCTIONS)],
    "operations": ["list_edit","list_declare","list_reconcile","list_forget","metadata_set","metadata_delete","workbook_import", "edit", "paste", "move", "fill", "replace_all", "undo", "redo", "sheet_add", "sheet_rename", "sheet_duplicate", "sheet_reorder", "sheet_delete", "rows_insert", "rows_delete", "columns_insert", "columns_delete"],
    "interchange": ["csv", "xlsx", "ods"], "managed_sql": False, "remote_binding": False,
    "follow_up_work": False, "chunked_jobs": True, "preparation_jobs": True, "background_calculation":True, "structural_events":True,
    "max_preparation_jobs":4, "preparation_job_seconds":10, "preparation_retention_seconds":300,
    "max_open_workbooks":8, "max_open_handles":128,
    "max_concurrent_requests":16, "max_header_bytes":65536, "request_read_timeout_seconds":10,
}

CAPABILITIES['operations'].extend(['table_create','table_drop','table_promote','table_schema','table_insert','table_update','table_delete','index_create','index_drop'])
CAPABILITIES['operations'].extend(['relationship_create','relationship_drop','table_repair'])
CAPABILITIES['operations'].extend(['query_save','query_rename','query_delete'])
CAPABILITIES['operations'].extend(['module_save','module_delete','macro_run'])
CAPABILITIES['operations'].extend(['name_save','name_delete'])
CAPABILITIES.update(names=True,max_names=256)
CAPABILITIES['operations'].extend(['binding_save','binding_delete'])
CAPABILITIES.update(event_bindings=True,max_bindings=16,binding_events=['cell_change','table_change'])
MAX_UPLOADS, UPLOAD_SECONDS, UPLOAD_CHUNK = 2, 300, 64*1024
CAPABILITIES.update(uploads=True,max_upload_bytes=LARGE_PACKAGE,max_import_cells=LARGE_CELLS,max_concurrent_uploads=MAX_UPLOADS)

ROUTES = {
    "/v1/workbooks/{workbook}/code": ("post", "Inspect inert workbook modules and the exact service-local activation key"),
    "/v1/workbooks/{workbook}/names": ("post", "Read the state-checked workbook name catalog"),
    "/v1/workbooks/{workbook}/tables":("post","Read the state-checked structured table catalog"),
    "/v1/workbooks/{workbook}/tables/preview":("post","Review selection values and types before independent table creation"),
    "/v1/workbooks/{workbook}/tables/read":("post","Read a bounded state-checked record page"),
    "/v1/workbooks/{workbook}/relationships/inspect":("post","Inspect native relationships and bounded orphan locations"),
    "/v1/workbooks/{workbook}/relationships/repair-preview":("post","Review narrowly scoped orphan repairs on an explicit repair handle"),
    "/v1/workbooks/{workbook}/queries/jobs":("post","Execute read-only SQL on a private snapshot with a bounded disk result spool"),
    "/v1/workbooks/{workbook}/queries/jobs/{job}":("get","Query execution status; only complete current results are published"),
    "/v1/workbooks/{workbook}/queries/jobs/{job}/page":("post","Read a bounded projected page of a completed frozen query"),
    "/v1/workbooks/{workbook}/queries/jobs/{job}/cancel":("post","Cancel execution or discard its completed result"),
    "/v1/workbooks/{workbook}/queries/jobs/{job}/release":("post","Release terminal query snapshot/result storage"),
    "/v1/workbooks/{workbook}/queries/preview":("post","Review authorization, dependencies and shape without evaluating data rows"),
    "/v1/workbooks/{workbook}/queries/read":("post","Read a saved definition without executing it"),
    **{f"/v1/workbooks/{{workbook}}/tables/export/{format}":('post','Export a complete bounded table/filter values snapshot') for format in ('csv','xlsx','ods')},
    **{f"/v1/workbooks/{{workbook}}/queries/jobs/{{job}}/export/{format}":('post','Export the complete completed query result within interchange limits') for format in ('csv','xlsx','ods')},
    "/v1/workbooks/{workbook}/tables/schema-preview":("post","Review atomic schema conversion, constraints and explicit column loss"),
    "/v1/workbooks/{workbook}/tables/edit-preview":("post","Convert reviewed record inputs without writing or running change hooks"),
    "/v1/workbooks/{workbook}/operation-jobs":("post","Stage a reviewed edit/CSV or frozen find/list job; never starts writes"),
    "/v1/workbooks/{workbook}/operation-jobs/{job}":("get","Inspect durable job status and batch receipts"),
    "/v1/workbooks/{workbook}/operation-jobs/{job}/append":("post","Append up to 1,000 explicitly typed edits"),
    "/v1/workbooks/{workbook}/operation-jobs/{job}/seal":("post","Seal a staged plan for review"),
    "/v1/workbooks/{workbook}/operation-jobs/{job}/status":("post","Page frozen results"),
    "/v1/workbooks/{workbook}/operation-jobs/{job}/start":("post","Execute the reviewed plan"),
    "/v1/workbooks/{workbook}/operation-jobs/{job}/resume":("post","Explicitly continue remaining known work after receipt reconciliation"),
    "/v1/workbooks/{workbook}/operation-jobs/{job}/cancel":("post","Cooperatively cancel; preserve committed receipts"),
    "/v1/workbooks/{workbook}/operation-jobs/{job}/release":("post","Delete terminal sidecar staging; preserve native receipts"),

    "/v1/workbooks/{workbook}/calculation/jobs":("post","Start a bounded read-only background calculation"),
    "/v1/workbooks/{workbook}/calculation/jobs/{job}":("get","Read pending/current/error/stale calculation state"),
    "/v1/workbooks/{workbook}/calculation/jobs/{job}/cancel":("post","Cancel or discard a calculation snapshot"),
    "/v1/workbooks/{workbook}/calculation/jobs/{job}/release":("post","Release a terminal calculation job"),
    "/v1/workbooks/{workbook}/metadata/read":("post","Read portable per-sheet presentation and list metadata"),
    "/v1/workbooks/{workbook}/records/read":("post","Review persistent declared-list record identities and verification state"),
    "/v1/workbooks/{workbook}/records/copy":("post","Read reviewed visible records as typed formula/value/style clipboard rows"),
    "/v1/workbooks/{workbook}/records/export/xlsx":("post","Export a formula-preserving declared-list projection"),
    "/v1/workbooks/{workbook}/records/export/ods":("post","Export a formula-preserving declared-list projection"),
    "/v1/convert/value":("post","Review explicit locale/ISO value conversion without mutation"),
    "/v1/functions": ("get", "Implemented formula signatures, rules and worked examples; no workbook required"),
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
    "/v1/workbooks/{workbook}/styles": ("post", "State-checked style-only rectangle without inputs, evaluation or hooks"),
    "/v1/workbooks/{workbook}/range": ("post", "Bounded consistent viewport read"),
    "/v1/workbooks/{workbook}/clipboard": ("post", "Consistent full-source clipboard read, at most 10,000 cells and 4 MiB"),
    "/v1/workbooks/{workbook}/list/export/csv": ("post", "Export visible list values from one snapshot as CSV"),
    "/v1/workbooks/{workbook}/list/export/xlsx": ("post", "Export visible list values from one snapshot as XLSX"),
    "/v1/workbooks/{workbook}/list/export/ods": ("post", "Export visible list values from one snapshot as ODS"),
    "/v1/workbooks/{workbook}/list/values": ("post", "State-checked list values in displayed order from one snapshot"),
    "/v1/workbooks/{workbook}/list": ("post", "Explicit full-scope sorted/filtered list projection"),
    "/v1/workbooks/{workbook}/navigate": ("post", "Bounded stored-input data-region boundary navigation"),
    "/v1/workbooks/{workbook}/summary": ("post", "Count and numeric summary of one bounded stored-data snapshot"),
    "/v1/workbooks/{workbook}/trace": ("post", "Trace bounded direct syntactic references from stored inputs without evaluation"),
    "/v1/workbooks/{workbook}/find": ("post", "Find in a declared source or display scope"),
    "/v1/workbooks/{workbook}/operations": ("post", "Atomic managed operation"),
    "/v1/workbooks/{workbook}/operations/{operation}": ("get", "Durable outcome lookup"),
    "/v1/workbooks/{workbook}/operations/{operation}/cancel": ("post", "Request cooperative cancellation; inspect the final receipt"),
    "/v1/workbooks/{workbook}/snapshot": ("post", "Exclusive consistent native copy"),
    "/v1/workbooks/{workbook}/close": ("post", "Release this client handle; other client handles remain valid"),
    "/v1/workbooks/{workbook}/export/csv": ("post", "Consistent values-only CSV export"),
    "/v1/uploads/{upload}": ("put", "Stream a raw spreadsheet package of 1 byte to 64 MiB into private staging; never parsed by this route"),
    "/v1/uploads/{upload}/release": ("post", "Delete a completed staged upload"),
}


def openapi():
    row = {"type":"integer","minimum":1,"maximum":MAX_ROWS}
    col = {"type":"integer","minimum":1,"maximum":MAX_COLS}
    rectangle = {"r1":{**row,"default":1},"c1":{**col,"default":1},
                 "r2":{**row,"description":"Inclusive final row; defaults to r1"},
                 "c2":{**col,"description":"Inclusive final column; defaults to c1"}}
    sheet = {"type":"string","minLength":1,"description":"Sheet name or stable sheet ID","default":"Sheet1"}
    kinds = ["auto","formula","text","number","boolean","null","date","time","datetime"]
    scalar = {"oneOf":[{"type":"string","nullable":True},{"type":"number"},{"type":"boolean"}]}
    name = {"type":"string","minLength":1,"maxLength":31,"pattern":r"^[^\[\]:*?/\\!\x00-\x1f]+$"}
    schemas = {
        "Error": {"type":"object", "required":["code","message","outcome"], "properties":{
            "code":{"type":"string"},"message":{"type":"string"},
            "outcome":{"type":"string","enum":["rejected","rolled_back","unknown"]},"operation_id":{"type":"string"}}},
        "OpenRequest": {"type":"object","required":["path"],"properties":{
            "path":{"type":"string","description":"Absolute path on the localhost service host"},
            "create":{"type":"boolean","default":False},"readonly":{"type":"boolean","default":False},"repair":{"type":"boolean","default":False,"description":"Restricted relationship-orphan inspection/repair handle; ordinary mutations are rejected"}},
            "example":{"path":"/tmp/example.dhwb","create":True}},
        "RangeRequest": {"type":"object","properties":{
            "calculation":{"type":"string","enum":["fresh","cached"],"default":"fresh","description":"Cached mode returns pending for formulas without verified nonvolatile results."},
            "preview":{"type":"boolean","default":False,"description":"Bounded display/input previews; omitted input/value fields are explicitly flagged and cannot be pasted as full data"},
            "sheet":sheet,**rectangle,
            "rows":{"type":"array","minItems":1,"maxItems":256,"items":row,"description":"Optional explicit source rows for projections; replaces r1/r2"}},
            "description":"At most 4,096 viewport cells and a 4 MiB response. Without coordinates, reads default to A1.",
            "example":{"sheet":"Sheet1","r1":1,"c1":1,"r2":32,"c2":16}},
        "Style":{"type":"object","additionalProperties":False,"properties":{
            "format":{"type":"string","enum":["general","text","fixed2","percent2","number","percent","currency","date_iso","date_mdy","date_dmy","time24","datetime_iso"]},
            "font_family":{"type":"string","minLength":1,"maxLength":64,"description":"Single portable family without control characters, quotes, comma or backslash; unavailable fonts may render with a Qt fallback."},
            "font_size":{"type":"integer","minimum":6,"maximum":72},
            "text_color":{"type":"string","pattern":"^#[0-9a-fA-F]{6}$"},
            "currency":{"type":"string","pattern":"^[A-Z]{3}$","default":"USD","description":"Explicit label for currency display, not exchange conversion or currency validation."},
            "grouping":{"type":"boolean","description":"Invariant comma thousands grouping for numeric categories; dot decimal separator. A grouping-only patch preserves percentages and precision."},
            "decimals":{"type":"integer","minimum":0,"maximum":15,"description":"Number/percent precision; legacy fixed2/percent2 always use two places. A precision-only patch preserves percentage families."},
            "bold":{"type":"boolean"},"italic":{"type":"boolean"},"wrap":{"type":"boolean"},
            "align":{"type":"string","enum":["left","center","right"]},
            **{"border_"+side:{"type":"string","enum":["none","thin","medium","thick","dashed","dotted"]} for side in ("left","right","top","bottom")},
            "border":{"type":"string","enum":["none","box"]},
            "background":{"type":"string","pattern":"^#[0-9a-fA-F]{6}$"}}},
        "CellEdit": {"type":"object","required":["row","col"],"properties":{
            "row":row,"col":col,"source":{"type":"string","maxLength":65536,"description":"Stored input, at most 64 KiB of UTF-8"},
            "kind":{"type":"string","enum":kinds,"default":"auto","description":"Explicit date/time/datetime use YYYY-MM-DD, HH:MM:SS[.ffffff], or YYYY-MM-DDTHH:MM:SS[.ffffff], without timezone offsets. Auto never guesses dates."},
            "style":{"$ref":"#/components/schemas/Style"},
            "clear":{"type":"boolean"},"clear_input":{"type":"boolean"},"replace_style":{"type":"boolean"}}},
        "ClipboardCell":{"type":"object","description":"A full clipboard cell, or {} for an absent cell. Full paste preserves input_kind and translates formula source using the origin coordinates. Kind unmaintained (native format 7) is a formula whose references a structural edit could not maintain: it is never evaluated, reads as the #REF! error, and copies as unmaintained without translation. An edit request naming the kind is rejected; paste preserves the kind a clipboard cell carries (the clipboard is client data).","properties":{
            "source":{"type":"string"},"input_kind":{"type":"string","enum":[*kinds,"unmaintained"],"default":"text"},
            "kind":{"type":"string","enum":[*kinds,"unmaintained","blank"]},"value":scalar,"value_kind":{"type":"string","enum":["date","time","datetime"]},"style":{"$ref":"#/components/schemas/Style"},
            "calculation":{"type":"string","enum":["current","error","pending"]},"error":{"type":"string"},
            "source_omitted":{"type":"boolean"},"value_omitted":{"type":"boolean"}}},
        "Receipt": {"type":"object","required":["operation_id","outcome","state","changed_cells"],"properties":{
            "operation_id":{"type":"string"},"outcome":{"type":"string","enum":["committed"]},"state":{"type":"string"},"changed_cells":{"type":"integer"},
            "reference_warnings":{"type":"integer","description":"Newly broken reference expressions in a row/column operation"},
            "reference_issues":{"type":"array","maxItems":100,"description":"First 100 affected formula locations after the structure change","items":{"type":"object","properties":{"sheet":sheet,"row":row,"col":col}}},
            "unmaintained":{"type":"array","maxItems":100,"description":"First 100 formulas a row/column, sheet or move operation could not read and marked unmaintained, at their locations after the operation","items":{"type":"object","properties":{"sheet":{"type":"string"},"cell":{"type":"string"}}}},
            "unmaintained_count":{"type":"integer","minimum":1,"description":"Number of formulas marked unmaintained; present only when nonzero"}}},
    }
    schemas["FunctionCatalog"]={"type":"object","required":["version","functions","notes"],"properties":{"version":{"type":"integer","enum":[1]},"notes":{"type":"array","items":{"type":"string"}},"functions":{"type":"array","items":{"type":"object","required":["name","signature","summary","rules","examples","category"],"properties":{**{key:{"type":"string"} for key in ("name","signature","summary","rules","category")},"examples":{"type":"array","items":{"type":"object","properties":{"formula":{"type":"string"},"result":scalar,"inputs":{"type":"array","items":{"$ref":"#/components/schemas/CellEdit"}}}}}}}}}}
    schemas["NavigationRequest"]={"type":"object","required":["row","col","direction","expected_state"],"properties":{
        "sheet":sheet,"row":row,"col":col,"direction":{"type":"string","enum":["up","down","left","right"]},"expected_state":{"type":"string"}},
        "description":"Current and adjacent populated inputs move to the end of their contiguous run; otherwise seek the next populated input or grid edge. Formula inputs and empty Text count as populated; native NULL/styled empty auto cells do not. One snapshot, 10,000 candidate stored cells/4 MiB inputs/2 seconds, no evaluation/hooks/writes. Limits reject without a partial target.",
        "example":{"row":1,"col":1,"direction":"down","expected_state":"from-workbook-info"}}
    schemas["Navigation"]={"type":"object","required":["state","sheet","row","col","basis"],"properties":{"state":{"type":"string"},"sheet":sheet,"row":row,"col":col,"basis":{"type":"string","enum":["stored_inputs"]}}}
    schemas["StylesRequest"]={"type":"object","required":["expected_state"],"properties":{"sheet":sheet,**rectangle,"expected_state":{"type":"string","minLength":1}},"description":"Dense style-only rows including {} for absent styles. One snapshot, 10,000 cells, 4 MiB response, two seconds. No source/value reads, formula evaluation, hooks or history; read-only handles supported."}
    schemas["Styles"]={"type":"object","required":["state","sheet","range","rows"],"properties":{"state":{"type":"string"},"sheet":sheet,"range":{"type":"array","minItems":4,"maxItems":4,"items":{"type":"integer"}},"rows":{"type":"array","maxItems":10000,"items":{"type":"array","maxItems":10000,"items":{"$ref":"#/components/schemas/Style"}}}}}
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
    actions['paste'][1]['record_projection']={'type':'boolean','default':False,'description':'Use explicit per-cell source row/column for copied declared-record formulas.'}
    actions['list_edit']=(['id','record_ids'],{'sheet':sheet,'id':{'type':'string'},'record_ids':{'type':'array','items':{'type':'string'}},'columns':{'type':'array','items':col},'mode':{'type':'string','enum':['all','values','formats','clear']},'clear_scope':{'type':'string','enum':['contents','formats','all']},'rows':{'type':'array','items':{'type':'array','items':{'$ref':'#/components/schemas/ClipboardCell'}}},'repeat':{'type':'boolean'}},{'id':'Records','record_ids':['replace-with-reviewed-record-id'],'columns':[1],'mode':'clear'})
    for action in ('list_declare','list_reconcile','list_forget'):
        extra={'spec':{'type':'object'}} if action=='list_declare' else {'mode':{'type':'string','enum':['retain_positions','regenerate']}} if action=='list_reconcile' else {}
        example={'id':'Records',**({'spec':{'r2':3,'c2':2}} if action=='list_declare' else {'mode':'retain_positions'} if action=='list_reconcile' else {})}
        actions[action]=(['id']+list(extra),{'sheet':sheet,'id':{'type':'string'},**extra},example)
    for action in ('metadata_set','metadata_delete'):
        actions[action]=(['namespace','id']+(['payload'] if action=='metadata_set' else []),{'sheet':sheet,'namespace':{'type':'string','enum':['presentation','list']},'id':{'type':'string','minLength':1,'maxLength':64},'payload':{'type':'object'}},{'namespace':'presentation','id':'view',**({'payload':{'frozen_rows':1}} if action=='metadata_set' else {})})
    from .relational_api import install as install_relational_schemas
    install_relational_schemas(schemas,actions)
    from .code_api import install as install_code_schemas
    install_code_schemas(schemas,actions)
    from .names_api import install as install_names_schemas
    install_names_schemas(schemas,actions)
    from .bindings_api import install as install_binding_schemas
    install_binding_schemas(schemas,actions)
    for action, (required, properties, example) in actions.items():
        schema_name = "".join(word.title() for word in action.split("_"))+"Operation"
        mapping[action] = "#/components/schemas/"+schema_name
        schemas[schema_name] = {"type":"object","required":["operation_id","expected_state","action",*required],
            "properties":{**common,"action":{"type":"string","enum":[action]},**properties},
            "example":{"operation_id":"generate-a-uuid","expected_state":"copy-from-workbook-info","action":action,**example}}
        if action in ("module_save","module_delete","macro_run","name_save","name_delete","binding_save","binding_delete"):schemas[schema_name]["additionalProperties"]=False
    schemas["OperationRequest"] = {"oneOf":[{"$ref":ref} for ref in mapping.values()],
        "discriminator":{"propertyName":"action","mapping":mapping},
        "description":"Atomic operations: at most 10,000 affected cells and a 4 MiB request. Range endpoints are inclusive and must be ordered. Copy fill uses the top-left input; series requires a numeric seed. Same-sheet Move retargets supported scalar/fully-contained range references into the cut rectangle; partially intersecting referenced ranges and unsupported syntax reject atomically. Sheet rename updates qualified name references while preserving stable UUID references, scanning at most 10,000 formulas/8 MiB. Sheet duplication preserves other-sheet references and retargets copied self-references, including UUIDs; it copies at most 10,000 stored cells/8 MiB. Sheet deletion replaces surviving name/UUID references and inherited ranges with permanent #REF! errors and reports affected locations; deleted and rewritten cells share the 10,000-cell limit. Unsupported syntax and required hooks reject reference-changing structure operations. Row/column changes retarget absolute and relative A1 references across sheets, expand/contract ranges and report newly broken references; required handlers, unsupported formula syntax and grid overflow reject atomically. Structure changes clear history. No managed SQL or chunked commits.",
        "example":schemas["EditOperation"]["example"]}
    schemas.update({
        "CsvPreviewRequest":{"type":"object","oneOf":[{"required":["csv"],"not":{"anyOf":[{"required":["csv_base64"]},{"required":["encoding"]}]}},{"required":["csv_base64"],"not":{"required":["csv"]}}],
            "properties":{"csv":{"type":"string","description":"Already decoded Unicode text; at most 3 MiB of UTF-8"},
                "csv_base64":{"type":"string","format":"byte","description":"Strict base64 of at most 3 MiB of source bytes; total JSON request remains limited to 4 MiB"},
                "encoding":{"type":"string","enum":list(ENCODINGS),"default":"utf-8-sig","description":"Strict byte decoding; UTF-16 requires a BOM"},
                "temporal_schema":{"type":"object","description":"Optional version-1 SHA-256-bound temporal companion from CSV export; cannot combine with column/header overrides."},
                "column_types":{"type":"object","additionalProperties":{"type":"string","enum":list(COLUMN_TYPES)},"description":"1-based CSV column numbers as keys; unspecified columns stay Text. Numbers/booleans use strict native input rules. Dates/times use native ISO kinds. Conversion errors set can_import=false and omit inputs; successful typed previews return inputs for one managed edit."},
                "header":{"type":"boolean","default":False,"description":"Keep the first CSV record as Text, including typed columns; do not skip it"},
                "delimiter":{"type":"string","minLength":1,"maxLength":1,"default":",","description":"Cannot be a quote, NUL, CR or LF"}},
            "example":{"csv":"ID,Value\n00123,7"}},
        "SnapshotRequest":{"type":"object","required":["path"],"properties":{"path":{"type":"string","description":"New server-local destination; never overwrite an existing file"}},"example":{"path":"/tmp/copy.dhwb"}},
        "CloseRequest":{"type":"object","example":{}},
        "TraceRequest":{"type":"object","required":["row","col","direction","expected_state"],"properties":{"sheet":sheet,"row":row,"col":col,"direction":{"enum":["precedents","dependents"],"type":"string"},"expected_state":{"type":"string","minLength":1}},"description":"Direct syntactic edges, including lazy branches; ranges stay compressed. Snapshot checked; no evaluation or hooks. Limits: 64 sheets, 10,000 formulas/AST work, 8 MiB inputs, 1,000 targets/issues, two seconds. Invalid syntax fails the whole scan; broken references reported explicitly."},
        "FindRequest":{"type":"object","required":["needle"],"properties":{"sheet":sheet,**rectangle,**search,"offset":{"type":"integer","minimum":0,"maximum":MAX_CELLS,"default":0},"limit":{"type":"integer","minimum":1,"maximum":500,"default":500},"expected_state":{"type":"string","minLength":1}},"description":"Row-major pages of up to 500 matches, previews up to 256 characters; count is over the full bounded scope. Later pages require expected_state from the initial response; any supplied state must match the same input snapshot. Each page re-evaluates the bounded search; no result cache, retained transaction or hooks. Two seconds, 10,000 source cells and 8 MiB matched data.","example":{"sheet":"Sheet1","r2":10,"c2":4,"needle":"Pen"}},
        "CsvExportRequest":{"type":"object","properties":{"sheet":sheet,**rectangle},"example":{"sheet":"Sheet1","r2":10,"c2":4}},
        "ListRequest":{"type":"object","properties":{"sheet":sheet,**rectangle,"header":{"type":"boolean","default":False},
            "sort":{"type":"array","maxItems":8,"items":{"type":"object","required":["col"],"properties":{"col":col,"descending":{"type":"boolean","default":False}}}},
            "filters":{"type":"array","maxItems":8,"items":{"type":"object","required":["col","op"],"properties":{"col":col,"op":{"type":"string","enum":["=","<>",">","<",">=","<=","contains"]},"value":scalar,"value_kind":{"type":"string","enum":["date","time","datetime"]}}}}},
            "example":{"sheet":"Sheet1","r2":10,"c2":4,"header":True,"sort":[{"col":2,"descending":True}]}}
    })
    schemas["ListValuesRequest"]={"type":"object","required":["expected_state"],"properties":{**schemas["ListRequest"]["properties"],
        "expected_state":{"type":"string","minLength":1},"view_r1":row,"view_r2":row,"select_c1":col,"select_c2":col,
        "exclude_rows":{"type":"array","maxItems":1024,"items":row},"exclude_columns":{"type":"array","maxItems":1024,"items":col}},
        "description":"Values and styles only, including explicit header unless excluded; formulas become typed calculated values. Empty results are valid. Two seconds, 10,000 source cells, 4 MiB response. Errors withhold the entire snapshot."}
    schemas["ListExportRequest"]={**schemas["ListValuesRequest"],"properties":{**schemas["ListValuesRequest"]["properties"],"mode":{"type":"string","enum":["values"],"default":"values"}}}
    schemas["PreparationRequest"] = {**schemas["CsvPreviewRequest"], "required":["job_id"],
        "properties":{**schemas["CsvPreviewRequest"]["properties"],"job_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,100}$"}},
        "description":"Ephemeral, idempotent input preparation: one active job, four retained jobs, ten seconds each, five-minute retention. No mutation or chunked commit. Reuse the same ID and input after a lost acknowledgement. Apply reviewed inputs as one ordinary managed edit."}
    schemas["SpreadsheetPreparation"] = {"type":"object","required":["job_id","file_base64"],"properties":{
        "job_id":schemas["PreparationRequest"]["properties"]["job_id"],
        "file_base64":{"type":"string","format":"byte","description":"At most 3 MiB of ZIP data / 16 MiB expanded, 128 entries"},
        "scope":{"type":"string","enum":["selection","workbook"],"default":"selection"},
        "sheet_index":{"type":"integer","minimum":1,"maximum":64,"default":1},"row":row,"col":col,
        "mode":{"type":"string","enum":["formulas","mixed","values"],"default":"formulas"}},
        "description":"Selected sheet or whole-workbook inputs, at most 10,000 stored cells total. Whole-workbook XLSX/ODS requires an empty destination and an explicit structural hook when handlers are activated, replaces sheets atomically and clears history. Values mode uses unverified formula caches; mixed mode uses them only for formulas DataHorizon cannot evaluate. XLSX formulas DataHorizon cannot evaluate are kept as formulas (or cached values) and itemized in the report instead of blocking. Whole-workbook previews are importable whenever the package is valid: unsupported content is skipped or degraded and itemized in report_entries (merged across sheets, at most 100 samples per entry, 200 entries) with matching severity totals, and sheets left without cells are still created. Explicit reviewed Apply is one managed edit; no code activation."}
    schemas["SpreadsheetExport"] = {**schemas["CsvExportRequest"],"properties":{**schemas["CsvExportRequest"]["properties"],
        "scope":{"type":"string","enum":["selection","workbook"],"default":"selection"},
        "mode":{"type":"string","enum":["formulas","values"],"default":"formulas"}}}
    schemas["JobAction"] = {"type":"object","properties":{}}
    schemas['CalculationRequest']={'type':'object','required':['job_id','expected_state'],'properties':{**schemas['RangeRequest']['properties'],'job_id':{'type':'string','pattern':'^[A-Za-z0-9_-]{1,100}$'},'expected_state':{'type':'string'},'stream_id':{'type':'string','pattern':'^[A-Za-z0-9_-]{1,64}$','default':'default'}},'description':'Read-only snapshot up to 10,000 cells, 4 MiB, two-second evaluation. State and per-stream generation guard publication; one UTC clock; cancellation never writes workbook inputs. Independent streams keep viewport work from superseding explicit client calculations. Omitted stream_id retains the default shared stream. Optional preview retains bounded source/display previews.'}
    schemas['RecordRequest']={'type':'object','required':['id','expected_state'],'properties':{**schemas['ListRequest']['properties'],'id':{'type':'string'},'expected_state':{'type':'string'},'columns':{'type':'array','items':col},'mode':{'type':'string'},'include_header':{'type':'boolean'},'exclude_rows':{'type':'array','items':row}}}
    schemas['MetadataRequest']={'type':'object','required':['expected_state'],'properties':{'sheet':sheet,'expected_state':{'type':'string'}}}
    schemas['ConversionRequest']={'type':'object','required':['source','kind'],'properties':{'source':{'type':'string','maxLength':65536},'kind':{'type':'string','enum':['number','date','time','datetime','timestamp_utc']},'locale':{'type':'string','enum':['invariant','en_US','de_DE','fr_FR']},'timezone_policy':{'type':'string','enum':['reject','utc']}}}
    schemas['CsvPreviewRequest']['properties']['locale']=schemas['ConversionRequest']['properties']['locale']
    schemas['SpreadsheetPreparation']['properties']['timezone_policy']={'type':'string','enum':['reject','utc'],'default':'reject'}
    from .job_api import install as install_job_schemas,request_schema as job_request_schema
    install_job_schemas(schemas)
    upload_id={"type":"string","pattern":"^[A-Za-z0-9_-]{1,64}$"}
    upload_example=b"PK example bytes"
    schemas["UploadReceipt"]={"type":"object","required":["upload_id","bytes","sha256"],"additionalProperties":False,"properties":{
        "upload_id":upload_id,"bytes":{"type":"integer","minimum":1,"maximum":LARGE_PACKAGE},"sha256":{"type":"string","pattern":"^[0-9a-f]{64}$","description":"SHA-256 of the received bytes, computed while copying"}},
        "description":"The complete body is staged in a private 0600 file, retained for 15 minutes after last use. The bytes are not parsed, executed or echoed; later import requests name the upload_id. At most four stored and two in-progress uploads.",
        "example":{"upload_id":"example-upload","bytes":len(upload_example),"sha256":hashlib.sha256(upload_example).hexdigest()}}
    schemas["UploadReleased"]={"type":"object","required":["released","upload_id"],"additionalProperties":False,"properties":{"released":{"type":"boolean","enum":[True]},"upload_id":upload_id},
        "example":{"released":True,"upload_id":"example-upload"}}
    paths = {}
    for path, (method, summary) in ROUTES.items():
        parameters = [{"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
                      for name in ("workbook", "operation", "job", "upload") if "{" + name + "}" in path]
        for parameter in parameters:
            if parameter["name"]=="upload":parameter["schema"]=upload_id
        operation = {"summary": summary, "parameters": parameters,
                     "responses": {"200": {"description": "JSON result"}, **{str(status):{"description":"Structured error; inspect outcome and operation_id before deciding whether a mutation finished"} for status in (400,401,403,404,408,409,413,500,503)},
                                   "431":{"description":"HTTP parser rejected oversized headers before dispatch; no operation receipt is implied"}},
                     "security": [{"localToken": []}]}
        if method == "post":
            name = "JobAction" if path.startswith("/v1/uploads/") else ("QueryRunRequest" if path.endswith('/queries/jobs') else "QueryPageRequest" if path.endswith('/page') else "JobAction") if '/queries/jobs' in path else "TableCatalogRequest" if path.endswith('/tables') else job_request_schema(path) if "/operation-jobs" in path else "ConversionRequest" if path=="/v1/convert/value" else "RecordRequest" if "/records/" in path else "MetadataRequest" if path.endswith(("/metadata/read","/records/read")) else ("CalculationRequest" if path.endswith("/calculation/jobs") else "JobAction") if "/calculation/jobs" in path else "ListExportRequest" if "/list/export/" in path else "ListValuesRequest" if path.endswith("/list/values") else "SnapshotRequest" if path == "/v1/recovery/inspect" else "SpreadsheetPreparation" if path in ("/v1/import/xlsx/jobs","/v1/import/ods/jobs") else "SpreadsheetExport" if path.endswith(("/export/xlsx","/export/ods")) else "PreparationRequest" if path == "/v1/import/csv/jobs" else "JobAction" if path.startswith("/v1/jobs/") else "OpenRequest" if path == "/v1/open" else "CsvPreviewRequest" if path == "/v1/import/csv/preview" else "RangeRequest" if path.endswith("/range") else "ClipboardRequest" if path.endswith("/clipboard") else "OperationRequest" if path.endswith("/operations") else {
                "code":"CodeReadRequest","names":"NamesReadRequest","inspect":"TableCatalogRequest","repair-preview":"RelationshipRepairRequest","cancel":"JobAction","edit-preview":"TableEditPreviewRequest","schema-preview":"TableSchemaPreviewRequest","read":"TableReadRequest","preview":"TablePreviewRequest","styles":"StylesRequest","navigate":"NavigationRequest","summary":"SummaryRequest","list":"ListRequest","find":"FindRequest","trace":"TraceRequest","snapshot":"SnapshotRequest","close":"CloseRequest","csv":"CsvExportRequest"}[path.rsplit("/",1)[-1]]
            schema = {"$ref":"#/components/schemas/"+name}
            if '/queries/' in path and path.endswith('/preview'):schema={'$ref':'#/components/schemas/QueryDefinitionRequest'}
            if '/queries/' in path and path.endswith('/read'):schema={'$ref':'#/components/schemas/QueryReadRequest'}
            if '/tables/export/' in path or '/queries/jobs/' in path and '/export/' in path:schema={'$ref':'#/components/schemas/RelationalExportRequest'}
            if '/queries/jobs/' in path and '/export/' in path:schema={'$ref':'#/components/schemas/QueryExportRequest'}
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": schema}}}
        for status in ("400","401","403","404","409","413","500","503"):
            operation["responses"][status]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/Error"}}}
        if path.endswith("/code"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/CodeCatalog"}}}
        elif path.endswith("/names"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/NameCatalog"}}}
        elif path=="/v1/functions":
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/FunctionCatalog"}}}
        elif path.endswith("/styles"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/Styles"}}}
        elif path.endswith("/navigate"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/Navigation"}}}
        elif path.endswith("/summary"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/Summary"}}}
        elif path.endswith("/operations"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/Receipt"}}}
        elif path.endswith("/{operation}"):
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"oneOf":[{"$ref":"#/components/schemas/Receipt"},{"$ref":"#/components/schemas/Error"}]}}}
        if '/operation-jobs' in path:
            response='JobReleased' if path.endswith('/release') else 'OperationJobStatus'
            operation['responses']['200']['content']={'application/json':{'schema':{'$ref':'#/components/schemas/'+response}}}
        if path.startswith('/v1/uploads/'):
            operation['responses']['200']['content']={'application/json':{'schema':{'$ref':'#/components/schemas/'+('UploadReleased' if path.endswith('/release') else 'UploadReceipt')}}}
        if method=='put':
            operation['description']=("Admission (origin, token, identifier, Content-Type, Content-Length, limits, capacity) completes before any body byte is read or file is created; "
                "a rejected request closes the connection without reading its body. The body is read in 64-KiB chunks under one 300-second deadline from its first byte. "
                "A short body, disconnect or timeout deletes the partial file. Re-uploading an existing upload_id is JOB_CONFLICT; release it first.")
            operation['requestBody']={'required':True,'content':{'application/octet-stream':{'schema':{'type':'string','format':'binary','minLength':1,'maxLength':LARGE_PACKAGE},'example':upload_example.decode()}}}
            operation['responses']['408']['content']={'application/json':{'schema':{'$ref':'#/components/schemas/Error'}}}
            operation['responses']['409']['description']='JOB_CONFLICT (upload_id exists), JOB_LIMIT (four stored uploads) or UPLOAD_BUSY (two uploads in progress)'
            operation['responses']['411']={'description':'LENGTH_REQUIRED: a decimal Content-Length is required; chunked transfer encoding is refused','content':{'application/json':{'schema':{'$ref':'#/components/schemas/Error'}}}}
            operation['responses']['415']={'description':'UNSUPPORTED_MEDIA_TYPE: Content-Type must be application/octet-stream','content':{'application/json':{'schema':{'$ref':'#/components/schemas/Error'}}}}
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
for(let variant of variants){let option=document.createElement('option');option.textContent=variant.title||variant.example?.action||'Request';option.value=JSON.stringify(variant.example||{});examples.append(option)}
examples.parentElement.hidden=variants.length<2;examples.onchange=()=>{document.querySelector('#body').value=JSON.stringify(JSON.parse(examples.value),null,2)};examples.onchange()};select.onchange()});
document.querySelector('#run').onclick=async()=>{let out=document.querySelector('#result');try{
let method=document.querySelector('#method').value,path=document.querySelector('#path').value;
if(!path.startsWith('/v1/')||path.startsWith('//'))throw Error('Use a /v1/ path');
let r=await fetch(path,{method,headers:{'Authorization':'Bearer '+document.querySelector('#token').value,'Content-Type':'application/json'},body:method==='POST'?document.querySelector('#body').value:undefined});
out.textContent=r.status+'\\n'+JSON.stringify(await r.json(),null,2)}catch(e){out.textContent=String(e)}};</script>"""


class Service(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, token=None, handler=None, *, max_requests=16, read_seconds=10, max_header_bytes=65536, approved_code=(), owner_channel=False, staging_root=None):
        if type(max_requests) is not int or max_requests<1 or type(max_header_bytes) is not int or max_header_bytes<1:
            raise ValueError("Positive request and header limits required")
        if type(read_seconds) not in (int,float) or not math.isfinite(read_seconds) or read_seconds<=0:
            raise ValueError("Positive finite read deadline required")
        from .workbook_code import approval_store
        self.approved_code=approval_store(approved_code)
        self.owner_channel=bool(owner_channel)
        self._request_slots=threading.BoundedSemaphore(max_requests)
        self.read_seconds,self.max_header_bytes=read_seconds,max_header_bytes
        self.token, self.handler = token or secrets.token_urlsafe(32), handler
        self.workbooks, self.registry_lock = {}, threading.RLock()
        self.active_operations={}
        self._path_locks, self._opening = {}, set()
        self._reserved_handles = 0
        self._stopping = False
        from .query_storage import cleanup_terminated
        cleanup_terminated()
        self.jobs = PreparationJobs()
        # Uploads, import stages and exports; a supplied root lets tests inspect staged files.
        self.staging = Staging(staging_root)
        self._uploads, self._upload_lock = set(), threading.Lock()
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

    def shutdown_request(self, request):
        # Closing with unread input can discard the response on Windows. Send
        # EOF first, then discard at most 64 KiB/100 ms without parsing or
        # admitting that input. Slow peers must not hold a handler or accept loop.
        try:
            request.shutdown(socket.SHUT_WR)
            deadline=time.monotonic()+.1
            remaining=65536
            while remaining:
                seconds=deadline-time.monotonic()
                if seconds<=0: break
                request.settimeout(seconds)
                chunk=request.recv(min(8192,remaining))
                if not chunk: break
                remaining-=len(chunk)
        except OSError:
            pass
        finally:
            self.close_request(request)

    def server_bind(self):
        # HTTPServer resolves a hostname here; a numeric loopback service has
        # no use for reverse DNS, which can block startup on macOS runners.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]

    def server_close(self):
        super().server_close()
        self.jobs.close()
        self.staging.close()
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
        repair=boolean(body.get('repair',False),'repair')
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
                    require(book.repair==repair,'OPEN_MODE_CONFLICT','Close existing handles before changing repair mode',409)
                self._reserved_handles += 1
                reserved = True
            try:
                if fresh:
                    book = Workbook(path, create=create, readonly=readonly, handler=self.handler,repair=repair,approved_code=self.approved_code)
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

    @contextmanager
    def upload_slot(self, identifier):
        """Reserve an in-progress upload and its staging file; release the file unless the body completes."""
        with self._upload_lock:
            require(len(self._uploads)<MAX_UPLOADS,"UPLOAD_BUSY",f"At most {MAX_UPLOADS} uploads can be in progress; retry when one finishes",409)
            target=self.staging.new("upload",identifier)
            self._uploads.add(identifier)
        try:
            yield target
        except BaseException:
            self.staging.release("upload",identifier)
            raise
        finally:
            with self._upload_lock:
                self._uploads.discard(identifier)

    def _release_upload(self, identifier):
        with self._upload_lock:
            require(IDENTIFIER.fullmatch(identifier),"INVALID_JOB","Identifier needs 1–64 ASCII letters, digits, _ or -")
            require(identifier not in self._uploads,"JOB_CONFLICT","The upload is still in progress",409)
            self.staging.path("upload",identifier)
            self.staging.release("upload",identifier)
        return {"released":True,"upload_id":identifier}

    def dispatch(self, method, path, body):
        if method=="POST" and path.startswith("/v1/uploads/") and path.endswith("/release") and path.count("/")==4:
            return self._release_upload(unquote(path.split("/")[3]))
        if method=='POST' and path=='/v1/convert/value':
            from .conversion import convert
            return convert(body)
        if method=="GET" and path=="/v1/functions":
            return function_catalog()
        if method == "GET" and path == "/v1/capabilities":
            if not self.owner_channel:return CAPABILITIES
            return dict(CAPABILITIES,macro_activation=CAPABILITIES['macro_activation']+', or by the owning process over its owner channel; never over HTTP')
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
        if method=='POST' and len(suffix)==3 and suffix[0]=='operations' and suffix[2]=='cancel':
            # Cancellation must reach the running writer, rather than queue
            # behind the same path lock held for its synchronous REST request.
            with self.registry_lock:
                require(self.workbooks.get(parts[2]) is book,'NOT_FOUND','Workbook handle was released',404)
                return self._cancel_operation(book,unquote(suffix[1]))
        with self._path_access(book.path):
            with self.registry_lock:
                require(self.workbooks.get(parts[2]) is book,"NOT_FOUND","Workbook handle was released",404)
            return self._dispatch_book(method,parts[2],book,suffix,body)

    def _cancel_operation(self,book,operation):
        with self.registry_lock:event=self.active_operations.get((book,operation))
        if event is not None:event.set()
        return dict(operation_id=operation,cancel_requested=event is not None,instructions='Inspect the durable receipt; a committed operation cannot be undone by cancellation.')

    def _dispatch_book(self, method, handle, book, suffix, body):
        if suffix and suffix[0]=='operation-jobs':
            jobs=book.operation_jobs()
            if method=='POST' and len(suffix)==1:return jobs.create(body)
            if method=='GET' and len(suffix)==2:return jobs.view(suffix[1])
            if method=='POST' and len(suffix)==3:
                identifier,action=suffix[1:]
                if action=='append':return jobs.append(identifier,body.get('cells'))
                if action=='seal':return jobs.seal(identifier)
                if action=='status':return jobs.view(identifier,body.get('offset',0),body.get('limit',100))
                if action in ('start','resume'):return jobs.start(identifier,body,resume=action=='resume')
                if action=='cancel':return jobs.cancel(identifier,body)
                if action=='release':return jobs.release(identifier)
            raise EngineError('NOT_FOUND','Unknown operation job action',404)

        if method=='POST' and suffix==['tables']:
            from .relational import catalog
            return catalog(book,body)
        if method=='POST' and suffix==['tables','preview']:
            from .table_promotion import preview
            return preview(book,body)
        if method=='POST' and suffix==['tables','read']:
            from .relational_rows import read
            return read(book,body)
        if method=='POST' and suffix==['tables','schema-preview']:
            from .table_schema import preview
            return preview(book,body)
        if method=='POST' and suffix==['tables','edit-preview']:
            from .table_edits import preview
            return preview(book,body)
        if method=='POST' and suffix==['relationships','inspect']:
            from .relationships import inspect
            return inspect(book,body)
        if method=='POST' and suffix==['relationships','repair-preview']:
            from .relationships import preview_repair
            return preview_repair(book,body)
        if method=='POST' and suffix==['queries','jobs']:return book.query_jobs().start(body)
        if method=='POST' and suffix==['queries','preview']:
            from .query_views import preview
            return preview(book,body)
        if method=='POST' and suffix==['queries','read']:
            from .query_views import read
            return read(book,body)
        if method=='POST' and len(suffix)==3 and suffix[:2]==['tables','export']:
            from .relational_export import export_table
            return export_table(book,suffix[2],body)
        if method=='POST' and len(suffix)==5 and suffix[:2]==['queries','jobs'] and suffix[3]=='export':
            from .relational_export import export_query
            return export_query(book.query_jobs(),suffix[2],suffix[4],body)
        if len(suffix) in (3,4) and suffix[:2]==['queries','jobs']:
            action=suffix[3] if len(suffix)==4 else None
            require(method=='GET' and action is None or method=='POST' and action in ('page','cancel','release'),'NOT_FOUND','Unknown query job resource',404)
            jobs=book.query_jobs()
            return jobs.page(suffix[2],body) if action=='page' else jobs.access(suffix[2],action)
        if method=='POST' and suffix==['records','copy']:
            from .record_operations import copy
            return copy(book,body)
        if method=='POST' and len(suffix)==3 and suffix[:2]==['records','export']:
            from .record_operations import export
            return export(book,suffix[2],body)
        if method=='POST' and suffix==['records','read']:
            from .records import read
            return read(book,body)
        if method=='POST' and suffix==['metadata','read']:
            from .metadata import read
            return read(book,body.get('sheet','Sheet1'),body)
        if method=='POST' and suffix==['calculation','jobs']:
            return book.calculation_jobs().start(body)
        if len(suffix) in (3,4) and suffix[:2]==['calculation','jobs']:
            action=suffix[3] if len(suffix)==4 else None
            require((method=='GET' and action is None) or (method=='POST' and action in ('cancel','release')),'NOT_FOUND','Unknown calculation action',404)
            return book.calculation_jobs().access(suffix[2],action)
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
        if method == "POST" and suffix == ["names"]:
            from .names import read as read_names
            return read_names(book,body)
        if method == "POST" and suffix == ["code"]:
            from .workbook_code import read
            return read(book,body)
        if method == "POST" and suffix == ["styles"]:
            return book.read_styles(body.get("sheet","Sheet1"),body)
        if method == "POST" and suffix == ["range"]:
            return book.read(body.get("sheet", "Sheet1"), body)
        if method == "POST" and suffix == ["clipboard"]:
            return book.read(body.get("sheet", "Sheet1"), {**body,"preview":False},limit=MAX_CELLS)
        if method == "POST" and len(suffix)==3 and suffix[:2]==["list","export"]:
            return export_list(book,suffix[2],body)
        if method == "POST" and suffix == ["list","values"]:
            return book.list_values(body.get("sheet","Sheet1"),body)
        if method == "POST" and suffix == ["list"]:
            return book.list_view(body.get("sheet", "Sheet1"), body)
        if method == "POST" and suffix == ["navigate"]:
            return book.navigate(body.get("sheet","Sheet1"),body)
        if method == "POST" and suffix == ["summary"]:
            return book.summary(body.get("sheet","Sheet1"),body)
        if method == "POST" and suffix == ["trace"]:
            return book.trace(body.get("sheet", "Sheet1"), body)
        if method == "POST" and suffix == ["find"]:
            return book.find(body.get("sheet", "Sheet1"), body)
        if method == "POST" and suffix == ["operations"]:
            operation=body.get('operation_id');require(isinstance(operation,str) and 1<=len(operation)<=100,'OPERATION_ID','An operation ID is required')
            key=(book,operation)
            with self.registry_lock:
                require(key not in self.active_operations,'OPERATION_RUNNING','Operation is active; inspect its receipt before retrying',409)
                event=threading.Event();self.active_operations[key]=event
            try:return book.mutate(body,cancel_event=event)
            finally:
                with self.registry_lock:self.active_operations.pop(key,None)
        if method=='POST' and len(suffix)==3 and suffix[0]=='operations' and suffix[2]=='cancel':
            return self._cancel_operation(book,unquote(suffix[1]))
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
            snapshot=book.read_workbook() if whole else book.read(body.get("sheet","Sheet1"),{**body,"preview":False},limit=MAX_CELLS,native=True)
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

    def receive_upload(self, path):
        """Admit, then copy exactly Content-Length untrusted bytes to a staging file without interpreting them."""
        self.close_connection=True  # A rejected body is never read, so the connection cannot be reused.
        require(path.startswith("/v1/uploads/"),"NOT_FOUND","Unknown API resource",404)
        identifier=unquote(path[len("/v1/uploads/"):])
        stem,_,action=identifier.rpartition("/")
        require(not (action=="release" and IDENTIFIER.fullmatch(stem)),"METHOD_NOT_ALLOWED","Release an upload with POST",405)
        require(IDENTIFIER.fullmatch(identifier),"INVALID_JOB","Identifier needs 1–64 ASCII letters, digits, _ or -")
        require(self.headers.get_content_type()=="application/octet-stream","UNSUPPORTED_MEDIA_TYPE","Content-Type must be application/octet-stream",415)
        lengths=self.headers.get_all("Content-Length") or []
        require("Transfer-Encoding" not in self.headers and len(lengths)==1 and lengths[0].strip().isascii() and lengths[0].strip().isdigit(),
                "LENGTH_REQUIRED","A decimal Content-Length is required; chunked transfer encoding is not accepted",411)
        length=int(lengths[0])
        require(0<length<=LARGE_PACKAGE,"REQUEST_LIMIT",f"Upload must be 1 byte to {LARGE_PACKAGE//(1024*1024)} MiB",413)
        with self.server.upload_slot(identifier) as target:
            # One total body deadline replaces the header read budget from here on.
            self.rfile.raw.deadline=time.monotonic()+UPLOAD_SECONDS
            digest,remaining=hashlib.sha256(),length
            descriptor=os.open(target,os.O_WRONLY|os.O_TRUNC|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_BINARY",0))
            with open(descriptor,"wb") as output:
                while remaining:
                    try:
                        chunk=self.rfile.read(min(UPLOAD_CHUNK,remaining))
                    except TimeoutError as error:
                        raise EngineError("REQUEST_TIMEOUT","Upload read deadline exceeded; the partial upload was deleted",408) from error
                    except ConnectionError as error:
                        raise EngineError("INVALID_UPLOAD","Connection lost before the body completed; the partial upload was deleted") from error
                    require(chunk,"INVALID_UPLOAD","Body ended before its Content-Length; the partial upload was deleted")
                    digest.update(chunk)
                    output.write(chunk)
                    remaining-=len(chunk)
        return {"upload_id":identifier,"bytes":length,"sha256":digest.hexdigest()}

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
            if self.command == "PUT":
                return self.respond(200, self.receive_upload(path))
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
    do_PUT = run_request


def read_owner_channel(stream, store, limit=4096):
    """Apply {"approve": key} lines read from an unbuffered stream.

    Problems yield one short stderr line and never stop the service. Lines are split here
    because a buffered reader's lock can abort interpreter shutdown while a daemon thread waits.
    """
    def warn(text):
        try:print('owner channel: '+text,file=sys.stderr,flush=True)
        except (OSError,ValueError):pass
    def apply(line):
        try:
            message=json.loads(line.decode('utf-8'))
            valid=isinstance(message,dict) and set(message)=={'approve'}
        except (ValueError,UnicodeError):valid=False
        if not valid:return warn('line ignored; expected {"approve": key}')
        try:store.grant(message['approve'])
        except ValueError:warn('approval rejected')
    pending,skipping=bytearray(),False
    while True:
        try:chunk=stream.read(4096)
        except (OSError,ValueError):return
        if not chunk:
            if pending and not skipping:apply(bytes(pending))
            return
        while chunk:
            end=chunk.find(b'\n')
            piece,chunk=(chunk,b'') if end<0 else (chunk[:end],chunk[end+1:])
            if not skipping:
                pending+=piece
                if len(pending)>limit:skipping=True;pending.clear()
            if end>=0:
                if skipping:warn('line too long, ignored')
                elif pending:apply(bytes(pending))
                pending.clear();skipping=False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--handler", type=Path, help="Explicitly activate a trusted service-side on_change Python file")
    parser.add_argument("--approve-code",action="append",default=[],help="Operator-approved workbook UUID:code SHA256 (repeatable; process-local)")
    parser.add_argument("--owner-channel",action="store_true",help="Accept code approval lines from standard input (the owning parent process only)")
    args = parser.parse_args()
    server = Service(args.port, handler=args.handler.resolve() if args.handler else None,approved_code=args.approve_code,owner_channel=args.owner_channel)
    # Only the launching parent receives this credential through its private pipe.
    print(encoded({"url": f"http://127.0.0.1:{server.server_port}", "token": server.token, "api_version": 1}), flush=True)
    if args.owner_channel:
        threading.Thread(target=read_owner_channel,args=(open(sys.stdin.fileno(),'rb',buffering=0,closefd=False),server.approved_code),daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
