# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook,EngineError
from datahorizon.names import names,read,valid_name


class NameTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'names.dhwb'
        self.book=Workbook(self.path,create=True);self.addCleanup(self.book.close)
        self.apply('sheet_rename',sheet='Sheet1',name='Supplies')

    def apply(self,action,**args):
        return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))

    def save(self,name,reference,**args):
        return self.apply('name_save',name=name,reference=reference,**args)

    def read(self,**args):
        return read(self.book,dict(expected_state=self.book.state(),**args))

    def delete(self,name_id):
        return self.apply('name_delete',name_id=name_id)

    def code_of(self,call):
        with self.assertRaises(EngineError) as error:call()
        return error.exception.body['code']

    def version(self):
        return self.book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]

    def test_save_read_rename_retarget_delete(self):
        nid=self.save('Quantities',"'Supplies'!B2:B4")['name_id']
        item=self.read()['names'][0]
        self.assertEqual(item['reference'],"'Supplies'!B2:B4");self.assertEqual((item['row1'],item['col1'],item['row2'],item['col2']),(2,2,4,2))
        self.assertFalse(item['broken']);self.assertEqual(item['sheet'],'Supplies')
        self.assertEqual(self.save('Qty',"'Supplies'!B2:B5",name_id=nid)['name_id'],nid)
        self.assertEqual([n['name'] for n in self.read()['names']],['Qty'])
        self.assertEqual(self.read(name='qty')['names'][0]['id'],nid)
        self.delete(nid);self.assertEqual(self.read()['names'],[])
        self.assertEqual(self.code_of(lambda:self.delete(nid)),'NAME_NOT_FOUND')

    def test_single_cell_unqualified_quotes_and_reversed_ranges(self):
        self.save('One','Supplies!B3');self.save('Flip','Supplies!B4:B2')
        self.apply('sheet_add',name="O'Brien");self.save('Odd',"'O''Brien'!A1")
        found={n['name']:n['reference'] for n in self.read()['names']}
        self.assertEqual(found,{'One':"'Supplies'!B3",'Flip':"'Supplies'!B2:B4",'Odd':"'O''Brien'!A1"})

    def test_first_name_upgrades_to_v6_and_rolls_back_on_failure(self):
        self.assertEqual(self.version(),1)
        self.code_of(lambda:self.save('Bad','Nope'))
        self.assertEqual(self.version(),1)
        handler=Path(self.temp.name)/'rules.py';handler.write_text("def on_name_structure(ctx,event):\n    if event['phase']=='after':raise ValueError('reject')\n")
        self.book.handler=handler
        self.assertEqual(self.code_of(lambda:self.save('Good',"Supplies!A1")),'HOOK_FAILED')
        self.assertEqual(self.version(),1);self.assertEqual(self.read()['names'],[])
        self.book.handler=None;self.save('Good',"Supplies!A1");self.assertEqual(self.version(),6)

    def test_invalid_identifiers_rejected(self):
        for bad in ['a1','XFD10','sum','Sum','TRUE','false','1x','has space','x'*65,'',None]:
            self.assertEqual(self.code_of(lambda:self.save(bad,"'Supplies'!A1")),'NAME_INVALID',bad)
        self.assertTrue(valid_name('x'*64));self.assertTrue(valid_name('_a1b'));self.assertFalse(valid_name('XFE1'))
        self.assertEqual(self.read()['names'],[])

    def test_duplicate_is_case_insensitive(self):
        nid=self.save('Total','Supplies!A1')['name_id']
        self.assertEqual(self.code_of(lambda:self.save('TOTAL','Supplies!A2')),'NAME_DUPLICATE')
        self.save('TOTAL','Supplies!A2',name_id=nid)       # a name may change its own case
        self.assertEqual(self.read()['names'][0]['name'],'TOTAL')

    def test_reference_must_name_an_existing_sheet(self):
        for bad in ['B3',"'Nope'!B3",'Supplies!','Supplies!B3:C','Supplies!XFE1','Supplies!A1048577','Supplies!A1:B2:C3','Supplies!Supplies!A1']:
            self.assertEqual(self.code_of(lambda:self.save('X',bad)),'REFERENCE_INVALID',bad)
        self.assertEqual(self.code_of(lambda:self.save('X','Supplies!A1',name_id=str(uuid.uuid4()))),'NAME_NOT_FOUND')

    def test_limit_256(self):
        for index in range(256):self.save(f'name_{index}','Supplies!A1')
        self.assertEqual(self.code_of(lambda:self.save('extra','Supplies!A1')),'NAME_LIMIT')
        self.assertEqual(len(self.read()['names']),256)
        self.save('renamed','Supplies!A2',name_id=self.read()['names'][0]['id'])

    def test_changes_clear_history(self):
        self.apply('edit',sheet='Supplies',cells=[dict(row=1,col=1,source='1')]);self.assertTrue(self.book.undo)
        self.save('Total','Supplies!A1');self.assertEqual(self.book.undo,[])
        self.apply('edit',sheet='Supplies',cells=[dict(row=1,col=1,source='2')]);self.assertTrue(self.book.undo)
        self.delete(self.read()['names'][0]['id']);self.assertEqual(self.book.undo,[])

    def test_handler_must_opt_in(self):
        handler=Path(self.temp.name)/'rules.py';handler.write_text("def on_code_structure(ctx,event):\n    pass\n")
        self.book.handler=handler
        self.assertEqual(self.code_of(lambda:self.save('Total','Supplies!A1')),'UNSUPPORTED_HOOK_SCOPE')
        handler.write_text("def on_change(ctx,changes):\n    pass\ndef on_name_structure(ctx,event):\n    ctx.set('Supplies',1,1,event['phase'])\n")
        self.save('Total','Supplies!A1');self.assertEqual(self.book.raw(self.book.sheet('Supplies'),1,1)['source'],'after')

    def test_stale_state_rejects(self):
        request=dict(action='name_save',operation_id=str(uuid.uuid4()),expected_state='stale',name='Total',reference='Supplies!A1')
        with self.assertRaises(EngineError) as error:self.book.mutate(request)
        self.assertEqual(error.exception.body['code'],'STALE_STATE');self.assertEqual(self.read()['names'],[])
        self.assertEqual(self.version(),1)

    def test_unknown_fields_rejected(self):
        self.assertEqual(self.code_of(lambda:self.save('Total','Supplies!A1',extra=1)),'INVALID_ARGUMENT')

    def test_deleted_sheet_reads_as_broken_and_broken_needs_new_reference(self):
        self.apply('sheet_add',name='Other');self.save('Gone','Other!A1')
        with closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            db.execute("DELETE FROM sheets WHERE name='Other'")
        self.book.refresh()
        item=self.read()['names'][0];self.assertTrue(item['broken']);self.assertIsNone(item['reference']);self.assertIsNone(item['sheet'])

    def test_stored_broken_name_and_external_corruption(self):
        nid=self.save('Total','Supplies!A1')['name_id']
        broken=dict(id=nid,name='Total',sheet_id=None,row1=None,col1=None,row2=None,col2=None)
        with closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            db.execute("UPDATE dh_relational SET payload=? WHERE kind='name'",(json.dumps(broken),))
        self.book.refresh()
        self.assertTrue(self.read()['names'][0]['broken'])
        self.assertEqual(self.code_of(lambda:self.apply('name_save',name='Renamed',name_id=nid)),'NAME_BROKEN')
        self.save('Renamed','Supplies!C3',name_id=nid);self.assertFalse(self.read()['names'][0]['broken'])
        with closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            db.execute("UPDATE dh_relational SET payload=? WHERE kind='name'",(json.dumps(dict(broken,approved=True)),))
        with self.assertRaises(EngineError):self.book.refresh()


if __name__=='__main__':unittest.main()
