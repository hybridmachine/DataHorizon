# SPDX-License-Identifier: GPL-3.0-only
"""Thin Python REST SDK. It never opens a workbook database."""

import json
from http.client import HTTPException
from collections import deque
from pathlib import Path
import queue
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.parse import quote
import uuid


class APIError(Exception):
    def __init__(self, status, body):
        super().__init__(body.get("message", str(body)))
        self.status, self.body = status, body


class UnknownOutcome(RuntimeError):
    def __init__(self, operation_id, message="Lost acknowledgement; look up this ID before sending another mutation"):
        super().__init__(message)
        self.operation_id=operation_id
        self.body={"code":"OUTCOME_UNKNOWN","message":message,"outcome":"unknown","operation_id":operation_id}


class CleanupError(RuntimeError):
    """Context cleanup failed; this does not reject earlier data operations."""

    def __init__(self, error, receipt=None):
        super().__init__(str(error))
        self.body={"code":"CLEANUP_FAILED","phase":"cleanup","message":str(error)}
        if receipt is not None:
            self.body["last_operation"]=dict(receipt)


class MetadataRefreshError(RuntimeError):
    """The operation outcome is known; workbook metadata still needs refresh."""

    def __init__(self, receipt, error):
        super().__init__("Confirmed operation outcome; workbook metadata refresh is required: "+str(error))
        self.receipt=dict(receipt)
        self.operation_id=receipt["operation_id"]
        self.body={"code":"REFRESH_FAILED","phase":"metadata_refresh","message":str(self),
                   "operation_id":self.operation_id,"outcome":receipt["outcome"],"receipt":self.receipt}


