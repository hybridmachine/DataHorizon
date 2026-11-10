# SPDX-License-Identifier: GPL-3.0-only
"""OpenAPI schemas for managed structured tables and query resources."""


def install(schemas,actions):
    column={'type':'object','additionalProperties':False,'required':['name'],'properties':{
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
    actions['table_drop']=(['table'],{'table':{'type':'string','minLength':1}},{'table':'Customers'})
