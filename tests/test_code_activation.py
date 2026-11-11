# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook,EngineError
from datahorizon.client import LocalService,APIError
from datahorizon.workbook_code import activation,authorize,fingerprint,approval_keys


class CodeActivationTests(unittest.TestCase):
    def test_approval_belongs_to_service_and_tracks_actual_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'code.dhwb'
            with closing(Workbook(path,create=True)) as book:
                book.mutate(dict(action='module_save',operation_id=str(uuid.uuid4()),expected_state=book.state(),fingerprint=fingerprint(book.db),name='Example',source='x=1',macros=[]))
                key=activation(book)['activation_key'];digest=fingerprint(book.db)
                with self.assertRaises(EngineError) as error:authorize(book,dict(fingerprint=digest,approved_code=[key]))
                self.assertEqual(error.exception.body['code'],'CODE_NOT_APPROVED')
            with LocalService(approved_code=[key]) as client,client.open(path) as book:
                inspected=client.request(book.base+'/code',dict(expected_state=book.state));self.assertTrue(inspected['approved'])
                with self.assertRaises(APIError):client.request(book.base+'/code',dict(expected_state=book.state,approve=True))
            with LocalService() as client,client.open(path) as book:
                self.assertFalse(client.request(book.base+'/code',dict(expected_state=book.state))['approved'])
            with closing(Workbook(path,approved_code=[key])) as book:
                self.assertTrue(authorize(book,dict(fingerprint=digest))['approved'])
                book.mutate(dict(action='edit',operation_id=str(uuid.uuid4()),expected_state=book.state(),cells=[dict(row=1,col=1,source='data only')]))
                self.assertTrue(activation(book)['approved'])
                with closing(sqlite3.connect(path,isolation_level=None)) as db:
                    value=json.loads(db.execute("SELECT payload FROM dh_relational WHERE kind='module'").fetchone()[0]);value['source']='x=2';db.execute("UPDATE dh_relational SET payload=? WHERE kind='module'",(json.dumps(value),))
                book.refresh();self.assertFalse(activation(book)['approved'])
                with self.assertRaises(EngineError) as error:authorize(book,dict(fingerprint=digest))
                self.assertEqual(error.exception.body['code'],'CODE_CHANGED')

    def test_keys_are_explicit_canonical_bounded_and_immutable(self):
        key=str(uuid.uuid4())+':'+'a'*64;values=[key];result=approval_keys(values);values.clear();self.assertEqual(result,frozenset([key]))
        for invalid in (key,['all'],[key.upper()],[True],[key]*257):
            with self.assertRaises(EngineError):approval_keys(invalid)


if __name__=='__main__':unittest.main()
