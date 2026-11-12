# SPDX-License-Identifier: GPL-3.0-only
"""Records the workbook_import structure event in Sheet1!AD1, vetoes a sheet named Veto and refuses any cell change event
other than its own marker, so a test sees that imported cells raise no change events."""
import json


def on_structure(context, event):
    if event['action'] != 'workbook_import': return
    marker = event['before'][0]['id']
    if event['phase'] == 'before':
        record = {'phases': ['before'], **event['arguments']}
    else:
        if any(sheet['name'] == 'Veto' for sheet in event['arguments']['sheets']):
            raise ValueError('Veto the whole import')
        record = json.loads(context.get(marker, 1, 30)['source'])
        record['phases'].append('after')
    context.set(marker, 1, 30, json.dumps(record, sort_keys=True), kind='text')


def on_change(context, changes):
    for change in changes:
        if (change['row'], change['col']) != (1, 30):
            raise ValueError('An imported cell raised a change event')
