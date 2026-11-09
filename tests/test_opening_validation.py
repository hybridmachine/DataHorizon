"""Opening retains data checks while using bounded, efficient native storage."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datahorizon.engine import Workbook,EngineError


class OpeningValidationTests(unittest.TestCase):
    def test_long_text_inline_layout_and_independent_sqlite_copy_preserve_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'source.dhwb';copy=Path(temp)/'copy.dhwb'
            with closing(Workbook(path,create=True)) as book:
                sid=book.sheet('Sheet1');identity=book.id
                self.assertEqual(book.db.execute('PRAGMA page_size').fetchone()[0],8192)
            with closing(sqlite3.connect(path)) as db,db:
                db.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'text')",((sid,r,'x'*1024) for r in range(1,1001)))
            # Typical 1-KiB cells previously consumed more than 4 KiB each.
            self.assertLess(path.stat().st_size,2*1024*1024)
            original=hashlib.sha256(path.read_bytes()).digest()
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:db.execute('VACUUM INTO ?', (str(copy),))
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),original)
            for target in (path,copy):
                with closing(Workbook(target,readonly=True)) as book:
                    self.assertEqual(book.id,identity)
                    self.assertEqual(book.read('Sheet1',dict(r1=1000,r2=1000))['cells'][0]['source'],'x'*1024)

    def test_late_coordinate_orphan_and_temporal_damage_reject_without_repair(self):
        for damage in ('coordinate','orphan','date'):
            with self.subTest(damage=damage),tempfile.TemporaryDirectory() as temp:
                path=Path(temp)/'source.dhwb'
                with closing(Workbook(path,create=True)) as book:sid=book.sheet('Sheet1')
                with closing(sqlite3.connect(path)) as db,db:
                    db.executemany("INSERT INTO cells(sheet_id,row,col,source,kind) VALUES(?,?,1,?,'text')",((sid,r,'kept') for r in range(1,1001)))
                    db.execute('PRAGMA ignore_check_constraints=ON')
                    if damage=='coordinate':db.execute('UPDATE cells SET row=1000.5 WHERE row=1000')
                    elif damage=='orphan':db.execute("UPDATE cells SET sheet_id='missing' WHERE row=1000")
                    else:
                        db.execute('UPDATE dh_meta SET format_version=2')
                        db.execute("UPDATE cells SET kind='date',source='2023-02-29' WHERE row=1000")
                original=path.read_bytes()
                with self.assertRaises(EngineError) as failure:Workbook(path,readonly=True)
                self.assertEqual(failure.exception.body['code'],'INVALID_INPUT' if damage=='date' else 'INVALID_FORMAT')
                self.assertEqual(path.read_bytes(),original)
