# SPDX-License-Identifier: GPL-3.0-only
"""Verify a pinned runtime archive before extracting it; never executes downloads."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile


def stage(archive, destination, manifest):
    metadata = json.loads(Path(manifest).read_text())
    with Path(archive).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != metadata['sha256']:
        raise ValueError('Python archive SHA-256 differs from the pinned release')
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Runtime destination already exists; use a new staging directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='runtime-stage-', dir=destination.parent) as temp:
        with tarfile.open(archive, 'r:gz') as source:
            for member in source.getmembers():
                if not member.name.startswith('python/'):
                    raise ValueError('Unexpected runtime archive root')
            source.extractall(temp, filter='data')
        runtime = Path(temp) / 'python'
        if not (runtime / 'bin/python3').is_file():
            raise ValueError('Runtime archive has no Python interpreter')
        for item in runtime.rglob('*'):
            if item.is_symlink() and not item.resolve().is_relative_to(runtime):
                raise ValueError('Runtime symlink escapes its installation')
        shutil.copyfile(manifest, runtime / 'DATAHORIZON-RUNTIME.json')
        runtime.rename(destination)
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).resolve().parents[1] / 'packaging/runtime-macos-arm64.json')
    args = parser.parse_args()
    print(json.dumps(stage(args.archive, args.destination, args.manifest), indent=2))
