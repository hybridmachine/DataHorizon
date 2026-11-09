# SPDX-License-Identifier: GPL-3.0-only
"""Package integrity and truthful qualification gates; no Qt installation needed."""
import json
import stat
from pathlib import Path
import tarfile
import tempfile
import subprocess
import unittest

from preview_package import inventory,package,verify_archive
from qualify_preview import matrix,candidate


class PackagingTests(unittest.TestCase):
    def test_archive_preserves_inventory_modes_and_local_only_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'installed'
            for name,value in [('bin/DataHorizon',b'executable'),('share/datahorizon/service/datahorizon/launch.py',b'# launcher'),('share/datahorizon/licenses/GUI-GPLv3.txt',b'license')]:
                path=root/name; path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(value)
            (root/'bin/DataHorizon').chmod(0o755)
            toolchain=Path(temp)/'toolchain.json';toolchain.write_text('{"qt":"fixture"}')
            output=Path(temp)/'preview.tar.gz'; manifest=package(root,output,toolchain)
            self.assertFalse(manifest['release_qualified'])
            with tarfile.open(output) as archive:
                self.assertEqual(archive.getmember('DataHorizon-preview/bin/DataHorizon').mode,
                                 stat.S_IMODE((root/'bin/DataHorizon').stat().st_mode))
                stored=json.load(archive.extractfile('DataHorizon-preview/package-manifest.json'))
                self.assertEqual(stored['files'],manifest['files'])
            manifest['files'][0]['sha256']='invalid'
            with self.assertRaises(ValueError): verify_archive(output,manifest)

    def test_missing_launcher_or_external_symlink_rejects_packaging(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'installed';root.mkdir()
            with self.assertRaises(ValueError): inventory(root)
            foreign=Path(temp)/'foreign';foreign.write_text('outside')
            try: (root/'escape').symlink_to(foreign)
            except OSError: return # Windows without symlink privileges still checks missing launcher.
            with self.assertRaisesRegex(ValueError,'External package symlink'): inventory(root)

    def test_missing_platforms_and_manual_gates_never_become_qualified(self):
        report={'platform':'Darwin','candidate':'one','automation_passed':True}
        result=matrix([report]);self.assertEqual(result['platforms']['Windows'],'pending');self.assertEqual(result['platforms']['Linux'],'pending')
        self.assertFalse(result['release_qualified']);self.assertTrue(result['manual_gates'])
        with self.assertRaises(ValueError): matrix([report,{**report,'platform':'Windows','candidate':'two'}])
        with self.assertRaises(ValueError): matrix([report,report])
        with self.assertRaises(ValueError): matrix([{**report,'automation_passed':False}])

    def test_candidate_normalizes_git_checkout_line_endings(self):
        with tempfile.TemporaryDirectory() as temp:
            values=[]
            for name,newline,conversion in (("lf","\n","false"),("crlf","\r\n","true")):
                root=Path(temp)/name;root.mkdir()
                subprocess.run(['git','init','--quiet',str(root)],check=True)
                subprocess.run(['git','-C',str(root),'config','core.autocrlf',conversion],check=True)
                (root/'cmake').mkdir()
                (root/'CMakeLists.txt').write_bytes(('project(Test)'+newline).encode())
                (root/'COPYING').write_bytes(('License'+newline).encode())
                (root/'LICENSES.md').write_bytes(('Scope'+newline).encode())
                (root/'cmake'/'test.in').write_bytes(('set(X 1)'+newline).encode())
                values.append(candidate(root))
            self.assertEqual(values[0],values[1])
