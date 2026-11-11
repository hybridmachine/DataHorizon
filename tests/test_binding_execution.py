# SPDX-License-Identifier: GPL-3.0-only
"""Approved workbook bindings run inside the transaction of every managed edit."""
from contextlib import closing
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid

from datahorizon.bindings import bindings
from datahorizon.csv_import import preview_csv
from datahorizon.engine import Workbook,EngineError
from datahorizon.workbook_code import activation,fingerprint

SOURCE='''import json

def _append(ctx, letter):
    current = ctx.get('Log!A1')
    ctx.set('Log!A1', (current['source'] if current else '') + letter)

def check_quantity(ctx, changes):
    for c in changes:
        value = ctx.value(c['sheet'], c['row'], c['col'])
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError('Quantity cannot be negative')

def log_a(ctx, changes):
    _append(ctx, 'a')

def log_b(ctx, changes):
    _append(ctx, 'b')

def record(ctx, changes):
    ctx.set('Log!A3', json.dumps([[c['row'], c['col'], c['action']] for c in changes]))

def stamp(ctx, changes):
    if changes[0]['action'] == 'edit':
        ctx.set('Log!A2', 'stamped')

def record_action(ctx, changes):
    ctx.set('Log!A4', changes[0]['action'])

def record_table(ctx, changes):
    ctx.set('Log!A5', json.dumps([[c['row_id'], c['action']] for c in changes]))

def crash(ctx, changes):
    import os
    os._exit(3)

def refuse(ctx, changes):
    raise ValueError('binding was called')

def spin(ctx, changes):
    while True:
        pass

def bump(ctx, changes):
    ctx.set('Log!A9', (ctx.value('Log!A9') or 0) + 1)

def run(ctx):
    ctx.set('Supplies!B2', -1)
    return 1
'''

FRAGILE='def on_edit(ctx, changes):\n    pass\n\ndef other(ctx, changes):\n    pass\n\nraise RuntimeError("broken at import")\n'

HANDLER='''MODE = %r

def _negative(ctx):
    if ctx.value('Supplies!B2') != -1:
        ctx.set('Supplies!B2', -1)

def on_structure(ctx, event):
    if event['phase'] == MODE:
        _negative(ctx)

def on_change(ctx, changes):
    if MODE == 'change':
        _negative(ctx)
'''


class BindingExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.book=Workbook(Path(self.temp.name)/'run.dhwb',create=True);self.addCleanup(self.book.close)
        self.apply('sheet_rename',sheet='Sheet1',name='Supplies');self.apply('sheet_add',name='Log')
        cells=[dict(row=1,col=1,source='Item'),dict(row=1,col=2,source='Quantity'),dict(row=1,col=3,source='Unit price'),dict(row=1,col=4,source='Amount')]
        for row,(item,quantity,price) in enumerate([('Notebook',2,'3.50'),('Pen',4,'1.25'),('Folder',3,'2.00')],start=2):
            cells+=[dict(row=row,col=1,source=item),dict(row=row,col=2,source=str(quantity)),dict(row=row,col=3,source=price),dict(row=row,col=4,source='=B%d*C%d'%(row,row))]
        cells.append(dict(row=6,col=4,source='=SUM(D2:D4)'))
        self.apply('edit',sheet='Supplies',cells=cells)
        self.apply('module_save',name='supplies',source=SOURCE,macros=[dict(name='run',parameters=[],result=dict(type='integer'))],fingerprint=fingerprint(self.book.db))
        self.apply('name_save',name='Quantities',reference='Supplies!B2:B4')

    def apply(self,action,**args):
        return self.book.mutate(dict(action=action,operation_id=str(uuid.uuid4()),expected_state=self.book.state(),**args))

    def approve(self):
        self.book.approved_code=frozenset([activation(self.book)['activation_key']])

    def bind(self,function,scope=None,event='cell_change',required=True,approve=True,module='supplies'):
        made=self.apply('binding_save',event=event,scope=scope or dict(kind='range',reference="'Supplies'!B2:B4"),module=module,function=function,required=required,fingerprint=fingerprint(self.book.db))
        if approve:self.approve()
        return made['binding_id']

    def edit(self,row,col,source,sheet='Supplies'):
        return self.apply('edit',sheet=sheet,cells=[dict(row=row,col=col,source=source)])

    def failure(self,call):
        with self.assertRaises(EngineError) as caught:call()
        return caught.exception.body

    def raw(self,sheet,row,col):
        return self.book.raw(self.book.sheet(sheet),row,col)

    def log(self,row=1):
        item=self.raw('Log',row,1);return item['source'] if item else None

    def test_rule_rejects_negative_quantity_from_every_path(self):
        self.bind('check_quantity')
        csv=preview_csv(dict(csv='-1',column_types={'1':'number'}))
        paths={
            'set_cell':lambda:self.edit(2,2,'-1'),
            'paste':lambda:self.apply('paste',sheet='Supplies',row=2,col=2,rows=[[dict(source='-1',input_kind='auto',style={})]]),
            'fill':lambda:self.apply('fill',sheet='Supplies',r1=2,c1=2,r2=4,c2=2,mode='series',step=-3),
            'csv_import':lambda:self.apply('edit',sheet='Supplies',label='Import CSV',cells=[{'row':r+1,'col':c+1,**item} for r,row in enumerate(csv['inputs'],1) for c,item in enumerate(row,1)]),
            'macro':lambda:self.apply('macro_run',module='supplies',macro='run',fingerprint=fingerprint(self.book.db)),
        }
        for path,call in paths.items():
            with self.subTest(path=path):
                outcome=self.failure(call)
                self.assertEqual(outcome['code'],'BINDING_FAILED');self.assertEqual(outcome['outcome'],'rolled_back')
                self.assertIn('supplies.check_quantity',outcome['message']);self.assertIn('Quantity cannot be negative',outcome['message'])
                self.assertEqual(self.book.operation(outcome['operation_id'])['code'],'BINDING_FAILED')
                self.assertEqual(self.raw('Supplies',2,2)['source'],'2');self.assertEqual(self.raw('Supplies',3,2)['source'],'4')
        self.assertEqual(self.edit(2,2,'5')['outcome'],'committed')

    def test_runs_without_any_external_handler(self):
        self.assertIsNone(self.book.handler);self.bind('log_a')
        self.edit(2,2,'5');self.assertEqual(self.log(),'a')

    def test_external_handler_runs_before_bindings_in_stored_order(self):
        self.bind('log_a');self.bind('log_b')
        handler=Path(self.temp.name)/'rules.py'
        handler.write_text("def on_change(ctx,changes):\n    if any(c['col']==2 for c in changes):\n        current=ctx.get('Log!A1')\n        ctx.set('Log!A1',(current['source'] if current else '')+'h')\n")
        self.book.handler=handler
        self.edit(2,2,'5')
        order=''.join(item['function'][-1] for item in bindings(self.book.db))
        self.assertEqual(self.log(),'h'+order)

    def test_generated_edits_join_one_undo_entry(self):
        self.bind('stamp');entries=len(self.book.undo)
        receipt=self.edit(2,2,'5');self.assertEqual(receipt['changed_cells'],2);self.assertEqual(len(self.book.undo),entries+1)
        self.assertEqual(self.log(2),'stamped')
        self.apply('undo');self.assertEqual(self.raw('Supplies',2,2)['source'],'2');self.assertIsNone(self.log(2))
        self.apply('redo');self.assertEqual(self.raw('Supplies',2,2)['source'],'5');self.assertEqual(self.log(2),'stamped')

    def test_changes_are_filtered_to_scope(self):
        self.bind('record')
        self.apply('edit',sheet='Supplies',cells=[dict(row=2,col=1,source='Pad'),dict(row=2,col=2,source='5'),dict(row=5,col=2,source='1')])
        self.assertEqual(json.loads(self.log(3)),[[2,2,'edit']])
        self.apply('edit',sheet='Log',cells=[dict(row=3,col=1,clear=True)])
        self.edit(5,2,'9');self.edit(2,1,'Book');self.assertIsNone(self.log(3))
        self.edit(2,2,'7',sheet='Log');self.assertIsNone(self.log(3))

    def test_sheet_and_name_scopes_match_by_identity(self):
        self.bind('log_a',scope=dict(kind='name',name='Quantities'));self.bind('log_b',scope=dict(kind='sheet',sheet='Supplies'))
        self.edit(9,9,'x');self.assertEqual(self.log(),'b')
        self.apply('sheet_rename',sheet='Supplies',name='Stock')
        self.apply('edit',sheet='Log',cells=[dict(row=1,col=1,clear=True)])
        self.edit(3,2,'8',sheet='Stock');self.assertEqual(sorted(self.log()),['a','b'])

    def test_unapproved_required_binding_blocks_edits_in_scope_only(self):
        made=self.bind('log_a',approve=False)
        outcome=self.failure(lambda:self.edit(2,2,'5'))
        self.assertEqual(outcome['code'],'BINDING_NOT_APPROVED');self.assertEqual(outcome['http_status'],403);self.assertIn('supplies.log_a',outcome['message'])
        self.assertEqual(outcome['binding'],dict(binding_id=made,module='supplies',function='log_a'))
        self.assertEqual(self.book.operation(outcome['operation_id'])['binding'],outcome['binding'])
        self.assertEqual(self.raw('Supplies',2,2)['source'],'2')
        receipt=self.edit(2,1,'Pad');self.assertEqual(receipt['outcome'],'committed');self.assertNotIn('binding_notices',receipt);self.assertIsNone(self.log())

    def test_deleting_a_sheet_with_a_rule_keeps_approval_for_other_rules(self):
        self.apply('sheet_add',name='Other')
        self.bind('log_a',scope=dict(kind='sheet',sheet='Other'));self.bind('log_a',scope=dict(kind='range',reference="'Other'!A1:A2"),required=False);self.bind('log_a')
        key=activation(self.book)['activation_key']
        receipt=self.apply('sheet_delete',sheet='Other');self.assertEqual(receipt['broken_bindings'],['supplies.log_a','supplies.log_a'])
        state=activation(self.book);self.assertEqual(state['activation_key'],key);self.assertTrue(state['approved'])
        receipt=self.edit(2,2,'5');self.assertEqual(receipt['outcome'],'committed');self.assertNotIn('binding_notices',receipt);self.assertEqual(self.log(),'a')

    def test_unapproved_optional_binding_is_skipped_with_notice(self):
        skipped=self.bind('log_a',required=False,approve=False)
        receipt=self.apply('edit',sheet='Supplies',cells=[dict(row=2,col=2,source='5'),dict(row=3,col=2,source='6')])
        notices=[dict(binding_id=skipped,function='log_a',reason='BINDING_NOT_APPROVED')]
        self.assertEqual(receipt['binding_notices'],notices);self.assertEqual(self.book.operation(receipt['operation_id'])['binding_notices'],notices)
        self.assertEqual(self.raw('Supplies',2,2)['source'],'5');self.assertIsNone(self.log())

    def test_function_removed_from_module_is_unavailable(self):
        required=self.bind('log_a');optional=self.bind('log_b',required=False)
        self.apply('module_save',module='supplies',name='supplies',source=SOURCE.replace('def log_a(','def renamed_a(').replace('def log_b(','def renamed_b('),macros=[dict(name='run',parameters=[],result=dict(type='integer'))],fingerprint=fingerprint(self.book.db))
        self.approve()
        outcome=self.failure(lambda:self.edit(2,2,'5'))
        self.assertEqual(outcome['code'],'BINDING_UNAVAILABLE');self.assertEqual(outcome['http_status'],409);self.assertIn('supplies.log_a',outcome['message'])
        self.assertEqual(outcome['binding'],dict(binding_id=required,module='supplies',function='log_a'))
        self.apply('binding_delete',binding_id=required,fingerprint=fingerprint(self.book.db));self.approve()
        receipt=self.edit(2,2,'5')
        self.assertEqual(receipt['binding_notices'],[dict(binding_id=optional,function='log_b',reason='BINDING_UNAVAILABLE')])

    def test_optional_binding_that_raises_still_rejects(self):
        self.bind('check_quantity',required=False)
        outcome=self.failure(lambda:self.edit(2,2,'-1'))
        self.assertEqual(outcome['code'],'BINDING_FAILED');self.assertEqual(self.raw('Supplies',2,2)['source'],'2')

    def test_undo_is_rejected_when_approval_was_lost_and_history_survives(self):
        self.bind('log_a');self.edit(2,2,'5');self.book.approved_code=frozenset()
        self.assertEqual(len(self.book.undo),1)
        self.assertEqual(self.failure(lambda:self.apply('undo'))['code'],'BINDING_NOT_APPROVED')
        self.assertEqual(len(self.book.undo),1);self.assertEqual(self.raw('Supplies',2,2)['source'],'5')
        self.approve();self.apply('undo');self.assertEqual(self.raw('Supplies',2,2)['source'],'2')

    def test_undo_and_redo_pass_action(self):
        self.bind('record_action')
        self.edit(2,2,'5');self.assertEqual(self.log(4),'edit')
        self.apply('undo');self.assertEqual(self.raw('Supplies',2,2)['source'],'2');self.assertEqual(self.log(4),'undo')
        self.apply('redo');self.assertEqual(self.raw('Supplies',2,2)['source'],'5');self.assertEqual(self.log(4),'redo')

    def test_table_change_binding_receives_table_records(self):
        self.apply('table_create',name='Items',columns=[dict(name='Title',type='text')])
        self.apply('table_create',name='Other',columns=[dict(name='Title',type='text')])
        self.bind('record_table',scope=dict(kind='table',table='Items'),event='table_change')
        self.apply('table_insert',table='Other',rows=[dict(Title='skip')]);self.assertIsNone(self.log(5))
        self.apply('table_insert',table='Items',rows=[dict(Title='one')]);self.assertEqual(json.loads(self.log(5)),[['1','table_insert']])

    def test_timeout_names_the_running_binding(self):
        first=self.bind('log_a',approve=False)
        while True:
            second=self.bind('spin',approve=False)
            if [item['id'] for item in bindings(self.book.db)]==[first,second]:break
            self.apply('binding_delete',binding_id=second,fingerprint=fingerprint(self.book.db))
        self.approve();previous={p.pid for p in multiprocessing.active_children()}
        outcome=self.failure(lambda:self.edit(2,2,'5'))
        self.assertEqual(outcome['code'],'BINDING_TIMEOUT');self.assertEqual(outcome['http_status'],408)
        self.assertTrue(outcome['message'].startswith('Binding supplies.spin:'),outcome['message'])
        self.assertEqual(self.raw('Supplies',2,2)['source'],'2');self.assertIsNone(self.log())
        self.assertEqual({p.pid for p in multiprocessing.active_children()},previous)

    def test_worker_crash_names_the_binding_and_leaves_no_worker(self):
        self.bind('crash');previous={p.pid for p in multiprocessing.active_children()}
        outcome=self.failure(lambda:self.edit(2,2,'5'))
        self.assertEqual(outcome['code'],'BINDING_FAILED');self.assertTrue(outcome['message'].startswith('Binding supplies.crash:'),outcome['message'])
        self.assertEqual(self.raw('Supplies',2,2)['source'],'2');self.assertEqual({p.pid for p in multiprocessing.active_children()},previous)
        self.assertFalse(any(t.name=='DataHorizonBindingBroker' for t in threading.enumerate()))

    def handler(self,mode):
        path=Path(self.temp.name)/('structure_%s.py'%mode);path.write_text(HANDLER%mode);self.book.handler=path

    def test_handler_edit_during_row_insert_reaches_bindings(self):
        self.bind('check_quantity')
        for mode,start in (('before',1),('after',10),('change',5)):
            with self.subTest(mode=mode):
                self.handler(mode)
                outcome=self.failure(lambda:self.apply('rows_insert',sheet='Supplies',start=start,count=1))
                self.assertEqual(outcome['code'],'BINDING_FAILED');self.assertIn('supplies.check_quantity',outcome['message'])
                self.assertEqual(self.raw('Supplies',2,2)['source'],'2');self.assertEqual(self.raw('Supplies',6,4)['source'],'=SUM(D2:D4)')
        self.book.handler=None;self.assertEqual(self.apply('rows_insert',sheet='Supplies',start=5,count=1)['outcome'],'committed')

    def test_sheet_structure_rewrites_do_not_call_bindings(self):
        self.apply('edit',sheet='Supplies',cells=[dict(row=2,col=5,source='=Log!A1')])
        self.bind('refuse',scope=dict(kind='sheet',sheet='Supplies'))
        self.bind('log_a',scope=dict(kind='range',reference="'Supplies'!E2"))
        # Approved: a binding that always raises is not called for the rewrite.
        self.assertEqual(self.apply('sheet_rename',sheet='Log',name='Logbook')['outcome'],'committed')
        self.assertIn('Logbook',self.raw('Supplies',2,5)['source']);self.assertIsNone(self.raw('Logbook',1,1))
        self.assertEqual(self.apply('sheet_duplicate',sheet='Supplies',name='Copy')['outcome'],'committed')
        # Unapproved required bindings do not block structural rewrites in their scope.
        self.book.approved_code=frozenset()
        self.assertEqual(self.apply('sheet_rename',sheet='Logbook',name='Log')['outcome'],'committed')
        self.assertEqual(self.raw('Supplies',2,5)['source'],"='Log'!A1")
        self.assertEqual(self.apply('sheet_delete',sheet='Log')['outcome'],'committed')
        self.assertNotIn('Log',self.raw('Supplies',2,5)['source'])
        self.assertEqual(self.failure(lambda:self.edit(2,5,'1'))['code'],'BINDING_NOT_APPROVED')

    def test_handler_edit_during_sheet_rename_reaches_bindings(self):
        self.bind('check_quantity');self.handler('after')
        self.assertEqual(self.failure(lambda:self.apply('sheet_rename',sheet='Log',name='Logbook'))['code'],'BINDING_FAILED')
        self.book.approved_code=frozenset()
        self.assertEqual(self.failure(lambda:self.apply('sheet_rename',sheet='Log',name='Logbook'))['code'],'BINDING_NOT_APPROVED')
        self.assertEqual(self.raw('Supplies',2,2)['source'],'2');self.assertIsNotNone(self.book.db.execute("SELECT 1 FROM sheets WHERE name='Log'").fetchone())

    def install_fragile(self):
        self.apply('module_save',name='fragile',source=FRAGILE,macros=[],fingerprint=fingerprint(self.book.db))

    def test_optional_binding_whose_module_fails_to_load_is_skipped(self):
        self.install_fragile()
        skipped=self.bind('on_edit',module='fragile',required=False);self.bind('log_a')
        receipt=self.edit(2,2,'5')
        self.assertEqual(receipt['binding_notices'],[dict(binding_id=skipped,function='on_edit',reason='BINDING_UNAVAILABLE')])
        self.assertEqual(self.raw('Supplies',2,2)['source'],'5');self.assertEqual(self.log(),'a')

    def test_required_binding_whose_module_fails_to_load_rejects(self):
        self.install_fragile()
        self.bind('on_edit',module='fragile',required=False);self.bind('other',module='fragile')
        outcome=self.failure(lambda:self.edit(2,2,'5'))
        self.assertEqual(outcome['code'],'BINDING_UNAVAILABLE');self.assertEqual(outcome['http_status'],409)
        self.assertIn('fragile.other',outcome['message']);self.assertIn('broken at import',outcome['message'])
        self.assertEqual(self.raw('Supplies',2,2)['source'],'2')

    def test_seventeen_rounds_is_hook_recursion(self):
        self.bind('bump',scope=dict(kind='sheet',sheet='Log'))
        outcome=self.failure(lambda:self.edit(1,1,'start',sheet='Log'))
        self.assertEqual(outcome['code'],'HOOK_RECURSION');self.assertIsNone(self.log());self.assertIsNone(self.log(9))

    def test_row_insert_does_not_call_cell_bindings(self):
        self.bind('log_a',scope=dict(kind='sheet',sheet='Supplies'))
        self.apply('rows_insert',sheet='Supplies',start=1,count=1);self.assertIsNone(self.log())
        self.apply('columns_delete',sheet='Supplies',start=3,count=1);self.assertIsNone(self.log())
        self.book.approved_code=frozenset()
        self.assertEqual(self.apply('rows_delete',sheet='Supplies',start=1,count=1)['outcome'],'committed')
        self.assertEqual(self.raw('Supplies',2,2)['source'],'2')

    def test_direct_sqlite_edit_runs_nothing_and_is_not_replayed(self):
        self.bind('check_quantity');self.bind('log_a')
        with closing(sqlite3.connect(self.book.path)) as direct:
            direct.execute("UPDATE cells SET source='-5' WHERE sheet_id=? AND row=2 AND col=2",(self.book.sheet('Supplies'),));direct.commit()
        self.book.refresh();self.assertEqual(self.raw('Supplies',2,2)['source'],'-5');self.assertIsNone(self.log())
        self.edit(9,1,'unrelated');self.assertIsNone(self.log())
        with closing(Workbook(self.book.path,approved_code=self.book.approved_code)) as reopened:
            reopened.mutate(dict(action='edit',operation_id=str(uuid.uuid4()),expected_state=reopened.state(),sheet='Supplies',cells=[dict(row=9,col=2,source='x')]))
            self.assertIsNone(reopened.raw(reopened.sheet('Log'),1,1));self.assertEqual(reopened.raw(reopened.sheet('Supplies'),2,2)['source'],'-5')

    def test_names_bindings_and_code_operations_stay_allowed_when_unapproved(self):
        self.bind('log_a',approve=False)
        self.assertEqual(self.apply('name_save',name='Prices',reference='Supplies!C2:C4')['outcome'],'committed')
        self.bind('log_b',approve=False)
        self.assertEqual(self.apply('module_save',name='extra',source='def nothing(ctx, changes):\n    pass\n',macros=[],fingerprint=fingerprint(self.book.db))['outcome'],'committed')
        self.assertEqual(self.failure(lambda:self.edit(2,2,'5'))['code'],'BINDING_NOT_APPROVED')


if __name__=='__main__':unittest.main()
