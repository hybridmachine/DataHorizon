def on_structure(context,event):
    assert event['phase'] in ('before','after')
    catalog=event['before'] if event['phase']=='before' else event['after']
    if catalog:
        context.set(catalog[0]['id'],1,8,event['phase']+':'+event['action'],kind='text')
    if event['phase']=='after' and event['arguments'].get('name')=='Reject':
        raise ValueError('Reject complete structure transaction')


def on_change(context,changes):
    for change in changes:
        if change['after'] and change['after']['source']=='block':
            raise ValueError('Reject generated/input change')
