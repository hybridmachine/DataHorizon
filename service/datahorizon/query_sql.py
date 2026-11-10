# SPDX-License-Identifier: GPL-3.0-only
"""Read-only SQL authorization, exact named bindings and lossless scalar transport."""
import math
import re
import sqlite3
from .engine import EngineError,require
from .relational import TYPES
from .table_promotion import convert

FUNCTIONS=set('abs avg coalesce count group_concat ifnull iif instr length like glob likelihood likely unlikely lower ltrim max min nullif printf format quote replace round rtrim sign substr substring sum total trim typeof unicode upper hex unhex date datetime time julianday strftime unixepoch timediff json json_extract json_array json_object json_valid json_type json_array_length json_group_array json_group_object row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value nth_value sqlite_version'.split())


def names(sql):
    require(isinstance(sql,str) and 0<len(sql.encode())<=65536 and '\0' not in sql,'INVALID_SQL','SQL needs 1–65,536 UTF-8 bytes without NUL')
    # SQLite quotes/comments are skipped before recognizing colon names. Other
    # parameter syntaxes are deliberately excluded from this contract.
    found=set();first=None;i=0
    while i<len(sql):
        ch=sql[i]
        if ch.isspace() or ch=='\ufeff':i+=1;continue
        if sql.startswith('--',i):
            end=sql.find('\n',i+2);i=len(sql) if end<0 else end+1;continue
        if sql.startswith('/*',i):
            end=sql.find('*/',i+2);require(end>=0,'INVALID_SQL','Unterminated SQL comment');i=end+2;continue
        if ch in "'\"`[":
            quote=']' if ch=='[' else ch;i+=1
            while i<len(sql):
                if sql[i]==quote:
                    i+=1
                    if quote!=']' and i<len(sql) and sql[i]==quote:i+=1;continue
                    break
                i+=1
            continue
        if ch==':':
            match=re.match(r'[A-Za-z][A-Za-z0-9_]{0,63}',sql[i+1:]);require(match is not None,'INVALID_SQL','Use :name parameters with ASCII letters/digits/underscores')
            found.add(match.group());i+=len(match.group())+1;continue
        require(ch not in '?@$','INVALID_SQL','Only :name parameters are supported')
        match=re.match(r'[A-Za-z_][A-Za-z0-9_]*',sql[i:])
        if match:
            if first is None:first=match.group().upper()
            i+=len(match.group())
        else:i+=1
    require(first in ('SELECT','WITH'),'SQL_DENIED','This workbench accepts one read-only SELECT or CTE')
    require(len(found)<=64,'LIMIT','At most 64 named parameters');return found


def bindings(sql,declarations,values):
    used=names(sql);require(isinstance(declarations,list) and len(declarations)<=64 and isinstance(values,dict),'INVALID_PARAMETERS','Declare up to 64 typed named parameters')
    result={};normalized=[]
    for declaration in declarations:
        require(isinstance(declaration,dict) and not set(declaration)-{'name','type','nullable'} and isinstance(declaration.get('name'),str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',declaration['name']),'INVALID_PARAMETERS','Invalid named parameter declaration')
        key=declaration['name'];kind=declaration.get('type','text');nullable=declaration.get('nullable',False)
        require(key not in result and kind in TYPES and type(nullable) is bool and key in values,'INVALID_PARAMETERS','Each parameter needs a unique name, declared type and explicit value')
        stored=convert(dict(name=key,type=kind,required=not nullable),values[key],'invariant');result[key]=int(stored) if type(stored) is bool else stored
        normalized.append(dict(name=key,type=kind,nullable=nullable))
    require(set(result)==used==set(values),'INVALID_PARAMETERS','Declarations and values must exactly match the SQL :names')
    return normalized,result


def authorize(db,registered):
    known={t['name'].lower():t for t in registered};dependencies={};denied=[]
    def check(action,one,two,database,trigger):
        if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_RECURSIVE):return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_READ:
            item=known.get((one or '').lower())
            if database in (None,'main') and item is not None and (not two or any(c['name'].lower()==two.lower() for c in item['columns']) or two.lower()=='_dh_row_id'):
                column=next((c['id'] for c in item['columns'] if c['name'].lower()==(two or '').lower()),'*' if not two else '_dh_row_id')
                dependencies.setdefault(item['id'],set()).add(column);return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_FUNCTION and (two or '').lower() in FUNCTIONS:return sqlite3.SQLITE_OK
        if not denied:denied.append(dict(action=action,object=one or two))
        return sqlite3.SQLITE_DENY
    db.set_authorizer(check);return dependencies,denied


def configure(db):
    db.execute('PRAGMA query_only=ON');db.execute('PRAGMA temp_store=FILE');db.execute('PRAGMA cache_size=-8192')
    # Native tables have up to 256 user columns plus reserved identity. The
    # result-width cap is checked separately after preparation.
    for category,limit in ((sqlite3.SQLITE_LIMIT_LENGTH,4*1024*1024),(sqlite3.SQLITE_LIMIT_SQL_LENGTH,65536),(sqlite3.SQLITE_LIMIT_COLUMN,512),
        (sqlite3.SQLITE_LIMIT_EXPR_DEPTH,100),(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT,32),(sqlite3.SQLITE_LIMIT_FUNCTION_ARG,64),
        (sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,64),(sqlite3.SQLITE_LIMIT_ATTACHED,0),(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH,4096),(sqlite3.SQLITE_LIMIT_VDBE_OP,250000)):
        db.setlimit(category,limit)


def scalar(value):
    if type(value) is int and abs(value)>2**53-1:return dict(type='integer',value=str(value))
    require(value is None or type(value) in (str,int,float),'QUERY_VALUE','BLOB query results require an explicit SQL representation such as hex()',409)
    require(not isinstance(value,str) or len(value.encode())<=65536,'QUERY_LIMIT','Query Text exceeds 64 KiB',409)
    require(type(value) is not float or math.isfinite(value),'QUERY_VALUE','Nonfinite SQL results are not representable',409)
    return value
