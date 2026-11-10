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
        'cursor':{'type':'string','maxLength':16384},'preview':{'type':'boolean','default':False},'count':{'type':'boolean','default':False},
        'sort':{'type':'array','maxItems':8,'items':{'type':'object','required':['column','direction'],'properties':{'column':{'type':'string','format':'uuid'},'direction':{'type':'string','enum':['asc','desc']}}}},
        'filters':{'type':'array','maxItems':8,'items':{'type':'object','required':['column','op'],'properties':{'column':{'type':'string','format':'uuid'},'op':{'type':'string','enum':['eq','ne','lt','le','gt','ge','contains','is_null','not_null']},'value':{'oneOf':[{'type':'string','nullable':True},{'type':'number'},{'type':'boolean'}]}}}},
        'table':{'type':'string'},'expected_state':{'type':'string'},'after':{'type':'string','pattern':'^[1-9][0-9]{0,18}$'},
        'columns':{'type':'array','maxItems':256,'uniqueItems':True,'items':{'type':'string','format':'uuid'}},
        'record_ids':{'type':'array','minItems':1,'maxItems':256,'uniqueItems':True,'items':{'type':'string','pattern':'^[1-9][0-9]{0,18}$'}},
        'limit':{'type':'integer','minimum':1,'maximum':256,'description':'Combined projected width/height is at most 4,096 values'}},
        'example':{'table':'Customers','expected_state':'copy-from-workbook-info','limit':32}}
    schema={'table':{'type':'string','minLength':1},'name':column['properties']['name'],
        'columns':preview['columns'],'expected_state':preview['expected_state'],'conversion_policy':preview['conversion_policy'],
        'drop_columns':{'type':'array','uniqueItems':True,'maxItems':256,'items':{'type':'string','format':'uuid'}}}
    schemas['TableSchemaPreviewRequest']={'type':'object','required':['table','columns','expected_state'],'properties':schema,
        'example':{'table':'Snapshot','columns':[{'id':'replace-with-current-column-id','name':'Value','type':'number'}],'expected_state':'copy-from-workbook-info'}}
    actions['table_schema']=(['table','columns','review_digest'],{**schema,'review_digest':{'type':'string','pattern':'^[0-9a-f]{64}$'}},
        {'table':'Snapshot','columns':[{'id':'replace-with-current-column-id','name':'Value','type':'number'}],'review_digest':'replace-with-reviewed-digest'})
    scalar={'oneOf':[{'type':'string','nullable':True},{'type':'number'},{'type':'boolean'}]}
    actions['index_create']=(['table','name','columns'],{'table':{'type':'string'},'name':column['properties']['name'],
        'columns':{'type':'array','minItems':1,'maxItems':16,'items':{'type':'object','required':['column','direction'],'properties':{'column':{'type':'string','format':'uuid'},'direction':{'type':'string','enum':['asc','desc']}}}},'unique':{'type':'boolean','default':False}},
        {'table':'Snapshot','name':'SnapshotValueIndex','columns':[{'column':'replace-with-current-column-id','direction':'asc'}]})
    actions['index_drop']=(['index'],{'index':{'type':'string'}},{'index':'SnapshotValueIndex'})
    values={'type':'object','additionalProperties':scalar,'description':'Column IDs or exact catalog names as keys; numbers are finite and all values follow declared types'}
    row_id={'type':'string','pattern':'^[1-9][0-9]{0,18}$'}
    data={'table':{'type':'string','minLength':1},'review_digest':{'type':'string','pattern':'^[0-9a-f]{64}$'},
        'rows':{'type':'array','minItems':1,'maxItems':10000,'items':values},
        'edits':{'type':'array','minItems':1,'maxItems':10000,'items':{'type':'object','additionalProperties':False,'required':['row_id','values'],'properties':{'row_id':row_id,'values':values}}},
        'record_ids':{'type':'array','minItems':1,'maxItems':10000,'uniqueItems':True,'items':row_id}}
    for action,required,example in (
        ('table_insert','rows',{'rows':[{'Value':2}]}),
        ('table_update','edits',{'edits':[{'row_id':'1','values':{'Value':3}}]}),
        ('table_delete','record_ids',{'record_ids':['1']})):
        actions[action]=(['table',required],{'table':data['table'],required:data[required],'review_digest':data['review_digest']},{'table':'Snapshot',**example})
    schemas['TableEditPreviewRequest']={'type':'object','required':['table','action','expected_state'],'properties':{**data,
        'expected_state':preview['expected_state'],'action':{'type':'string','enum':['table_insert','table_update','table_delete']},'blank_as_null':{'type':'boolean','default':False}},
        'description':'Read-only invariant type conversion. Apply the returned canonical operation and digest at the reviewed state. Native constraints and Python rules execute on Apply.',
        'example':{'table':'Snapshot','action':'table_insert','rows':[{'Value':'2.5'}],'expected_state':'copy-from-workbook-info'}}
    relation={key:{'type':'string','minLength':1} for key in ('name','parent_table','parent_column','child_table','child_column')}
    relation.update(on_delete={'type':'string','enum':['restrict'],'default':'restrict'},on_update={'type':'string','enum':['restrict'],'default':'restrict'})
    actions['relationship_create']=(['name','parent_table','parent_column','child_table','child_column'],relation,
        dict(name='ParentChild',parent_table='RelationParents',parent_column='Key',child_table='RelationChildren',child_column='Key'))
    actions['relationship_drop']=(['relationship'],{'relationship':{'type':'string'}},{'relationship':'ParentChild'})
    repair={'relationship':{'type':'string'},'mode':{'type':'string','enum':['update','delete']},'edits':data['edits'],'record_ids':data['record_ids'],'review_digest':data['review_digest']}
    actions['table_repair']=(['relationship','mode','review_digest'],repair,{'relationship':'ParentChild','mode':'update','edits':[{'row_id':'1','values':{'Key':'A'}}],'review_digest':'replace-with-reviewed-digest'})
    schemas['RelationshipRepairRequest']={'type':'object','required':['relationship','mode','expected_state'],'properties':{**repair,'expected_state':preview['expected_state']},'example':{'relationship':'ParentChild','mode':'update','edits':[{'row_id':'1','values':{'Key':'A'}}],'expected_state':'copy-from-workbook-info'}}
