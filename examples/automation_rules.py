# SPDX-License-Identifier: GPL-3.0-only
"""A fixture rule supplied by the workbook author, not application behavior."""
def on_change(ctx,changes):pass
def on_structure(ctx,event):pass
def on_table_structure(ctx,event):pass
def on_code_structure(ctx,event):pass


def on_table_change(ctx,changes):
    for change in changes:
        if change['after'] is None:continue
        schema=ctx.table_schema(change['table'])
        if schema['name']!='Measurements':continue
        column=next(item['id'] for item in schema['columns'] if item['name']=='Value')
        if change['after'][column]<0:raise ValueError('Example rule: Value must be nonnegative')
        if change['action'] not in ('undo','redo'):ctx.set('Sheet1',1,2,'Validated','text')
