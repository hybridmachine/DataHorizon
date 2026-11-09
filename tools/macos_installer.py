# SPDX-License-Identifier: GPL-3.0-only
"""Build and verify a development DMG; never claims signed-release qualification."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile


def command(*args):
    return subprocess.check_output([str(a) for a in args], text=True, stderr=subprocess.STDOUT)


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def macho(path):
    with path.open('rb') as stream:
        return stream.read(4) in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca')


def audit(bundle):
    """Resolve non-system Mach-O dependencies within their relocated bundle."""
    bundle = bundle.resolve()
    binaries = [p for p in bundle.rglob('*') if p.is_file() and not p.is_symlink() and macho(p)]
    main = bundle / 'Contents/MacOS/DataHorizon'
    runtime_main = bundle / 'Contents/Resources/runtime/bin/python3'
    def paths(binary):
        return re.findall(r'cmd LC_RPATH\n\s*cmdsize \d+\n\s*path (.*?) \(offset', command('otool', '-l', binary))
    def expand(value, owner, executable):
        return Path(value.replace('@loader_path', str(owner.parent)).replace('@executable_path', str(executable.parent)))
    for binary in binaries:
        details = command('otool', '-l', binary)
        identifiers = re.findall(r'cmd LC_ID_DYLIB\n\s*cmdsize \d+\n\s*name (.*?) \(offset', details)
        executable = runtime_main if binary.is_relative_to(bundle/'Contents/Resources/runtime') else main
        rpaths = [expand(p, binary, executable) for p in paths(binary)]
        rpaths += [expand(p, executable, executable) for p in paths(executable)]
        for dependency in command('otool', '-L', binary).splitlines()[1:]:
            if ' (compatibility version' not in dependency:
                continue  # Universal binaries repeat per-architecture headings.
            dependency = dependency.strip().split(' (compatibility')[0]
            if dependency in identifiers:
                continue  # A dylib's own install ID is not a loaded dependency.
            if dependency.startswith(('/System/Library/', '/usr/lib/')):
                continue
            candidates = [p/dependency[len('@rpath/'):] for p in rpaths] if dependency.startswith('@rpath/') else [expand(dependency, binary, executable)]
            if not any(p.exists() and p.resolve().is_relative_to(bundle) for p in candidates):
                raise ValueError(f'Unresolved/external dependency in {binary.relative_to(bundle)}: {dependency}')
        versions = re.findall(r'\bminos (\d+(?:\.\d+)*)', details)
        if any(tuple(map(int, version.split('.'))) > (15, 0, 0) for version in versions):
            raise ValueError(f'Binary exceeds macOS 15 baseline: {binary.relative_to(bundle)} {versions}')
    command('codesign', '--verify', '--deep', '--strict', bundle)
    return {'macho_files':len(binaries), 'all_non_system_dependencies_internal':True, 'strict_signature_verified':True}


def build(prefix, output, dependency_dir, root):
    if sys.platform != 'darwin':
        raise ValueError('The native DMG builder requires macOS')
    if command('git', '-C', root, 'status', '--porcelain').strip():
        raise ValueError('Commit all source changes before building a source-bound installer')
    head = command('git', '-C', root, 'rev-parse', 'HEAD').strip()
    app = prefix / 'DataHorizon.app'
    provenance = json.loads((app/'Contents/Resources/source.json').read_text())
    if provenance['source_commit'] != head:
        raise ValueError('Installed bundle is from another source commit; reconfigure/build/install')
    result = {'source_commit':head, 'platform':'macOS 15+ ARM64', 'audit':audit(app),
              'developer_id_signed':False, 'notarized':False, 'clean_machine_qualified':False,
              'release_qualified':False, 'files':[]}
    output.mkdir(parents=True, exist_ok=False)
    source = output/'DataHorizon-source.tar.gz'
    subprocess.run(['git','-C',str(root),'archive','--format=tar.gz','--prefix=DataHorizon/','--output='+str(source),head],check=True)
    dependencies = json.loads((root/'packaging/qt/6.12.0/sources.json').read_text())
    runtime = json.loads((root/'packaging/runtime-macos-arm64.json').read_text())
    dependencies += [{'url':runtime['provenance_archive_url'],'sha256':runtime['provenance_archive_sha256']},
                     {'url':'https://www.python.org/ftp/python/3.13.18/Python-3.13.18.tar.xz',
                      'sha256':'abb0102e6d4ae027509bcb82327d9dd85515b44a3ba8ad538cfc5e9847029f68'}]
    from urllib.parse import unquote, urlparse
    for item in dependencies:
        name = unquote(Path(urlparse(item['url']).path).name)
        archive = dependency_dir/name
        # The runtime provenance download may have a convenient local alias.
        if not archive.exists() and name.startswith('cpython-'):
            archive = dependency_dir/'cpython-3.13.18-full.tar.zst'
        if sha256(archive) != item['sha256']:
            raise ValueError('Dependency source checksum differs: '+name)
        shutil.copyfile(archive, output/name)
    temp = tempfile.mkdtemp(prefix='datahorizon-dmg-')
    safe_to_remove = True
    try:
        stage = Path(temp)/'stage';stage.mkdir()
        shutil.copytree(app,stage/app.name,symlinks=True)
        (stage/'Applications').symlink_to('/Applications',target_is_directory=True)
        (stage/'READ ME.txt').write_text('DataHorizon development preview for macOS 15+ Apple Silicon.\nCopy DataHorizon.app to Applications to install. Workbooks store edits automatically.\nThis ad-hoc signed development image is not Developer ID signed or notarized.\nClean-machine and physical/VoiceOver acceptance remain pending.\nSee the accompanying source archives, manifest and bundled licenses.\n')
        image = output/'DataHorizon-macOS-arm64-development.dmg'
        command('hdiutil','create','-quiet','-format','UDZO','-volname','DataHorizon Development','-srcfolder',stage,image)
        mount = Path(temp)/'mounted';mount.mkdir()
        attached = False
        try:
            safe_to_remove = False
            receipt = plistlib.loads(subprocess.check_output(['hdiutil','attach','-readonly','-nobrowse','-mountpoint',str(mount),'-plist',str(image)]))
            attached = any(Path(e.get('mount-point','/')).resolve()==mount.resolve() for e in receipt['system-entities'])
            if not attached:
                raise ValueError('Owned disk image mount was not confirmed')
            result['mounted_audit'] = audit(mount/app.name)
            if (mount/'Applications').readlink()!=Path('/Applications'):
                raise ValueError('Installer application destination link differs')
            # Copy the mounted app into a private installation prefix, then use
            # the same relocated GUI/CLI/SDK smoke contract as ordinary installs.
            installed=Path(temp)/'installed';installed.mkdir()
            shutil.copytree(mount/app.name,installed/app.name,symlinks=True)
            smoke_report=Path(temp)/'smoke.json'
            command(sys.executable,root/'tests/package_smoke.py',installed,'--output',smoke_report)
            result['mounted_copy_smoke']=json.loads(smoke_report.read_text())
        finally:
            if attached:
                command('hdiutil','detach',mount)
                safe_to_remove = True
    finally:
        if safe_to_remove:
            shutil.rmtree(temp)
        else:
            print('Disk-image cleanup needs inspection; preserved '+temp, file=sys.stderr)
    for path in sorted(output.iterdir()):
        result['files'].append({'name':path.name,'bytes':path.stat().st_size,'sha256':sha256(path)})
    result['limits']=['No Developer ID identity or notarization credentials available',
                     'Mounted/relocated smoke is not an actual clean-machine run',
                     'Full dependency source/build and distribution review remains a release gate']
    (output/'manifest.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dependency-dir',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.prefix.resolve(),args.output.resolve(),args.dependency_dir.resolve(),Path(__file__).resolve().parents[1]),indent=2))
