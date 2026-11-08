"""Versioned portable presentation and explicitly declared list definitions."""
import json
import math
import re
from .engine import require,integer,rectangle,boolean,encoded
from .temporal import Temporal


def name(value):
    require(isinstance(value,str) and 1<=len(value)<=64 and value==value.strip() and not any(ord(c)<32 or ord(c)==127 for c in value),'INVALID_METADATA','Names need 1–64 printable characters')
    return value


def list_spec(value):
    require(isinstance(value,dict) and not set(value)-{'r1','c1','r2','c2','header','sort','filters'},'INVALID_METADATA','Unsupported list definition')
    r1,c1,r2,c2=rectangle(value);boolean(value.get('header',False),'header')
    for key in ('sort','filters'):
        items=value.get(key,[]);require(isinstance(items,list) and len(items)<=8,'INVALID_METADATA','At most eight list criteria')
        for item in items:
            require(isinstance(item,dict),'INVALID_METADATA','List criteria must be objects');integer(item.get('col'),c1,c2,'column')
            if key=='sort':
                require(not set(item)-{'col','descending'},'INVALID_METADATA','Invalid sort key');boolean(item.get('descending',False),'descending')
            else:
                require(not set(item)-{'col','op','value','value_kind'} and item.get('op') in ('=','<>','<','>','<=','>=','contains'),'INVALID_METADATA','Invalid filter')
                scalar=item.get('value');require(type(scalar) in (str,int,float,bool,type(None)),'INVALID_METADATA','Filter must be scalar')
                require(type(scalar) not in (int,float) or math.isfinite(scalar),'INVALID_METADATA','Filter must be finite')
                if 'value_kind' in item:
                    require(item['value_kind'] in ('date','time','datetime') and item['op']!='contains','INVALID_METADATA','Invalid temporal filter')
                    try:Temporal(scalar,item['value_kind'])
                    except ValueError:require(False,'INVALID_METADATA','Invalid ISO filter value')
                if item['op']=='contains':require(type(scalar) is str,'INVALID_METADATA','Contains requires Text')
    return dict(r1=r1,c1=c1,r2=r2,c2=c2,header=value.get('header',False),sort=value.get('sort',[]),filters=value.get('filters',[]))


def presentation(value):
    require(isinstance(value,dict) and not set(value)-{'rows','columns','hidden_rows','hidden_columns','freeze_top_row','freeze_first_column','frozen_rows','frozen_columns','named_views'},'INVALID_METADATA','Unsupported presentation setting')
    for key,maximum,low,high in (('rows',1048576,18,1000),('columns',16384,30,2000)):
        sizes=value.get(key,{});require(isinstance(sizes,dict) and len(sizes)<=1024,'INVALID_METADATA','At most 1,024 section sizes')
        for section,size in sizes.items():
            require(isinstance(section,str) and re.fullmatch(r'0|[1-9][0-9]{0,6}',section),'INVALID_METADATA','Invalid section key');integer(int(section),0,maximum-1,'section');integer(size,low,high,'size')
    for key,maximum in (('hidden_rows',1048576),('hidden_columns',16384)):
        sections=value.get(key,[]);require(isinstance(sections,list) and len(sections)<=1024,'INVALID_METADATA','At most 1,024 hidden sections')
        for section in sections:integer(section,0,maximum-1,'hidden section')
        require(len(set(sections))==len(sections),'INVALID_METADATA','Duplicate hidden sections')
    for key,maximum in (('frozen_rows',16),('frozen_columns',8)):
        if key in value:integer(value[key],0,maximum,key)
    for key in ('freeze_top_row','freeze_first_column'):
        if key in value:boolean(value[key],key)
    views=value.get('named_views',{});require(isinstance(views,dict) and len(views)<=16,'INVALID_METADATA','At most 16 named lists')
    for label,spec in views.items():name(label);list_spec(spec)
    return value


def validate(namespace,identifier,value):
    name(identifier);require(namespace in ('presentation','list'),'INVALID_METADATA','Unsupported metadata namespace')
    require(len(encoded(value).encode())<=256*1024,'LIMIT','Metadata payload exceeds 256 KiB')
    return presentation(value) if namespace=='presentation' else list_spec(value)


def read(book,sheet,args):
    with book.lock:
        book.refresh();book.db.execute('BEGIN')
        try:
            book.refresh();require(args.get('expected_state')==book.state(),'STALE_STATE','Refresh metadata before continuing',409)
            identity=book.sheet(sheet)
            rows=[] if book.db.execute('SELECT format_version FROM dh_meta').fetchone()[0]<3 else book.db.execute('SELECT namespace,id,payload FROM dh_objects WHERE sheet_id=? ORDER BY namespace,id',(identity,)).fetchall()
            result=dict(state=book.state(),sheet=identity,objects=[dict(namespace=n,id=i,payload=json.loads(p)) for n,i,p in rows if n in ('presentation','list')])
            require(len(encoded(result).encode())<=4*1024*1024,'READ_LIMIT','Metadata response exceeds 4 MiB');return result
        finally:book.db.execute('ROLLBACK')
