# SPDX-License-Identifier: GPL-3.0-only
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import json
import tempfile
import unittest
import uuid
from datahorizon.engine import Workbook,EngineError
from datahorizon.relationships import inspect,preview_repair
from datahorizon.relational import table
from datahorizon.table_schema import preview
from datahorizon.client import LocalService

class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'relations.dhwb';self.book=Workbook(self.path,create=True);self.addCleanup(lambda:self.book.close())
        self.apply('table_create',name='Customers',columns=[dict(name='ID',required=True,unique=True),dict(name='Name')]);self.apply('table_create',name='Orders',columns=[dict(name='Customer'),dict(name='Qty',type='integer')])
        self.apply('table_insert',table='Customers',rows=[dict(ID='C001',Name='Ada'),dict(ID='C002',Name='Grace')]);self.apply('table_insert',table='Orders',rows=[dict(Customer='C001',Qty=2),dict(Customer=None,Qty=3)])
    def apply(self,action,**args):return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))
    def relation(self,**rules):return self.apply('relationship_create',name='CustomerOrders',parent_table='Customers',parent_column='ID',child_table='Orders',child_column='Customer',**rules)['relationship']
    def test_restrictive_native_foreign_keys_support_null_and_block_parent_updates(self):
        rel=self.relation();self.book.validate();self.assertEqual(len(self.book.db.execute('PRAGMA foreign_key_list(Orders)').fetchall()),1)
        for action,args in [('table_insert',dict(table='Orders',rows=[dict(Customer='missing')])),('table_update',dict(table='Customers',edits=[dict(row_id='1',values=dict(ID='renamed'))])),('table_delete',dict(table='Customers',record_ids=['1']))]:
            with self.assertRaises(EngineError) as failure:self.apply(action,**args)
            self.assertEqual(failure.exception.body['outcome'],'rolled_back')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            with self.assertRaises(sqlite3.IntegrityError):db.execute("INSERT INTO Orders(Customer) VALUES('missing')")
        self.apply('table_insert',table='Orders',rows=[dict(Customer=None)]);self.assertTrue(rel['id'])
    def test_incompatible_nullable_nonunique_self_and_cascade_definitions_reject(self):
        for overrides in (dict(on_delete='cascade'),dict(on_update='cascade'),dict(parent_table='Orders',parent_column='Customer',child_table='Customers',child_column='ID'),dict(parent_table='Orders',parent_column='Qty'),dict(parent_table='Customers',child_table='Customers',child_column='ID')):
            args=dict(name='Invalid',parent_table='Customers',parent_column='ID',child_table='Orders',child_column='Customer');args.update(overrides)
            with self.assertRaises(EngineError):self.apply('relationship_create',**args)
        self.book.validate()
    def test_orphan_creation_rejection_and_required_hook_failure_are_atomic(self):
        self.apply('table_insert',table='Orders',rows=[dict(Customer='missing')])
        with self.assertRaises(EngineError):self.relation()
        self.assertEqual(self.book.db.execute('PRAGMA foreign_key_list(Orders)').fetchall(),[])
        self.apply('table_delete',table='Orders',record_ids=['3']);handler=Path(self.temp.name)/'reject.py';handler.write_text('def on_table_structure(ctx,event):\n    if event["phase"]=="after":\n        ctx.set("Sheet1",1,1,"generated")\n        raise ValueError("reject relationship")\n')
        self.book.handler=str(handler)
        with self.assertRaises(EngineError):self.relation()
        self.assertEqual(self.book.db.execute('PRAGMA foreign_key_list(Orders)').fetchall(),[]);self.assertIsNone(self.book.raw(self.book.sheet('Sheet1'),1,1));self.book.handler=None;self.book.validate()
    def test_owned_index_dependency_and_native_copy_reopen(self):
        rel=self.relation();index=self.book.db.execute("SELECT id FROM dh_relational WHERE kind='index'").fetchone()[0]
        with self.assertRaises(EngineError) as failure:self.apply('index_drop',index=index)
        self.assertEqual(failure.exception.body['code'],'INDEX_OWNED')
        for name in ('Customers','Orders'):
            with self.assertRaises(EngineError):self.apply('table_drop',table=name,allow_data_loss=True)
        customers=table(self.book.db,'Customers');review=preview(self.book,dict(table=customers['id'],columns=customers['columns'],name='Renamed',expected_state=self.book.state()))
        with self.assertRaises(EngineError) as failure:self.apply('table_schema',**review['source_request'],review_digest=review['review_digest'])
        self.assertEqual(failure.exception.body['code'],'DEPENDENCY_BLOCKED')
        copy=Path(self.temp.name)/'copy.dhwb';self.book.snapshot(copy)
        with closing(Workbook(copy,readonly=True)) as reopened:reopened.validate()
        self.apply('relationship_drop',relationship=rel['id']);self.assertEqual(self.book.db.execute('PRAGMA foreign_key_list(Orders)').fetchall(),[]);self.book.validate()
    def test_child_column_rename_preserves_fk_and_index(self):
        self.relation();orders=table(self.book.db,'Orders');columns=[dict(col) for col in orders['columns']];columns[0]['name']='Buyer'
        review=preview(self.book,dict(table='Orders',columns=columns,expected_state=self.book.state()));self.apply('table_schema',**review['source_request'],review_digest=review['review_digest']);self.book.validate()
        self.assertEqual(self.book.db.execute('PRAGMA foreign_key_list(Orders)').fetchone()[3],'Buyer')
    def test_generated_parent_child_deletes_undo_in_dependency_order(self):
        self.relation();child=table(self.book.db,'Orders')['id'];handler=Path(self.temp.name)/'cascade-in-user-code.py'
        handler.write_text('def on_table_change(ctx,changes):\n    for change in changes:\n        if change["table"]=='+repr(child)+' and change["action"]=="table_delete" and change["row_id"]=="1":\n            ctx.table_delete("Customers","1")\n')
        self.book.handler=str(handler);self.apply('table_delete',table='Orders',record_ids=['1']);self.assertIsNone(self.book.db.execute('SELECT ID FROM Customers WHERE _dh_row_id=1').fetchone())
        self.apply('undo');self.assertEqual(self.book.db.execute('SELECT ID FROM Customers WHERE _dh_row_id=1').fetchone()[0],'C001');self.assertEqual(self.book.db.execute('SELECT Customer FROM Orders WHERE _dh_row_id=1').fetchone()[0],'C001')
        self.apply('redo');self.assertIsNone(self.book.db.execute('SELECT ID FROM Customers WHERE _dh_row_id=1').fetchone());self.book.handler=None
    def orphan(self,count=1):
        rel=self.relation();self.book.close()
        with closing(sqlite3.connect(self.path,isolation_level=None)) as db:
            for _ in range(count):db.execute("INSERT INTO Orders(Customer,Qty) VALUES('missing',1)")
        self.book=Workbook(self.path,repair=True);return rel
    def test_external_orphan_is_inspected_without_replaying_handlers_and_repaired_explicitly(self):
        rel=self.orphan();report=inspect(self.book,dict(expected_state=self.book.state()));self.assertEqual(len(report['orphans']),1);row=report['orphans'][0]['row_id']
        with self.assertRaises(EngineError) as failure:self.apply('table_update',table='Orders',edits=[dict(row_id=row,values=dict(Customer='C001'))])
        self.assertEqual(failure.exception.body['code'],'REPAIR_SCOPE')
        with self.assertRaises(EngineError):Workbook(self.path)
        review=preview_repair(self.book,dict(relationship=rel['id'],mode='update',edits=[dict(row_id=row,values=dict(Customer='C001'))],expected_state=self.book.state()))
        self.apply('table_repair',**{k:v for k,v in review['operation'].items() if k!='action'},review_digest=review['review_digest']);self.assertEqual(self.book.info()['relational_issues'],[]);self.assertEqual(self.book.undo,[])
        self.book.close();self.book=Workbook(self.path);self.book.validate()
    def test_incomplete_unrelated_and_stale_repairs_roll_back(self):
        rel=self.orphan(2);ids=[r['row_id'] for r in inspect(self.book,dict(expected_state=self.book.state()))['orphans']]
        for edits in ([dict(row_id='1',values=dict(Customer='C002'))],[dict(row_id=ids[0],values=dict(Qty=9))]):
            with self.assertRaises(EngineError):preview_repair(self.book,dict(relationship=rel['id'],mode='update',edits=edits,expected_state=self.book.state()))
        review=preview_repair(self.book,dict(relationship=rel['id'],mode='delete',record_ids=ids[:1],expected_state=self.book.state()))
        with self.assertRaises(EngineError) as failure:self.apply('table_repair',**{k:v for k,v in review['operation'].items() if k!='action'},review_digest=review['review_digest'])
        self.assertEqual(failure.exception.body['code'],'REPAIR_INCOMPLETE');self.assertEqual(len(inspect(self.book,dict(expected_state=self.book.state()))['orphans']),2)
        review=preview_repair(self.book,dict(relationship=rel['id'],mode='delete',record_ids=ids,expected_state=self.book.state()))
        with self.assertRaises(EngineError):self.apply('table_repair',**{k:v for k,v in review['operation'].items() if k!='action'},review_digest='0'*64)
        self.apply('table_repair',**{k:v for k,v in review['operation'].items() if k!='action'},review_digest=review['review_digest']);self.assertEqual(self.book.info()['relational_issues'],[])
    def test_required_repair_handler_failure_rolls_back_generated_cells_and_key(self):
        rel=self.orphan();row=inspect(self.book,dict(expected_state=self.book.state()))['orphans'][0]['row_id'];handler=Path(self.temp.name)/'repair-rule.py'
        handler.write_text('def on_table_change(ctx,changes):\n    ctx.set("Sheet1",1,1,"generated")\n    raise ValueError("reject repair")\n');self.book.handler=str(handler)
        review=preview_repair(self.book,dict(relationship=rel['id'],mode='update',edits=[dict(row_id=row,values=dict(Customer='C001'))],expected_state=self.book.state()))
        with self.assertRaises(EngineError) as failure:self.apply('table_repair',**{k:v for k,v in review['operation'].items() if k!='action'},review_digest=review['review_digest'])
        self.assertEqual(failure.exception.body['code'],'HOOK_FAILED');self.assertIsNone(self.book.raw(self.book.sheet('Sheet1'),1,1));self.assertEqual(len(inspect(self.book,dict(expected_state=self.book.state()))['orphans']),1);self.book.handler=None

