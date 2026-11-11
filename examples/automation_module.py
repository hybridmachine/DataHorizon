# SPDX-License-Identifier: GPL-3.0-only
"""Example workbook source. Storage never executes this module."""


def adjust(ctx,row_id,delta):
    schema=ctx.table_schema('Measurements')
    column=next(item['id'] for item in schema['columns'] if item['name']=='Value')
    old=ctx.table_get('Measurements',row_id)['values'][column]
    new=old+delta
    ctx.table_set('Measurements',row_id,{'Value':new})
    ctx.set('Sheet1',1,1,'Updated to '+str(new),'text')
    print('Adjusted record',row_id,'from',old,'to',new)
    return new


def spin(ctx):
    print('Slow example: cancel this operation by its operation ID')
    while True:pass
