# SPDX-License-Identifier: GPL-3.0-only
"""Thin Python REST SDK. It never opens a workbook database."""

import hashlib
import json
from http.client import HTTPException
from collections import deque
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
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


class IntegrityError(ValueError):
    """Bytes sent or received do not match what the service reported."""

    def __init__(self, message, **identifiers):
        super().__init__(message)
        self.body = {"code": "INTEGRITY_MISMATCH", "message": message, "outcome": "rejected", **identifiers}


# Ordinary requests keep a short timeout; transfers and a staged import's apply (up to 300 seconds) get their own.
REQUEST_SECONDS = 15
TRANSFER_SECONDS = 330
APPLY_SECONDS = 330
JOB_WAIT_SECONDS = 330
DOWNLOAD_CHUNK = 64 * 1024


def report_scope_lines(result):
    """The fixed scope statements of a preview, review summary or export result, which precede its one line per report
    entry: the service's scope_lines, or for an older service that omits them, the report lines before those entry lines."""
    if isinstance(result.get("scope_lines"), list): return list(result["scope_lines"])
    report, entries = result.get("report") or [], result.get("report_entries") or []
    return list(report[:len(report)-len(entries)])


def spreadsheet_format(path):
    """xlsx or ods from a file name's extension."""
    suffix = Path(path).suffix.lower()
    if suffix not in (".xlsx", ".ods"): raise ValueError("Use a .xlsx or .ods file name, or name the format explicitly")
    return suffix[1:]


def _check_destination(target, overwrite, native=None):
    if native is not None:
        source = Path(native).expanduser().absolute()
        if target == source or target.exists() and source.exists() and os.path.samefile(target, source):
            raise ValueError("Export destination identifies the native workbook")
    # lexists: a dangling symlink is an existing destination too.
    if os.path.lexists(target) and not overwrite:
        raise FileExistsError(f"Destination {target} exists; explicitly enable overwrite")


def _new_file_mode():
    mask = os.umask(0o022); os.umask(mask)
    return 0o666 & ~mask


def publish_file(temporary, target, overwrite):
    """Move a complete temporary sibling file into place: replacing (keeping the existing file's permissions) only when
    overwrite, otherwise with an atomic no-clobber link. The temporary file is left for the caller to remove."""
    target = Path(target)
    try: mode = os.stat(target).st_mode & 0o7777 if overwrite else None
    except FileNotFoundError: mode = None
    os.chmod(temporary, _new_file_mode() if mode is None else mode)
    if overwrite: os.replace(temporary, target)
    else:
        # Atomic no-clobber publication; a concurrent creator wins safely.
        try: os.link(temporary, target)
        except FileExistsError: raise FileExistsError(f"Destination {target} exists; explicitly enable overwrite") from None
        os.unlink(temporary)


