# SPDX-License-Identifier: GPL-3.0-only
"""Repeatable platform-local build, native automation, relocation and archive evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

from preview_package import package
from package_smoke import smoke

PLATFORMS=('Darwin','Windows','Linux')
MANUAL_GATES=('physical keyboard/clipboard/native dialogs','screen-reader navigation','multiple monitors and system appearance','clean-machine installer/runtime','signing/notarization','distribution licenses/notices')


def matrix(reports):
    result={'platforms':{name:'pending' for name in PLATFORMS},'manual_gates':list(MANUAL_GATES),'release_qualified':False}
    candidates=set(); seen=set()
    for report in reports:
        name=report['platform']
        if name not in PLATFORMS or name in seen: raise ValueError('Unknown or duplicate platform evidence')
        seen.add(name)
        if report.get('automation_passed') is not True: raise ValueError('Failed automation cannot qualify a platform')
        candidates.add(report['candidate'])
        result['platforms'][name]='local automation passed; manual qualification pending'
    if len(candidates)>1: raise ValueError('Platform reports refer to different source candidates')
    return result


def candidate(root):
    digest=hashlib.sha256()
    paths=[root/'CMakeLists.txt',root/'COPYING',root/'LICENSES.md']
    for directory in ('desktop','service','tests','cmake','tools','packaging'):
        paths.extend(path for path in (root/directory).rglob('*') if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc')
    for path in sorted(paths,key=lambda path:path.relative_to(root).as_posix()):
        relative=path.relative_to(root).as_posix()
        # Git's clean conversion normalizes checkout line endings across systems,
        # while hashing current (including uncommitted) bytes without writing objects.
        value=subprocess.check_output(['git','hash-object','--path='+relative,str(path)],cwd=root,text=True).strip()
        digest.update(relative.encode()); digest.update(b'\0'); digest.update(value.encode())
    return digest.hexdigest()


def qualify(build,output,config):
    root=Path(__file__).resolve().parents[1]; build=build.resolve(); output=output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    report={'format':1,'platform':platform.system(),'candidate':None,'automation_passed':False,'steps':[]}
    def run(name,command,environment=None,timeout=600):
        start=time.monotonic()
        with (output/(name+'.txt')).open('w') as log:
            result=subprocess.run(command,cwd=root,env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
        report['steps'].append({'name':name,'seconds':round(time.monotonic()-start,2),'returncode':result.returncode})
        if result.returncode: raise RuntimeError(name+' failed; see '+str(output/(name+'.txt')))
    try:
        report['candidate']=candidate(root)
        run('build',['cmake','--build',str(build),'--config',config,'--parallel','2'])
        run('contracts',['ctest','--test-dir',str(build),'-C',config,'--output-on-failure'])
        binary=build/config if (build/config/('native_acceptance.exe' if os.name=='nt' else 'native_acceptance')).exists() else build
        executable=binary/('native_acceptance.exe' if os.name=='nt' else 'native_acceptance')
        for scale in ('1','2'):
            env=dict(os.environ); env.pop('QT_QPA_PLATFORM',None); env['QT_SCALE_FACTOR']=scale
            target=output/('native-'+scale+'.json')
            run('native-'+scale,[str(executable),str(target)],env,timeout=60)
            native=json.loads(target.read_text())
            expected={'Darwin':{'cocoa'},'Windows':{'windows'},'Linux':{'xcb','wayland'}}[platform.system()]
            if native['platform_plugin'] not in expected or native['scale_request']!=scale: raise ValueError('Native evidence platform/scale mismatch')
        prefix=output/'installed'
        run('install',['cmake','--install',str(build),'--config',config,'--prefix',str(prefix)])
        report['package_smoke']=smoke(prefix)
        archive=output/'DataHorizon-preview.tar.gz'; manifest=package(prefix,archive,build/'build-toolchain.json')
        with archive.open('rb') as source: archive_digest=hashlib.file_digest(source,'sha256').hexdigest()
        report['archive']={'name':archive.name,'files':len(manifest['files']),'sha256':archive_digest,'inventory_verified':True}
        if report['candidate']!=candidate(root): raise ValueError('Source changed during qualification; rerun the candidate')
        report['automation_passed']=True
        report['matrix']=matrix([report])
    except Exception as error:
        report['error']=str(error)
        raise
    finally:
        (output/'qualification.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--build',type=Path,default=Path('build'));parser.add_argument('--output-dir',required=True,type=Path);parser.add_argument('--config',default='Release')
    args=parser.parse_args(); result=qualify(args.build,args.output_dir,args.config)
    print(json.dumps({'automation_passed':result['automation_passed'],'platform':result['platform'],'release_qualified':False}))
