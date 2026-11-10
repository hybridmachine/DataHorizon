# SPDX-License-Identifier: GPL-3.0-only
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
    parser.add_argument("--repair",action="store_true",help="Restricted managed relationship repair handle")
    parser.add_argument("file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create")
    commands.add_parser("info")
    query=commands.add_parser('query',help='Read-only SQL jobs; run reads JSON stdin and returns the complete frozen result. Later actions require a workbook kept open on --url.')
    query.add_argument('query_action',choices=['run','status','page','cancel','release']);query.add_argument('--job-id');query.add_argument('--offset',type=int,default=0);query.add_argument('--limit',type=int,default=64)
    commands.add_parser('tables',help='Inspect native structured table definitions')
    table_create=commands.add_parser('table-create',help='Create a table; read a column-definition array from JSON stdin')
    table_create.add_argument('name')
    table_drop=commands.add_parser('table-drop',help='Drop a table through managed execution; populated tables require --allow-data-loss')
    table_drop.add_argument('table')
    table_drop.add_argument('--allow-data-loss',action='store_true')
    table_schema=commands.add_parser('table-schema',help='Preview column changes from JSON stdin; --apply performs the reviewed atomic rebuild')
    table_schema.add_argument('table');table_schema.add_argument('--apply',action='store_true')
    record=commands.add_parser('table-records',help='Read or atomically insert/update/delete table records; mutations read JSON stdin')
    record.add_argument('table');record.add_argument('record_action',choices=['read','insert','update','delete']);record.add_argument('--limit',type=int,default=256);record.add_argument('--after')
    record.add_argument('--preview',action='store_true');record.add_argument('--convert-inputs',action='store_true')
    relation=commands.add_parser('relationship',help='Create from JSON stdin, drop by name/ID, inspect, or preview/apply orphan repair JSON');relation.add_argument('relation_action',choices=['create','drop','inspect','repair']);relation.add_argument('name',nargs='?');relation.add_argument('--apply',action='store_true')
    browse=commands.add_parser('table-browse',help='Read a full-table sorted/filtered/projected page from JSON stdin');browse.add_argument('table')
    index=commands.add_parser('table-index',help='Create an index from JSON stdin, or drop by name/ID');index.add_argument('index_action',choices=['create','drop']);index.add_argument('name')
    table_from=commands.add_parser('table-from-range',help='Preview a source selection from JSON stdin; --apply creates the reviewed value snapshot in this session')
    table_from.add_argument('name');table_from.add_argument('--apply',action='store_true')
    commands.add_parser("inspect",help="Inspect a native file read-only without code activation or repair")
    read = commands.add_parser("read")
    read.add_argument("address")
    read.add_argument("--sheet", default="Sheet1")
    write = commands.add_parser("set")
    write.add_argument("address")
    write.add_argument("source")
    write.add_argument("--kind", default="auto", choices=["auto", "text", "number", "boolean", "null", "date", "time", "datetime"])
    write.add_argument("--sheet", default="Sheet1")
    write.add_argument("--locale",choices=["invariant","en_US","de_DE","fr_FR"])
    write.add_argument("--timezone-policy",choices=["reject","utc"],default="reject")
    apply = commands.add_parser("apply", help="Read a complete managed operation JSON object from stdin")
    apply.add_argument("--sheet", default="Sheet1")
    outcome = commands.add_parser("outcome")
    outcome.add_argument("operation_id")
    calculate=commands.add_parser('calculate',help='Calculate one bounded range as a background job and wait for its snapshot')
    calculate.add_argument('--sheet',default='Sheet1');calculate.add_argument('--r1',type=int,default=1);calculate.add_argument('--c1',type=int,default=1);calculate.add_argument('--r2',type=int,default=32);calculate.add_argument('--c2',type=int,default=16)
    job=commands.add_parser('job',help='Durable reviewed operation jobs; create/append/start/resume read JSON stdin; execution waits for a terminal outcome')
    job.add_argument('job_action',choices=['create','append','seal','status','start','resume','cancel','release']);job.add_argument('--job-id');job.add_argument('--offset',type=int,default=0);job.add_argument('--limit',type=int,default=100)
    direct = commands.add_parser("direct-sql", help="Explicit independent SQLite access; bypasses application behavior")
    direct.add_argument("sql")
    args = parser.parse_args()
    releasing = False
    result = None
    try:
        if args.command in ("read", "set"):
            row, col = coordinates(args.address)
        if args.command == "direct-sql":
            if args.repair:raise ValueError('--repair is a restricted managed mode; use relationship repair commands')
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
            if args.command == "inspect":
                result=client.inspect_native(args.file)
                output=json.dumps(result,allow_nan=False)
                releasing=True
            else:
                with client.open(args.file, create=args.command == "create", readonly=args.readonly,repair=args.repair) as book:
                    if args.command in ("create", "info"):
                        result = book.info
                    elif args.command=='tables':result=book.tables()
                    elif args.command=='query':
                        import time
                        if args.query_action=='run':
                            result=book.run_query(**json.load(sys.stdin),**({'job_id':args.job_id} if args.job_id else {}))
                            while result['status']=='running':time.sleep(.05);result=book.query(result['job_id'])
                            if result['status']=='completed':
                                rows=[];columns=[col['id'] for col in result['result']['columns']];offset=0
                                while True:
                                    page=book.query(result['job_id'],'page',offset=offset,columns=columns);rows.extend(page['rows'])
                                    if page['next_offset'] is None:break
                                    offset=page['next_offset']
                                result['rows']=rows
                            book.query(result['job_id'],'release')
                        else:
                            if not args.job_id:raise ValueError('--job-id is required')
                            result=book.query(args.job_id,None if args.query_action=='status' else args.query_action,**(dict(offset=args.offset,limit=args.limit) if args.query_action=='page' else {}))
                    elif args.command=='table-create':result=book.create_table(args.name,json.load(sys.stdin))
                    elif args.command=='table-drop':result=book.drop_table(args.table,allow_data_loss=args.allow_data_loss)
                    elif args.command=='table-schema':
                        options=json.load(sys.stdin);review=book.preview_table_schema(args.table,**options)
                        result=book.alter_table(review) if args.apply else review
                    elif args.command=='table-records':
                        if args.record_action=='read':result=book.read_table(args.table,limit=args.limit,**({'after':args.after} if args.after else {}))
                        else:
                            payload=json.load(sys.stdin);key={'insert':'rows','update':'edits','delete':'record_ids'}[args.record_action]
                            if args.preview or args.convert_inputs:
                                review=book.preview_record_edits(args.table,'table_'+args.record_action,**{key:payload})
                                result=review if args.preview else book.apply_record_preview(review)
                            else:result=getattr(book,{'insert':'insert_records','update':'update_records','delete':'delete_records'}[args.record_action])(args.table,payload)
                    elif args.command=='relationship':
                        if args.relation_action=='inspect':result=book.inspect_relationships()
                        elif args.relation_action=='create':result=book.create_relationship(args.name,**json.load(sys.stdin))
                        elif args.relation_action=='drop':result=book.drop_relationship(args.name)
                        else:
                            review=book.preview_orphan_repair(args.name,**json.load(sys.stdin));result=book.apply_orphan_repair(review) if args.apply else review
                    elif args.command=='table-browse':result=book.read_table(args.table,**json.load(sys.stdin))
                    elif args.command=='table-index':result=book.drop_index(args.name) if args.index_action=='drop' else book.create_index(name=args.name,**json.load(sys.stdin))
                    elif args.command=='table-from-range':
                        options=json.load(sys.stdin);review=book.preview_table(args.name,**options)
                        result=book.promote_table(review) if args.apply else review
                    elif args.command == "read":
                        result = book.read(args.sheet, row, col, row, col)
                    elif args.command == "set":
                        if args.locale or args.timezone_policy=="utc":
                            converted=client.convert_value(args.source,args.kind,args.locale or "invariant",args.timezone_policy);result=book.set(row,col,converted['cell']['source'],args.sheet,converted['cell']['kind']);result={**result,'conversion':converted}
                        else:result = book.set(row, col, args.source, args.sheet, args.kind)
                    elif args.command == "apply":
                        request = json.load(sys.stdin)
                        if not isinstance(request, dict):
                            raise ValueError("Operation JSON must be an object")
                        result = book.apply(request.pop("action"), request.pop("sheet", args.sheet), **request)
                    elif args.command=='job':
                        import time
                        arguments=json.load(sys.stdin) if args.job_action in ('create','append','start','resume') else {}
                        if args.job_action=='create':result=book.create_job(job_id=args.job_id,**arguments)
                        else:
                            if not args.job_id:raise ValueError('--job-id is required')
                            if args.job_action=='status':arguments.update(offset=args.offset,limit=args.limit)
                            if args.job_action in ('start','resume'):arguments.setdefault('expected_state',book.state)
                            result=book.operation_job(args.job_id,args.job_action,**arguments)
                        while result.get('status') in ('running',) or (args.job_action=='create' and 'source_path' in arguments and result.get('status')=='staging'):
                            time.sleep(.05);result=book.operation_job(result['job_id'])
                    elif args.command=='calculate':
                        import time
                        result=book.calculate(args.sheet,r1=args.r1,c1=args.c1,r2=args.r2,c2=args.c2)
                        while result['status']=='running':time.sleep(.05);result=book.calculation(result['job_id'])
                        book.calculation(result['job_id'],'release')
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
