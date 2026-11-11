# SPDX-License-Identifier: GPL-3.0-only
"""OpenAPI schemas for managed structured tables and query resources."""


def install(schemas,actions):
    column={'type':'object','additionalProperties':False,'required':['name'],'properties':{
        'id':{'type':'string','format':'uuid'},
        'name':{'type':'string','pattern':'^[A-Za-z][A-Za-z0-9_]{0,63}$'},
        'type':{'type':'string','enum':['text','integer','number','boolean','date','time','datetime'],'default':'text'},
        'required':{'type':'boolean','default':False},'unique':{'type':'boolean','default':False}}}
    schemas['TableColumn']=column
    schemas['TableCatalogRequest']={'type':'object','required':['expected_state'],'additionalProperties':False,
        'properties':{'expected_state':{'type':'string','minLength':1}},
        'example':{'expected_state':'copy-from-workbook-info'}}
    actions['table_create']=(['name','columns'],{'name':column['properties']['name'],
        'columns':{'type':'array','minItems':1,'maxItems':256,'items':{'$ref':'#/components/schemas/TableColumn'}}},
        {'name':'Customers','columns':[{'name':'CustomerID','type':'text','required':True,'unique':True},{'name':'Name'}]})
    actions['table_drop']=(['table'],{'table':{'type':'string','minLength':1},'allow_data_loss':{'type':'boolean','default':False}},{'table':'Customers'})
    preview={'name':column['properties']['name'],'sheet':{'type':'string'},'expected_state':{'type':'string','minLength':1},
        'header':{'type':'boolean','default':True},'columns':{'type':'array','minItems':1,'maxItems':256,'items':{'$ref':'#/components/schemas/TableColumn'}},
        'conversion_policy':{'type':'string','enum':['strict','invariant'],'default':'strict'},'preview_clock':{'type':'string'},
        **{key:{'type':'integer','minimum':1,'maximum':1048576 if key.startswith('r') else 16384} for key in ('r1','c1','r2','c2')}}
    schemas['TablePreviewRequest']={'type':'object','required':['expected_state','name'],'properties':preview,
        'example':{'name':'Snapshot','sheet':'Sheet1','r1':1,'r2':1,'c1':1,'c2':1,'header':True,'columns':[{'name':'Value'}],'expected_state':'copy-from-workbook-info'}}
    actions['table_promote']=(['name','columns','review_digest','preview_clock'],{**preview,'review_digest':{'type':'string','pattern':'^[0-9a-f]{64}$'}},
        {'name':'Snapshot','sheet':'Sheet1','r1':1,'r2':1,'c1':1,'c2':1,'header':True,'columns':[{'name':'Value'}],
         'review_digest':'replace-with-reviewed-digest','preview_clock':'2026-11-09T12:00:00'})
    schemas['TableReadRequest']={'type':'object','required':['table','expected_state'],'properties':{
        'table':{'type':'string'},'expected_state':{'type':'string'},'after':{'type':'string','pattern':'^[1-9][0-9]{0,18}$'},
        'columns':{'type':'array','maxItems':256,'uniqueItems':True,'items':{'type':'string','format':'uuid'}},
        'limit':{'type':'integer','minimum':1,'maximum':256,'description':'Combined projected width/height is at most 4,096 values'}},
        'example':{'table':'Customers','expected_state':'copy-from-workbook-info','limit':32}}
    schema={'table':{'type':'string','minLength':1},'name':column['properties']['name'],
        'columns':preview['columns'],'expected_state':preview['expected_state'],'conversion_policy':preview['conversion_policy'],
        'drop_columns':{'type':'array','uniqueItems':True,'maxItems':256,'items':{'type':'string','format':'uuid'}}}
    schemas['TableSchemaPreviewRequest']={'type':'object','required':['table','columns','expected_state'],'properties':schema,
        'example':{'table':'Snapshot','columns':[{'id':'replace-with-current-column-id','name':'Value','type':'number'}],'expected_state':'copy-from-workbook-info'}}
    actions['table_schema']=(['table','columns','review_digest'],{**schema,'review_digest':{'type':'string','pattern':'^[0-9a-f]{64}$'}},
        {'table':'Snapshot','columns':[{'id':'replace-with-current-column-id','name':'Value','type':'number'}],'review_digest':'replace-with-reviewed-digest'})
