"""Thin Python REST SDK. It never opens a workbook database."""

import json
from http.client import HTTPException
from collections import deque
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
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


class Client:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def request(self, path, body=None):
        data = json.dumps(body, allow_nan=False).encode() if body is not None else None
        request = Request(self.url + path, data=data,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                body = json.load(error)
            finally:
                error.close()
            if not isinstance(body, dict):
                raise ValueError("Invalid JSON error response")
            raise APIError(error.code, body) from error

    def open(self, path, create=False, readonly=False):
        info = self.request("/v1/open", {"path": str(Path(path).expanduser().resolve()), "create": create, "readonly": readonly})
        return WorkbookClient(self, info)


class WorkbookClient:
    def __init__(self, client, info):
        self.client, self.info = client, info
        self.base = "/v1/workbooks/" + info["id"]
        self.state = info["state"]
        self.pending_operation = None

    def refresh(self):
        self.info = self.client.request(self.base)
        self.state = self.info["state"]
        return self.info

    def read(self, sheet="Sheet1", r1=1, c1=1, r2=32, c2=16):
        return self.client.request(self.base + "/range", {"sheet": sheet, "r1": r1, "c1": c1, "r2": r2, "c2": c2})

    def apply(self, action, sheet="Sheet1", operation_id=None, **arguments):
        if self.pending_operation:
            raise UnknownOutcome(self.pending_operation)
        request = {"action": action, "sheet": sheet, "operation_id": operation_id or str(uuid.uuid4()),
                   "expected_state": self.state, **arguments}
        json.dumps(request,allow_nan=False)  # Invalid local input has no unknown network outcome.
        self.pending_operation=request["operation_id"]
        try:
            response = self.client.request(self.base + "/operations", request)
            self.validate_receipt(response, request["operation_id"], committed=True)
        except APIError as error:
            if (error.body.get("outcome") in ("rejected","rolled_back")
                    and isinstance(error.body.get("code"), str) and error.body["code"]
                    and isinstance(error.body.get("message"), str)
                    and error.body.get("operation_id",request["operation_id"]) == request["operation_id"]):
                self.pending_operation=None
                raise
            raise UnknownOutcome(request["operation_id"],str(error)) from error
        except (OSError, ValueError, HTTPException) as error:
            raise UnknownOutcome(request["operation_id"],str(error)) from error
        self.pending_operation=None
        self.state = response["state"]
        return response

    def set(self, row, col, source, sheet="Sheet1", kind="auto"):
        return self.apply("edit", sheet, cells=[{"row": row, "col": col, "source": str(source), "kind": kind}])

    def outcome(self, operation_id):
        try:
            receipt=self.client.request(self.base + "/operations/" + quote(operation_id, safe=""))
            self.validate_receipt(receipt, operation_id)
        except (OSError, ValueError, HTTPException) as error:
            raise UnknownOutcome(operation_id, str(error)) from error
        if operation_id==self.pending_operation and receipt.get("outcome") in ("committed","rejected","rolled_back"):
            self.pending_operation=None
            self.refresh()
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
        if self.pending_operation:
            raise UnknownOutcome(self.pending_operation)
        return self.client.request(self.base + "/close", {})

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class LocalService:
    """Owns only the child service it started; external services are never stopped."""

    def __init__(self, handler=None):
        command = [sys.executable, "-m", "datahorizon.service"]
        if handler:
            command += ["--handler", str(handler)]
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
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
            raise RuntimeError("Local service failed to start; run python -m datahorizon.service for diagnostics") from None

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

    def __exit__(self, *_):
        self.close()
