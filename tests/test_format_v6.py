# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from datahorizon.calculation_store import TABLES as CALC_TABLES
from datahorizon.engine import Workbook,EngineError
from datahorizon.workbook_code import fingerprint


def format_version(book):
    return book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]


class FormatV6Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'v6.dhwb'

    def new_book(self):
        book=Workbook(self.path,create=True);self.addCleanup(book.close);return book

    def save_module(self,book,name,source):
        return book.mutate(dict(action='module_save',operation_id=str(uuid.uuid4()),expected_state=book.state(),
                                fingerprint=fingerprint(book.db),name=name,source=source,macros=[]))

    def raw(self,statement,*args):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(statement,args);db.commit()

    def downgrade_to(self,version):
        with closing(sqlite3.connect(self.path)) as db:
            for table in CALC_TABLES:db.execute(f'DROP TABLE IF EXISTS {table}')
            db.execute('UPDATE dh_meta SET format_version=?',(version,));db.commit()

    def test_first_module_save_enters_v5(self):
        book=self.new_book();self.save_module(book,'m','def f(ctx):\n    return 1\n')
        self.assertEqual(format_version(book),9)

    def test_module_save_does_not_downgrade_v6(self):
        book=self.new_book();self.save_module(book,'m','def f(ctx):\n    return 1\n')
        book.close();self.downgrade_to(6)
        book=Workbook(self.path);self.addCleanup(book.close)
        self.save_module(book,'n','def g(ctx):\n    return 2\n')
        self.assertEqual(format_version(book),9)

    def test_v6_file_opens(self):
        book=self.new_book();self.save_module(book,'m','def f(ctx):\n    return 1\n');book.close()
        self.downgrade_to(6)
        reopened=Workbook(self.path);self.addCleanup(reopened.close);self.assertEqual(format_version(reopened),6)

    def test_v9_is_rejected_on_open(self):
        self.new_book().close();self.raw('UPDATE dh_meta SET format_version=10')
        with self.assertRaises(EngineError) as raised:Workbook(self.path)
        self.assertEqual(raised.exception.body['code'],'FORMAT_VERSION')

    def test_name_kind_rejected_below_v6(self):
        book=self.new_book();self.save_module(book,'m','def f(ctx):\n    return 1\n');book.close()
        self.downgrade_to(5)
        self.raw("INSERT INTO dh_relational VALUES('name',?,?)",str(uuid.uuid4()),'{}')
        with self.assertRaises(EngineError) as raised:Workbook(self.path)
        self.assertEqual(raised.exception.body['code'],'INVALID_FORMAT')


if __name__=='__main__':unittest.main()
