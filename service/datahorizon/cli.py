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
import unicodedata
from urllib.parse import quote

from .client import APIError, Client, LocalService, write_file
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
        print(output.replace("\x7f","\\u007f"),flush=True)
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
        try: print(json_text(diagnostic),file=sys.stderr,flush=True)
        except OSError: pass  # Neither output channel is available; still fail.
        raise SystemExit(1)
    if cleanup is not None:
        try: print(json_text(cleanup),file=sys.stderr,flush=True)
        except OSError: raise SystemExit(1)


SEVERITY_ORDER = ("unevaluable", "omitted", "approximated", "converted")
UNPRINTABLE = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def terminal_text(value):
    """Text from a file or the service, safe to print: controls, format characters (including bidi overrides),
    surrogates, private-use, unassigned characters and line/paragraph separators become \\xNN, \\uNNNN or \\UNNNNNNNN."""
    def escape(character):
        point = ord(character)
        if unicodedata.category(character) not in UNPRINTABLE: return character
        return f"\\x{point:02x}" if point < 0x100 else f"\\u{point:04x}" if point < 0x10000 else f"\\U{point:08x}"
    return "".join(escape(character) for character in str(value))


def json_text(value):
    """JSON for a terminal: ensure_ascii escapes everything outside ASCII, and DEL is escaped as well."""
    return json.dumps(value, allow_nan=False).replace("\x7f", "\\u007f")


def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def report_text(review):
    """The conversion report grouped for reading: totals by severity, then one line per entry in severity order."""
    entries = review.get("report_entries") or []
    totals = review.get("totals") or {}
    report = review.get("report") or []
    lines = ["Conversion report: " + ", ".join(f"{severity} {totals.get(severity, 0)}" for severity in SEVERITY_ORDER)]
    # The report's lines are its fixed scope statements followed by one line per entry.
    lines += ["  " + line for line in report[:len(report) - len(entries)]]
    rank = {severity: index for index, severity in enumerate(SEVERITY_ORDER)}
    for entry in sorted(entries, key=lambda item: rank.get(item["severity"], len(rank))):
        first = (entry.get("samples") or [None])[0]
        where = "" if first is None else "; first at " + (f"{first['sheet']}!{first['cell']}" if first.get("sheet") else first.get("cell", "workbook"))
        lines.append(f"  {entry['severity']:<12} {entry['count']:>7}  {entry['category']}: {entry['detail']}{where}")
    if "sheets" in review:
        lines.append("Sheets:")
        lines += [f"  {sheet['name']}: {plural(sheet['stored_cells'], 'cell')}" for sheet in review["sheets"]]
        lines.append(f"Total: {plural(review['stored_cells'], 'cell')} in {plural(len(review['sheets']), 'sheet')}")
    return "\n".join(terminal_text(line) for line in lines)


def write_report(path, review, keys, overwrite):
    """Write the report JSON through a temporary sibling; an existing file needs overwrite."""
    data = json.dumps({key: review[key] for key in keys if key in review}, allow_nan=False, indent=2) + "\n"
    write_file(path, data.encode("utf-8"), overwrite=overwrite)


def check_report_path(path, overwrite):
    if path and os.path.lexists(Path(path).expanduser()) and not overwrite:
        raise FileExistsError(f"Report destination {path} exists; pass --overwrite to replace it")


def show_progress(label):
    """Progress lines on an interactive stderr only, so redirected error output stays structured."""
    if not sys.stderr.isatty(): return None
    seen = []
    def report(completed, phase):
        if phase and phase not in seen:
            seen.append(phase)
            print(terminal_text(f"{label}: {phase}"), file=sys.stderr, flush=True)
    return report


