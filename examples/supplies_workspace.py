# SPDX-License-Identifier: GPL-3.0-only
"""Build the storyboard's Supplies workbook through the SDK, or exercise it after owner approval."""
import argparse
import json
from pathlib import Path
import time
import datahorizon
from datahorizon.client import APIError,LocalService

# Workbook code supplied by the workbook author. Storage never executes it.
MODULE='''def restock_pens(ctx, extra):
    quantity = ctx.value("'Supplies'!B3") + extra
    if quantity < 0:
        raise ValueError("Below zero")
    ctx.set("PenQuantity", quantity)
    print("Pen quantity is now", quantity)
    return ctx.value("Total")


def check_quantity(ctx, changes):
    for change in changes:
        if change["after"] is None:
            continue
        value = ctx.value(change["sheet"], change["row"], change["col"])
        if isinstance(value, (int, float)) and value < 0:
            raise ValueError("Below zero")
'''
DECLARATIONS=[dict(name='restock_pens',parameters=[dict(name='extra',type='number')],result=dict(type='number'))]
ROWS=[('Notebook','2','3.50'),('Pen','4','1.25'),('Folder','3','2.00')]
NAMES=[('PenQuantity',"'Supplies'!B3"),('Quantities',"'Supplies'!B2:B4"),('Total',"'Supplies'!D6")]


def build(book):
    book.apply('sheet_rename',sheet='Sheet1',name='Supplies')
    cells=[dict(row=1,col=col,source=text) for col,text in enumerate(['Item','Quantity','Unit price','Amount'],1)]
    for row,(item,quantity,price) in enumerate(ROWS,2):
        cells+=[dict(row=row,col=1,source=item),dict(row=row,col=2,source=quantity),dict(row=row,col=3,source=price),dict(row=row,col=4,source='=B%d*C%d'%(row,row))]
    cells.append(dict(row=6,col=4,source='=SUM(D2:D4)'))
    book.apply('edit','Supplies',cells=cells,label='Supplies')
    for name,reference in NAMES:book.save_name(name,reference)
    book.save_module('supplies',MODULE,DECLARATIONS,fingerprint=book.code()['fingerprint'])
    book.save_binding('cell_change',dict(kind='name',name='Quantities'),'supplies','check_quantity',fingerprint=book.code()['fingerprint'])


def value(book,name):
    target=next(item for item in book.names()['names'] if item['name']==name)
    return book.read(target['sheet'],target['row1'],target['col1'],target['row1'],target['col1'])['cells'][0]['value']


def rejection(error):
    return {key:error.body.get(key) for key in ('code','outcome','message','binding','macro') if key in error.body}


def exercise(service,book,code,report):
    service.approve(code['activation_key'])
    end=time.monotonic()+10
    while not book.code()['approved']:
        if time.monotonic()>end:raise RuntimeError('Approval did not reach the open workbook')
        time.sleep(.05)
    report.update(approved=True,total_before=value(book,'Total'))
    result=book.run_macro('supplies','restock_pens',fingerprint=code['fingerprint'],arguments=dict(extra=2))
    report.update(macro_result=result['macro_result'],macro_logs=result['macro']['logs'],total_after_run=value(book,'Total'),pen_quantity_after_run=value(book,'PenQuantity'))
    book.apply('undo')
    report.update(total_after_undo=value(book,'Total'),pen_quantity_after_undo=value(book,'PenQuantity'))
    try:book.run_macro('supplies','restock_pens',fingerprint=code['fingerprint'],arguments=dict(extra=-10))
    except APIError as error:report['failed_run']=rejection(error)
    else:raise AssertionError('restock_pens accepted a negative quantity')
    report['total_after_failure']=value(book,'Total')
    try:book.set(2,2,'-1','Supplies')
    except APIError as error:report['rejected_edit']=rejection(error)
    else:raise AssertionError('check_quantity accepted a negative quantity')
    report['quantity_after_rejection']=book.read('Supplies',2,2,2,2)['cells'][0]['value']
    book.set(2,2,'3','Supplies');report['accepted_edit_total']=value(book,'Total')
    book.apply('undo')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['create','exercise']);parser.add_argument('workbook',type=Path);parser.add_argument('--report',type=Path);args=parser.parse_args()
    service=LocalService(owner_channel=args.action=='exercise')
    with service as client,client.open(args.workbook,create=args.action=='create') as book:
        if args.action=='create':build(book)
        code=book.code()
        report=dict(sdk_module=str(Path(datahorizon.__file__).resolve()),format_version=book.refresh()['format_version'],activation_key=code['activation_key'],fingerprint=code['fingerprint'],approved=code['approved'])
        if args.action=='create':
            report.update(modules=[dict(name=item['name'],macros=item['macros']) for item in code['modules']],
                          names=[dict(name=item['name'],reference=item['reference']) for item in book.names()['names']],
                          bindings=[dict(event=item['event'],module=item['module'],function=item['function'],required=item['required'],scope=item['scope']['kind'],reference=item['reference']) for item in code['bindings']])
        else:exercise(service,book,code,report)
        report['final_state']=book.state
    output=json.dumps(report,indent=2)
    if args.report:args.report.write_text(output+'\n')
    print(output)


if __name__=='__main__':main()
