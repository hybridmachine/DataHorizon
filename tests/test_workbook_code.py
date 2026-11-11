# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook,EngineError
from datahorizon.workbook_code import fingerprint,read


SOURCE='def double(ctx, amount):\n    return amount * 2\n'
MACROS=[dict(name='double',parameters=[dict(name='amount',type='integer')],result=dict(type='integer'))]


class WorkbookCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'code.dhwb'
        self.book=Workbook(self.path,create=True);self.addCleanup(self.book.close)

    def apply(self,action,**args):
        return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))

    def save(self,**args):
        return self.apply('module_save',fingerprint=fingerprint(self.book.db),**{**dict(name='Examples',source=SOURCE,macros=MACROS),**args})

    def test_source_never_executes_on_save_read_copy_or_reopen(self):
        result=self.apply('module_save',fingerprint=fingerprint(self.book.db),name='Dormant',source="raise RuntimeError('must not execute')",macros=[])
        self.assertEqual(self.book.info()['format_version'],5)
        inspected=read(self.book,dict(expected_state=self.book.state()));self.assertEqual(inspected['modules'][0]['id'],result['module_id'])
        copy=Path(self.temp.name)/'copy.dhwb';self.book.snapshot(copy)
        reopened=Workbook(copy)
        try:self.assertEqual(read(reopened,dict(expected_state=reopened.state()))['modules'],inspected['modules'])
        finally:reopened.close()

    def test_save_rename_delete_preserve_identity_and_clear_history(self):
        result=self.save();identity=result['module_id'];before=fingerprint(self.book.db)
        self.apply('edit',cells=[dict(row=1,col=1,source='1')]);self.assertTrue(self.book.undo)
        renamed=self.apply('module_save',module=identity,name='Renamed',source=SOURCE,macros=MACROS,fingerprint=before)
        self.assertEqual(renamed['module_id'],identity);self.assertNotEqual(before,fingerprint(self.book.db));self.assertEqual(self.book.undo,[])
        self.apply('module_delete',module=identity,fingerprint=fingerprint(self.book.db));self.assertEqual(read(self.book,dict(expected_state=self.book.state()))['modules'],[])
        self.assertEqual(self.book.info()['format_version'],5)

    def test_invalid_declarations_and_review_races_roll_back_migration(self):
        cases=[dict(name='class',source=SOURCE,macros=MACROS),dict(name='Examples',source='def broken(',macros=[]),dict(name='Examples',source=SOURCE,macros=[dict(name='missing',result=dict(type='integer'))]),dict(name='Examples',source='x'*131073,macros=[])]
        for case in cases:
            with self.assertRaises(EngineError):self.apply('module_save',fingerprint=fingerprint(self.book.db),**case)
            self.assertEqual(self.book.info()['format_version'],1)
        old=fingerprint(self.book.db);self.save()
        with self.assertRaises(EngineError) as error:self.apply('module_delete',module='Examples',fingerprint=old)
        self.assertEqual(error.exception.body['code'],'CODE_CHANGED')

    def test_required_code_hook_rolls_back_metadata_generated_edits_and_upgrade(self):
        handler=Path(self.temp.name)/'rules.py';handler.write_text("def on_code_structure(ctx,event):\n    ctx.set('Sheet1',1,1,'generated')\n    if event['phase']=='after':raise ValueError('reject code')\n")
        self.book.handler=handler
        with self.assertRaises(EngineError) as error:self.save()
        self.assertEqual(error.exception.body['code'],'HOOK_FAILED');self.assertEqual(self.book.info()['format_version'],1)
        self.assertIsNone(self.book.raw(self.book.sheet('Sheet1'),1,1))

    def test_external_code_validation_rejects_unknown_fields_and_wrong_version(self):
        self.save()
        with closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            payload=json.loads(db.execute("SELECT payload FROM dh_relational WHERE kind='module'").fetchone()[0]);payload['approved']=True
            db.execute("UPDATE dh_relational SET payload=? WHERE kind='module'",(json.dumps(payload),))
        with self.assertRaises(EngineError):self.book.refresh()

    def test_all_prior_formats_migrate_without_losing_data(self):
        for version in (1,2,3,4):
            path=Path(self.temp.name)/f'v{version}.dhwb';book=Workbook(path,create=True)
            def apply(action,**args):return book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=book.state(),**args))
            try:
                if version==2:apply('edit',cells=[dict(row=1,col=1,source='2026-11-09',kind='date')])
                if version==3:apply('metadata_set',namespace='presentation',id='view',payload={})
                if version==4:apply('table_create',name='Data',columns=[dict(name='Value')])
                self.assertEqual(book.info()['format_version'],version)
                apply('module_save',name='Examples',source=SOURCE,macros=MACROS,fingerprint=fingerprint(book.db))
                self.assertEqual(book.info()['format_version'],5)
                self.assertIn('Examples',book.read_workbook()['native_omissions'])
            finally:book.close()


if __name__=='__main__':unittest.main()
