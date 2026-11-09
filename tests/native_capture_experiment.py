# SPDX-License-Identifier: GPL-3.0-only
"""Optional APSW preupdate comparison; never imported by the installed service.

Requires the pinned wheel in requirements-capture.txt. No SQLite pointer/ABI
access and no Session object share this connection with its preupdate hook.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sqlite3
import tempfile
import time

import apsw

from sqlite_capture_experiment import (CASES, Journal, SCHEMA, TABLES, authorize,
    correctness_report as journal_correctness, difference, json_value,
    measure as journal_measure, negative_controls, snapshot)


class CaptureFailure(RuntimeError): pass


def fixture(path=':memory:', *, recursive=True):
    db=apsw.Connection(str(path))
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA recursive_triggers='+('ON' if recursive else 'OFF'))
        list(db.execute(SCHEMA))
        return db
    except BaseException:
        db.close(); raise


def charge(row):
    return sum(0 if value is None else len(value if isinstance(value,bytes) else str(value).encode('utf-8')) for value in row or ())


class NativeCapture:
    def __init__(self, db, max_events=10_000, max_bytes=4*1024*1024):
        if not hasattr(db,'preupdate_hook'):
            raise RuntimeError('Binding must expose SQLITE_ENABLE_PREUPDATE_HOOK')
        self.db,self.max_events,self.max_bytes=db,max_events,max_bytes
        self.clear()
        db.preupdate_hook(self.capture)

    def clear(self):
        self.rows,self.depths=[],[]
        self.charged_bytes=0
        self.failure=None

    def capture(self, update):
        if self.failure is not None: return
        try:
            table,operation=update.table_name,update.op
            if update.database_name!='main' or table not in TABLES or update.blob_write>=0:
                raise CaptureFailure('Unqualified native capture surface')
            if len(self.rows)>=self.max_events: raise CaptureFailure('CAPTURE_ROW_LIMIT')
            # These properties are valid only within this callback. Copy typed
            # images now; never retain the transient PreUpdate object/rowids.
            old_image,new_image=update.old,update.new
            old=None if old_image is None else tuple(old_image)
            new=None if new_image is None else tuple(new_image)
            charged=128+charge(old)+charge(new)
            if self.charged_bytes+charged>self.max_bytes: raise CaptureFailure('CAPTURE_BYTE_LIMIT')
            self.rows.append((len(self.rows)+1,table,operation,old,new))
            self.depths.append(update.depth)
            self.charged_bytes+=charged
        except Exception as error:
            # A callback exception alone cannot veto an SQLite update. Retain a
            # failure flag, interrupt the VM, and require driver rollback even
            # if SQLite finishes the statement before noticing the interrupt.
            self.failure=f'{type(error).__name__}: {error}'
            self.db.interrupt()

    def events(self): return iter(self.rows)

    def replay(self, before):
        # Reuse the established image checker, not the TEMP capture algorithm.
        return Journal.replay(self,before)


def native_authorize(action, table, column, database, trigger):
    if database=='temp' or (database=='main' and table=='reserved'): return apsw.SQLITE_DENY
    return authorize(action,table,column,database,trigger)


def operation(db, capture, statements, *, rollback=False, instruction_limit=None):
    capture.clear()
    db.execute('BEGIN IMMEDIATE')
    instructions=0
    def progress():
        nonlocal instructions
        instructions+=100
        return bool(capture.failure is not None or (instruction_limit is not None and instructions>=instruction_limit))
    try:
        db.set_authorizer(native_authorize)
        db.set_progress_handler(progress,100)
        for sql,parameters in statements:
            with closing(db.cursor()) as cursor:
                # APSW can execute several statements in one string. Only one
                # execution per entry is allowed; a second rolls back the first.
                executions=0
                def single_statement(cursor, statement, bindings):
                    nonlocal executions
                    executions+=1
                    if executions>1: raise CaptureFailure('Single statements required')
                    return True
                cursor.set_exec_trace(single_statement)
                for _ in cursor.execute(sql,parameters): pass
            if capture.failure is not None: raise CaptureFailure(capture.failure)
        db.set_authorizer(None)
        db.set_progress_handler(None)
        result={'events':list(capture.events()),'event_count':len(capture.rows),
                'charged_bytes':capture.charged_bytes,'max_trigger_depth':max(capture.depths,default=0),
                'instructions_rounded':instructions}
        db.execute('ROLLBACK' if rollback else 'COMMIT')
        if rollback: capture.clear()  # Native callbacks do not undo themselves.
        return result
    except BaseException as error:
        db.set_authorizer(None)
        db.set_progress_handler(None)
        failure=capture.failure
        if db.in_transaction: db.execute('ROLLBACK')
        capture.clear()
        if failure is not None: raise CaptureFailure(failure) from error
        raise


def correctness_report():
    results={}
    for name,statements in CASES.items():
        with closing(fixture()) as db:
            capture=NativeCapture(db); before=snapshot(db)
            result=operation(db,capture,statements)
            after=snapshot(db)
            if difference(capture.replay(before),after): raise AssertionError('Native capture mismatch: '+name)
            results[name]={'event_count':result['event_count'],'changed_rows':len(difference(before,after)),
                           'max_trigger_depth':result['max_trigger_depth'], 'charged_bytes':result['charged_bytes'],
                           'typed_images':json_value(result['events'])}
    journal=journal_correctness()
    if any(results[name]['changed_rows']!=journal[name]['changed_rows'] for name in CASES):
        raise AssertionError('Native and journal changed-row scopes differ')
    return results


def replacement_control():
    with closing(fixture(recursive=False)) as db:
        capture=NativeCapture(db); before=snapshot(db)
        result=operation(db,capture,CASES['replace_unique_key'])
        if difference(capture.replay(before),snapshot(db)): raise AssertionError('Native replacement delete missing')
        if [event[2] for event in result['events']]!=['DELETE','INSERT']: raise AssertionError('Replacement event sequence changed')
        return {'recursive_triggers':False,'native_actions':[event[2] for event in result['events']],
                'missed_rows':0,'temp_journal_control_missed_rows':negative_controls()['recursive_triggers_off']['missed_rows']}


def rollback_control():
    with closing(fixture()) as db:
        capture=NativeCapture(db); before=snapshot(db)
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE records SET value=11 WHERE key='a'")
        attempted=len(capture.rows)
        db.execute('ROLLBACK')
        remaining=len(capture.rows)
        if difference(before,snapshot(db)) or attempted!=2 or remaining!=attempted:
            raise AssertionError('Native rollback control changed')
        capture.clear()
        return {'attempted_events':attempted,'events_after_raw_rollback':remaining,
                'events_after_driver_discard':len(capture.rows),'stored_rows_restored':True}


def direct_access_report():
    with tempfile.TemporaryDirectory() as temp,closing(fixture(Path(temp)/'capture.sqlite')) as managed:
        capture=NativeCapture(managed)
        with closing(sqlite3.connect(managed.filename)) as direct,direct:
            direct.execute("UPDATE records SET value=42 WHERE key='a'")
        if capture.rows: raise AssertionError('Independent connection ran native callback')
        if managed.execute("SELECT value FROM records WHERE key='a'").fetchone()[0]!=42:
            raise AssertionError('Independent edit not refreshed')
        return {'independent_stdlib_write_value':42,'native_events':0,'callbacks_registered_on_direct_connection':False}


def measure(samples):
    import math
    import statistics
    result=journal_measure(samples)
    with closing(fixture()) as db:
        db.execute('DROP TRIGGER record_effect'); capture=NativeCapture(db)
        durations=[]
        for index in range(samples):
            started=time.perf_counter()
            operation(db,capture,[("UPDATE records SET value=? WHERE key='a'",(index,))])
            durations.append((time.perf_counter()-started)*1000)
        result['apsw_preupdate']={'samples':samples,'median_ms':round(statistics.median(durations),4),
                                 'p95_ms':round(sorted(durations)[math.ceil(samples*.95)-1],4)}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.samples<1: parser.error('Positive sample count required')
    report={'recorded_at_utc':datetime.now(timezone.utc).isoformat(),'platform':platform.platform(),
            'python':platform.python_version(),'apsw':apsw.apswversion(),'native_sqlite':apsw.sqlitelibversion(),
            'stdlib_sqlite':sqlite3.sqlite_version,'native_compile_options':list(apsw.compile_options),
            'correctness':correctness_report(),'replacement_control':replacement_control(),
            'rollback_control':rollback_control(),'direct_access':direct_access_report(),
            'warm_in_memory_measurements':measure(args.samples),
            'limitations':['Optional pinned wheel; no installed service/API/format change or managed SQL enablement.',
                'Real-table fixture only; arbitrary schemas, virtual tables, BEFORE/same-row rewrites and incremental BLOB writes unqualified.',
                'Raw images capped at 10000 events and 4 MiB charged payload; callback row copying is not an RSS bound.',
                'Native callbacks survive rollback; driver discards all attempted images on failure/rollback. User savepoints are denied.',
                'No Session object is created on the preupdate connection; coexistence is unsupported.',
                'Bindings use different SQLite builds; timings exclude REST, hooks, durability and cold caches and do not isolate binding overhead.']}
    output=json.dumps(report,indent=2,allow_nan=False)+'\n'
    if args.output: args.output.write_text(output,encoding='utf-8')
    else: print(output,end='')


if __name__=='__main__': main()
