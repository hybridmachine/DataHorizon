# SPDX-License-Identifier: GPL-3.0-only
"""OpenAPI contract for inert workbook code and explicit atomic invocation."""


def install(schemas,actions):
    name=dict(type='string',pattern='^[A-Za-z][A-Za-z0-9_]{0,63}$')
    digest=dict(type='string',pattern='^[0-9a-f]{64}$')
    scalar={'oneOf':[dict(type='string',nullable=True),dict(type='number'),dict(type='boolean')]}
    declaration=dict(type='object',additionalProperties=False,required=['type'],properties={'type':dict(type='string',enum=['text','integer','number','boolean','date','time','datetime']),'nullable':dict(type='boolean',default=False)})
    parameter={**declaration,'required':['name','type'],'properties':{**declaration['properties'],'name':name,'default':scalar}}
    macro=dict(type='object',additionalProperties=False,required=['name','result'],properties={'name':name,'parameters':dict(type='array',maxItems=32,items=parameter),'result':declaration})
    properties=dict(name=name,source=dict(type='string',maxLength=131072,description='At most 128 KiB of UTF-8; static validation never executes source'),macros=dict(type='array',maxItems=32,items=macro),fingerprint=digest,module=dict(type='string',minLength=1))
    example=dict(name='Examples',source='def double(ctx, amount):\n    return amount * 2\n',macros=[dict(name='double',parameters=[dict(name='amount',type='integer')],result=dict(type='integer'))],fingerprint='0'*64)
    actions['module_save']=(['name','source','fingerprint'],properties,example)
    actions['module_delete']=(['module','fingerprint'],dict(module=properties['module'],fingerprint=digest),dict(module='Examples',fingerprint='0'*64))
    actions['macro_run']=(['module','macro','fingerprint'],dict(module=properties['module'],macro=name,fingerprint=digest,arguments=dict(type='object',additionalProperties=scalar,description='Exact typed scalars; at most 128 KiB. Unknown/missing arguments reject.')),dict(module='Examples',macro='double',fingerprint='0'*64,arguments=dict(amount=3)))
    # The SDK's common sheet argument is ignored by workbook-scoped operations.
    for action in ('module_save','module_delete','macro_run'):actions[action][1]['sheet']=dict(type='string')
    schemas['CodeReadRequest']=dict(type='object',additionalProperties=False,required=['expected_state'],properties=dict(expected_state=dict(type='string',minLength=1),module=properties['module']),example=dict(expected_state='copy-from-workbook-info'))
    schemas['WorkbookModule']=dict(type='object',additionalProperties=False,required=['id','name','source','macros'],properties={key:value for key,value in properties.items() if key not in ('module','fingerprint')}|dict(id=dict(type='string',format='uuid')))
    schemas['CodeCatalog']=dict(type='object',required=['state','fingerprint','activation_key','approved','modules'],properties=dict(state=dict(type='string'),fingerprint=digest,activation_key=dict(type='string'),approved=dict(type='boolean'),modules=dict(type='array',maxItems=16,items={'$ref':'#/components/schemas/WorkbookModule'})),description='Execution grants are service-local: set at start-up, or added by the owning process over its private channel when the service was started with one. There is no REST grant endpoint and no request field can grant. Opening/inspection never runs code.')
    schemas['MacroDiagnostics']=dict(type='object',required=['module','name','fingerprint','logs','execution'],properties=dict(module=dict(type='string',format='uuid'),name=name,fingerprint=digest,logs=dict(type='string',maxLength=8192,description='Last 8 KiB UTF-8 stdout/stderr, including on worker failure'),traceback=dict(type='string',maxLength=8192),context_code=dict(type='string'),elapsed_ms=dict(type='number',minimum=0),execution=dict(type='string',enum=['running','completed','failed'])))
    schemas['Receipt']['properties'].update(macro={'$ref':'#/components/schemas/MacroDiagnostics'},macro_result=scalar,module_id=dict(type='string',format='uuid'),code_fingerprint=digest)
    schemas['Error']['properties']['macro']={'$ref':'#/components/schemas/MacroDiagnostics'}
