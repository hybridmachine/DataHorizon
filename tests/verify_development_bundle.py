"""Verify actual macOS resource damage is repaired by the development signer."""
import argparse
import hashlib
import pathlib
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=pathlib.Path, required=True)
    parser.add_argument('--script', type=pathlib.Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='datahorizon-signature-') as folder:
        bundle = pathlib.Path(folder) / 'DataHorizon.app'
        shutil.copytree(args.bundle, bundle, symlinks=True)
        executable = bundle / 'Contents/MacOS/DataHorizon'
        verify = ['codesign', '--verify', '--deep', '--strict', str(bundle)]
        subprocess.run(verify, check=True)
        resource = bundle / 'Contents/Resources/datahorizon.icns'
        with resource.open('ab') as stream:
            stream.write(b'\nresource-change-regression\n')
        assert subprocess.run(verify, capture_output=True).returncode != 0
        command = ['cmake', f'-DAPP_BUNDLE={bundle}', '-P', str(args.script)]
        subprocess.run(command, check=True)
        subprocess.run(verify, check=True)
        sealed = hashlib.sha256(executable.read_bytes()).digest()
        subprocess.run(command, check=True)
        assert hashlib.sha256(executable.read_bytes()).digest() == sealed
    print('Damaged resource envelope rejected, repaired and verified; resealing is idempotent.')


if __name__ == '__main__':
    main()
