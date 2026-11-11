# SPDX-License-Identifier: GPL-3.0-only
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datahorizon import query_storage

class QueryStorageTests(unittest.TestCase):
    def test_restart_only_removes_owned_well_formed_directories_of_proven_dead_services(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);nonce='a'*32
            def directory(pid,suffix,marker=True):
                path=root/f'datahorizon-query-{pid}-{nonce}-{suffix}';path.mkdir()
                if marker:(path/'owner.json').write_text(json.dumps(dict(pid=pid,nonce=nonce)))
                (path/'snapshot.sqlite').write_bytes(b'private');return path
            dead=directory(101,'dead');live=directory(102,'live');unproven=directory(103,'unknown',False);mismatched=directory(104,'mismatch');(mismatched/'owner.json').write_text('{}')
            with patch.object(query_storage.tempfile,'gettempdir',return_value=temp),patch.object(query_storage,'alive',side_effect=lambda pid:pid!=101):query_storage.cleanup_terminated()
            self.assertFalse(dead.exists());self.assertTrue(live.exists());self.assertTrue(unproven.exists());self.assertTrue(mismatched.exists())
    def test_private_creation_and_release_own_the_snapshot_directory(self):
        directory=query_storage.create();path=Path(directory.name)
        try:
            self.assertEqual(json.loads((path/'owner.json').read_text())['pid'],os.getpid())
            if os.name!='nt':self.assertEqual(path.stat().st_mode&0o777,0o700)
        finally:directory.cleanup()
        self.assertFalse(path.exists())
