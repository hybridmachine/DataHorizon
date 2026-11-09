# SPDX-License-Identifier: GPL-3.0-only
"""Compare native opening and storage geometry; each open retains its five-second budget."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'service'))
from datahorizon.engine import Workbook,EngineError,encoded
from cache_rss_benchmark import distribution,peak_rss


def fingerprint(db):
    digest=hashlib.sha256(); count=0
    for table,order in (('dh_meta','singleton'),('sheets','id'),('cells','sheet_id,row,col'),('operations','id')):
        for row in db.execute('SELECT * FROM '+table+' ORDER BY '+order):
            digest.update(encoded(row).encode());digest.update(b'\n');count+=table=='cells'
    return digest.hexdigest(),count


def measure(path,samples,last_row):
    times=[];failures=[]
    for _ in range(samples):
        start=time.perf_counter()
        try:
            with closing(Workbook(path,readonly=True)) as book:
                cells=book.read('Sheet1',dict(r1=last_row,r2=last_row,c1=1,c2=1))['cells']
                assert len(cells)==1 and cells[0]['source']=='x'*1024
            times.append((time.perf_counter()-start)*1000)
        except EngineError as error:
            failures.append(dict(code=error.body['code'],milliseconds=round((time.perf_counter()-start)*1000,3)))
    return dict(successful=distribution(times) if times else None,failures=failures)


def benchmark(rows,samples,legacy=None):
    with tempfile.TemporaryDirectory(prefix='dh-opening-') as temp:
        root=Path(temp);old=legacy or root/'legacy.dhwb';new=root/'optimized.dhwb'
        if legacy is None:
            with closing(Workbook(old,create=True)) as book:sid=book.sheet('Sheet1')
            with closing(sqlite3.connect(old)) as db:
                db.execute('PRAGMA page_size=4096');db.execute('VACUUM')
                with db:db.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'text')",((sid,r,'x'*1024) for r in range(1,rows+1)))
        with closing(sqlite3.connect(old.as_uri()+'?mode=ro',uri=True)) as db:
            before,count=fingerprint(db)
            page=db.execute('PRAGMA page_size').fetchone()[0]
            # An explicit, independent SQLite maintenance copy changes physical
            # layout, not source data. It never updates or replaces the source.
            db.execute('PRAGMA page_size=8192');db.execute('PRAGMA synchronous=FULL')
            started=time.perf_counter();db.execute('VACUUM INTO ?', (str(new),));copy_ms=(time.perf_counter()-started)*1000
            assert fingerprint(db)==(before,count)
        with closing(sqlite3.connect(new)) as db:
            assert fingerprint(db)==(before,count)
            assert db.execute('PRAGMA page_size').fetchone()[0]==8192
        return dict(platform=platform.platform(),python=platform.python_version(),sqlite=sqlite3.sqlite_version,
                    cells=count,source_fingerprint=before,logical_content_preserved=True,copy_ms=round(copy_ms,3),
                    legacy=dict(page_bytes=page,file_bytes=old.stat().st_size,**measure(old,samples,count)),
                    optimized=dict(page_bytes=8192,file_bytes=new.stat().st_size,**measure(new,samples,count)),
                    driver_peak_rss_bytes=peak_rss(),opening_budget_seconds=5,
                    limitations=['Fresh connections with uncontrolled OS cache state; this is not OS-cold qualification.',
                                 'Source fingerprints and the maintenance copy warm OS caches before opening measurements.',
                                 'The driver peak includes SQLite copying and fingerprinting; it is not isolated service RSS.',
                                 'Existing native files retain their page size until explicitly copied; no automatic rewrite.'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows',type=int,default=270000);parser.add_argument('--samples',type=int,default=5)
    parser.add_argument('--legacy',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not 1<=args.rows<=1048576 or args.samples<1:parser.error('Positive bounded rows and sample count required')
    report=benchmark(args.rows,args.samples,args.legacy.resolve() if args.legacy else None)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
