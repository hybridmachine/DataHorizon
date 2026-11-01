"""Explicitly activated trusted hooks, isolated for timeout/crash containment."""

import multiprocessing
import os
from contextlib import redirect_stderr, redirect_stdout
import runpy
import time
import threading
import traceback


class HookFailure(Exception):
    pass


def _worker(pipe, path, changes, operation):
    class LogTail:
        def __init__(self):
            self.text=""

        def write(self, text):
            self.text=(self.text+text)[-8192:]
            return len(text)

        def flush(self):
            pass

    class Context:
        operation_id = operation

        def request(self, method, **arguments):
            pipe.send({"method": method, **arguments})
            response = pipe.recv()
            if "error" in response:
                raise RuntimeError(response["error"])
            return response.get("result")

        def get(self, sheet, row, col):
            return self.request("get", sheet=sheet, row=row, col=col)

        def value(self, sheet, row, col):
            return self.request("value", sheet=sheet, row=row, col=col)

        def set(self, sheet, row, col, source, kind="auto"):
            return self.request("set", sheet=sheet, row=row, col=col, source=source, kind=kind)

    logs=LogTail()
    # Also bound ordinary Python execution if the service is terminated before
    # its parent-side timeout can reap this worker. This is not a sandbox.
    watchdog=threading.Timer(2.5,lambda:os._exit(124))
    watchdog.daemon=True
    watchdog.start()
    # Workbook print/logging cannot corrupt the parent's startup protocol or
    # block on its stdout pipe. The diagnostic tail is included on failure.
    with redirect_stdout(logs), redirect_stderr(logs):
        try:
            module = runpy.run_path(path)
            module["on_change"](Context(), changes)
            pipe.send({"method": "done"})
        except BaseException:
            pipe.send({"method": "failed", "error": traceback.format_exc(limit=8)+logs.text})
        finally:
            watchdog.cancel()
            pipe.close()


def invoke(path, changes, operation, dispatch, deadline):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_worker, args=(child, str(path), changes, operation), daemon=True)
    process.start()
    child.close()
    deadline = min(deadline, time.monotonic() + 2)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not parent.poll(max(0, remaining)):
                raise HookFailure("Required Python handler timed out; operation rolled back")
            try:
                message = parent.recv()
            except EOFError as error:
                raise HookFailure("Required Python handler exited without a result") from error
            method = message.pop("method")
            if method == "done":
                return
            if method == "failed":
                raise HookFailure(message["error"])
            try:
                parent.send({"result": dispatch(method, message)})
            except Exception as error:
                # A broker validation error always fails the whole operation, even if
                # workbook code attempts to catch it and continue.
                raise HookFailure(str(error)) from error
    finally:
        parent.close()
        process.join(timeout=0.1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join()