class RelationshipClientTests(unittest.TestCase):
    def test_rest_sdk_repair_mode_and_catalog_contract(self):
        with tempfile.TemporaryDirectory() as temp,LocalService() as client:
            path=Path(temp)/'client.dhwb'
            with client.open(path,create=True) as book:
                book.create_table('Parents',[dict(name='Key',required=True,unique=True)]);book.create_table('Children',[dict(name='Key')]);book.insert_records('Parents',[dict(Key='A')]);book.create_relationship('ParentChild','Parents','Key','Children','Key');self.assertEqual(len(book.tables()['relationships']),1)
            with closing(sqlite3.connect(path,isolation_level=None)) as db:db.execute("INSERT INTO Children(Key) VALUES('orphan')")
            cli=subprocess.run([sys.executable,'-m','datahorizon.cli','--repair',str(path),'relationship','inspect'],capture_output=True,text=True,check=True)
            self.assertEqual(len(json.loads(cli.stdout)['orphans']),1)
            with client.open(path,repair=True) as book:
                report=book.inspect_relationships();self.assertTrue(report['repair']);review=book.preview_orphan_repair('ParentChild','update',edits=[dict(row_id=report['orphans'][0]['row_id'],values=dict(Key='A'))]);book.apply_orphan_repair(review)
            with client.open(path) as book:self.assertEqual(book.inspect_relationships()['orphans'],[])
