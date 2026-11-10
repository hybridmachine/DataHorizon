# SPDX-License-Identifier: GPL-3.0-only
"""Reproducible relational acceptance data; all managed work uses REST SDK calls."""
from contextlib import closing
from pathlib import Path
import argparse
import hashlib
import json
import sqlite3
import time
import datahorizon
from datahorizon.client import LocalService,APIError

CUSTOMERS=[['CustomerID','Name'],['C001','Ada'],['C002','Grace']]
ORDERS=[['OrderID','CustomerID','Quantity','UnitPrice'],['O100','C001',2,12.5],['O101','C001',1,10],['O102','C002',3,7]]
SQL='''SELECT c.CustomerID, c.Name,
       COUNT(o.OrderID) AS OrderCount,
       COALESCE(SUM(o.Quantity * o.UnitPrice), 0) AS Total
FROM Customers AS c
LEFT JOIN Orders AS o ON o.CustomerID = c.CustomerID
GROUP BY c.CustomerID, c.Name
HAVING COALESCE(SUM(o.Quantity * o.UnitPrice), 0) >= :minimum_total
ORDER BY c.CustomerID;'''


def complete(book,result):
    deadline=time.monotonic()+10
    while result['status']=='running' and time.monotonic()<deadline:time.sleep(.01);result=book.query(result['job_id'])
    if result['status']!='completed':raise RuntimeError('Query did not complete: '+json.dumps(result))
    return result


def totals(book,minimum=0):
    result=complete(book,book.run_saved_query('Customer totals',values=dict(minimum_total=minimum)))
    try:return [row['values'] for row in book.query(result['job_id'],'page')['rows']]
    finally:book.query(result['job_id'],'release')


def create(client,path):
    with client.open(path,create=True) as book:
        book.apply('sheet_rename',sheet='Sheet1',name='Automation')
        book.apply('edit',sheet='Automation',cells=[dict(row=1,col=1,source='No handler changes yet',kind='text')])
        for name,rows in (('Customers source',CUSTOMERS),('Orders source',ORDERS)):
            book.apply('sheet_add',name=name);cells=[]
            for r,row in enumerate(rows,1):
                for c,value in enumerate(row,1):cells.append(dict(row=r,col=c,source=str(value),kind='number' if type(value) in (int,float) else 'text'))
            book.apply('edit',sheet=name,cells=cells)
        columns=[dict(name='CustomerID',required=True,unique=True),dict(name='Name')]
        book.promote_table(book.preview_table('Customers','Customers source',r1=1,c1=1,r2=3,c2=2,header=True,columns=columns))
        columns=[dict(name='OrderID',required=True,unique=True),dict(name='CustomerID',required=True),dict(name='Quantity',type='integer',required=True),dict(name='UnitPrice',type='number',required=True)]
        book.promote_table(book.preview_table('Orders','Orders source',r1=1,c1=1,r2=4,c2=4,header=True,columns=columns))
        book.create_relationship('CustomerOrders','Customers','CustomerID','Orders','CustomerID')
        catalog=book.tables();orders=next(item for item in catalog['tables'] if item['name']=='Orders');quantity=next(c['id'] for c in orders['columns'] if c['name']=='Quantity')
        book.create_index('Orders','OrdersByQuantity',[dict(column=quantity,direction='asc')])
        review=book.preview_query('Customer totals',SQL,parameters=[dict(name='minimum_total',type='number')],defaults=dict(minimum_total=0));book.save_query(review)
        return dict(tables=book.tables()['tables'],baseline=totals(book),minimum_30=totals(book,30),format_version=book.refresh()['format_version'])


