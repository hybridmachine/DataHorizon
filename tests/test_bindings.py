# SPDX-License-Identifier: GPL-3.0-only
"""Event bindings are inert v6 catalog objects covered by the code fingerprint."""
from contextlib import closing
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid

from datahorizon.engine import Workbook,EngineError,encoded
from datahorizon.workbook_code import fingerprint,modules,read as read_code
from datahorizon.names import names as stored_names

SOURCE='def on_edit(ctx):\n    pass\n\nasync def later(ctx):\n    pass\n\nclass Holder:\n    def method(self):\n        pass\n'


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.dir=Path(self.temp.name)
        self.book=Workbook(self.dir/'b.dhwb',create=True);self.addCleanup(self.book.close)
        self.apply('sheet_rename',sheet='Sheet1',name='Supplies')
        self.apply('module_save',name='Rules',source=SOURCE,macros=[],fingerprint=fingerprint(self.book.db))

    def apply(self,action,**args):
        return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))

    def save(self,scope,event='cell_change',**args):
        args.setdefault('module','Rules');args.setdefault('function','on_edit')
        return self.apply('binding_save',event=event,scope=scope,fingerprint=fingerprint(self.book.db),**args)

    def delete(self,binding_id):
        return self.apply('binding_delete',binding_id=binding_id,fingerprint=fingerprint(self.book.db))

    def read(self):
        return read_code(self.book,dict(expected_state=self.book.state()))

    def code(self,call):
        with self.assertRaises(EngineError) as error:call()
        return error.exception.body['code']

    def version(self):
        return self.book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]

    def rng(self,reference="'Supplies'!B2:B4"):
        return dict(kind='range',reference=reference)

    def test_no_bindings_keeps_existing_fingerprint(self):
        expected=hashlib.sha256(encoded(modules(self.book.db)).encode()).hexdigest()
        before=self.version();self.assertEqual(fingerprint(self.book.db),expected)
        self.assertEqual(self.read()['bindings'],[]);self.assertEqual(self.version(),before)
        made=self.save(self.rng());self.assertNotEqual(fingerprint(self.book.db),expected)
        self.delete(made['binding_id'])
        self.assertEqual(fingerprint(self.book.db),expected);self.assertEqual(self.version(),6)

    def test_adding_removing_or_redirecting_changes_fingerprint(self):
        seen=[fingerprint(self.book.db)]
        made=self.save(self.rng());seen.append(fingerprint(self.book.db))
        self.save(self.rng("'Supplies'!C2:C4"),binding_id=made['binding_id']);seen.append(fingerprint(self.book.db))
        self.save(dict(kind='sheet',sheet='Supplies'),binding_id=made['binding_id']);seen.append(fingerprint(self.book.db))
        self.save(dict(kind='sheet',sheet='Supplies'),binding_id=made['binding_id'],required=False);seen.append(fingerprint(self.book.db))
        self.assertEqual(len(set(seen)),len(seen))
        self.assertEqual(len(self.read()['bindings']),1)

    def test_update_keeps_identity_and_unchanged_range_id(self):
        made=self.save(self.rng());first=self.read()['bindings'][0]['scope']['range_id']
        self.save(self.rng(),binding_id=made['binding_id'],required=False)
        item=self.read()['bindings'][0];self.assertEqual(item['id'],made['binding_id']);self.assertEqual(item['scope']['range_id'],first);self.assertFalse(item['required'])
        self.save(self.rng("'Supplies'!B2:B5"),binding_id=made['binding_id'])
        self.assertNotEqual(self.read()['bindings'][0]['scope']['range_id'],first)

    def test_moving_rows_keeps_fingerprint_and_moves_scope(self):
        self.save(self.rng());fp=fingerprint(self.book.db);rid=self.read()['bindings'][0]['scope']['range_id']
        self.apply('rows_insert',sheet='Supplies',start=1,count=1)
        self.assertEqual(fingerprint(self.book.db),fp)
        item=self.read()['bindings'][0];self.assertEqual(item['reference'],"'Supplies'!B3:B5");self.assertEqual(item['scope']['range_id'],rid);self.assertEqual(item['module'],'Rules')
        self.apply('columns_insert',sheet='Supplies',start=1,count=1)
        self.assertEqual(self.read()['bindings'][0]['reference'],"'Supplies'!C3:C5");self.assertEqual(fingerprint(self.book.db),fp)

    def test_retargeting_a_name_scope_keeps_fingerprint(self):
        self.apply('name_save',name='Quantities',reference="Supplies!B2:B4")
        self.save(dict(kind='name',name='Quantities'));fp=fingerprint(self.book.db)
        name_id=self.read()['bindings'][0]['scope']['name_id']
        self.apply('name_save',name='Quantities',name_id=name_id,reference="Supplies!D1:D2")
        self.assertEqual(fingerprint(self.book.db),fp)
        item=self.read()['bindings'][0];self.assertEqual(item['reference'],"'Supplies'!D1:D2");self.assertFalse(item['broken'])
        self.apply('name_delete',name_id=name_id)
        item=self.read()['bindings'][0];self.assertTrue(item['broken']);self.assertIsNone(item['reference']);self.assertEqual(fingerprint(self.book.db),fp)

    def test_deleted_scope_is_broken_and_reported(self):
        self.apply('sheet_add',name='Other')
        self.save(self.rng());self.save(dict(kind='sheet',sheet='Other'),function='on_edit')
        receipt=self.apply('rows_delete',sheet='Supplies',start=2,count=3)
        self.assertEqual(receipt['broken_bindings'],['Rules.on_edit'])
        item=next(b for b in self.read()['bindings'] if b['scope']['kind']=='range')
        self.assertTrue(item['broken']);self.assertIsNone(item['reference']);self.assertIsNotNone(item['scope']['range_id']);self.assertEqual(item['scope']['sheet_id'],self.book.sheet('Supplies'))
        receipt=self.apply('sheet_delete',sheet='Other');self.assertEqual(receipt['broken_bindings'],['Rules.on_edit'])
        receipt=self.apply('sheet_add',name='Third');self.assertNotIn('broken_bindings',receipt)
        self.assertEqual(sum(b['broken'] for b in self.read()['bindings']),2)

    def test_range_scope_broken_when_sheet_deleted(self):
        self.apply('sheet_add',name='Other');self.save(self.rng("'Other'!A1"))
        receipt=self.apply('sheet_delete',sheet='Other');self.assertEqual(receipt['broken_bindings'],['Rules.on_edit'])
        item=self.read()['bindings'][0];self.assertTrue(item['broken']);self.assertIsNone(item['scope']['sheet_id'])
        self.assertNotIn('broken_bindings',self.apply('sheet_add',name='Z'))

    def test_table_scope(self):
        self.apply('table_create',name='Items',columns=[dict(name='Title',type='text')])
        self.save(dict(kind='table',table='Items'),event='table_change')
        item=self.read()['bindings'][0];self.assertFalse(item['broken']);self.assertIsNone(item['reference'])
        self.apply('table_drop',table='Items');self.assertTrue(self.read()['bindings'][0]['broken'])

    def test_name_delete_reports_rules_scoped_to_it(self):
        self.apply('name_save',name='Quantities',reference="Supplies!B2:B4")
        self.apply('name_save',name='Other',reference="Supplies!C1")
        self.apply('module_save',name='Audit',source=SOURCE,macros=[],fingerprint=fingerprint(self.book.db))
        self.save(dict(kind='name',name='Quantities'));self.save(dict(kind='name',name='Quantities'),module='Audit')
        self.save(dict(kind='name',name='Other'),required=False)
        target=next(item['scope']['name_id'] for item in self.read()['bindings'] if item['module']=='Rules' and item['required'])
        receipt=self.apply('name_save',name='Quantities',name_id=target,reference="Supplies!D1:D2")
        self.assertNotIn('broken_bindings',receipt)
        receipt=self.apply('name_delete',name_id=target)
        self.assertEqual(receipt['broken_bindings'],['Audit.on_edit','Rules.on_edit'])
        self.apply('name_save',name='Unused',reference="Supplies!E1")
        unused=next(item['id'] for item in stored_names(self.book.db) if item['name']=='Unused')
        self.assertNotIn('broken_bindings',self.apply('name_delete',name_id=unused))

    def test_structural_name_break_reports_rules_scoped_to_it(self):
        self.apply('name_save',name='Quantities',reference="Supplies!B2:B4")
        self.apply('sheet_add',name='Other');self.apply('name_save',name='Elsewhere',reference="Other!A1")
        self.save(dict(kind='name',name='Quantities'));self.save(dict(kind='name',name='Elsewhere'),required=False)
        receipt=self.apply('rows_delete',sheet='Supplies',start=2,count=3)
        self.assertEqual(receipt['broken_names'],['Quantities']);self.assertEqual(receipt['broken_bindings'],['Rules.on_edit'])
        receipt=self.apply('sheet_delete',sheet='Other')
        self.assertEqual(receipt['broken_names'],['Elsewhere']);self.assertEqual(receipt['broken_bindings'],['Rules.on_edit'])
        self.assertTrue(all(item['broken'] for item in self.read()['bindings']))

    def test_table_drop_reports_rules_scoped_to_it(self):
        self.apply('table_create',name='Items',columns=[dict(name='Title',type='text')])
        self.apply('table_create',name='Kept',columns=[dict(name='Title',type='text')])
        self.save(dict(kind='table',table='Items'),event='table_change')
        self.save(dict(kind='table',table='Kept'),event='table_change',required=False)
        self.assertNotIn('broken_bindings',self.apply('table_create',name='Spare',columns=[dict(name='Title',type='text')]))
        self.assertNotIn('broken_bindings',self.apply('table_drop',table='Spare'))
        receipt=self.apply('table_drop',table='Items');self.assertEqual(receipt['broken_bindings'],['Rules.on_edit'])

    def test_event_and_scope_pairing(self):
        self.apply('table_create',name='Items',columns=[dict(name='Title',type='text')])
        self.assertEqual(self.code(lambda:self.save(dict(kind='table',table='Items'))),'BINDING_INVALID')
        self.assertEqual(self.code(lambda:self.save(self.rng(),event='table_change')),'BINDING_INVALID')
        self.assertEqual(self.code(lambda:self.save(self.rng(),event='nope')),'BINDING_INVALID')
        self.assertEqual(self.code(lambda:self.save(dict(kind='sheet',sheet='Supplies',extra=1))),'BINDING_INVALID')
        self.assertEqual(self.code(lambda:self.save(self.rng(),required='yes')),'BINDING_INVALID')

    def test_function_must_be_top_level_def(self):
        for function in ('later','Holder','method','missing',3):
            self.assertEqual(self.code(lambda:self.save(self.rng(),function=function)),'BINDING_INVALID')
        self.assertEqual(self.code(lambda:self.save(self.rng(),module='Nope')),'MODULE_NOT_FOUND')
        self.assertEqual(self.version(),5)

    def test_stale_fingerprint_rejects(self):
        self.assertEqual(self.code(lambda:self.apply('binding_save',event='cell_change',scope=self.rng(),module='Rules',function='on_edit',fingerprint='0'*64)),'CODE_CHANGED')
        made=self.save(self.rng())
        self.assertEqual(self.code(lambda:self.apply('binding_delete',binding_id=made['binding_id'],fingerprint='0'*64)),'CODE_CHANGED')
        self.assertEqual(self.code(lambda:self.apply('binding_delete',binding_id=str(uuid.uuid4()),fingerprint=fingerprint(self.book.db))),'BINDING_NOT_FOUND')

    def test_limit_16(self):
        for row in range(16):self.save(self.rng("'Supplies'!A%d"%(row+1)))
        self.assertEqual(self.code(lambda:self.save(self.rng("'Supplies'!A20"))),'BINDING_LIMIT')
        first=self.read()['bindings'][0]['id'];self.save(self.rng("'Supplies'!A30"),binding_id=first)
        self.delete(first);self.save(self.rng("'Supplies'!A20"))

    def test_module_with_binding_cannot_be_deleted(self):
        made=self.save(self.rng())
        self.assertEqual(self.code(lambda:self.apply('module_delete',module='Rules',fingerprint=fingerprint(self.book.db))),'MODULE_IN_USE')
        self.delete(made['binding_id']);self.apply('module_delete',module='Rules',fingerprint=fingerprint(self.book.db))

    def test_open_and_read_never_execute(self):
        marker=self.dir/'marker.txt'
        source='open(%r,"w").write("ran")\ndef on_edit(ctx):\n    pass\n'%str(marker)
        self.apply('module_save',name='Marked',source=source,macros=[],fingerprint=fingerprint(self.book.db))
        self.save(self.rng(),module='Marked');path=self.dir/'b.dhwb'
        self.book.close();shutil.copy(path,self.dir/'copy.dhwb')
        with closing(Workbook(path)) as reopened:
            self.assertEqual(len(read_code(reopened,dict(expected_state=reopened.state()))['bindings']),1)
        with closing(Workbook(self.dir/'copy.dhwb')) as copy:
            self.assertEqual(len(read_code(copy,dict(expected_state=copy.state()))['bindings']),1)
        self.assertFalse(marker.exists())
        self.book=Workbook(path)

    def test_corrupt_stored_binding_rejected_on_open(self):
        import sqlite3,json
        made=self.save(self.rng());path=self.dir/'b.dhwb';self.book.close()
        with closing(sqlite3.connect(path,isolation_level=None)) as db:
            row=db.execute("SELECT payload FROM dh_relational WHERE kind='binding'").fetchone()
            value=json.loads(row[0]);value['module_id']=str(uuid.uuid4())
            db.execute("UPDATE dh_relational SET payload=?",(json.dumps(value),))
        with self.assertRaises(EngineError):Workbook(path)
        self.book=Workbook(self.dir/'other.dhwb',create=True)


if __name__=='__main__':unittest.main()
