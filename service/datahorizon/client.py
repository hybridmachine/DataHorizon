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

    def inspect_native(self, path):
        return self.request("/v1/recovery/inspect", {"path":str(Path(path).expanduser().resolve())})

    def prepare_csv(self, request, job_id=None):
        return self.request("/v1/import/csv/jobs", {**request,"job_id":job_id or str(uuid.uuid4())})

    def prepare_spreadsheet(self, data, *, format="xlsx", sheet_index=1, mode="formulas", scope="selection", job_id=None):
        import base64
        if format not in ("xlsx","ods"): raise ValueError("Unsupported spreadsheet format")
        return self.request("/v1/import/"+format+"/jobs", {"file_base64":base64.b64encode(data).decode(),
            "job_id":job_id or str(uuid.uuid4()),"sheet_index":sheet_index,"mode":mode,"scope":scope})

    def preparation(self, job_id, action=None):
        path = "/v1/jobs/"+quote(job_id,safe="")
        return self.request(path+"/"+action, {}) if action in ("cancel","release") else self.request(path)

    def open(self, path, create=False, readonly=False):
        info = self.request("/v1/open", {"path": str(Path(path).expanduser().resolve()), "create": create, "readonly": readonly})
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

    def read(self, sheet="Sheet1", r1=1, c1=1, r2=32, c2=16):
        self._ensure_open()
        return self.client.request(self.base + "/range", {"sheet": sheet, "r1": r1, "c1": c1, "r2": r2, "c2": c2})

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

    def set(self, row, col, source, sheet="Sheet1", kind="auto"):
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

    def __init__(self, handler=None):
        command = [sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve().with_name("launch.py"))]
        if handler:
            command += ["--handler", str(handler)]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
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

    def close(self):
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