def write_file(path, data, *, overwrite=False):
    """Write bytes to `path` through a temporary sibling, published as publish_file does."""
    target = Path(path).expanduser().absolute()
    _check_destination(target, overwrite)
    descriptor, temporary = tempfile.mkstemp(prefix=".datahorizon-write-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        publish_file(temporary, target, overwrite)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


class _HashingReader:
    """A file-like request body that hashes and counts what is sent, stopping at the declared size."""

    def __init__(self, source, size):
        self.source, self.remaining, self.sent, self.digest = source, size, 0, hashlib.sha256()

    def read(self, size=-1):
        if self.remaining <= 0: return b""
        chunk = self.source.read(min(DOWNLOAD_CHUNK if size is None or size < 0 else size, self.remaining))
        self.digest.update(chunk); self.sent += len(chunk); self.remaining -= len(chunk)
        return chunk


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class Client:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token
        # Local bearer credentials and workbook payloads must stay on the
        # configured connection, independently of ambient proxy settings.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, path, body=None, *, timeout=REQUEST_SECONDS):
        data = json.dumps(body, allow_nan=False).encode() if body is not None else None
        request = Request(self.url + path, data=data,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with self._open(request, timeout) as response:
            return json.load(response)

    def _open(self, request, timeout):
        """Open a request on the configured connection; a structured error response raises APIError."""
        try:
            return self._opener.open(request, timeout=timeout)
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

    def capabilities(self):
        return self.request("/v1/capabilities")

    def upload(self, path, upload_id=None):
        """Stream one spreadsheet package to the service's upload staging and return its receipt.

        The file is read in chunks while it is sent, never wholly; its SHA-256 is computed on the way and must equal
        the service's, otherwise the upload is released and IntegrityError is raised."""
        upload = upload_id or uuid.uuid4().hex
        with open(Path(path).expanduser(), "rb") as source:
            size = os.fstat(source.fileno()).st_size
            limit = self.capabilities().get("max_upload_bytes")
            if not size: raise ValueError("The spreadsheet file is empty; nothing was uploaded")
            if isinstance(limit, int) and size > limit:
                raise ValueError(f"The spreadsheet file is {size} bytes, more than the service's {limit}-byte upload limit; nothing was uploaded")
            reader = _HashingReader(source, size)
            request = Request(self.url + "/v1/uploads/" + quote(upload, safe=""), data=reader, method="PUT",
                              headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/octet-stream",
                                       "Content-Length": str(size)})
            with self._open(request, TRANSFER_SECONDS) as response:
                receipt = json.load(response)
        if reader.sent != size or not isinstance(receipt, dict) or receipt.get("bytes") != size or receipt.get("sha256") != reader.digest.hexdigest():
            try: self.release_upload(upload)
            except (APIError, OSError, ValueError, HTTPException): pass
            raise IntegrityError("The service received different bytes than were read from the file; the upload was released", upload_id=upload)
        return receipt

    def release_upload(self, upload_id):
        return self.request("/v1/uploads/" + quote(upload_id, safe="") + "/release", {})

    def prepare_spreadsheet_file(self, path, *, format=None, mode="mixed", job_id=None, local=False, timezone_policy="reject", progress=None):
        """Prepare a whole XLSX or ODS workbook file into an import stage and return the completed review summary.

        `mode` defaults to "mixed" here, as the desktop preselects it and the CLI defaults to it: formulas DataHorizon
        can evaluate stay formulas and the rest take their cached values. The REST preparation route itself defaults to
        "formulas" when no mode is sent; pass mode="formulas" to keep every formula. The file is uploaded (and the upload released afterwards, whatever happens), or with local=True named by its
        absolute path for a service on this machine to read. A failed or cancelled preparation raises APIError with
        the job's error (status None) after releasing the job; an interruption cancels the job."""
        source = Path(path).expanduser()
        format = format or spreadsheet_format(source)
        if format not in ("xlsx", "ods"): raise ValueError("Unsupported spreadsheet format")
        job = job_id or uuid.uuid4().hex
        request = {"job_id": job, "scope": "workbook", "mode": mode, "timezone_policy": timezone_policy}
        upload = None
        try:
            if local:
                request["source_path"] = str(source.resolve())
            else:
                upload = self.upload(source)["upload_id"]
                request["upload_id"] = upload
            view = self.start_job("/v1/import/" + format + "/jobs", request)
            return self.finish_job(view, progress, "Preparation")
        finally:
            if upload is not None:
                try: self.release_upload(upload)
                except (APIError, OSError, ValueError, HTTPException): pass  # Uploads expire after 15 minutes.

    def start_job(self, path, request):
        """Send a job-creating request. When it ends without the service's answer (a timeout, a lost connection or an
        interruption) the job may exist, so it is discarded; a structured refusal created nothing and discards nothing."""
        try:
            return self.request(path, request)
        except APIError:
            raise
        except BaseException:
            self.discard_job(request["job_id"])
            raise

    def finish_job(self, view, progress=None, label="Job"):
        """Poll a preparation-style job until it leaves running; return its result or raise its error. An interruption or
        an exhausted wait cancels the job and releases it once it has stopped."""
        job, deadline, pause = view["job_id"], time.monotonic() + JOB_WAIT_SECONDS, 0.02
        try:
            while view["status"] == "running":
                if progress is not None: progress(view.get("completed", 0), view.get("phase", ""))
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{label} job {job} did not finish within {JOB_WAIT_SECONDS} seconds; it was cancelled")
                time.sleep(pause); pause = min(pause * 2, 0.5)
                view = self.preparation(job)
        except BaseException:
            self.discard_job(job)
            raise
        if progress is not None: progress(view.get("completed", 0), view.get("phase", ""))
        if view["status"] == "completed": return view["result"]
        self.discard_job(job)
        error = view.get("error") if isinstance(view.get("error"), dict) else {"code": "JOB_" + view["status"].upper(), "message": label + " did not complete", "outcome": "rejected"}
        raise APIError(None, {**error, "job_id": job})

    def discard_job(self, job):
        """Best-effort cancel and release of a job this client started; the service expires it otherwise."""
        try:
            view = self.preparation(job, "cancel")
            for _ in range(100):
                if view.get("status") != "running": break
                time.sleep(0.02); view = self.preparation(job)
            self.preparation(job, "release")
        except (APIError, OSError, ValueError, HTTPException): pass

    def download(self, export_id, destination, *, overwrite=False, expected=None, native=None):
        """Stream a completed export to `destination` through a temporary sibling file, then move it into place.

        An existing destination is refused unless overwrite. `expected` ({bytes, sha256}) is checked before the file is
        moved into place; a short or mismatched body raises and leaves no temporary file. Returns {path, bytes, sha256}."""
        target = Path(destination).expanduser().absolute()
        _check_destination(target, overwrite, native)
        request = Request(self.url + "/v1/exports/" + quote(export_id, safe=""), method="GET",
                          headers={"Authorization": "Bearer " + self.token})
        descriptor, temporary = tempfile.mkstemp(prefix=".datahorizon-download-", dir=target.parent)
        try:
            digest, written = hashlib.sha256(), 0
            with os.fdopen(descriptor, "wb") as output, self._open(request, TRANSFER_SECONDS) as response:
                length = response.headers.get("Content-Length")
                while chunk := response.read(DOWNLOAD_CHUNK):
                    digest.update(chunk); output.write(chunk); written += len(chunk)
                output.flush(); os.fsync(output.fileno())
            if length is None or not length.isdigit() or int(length) != written:
                raise IntegrityError("The download ended before its Content-Length", export_id=export_id)
            if expected is not None and (expected.get("bytes") != written or expected.get("sha256") != digest.hexdigest()):
                raise IntegrityError("The downloaded file does not match the export's size and SHA-256", export_id=export_id)
            _check_destination(target, overwrite, native)
            publish_file(temporary, target, overwrite)
            return {"path": str(target), "bytes": written, "sha256": digest.hexdigest()}
        finally:
            try: os.unlink(temporary)
            except FileNotFoundError: pass

    def release_export(self, export_id):
        return self.request("/v1/exports/" + quote(export_id, safe="") + "/release", {})

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

    def charts(self,sheet=None,chart_id=None):
        self._ensure_open()
        scope=dict(**(dict(sheet=sheet) if sheet is not None else {}),**(dict(chart_id=chart_id) if chart_id is not None else {}))
        return self.client.request(self.base+'/charts',dict(expected_state=self.state,**scope))['charts']

    def create_chart(self,spec,*,sheet='Sheet1',name=None,anchor=None,offset=None,size=None,operation_id=None):
        placement={key:value for key,value in (('name',name),('anchor',anchor),('offset',offset),('size',size)) if value is not None}
        return self.apply('chart_create',sheet,operation_id,spec=spec,**placement)

    def update_chart(self,chart_id,*,operation_id=None,**changes):
        return self.apply('chart_update',operation_id=operation_id,chart_id=chart_id,**changes)

    def delete_chart(self,chart_id,*,operation_id=None):
        return self.apply('chart_delete',operation_id=operation_id,chart_id=chart_id)

    def suggest_chart(self,sheet='Sheet1',*,r1,c1,r2,c2,type='column'):
        self._ensure_open()
        return self.client.request(self.base+'/charts/suggest',dict(expected_state=self.state,sheet=sheet,r1=r1,c1=c1,r2=r2,c2=c2,type=type))

    def chart_job(self,chart_id,kind,*,width=None,height=None,job_id=None):
        self._ensure_open()
        size={key:value for key,value in (('width',width),('height',height)) if value is not None}
        return self.client.request(self.base+'/charts/jobs',dict(job_id=job_id or str(uuid.uuid4()),expected_state=self.state,chart_id=chart_id,kind=kind,**size))

    def chart_job_status(self,job_id,action=None):
        self._ensure_open()
        if action not in (None,'cancel','release'):raise ValueError('Unknown chart job action')
        return self.client.request(self.base+'/charts/jobs/'+quote(job_id,safe='')+('/'+action if action else ''),{} if action else None)

    def _chart_result(self,job,timeout):
        job_id=job['job_id']
        try:
            deadline=time.monotonic()+timeout
            while job['status']=='running':
                if time.monotonic()>=deadline:
                    try:self.chart_job_status(job_id,'cancel')
                    except (APIError,OSError):pass
                    raise TimeoutError('Chart job did not finish within %s seconds'%timeout)
                time.sleep(.05)
                job=self.chart_job_status(job_id)
            if job['status']=='completed':return job['result']
            if job['status']=='stale':raise APIError(409,{'code':'STALE_STATE','message':'The workbook changed while the chart job ran; resolve or render again','outcome':'rejected'})
            error=job.get('error') or {'code':'CHART_JOB_'+str(job['status']).upper(),'message':'Chart job '+str(job['status']),'outcome':'rejected'}
            raise APIError(error.get('status',409),error)
        finally:
            try:self.chart_job_status(job_id,'release')
            except (APIError,OSError):pass

    def resolve_chart(self,chart_id,*,timeout=30):
        return self._chart_result(self.chart_job(chart_id,'resolve'),timeout)

    def render_chart(self,chart_id,width,height,*,timeout=30):
        return self._chart_result(self.chart_job(chart_id,'render',width=width,height=height),timeout)

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

    def export_spreadsheet_file(self, destination, *, format=None, overwrite=False, progress=None):
        """Export the whole workbook (formulas) with an export job and stream it to `destination`.

        The file is downloaded to a temporary sibling, checked against the job's size and SHA-256, and moved into place;
        the export is released afterwards whatever happens. Returns {path, bytes, sha256, state, report, scope_lines, report_entries, totals}."""
        self._ensure_open()
        target = Path(destination).expanduser().absolute()
        format = format or spreadsheet_format(target)
        if format not in ("xlsx", "ods"): raise ValueError("Unsupported spreadsheet format")
        _check_destination(target, overwrite, self.info.get("path"))
        job = uuid.uuid4().hex
        view = self.client.start_job(self.base + "/export/" + format + "/jobs", {"job_id": job, "mode": "formulas"})
        result = self.client.finish_job(view, progress, "Export")
        try:
            written = self.client.download(result["export_id"], target, overwrite=overwrite, expected=result, native=self.info.get("path"))
        finally:
            try: self.client.release_export(result["export_id"])
            except (APIError, OSError, ValueError, HTTPException): pass  # Exports expire five minutes after last use.
        return {**written, **{key: result[key] for key in ("state", "report", "report_entries", "totals")}, "scope_lines": report_scope_lines(result)}

    def import_staged(self, summary, *, target="replace_empty", operation_id=None):
        """Apply a prepared stage's review summary as one workbook_import operation and return the receipt."""
        if summary.get("can_import") is not True:
            raise ValueError("Correct import issues and review again before Apply")
        if not summary.get("stage_id") or not summary.get("stage_digest"):
            raise ValueError("This review summary has no import stage; use import_prepared")
        if target not in ("replace_empty", "add_sheets"): raise ValueError("Choose target replace_empty or add_sheets")
        return self.apply("workbook_import", operation_id=operation_id, request_timeout=APPLY_SECONDS, stage_id=summary["stage_id"],
                          stage_digest=summary["stage_digest"], target=target, label="Import workbook")

    def import_prepared(self, preview, sheet="Sheet1"):
        if preview.get("can_import") is not True:
            raise ValueError("Correct import issues and review again before Apply")
        if preview.get("stage_id"):
            return self.import_staged(preview)
        if preview.get("scope")=="workbook":
            return self.apply("workbook_import",sheets=preview["workbook_sheets"],label="Import workbook")
        return self.apply("edit",sheet,cells=preview["cells"],label="Import spreadsheet")

    def apply(self, action, sheet="Sheet1", operation_id=None, *, request_timeout=REQUEST_SECONDS, **arguments):
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
            # Only a longer-running action names its own timeout; ordinary calls are sent exactly as before.
            response = self.client.request(self.base + "/operations", request,
                                           **({} if request_timeout == REQUEST_SECONDS else {"timeout": request_timeout}))
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
