# SPDX-License-Identifier: GPL-3.0-only
"""OpenAPI contract for workbook names: sheet-qualified rectangles with stable identity."""


def install(schemas,actions):
    name=dict(type='string',minLength=1,maxLength=64,description='Name identifier of at most 64 characters; at most 256 names per workbook')
    reference=dict(type='string',minLength=1,description="Sheet-qualified cell or rectangle such as Sheet1!A1:B2 or 'My Sheet'!C3")
    name_id=dict(type='string',format='uuid')
    actions['name_save']=(['name','reference'],dict(name=name,reference=reference,name_id={**name_id,'description':'Present to rename or retarget an existing name'},sheet=dict(type='string')),dict(name='Quantities',reference='Sheet1!B2:B4'))
    actions['name_delete']=(['name_id'],dict(name_id=name_id,sheet=dict(type='string')),dict(name_id='00000000-0000-4000-8000-000000000000'))
    schemas['NamesReadRequest']=dict(type='object',additionalProperties=False,required=['expected_state'],properties=dict(expected_state=dict(type='string',minLength=1),name=name),example=dict(expected_state='copy-from-workbook-info'))
    schemas['WorkbookName']=dict(type='object',required=['id','name','sheet_id','row1','col1','row2','col2','sheet','reference','broken'],properties=dict(id=name_id,name=name,sheet_id=dict(type='string',nullable=True),row1=dict(type='integer',nullable=True),col1=dict(type='integer',nullable=True),row2=dict(type='integer',nullable=True),col2=dict(type='integer',nullable=True),sheet=dict(type='string',nullable=True),reference=dict(type='string',nullable=True,description='Null when the name is broken'),broken=dict(type='boolean')))
    schemas['NameCatalog']=dict(type='object',required=['state','names'],properties=dict(state=dict(type='string'),names=dict(type='array',maxItems=256,items={'$ref':'#/components/schemas/WorkbookName'})),description='Names are inert metadata; they are not yet usable inside formulas.')
    schemas['Receipt']['properties'].update(broken_names=dict(type='array',items=dict(type='string'),description='Names broken by this structural change'))