def exercise(client,path,*,handler=False,exports=None):
    with client.open(path) as book:
        baseline=totals(book);assert baseline==[['C001','Ada',2,35.0],['C002','Grace',1,21.0]],baseline
        assert totals(book,30)==[['C001','Ada',2,35.0]]
        orders=next(item for item in book.tables()['tables'] if item['name']=='Orders');columns={c['name']:c['id'] for c in orders['columns']}
        rows=book.read_table('Orders',filters=[dict(column=columns['OrderID'],op='eq',value='O100')])['records'];row=rows[0]['row_id']
        before_marker=book.read('Automation',1,1,1,1)['cells']
        book.update_records('Orders',[dict(row_id=row,values=dict(Quantity=4))]);changed=totals(book);assert changed==[['C001','Ada',2,60.0],['C002','Grace',1,21.0]],changed
        book.apply('undo');assert totals(book)==baseline
        assert book.read('Automation',1,1,1,1)['cells']==before_marker
        if handler:
            try:book.update_records('Orders',[dict(row_id=row,values=dict(Quantity=0))])
            except APIError as error:assert error.body['code']=='HOOK_FAILED' and error.body['outcome']=='rolled_back',error.body
            else:raise AssertionError('Activated fixture rule did not reject Quantity=0')
            assert totals(book)==baseline;assert book.read('Automation',1,1,1,1)['cells']==before_marker
        customer=next(item for item in book.tables()['tables'] if item['name']=='Customers');key=next(c['id'] for c in customer['columns'] if c['name']=='CustomerID');target=book.read_table('Customers',filters=[dict(column=key,op='eq',value='C001')])['records'][0]['row_id']
        try:book.delete_records('Customers',[target])
        except APIError as error:assert error.body['outcome']=='rolled_back'
        else:raise AssertionError('Referenced parent deletion unexpectedly committed')
        exported=[]
        if exports:
            exports=Path(exports);exports.mkdir(parents=True,exist_ok=True);result=complete(book,book.run_saved_query('Customer totals'))
            try:
                for format in ('csv','xlsx','ods'):
                    snapshot=book.export_query(result['job_id'],format);published=book.publish_export(snapshot,exports/('customer-totals.'+format));assert snapshot['records']==2;exported.append({**published,'sha256':hashlib.sha256((exports/('customer-totals.'+format)).read_bytes()).hexdigest()})
            finally:book.query(result['job_id'],'release')
        return dict(baseline=baseline,minimum_30=totals(book,30),changed_quantity=changed,undo_restored=True,required_rule_checked=handler,parent_restrict_checked=True,exports=exported,final_state=book.state)


def independent(path,orphan=False):
    with closing(sqlite3.connect(path,isolation_level=None)) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        marker=db.execute("SELECT source FROM cells JOIN sheets ON sheets.id=cells.sheet_id WHERE sheets.name='Automation' AND row=1 AND col=1").fetchone()
        if orphan:db.execute("UPDATE Orders SET CustomerID='C999' WHERE OrderID='O102'")
        else:db.execute("UPDATE Orders SET Quantity=4 WHERE OrderID='O102'")
        assert db.execute("SELECT source FROM cells JOIN sheets ON sheets.id=cells.sheet_id WHERE sheets.name='Automation' AND row=1 AND col=1").fetchone()==marker
    return dict(mode='independent SQLite',handlers=False,orphan=orphan,automation_marker_unchanged=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['create','verify','exercise','direct-edit','orphan','repair']);parser.add_argument('workbook',type=Path);parser.add_argument('--handler',type=Path);parser.add_argument('--exports',type=Path);parser.add_argument('--report',type=Path);args=parser.parse_args()
    if args.action in ('direct-edit','orphan'):result=independent(args.workbook,args.action=='orphan')
    else:
        with LocalService(args.handler) as client:
            if args.action=='create':result=create(client,args.workbook)
            elif args.action=='exercise':result=exercise(client,args.workbook,handler=args.handler is not None,exports=args.exports)
            elif args.action=='verify':
                with client.open(args.workbook) as book:result=dict(totals=totals(book),catalog=book.tables())
            else:
                with client.open(args.workbook,repair=True) as book:
                    report=book.inspect_relationships();assert len(report['orphans'])==1,report;orphan=report['orphans'][0];review=book.preview_orphan_repair(orphan['relationship'],'update',edits=[dict(row_id=orphan['row_id'],values=dict(CustomerID='C002'))]);book.apply_orphan_repair(review);result=dict(repaired=True,remaining=book.inspect_relationships()['orphans'])
    result.update(sdk_module=str(Path(datahorizon.__file__).resolve()),workbook=str(args.workbook.resolve()))
    if args.workbook.exists():result['workbook_sha256']=hashlib.sha256(args.workbook.read_bytes()).hexdigest()
    output=json.dumps(result,indent=2);print(output)
    if args.report:args.report.write_text(output+'\n')

if __name__=='__main__':main()
