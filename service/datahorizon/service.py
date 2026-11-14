# SPDX-License-Identifier: GPL-3.0-only
"""Authenticated loopback-only development REST service; no third-party runtime."""

import argparse
from contextlib import contextmanager
import copy
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
import stat
from socketserver import TCPServer
import sqlite3
import sys
import threading
import time
import traceback
from urllib.parse import unquote, urlsplit
import uuid

from .engine import EngineError, MAX_CELLS, STAGE_PATH, Workbook, boolean, encoded, require
from .formula import MAX_COLS, MAX_ROWS, SUPPORTED_FUNCTIONS
from .csv_import import COLUMN_TYPES, ENCODINGS, preview_csv
from .jobs import PreparationJobs
from .interchange import mode as import_mode, xlsx_preview, xlsx_export, preparation_scope
from .import_stage import ImportStage
from .ods import ods_preview, ods_export
from .recovery import inspect_native
from .list_export import export_list
from .functions import function_catalog
from .staging import IDENTIFIER, Staging, sweep_terminated
from .conversion_report import ConversionReport
from .xlsx_stream import LARGE_CELLS, LARGE_EXPANDED, LARGE_PACKAGE, LARGE_SHEETS, export_lines, read_workbook, write_workbook
from . import ods_stream
from .ods_stream import read_workbook as ods_read_workbook, write_workbook as ods_write_workbook

