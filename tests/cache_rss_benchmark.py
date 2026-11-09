# SPDX-License-Identifier: GPL-3.0-only
"""Fresh-process/warm REST and per-process peak-RSS measurements, no OS cache purge."""
import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import queue
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time

# This launcher is invoked by absolute trusted path in isolated mode.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'service'))
from datahorizon.client import Client
from datahorizon.service import Service


def peak_rss():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb',wintypes.DWORD),('faults',wintypes.DWORD)] + [
                (name,ctypes.c_size_t) for name in ('peak','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile')]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        psapi=ctypes.WinDLL('psapi',use_last_error=True)
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype=wintypes.BOOL
        counters=Counters(); counters.cb=ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.peak)
    import resource
    value=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform=='darwin' else value*1024


class MeteredService(Service):
    # Test-only authenticated route; never installed or exposed by normal service.
    def dispatch(self,method,path,body):
        if method=='GET' and path=='/benchmark/memory':
            return {'pid':os.getpid(),'peak_rss_bytes':peak_rss()}
        return super().dispatch(method,path,body)


@contextmanager
def measured_service():
    with tempfile.TemporaryFile(mode='w+t',encoding='utf-8') as diagnostics:
        process=subprocess.Popen([sys.executable,'-I','-B','-X','utf8',str(Path(__file__).resolve()),'--serve'],
                                 stdout=subprocess.PIPE,stderr=diagnostics,text=True,encoding='utf-8')
        startup=queue.Queue()
        reader=threading.Thread(target=lambda:startup.put(process.stdout.readline()),daemon=True); reader.start()
        try:
            info=json.loads(startup.get(timeout=15))
            yield Client(info['url'],info['token'])
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            reader.join(timeout=1); process.stdout.close()


def distribution(values):
    return {'samples':len(values),'median_ms':round(statistics.median(values),3),
            'p95_ms':round(sorted(values)[math.ceil(len(values)*.95)-1],3)}


def purge_os_cache():
    if sys.platform!='darwin':
        raise RuntimeError('OS cache purge is currently supported only by the macOS qualification driver')
    result=subprocess.run(['sudo','-n','/usr/sbin/purge'],capture_output=True,text=True,timeout=15)
    if result.returncode:
        raise RuntimeError('macOS cache purge failed: '+(result.stderr.strip() or result.stdout.strip()))


def measurements(samples,disk_rows,sparse_rows=100_000,*,purge=None):
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'memory.dhwb'
        with measured_service() as client:
            book=client.open(path,create=True)
            book.apply('sheet_add',name='Disk')
            sheets={s['name']:s['id'] for s in book.refresh()['sheets']}
            book.close()
        with closing(sqlite3.connect(path)) as direct,direct:
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'number')",
                               ((sheets['Sheet1'],r,str(r)) for r in range(1,sparse_rows+1)))
            direct.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'text')",
                               ((sheets['Disk'],r,'x'*1024) for r in range(1,disk_rows+1)))
        cold,first,peaks=[],[],[]
        for sample in range(samples):
            # Purge only after all fixture writes/metadata probes have finished.
            # A failed purge aborts instead of relabeling warm data as OS-cold.
            if purge is not None:purge()
            started=time.perf_counter()
            with measured_service() as client:
                book=client.open(path,readonly=True)
                read_started=time.perf_counter()
                cells=book.read(sheet='Disk',r1=max(1,disk_rows-31),r2=disk_rows,c1=1,c2=16)['cells']
                first.append((time.perf_counter()-read_started)*1000)
                cold.append((time.perf_counter()-started)*1000)
                if len(cells)!=min(32,disk_rows) or any(c['value']!='x'*1024 for c in cells):
                    raise AssertionError('Fresh-process read changed stored data')
                peaks.append(client.request('/benchmark/memory')['peak_rss_bytes'])
                book.close()
            if (sample+1)%10==0: print(f'Fresh process {sample+1}/{samples}',file=sys.stderr,flush=True)
        with measured_service() as client:
            book=client.open(path,readonly=True)
            warm=[]
            for _ in range(samples):
                started=time.perf_counter()
                cells=book.read(sheet='Disk',r1=max(1,disk_rows-31),r2=disk_rows,c1=1,c2=16)['cells']
                warm.append((time.perf_counter()-started)*1000)
                if len(cells)!=min(32,disk_rows) or any(c['value']!='x'*1024 for c in cells):
                    raise AssertionError('Warm viewport changed stored data')
            peaks.append(client.request('/benchmark/memory')['peak_rss_bytes'])
            book.close()
        target=256*1024*1024
        return {'dataset':{'sparse_cells':sparse_rows,'long_text_cells':disk_rows,'database_bytes':path.stat().st_size,
                           'larger_than_service_memory_target':path.stat().st_size>target},
                'fresh_process_start_open_first_read':distribution(cold),'fresh_process_first_viewport_only':distribution(first),
                'viewport_scope':[max(1,disk_rows-31),1,disk_rows,16],
                'warm_viewport':distribution(warm),'service_peak_rss_bytes':max(peaks),
                'driver_peak_rss_bytes':peak_rss(),'service_memory_target_bytes':target,
                'measured_service_within_target':max(peaks)<target,
                'os_cache_purge_before_every_fresh_sample':purge is not None,
                'cache_state':'purged OS caches before each fresh process' if purge is not None else 'uncontrolled OS caches'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--disk-rows',type=int,default=270_000)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--purge-os-cache',action='store_true',help='Require successful noninteractive macOS cache purge before every fresh sample; fails explicitly when unavailable')
    args=parser.parse_args()
    if args.serve:
        server=MeteredService()
        print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}','token':server.token}),flush=True)
        try: server.serve_forever()
        finally: server.server_close()
        return
    if args.samples<1 or not 1<=args.disk_rows<=1_048_576:
        parser.error('Positive samples and 1..1048576 disk rows required')
    report={'recorded_at_utc':datetime.now(timezone.utc).isoformat(),'platform':platform.platform(),
            'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,
            'limitations':['Native validation may warm SQLite pages before the first viewport; opening plus first read is the end-to-end metric.',
                           'Peak RSS/working set is per owned service process; no worker processes are active in this fixture.',
                           'Dataset-specific memory evidence is not a bound on all workbook shapes or multiple open engines.']}
    try:report.update(measurements(args.samples,args.disk_rows,purge=purge_os_cache if args.purge_os_cache else None))
    except RuntimeError as error:
        report.update(qualification_passed=False,error=str(error),os_cold_qualified=False)
        if args.output:args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        raise
    output=json.dumps(report,indent=2)+'\n'
    if args.output: args.output.write_text(output,encoding='utf-8')
    else: print(output,end='')


if __name__=='__main__': main()
