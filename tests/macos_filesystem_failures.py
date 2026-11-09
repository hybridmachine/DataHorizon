"""Opt-in real ENOSPC checks in a private 64-MiB disk image, never the host volume."""
import argparse
import errno
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'service'))
from datahorizon.client import APIError,LocalService,UnknownOutcome
from test_failure_recovery_clients import COUNTER


def run(probe,output):
    if sys.platform!='darwin':raise RuntimeError('This qualification driver requires macOS disk-image tooling')
    work=Path(tempfile.mkdtemp(prefix='dh-filesystem-failures-')).resolve();image=work/'fixture.dmg';mount=work/'volume';mount.mkdir()
    report=dict(platform=platform.platform(),passed=False,cleanup_detached=False,scope='Private 64-MiB HFS+ image only')
    mounted=False;owner=None
    def command(arguments):
        return subprocess.run(arguments,check=True,capture_output=True,timeout=45).stdout
    try:
        command(['hdiutil','create','-size','64m','-fs','HFS+','-volname','DataHorizonFailureFixture',str(image)])
        attached=plistlib.loads(command(['hdiutil','attach','-nobrowse','-noautoopen','-mountpoint',str(mount),'-plist',str(image)]))
        mounted=True
        if not any(entity.get('mount-point') and Path(entity['mount-point']).resolve()==mount for entity in attached['system-entities']):raise RuntimeError('Unexpected disk-image mount')
        if mount.stat().st_dev==work.stat().st_dev or shutil.disk_usage(mount).total>128*1024*1024:raise RuntimeError('Refusing to fill anything except the private bounded volume')
        path=mount/'source.dhwb';hook=work/'counter.py';hook.write_text(COUNTER)
        owner=LocalService(hook);client=owner.client;book=client.open(path,create=True);book.set(1,1,'7')
        before=path.read_bytes();free_before=shutil.disk_usage(mount).free
        filler=mount/'space-fixture';written=0
        try:
            with filler.open('wb',buffering=0) as stream:
                while written<128*1024*1024:written+=stream.write(b'x'*65536)
        except OSError as error:
            if error.errno!=errno.ENOSPC:raise
        else:raise RuntimeError('The bounded image did not produce ENOSPC; no data operation was attempted')
        report.update(volume_bytes=shutil.disk_usage(mount).total,free_before=free_before,free_after_fill=shutil.disk_usage(mount).free,filler_bytes=written)
        exported=command([str(probe.resolve()),str(path),str(mount/'blocked-export.xlsx')]);report['export']=json.loads(exported)
        if report['export']['export_succeeded'] or not report['export']['native_hash_preserved']:raise AssertionError('Full-volume export did not preserve the native source')
        try:book.apply('edit',operation_id='full-volume-edit',cells=[dict(row=1,col=1,source='x'*60000)])
        except UnknownOutcome as error:report['edit']=dict(type='unknown',operation_id=error.operation_id,body=error.body)
        except APIError as error:
            if error.body.get('outcome') not in ('rejected','rolled_back'):raise
            report['edit']=dict(type='known_failure',body=error.body)
        else:raise AssertionError('Full-volume edit unexpectedly committed')
        filler.unlink()
        if path.read_bytes()!=before:raise AssertionError('Failed full-volume edit changed native bytes')
        values=[cell['value'] for cell in book.read(c2=2)['cells']]
        if values!=[7,1]:raise AssertionError('Failed edit or handler effects were retained')
        report['native_inputs_and_hook_count']=values
        if report['edit']['type']=='unknown':
            if book.pending_operation!='full-volume-edit':raise AssertionError('Client discarded the unknown operation ID')
            try:book.outcome('full-volume-edit')
            except UnknownOutcome:report['missing_receipt_remains_unknown']=True
            else:raise AssertionError('Unexpected receipt after the full-volume failure')
        report['passed']=True
    except Exception as error:
        report['error']=str(error)
    finally:
        if owner is not None:owner.close()
        if mounted:
            try:command(['hdiutil','detach',str(mount)]);report['cleanup_detached']=True
            except Exception as error:report.update(passed=False,cleanup_error=str(error),preserved_work_directory=str(work))
        else:report['cleanup_detached']=True
        if report['cleanup_detached']:shutil.rmtree(work)
        output.write_text(json.dumps(report,indent=2)+'\n')
    if not report['passed']:raise RuntimeError(report.get('error') or report.get('cleanup_error'))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--probe',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();print(json.dumps(run(args.probe,args.output),indent=2))
