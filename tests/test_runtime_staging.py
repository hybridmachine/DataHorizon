# SPDX-License-Identifier: GPL-3.0-only
"""Pinned archives must be verified before they can alter a runtime destination."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('stage_runtime', Path(__file__).resolve().parents[1] / 'tools/stage_runtime.py')
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)


class RuntimeStagingTests(unittest.TestCase):
    def test_wrong_digest_leaves_destination_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); archive = root / 'archive'; archive.write_bytes(b'not a tar')
            manifest = root / 'manifest'; manifest.write_text(json.dumps({'sha256':'0'*64}))
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                staging.stage(archive, root / 'runtime', manifest)
            self.assertFalse((root / 'runtime').exists())

    def test_archive_cannot_write_outside_its_runtime(self):
        for name, link in [('python/../../escape', None), ('python/escape', '../../escape')]:
            with self.subTest(name=name, link=link), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); archive = root / 'archive.tar.gz'
                with tarfile.open(archive, 'w:gz') as output:
                    entry = tarfile.TarInfo(name)
                    if link:
                        entry.type = tarfile.SYMTYPE; entry.linkname = link
                        output.addfile(entry)
                    else:
                        entry.size = 6; output.addfile(entry, io.BytesIO(b'escape'))
                manifest = root / 'manifest'; manifest.write_text(json.dumps({'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}))
                with self.assertRaises((ValueError, tarfile.FilterError)):
                    staging.stage(archive, root / 'runtime', manifest)
                self.assertFalse((root / 'escape').exists())
                self.assertFalse((root / 'runtime').exists())


if __name__ == '__main__':
    unittest.main()
