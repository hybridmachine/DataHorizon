"""OpenAPI contracts for the bounded durable-job resources."""

def install(schemas):
    ref=lambda name:{'$ref':'#/components/schemas/'+name}
    listing=schemas['ListRequest']['properties'];csv=schemas['CsvPreviewRequest']['properties']
    common={key:value for key,value in listing.items() if key not in ('sort','filters')}
    common.update(job_id={'type':'string','pattern':'^[A-Za-z0-9_-]{1,64}$'},
                  expected_state={'type':'string','minLength':1},
                  mode={'type':'string','enum':['atomic','batches'],'default':'atomic'},
                  batch_size={'type':'integer','minimum':1,'maximum':1000,'default':1000})
    def create(name,action,extra,required,example,**constraints):
        schemas[name]={'type':'object','title':name.removesuffix('Request'),'required':['job_id','expected_state',*required],
            'properties':{**common,'action':{'type':'string','enum':[action]},**extra},
            'example':{'job_id':'reviewed-job','expected_state':'from-workbook-info','action':action,**example},
            **constraints}
    create('EditJobRequest','edit',{},[],{},
           description='Stage edits only; append 1–1,000 cells, seal, inspect and explicitly start. Omitted action defaults to edit.',
           **{'not':{'required':['source_path']}})
    csv_options={key:csv[key] for key in ('encoding','delimiter','column_types','header','locale')}
    csv_options['delimiter']={'type':'string','minLength':1,'maxLength':1,'pattern':'^[^\r\n\u0000"]$','default':','}
    csv_options.update(source_path={'type':'string','minLength':1,'description':'Server-local CSV file, at most 64 MiB; streamed preparation never writes native inputs.'},row=listing['r1'],col=listing['c1'])
    create('CsvJobRequest','edit',csv_options,['source_path'],
           {'source_path':'/tmp/reviewed.csv','mode':'batches','column_types':{'1':'number'},'locale':'en_US'})
    create('FindJobRequest','find',{'needle':{'type':'string','minLength':1},'field':{'type':'string','enum':['source','display'],'default':'source'}},
           ['action','needle'],{'needle':'reviewed text','r2':1000},**{'not':{'required':['source_path']}})
    create('ListJobRequest','list',{key:listing[key] for key in ('sort','filters')},['action'],
           {'r2':1000,'sort':[{'col':1,'descending':False}]},**{'not':{'required':['source_path']}})
    schemas['OperationJobRequest']={'oneOf':[ref(name) for name in ('EditJobRequest','CsvJobRequest','FindJobRequest','ListJobRequest')],
        'description':'Disk-staged plans; no writes until explicit execution. Atomic: 10,000 cells/3 MiB. Batches: 100,000 cells/64 MiB, at most 1,000 attempts. Read scope: 100,000 cells/256 columns. State, combined bounds and CSV-width checks also apply at runtime. Additional extension fields remain accepted where the service ignores them.'}
    schemas['StagedCellEdit']={**schemas['CellEdit'],'additionalProperties':False}
    schemas['AppendJobRequest']={'type':'object','required':['cells'],'properties':{'cells':{'type':'array','minItems':1,'maxItems':1000,'items':ref('StagedCellEdit')}},
        'description':'At most 1 MiB per upload; unique coordinates across the staged plan.',
        'example':{'cells':[{'row':1,'col':1,'source':'10','kind':'number'}]}}
    execution={'type':'string','pattern':'^[0-9a-f]{32}$','description':'From inspected v2 journal; prevents retargeting a recreated friendly job ID.'}
    for action in ('Start','Resume'):
        schemas[action+'JobRequest']={'type':'object','required':['expected_state','plan_digest'],
            'properties':{'expected_state':common['expected_state'],'plan_digest':{'type':'string','pattern':'^[0-9a-f]{64}$'},'execution_id':execution},
            'description':('Start unchanged prepared inputs.' if action=='Start' else 'Explicitly accept reviewed current inputs after native receipt reconciliation; never replay unknown writes.')+' Replace example state/digest/execution tokens with inspected response fields. After a lost reply inspect the retained ID; do not automatically repeat execution.',
            'example':{'expected_state':'from-current-workbook-info','plan_digest':'0'*64,'execution_id':'0'*32}}
    schemas['JobPageRequest']={'type':'object','properties':{'offset':{'type':'integer','minimum':0,'maximum':100000,'default':0},'limit':{'type':'integer','minimum':1,'maximum':256,'default':100}},'example':{'offset':0,'limit':100}}
    schemas['CancelJobRequest']={'type':'object','properties':{'execution_id':execution},'example':{},'description':'Cooperative cancellation; running is an acknowledgement, not proof of termination. Retain earlier committed receipts.'}
    for name in ('SealJobRequest','ReleaseJobRequest'):
        schemas[name]={'type':'object','properties':{},'example':{},'description':'Seal only after staging completes.' if name.startswith('Seal') else 'Release terminal sidecar storage only; native receipts remain. Unknown admitted attempts block release.'}
    schemas['OperationJobAction']={'type':'object','description':'Compatibility alias. Use each action resource’s specific request schema.'}
    schemas['JobBatch']={'type':'object','required':['operation_id','start','count','receipt'],'properties':{
        'operation_id':{'type':'string'},'start':{'type':'integer','minimum':0},'count':{'type':'integer','minimum':0,'maximum':10000},
        'receipt':{'type':'object','nullable':True,'properties':{'operation_id':{'type':'string'},'outcome':{'type':'string','enum':['committed','rejected','rolled_back','unknown','not_admitted']},'original_receipt':{'type':'object'},'original_fingerprint':{'type':'string'}},'description':'Null until reconciliation. not_admitted is sidecar evidence of a legacy fingerprint conflict, never a fabricated native rollback receipt.'}}}
    schemas['OperationJobStatus']={'type':'object','required':['job_id','status'],'properties':{
        'job_id':common['job_id'],'execution_id':execution,'version':{'type':'integer','enum':[1,2]},
        'status':{'type':'string','enum':['staging','prepared','running','completed','cancelled','failed','interrupted','stale','unknown']},
        'expected_state':common['expected_state'],'plan_digest':{'type':'string','nullable':True},'phase':{'type':'string'},
        'next':{'type':'integer','minimum':0},'total':{'type':'integer','minimum':0},'processed':{'type':'integer','minimum':0},'committed_batches':{'type':'integer','minimum':0},
        'batches':{'type':'array','maxItems':1000,'items':ref('JobBatch')},'plan':{'type':'object'},'preview':{'type':'array','items':ref('StagedCellEdit')},
        'results':{'type':'array','maxItems':256,'items':{'type':'object'}},'offset':{'type':'integer'},'has_more':{'type':'boolean'},'pending_operation':{'type':'string'},
        'clock':{'type':'string','description':'Recorded UTC civil clock.'},'error':ref('Error'),'report':{'type':'array','items':{'type':'string'}}},
        'description':'Job lifecycle differs from native operation outcome; inspect batch receipts. Four journals per native file; pages at most 4 MiB. Changed read inputs withhold frozen results.'}
    schemas['JobReleased']={'type':'object','required':['released','job_id'],'properties':{'released':{'type':'boolean','enum':[True]},'job_id':common['job_id']}}


def request_schema(path):
    if path.endswith('/operation-jobs'):return 'OperationJobRequest'
    return {'append':'AppendJobRequest','seal':'SealJobRequest','status':'JobPageRequest','start':'StartJobRequest','resume':'ResumeJobRequest','cancel':'CancelJobRequest','release':'ReleaseJobRequest'}[path.rsplit('/',1)[-1]]