def _exit_context(owner, error):
    try:
        owner.close()
    except Exception as cleanup:
        if isinstance(cleanup,UnknownOutcome):
            if error is None:
                raise
            if isinstance(error,UnknownOutcome) and error.operation_id==cleanup.operation_id:
                return False
        failure=CleanupError(cleanup,getattr(owner,"_last_receipt",None))
        if error is None:
            raise failure from cleanup
        # Preserve the original exception and durable receipt, including its ID.
        error.add_note("Context cleanup also failed: "+str(cleanup))
        error.cleanup_errors=[*getattr(error,"cleanup_errors",[]),failure.body]
    return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class Client:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token
        # Local bearer credentials and workbook payloads must stay on the
        # configured connection, independently of ambient proxy settings.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, path, body=None):
        data = json.dumps(body, allow_nan=False).encode() if body is not None else None
        request = Request(self.url + path, data=data,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                if 300 <= error.code < 400:
                    raise ValueError("Service redirects are not allowed") from error
                body = json.load(error)
            finally:
                error.close()
            if not isinstance(body, dict):
                raise ValueError("Invalid JSON error response")
            raise APIError(error.code, body) from error

    def functions(self):
        return self.request("/v1/functions")

    def inspect_native(self, path):
        return self.request("/v1/recovery/inspect", {"path":str(Path(path).expanduser().resolve())})

    def convert_value(self,source,kind,locale='invariant',timezone_policy='reject'):
        return self.request('/v1/convert/value',dict(source=source,kind=kind,locale=locale,timezone_policy=timezone_policy))

    def prepare_csv(self, request, job_id=None):
        return self.request("/v1/import/csv/jobs", {**request,"job_id":job_id or str(uuid.uuid4())})

    def prepare_spreadsheet(self, data, *, format="xlsx", sheet_index=1, mode="formulas", scope="selection", job_id=None, timezone_policy="reject"):
        import base64
        if format not in ("xlsx","ods"): raise ValueError("Unsupported spreadsheet format")
        return self.request("/v1/import/"+format+"/jobs", {"file_base64":base64.b64encode(data).decode(),
            "job_id":job_id or str(uuid.uuid4()),"sheet_index":sheet_index,"mode":mode,"scope":scope,"timezone_policy":timezone_policy})

    def preparation(self, job_id, action=None):
        path = "/v1/jobs/"+quote(job_id,safe="")
        return self.request(path+"/"+action, {}) if action in ("cancel","release") else self.request(path)

    def open(self, path, create=False, readonly=False,repair=False):
        info = self.request("/v1/open", {"path": str(Path(path).expanduser().resolve()), "create": create, "readonly": readonly,**({"repair":True} if repair else {})})
        return WorkbookClient(self, info)


class WorkbookClient:
    def __init__(self, client, info):
        self.client, self.info = client, info
        self.base = "/v1/workbooks/" + info["id"]
        self.state = info["state"]
        self.pending_operation = None
        self._last_receipt = None
        self._refresh_receipt = None
        self._closed = False

    def _ensure_open(self):
        if self._closed:
            raise ValueError("Workbook client is closed; request was not sent. Open a new client handle.")

    def refresh(self):
        self._ensure_open()
        info = self.client.request(self.base)
        if (not isinstance(info,dict) or info.get("id")!=self.info["id"]
                or not isinstance(info.get("state"),str) or not info["state"]):
            raise ValueError("Invalid workbook metadata response")
        self.info, self.state = info, info["state"]
        if self._refresh_receipt is not None:
            self.pending_operation=None
            self._refresh_receipt=None
        return self.info

    def read(self, sheet="Sheet1", r1=1, c1=1, r2=32, c2=16, *, calculation='fresh'):
        self._ensure_open()
        return self.client.request(self.base + "/range", {"sheet": sheet, "r1": r1, "c1": c1, "r2": r2, "c2": c2,'calculation':calculation})

    def read_styles(self,sheet="Sheet1",r1=1,c1=1,r2=1,c2=1):
        self._ensure_open()
        return self.client.request(self.base+"/styles",dict(sheet=sheet,r1=r1,c1=c1,r2=r2,c2=c2,expected_state=self.state))

    def calculate(self,sheet='Sheet1',job_id=None,**scope):
        self._ensure_open()
        return self.client.request(self.base+'/calculation/jobs',dict(sheet=sheet,job_id=job_id or str(uuid.uuid4()),expected_state=self.state,**scope))

    def create_job(self,action='edit',sheet='Sheet1',job_id=None,**options):
        self._ensure_open()
        return self.client.request(self.base+'/operation-jobs',dict(action=action,sheet=sheet,job_id=job_id or str(uuid.uuid4()),expected_state=self.state,**options))

    def operation_job(self,job_id,action='status',**arguments):
        self._ensure_open()
        if action not in ('status','append','seal','start','resume','cancel','release'):raise ValueError('Unknown job action')
        if action in ('start','resume') and self.pending_operation:
            if self._refresh_receipt is not None:raise MetadataRefreshError(self._refresh_receipt,'Refresh before executing a job')
            raise UnknownOutcome(self.pending_operation)
        # Job IDs survive transport errors; never automatically repeat start/resume.
        return self.client.request(self.base+'/operation-jobs/'+quote(job_id,safe='')+'/'+action,arguments)

    def declare_list(self,identifier,sheet='Sheet1',**spec):
        return self.apply('list_declare',sheet,id=identifier,spec=spec)

    def code(self,module=None):
        self._ensure_open()
        return self.client.request(self.base+'/code',dict(expected_state=self.state,**(dict(module=module) if module is not None else {})))

    def save_module(self,name,source,macros,*,fingerprint,module=None,operation_id=None):
        return self.apply('module_save',name=name,source=source,macros=macros,fingerprint=fingerprint,operation_id=operation_id,**(dict(module=module) if module is not None else {}))

    def delete_module(self,module,*,fingerprint,operation_id=None):
        return self.apply('module_delete',module=module,fingerprint=fingerprint,operation_id=operation_id)

    def names(self,name=None):
        self._ensure_open()
        return self.client.request(self.base+'/names',dict(expected_state=self.state,**(dict(name=name) if name is not None else {})))

    def save_name(self,name,reference,*,name_id=None,operation_id=None):
        return self.apply('name_save',name=name,reference=reference,operation_id=operation_id,**(dict(name_id=name_id) if name_id is not None else {}))

    def delete_name(self,name_id,*,operation_id=None):
        return self.apply('name_delete',name_id=name_id,operation_id=operation_id)

    def save_binding(self,event,scope,module,function,*,fingerprint,required=True,binding_id=None,operation_id=None):
        return self.apply('binding_save',event=event,scope=scope,module=module,function=function,required=required,fingerprint=fingerprint,operation_id=operation_id,**(dict(binding_id=binding_id) if binding_id is not None else {}))

    def delete_binding(self,binding_id,*,fingerprint,operation_id=None):
        return self.apply('binding_delete',binding_id=binding_id,fingerprint=fingerprint,operation_id=operation_id)

    def run_macro(self,module,macro,*,fingerprint,arguments=None,operation_id=None):
        return self.apply('macro_run',module=module,macro=macro,fingerprint=fingerprint,arguments={} if arguments is None else arguments,operation_id=operation_id,label='Macro '+module+'.'+macro)

    def tables(self):
        self._ensure_open()
        return self.client.request(self.base+'/tables',dict(expected_state=self.state))

    def create_table(self,name,columns):
        return self.apply('table_create',name=name,columns=columns)

    def drop_table(self,table,*,allow_data_loss=False):
        return self.apply('table_drop',table=table,allow_data_loss=allow_data_loss)

    def preview_table_schema(self,table,columns,**options):
        self._ensure_open()
        return self.client.request(self.base+'/tables/schema-preview',dict(table=table,columns=columns,expected_state=self.state,**options))

    def alter_table(self,preview):
        if preview.get('can_apply') is not True:raise ValueError('Correct schema conversion issues before Apply')
        return self.apply('table_schema',**preview['source_request'],review_digest=preview['review_digest'],expected_state=preview['state'])

    def preview_table(self,name,sheet='Sheet1',**scope):
        self._ensure_open()
        return self.client.request(self.base+'/tables/preview',dict(name=name,sheet=sheet,expected_state=self.state,**scope))

    def promote_table(self,preview):
        if preview.get('can_create') is not True:raise ValueError('Correct table conversion issues before Apply')
        source=dict(preview['source_request']);sheet=source.pop('sheet')
        return self.apply('table_promote',sheet,**source,columns=preview['columns'],preview_clock=preview['preview_clock'],
                          review_digest=preview['review_digest'],expected_state=preview['state'])

    def read_table(self,table,**scope):
        self._ensure_open()
        return self.client.request(self.base+'/tables/read',dict(table=table,expected_state=self.state,**scope))

    def create_relationship(self,name,parent_table,parent_column,child_table,child_column,**rules):
        return self.apply('relationship_create',name=name,parent_table=parent_table,parent_column=parent_column,child_table=child_table,child_column=child_column,**rules)

    def drop_relationship(self,relationship):return self.apply('relationship_drop',relationship=relationship)

    def inspect_relationships(self):
        self._ensure_open()
        return self.client.request(self.base+'/relationships/inspect',dict(expected_state=self.state))

    def preview_orphan_repair(self,relationship,mode,**targets):
        self._ensure_open()
        return self.client.request(self.base+'/relationships/repair-preview',dict(relationship=relationship,mode=mode,expected_state=self.state,**targets))

    def apply_orphan_repair(self,preview):
        operation=dict(preview['operation']);operation.pop('action',None)
        return self.apply('table_repair',**operation,review_digest=preview['review_digest'],expected_state=preview['state'])

    def create_index(self,table,name,columns,*,unique=False):
        return self.apply('index_create',table=table,name=name,columns=columns,unique=unique)

    def drop_index(self,index):return self.apply('index_drop',index=index)

    def cancel_operation(self,operation):
        self._ensure_open()
        return self.client.request(self.base+'/operations/'+quote(operation,safe='')+'/cancel',{})

    def insert_records(self,table,rows):
        return self.apply('table_insert',table=table,rows=rows,label='Insert records')

    def update_records(self,table,edits):
        return self.apply('table_update',table=table,edits=edits,label='Edit records')

    def delete_records(self,table,record_ids):
        return self.apply('table_delete',table=table,record_ids=record_ids,label='Delete records')

    def preview_record_edits(self,table,action,**options):
        self._ensure_open()
        return self.client.request(self.base+'/tables/edit-preview',dict(table=table,action=action,expected_state=self.state,**options))

    def apply_record_preview(self,preview):
        if preview.get('can_apply') is not True:raise ValueError('Correct record conversion errors before Apply')
        request=dict(preview['operation']);action=request.pop('action')
        return self.apply(action,**request,expected_state=preview['state'],review_digest=preview['review_digest'])

    def records(self,identifier,sheet='Sheet1',**criteria):
        self._ensure_open()
        return self.client.request(self.base+'/records/read',dict(id=identifier,sheet=sheet,expected_state=self.state,**criteria))

    def copy_records(self,identifier,sheet='Sheet1',**criteria):
        self._ensure_open()
        return self.client.request(self.base+'/records/copy',dict(id=identifier,sheet=sheet,expected_state=self.state,**criteria))

    def edit_records(self,identifier,record_ids,sheet='Sheet1',**arguments):
        return self.apply('list_edit',sheet,id=identifier,record_ids=record_ids,**arguments)

    def export_records(self,identifier,format='xlsx',sheet='Sheet1',**criteria):
        self._ensure_open()
        if format not in ('xlsx','ods'):raise ValueError('Choose XLSX or ODS')
        return self.client.request(self.base+'/records/export/'+format,dict(id=identifier,sheet=sheet,expected_state=self.state,**criteria))

    def reconcile_list(self,identifier,mode,sheet='Sheet1'):
        return self.apply('list_reconcile',sheet,id=identifier,mode=mode)

    def metadata(self,sheet='Sheet1'):
        self._ensure_open()
        return self.client.request(self.base+'/metadata/read',dict(sheet=sheet,expected_state=self.state))

    def set_metadata(self,namespace,identifier,payload,sheet='Sheet1'):
        return self.apply('metadata_set',sheet,namespace=namespace,id=identifier,payload=payload)

    def calculation(self,job_id,action=None):
        self._ensure_open()
        if action not in (None,'cancel','release'):raise ValueError('Unknown calculation action')
        return self.client.request(self.base+'/calculation/jobs/'+quote(job_id,safe='')+('/'+action if action else ''),{} if action else None)

    def run_query(self,sql,*,parameters=None,values=None,job_id=None):
        self._ensure_open()
        return self.client.request(self.base+'/queries/jobs',dict(sql=sql,parameters=parameters or [],values=values or {},job_id=job_id or str(uuid.uuid4()),expected_state=self.state))

    def preview_query(self,name,sql,*,parameters=None,defaults=None,query=None):
        self._ensure_open()
        return self.client.request(self.base+'/queries/preview',dict(name=name,sql=sql,parameters=parameters or [],defaults=defaults or {},expected_state=self.state,**({'query':query} if query else {})))

    def save_query(self,preview):return self.apply('query_save',**preview['source_request'],review_digest=preview['review_digest'],expected_state=preview['state'])

    def query_definition(self,query):
        self._ensure_open()
        return self.client.request(self.base+'/queries/read',dict(query=query,expected_state=self.state))

    def rename_query(self,query,name):return self.apply('query_rename',query=query,name=name)

    def delete_query(self,query):return self.apply('query_delete',query=query)

    def run_saved_query(self,query,*,values=None,job_id=None):
        self._ensure_open()
        return self.client.request(self.base+'/queries/jobs',dict(query=query,values=values or {},job_id=job_id or str(uuid.uuid4()),expected_state=self.state))

    def export_table(self,table,format='csv',**scope):
        self._ensure_open()
        if format not in ('csv','xlsx','ods'):raise ValueError('Choose CSV, XLSX or ODS')
        return self.client.request(self.base+'/tables/export/'+format,dict(table=table,expected_state=self.state,**scope))

    def export_query(self,job_id,format='csv',**scope):
        self._ensure_open()
        if format not in ('csv','xlsx','ods'):raise ValueError('Choose CSV, XLSX or ODS')
        return self.client.request(self.base+'/queries/jobs/'+quote(job_id,safe='')+'/export/'+format,dict(expected_state=self.state,**scope))

    def publish_export(self,snapshot,path,*,overwrite=False):
        from .publication import publish
        def current():
            self.refresh()
            if self.state!=snapshot.get('state'):raise ValueError('Stored inputs changed; review a fresh export before publication')
        return publish(snapshot,path,self.info['path'],current=current,overwrite=overwrite)

    def query(self,job_id,action=None,**page):
        self._ensure_open()
        if action not in (None,'page','cancel','release'):raise ValueError('Unknown query action')
        body=dict(expected_state=self.state,**page) if action=='page' else {} if action else None
        return self.client.request(self.base+'/queries/jobs/'+quote(job_id,safe='')+('/'+action if action else ''),body)

    def trace(self,row,col,direction="precedents",sheet="Sheet1"):
        self._ensure_open()
        return self.client.request(self.base+"/trace",dict(sheet=sheet,row=row,col=col,direction=direction,expected_state=self.state))

    def find(self,needle,sheet="Sheet1",**scope):
        self._ensure_open()
        return self.client.request(self.base+"/find",{"sheet":sheet,"needle":needle,"expected_state":self.state,**scope})

    def export_list(self,format="csv",sheet="Sheet1",**criteria):
        self._ensure_open()
        if format not in ("csv","xlsx","ods"):raise ValueError("Unsupported list export format")
        return self.client.request(self.base+"/list/export/"+format,{"sheet":sheet,"expected_state":self.state,**criteria})

    def list_values(self,sheet="Sheet1",**criteria):
        self._ensure_open()
        return self.client.request(self.base+"/list/values",{"sheet":sheet,"expected_state":self.state,**criteria})

    def navigate(self,row,col,direction,sheet="Sheet1",expected_state=None):
        self._ensure_open()
        return self.client.request(self.base+"/navigate",{"sheet":sheet,"row":row,"col":col,"direction":direction,"expected_state":self.state if expected_state is None else expected_state})

    def summary(self, sheet="Sheet1", **scope):
        self._ensure_open()
        return self.client.request(self.base+"/summary",{"sheet":sheet,**scope})

    def export_spreadsheet(self, format="xlsx", **scope):
        self._ensure_open()
        if format not in ("xlsx","ods"): raise ValueError("Unsupported spreadsheet format")
        return self.client.request(self.base+"/export/"+format,scope)

    def import_prepared(self, preview, sheet="Sheet1"):
        if preview.get("can_import") is not True:
            raise ValueError("Correct import issues and review again before Apply")
        if preview.get("scope")=="workbook":
            return self.apply("workbook_import",sheets=preview["workbook_sheets"],label="Import workbook")
        return self.apply("edit",sheet,cells=preview["cells"],label="Import spreadsheet")

    def apply(self, action, sheet="Sheet1", operation_id=None, **arguments):
        self._ensure_open()
        if self.pending_operation:
            if self._refresh_receipt is not None:
                raise MetadataRefreshError(self._refresh_receipt,"Refresh before sending another mutation")
            raise UnknownOutcome(self.pending_operation)
        if self.info.get("readonly"):
            raise APIError(403,{"code":"READ_ONLY","message":"Workbook is open read-only; request was not sent","outcome":"rejected"})
        operation=str(uuid.uuid4()) if operation_id is None else operation_id
        if not isinstance(operation,str) or not operation or len(operation)>100:
            raise ValueError("operation_id must be 1-100 characters of text; request was not sent")
        try:
            operation.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("operation_id must be valid Unicode text; request was not sent") from error
        request = {"action": action, "sheet": sheet, "operation_id": operation,
                   "expected_state": self.state, **arguments}
        # Invalid local input has no unknown network outcome.
        if len(json.dumps(request,allow_nan=False).encode())>4*1024*1024:
            raise ValueError("Request exceeds the 4 MiB limit; request was not sent")
        self.pending_operation=request["operation_id"]
        try:
            response = self.client.request(self.base + "/operations", request)
            self.validate_receipt(response, request["operation_id"], committed=True)
        except APIError as error:
            if (error.body.get("outcome") in ("rejected","rolled_back")
                    and isinstance(error.body.get("code"), str) and error.body["code"]
                    and isinstance(error.body.get("message"), str)
                    and error.body.get("operation_id") == request["operation_id"]):
                self.pending_operation=None
                self._last_receipt=dict(error.body)
                raise
            raise UnknownOutcome(request["operation_id"],str(error)) from error
        except (OSError, ValueError, HTTPException) as error:
            raise UnknownOutcome(request["operation_id"],str(error)) from error
        self.pending_operation=None
        self.state = response["state"]
        self._last_receipt=dict(response)
        return response

    def set_localized(self,row,col,source,kind,locale,sheet='Sheet1'):
        converted=self.client.convert_value(source,kind,locale)
        return self.set(row,col,converted['cell']['source'],sheet,converted['cell']['kind'])

    def set(self, row, col, source, sheet="Sheet1", kind="auto"):
        """Set an explicit kind; date/time/datetime require strict civil ISO strings."""
        return self.apply("edit", sheet, cells=[{"row": row, "col": col, "source": str(source), "kind": kind}])

    def outcome(self, operation_id):
        self._ensure_open()
        if operation_id==self.pending_operation and self._refresh_receipt is not None:
            receipt=dict(self._refresh_receipt)
        else:
            try:
                receipt=self.client.request(self.base + "/operations/" + quote(operation_id, safe=""))
                self.validate_receipt(receipt, operation_id)
            except (APIError, OSError, ValueError, HTTPException) as error:
                raise UnknownOutcome(operation_id, str(error)) from error
        self._last_receipt=dict(receipt)
        if operation_id==self.pending_operation and receipt.get("outcome") in ("committed","rejected","rolled_back"):
            self._refresh_receipt=dict(receipt)
            try:
                self.refresh()
            except Exception as error:
                raise MetadataRefreshError(receipt,error) from error
        return receipt

    @staticmethod
    def validate_receipt(receipt, operation_id, committed=False):
        if not isinstance(receipt, dict) or receipt.get("operation_id") != operation_id:
            raise ValueError("Invalid operation receipt or mismatched operation ID")
        if receipt.get("outcome") == "committed":
            if not isinstance(receipt.get("state"), str) or not receipt["state"] or type(receipt.get("changed_cells")) is not int or receipt["changed_cells"] < 0:
                raise ValueError("Invalid committed operation receipt")
        elif committed or receipt.get("outcome") not in ("rejected", "rolled_back") or not isinstance(receipt.get("code"), str) or not receipt["code"] or not isinstance(receipt.get("message"), str):
            raise ValueError("Invalid operation outcome")

    def close(self):
        if self._closed:
            return {"closed":True}
        if self.pending_operation and self._refresh_receipt is None:
            raise UnknownOutcome(self.pending_operation)
        response=self.client.request(self.base + "/close", {})
        if not isinstance(response,dict) or response.get("closed") is not True:
            raise ValueError("Invalid workbook close acknowledgement")
        self.pending_operation=None
        self._refresh_receipt=None
        self._closed=True
        return response

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, _type, error, _traceback):
        return _exit_context(self,error)


