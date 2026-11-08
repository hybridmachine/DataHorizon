from contextlib import closing
from pathlib import Path
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook,EngineError
from datahorizon.metadata import read
from datahorizon.client import LocalService


class PortableMetadataTests(unittest.TestCase):
    def test_native_upgrade_reopen_copy_duplicate_and_invalid_atomicity(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'view.dhwb'
            with closing(Workbook(path,create=True)) as book:
                def apply(action='metadata_set',**args):return book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=book.state(),**args))
                view=dict(rows={'0':40},columns={'1':180},hidden_rows=[3],frozen_rows=1,named_views={'Positive':dict(r2=3,filters=[dict(col=1,op='>',value=0)])})
                apply(namespace='presentation',id='view',payload=view)
                self.assertEqual(book.info()['format_version'],3)
                self.assertEqual(read(book,'Sheet1',dict(expected_state=book.state()))['objects'][0]['payload'],view)
                state=book.state()
                with self.assertRaises(EngineError):apply(namespace='presentation',id='view',payload=dict(frozen_rows=999))
                self.assertEqual(book.state(),state)
                apply('sheet_duplicate',sheet='Sheet1',name='Copy')
                self.assertEqual(read(book,'Copy',dict(expected_state=book.state()))['objects'][0]['payload'],view)
                book.snapshot(Path(temp)/'snapshot.dhwb')
            for opened in (path,Path(temp)/'snapshot.dhwb'):
                with closing(Workbook(opened,readonly=True)) as book:self.assertEqual(read(book,'Sheet1',dict(expected_state=book.state()))['objects'][0]['payload'],view)

    def test_rest_sdk_named_list_and_read_only(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'rest.dhwb'
            with client.open(path,create=True) as book:
                spec=dict(r2=10,c2=2,filters=[dict(col=2,op='>=',value='2024-02-29',value_kind='date')])
                book.set_metadata('list','Dates',spec)
                self.assertEqual(book.metadata()['objects'][0]['id'],'Dates')
            with client.open(path,readonly=True) as book:self.assertEqual(book.metadata()['objects'][0]['namespace'],'list')