CAPABILITIES = {
    "api_version": 1, "format_version": 8, "supported_format_versions": [1,2,3,4,5,6,7,8], "new_file_format_version": 1, "max_operation_cells": MAX_CELLS,
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
CAPABILITIES['operations'].extend(['chart_create','chart_update','chart_delete'])
CAPABILITIES.update(charts=True,max_charts=64,chart_spec_version=1,chart_jobs=True,chart_job_seconds=30,max_chart_render_points=10_000)
MAX_UPLOADS, UPLOAD_SECONDS, UPLOAD_CHUNK = 2, 300, 64*1024
CAPABILITIES.update(uploads=True,max_upload_bytes=LARGE_PACKAGE,max_import_cells=LARGE_CELLS,max_concurrent_uploads=MAX_UPLOADS)
LARGE_PREPARATION_SECONDS = 300
PACKAGE_INPUTS = ("file_base64","upload_id","source_path")
CAPABILITIES.update(import_source_path=True,large_preparation_job_seconds=LARGE_PREPARATION_SECONDS)
MAX_DOWNLOADS, DOWNLOAD_SECONDS, DOWNLOAD_CHUNK = 2, 300, 64*1024
EXPORT_JOB_SECONDS = 300
EXPORT_JOB = "xlsx_export"  # the PreparationJobs kind of a large XLSX export
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ODS_TYPE = "application/vnd.oasis.opendocument.spreadsheet"
# The formats of the large path: the PreparationJobs kind of each format's export job, and what its download is.
LARGE_FORMATS = ("xlsx","ods")
EXPORT_KINDS = {"xlsx":EXPORT_JOB,"ods":"ods_export"}
DOWNLOADS = {EXPORT_JOB:(XLSX_TYPE,"export.xlsx"),"ods_export":(ODS_TYPE,"export.ods")}
CAPABILITIES.update(export_jobs=True,max_export_cells=LARGE_CELLS,export_job_seconds=EXPORT_JOB_SECONDS,max_concurrent_downloads=MAX_DOWNLOADS,
                    large_interchange_formats=list(LARGE_FORMATS))

ROUTES = {
    "/v1/workbooks/{workbook}/code": ("post", "Inspect inert workbook modules and the exact service-local activation key"),
    "/v1/workbooks/{workbook}/names": ("post", "Read the state-checked workbook name catalog"),
    "/v1/workbooks/{workbook}/charts": ("post", "Read the state-checked chart catalog"),
    "/v1/workbooks/{workbook}/charts/suggest": ("post", "Suggest a chart specification from a range"),
    "/v1/workbooks/{workbook}/charts/jobs": ("post", "Start a cancellable read-only chart resolve or render job"),
    "/v1/workbooks/{workbook}/charts/jobs/{job}": ("get", "Read a chart job; a result is withheld as stale once the workbook changes"),
    "/v1/workbooks/{workbook}/charts/jobs/{job}/cancel": ("post", "Cancel a chart job or discard its result"),
    "/v1/workbooks/{workbook}/charts/jobs/{job}/release": ("post", "Release a terminal chart job"),
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
    "/v1/import/ods/jobs": ("post", "Prepare bounded ODS selected-sheet or whole-workbook inputs with conversion review, or a whole workbook from an upload or local file into an import stage"),
    "/v1/workbooks/{workbook}/export/ods": ("post", "Bounded ODS selection or whole-workbook snapshot and conversion report"),
    "/v1/import/xlsx/jobs": ("post", "Prepare bounded selected-sheet or whole-workbook XLSX with conversion review, or a whole workbook from an upload or local file into an import stage"),
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
    "/v1/uploads/{upload}": ("put", f"Stream a raw spreadsheet package of 1 byte to {LARGE_PACKAGE//(1024*1024)} MiB into private staging; never parsed by this route"),
    "/v1/uploads/{upload}/release": ("post", "Delete a completed staged upload"),
    "/v1/workbooks/{workbook}/export/xlsx/jobs": ("post", f"Export the whole workbook, up to {LARGE_CELLS:,} cells, as XLSX formulas into private staging from one consistent state"),
    "/v1/workbooks/{workbook}/export/ods/jobs": ("post", f"Export the whole workbook, up to {LARGE_CELLS:,} cells, as ODS formulas into private staging from one consistent state"),
    "/v1/exports/{export}": ("get", "Stream a completed large XLSX or ODS export as a binary download"),
    "/v1/exports/{export}/release": ("post", "Delete a large XLSX or ODS export and forget its job"),
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
        "workbook_import": ([],{"sheets":{"type":"array","minItems":1,"maxItems":64,"description":"Reviewed sheets from a bounded preparation, at most 10,000 cells in all","items":{"type":"object","required":["name","cells"],"properties":{"name":name,"cells":{"type":"array","items":{"$ref":"#/components/schemas/CellEdit"}}}}},
            "stage_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,64}$","description":"A completed upload or source_path preparation's stage_id; a missing or released stage is JOB_NOT_FOUND (404) unless this operation_id already has a receipt"},
            "stage_digest":{"type":"string","pattern":"^[0-9a-f]{64}$","description":"The reviewed summary's stage_digest; it must equal the digest recomputed from the stage, and a source_path stage's file must keep its recorded size and modification time, otherwise STALE_PLAN (409) with nothing changed"},
            "target":{"type":"string","enum":["replace_empty","add_sheets"],"default":"replace_empty","description":"replace_empty replaces the sheets of a workbook with no cells, tables, queries, names, modules or bindings (DESTINATION_NOT_EMPTY otherwise). add_sheets appends the sheets after the existing ones and leaves everything else untouched; a name equal to an existing sheet's, ignoring ASCII case, rejects the whole import with SHEET_NAME_CONFLICT (409) listing conflicts. Formulas naming an existing sheet refer to it."}},
            {"sheets":[{"name":"Sheet1","cells":[]}]}),
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
    from .charts_api import install as install_chart_schemas
    install_chart_schemas(schemas,actions)
    for action, (required, properties, example) in actions.items():
        schema_name = "".join(word.title() for word in action.split("_"))+"Operation"
        mapping[action] = "#/components/schemas/"+schema_name
        schemas[schema_name] = {"type":"object","required":["operation_id","expected_state","action",*required],
            "properties":{**common,"action":{"type":"string","enum":[action]},**properties},
            "example":{"operation_id":"generate-a-uuid","expected_state":"copy-from-workbook-info","action":action,**example}}
        if action in ("module_save","module_delete","macro_run","name_save","name_delete","binding_save","binding_delete","chart_create","chart_update","chart_delete"):schemas[schema_name]["additionalProperties"]=False
    schemas["WorkbookImportOperation"].update(oneOf=[
        {"title":"Reviewed sheets","required":["sheets"],"not":{"anyOf":[{"required":["stage_id"]},{"required":["stage_digest"]}]},
         "example":schemas["WorkbookImportOperation"]["example"]},
        {"title":"Prepared stage","required":["stage_id","stage_digest"],"not":{"required":["sheets"]},
         "example":{"operation_id":"generate-a-uuid","expected_state":"copy-from-workbook-info","action":"workbook_import",
                    "stage_id":"example-xlsx-apply","stage_digest":"copy-from-the-review-summary","target":"replace_empty"}}],
        description="Imports a whole workbook as one operation that clears Undo/Redo, raises the structural before/after event "
            "(arguments: target and each imported sheet's name and cell count) and no cell change events; an activated required handler "
            "without on_structure rejects before any change. Supply reviewed sheets (at most 10,000 cells) or a prepared stage (up to "
            "1,000,000 cells and 255 sheets, a 300-second budget with cooperative cancellation). A stage is loaded in one transaction "
            "that holds the workbook's writer lock for the whole load (up to 300 seconds); other writers wait for it or time out; "
            "cancellation or any failure rolls back sheets and cells and records a rolled-back receipt. Temporal inputs raise the native "
            "format to 2. A successful apply deletes the stage and its preparation job; a refused or failed apply keeps the stage. "
            "Staged cells are stored exactly as the same cells applied as edits would be. A retry with the same operation_id returns the "
            "stored receipt and releases nothing. The receipt adds imported: {sheets, cells}.")
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
        "sheet_index":{"type":"integer","minimum":1,"maximum":4096,"default":1,"description":"1-based position in the package's sheet list (an XLSX chart, dialog or macro sheet keeps its place but cannot be chosen); any worksheet can be selected"},"row":row,"col":col,
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
    large={"job_id":{"type":"string","pattern":"^[A-Za-z0-9_-]{1,64}$","description":"Also names the import stage, so at most 64 characters"},
        "scope":{"type":"string","enum":["workbook"],"description":"Required: a whole workbook is prepared into a stage"},
        "mode":schemas["SpreadsheetPreparation"]["properties"]["mode"],"timezone_policy":schemas["SpreadsheetPreparation"]["properties"]["timezone_policy"]}
    staged=(f"Reads the package with the streaming reader into a private import stage (at most {LARGE_CELLS:,} stored cells, {LARGE_SHEETS} sheets, "
            f"{LARGE_PACKAGE//(1024*1024)} MiB packed, {LARGE_EXPANDED//(1024*1024)} MiB expanded) "
            "within 300 seconds. The completed job result is the review summary (scope, stage_id equal to job_id, stage_digest, sheets, stored_cells, report, report_entries, totals, "
            "sample of the first 100 cells per sheet, can_import, format, mode, source), always under 1 MiB, never the cells. No workbook changes. "
            "Cancelling or releasing the job, or its five-minute expiry, deletes the stage; at most four stages exist at once (JOB_LIMIT).")
    schemas["SpreadsheetPreparation"].update(title="Inline package",example={"job_id":"example-xlsx-inline","file_base64":"UEsDBA==","scope":"workbook"},
        **{"not":{"anyOf":[{"required":["upload_id"]},{"required":["source_path"]}]}})
    schemas["XlsxUploadPreparation"]={"type":"object","title":"Staged upload","required":["job_id","upload_id","scope"],"additionalProperties":False,
        "properties":{**large,"upload_id":{**upload_id,"description":"A completed upload from PUT /v1/uploads/{upload}; preparation never releases it"}},
        "description":staged+" The source records the upload's SHA-256.",
        "example":{"job_id":"example-xlsx-upload","upload_id":"example-upload","scope":"workbook","mode":"mixed"}}
    schemas["XlsxPathPreparation"]={"type":"object","title":"Local file","required":["job_id","source_path","scope"],"additionalProperties":False,
        "properties":{**large,"source_path":{"type":"string","minLength":1,"description":
            "Absolute path to a regular file the service process can read, opened only for reading. Intended for a service on the same machine as the caller: "
            "any holder of the bearer token can ask the service to read a file its user can read. The file is only parsed as a spreadsheet package; none of its bytes "
            "are returned except parsed cells in the sample. Symlinks are resolved once; the resolved path, size and modification time are recorded and checked again at Apply."}},
        "description":staged,"example":{"job_id":"example-xlsx-path","source_path":"/tmp/example.xlsx","scope":"workbook"}}
    schemas["XlsxPreparationRequest"]={"oneOf":[{"$ref":"#/components/schemas/"+name} for name in ("SpreadsheetPreparation","XlsxUploadPreparation","XlsxPathPreparation")],
        "description":"Exactly one of file_base64 (the bounded inline preview, unchanged), upload_id or source_path; otherwise INVALID_ARGUMENT. "
            "With upload_id or source_path, scope must be workbook and row, col and sheet_index are refused; a missing upload is JOB_NOT_FOUND, "
            "a source_path that is not an absolute path to an existing regular file is INVALID_PATH, an oversized file is LIMIT. "
            "A malformed package ends the job failed with INVALID_SPREADSHEET, a limit with LIMIT, a cancel with CANCELLED; the stage is then deleted."}
    # The same staged preparation for ODS, read by the streaming ODS reader.
    schemas["OdsUploadPreparation"]={**copy.deepcopy(schemas["XlsxUploadPreparation"]),
        "example":{"job_id":"example-ods-upload","upload_id":"example-ods","scope":"workbook","mode":"mixed"}}
    schemas["OdsPathPreparation"]={**copy.deepcopy(schemas["XlsxPathPreparation"]),"example":{"job_id":"example-ods-path","source_path":"/tmp/example.ods","scope":"workbook"}}
    schemas["OdsPreparationRequest"]={"oneOf":[{"$ref":"#/components/schemas/"+name} for name in ("SpreadsheetPreparation","OdsUploadPreparation","OdsPathPreparation")],
        "description":schemas["XlsxPreparationRequest"]["description"]+" The ODS package must start with its uncompressed mimetype entry; "
            "an encrypted package is INVALID_SPREADSHEET. Empty repeated rows and cells cost nothing; a repeat that would store more than "
            f"{LARGE_CELLS:,} cells is LIMIT before any cell is stored. The large-path reader (upload_id or source_path) accepts trailing empty "
            "row and column repeats that extend beyond the 1,048,576 × 16,384 grid, which the inline file_base64 reader rejects. The summary's format is ods."}
    schemas["UploadReleased"]={"type":"object","required":["released","upload_id"],"additionalProperties":False,"properties":{"released":{"type":"boolean","enum":[True]},"upload_id":upload_id},
        "example":{"released":True,"upload_id":"example-upload"}}
    export_id={"type":"string","pattern":"^[A-Za-z0-9_-]{1,64}$"}
    schemas["XlsxExportJobRequest"]={"type":"object","required":["job_id"],"additionalProperties":False,"properties":{
        "job_id":{**export_id,"description":"Also names the export file, so at most 64 characters; the export_id equals it"},
        "mode":{"type":"string","enum":["formulas"],"default":"formulas","description":
            "Formulas are written without cached values and full recalculation on load is requested. values and mixed are UNSUPPORTED (400) here; "
            "the bounded POST /v1/workbooks/{workbook}/export/xlsx route still exports values"}},
        "description":(f"Starts a preparation-style job (status, cancel and release through /v1/jobs/{{job}}) that copies one consistent workbook state "
            f"to a private snapshot, then writes every sheet into a private staging file cell by cell: at most {LARGE_CELLS:,} cells, {LARGE_SHEETS} sheets, "
            f"{LARGE_EXPANDED//(1024*1024)} MiB uncompressed and 1,024 distinct styles, within {EXPORT_JOB_SECONDS} seconds, without holding the workbook while it writes. "
            "Strings are written inline. Exceeding a limit, or text over Excel's 32,767-character cell limit, fails the job with LIMIT and deletes the partial file; "
            "cancelling deletes it too. The job holds the file: releasing or cancelling the job, or its five-minute expiry after the last download, deletes it. "
            "A retry with the same job_id and inputs from the same workbook returns the existing job; any other reuse is JOB_CONFLICT. "
            "At most four exports are stored at once (JOB_LIMIT), and one preparation or export job runs at a time (JOB_BUSY)."),
        "example":{"job_id":"example-xlsx-export","mode":"formulas"}}
    schemas["OdsExportJobRequest"]={**copy.deepcopy(schemas["XlsxExportJobRequest"]),
        "description":schemas["XlsxExportJobRequest"]["description"].replace("Strings are written inline. Exceeding a limit, or text over Excel's 32,767-character cell limit,",
            "Formulas carry no cached values (each is itemized as approximated). Exceeding a limit")
            +" The body of content.xml is first written to a second private staging file, deleted when the job ends, because the "
            "automatic styles that precede it are known only after every cell. As in the bounded ODS export, a formula the published "
            "function tier cannot translate fails the job with UNSUPPORTED_FORMULA and text with XML control characters with UNSUPPORTED_CELL. "
            "The download is served as application/vnd.oasis.opendocument.spreadsheet named export.ods.",
        "example":{"job_id":"example-ods-export","mode":"formulas"}}
    schemas["OdsExportJobRequest"]["properties"]["mode"]={**schemas["OdsExportJobRequest"]["properties"]["mode"],"description":
        "Formulas are written without cached values. values and mixed are UNSUPPORTED (400) here; the bounded POST /v1/workbooks/{workbook}/export/ods route still exports values"}
    severities=("converted","approximated","omitted","unevaluable")
    report_example=[{"category":"style.border","severity":"approximated","detail":"Border colors map to the native default color.","count":1,"samples":[{"sheet":"Sheet1","cell":"A1"}]}]
    totals_example={"converted":0,"approximated":1,"omitted":0,"unevaluable":0}
    schemas["ReportEntry"]={"type":"object","required":["category","severity","detail","count","samples"],"properties":{
        "category":{"type":"string","description":"Stable dotted category, such as style.border or formula.unsupported_function. "
                    "sheet.feature and workbook.feature mark a worksheet or workbook feature that is not imported; their detail is a stable feature id such as merged_cells or defined_names"},
        "severity":{"type":"string","enum":list(severities),"description":
            "converted: stored in another form (for example a formula replaced by its cached value, or an error value kept as text); "
            "approximated: kept with a close but not identical meaning; omitted: dropped; unevaluable: a formula kept that DataHorizon cannot calculate"},
        "detail":{"type":"string"},"count":{"type":"integer","minimum":1,"description":"Cells (or, for a feature entry, items such as merged ranges, rules or columns) this entry covers"},
        "at_least":{"type":"boolean","enum":[True],"description":"Present only when the count saturated at a scanner bound (for example more distinct validation names than "
                    "are tracked): the entry covers at least count items. Absent otherwise"},
        "samples":{"type":"array","maxItems":100,"description":"First locations in reading order; a null cell is sheet-wide and a null sheet is workbook-wide",
                   "items":{"type":"object","properties":{"sheet":{"type":"string","nullable":True},"cell":{"type":"string","nullable":True}}}}},
        "description":"One merged line of a conversion report. Entries are ordered by severity (unevaluable, omitted, approximated, converted), then count; at most 200, the last of which may summarise the remainder.",
        "example":report_example[0]}
    schemas["ReportTotals"]={"type":"object","required":list(severities),"properties":{key:{"type":"integer","minimum":0} for key in severities},
        "description":"Counts per severity over every report entry, including any not itemized","example":totals_example}
    entry={"$ref":"#/components/schemas/ReportEntry"}
    totals={"$ref":"#/components/schemas/ReportTotals"}
    lines={"type":"array","items":{"type":"string"},"description":"Fixed scope statements, then one readable line per report entry"}
    scope={"type":"array","items":{"type":"string"},"description":"The fixed scope statements alone: the lines report starts with, before its one line per report entry. "
           "Read these instead of slicing report"}
    schemas["ExportJobResult"]={"type":"object","required":["export_id","bytes","sha256","state","report","scope_lines","report_entries","totals"],"additionalProperties":False,"properties":{
        "export_id":export_id,"bytes":{"type":"integer","minimum":1},"sha256":{"type":"string","pattern":"^[0-9a-f]{64}$","description":"SHA-256 of the file GET /v1/exports/{export} returns"},
        "state":{"type":"string","description":"The workbook state that was exported"},"report":lines,"scope_lines":scope,
        "report_entries":{"type":"array","maxItems":200,"items":entry},"totals":totals},
        "description":"The result of a completed XLSX or ODS export job: what to download and the conversion report (approximated cells, such as unmaintained formulas written unchanged, are itemized).",
        "example":{"export_id":"example-xlsx-export","bytes":5120,"sha256":"0"*64,"state":"copy-from-workbook-info","report":["Formulas are written without cached values."],
                   "scope_lines":["Formulas are written without cached values."],"report_entries":report_example,"totals":totals_example}}
    schemas["XlsxExportJobResult"]=schemas["ExportJobResult"]  # The earlier name of the same result, kept for clients.
    schemas["StagedImportSummary"]={"type":"object","required":["scope","stage_id","stage_digest","sheets","stored_cells","report","scope_lines","report_entries","totals","sample","can_import","format","mode","source"],"properties":{
        "scope":{"type":"string","enum":["workbook"]},"stage_id":{**upload_id,"description":"Equal to the job_id; names the stage for workbook_import"},
        "stage_digest":{"type":"string","pattern":"^[0-9a-f]{64}$","description":"Send unchanged with stage_id to workbook_import"},
        "sheets":{"type":"array","maxItems":LARGE_SHEETS,"items":{"type":"object","required":["name","stored_cells"],"properties":{"name":name,"stored_cells":{"type":"integer","minimum":0}}}},
        "stored_cells":{"type":"integer","minimum":0,"maximum":LARGE_CELLS},"report":lines,"scope_lines":scope,"report_entries":{"type":"array","maxItems":200,"items":entry},"totals":totals,
        "sample":{"type":"array","description":"The first 100 stored cells of each sheet, row-major, with resolved styles; long sources are truncated and flagged",
                  "items":{"type":"object","required":["sheet","cells"],"properties":{"sheet":{"type":"string"},"cells":{"type":"array","maxItems":100,"items":{"type":"object","properties":{
                      "row":row,"col":col,"source":{"type":"string"},"kind":{"type":"string"},"style":{"$ref":"#/components/schemas/Style"},"truncated":{"type":"boolean"}}}}}}},
        "can_import":{"type":"boolean","description":"True for every completed stage with at least one worksheet: unsupported content is itemized in the report instead of blocking. False only when the package has no worksheets (for example only chart sheets); such a stage cannot be applied"},
        "format":{"type":"string","enum":list(LARGE_FORMATS)},"mode":{"type":"string","enum":["formulas","mixed","values"]},
        "source":{"type":"object","required":["kind"],"properties":{"kind":{"type":"string","enum":["upload","path"]},"upload_id":upload_id,
            "sha256":{"type":"string","pattern":"^[0-9a-f]{64}$","description":"An upload's SHA-256 as read by the preparation"},
            "path":{"type":"string","description":"The resolved source_path"},"size":{"type":"integer","minimum":0},"mtime_ns":{"type":"integer"}}}},
        "description":"The completed result of a whole-workbook preparation from upload_id or source_path: what Apply would import and what it converts, approximates or omits. Always under 1 MiB; never the cells.",
        "example":{"scope":"workbook","stage_id":"example-xlsx-upload","stage_digest":"0"*64,"sheets":[{"name":"Sheet1","stored_cells":1}],"stored_cells":1,
                   "report":["All sheets, names, stored inputs and mapped direct cell styles from one consistent workbook state."],
                   "scope_lines":["All sheets, names, stored inputs and mapped direct cell styles from one consistent workbook state."],"report_entries":report_example,"totals":totals_example,
                   "sample":[{"sheet":"Sheet1","cells":[{"row":1,"col":1,"source":"7","kind":"number","style":{}}]}],"can_import":True,"format":"xlsx","mode":"mixed",
                   "source":{"kind":"upload","upload_id":"example-upload","sha256":"0"*64}}}
    schemas["SpreadsheetImportPreview"]={"type":"object","required":["can_import","report","report_entries","totals","stored_cells","issues"],"properties":{
        "can_import":{"type":"boolean"},"issues":{"type":"array","items":{"type":"string"},"description":"Problems that block Apply"},"report":lines,"scope_lines":scope,
        "report_entries":{"type":"array","maxItems":200,"items":entry},"totals":totals,"stored_cells":{"type":"integer","minimum":0,"maximum":MAX_CELLS},
        "scope":{"type":"string","enum":["workbook"],"description":"Present for a whole-workbook preview"},
        "sheet":{"type":"string","description":"The selected sheet (selection scope)"},"sheets":{"type":"array","items":{"type":"string"},"description":"Every sheet name in the package (selection scope)"},
        "cells":{"type":"array","items":{"$ref":"#/components/schemas/CellEdit"},"description":"Reviewed edits for one managed edit (selection scope)"},
        "workbook_sheets":{"type":"array","items":{"type":"object","required":["name","cells"],"properties":{"name":name,"cells":{"type":"array","items":{"$ref":"#/components/schemas/CellEdit"}}}},
                           "description":"Reviewed sheets for workbook_import (workbook scope)"}},
        "description":"The completed result of a bounded inline (file_base64) preparation.",
        "example":{"can_import":True,"issues":[],"report":[],"report_entries":[],"totals":dict.fromkeys(severities,0),"stored_cells":1,"scope":"workbook",
                   "workbook_sheets":[{"name":"Sheet1","cells":[{"row":1,"col":1,"source":"7","kind":"number"}]}]}}
    job_status={"type":"string","enum":["running","completed","failed","cancelled"]}
    job_view={"job_id":{"type":"string"},"status":job_status,"completed":{"type":"integer","minimum":0,"description":"Progress units of the current phase"},"phase":{"type":"string"},
              "error":{"$ref":"#/components/schemas/Error"}}
    schemas["PreparationJob"]={"type":"object","required":["job_id","status","completed","phase"],"properties":{**job_view,"result":{"type":"object","description":
        "Present when completed: a CSV preview, a SpreadsheetImportPreview, a StagedImportSummary or an ExportJobResult, by the route that started the job"}},
        "description":"A preparation or export job. Poll while status is running; error is present when failed or cancelled.",
        "example":{"job_id":"example-xlsx-upload","status":"running","completed":0,"phase":"Queued"}}
    schemas["SpreadsheetPreparationJob"]={"type":"object","required":["job_id","status","completed","phase"],"properties":{**job_view,"result":{"anyOf":[
        {"$ref":"#/components/schemas/StagedImportSummary"},{"$ref":"#/components/schemas/SpreadsheetImportPreview"}],
        "description":"A StagedImportSummary for upload_id or source_path, a SpreadsheetImportPreview for file_base64"}},
        "description":"An XLSX or ODS preparation job; poll GET /v1/jobs/{job} while status is running.",
        "example":{"job_id":"example-xlsx-upload","status":"running","completed":0,"phase":"Queued"}}
    schemas["ExportJob"]={"type":"object","required":["job_id","status","completed","phase"],"properties":{**job_view,"result":{"$ref":"#/components/schemas/ExportJobResult"}},
        "description":"A large export job; poll GET /v1/jobs/{job} while status is running, then download GET /v1/exports/{export}.",
        "example":{"job_id":"example-xlsx-export","status":"running","completed":0,"phase":"Queued"}}
    schemas["SpreadsheetExportResult"]={"type":"object","required":["file_base64","report","state"],"properties":{
        "file_base64":{"type":"string","format":"byte"},"report":lines,"scope_lines":scope,"report_entries":{"type":"array","maxItems":200,"items":entry},"state":{"type":"string"}},
        "description":"A bounded XLSX or ODS export: the package and its conversion report from one consistent state. At whole-workbook scope in formulas mode "
                       "every stored formula is written: XLSX writes one DataHorizon cannot evaluate as stored and counts it (unevaluable, by formula category; "
                       "formula.syntax approximated when its references cannot be read); ODS writes one that does not translate to OpenFormula as its current value "
                       "(formula.unsupported_syntax converted) or as an empty cell (omitted). Selection scope stays strict. values mode needs every cell current and "
                       "error-free, else CALCULATION_ERROR names the first 100 blocking cells (cells, blocked_count).",
        "example":{"file_base64":"UEsDBA==","report":[],"report_entries":[],"state":"copy-from-workbook-info"}}
    schemas["Receipt"]["properties"]["imported"]={"type":"object","required":["sheets","cells"],"properties":{"sheets":{"type":"integer","minimum":0},"cells":{"type":"integer","minimum":0}},
        "description":"workbook_import only: sheets and stored cells imported"}
    location={"type":"object","required":["sheet","row","col"],"properties":{"sheet":{"type":"string"},"row":row,"col":col}}
    def counted(description,**extra):
        return {"type":"object","required":["count","samples"],"description":description,"properties":{"count":{"type":"integer","minimum":0},
                "samples":{"type":"array","maxItems":100,"items":{**location,"properties":{**location["properties"],**extra}}}}}
    schemas["NativeInspection"]={"type":"object","required":["path","workbook_id","format_version","sheets","sqlite_integrity","readonly","inputs","report"],"properties":{
        "path":{"type":"string"},"workbook_id":{"type":"string"},"format_version":{"type":"integer"},"sheets":{"type":"array","items":{"type":"object"}},
        "sqlite_integrity":{"type":"string","enum":["ok"]},"readonly":{"type":"boolean","enum":[True]},"report":lines,
        "inputs":{"type":"object","required":["checked_cells","checked_bytes","complete","issue_count","issues","unevaluable_formulas","unmaintained_formulas","inputs_valid","state"],"properties":{
            "checked_cells":{"type":"integer","minimum":0},"checked_bytes":{"type":"integer","minimum":0},"complete":{"type":"boolean"},
            "stop_reason":{"type":"string","enum":["cell_limit","byte_limit","time_limit","storage_read_limit_or_error"]},
            "issue_count":{"type":"integer","minimum":0},"issues":{"type":"array","maxItems":100,"items":{"type":"object","required":["code","message"],"properties":{
                "sheet":{"type":"string"},"row":row,"col":col,"code":{"type":"string"},"message":{"type":"string"}}},
                "description":"Invalid stored inputs: a wrong type or style, or a formula cell whose source cannot even be scanned for references"},
            "unevaluable_formulas":counted("Valid formula inputs the calculator cannot evaluate but whose references the lenient scanner reads (as degraded imports store them); "
                                           "each sample names its formula_admission reason. Not issues",reason={"type":"string"}),
            "unmaintained_formulas":counted("Valid cells of kind unmaintained (references not maintained through a structural edit). Not issues"),
            "inputs_valid":{"type":"boolean","description":"True when inspection completed with no issue; unevaluable and unmaintained formulas do not make it false"},
            "state":{"type":"string"},"max_cells":{"type":"integer"},"max_bytes":{"type":"integer"},"formula_check":{"type":"string"}}}},
        "description":"A read-only inspection of a native file: integrity, catalog and a bounded check of stored inputs. Nothing is evaluated, repaired or run."}
    schemas["Error"]["properties"]["cells"]={"type":"array","maxItems":100,"items":{"type":"string"},"description":"CALCULATION_ERROR of a values export only: the first blocking cells, as Sheet!A1 (whole workbook) or A1 (selection)"}
    schemas["Error"]["properties"]["blocked_count"]={"type":"integer","minimum":1,"description":"CALCULATION_ERROR of a values export only: every cell without a current, error-free value"}
    schemas["Error"]["properties"]["conflicts"]={"type":"array","items":{"type":"string"},"description":"SHEET_NAME_CONFLICT only: imported sheet names that already exist"}
    schemas["ExportReleased"]={"type":"object","required":["released","export_id"],"additionalProperties":False,"properties":{"released":{"type":"boolean","enum":[True]},"export_id":export_id},
        "example":{"released":True,"export_id":"example-xlsx-export"}}
    paths = {}
    for path, (method, summary) in ROUTES.items():
        parameters = [{"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
                      for name in ("workbook", "operation", "job", "upload", "export") if "{" + name + "}" in path]
        for parameter in parameters:
            if parameter["name"]=="upload":parameter["schema"]=upload_id
            if parameter["name"]=="export":parameter["schema"]=export_id
        operation = {"summary": summary, "parameters": parameters,
                     "responses": {"200": {"description": "JSON result"}, **{str(status):{"description":"Structured error; inspect outcome and operation_id before deciding whether a mutation finished"} for status in (400,401,403,404,408,409,413,500,503)},
                                   "431":{"description":"HTTP parser rejected oversized headers before dispatch; no operation receipt is implied"}},
                     "security": [{"localToken": []}]}
        if method == "post":
            name = "JobAction" if path.startswith(("/v1/uploads/","/v1/exports/")) else "XlsxExportJobRequest" if path.endswith("/export/xlsx/jobs") else "OdsExportJobRequest" if path.endswith("/export/ods/jobs") else ("QueryRunRequest" if path.endswith('/queries/jobs') else "QueryPageRequest" if path.endswith('/page') else "JobAction") if '/queries/jobs' in path else "TableCatalogRequest" if path.endswith('/tables') else job_request_schema(path) if "/operation-jobs" in path else "ConversionRequest" if path=="/v1/convert/value" else "RecordRequest" if "/records/" in path else "MetadataRequest" if path.endswith(("/metadata/read","/records/read")) else ("CalculationRequest" if path.endswith("/calculation/jobs") else "JobAction") if "/calculation/jobs" in path else ("ChartJobRequest" if path.endswith("/charts/jobs") else "JobAction") if "/charts/jobs" in path else "ChartSuggestRequest" if path.endswith("/charts/suggest") else "ListExportRequest" if "/list/export/" in path else "ListValuesRequest" if path.endswith("/list/values") else "SnapshotRequest" if path == "/v1/recovery/inspect" else "XlsxPreparationRequest" if path=="/v1/import/xlsx/jobs" else "OdsPreparationRequest" if path=="/v1/import/ods/jobs" else "SpreadsheetExport" if path.endswith(("/export/xlsx","/export/ods")) else "PreparationRequest" if path == "/v1/import/csv/jobs" else "JobAction" if path.startswith("/v1/jobs/") else "OpenRequest" if path == "/v1/open" else "CsvPreviewRequest" if path == "/v1/import/csv/preview" else "RangeRequest" if path.endswith("/range") else "ClipboardRequest" if path.endswith("/clipboard") else "OperationRequest" if path.endswith("/operations") else {
                "code":"CodeReadRequest","names":"NamesReadRequest","charts":"ChartsReadRequest","inspect":"TableCatalogRequest","repair-preview":"RelationshipRepairRequest","cancel":"JobAction","edit-preview":"TableEditPreviewRequest","schema-preview":"TableSchemaPreviewRequest","read":"TableReadRequest","preview":"TablePreviewRequest","styles":"StylesRequest","navigate":"NavigationRequest","summary":"SummaryRequest","list":"ListRequest","find":"FindRequest","trace":"TraceRequest","snapshot":"SnapshotRequest","close":"CloseRequest","csv":"CsvExportRequest"}[path.rsplit("/",1)[-1]]
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
        elif "/charts" in path:
            response="ChartCatalog" if path.endswith("/charts") else "ChartSuggestion" if path.endswith("/suggest") else "JobReleased" if path.endswith("/release") else "ChartJob"
            operation["responses"]["200"]["content"]={"application/json":{"schema":{"$ref":"#/components/schemas/"+response}}}
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
        response={'/v1/import/xlsx/jobs':'SpreadsheetPreparationJob','/v1/import/ods/jobs':'SpreadsheetPreparationJob','/v1/jobs/{job}':'PreparationJob',
                  '/v1/jobs/{job}/cancel':'PreparationJob','/v1/jobs/{job}/release':'JobReleased','/v1/recovery/inspect':'NativeInspection'}.get(path)
        if path.endswith(('/export/xlsx/jobs','/export/ods/jobs')):response='ExportJob'
        if path.endswith(('/workbooks/{workbook}/export/xlsx','/workbooks/{workbook}/export/ods')):response='SpreadsheetExportResult'
        if response:operation['responses']['200']['content']={'application/json':{'schema':{'$ref':'#/components/schemas/'+response}}}
        if path.startswith('/v1/exports/') and method=='post':
            operation['description']="Deletes the export file and forgets its completed job. JOB_NOT_FOUND unless the export job has completed; JOB_CONFLICT while a download of it is in progress."
            operation['responses']['200']['content']={'application/json':{'schema':{'$ref':'#/components/schemas/ExportReleased'}}}
            operation['responses']['409']['description']='JOB_CONFLICT (a download is in progress)'
        if path=='/v1/exports/{export}':
            operation['description']=(f"Streams the file of a completed export job in 64-KiB chunks with Content-Length and Content-Disposition: attachment; filename=\"export.xlsx\" "
                f"(an XLSX export job) or filename=\"export.ods\" (an ODS export job), with that format's content type. "
                f"Origin and bearer-token checks apply as on every route. Downloading restarts the job's and the file's retention; it can be repeated until the export is released. "
                f"At most {MAX_DOWNLOADS} downloads are in progress at once (DOWNLOAD_BUSY). A download stops after {DOWNLOAD_SECONDS} seconds; a client that stops reading loses the connection.")
            operation['responses']['200']={'description':'The XLSX or ODS package','headers':{
                'Content-Length':{'schema':{'type':'integer','minimum':1}},'Content-Disposition':{'schema':{'type':'string','enum':['attachment; filename="export.xlsx"','attachment; filename="export.ods"']}}},
                'content':{XLSX_TYPE:{'schema':{'type':'string','format':'binary'}},ODS_TYPE:{'schema':{'type':'string','format':'binary'}}}}
            operation['responses']['404']['description']='JOB_NOT_FOUND: no completed export job has that ID (including while it is still running, and after release)'
            operation['responses']['409']['description']='DOWNLOAD_BUSY (two downloads in progress)'
        if path.endswith(('/export/xlsx/jobs','/export/ods/jobs')):
            operation['responses']['200']['description']='The job view (job_id, status, completed, phase, and result or error); poll GET /v1/jobs/{job} until it is not running'
            operation['responses']['400']['description']='INVALID_JOB, INVALID_ARGUMENT, or UNSUPPORTED for mode values or mixed'
            operation['responses']['409']['description']='JOB_CONFLICT (job_id used for different inputs), JOB_BUSY (another job running) or JOB_LIMIT (four stored jobs or exports)'
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
<label>Method<select id="method"><option>GET</option><option>POST</option><option>PUT</option></select></label>
<label>Path<input id="path" value="/v1/capabilities"></label>
<label>Body (JSON; raw bytes for PUT uploads)<textarea id="body" rows="8">{}</textarea></label><button id="run">Send request</button><pre id="result">Ready</pre>
<script>fetch('/openapi.json').then(r=>r.json()).then(api=>{let select=document.querySelector('#endpoint');
for(let [path,methods] of Object.entries(api.paths))for(let [method,op] of Object.entries(methods)){
let option=document.createElement('option');option.textContent=method.toUpperCase()+' '+path+' — '+op.summary;option.value=JSON.stringify([path,method,op]);select.append(option)}
select.onchange=()=>{let[path,method,op]=JSON.parse(select.value);document.querySelector('#path').value=path;document.querySelector('#method').value=method.toUpperCase();
let ref=op.requestBody?.content?.['application/json']?.schema?.['$ref'],schema=ref?api.components.schemas[ref.split('/').pop()]:{};
let variants=schema.oneOf?schema.oneOf.map(v=>api.components.schemas[v['$ref'].split('/').pop()]):[schema],examples=document.querySelector('#example');examples.replaceChildren();
for(let variant of variants){let option=document.createElement('option');option.textContent=variant.title||variant.example?.action||'Request';option.value=JSON.stringify(variant.example||{});examples.append(option)}
examples.parentElement.hidden=variants.length<2;examples.onchange=()=>{document.querySelector('#body').value=JSON.stringify(JSON.parse(examples.value),null,2)};examples.onchange();
let raw=op.requestBody?.content?.['application/octet-stream'];if(raw)document.querySelector('#body').value=raw.example??''};select.onchange()});
document.querySelector('#run').onclick=async()=>{let out=document.querySelector('#result');try{
let method=document.querySelector('#method').value,path=document.querySelector('#path').value;
if(!path.startsWith('/v1/')||path.startsWith('//'))throw Error('Use a /v1/ path');
let r=await fetch(path,{method,headers:{'Authorization':'Bearer '+document.querySelector('#token').value,'Content-Type':method==='PUT'?'application/octet-stream':'application/json'},body:method==='GET'?undefined:document.querySelector('#body').value});
let type=r.headers.get('Content-Type')||'';out.textContent=r.status+'\\n'+(type.startsWith('application/json')?JSON.stringify(await r.json(),null,2):type+', '+(await r.blob()).size+' bytes (save downloads with the SDK or CLI)')}catch(e){out.textContent=String(e)}};</script>"""


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
        # Staging directories of services that stopped without their orderly shutdown (killed, or on Windows terminated).
        sweep_terminated()
        self.jobs = PreparationJobs()
        # Uploads, import stages and exports; a supplied root lets tests inspect staged files.
        self.staging = Staging(staging_root)
        self._uploads, self._upload_lock = set(), threading.Lock()
        # In-progress downloads per export id. Lock order: jobs.lock, the upload lock, the download lock, the staging lock.
        self._downloads, self._download_lock = {}, threading.Lock()
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

    def _prepare_package(self, body, fmt):
        """Inline packages keep the bounded tree preview of format `fmt` (xlsx or ods); an upload or local file is read
        into a stage by that format's streaming reader."""
        inputs = [key for key in PACKAGE_INPUTS if key in body]
        require(len(inputs)==1,"INVALID_ARGUMENT","Supply exactly one of file_base64, upload_id or source_path")
        if inputs == ["file_base64"]:
            return self.jobs.start(body,xlsx_preview if fmt=="xlsx" else ods_preview,fmt)
        job_id = body.get("job_id")
        # The stage is named after the job, and staging identifiers are shorter than job IDs.
        require(isinstance(job_id,str) and IDENTIFIER.fullmatch(job_id),"INVALID_JOB",
                "With upload_id or source_path, job_id must be 1-64 ASCII letters, digits, underscores or hyphens")
        require(body.get("scope")=="workbook","INVALID_ARGUMENT","upload_id and source_path prepare whole workbooks; set scope to workbook")
        require(not {"row","col","sheet_index"} & set(body),"INVALID_ARGUMENT","row, col and sheet_index do not apply to a whole-workbook import")
        import_mode(body)
        require(body.get("timezone_policy","reject") in ("reject","utc"),"INVALID_ARGUMENT","Choose reject or utc timestamp policy")
        # One lock orders the retry check, stage reservation and job creation against expiry and release.
        # Expired jobs' on_discard callbacks can run while it is held; lock order: jobs.lock, then the
        # upload lock, then the staging lock (see PreparationJobs).
        with self.jobs.lock:
            try:
                self.jobs.access(job_id)
            except EngineError as error:
                if error.body["code"] != "JOB_NOT_FOUND": raise
            else:
                # A retry: the existing job (or JOB_CONFLICT); its stage already exists, so none is reserved.
                return self.jobs.start(body,self._stage_preparation(job_id,None,fmt),fmt,seconds=LARGE_PREPARATION_SECONDS)
            source = self._package_source(body)
            self.staging.new("stage",job_id)
            try:
                return self.jobs.start(body,self._stage_preparation(job_id,source,fmt),fmt,seconds=LARGE_PREPARATION_SECONDS,
                                       on_discard=lambda:self.staging.release("stage",job_id))
            except BaseException:
                self.staging.release("stage",job_id)
                raise

    def _package_source(self, body):
        """Validate the named package now so that request errors are immediate; return how to read it."""
        if "upload_id" in body:
            upload = body["upload_id"]
            require(isinstance(upload,str),"INVALID_ARGUMENT","upload_id must be text")
            with self._upload_lock:
                require(upload not in self._uploads,"JOB_CONFLICT","The upload is still in progress",409)
                path = self.staging.path("upload",upload)
            return {"kind":"upload","upload_id":upload,"file":path,"missing":("JOB_NOT_FOUND","The upload was released before it was read",404)}
        raw = body["source_path"]
        require(isinstance(raw,str) and os.path.isabs(raw),"INVALID_PATH","source_path must be an absolute path")
        try:
            # Symlinks are resolved once; the job reads the resolved file.
            resolved = Path(raw).resolve(strict=True)
            info = resolved.stat()
        except (OSError,RuntimeError,ValueError) as error:
            raise EngineError("INVALID_PATH","source_path does not name an existing file") from error
        require(stat.S_ISREG(info.st_mode),"INVALID_PATH","source_path must name a regular file")
        require(os.access(resolved,os.R_OK),"INVALID_PATH","source_path is not readable by the service")
        require(info.st_size<=LARGE_PACKAGE,"LIMIT",f"Spreadsheet package exceeds {LARGE_PACKAGE//(1024*1024)} MiB")
        return {"kind":"path","path":str(resolved),"size":info.st_size,"mtime_ns":info.st_mtime_ns,"file":resolved,
                "missing":("INVALID_PATH","source_path is no longer a readable file",400)}

    @staticmethod
    def _open_package(source):
        """Open the package once, without blocking (a FIFO swapped in after validation cannot hang the job) and
        without following a link at the resolved path; only a regular file matching what was validated is read."""
        flags = os.O_RDONLY|getattr(os,"O_NONBLOCK",0)|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_BINARY",0)
        try:
            descriptor = os.open(source["file"],flags)
        except OSError as error:
            raise EngineError(*source["missing"]) from error
        package = None
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode),*source["missing"])
            if source["kind"] == "path":
                require((info.st_size,info.st_mtime_ns) == (source["size"],source["mtime_ns"]),"STALE_PLAN",
                        "The source file changed after it was validated; prepare it again",409)
            package = open(descriptor,"rb")
            return package
        finally:
            if package is None: os.close(descriptor)

    def _stage_preparation(self, job_id, source, fmt):
        """The job body: read the package of format `fmt` into the reserved stage and return its review summary."""
        def prepare(request, checkpoint):
            require(source is not None,"JOB_NOT_FOUND","Preparation job expired while it was retried; start it again",404)
            touched = [time.monotonic()]
            def progress(completed, phase):
                checkpoint(completed, phase)
                # Keep the stage, and the upload being read, from expiring during a long preparation.
                if time.monotonic()-touched[0] >= 10:
                    touched[0] = time.monotonic()
                    self.staging.touch("stage",job_id)
                    if source["kind"] == "upload":
                        try: self.staging.touch("upload",source["upload_id"])
                        except EngineError: pass  # The client may release an upload that is open for reading.
            recorded = {key:value for key,value in source.items() if key not in ("file","missing")}
            # One open file: the bytes hashed and checked are the bytes parsed, whatever happens at the path.
            with self._open_package(source) as package:
                if source["kind"] == "upload":
                    digest = hashlib.sha256()
                    while chunk := package.read(1024*1024):
                        digest.update(chunk)
                        progress(0,"Checking upload")
                    recorded["sha256"] = digest.hexdigest()
                stage = ImportStage(self.staging.path("stage",job_id))
                # Looked up when the job runs, so the readers can be replaced in tests.
                reader,scope = (read_workbook,None) if fmt=="xlsx" else (ods_read_workbook,ods_stream.scope_lines(request))
                try:
                    report = reader(package,request,stage,progress)
                    if source["kind"] == "path":
                        info = os.fstat(package.fileno())
                        require((info.st_size,info.st_mtime_ns) == (source["size"],source["mtime_ns"]),"STALE_PLAN",
                                "The source file changed while it was prepared; prepare it again",409)
                    return stage.finish(report,{"format":fmt,"mode":import_mode(request),"source":recorded},scope=scope)
                finally:
                    stage.close()
        return prepare

    def _start_export(self, book, body, fmt):
        """A large export job of format `fmt` (xlsx or ods); the export file is reserved under the job id and owned by the job."""
        label = fmt.upper()
        job_id = body.get("job_id")
        require(isinstance(job_id,str) and IDENTIFIER.fullmatch(job_id),"INVALID_JOB",
                "job_id must be 1-64 ASCII letters, digits, underscores or hyphens; it also names the export")
        require(not set(body)-{"job_id","mode"},"INVALID_ARGUMENT",f"A large {label} export takes only job_id and mode")
        selected = body.get("mode","formulas")
        require(selected in ("formulas","mixed","values"),"INVALID_ARGUMENT","mode must be formulas")
        require(selected=="formulas","UNSUPPORTED",f"A large {label} export writes formulas only; export values with the bounded "
                f"POST /v1/workbooks/{{workbook}}/export/{fmt} route (at most 10,000 cells)")
        # The workbook is an input: a retry must come from the same workbook to find its job.
        request = {"job_id":job_id,"mode":"formulas","workbook":book.id,"path":str(book.path)}
        kind = EXPORT_KINDS[fmt]
        # As in _prepare_package, one lock orders the retry check, file reservation and job creation against expiry and release.
        with self.jobs.lock:
            try:
                self.jobs.access(job_id)
            except EngineError as error:
                if error.body["code"] != "JOB_NOT_FOUND": raise
            else:
                return self.jobs.start(request,self._export_job(book,job_id,fmt),kind,seconds=EXPORT_JOB_SECONDS)
            self.staging.new("export",job_id)
            try:
                return self.jobs.start(request,self._export_job(book,job_id,fmt),kind,seconds=EXPORT_JOB_SECONDS,
                                       on_discard=lambda:self.staging.release("export",job_id))
            except BaseException:
                self.staging.release("export",job_id)
                raise

    def _write_export(self, fmt, job_id, target, stream, report, progress):
        """Write the snapshot as format `fmt` into `target`; return the written summary and the fixed report lines.
        The ODS writer's body scratch file is a staging entry of the job, released whatever happens."""
        if fmt == "xlsx":
            written = write_workbook(target,stream,report,progress)
            return written,export_lines(stream.native,written["future_functions"],written["unevaluated_formulas"])
        def reserve():
            self.staging.release("part",job_id)  # Left over from a crash: the job's id is held by its export file.
            return self.staging.new("part",job_id)
        try:
            written = ods_write_workbook(target,stream,report,progress,scratch=reserve)
        finally:
            self.staging.release("part",job_id)
        return written,ods_stream.export_lines(stream.native,written["modern_functions"],written["formulas"],written["unevaluated_formulas"])

    def _export_job(self, book, job_id, fmt):
        """The job body: snapshot the workbook, write the package into the reserved file and return its summary."""
        def prepare(request, checkpoint):
            touched = [time.monotonic()]
            def progress(completed, phase):
                checkpoint(completed, phase)
                # Keep the file from expiring during a long export.
                if time.monotonic()-touched[0] >= 10:
                    touched[0] = time.monotonic()
                    self.staging.touch("export",job_id)
            target = self.staging.path("export",job_id)
            def reserve():
                # The job's id is held by its export file, so a snapshot under that id is left over from a crash.
                self.staging.release("snapshot",job_id)
                return self.staging.new("snapshot",job_id)
            checkpoint(0,"Reading workbook")
            try:
                with book.stream_workbook(reserve,checkpoint=progress) as stream:
                    report = ConversionReport()
                    written,lines = self._write_export(fmt,job_id,target,stream,report,progress)
            finally:
                self.staging.release("snapshot",job_id)
            return {"export_id":job_id,"bytes":written["bytes"],"sha256":written["sha256"],"state":stream.state,
                    "report":lines+report.lines(),"scope_lines":lines,
                    "report_entries":report.entries(),"totals":report.totals()}
        return prepare

    @contextmanager
    def download_slot(self, identifier):
        """Reserve one of MAX_DOWNLOADS in-progress downloads of a completed export; yield its open file, its size and
        (content type, file name) for the format the job wrote. The export's retention restarts."""
        require(isinstance(identifier,str) and IDENTIFIER.fullmatch(identifier),"INVALID_JOB","Identifier needs 1–64 ASCII letters, digits, _ or -")
        with self.jobs.lock:
            result = self.jobs.retain(identifier,tuple(DOWNLOADS))
            served = DOWNLOADS[self.jobs.entries[identifier]["kind"]]
            with self._download_lock:
                require(sum(self._downloads.values())<MAX_DOWNLOADS,"DOWNLOAD_BUSY",
                        f"At most {MAX_DOWNLOADS} downloads can be in progress; retry when one finishes",409)
                path = self.staging.path("export",identifier)
                file = open(os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_BINARY",0)),"rb")
                try:
                    size = os.fstat(file.fileno()).st_size
                    require(size==result["bytes"],"STORAGE_ERROR","The export file no longer matches its job; release it and export again",409)
                except BaseException:
                    file.close()
                    raise
                self._downloads[identifier] = self._downloads.get(identifier,0)+1
        try:
            yield file,size,served
        finally:
            file.close()
            with self._download_lock:
                remaining = self._downloads.pop(identifier)-1
                if remaining: self._downloads[identifier] = remaining

    def _release_export(self, identifier):
        require(IDENTIFIER.fullmatch(identifier),"INVALID_JOB","Identifier needs 1–64 ASCII letters, digits, _ or -")
        with self.jobs.lock:
            self.jobs.retain(identifier,tuple(DOWNLOADS))
            with self._download_lock:
                require(identifier not in self._downloads,"JOB_CONFLICT","A download of this export is in progress; retry when it finishes",409)
            # The job is forgotten without its discard callback, which this release replaces.
            self.jobs.consume(identifier)
            self.staging.release("export",identifier)
        return {"released":True,"export_id":identifier}

    def dispatch(self, method, path, body):
        if method=="POST" and path.startswith("/v1/uploads/") and path.endswith("/release") and path.count("/")==4:
            return self._release_upload(unquote(path.split("/")[3]))
        if method=="POST" and path.startswith("/v1/exports/") and path.endswith("/release") and path.count("/")==4:
            return self._release_export(unquote(path.split("/")[3]))
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
        if method == "POST" and path in ("/v1/import/xlsx/jobs","/v1/import/ods/jobs"):
            return self._prepare_package(body,path.split("/")[3])
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

    @staticmethod
    def _file_identity(path):
        info=os.stat(path)
        return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)

    def _stage_file(self, stage_id):
        """The stage's path for the engine and the identity of the file found there, or (None, None) when it is gone:
        then only a stored receipt can answer."""
        require(isinstance(stage_id,str),"INVALID_ARGUMENT","stage_id must be text")
        try:
            path=self.staging.path("stage",stage_id)
            return str(path),self._file_identity(path)
        except (EngineError,FileNotFoundError) as error:
            if isinstance(error,EngineError) and error.body["code"] != "JOB_NOT_FOUND": raise
            return None,None

    def _consume_stage(self, stage_id, path, identity):
        """Called only after this request committed the import: the stage it read is spent, so its job is forgotten
        without its discard callback and the file is deleted. Stage ids are client-chosen job ids, so nothing is
        released unless the file is still the one that was read and no preparation job is running under that id.
        The import has committed, so a failure to delete is logged and left to the staging retention sweep."""
        try:
            with self.jobs.lock:
                # Stat without staging.path, which would touch the file and change its identity.
                try: current=self._file_identity(path)
                except FileNotFoundError: return
                if current!=identity: return
                if self.jobs.consume(stage_id) or stage_id not in self.jobs.entries:
                    self.staging.release("stage",stage_id)
        except OSError:
            print("Applied import stage could not be released:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

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
        if method == "POST" and suffix == ["charts"]:
            from .charts import read as read_charts
            return read_charts(book,body)
        if method == "POST" and suffix == ["charts","suggest"]:
            from .chart_resolve import suggest
            with book.lock:
                book.refresh();return suggest(book,body)
        if method == "POST" and suffix == ["charts","jobs"]:
            return book.chart_jobs().start(body)
        if len(suffix) in (3,4) and suffix[:2] == ["charts","jobs"]:
            action=suffix[3] if len(suffix)==4 else None
            require((method=='GET' and action is None) or (method=='POST' and action in ('cancel','release')),'NOT_FOUND','Unknown chart job action',404)
            return book.chart_jobs().access(suffix[2],action)
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
            staged=body.get("action")=="workbook_import" and "stage_id" in body
            try:
                # Refusals before the engine is entered name the operation and are rejected: nothing was admitted.
                require(STAGE_PATH not in body,"INVALID_ARGUMENT",f"{STAGE_PATH} is set by the service; name a stage with stage_id")
                if staged:
                    path,identity=self._stage_file(body["stage_id"])
                    body={**body,STAGE_PATH:path}
            except EngineError as error:
                error.body.update(outcome="rejected",operation_id=operation)
                raise
            key=(book,operation)
            with self.registry_lock:
                require(key not in self.active_operations,'OPERATION_RUNNING','Operation is active; inspect its receipt before retrying',409)
                event=threading.Event();self.active_operations[key]=event
            fresh=[]
            try:receipt=book.mutate(body,cancel_event=event,on_commit=lambda:fresh.append(True))
            finally:
                with self.registry_lock:self.active_operations.pop(key,None)
            # A replayed receipt never releases: the id may now name another preparation's stage.
            if staged and fresh:self._consume_stage(body["stage_id"],path,identity)
            return receipt
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
        if method == "POST" and len(suffix) == 3 and suffix[0] == "export" and suffix[1] in LARGE_FORMATS and suffix[2] == "jobs":
            return self._start_export(book,body,suffix[1])
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

    def send_export(self, path):
        """Stream a completed export's file. Errors before the headers are JSON responses; afterwards a failed or
        abandoned transfer only closes the connection, and the file is always closed."""
        identifier=unquote(path[len("/v1/exports/"):])
        stem,_,action=identifier.rpartition("/")
        require(not (action=="release" and IDENTIFIER.fullmatch(stem)),"METHOD_NOT_ALLOWED","Release an export with POST",405)
        with self.server.download_slot(identifier) as (file,size,(content_type,filename)):
            deadline=time.monotonic()+DOWNLOAD_SECONDS
            try:
                self.connection.settimeout(30)
                self.send_response(200)
                self.send_header("Content-Type",content_type)
                self.send_header("Content-Length",str(size))
                self.send_header("Content-Disposition",f'attachment; filename="{filename}"')
                self.send_header("Cache-Control","no-store")
                self.send_header("X-Content-Type-Options","nosniff")
                self.send_header("X-Frame-Options","DENY")
                self.end_headers()
                while chunk:=file.read(DOWNLOAD_CHUNK):
                    remaining=deadline-time.monotonic()
                    if remaining<=0: raise TimeoutError("Download deadline exceeded")
                    self.connection.settimeout(min(30,remaining))
                    self.wfile.write(chunk)
            except OSError:
                # Disconnect, reset or timeout (all OSError): the response cannot be completed.
                self.close_connection=True

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
            if self.command == "GET" and path.startswith("/v1/exports/"):
                return self.send_export(path)
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


STOP_SIGNALS = ("SIGTERM", "SIGINT", "SIGBREAK")  # SIGBREAK: a Windows console's Ctrl+Break


def stop_on_signals():
    """Make a stop signal end serve_forever as Ctrl+C does, so main's orderly shutdown runs (jobs cancelled and closed,
    staging removed, workbooks closed). Once one arrives, further stop signals are ignored so they cannot interrupt
    that shutdown. On Windows, Popen.terminate and QProcess::kill end the process without a signal: its staging
    directory is then removed by the next service's sweep_terminated."""
    import signal
    present = [getattr(signal, name) for name in STOP_SIGNALS if hasattr(signal, name)]
    def stop(signum, frame):
        for number in present:
            try: signal.signal(number, signal.SIG_IGN)
            except (OSError, ValueError): pass
        raise KeyboardInterrupt
    for number in present:
        try: signal.signal(number, stop)
        except (OSError, ValueError): pass  # not available in this process or platform


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
        stop_on_signals()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
