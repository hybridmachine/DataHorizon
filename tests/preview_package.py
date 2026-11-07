"""Create a local development archive with integrity inventory; no release qualification."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import stat
import subprocess
import tarfile


def inventory(prefix):
    prefix=Path(prefix).resolve(); entries=[]
    for path in sorted(prefix.rglob("*")):
        name=path.relative_to(prefix).as_posix()
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(prefix): raise ValueError("External package symlink: "+name)
            entries.append({"path":name,"symlink":str(path.readlink())})
        elif path.is_file():
            digest=hashlib.sha256()
            with path.open('rb') as source:
                for block in iter(lambda:source.read(1024*1024),b''): digest.update(block)
            entries.append({"path":name,"size":path.stat().st_size,"mode":stat.S_IMODE(path.stat().st_mode),"sha256":digest.hexdigest()})
    names={e['path'] for e in entries}
    if not any(n.endswith('/service/datahorizon/launch.py') for n in names): raise ValueError('Installed service launcher is missing')
    if not any(n.endswith('/licenses/GUI-GPLv3.txt') for n in names): raise ValueError('GUI license text is missing')
    if not any(n in ('bin/DataHorizon','bin/DataHorizon.exe','DataHorizon.app/Contents/MacOS/DataHorizon') for n in names): raise ValueError('Application executable is missing')
    return entries


def package(prefix,output,toolchain):
    prefix=Path(prefix).resolve(); output=Path(output).absolute()
    if output.is_relative_to(prefix): raise ValueError('Archive output must be outside the installed prefix')
    entries=inventory(prefix)
    manifest={'format':1,'kind':'local-development-preview','platform':platform.system(),
              'toolchain':json.loads(Path(toolchain).read_text()),'files':entries,'release_qualified':False,
              'open_distribution_gates':['service/SDK licenses','dependency notices and redistribution review','runtime selection/bundling','signing and installers','clean-machine and platform/manual acceptance']}
    try:
        manifest['source_head']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=Path(__file__).resolve().parents[1],text=True).strip()
        manifest['source_dirty']=bool(subprocess.check_output(['git','status','--porcelain'],cwd=Path(__file__).resolve().parents[1],text=True).strip())
    except (OSError,subprocess.CalledProcessError): manifest['source_head']=None
    with tarfile.open(output,'x:gz',dereference=False) as archive:
        # Inventory traversal has already fenced symlink targets. Preserve Qt framework links.
        archive.add(prefix,arcname='DataHorizon-preview')
        data=(json.dumps(manifest,indent=2)+'\n').encode(); info=tarfile.TarInfo('DataHorizon-preview/package-manifest.json'); info.size=len(data); info.mode=0o644
        archive.addfile(info,io.BytesIO(data))
    verify_archive(output,manifest)
    return manifest


def verify_archive(output,manifest):
    expected={entry['path']:entry for entry in manifest['files']}; seen=set()
    with tarfile.open(output,'r:gz') as archive:
        with archive.extractfile('DataHorizon-preview/package-manifest.json') as source: stored=json.load(source)
        if stored!=manifest: raise ValueError('Archive manifest changed')
        for member in archive:
            if member.isdir() or member.name=='DataHorizon-preview/package-manifest.json': continue
            name=member.name.removeprefix('DataHorizon-preview/')
            if name not in expected or name in seen: raise ValueError('Unexpected or duplicate archive member')
            seen.add(name); item=expected[name]
            if 'symlink' in item:
                if not member.issym() or member.linkname!=item['symlink']: raise ValueError('Archive symlink changed')
            else:
                if member.issym() or member.mode!=item['mode']: raise ValueError('Archive file mode changed')
                with archive.extractfile(member) as source:
                    digest=hashlib.file_digest(source,'sha256').hexdigest()
                if digest!=item['sha256'] or member.size not in (0,item['size']): raise ValueError('Archive content changed')
    if seen!=set(expected): raise ValueError('Archive is missing inventoried files')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('prefix',type=Path);parser.add_argument('output',type=Path);parser.add_argument('--toolchain',required=True,type=Path)
    args=parser.parse_args(); result=package(args.prefix,args.output,args.toolchain)
    print(json.dumps({'archive':args.output.name,'files':len(result['files']),'release_qualified':False}))
