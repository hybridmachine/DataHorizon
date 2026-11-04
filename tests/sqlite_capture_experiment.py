"""Isolated M1 SQLite capture experiment; never installed or used by the service.

Run directly to write a repeatable correctness/measurement report. The small
snapshot oracle deliberately scans the fixture; the candidate journal does not.
"""

import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import tempfile
import time


TABLES = {
    "parents": ("id", "label"),
    "records": ("parent_id", "key", "value"),
    "effects": ("key", "value"),
}
KEYS = {"parents": (0,), "records": (0, 1), "effects": (0,)}
SCHEMA = """
CREATE TABLE parents(id INTEGER PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE records(
    parent_id INTEGER NOT NULL REFERENCES parents(id)
        ON UPDATE CASCADE ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
    key TEXT NOT NULL UNIQUE, value,
    PRIMARY KEY(parent_id,key)) WITHOUT ROWID;
CREATE TABLE effects(key TEXT PRIMARY KEY NOT NULL, value);
CREATE TABLE reserved(revision INTEGER NOT NULL);
INSERT INTO reserved VALUES(0);
INSERT INTO parents VALUES(1,'one'),(2,'two');
INSERT INTO records VALUES(1,'a',10),(1,'b',20),(2,'c',30);
CREATE TRIGGER record_effect AFTER UPDATE OF value ON records
WHEN OLD.value IS NOT NEW.value BEGIN
    INSERT INTO effects VALUES(NEW.key,NEW.value)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value;
END;
CREATE VIEW record_values AS SELECT key,value FROM records;
CREATE TRIGGER view_edit INSTEAD OF UPDATE ON record_values BEGIN
    UPDATE records SET value=NEW.value WHERE key=OLD.key;
END;
"""


def fixture(path=":memory:"):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA recursive_triggers=ON")
    db.executescript(SCHEMA)
    return db


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def identity(table, row):
    return table, tuple(row[index] for index in KEYS[table])


def same_row(left, right):
    # Python considers 1 == 1.0. SQLite storage types belong to the contract too.
    return left == right and (left is None or right is None or
                             tuple(map(type, left)) == tuple(map(type, right)))


def snapshot(db):
    return {identity(table, row): row for table, columns in TABLES.items()
            for row in db.execute(f"SELECT {','.join(map(quote, columns))} FROM {quote(table)}")}


def difference(before, after):
    return {key: (before.get(key), after.get(key)) for key in before.keys() | after.keys()
            if not same_row(before.get(key), after.get(key))}


class Journal:
    """TEMP AFTER triggers retain typed row images without SQLite callbacks.

    Limits cover raw events (including no-ops), not just final changed rows.
    The byte charge is payload plus 128 bytes/event, not an SQLite RSS bound.
    """

    def __init__(self, db, max_events=10_000, max_bytes=4*1024*1024):
        self.db = db
        db.execute("PRAGMA temp_store=FILE")
        db.execute("CREATE TEMP TABLE capture_budget(events INTEGER, bytes INTEGER)")
        db.execute("INSERT INTO capture_budget VALUES(0,0)")
        # No column affinity: NULL, INTEGER, REAL, TEXT and BLOB remain distinct.
        db.execute("""CREATE TEMP TABLE capture(
            seq INTEGER PRIMARY KEY, table_name TEXT, operation TEXT,
            old0,old1,old2,new0,new1,new2)""")
        for table, columns in TABLES.items():
            for operation in ("INSERT", "UPDATE", "DELETE"):
                values, charges = [], []
                for prefix, absent in (("OLD", operation == "INSERT"),
                                       ("NEW", operation == "DELETE")):
                    for index in range(3):
                        value = "NULL" if absent or index >= len(columns) else f"{prefix}.{quote(columns[index])}"
                        values.append(value)
                        charges.append(f"coalesce(length(CAST({value} AS BLOB)),0)")
                db.execute(f"""CREATE TEMP TRIGGER {quote('capture_'+table+'_'+operation)}
                    AFTER {operation} ON main.{quote(table)} BEGIN
                    UPDATE capture_budget SET events=events+1,bytes=bytes+128+{'+'.join(charges)};
                    SELECT CASE WHEN (SELECT events FROM capture_budget)>{int(max_events)}
                        THEN RAISE(ABORT,'CAPTURE_ROW_LIMIT') END;
                    SELECT CASE WHEN (SELECT bytes FROM capture_budget)>{int(max_bytes)}
                        THEN RAISE(ABORT,'CAPTURE_BYTE_LIMIT') END;
                    INSERT INTO capture VALUES(NULL,'{table}','{operation}',{','.join(values)});
                    END""")

    def clear(self):
        self.db.execute("DELETE FROM temp.capture")
        self.db.execute("UPDATE temp.capture_budget SET events=0,bytes=0")

    def events(self, batch_size=128):
        if not 1 <= batch_size <= 128:
            raise ValueError("Journal reads require 1..128 events per batch")
        with closing(self.db.execute("SELECT * FROM temp.capture ORDER BY seq")) as cursor:
            while batch := cursor.fetchmany(batch_size):
                for seq, table, operation, *values in batch:
                    size = len(TABLES[table])
                    yield (seq, table, operation,
                           None if operation == "INSERT" else tuple(values[:size]),
                           None if operation == "DELETE" else tuple(values[3:3+size]))

    def replay(self, before):
        """Check EVERY old image and replay every new image against the oracle.

        This detects missing replacement deletes and out-of-order same-row
        trigger images; merely comparing event counts would miss those errors.
        """
        result = dict(before)
        for _, table, operation, old, new in self.events():
            if old is not None:
                key = identity(table, old)
                if not same_row(result.get(key), old):
                    raise AssertionError(f"Missing/out-of-order old image: {table} {operation} {old!r}")
                del result[key]
            if new is not None:
                key = identity(table, new)
                if key in result:
                    raise AssertionError(f"Uncaptured replacement delete: {table} {new!r}")
                result[key] = new
        return result


