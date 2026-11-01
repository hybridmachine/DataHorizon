"""Managed CLI. Local file mode starts a headless REST service."""

import argparse
from contextlib import closing, nullcontext
import json
import os
import sqlite3
import sys

from .client import APIError, Client, LocalService
from .formula import coordinates


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
    try:
        if args.command == "direct-sql":
            with closing(sqlite3.connect(args.file)) as db, db:
                cursor = db.execute(args.sql)
                print(json.dumps({"mode": "direct", "handlers": False, "rows": cursor.fetchall()}))
            return
        context = nullcontext(Client(args.url, os.environ["DATAHORIZON_TOKEN"])) if args.url else LocalService(args.handler)
        with context as client:
            with client.open(args.file, create=args.command == "create", readonly=args.readonly) as book:
                if args.command in ("create", "info"):
                    result = book.info
                elif args.command == "read":
                    row, col = coordinates(args.address)
                    result = book.read(args.sheet, row, col, row, col)
                elif args.command == "set":
                    row, col = coordinates(args.address)
                    result = book.set(row, col, args.source, args.sheet, args.kind)
                elif args.command == "apply":
                    request = json.load(sys.stdin)
                    if not isinstance(request, dict):
                        raise ValueError("Operation JSON must be an object")
                    result = book.apply(request.pop("action"), request.pop("sheet", args.sheet), **request)
                else:
                    result = book.outcome(args.operation_id)
                print(json.dumps(result, allow_nan=False))
    except (APIError, OSError, ValueError, KeyError, RuntimeError) as error:
        print(json.dumps(getattr(error, "body", {"code": "CLIENT_ERROR", "message": str(error)})), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
