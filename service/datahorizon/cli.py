"""Managed CLI. Local file mode starts a headless REST service."""

import argparse
import base64
from contextlib import closing, nullcontext
from http.client import HTTPException
import json
import os
from pathlib import Path
import sqlite3
import sys

from .client import APIError, Client, LocalService
from .formula import FormulaError, coordinates


def sql_json_value(value):
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"Unsupported SQL result type: {type(value).__name__}")


def direct_sql_result(db, sql):
    # Python's legacy implicit BEGIN detection does not cover WITH-prefixed
    # writes or DDL. Own one transaction through result serialization/commit.
    blocked = {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
               sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
    denied = False

    def authorize(action, *_):
        nonlocal denied
        if action in blocked:
            denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with db:
        db.execute("BEGIN")
        db.set_authorizer(authorize)
        try:
            cursor = db.execute(sql)
            output = json.dumps({"mode":"direct", "handlers":False, "rows":cursor.fetchall()},
                                default=sql_json_value, allow_nan=False)
        except sqlite3.DatabaseError as error:
            if denied:
                raise APIError(400, {"code":"UNSUPPORTED_DIRECT_SQL", "outcome":"rejected",
                    "message":"Direct SQL owns its transaction; transaction controls, savepoints, PRAGMA, ATTACH and DETACH are unavailable. Use an independent SQLite tool for these commands."}) from error
            raise
        finally:
            # The CLI's commit/rollback must remain allowed after user SQL.
            db.set_authorizer(None)
    return output


def emit_result(output, result, command, cleanup=None):
    try:
        # Flush here so a buffered pipe failure remains inside outcome handling.
        print(output,flush=True)
    except (OSError,ValueError) as error:
        # Avoid a second failed stdout flush at interpreter shutdown corrupting
        # the structured diagnostic or replacing the exit status with 120.
        try:
            target=sys.stdout.fileno()
            descriptor=os.open(os.devnull,os.O_WRONLY)
            if descriptor!=target:
                try: os.dup2(descriptor,target)
                finally: os.close(descriptor)
        except (OSError,ValueError,AttributeError):
            pass
        diagnostic={"code":"OUTPUT_FAILED","phase":"output","message":str(error)}
        if isinstance(result,dict) and "operation_id" in result:
            diagnostic.update(operation_id=result["operation_id"],outcome=result["outcome"],receipt=result)
        elif command=="direct-sql":
            diagnostic.update(outcome="committed",mode="direct",handlers=False)
        elif command=="create":
            diagnostic.update(outcome="committed",path=result["path"],workbook_id=result.get("workbook_id"))
        if cleanup is not None: diagnostic["cleanup_errors"]=[cleanup]
        try: print(json.dumps(diagnostic),file=sys.stderr,flush=True)
        except OSError: pass  # Neither output channel is available; still fail.
        raise SystemExit(1)
    if cleanup is not None:
        try: print(json.dumps(cleanup),file=sys.stderr,flush=True)
        except OSError: raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Attach to an existing localhost REST service")
    parser.add_argument("--handler", help="Explicit trusted hook activation for an owned local service")
    parser.add_argument("--readonly", action="store_true")
    parser.add_argument("file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create")
    commands.add_parser("info")
    read = commands.add_parser("read")
    read.add_argument("address")
    read.add_argument("--sheet", default="Sheet1")
    write = commands.add_parser("set")
    write.add_argument("address")
    write.add_argument("source")
    write.add_argument("--kind", default="auto", choices=["auto", "text", "number", "boolean", "null"])
    write.add_argument("--sheet", default="Sheet1")
    apply = commands.add_parser("apply", help="Read a complete managed operation JSON object from stdin")
    apply.add_argument("--sheet", default="Sheet1")
    outcome = commands.add_parser("outcome")
    outcome.add_argument("operation_id")
    direct = commands.add_parser("direct-sql", help="Explicit independent SQLite access; bypasses application behavior")
    direct.add_argument("sql")
    args = parser.parse_args()
    releasing = False
    result = None
    try:
        if args.command in ("read", "set"):
            row, col = coordinates(args.address)
        if args.command == "direct-sql":
            path = Path(args.file).expanduser().resolve()
            target = path.as_uri()+"?mode=ro" if args.readonly else str(path)
            with closing(sqlite3.connect(target, uri=args.readonly)) as db:
                output=direct_sql_result(db,args.sql)
                releasing=True
            releasing=False
            emit_result(output,result,args.command)
            return
        context = nullcontext(Client(args.url, os.environ["DATAHORIZON_TOKEN"])) if args.url else LocalService(args.handler)
        with context as client:
            with client.open(args.file, create=args.command == "create", readonly=args.readonly) as book:
                if args.command in ("create", "info"):
                    result = book.info
                elif args.command == "read":
                    result = book.read(args.sheet, row, col, row, col)
                elif args.command == "set":
                    result = book.set(row, col, args.source, args.sheet, args.kind)
                elif args.command == "apply":
                    request = json.load(sys.stdin)
                    if not isinstance(request, dict):
                        raise ValueError("Operation JSON must be an object")
                    result = book.apply(request.pop("action"), request.pop("sheet", args.sheet), **request)
                else:
                    result = book.outcome(args.operation_id)
                output=json.dumps(result, allow_nan=False)
                releasing=True
        releasing=False
        emit_result(output,result,args.command)
    except (APIError, OSError, HTTPException, ValueError, TypeError, KeyError, RuntimeError, FormulaError, sqlite3.Error) as error:
        if releasing:
            # Cleanup cannot replace a completed command's result or invite a
            # retry of an acknowledged mutation. Keep diagnostics separate.
            diagnostic={"code":"CLEANUP_FAILED","phase":"cleanup","message":str(error)}
            if isinstance(result,dict) and "operation_id" in result:
                diagnostic.update(operation_id=result["operation_id"],operation_outcome=result.get("outcome"))
            emit_result(output,result,args.command,cleanup=diagnostic)
            return
        code = "INVALID_ADDRESS" if isinstance(error, FormulaError) else "READ_ONLY" if getattr(error,"sqlite_errorcode",None)==sqlite3.SQLITE_READONLY else "CLIENT_ERROR"
        fallback = {"code": code, "message": str(error), "outcome": "rejected"}
        body=dict(getattr(error, "body", fallback))
        if getattr(error,"cleanup_errors",None): body["cleanup_errors"]=error.cleanup_errors
        print(json.dumps(body), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