class LocalService:
    """Owns only the child service it started; external services are never stopped."""

    def __init__(self, handler=None, *, approved_code=(), owner_channel=False):
        command = [sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve().with_name("launch.py"))]
        from .code_approval import validate_approvals
        for key in sorted(validate_approvals(approved_code)):command += ["--approve-code",key]
        if handler:
            command += ["--handler", str(handler)]
        self.owner_channel = bool(owner_channel)
        if self.owner_channel:command.append("--owner-channel")
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE if self.owner_channel else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        self.diagnostics = deque(maxlen=40)
        self._diagnostics_thread=threading.Thread(target=lambda:self.diagnostics.extend(iter(self.process.stderr.readline,"")),daemon=True)
        self._diagnostics_thread.start()
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(self.process.stdout.readline()), daemon=True).start()
        try:
            info = json.loads(ready.get(timeout=10))
            self.client = Client(info["url"], info["token"])
        except Exception:
            self.close()
            raise RuntimeError("Local service failed to start; inspect its diagnostics and configured Python interpreter") from None

    def approve(self, key):
        """Owner-only grant: the single private line to the child's standard input."""
        from .code_approval import validate_approvals
        if not self.owner_channel:raise RuntimeError("This service was started without an owner channel")
        validate_approvals([key])
        if self.process.poll() is not None or self.process.stdin is None or self.process.stdin.closed:raise RuntimeError("The service is not running")
        try:
            self.process.stdin.write(json.dumps({"approve":key})+"\n");self.process.stdin.flush()
        except (OSError,ValueError):raise RuntimeError("The service is not running") from None

    def close(self):
        if self.owner_channel and self.process.stdin is not None:
            try:self.process.stdin.close()
            except (OSError,ValueError):pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self._diagnostics_thread.join(timeout=1)
        self.process.stderr.close()

    def __enter__(self):
        return self.client

    def __exit__(self, _type, error, _traceback):
        return _exit_context(self,error)
