# SPDX-License-Identifier: GPL-3.0-only
"""OpenAPI contract for event bindings: inert workbook-function attachments run by approved managed edits."""


def install(schemas,actions):
    digest=dict(type='string',pattern='^[0-9a-f]{64}$')
    binding_id=dict(type='string',format='uuid')
    def scope(kind,field,description,example):
        return dict(type='object',additionalProperties=False,required=['kind',field],properties={'kind':dict(type='string',enum=[kind]),field:dict(type='string',minLength=1,description=description)},example=example)
    schemas['BindingScope']={'oneOf':[
        scope('sheet','sheet','Sheet name or ID',dict(kind='sheet',sheet='Supplies')),
        scope('range','reference',"Sheet-qualified rectangle such as 'Supplies'!B2:B4",dict(kind='range',reference="'Supplies'!B2:B4")),
        scope('name','name','Workbook name',dict(kind='name',name='Quantities')),
        scope('table','table','Table name or ID',dict(kind='table',table='Orders'))],
        'description':'cell_change allows sheet, range and name scopes; table_change allows table scopes.'}
    actions['binding_save']=(['event','scope','module','function','fingerprint'],dict(
        event=dict(type='string',enum=['cell_change','table_change']),scope={'$ref':'#/components/schemas/BindingScope'},
        module=dict(type='string',minLength=1,description='Module name or ID'),function=dict(type='string',minLength=1,description='Top-level def in the module'),
        required=dict(type='boolean',default=True,description='A required binding rejects the whole operation when it fails or is not approved'),
        fingerprint=digest,binding_id={**binding_id,'description':'Present to change an existing binding; at most 16 bindings per workbook'},sheet=dict(type='string')),
        dict(event='cell_change',scope=dict(kind='range',reference="'Sheet1'!B2:B4"),module='Examples',function='check_quantity',required=True,fingerprint='0'*64))
    actions['binding_delete']=(['binding_id','fingerprint'],dict(binding_id=binding_id,fingerprint=digest,sheet=dict(type='string')),dict(binding_id='00000000-0000-4000-8000-000000000000',fingerprint='0'*64))
    stored_scope=dict(type='object',description='Stored scope with stable identities; coordinates are null when the scope is broken',properties=dict(kind=dict(type='string',enum=['sheet','range','name','table'])))
    schemas['WorkbookBinding']=dict(type='object',required=['id','event','module_id','function','required','scope','module','reference','broken'],properties=dict(
        id=binding_id,event=dict(type='string',enum=['cell_change','table_change']),module_id=dict(type='string',format='uuid'),function=dict(type='string'),required=dict(type='boolean'),
        scope=stored_scope,module=dict(type='string',nullable=True),reference=dict(type='string',nullable=True,description='Null when the scope is broken or has no cell reference'),broken=dict(type='boolean')))
    schemas['CodeCatalog']['properties']['bindings']=dict(type='array',maxItems=16,items={'$ref':'#/components/schemas/WorkbookBinding'},description='Inert attachments; reading them never runs code')
    schemas['BindingNotice']=dict(type='object',required=['binding_id','function','reason'],properties=dict(binding_id=binding_id,function=dict(type='string'),reason=dict(type='string',enum=['BINDING_NOT_APPROVED','BINDING_UNAVAILABLE'])),description='An optional binding was skipped; the operation still committed')
    schemas['BindingDiagnostics']=dict(type='object',required=['binding_id','module','function'],properties=dict(binding_id=binding_id,module=dict(type='string'),function=dict(type='string'),
        traceback=dict(type='string',maxLength=8192),logs=dict(type='string',maxLength=8192,description='Last 8 KiB UTF-8 stdout/stderr'),context_code=dict(type='string')),
        description='Names the binding that rejected the operation with BINDING_FAILED (409), BINDING_TIMEOUT (408), BINDING_NOT_APPROVED (403) or BINDING_UNAVAILABLE (409).')
    schemas['Receipt']['properties'].update(binding_id=binding_id,binding_notices=dict(type='array',items={'$ref':'#/components/schemas/BindingNotice'}),
        broken_names=dict(type='array',items=dict(type='string'),description='Names broken by this structural change'),
        broken_bindings=dict(type='array',items=dict(type='string'),description='Bindings, as "<module>.<function>", whose scope this structural change broke'))
    schemas['Error']['properties']['binding']={'$ref':'#/components/schemas/BindingDiagnostics'}