BLOCKED_ACTIONS = {getattr(sqlite3, name) for name in (
    "SQLITE_TRANSACTION", "SQLITE_SAVEPOINT", "SQLITE_PRAGMA", "SQLITE_ATTACH", "SQLITE_DETACH",
    "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
    "SQLITE_CREATE_VTABLE", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW", "SQLITE_DROP_VTABLE", "SQLITE_ALTER_TABLE", "SQLITE_REINDEX", "SQLITE_ANALYZE")}
WRITES = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}


def authorize(action, table, column, database, trigger):
    if action in BLOCKED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if database == "temp" and not (trigger or "").startswith("capture_"):
        return sqlite3.SQLITE_DENY
    if action in WRITES and database == "main" and table not in TABLES:
        # Views use INSTEAD OF triggers; actual writes still hit the allowlist.
        if table != "record_values":
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def operation(db, journal, statements, *, rollback=False, instruction_limit=None):
    """Trusted driver owns the transaction; any statement/commit failure rolls back.

    User SQL is a sequence of single prepared statements, never executescript.
    Captured rows are consumed before commit, just as transactional hooks must be.
    """
    journal.clear()
    db.execute("BEGIN IMMEDIATE")
    instructions = 0

    def progress():
        nonlocal instructions
        instructions += 100
        return int(instruction_limit is not None and instructions >= instruction_limit)

    try:
        db.set_authorizer(authorize)
        db.set_progress_handler(progress, 100)
        for sql, parameters in statements:
            with closing(db.execute(sql, parameters)) as cursor:
                # Drain bounded reads rather than materializing RETURNING output.
                while cursor.fetchmany(128):
                    pass
        db.set_authorizer(None)
        db.set_progress_handler(None, 0)
        events = list(journal.events())  # raw events already capped by SQL triggers
        budget = db.execute("SELECT * FROM temp.capture_budget").fetchone()
        db.execute("ROLLBACK" if rollback else "COMMIT")
        return {"events": events, "event_count": budget[0], "charged_bytes": budget[1],
                "instructions_rounded": instructions}
    except BaseException:
        # Interrupted SQLite may already have rolled back the whole transaction.
        db.set_authorizer(None)
        db.set_progress_handler(None, 0)
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


CASES = {
    "insert": [("INSERT INTO records VALUES(1,'d','00123')", ())],
    "update_and_after_trigger": [("UPDATE records SET value=11 WHERE key='a'", ())],
    "delete": [("DELETE FROM records WHERE key='a'", ())],
    "upsert_update": [("INSERT INTO records VALUES(1,'a',12) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ())],
    "upsert_insert": [("INSERT INTO records VALUES(1,'d',12) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ())],
    "replace_primary_key": [("INSERT OR REPLACE INTO records VALUES(1,'a',12)", ())],
    "replace_unique_key": [("INSERT OR REPLACE INTO records VALUES(2,'a',12)", ())],
    "update_or_replace": [("UPDATE OR REPLACE records SET key='b' WHERE key='a'", ())],
    "delete_cascade": [("DELETE FROM parents WHERE id=1", ())],
    "update_cascade": [("UPDATE parents SET id=3 WHERE id=1", ())],
    "primary_key_move": [("UPDATE records SET key='z' WHERE key='a'", ())],
    "instead_of_view": [("UPDATE record_values SET value=11 WHERE key='a'", ())],
    "cte_write": [("WITH picked AS (SELECT key FROM records WHERE parent_id=1) UPDATE records SET value=11 WHERE key IN picked", ())],
    "delete_all": [("DELETE FROM records", ())],
    "no_op": [("UPDATE records SET value=value WHERE key='a'", ())],
    "ignore": [("INSERT OR IGNORE INTO records VALUES(1,'a',11)", ())],
    "coalesced": [("UPDATE records SET value=? WHERE key='a'", (value,)) for value in (11, 12, 10)] +
                 [("DELETE FROM effects", ())],
    "typed_values": [("UPDATE records SET value=? WHERE key='a'", (value,))
                     for value in (None, 1, 1.0, "1", b"\x00\xff", "\x00\u00e9")],
}


def json_value(value):
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def correctness_report():
    results = {}
    for name, statements in CASES.items():
        with closing(fixture()) as db:
            journal = Journal(db)
            before = snapshot(db)
            result = operation(db, journal, statements)
            after = snapshot(db)
            if difference(journal.replay(before), after):
                raise AssertionError(f"Capture mismatch: {name}")
            results[name] = {"event_count": result["event_count"], "changed_rows": len(difference(before, after)),
                             "charged_bytes": result["charged_bytes"],
                             "typed_images": json_value(result["events"])}
    return results


def negative_controls():
    with closing(fixture()) as db:
        before = snapshot(db)
        returned = db.execute("DELETE FROM parents WHERE id=1 RETURNING *").fetchall()
        returning = {"returned_rows": len(returned), "actual_changed_rows": len(difference(before, snapshot(db))),
                     "changes_count": db.execute("SELECT changes()").fetchone()[0]}
        if returning["returned_rows"] != 1 or returning["actual_changed_rows"] != 3:
            raise AssertionError("RETURNING negative control changed")
    with closing(fixture()) as db:
        db.execute("PRAGMA recursive_triggers=OFF")
        journal = Journal(db)
        before = snapshot(db)
        operation(db, journal, CASES["replace_unique_key"])
        missed = len(difference(journal.replay(before), snapshot(db)))
        if missed != 1:
            raise AssertionError("Expected an uncaptured REPLACE deletion with recursion disabled")
    return {"returning_and_changes": returning, "recursive_triggers_off": {"missed_rows": missed}}


def measure(samples):
    result = {}
    for candidate in ("explicit_known_targets", "temp_trigger_journal"):
        with closing(fixture()) as db:
            db.execute("DROP TRIGGER record_effect")
            journal = Journal(db) if candidate == "temp_trigger_journal" else None
            durations = []
            for index in range(samples):
                started = time.perf_counter()
                if journal:
                    operation(db, journal, [("UPDATE records SET value=? WHERE key='a'", (index,))])
                else:
                    db.execute("BEGIN IMMEDIATE")
                    old = db.execute("SELECT * FROM records WHERE key='a'").fetchone()
                    db.execute("UPDATE records SET value=? WHERE key='a'", (index,))
                    new = db.execute("SELECT * FROM records WHERE key='a'").fetchone()
                    if old is None or new[2] != index:
                        raise AssertionError("Explicit edit fixture failed")
                    db.execute("COMMIT")
                durations.append((time.perf_counter()-started)*1000)
            result[candidate] = {"samples": samples, "median_ms": round(statistics.median(durations), 4),
                                 "p95_ms": round(sorted(durations)[math.ceil(samples*.95)-1], 4)}
    return result


def direct_access_report():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp)/"capture.sqlite"
        with closing(fixture(path)) as managed:
            journal = Journal(managed)
            with closing(sqlite3.connect(path)) as direct:
                direct.execute("UPDATE records SET value=42 WHERE key='a'")
                direct.commit()
                triggers = direct.execute("SELECT name FROM sqlite_temp_schema WHERE type='trigger'").fetchall()
            if list(journal.events()) or triggers:
                raise AssertionError("TEMP journal leaked into independent access")
            with closing(sqlite3.connect(":memory:")) as copied:
                managed.backup(copied)
                if copied.execute("SELECT name FROM sqlite_temp_schema").fetchall():
                    raise AssertionError("Snapshot contains capture metadata")
                if difference(snapshot(managed), snapshot(copied)):
                    raise AssertionError("Snapshot changed stored values")
            return {"independent_write_value": managed.execute("SELECT value FROM records WHERE key='a'").fetchone()[0],
                    "managed_capture_events": 0, "independent_temp_triggers": 0, "snapshot_matches": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    with closing(sqlite3.connect(":memory:")) as db:
        compile_options = [row[0] for row in db.execute("PRAGMA compile_options")]
    report = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
              "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "binding": "Python standard-library sqlite3", "compile_options": compile_options,
              "native_capture_apis_exposed": [name for name in ("set_update_hook", "preupdate_hook", "session")
                                             if hasattr(sqlite3.Connection, name)],
              "correctness": correctness_report(), "negative_controls": negative_controls(),
              "direct_access": direct_access_report(), "warm_in_memory_measurements": measure(args.samples),
              "limitations": ["Isolated fixture, not the native format or an exposed managed SQL API.",
                              "Arbitrary schemas, virtual tables, BEFORE triggers and same-row trigger rewrites are unqualified.",
                              "Raw journal limited to 10000 events and 4 MiB charged payload; this is not an RSS guarantee.",
                              "No native preupdate/session binding measured; compile flags alone do not expose Python APIs.",
                              "Timings exclude REST, disk durability, hooks, cold caches and native platform acceptance."]}
    output = json.dumps(report, indent=2, allow_nan=False)+"\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
