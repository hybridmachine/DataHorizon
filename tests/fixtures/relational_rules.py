# SPDX-License-Identifier: GPL-3.0-only
"""Explicitly activated acceptance fixture; these are user rules, not product behavior."""

def on_change(context,changes):
    pass


def on_structure(context,event):
    pass


def on_table_structure(context,event):
    pass


def on_table_change(context,changes):
    for change in changes:
        if change['after'] is None:continue
        schema=context.table_schema(change['table'])
        if schema['name']!='Orders':continue
        quantity=next(column['id'] for column in schema['columns'] if column['name']=='Quantity')
        if change['after'][quantity]<=0:raise ValueError('Fixture rule: Quantity must be positive')
        if change['action'] in ('table_update','table_insert','table_promote'):
            context.set('Automation',1,1,'Valid order edit: '+change['row_id'])