def confirm(question):
    """True on y/yes, False on anything else; None when stdin has nothing to answer with (non-interactive).
    The question goes to stderr, so it is seen when stdout is redirected."""
    print(question + " [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = sys.stdin.readline() if sys.stdin is not None else ""
    except (OSError, ValueError):
        answer = ""
    if not answer:
        print(file=sys.stderr, flush=True)
        return None
    # Echo a piped answer so the transcript reads as asked.
    if not sys.stdin.isatty(): print(terminal_text(answer.strip()), file=sys.stderr, flush=True)
    return answer.strip().lower() in ("y", "yes")


def import_spreadsheet(client, book, args):
    """Prepare, show the report, confirm and apply. The prepared job is cancelled and released unless the import commits."""
    check_report_path(args.report_json, args.overwrite)
    summary = client.prepare_spreadsheet_file(args.source, mode=args.mode, local=args.local, timezone_policy=args.timezone_policy,
                                              progress=show_progress("Preparing"))
    job, receipt = summary["stage_id"], None
    try:
        print(report_text(summary), flush=True)
        if args.report_json:
            write_report(args.report_json, summary, ("format", "mode", "stored_cells", "sheets", "totals", "report", "report_entries"), args.overwrite)
        if not args.yes:
            answer = confirm(f"Import {plural(summary['stored_cells'], 'cell')} into {plural(len(summary['sheets']), 'sheet')}?")
            if answer is not True:
                body = ({"code": "CONFIRMATION_REQUIRED", "message": "No confirmation on standard input; pass --yes to import without asking. Nothing was imported.", "outcome": "rejected"}
                        if answer is None else {"code": "DECLINED", "message": "Import declined; nothing was imported.", "outcome": "rejected"})
                print(json_text(body), file=sys.stderr, flush=True)
                raise SystemExit(2 if answer is None else 1)
        receipt = book.import_staged(summary, target="add_sheets" if args.add_sheets else "replace_empty")
    finally:
        if receipt is None: client.discard_job(job)  # The service releases the stage only after a successful apply.
    imported = receipt["imported"]
    return terminal_text(f"Imported {plural(imported['cells'], 'cell')} into {plural(imported['sheets'], 'sheet')} "
                         f"(operation {receipt['operation_id']}, state {receipt['state']})"), receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Attach to an existing localhost REST service")
    parser.add_argument("--approve-code",action="append",default=[],help="Exact operator-approved workbook UUID:code SHA256 for an owned service (repeatable)")
    parser.add_argument("--handler", help="Explicit trusted hook activation for an owned local service")
    parser.add_argument("--readonly", action="store_true")
    parser.add_argument("--repair",action="store_true",help="Restricted managed relationship repair handle")
    parser.add_argument("file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create")
    commands.add_parser("info")
    code=commands.add_parser('code',help='Inspect inert modules; save reads JSON name/source/macros/fingerprint from stdin')
    code.add_argument('code_action',choices=['read','save','delete']);code.add_argument('module',nargs='?');code.add_argument('--fingerprint')
    names=commands.add_parser('names',help='Inspect workbook names; save reads JSON name/reference/name_id? from stdin; delete takes the name id')
    names.add_argument('names_action',choices=['read','save','delete']);names.add_argument('name_id',nargs='?')
    bindings=commands.add_parser('bindings',help='Inspect event bindings; save reads JSON event/scope/module/function/fingerprint/required?/binding_id? from stdin; delete takes the binding id and --fingerprint')
    bindings.add_argument('bindings_action',choices=['read','save','delete']);bindings.add_argument('binding_id',nargs='?');bindings.add_argument('--fingerprint')
    macro=commands.add_parser('macro',help='Invoke one approved atomic macro; JSON scalar arguments on stdin')
    macro.add_argument('module');macro.add_argument('name');macro.add_argument('--fingerprint',required=True);macro.add_argument('--operation-id')
    query=commands.add_parser('query',help='Read-only SQL jobs; run reads JSON stdin and returns the complete frozen result. Later actions require a workbook kept open on --url.')
    query.add_argument('query_action',choices=['run','status','page','cancel','release']);query.add_argument('--job-id');query.add_argument('--offset',type=int,default=0);query.add_argument('--limit',type=int,default=64);query.add_argument('--saved');query.add_argument('--export',choices=['csv','xlsx','ods']);query.add_argument('--output');query.add_argument('--overwrite',action='store_true')
    view=commands.add_parser('view',help='Native saved definitions; opening does not execute SQL');view.add_argument('view_action',choices=['list','read','preview','save','rename','delete']);view.add_argument('name',nargs='?');view.add_argument('--new-name')
    table_export=commands.add_parser('table-export',help='Complete bounded table/filter values export');table_export.add_argument('table');table_export.add_argument('format',choices=['csv','xlsx','ods']);table_export.add_argument('--scope-json',default='{}');table_export.add_argument('--output');table_export.add_argument('--overwrite',action='store_true')
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
    cancel=commands.add_parser('cancel-operation',help='Request cancellation; inspect outcome before deciding whether data committed')
    cancel.add_argument('operation_id')
    cancel.add_argument('--handle',help='An existing handle in --url service; cancellation must not wait for a new open')
    calculate=commands.add_parser('calculate',help='Calculate one bounded range as a background job and wait for its snapshot')
    calculate.add_argument('--sheet',default='Sheet1');calculate.add_argument('--r1',type=int,default=1);calculate.add_argument('--c1',type=int,default=1);calculate.add_argument('--r2',type=int,default=32);calculate.add_argument('--c2',type=int,default=16)
    job=commands.add_parser('job',help='Durable reviewed operation jobs; create/append/start/resume read JSON stdin; execution waits for a terminal outcome')
    job.add_argument('job_action',choices=['create','append','seal','status','start','resume','cancel','release']);job.add_argument('--job-id');job.add_argument('--offset',type=int,default=0);job.add_argument('--limit',type=int,default=100)
    spreadsheet_import=commands.add_parser('import-spreadsheet',help='Prepare a whole .xlsx/.ods workbook, print its conversion report, and import it after confirmation')
    spreadsheet_import.add_argument('source');spreadsheet_import.add_argument('--mode',choices=['formulas','mixed','values'],default='mixed')
    spreadsheet_import.add_argument('--add-sheets',action='store_true',help='Add the sheets after the existing ones instead of filling an empty workbook')
    spreadsheet_import.add_argument('--local',action='store_true',help='Let a service on this machine read SOURCE by path instead of uploading it')
    spreadsheet_import.add_argument('--report-json',help='Also write the full conversion report as JSON to this path')
    spreadsheet_import.add_argument('--yes',action='store_true',help='Import without asking for confirmation')
    spreadsheet_import.add_argument('--timezone-policy',choices=['reject','utc'],default='reject')
    spreadsheet_import.add_argument('--overwrite',action='store_true',help='Replace an existing --report-json file')
    spreadsheet_export=commands.add_parser('export-spreadsheet',help='Export the whole workbook (formulas) as .xlsx/.ods, streamed to DESTINATION')
    spreadsheet_export.add_argument('destination');spreadsheet_export.add_argument('--overwrite',action='store_true',help='Replace an existing destination and --report-json file');spreadsheet_export.add_argument('--report-json')
    direct = commands.add_parser("direct-sql", help="Explicit independent SQLite access; bypasses application behavior")
    direct.add_argument("sql")
    args = parser.parse_args()
    releasing = False
    result = None
    try:
        if args.approve_code and (args.url or args.command=="direct-sql"):raise ValueError("--approve-code configures an owned managed service; configure remote service approval at its operator startup")
        if args.command=='cancel-operation' and (not args.url or not args.handle):raise ValueError('cancel-operation requires --url and --handle for the existing service execution')
        if args.command=='query' and ((args.output and not args.export) or (args.export and args.query_action!='run')):raise ValueError('Query file output requires query run --export FORMAT')
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
        context = nullcontext(Client(args.url, os.environ["DATAHORIZON_TOKEN"])) if args.url else LocalService(args.handler,approved_code=args.approve_code)
        with context as client:
            if args.command=='cancel-operation':
                result=client.request('/v1/workbooks/'+quote(args.handle,safe='')+'/operations/'+quote(args.operation_id,safe='')+'/cancel',{})
                output=json.dumps(result,allow_nan=False);releasing=True
            elif args.command == "inspect":
                result=client.inspect_native(args.file)
                output=json.dumps(result,allow_nan=False)
                releasing=True
            else:
                with client.open(args.file, create=args.command == "create", readonly=args.readonly,repair=args.repair) as book:
                    if args.command in ("create", "info"):
                        result = book.info
                    elif args.command=='code':
                        if args.code_action=='read':result=book.code(args.module)
                        elif args.code_action=='save':
                            options=json.load(sys.stdin)
                            if args.module:options['module']=args.module
                            result=book.save_module(**options)
                        else:
                            if not args.module or not args.fingerprint:raise ValueError('code delete requires module and --fingerprint')
                            result=book.delete_module(args.module,fingerprint=args.fingerprint)
                    elif args.command=='names':
                        if args.names_action=='read':result=book.names()
                        elif args.names_action=='save':result=book.save_name(**json.load(sys.stdin))
                        else:
                            if not args.name_id:raise ValueError('names delete requires the name id')
                            result=book.delete_name(args.name_id)
                    elif args.command=='bindings':
                        if args.bindings_action=='read':result=book.code()['bindings']
                        elif args.bindings_action=='save':result=book.save_binding(**json.load(sys.stdin))
                        else:
                            if not args.binding_id or not args.fingerprint:raise ValueError('bindings delete requires the binding id and --fingerprint')
                            result=book.delete_binding(args.binding_id,fingerprint=args.fingerprint)
                    elif args.command=='macro':result=book.run_macro(args.module,args.name,fingerprint=args.fingerprint,arguments=json.load(sys.stdin),operation_id=args.operation_id)
                    elif args.command=='tables':result=book.tables()
                    elif args.command=='query':
                        import time
                        if args.query_action=='run':
                            options=json.load(sys.stdin)
                            result=book.run_saved_query(args.saved,values=options,job_id=args.job_id) if args.saved else book.run_query(**options,**({'job_id':args.job_id} if args.job_id else {}))
                            while result['status']=='running':time.sleep(.05);result=book.query(result['job_id'])
                            if result['status']=='completed' and args.export:
                                exported=book.export_query(result['job_id'],args.export);result['export']=book.publish_export(exported,args.output,overwrite=args.overwrite) if args.output else exported
                            elif result['status']=='completed':
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
                    elif args.command=='view':
                        if args.view_action=='list':result=book.tables()['queries']
                        elif args.view_action=='read':result=book.query_definition(args.name)
                        elif args.view_action=='rename':result=book.rename_query(args.name,args.new_name)
                        elif args.view_action=='delete':result=book.delete_query(args.name)
                        else:
                            review=book.preview_query(args.name,**json.load(sys.stdin));result=book.save_query(review) if args.view_action=='save' else review
                    elif args.command=='table-export':
                        exported=book.export_table(args.table,args.format,**json.loads(args.scope_json));result=book.publish_export(exported,args.output,overwrite=args.overwrite) if args.output else exported
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
                    elif args.command=='import-spreadsheet':output,result=import_spreadsheet(client,book,args)
                    elif args.command=='export-spreadsheet':
                        check_report_path(args.report_json,args.overwrite)
                        written=book.export_spreadsheet_file(args.destination,overwrite=args.overwrite,progress=show_progress('Exporting'))
                        print(report_text(written),flush=True)
                        if args.report_json:write_report(args.report_json,written,('path','bytes','sha256','state','report','report_entries','totals'),args.overwrite)
                        output=terminal_text(f"Wrote {written['path']} ({written['bytes']} bytes)");result=None
                    elif args.command=='calculate':
                        import time
                        result=book.calculate(args.sheet,r1=args.r1,c1=args.c1,r2=args.r2,c2=args.c2)
                        while result['status']=='running':time.sleep(.05);result=book.calculation(result['job_id'])
                        book.calculation(result['job_id'],'release')
                    else:
                        result = book.outcome(args.operation_id)
                    if args.command not in ('import-spreadsheet','export-spreadsheet'):output=json.dumps(result, allow_nan=False)
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
        print(json_text(body), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
